"""
Intent router - the bridge between the trained mBERT model and the dialog logic.

Responsibilities:
  1. call the existing mBERT intent detector (never re-implement one),
  2. apply the confidence policy,
  3. translate the model's label names into canonical dialog intents.

Step 3 is what lets the model be retrained, renamed or replaced without a
single edit to the Dialog Manager: only `INTENT_MAPPING` in config.py changes.
"""
from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

from dialog_manager.semantic_fallback import corroborate_booking, escalate_emergency

from config import (
    EMERGENCY_SAFETY_NET,
    CONFIDENCE_THRESHOLD,
    HIGH_CONFIDENCE,
    INFO_TOPIC,
    INTENT_MAPPING,
    INTENT_MODEL_DIR,
    Intent,
    add_intent_module_to_path,
)

logger = logging.getLogger(__name__)


class IntentDetector(Protocol):
    """Anything that turns text into an intent + confidence."""

    def predict(self, text: str) -> dict[str, Any]:
        ...


@dataclass
class IntentResult:
    """One classification, already mapped into the dialog vocabulary."""
    intent: str                       # canonical Intent
    raw_intent: str                   # the label mBERT actually produced
    confidence: float
    below_threshold: bool
    topic: str | None = None          # fee / address / timing / ...
    top_k: list[dict[str, Any]] = field(default_factory=list)
    # True when the semantic layer, not the classifier, decided this intent.
    corroborated: bool = False
    # Set when the emergency safety net overrode the classifier.
    safety_override: bool = False

    @property
    def is_known(self) -> bool:
        return self.intent != Intent.UNKNOWN and not self.below_threshold

    @property
    def band(self) -> str:
        """"high" / "medium" / "low" - see config.HIGH_CONFIDENCE."""
        if self.confidence >= HIGH_CONFIDENCE:
            return "high"
        if not self.below_threshold:
            return "medium"
        return "low"

    @property
    def is_confident(self) -> bool:
        """Safe to act on without corroborating context."""
        return self.band == "high"


class MBertIntentDetector:
    """
    Thin adapter over `intent_detection/src/inference.py`.

    The model is loaded once, lazily: loading 178 M parameters per request
    would add seconds to every turn of a live phone call.
    """

    def __init__(self, model_dir=None, eager: bool = False):
        self.model_dir = model_dir or INTENT_MODEL_DIR
        self._predictor = None
        if eager:
            self._load()

    # Module names that exist in BOTH this project and intent_detection/src.
    # `config` in particular would otherwise resolve to our root config.py and
    # break the intent module's imports.
    _SHADOWED = ("config", "inference", "dataset", "preprocess")

    def _load(self):
        if self._predictor is not None:
            return self._predictor
        add_intent_module_to_path()

        # Import the intent module against ITS OWN `config`, then put ours
        # back. Without this swap `from config import MAX_LENGTH` inside
        # inference.py picks up this project's config.py and fails.
        saved = {name: sys.modules.pop(name)
                 for name in self._SHADOWED if name in sys.modules}
        try:
            inference = importlib.import_module("inference")
            predictor_cls = inference.IntentPredictor
        except Exception as exc:                            # pragma: no cover
            raise RuntimeError(
                "Could not import the intent detection module from "
                f"{self.model_dir}. Train it first: "
                "python intent_detection/src/train.py") from exc
        finally:
            for name in self._SHADOWED:
                sys.modules.pop(name, None)
            sys.modules.update(saved)

        # threshold=0.0 so the *Dialog Manager* owns the confidence policy;
        # otherwise the intent module would hide the raw prediction from us.
        self._predictor = predictor_cls(model_dir=self.model_dir, threshold=0.0)
        logger.info("mBERT loaded from %s on %s",
                    self.model_dir, self._predictor.device)
        return self._predictor

    @property
    def is_loaded(self) -> bool:
        return self._predictor is not None

    def predict(self, text: str) -> dict[str, Any]:
        result = self._load().predict(text)
        return {
            "intent": result["raw_intent"],
            "confidence": float(result["confidence"]),
            "top_k": result.get("top_k", []),
        }


class IntentRouter:
    """Applies the confidence threshold and the label mapping."""

    def __init__(self, detector: IntentDetector | None = None,
                 threshold: float = CONFIDENCE_THRESHOLD,
                 mapping: dict[str, str] | None = None,
                 use_semantic_fallback: bool = True,
                 use_emergency_safety_net: bool | None = None):
        self.detector = detector or MBertIntentDetector()
        self.threshold = threshold
        self.mapping = mapping or INTENT_MAPPING
        self.use_semantic_fallback = use_semantic_fallback
        self.use_emergency_safety_net = (EMERGENCY_SAFETY_NET if use_emergency_safety_net is None
                                         else use_emergency_safety_net)

    # ------------------------------------------------------------------
    def route(self, text: str) -> IntentResult:
        """Classify one utterance. Never raises - a model failure is UNKNOWN."""
        if not text or not text.strip():
            return IntentResult(Intent.UNKNOWN, "empty", 0.0, True)

        try:
            prediction = self.detector.predict(text)
        except Exception as exc:                            # pragma: no cover
            # A model crash must not end the phone call.
            logger.error("intent detection failed: %s", exc)
            return IntentResult(Intent.UNKNOWN, "error", 0.0, True)

        raw = prediction.get("intent", "unknown")
        confidence = float(prediction.get("confidence", 0.0))
        below = confidence < self.threshold

        mapped = self.mapping.get(raw)
        if mapped is None:
            # The model was retrained with new label names and config
            # INTENT_MAPPING was not updated: loud in the log, safe at runtime.
            logger.warning("mBERT label %r is not in INTENT_MAPPING", raw)
            mapped = Intent.UNKNOWN

        result = IntentResult(
            intent=Intent.UNKNOWN if below else mapped,
            raw_intent=raw,
            confidence=confidence,
            below_threshold=below,
            topic=INFO_TOPIC.get(raw),
            top_k=prediction.get("top_k", []),
        )

        # Semantic corroboration: an unsure classifier should not send an
        # obvious appointment request to "sorry, I did not understand". Only
        # fires on LOW/MEDIUM confidence, never on an emergency, and only when
        # the sentence carries subject + action and no competing vocabulary.
        # Emergency safety net first: an acute danger sign outranks everything,
        # including a confident wrong label.
        if self.use_emergency_safety_net and escalate_emergency(text, result):
            logger.warning("emergency safety net: %s (%.2f) -> emergency for %r",
                           result.raw_intent, confidence, text)
            result.intent = Intent.EMERGENCY
            result.below_threshold = False
            result.safety_override = True
            return result

        if self.use_semantic_fallback:
            corroborated = corroborate_booking(text, result)
            if corroborated is not None:
                logger.info("semantic fallback: %s (%.2f) -> %s for %r",
                            result.raw_intent, confidence, corroborated, text)
                result.intent = corroborated
                result.below_threshold = False
                result.corroborated = True
        return result

    def known_intents(self) -> list[str]:
        return sorted(set(self.mapping.values()))
