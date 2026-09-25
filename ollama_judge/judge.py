"""
The Ollama Judge.

Takes a backend result and produces the sentence the caller hears. The LLM only
ever rephrases facts it was given; the flow is:

    backend result -> Ollama -> response validator -> approved text
                                      |
                                      +-- rejected -> deterministic fallback

So the worst case when the model misbehaves is a slightly stiffer sentence,
never a wrong one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ollama_judge.fallback import build_fallback_response
from ollama_judge.language import ENGLISH, detect_utterance_language
from ollama_judge.ollama_service import OllamaService
from ollama_judge.prompts import FEW_SHOT, JUDGE_SYSTEM_PROMPT, build_judge_input
from ollama_judge.response_validator import validate_llm_response

logger = logging.getLogger(__name__)

def detect_language(text: str) -> str:
    """Kept for backwards compatibility; see ollama_judge.language."""
    return detect_utterance_language(text)


@dataclass
class JudgeResult:
    """What the judge decided, plus how it got there."""
    response: str
    decision: str                  # approved | needs_correction | fallback
    source: str                    # ollama | fallback
    reason: str = ""
    validation_problems: list[str] = field(default_factory=list)
    llm_raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "response": self.response,
            "decision": self.decision,
            "source": self.source,
            "reason": self.reason,
            "validation_problems": self.validation_problems,
        }


class OllamaJudge:
    """Wraps the LLM with a validator and a deterministic safety net."""

    def __init__(self, service: OllamaService | None = None,
                 use_few_shot: bool = True):
        self.service = service or OllamaService()
        self.use_few_shot = use_few_shot

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.service.is_available()

    # ------------------------------------------------------------------
    def judge(self, user_text: str, intent: str, confidence: float,
              dialog_state: dict, backend_result,
              language: str | None = None) -> JudgeResult:
        """
        Produce the final spoken response for one turn.

        `backend_result` is an OperationResult. When it is None (no backend
        call was needed - a greeting, a clarification) the Dialog Manager's own
        wording is used and the judge is skipped entirely.
        """
        language = language or detect_utterance_language(user_text)
        # The deterministic sentence is built in the caller's language, so a
        # rejected or unavailable LLM still answers in Urdu / Roman Urdu.
        deterministic = build_fallback_response(backend_result, language)

        if backend_result is None:
            return JudgeResult(deterministic, "approved", "fallback",
                               "no backend operation for this turn")

        if not self.service.is_available():
            return JudgeResult(deterministic, "approved", "fallback",
                               "Ollama unavailable")

        prompt = build_judge_input(
            user_text=user_text, intent=intent, confidence=confidence,
            dialog_state=dialog_state,
            backend_result=backend_result.to_dict(), language=language)

        parsed = self.service.chat_json(
            JUDGE_SYSTEM_PROMPT, prompt,
            examples=FEW_SHOT if self.use_few_shot else None)

        if not parsed:
            return JudgeResult(deterministic, "approved", "fallback",
                               "model returned no usable JSON")

        candidate = str(parsed.get("response") or "").strip()
        decision = str(parsed.get("decision") or "approved").lower()
        reason = str(parsed.get("reason") or "")

        if not candidate:
            return JudgeResult(deterministic, "approved", "fallback",
                               reason or "model produced no sentence")

        # A small model will sometimes flag "needs_correction" because it
        # doubts the backend's data - which it has no standing to do. The
        # validator, not the model, decides whether a sentence is faithful, so
        # we still check the text rather than discarding it unread.
        if decision == "needs_correction":
            logger.info("model self-flagged needs_correction (%s); "
                        "validating its text anyway", reason)

        # The guard that actually enforces the rules the prompt asks for.
        report = validate_llm_response(candidate, backend_result,
                                       language)
        if not report.valid:
            return JudgeResult(
                deterministic, "needs_correction", "fallback",
                "validator rejected the generated response",
                validation_problems=report.problems, llm_raw=candidate)

        return JudgeResult(candidate, "approved", "ollama", reason,
                           llm_raw=candidate)

    # ------------------------------------------------------------------
    def rephrase(self, deterministic_text: str, user_text: str,
                 backend_result=None) -> str:
        """
        Make a Dialog Manager sentence sound natural, in the caller's language.

        Used for turns with no backend operation (asking for a missing slot,
        greeting, clarifying). Any doubt at all and the original is returned
        unchanged - the wording is cosmetic, so there is no reason to risk it.
        """
        if not self.service.is_available() or not deterministic_text:
            return deterministic_text

        language = detect_language(user_text)
        if language == "english":
            return deterministic_text          # nothing to gain

        system = (
            "You translate a clinic receptionist's sentence into the caller's "
            "language. Keep every name, number, date and time EXACTLY as given. "
            "Do not add or remove information. Keep it to one short sentence "
            "suitable for speaking aloud. "
            'Return ONLY JSON: {"response": "<translated sentence>"}')
        user = (f'Caller language: {language}\n'
                f'Sentence to translate: "{deterministic_text}"')

        parsed = self.service.chat_json(system, user)
        if not parsed:
            return deterministic_text
        candidate = str(parsed.get("response") or "").strip()
        if not candidate:
            return deterministic_text

        if backend_result is not None:
            report = validate_llm_response(candidate, backend_result,
                                       language)
            if not report.valid:
                return deterministic_text
        return candidate
