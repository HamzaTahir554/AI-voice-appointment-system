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

import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from dialog_manager.semantic_fallback import (corroborate_booking, correct_known_confusion,
                                               escalate_emergency)

from config import (
    EMERGENCY_SAFETY_NET,
    CONFIDENCE_THRESHOLD,
    HIGH_CONFIDENCE,
    INFO_TOPIC,
    INTENT_MAPPING,
    INTENT_MODEL_DIR,
    INTENT_SRC_DIR,
    Intent,
)

logger = logging.getLogger(__name__)
_LOAD_LOCK = threading.Lock()      # see IntentRouter._load


def _import_intent_inference(src_dir):
    """
    intent_detection/src/inference.py, loaded as a private module.

    The intent module is a sibling project with its own `config.py`, which
    inference.py imports as plain `config`. It used to be imported by putting
    src/ on sys.path and swapping sys.modules["config"] for the duration. That
    swap is process-wide: while torch imported (seconds), any other thread
    importing a project module got the intent module's `config` - the API
    loads mBERT on a background thread at start-up, and the test suite caught
    a module importing the wrong `config` in that window.

    Here each file of src/ is loaded under a private name ("_intent_src_config")
    and the modules loaded this way resolve their sibling imports to each
    other; sys.path and this project's `config` are never touched.
    """
    import builtins
    import importlib.util
    from pathlib import Path

    src_dir = Path(src_dir)
    siblings = {path.stem for path in src_dir.glob("*.py")}
    loaded: dict[str, Any] = {}

    def private_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name in siblings:
            if name not in loaded:
                loaded[name] = load(name)
            return loaded[name]
        return builtins.__import__(name, globals, locals, fromlist, level)

    def load(name):
        private = f"_intent_src_{name}"
        spec = importlib.util.spec_from_file_location(private, src_dir / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        # Every `import` the module runs - at load time or later inside a
        # function - goes through private_import.
        module.__dict__["__builtins__"] = {**builtins.__dict__, "__import__": private_import}
        sys.modules[private] = module          # a private name: shadows nothing
        spec.loader.exec_module(module)
        return module

    return load("inference")


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

    def _load(self):
        if self._predictor is not None:
            return self._predictor
        with _LOAD_LOCK:                     # two threads must not load it twice
            if self._predictor is None:
                self._load_locked()
        return self._predictor

    def _load_locked(self) -> None:
        try:
            predictor_cls = _import_intent_inference(INTENT_SRC_DIR).IntentPredictor
        except Exception as exc:                            # pragma: no cover
            raise RuntimeError(
                "Could not import the intent detection module from "
                f"{self.model_dir}. Train it first: "
                "python intent_detection/src/train.py") from exc

        # threshold=0.0 so the *Dialog Manager* owns the confidence policy;
        # otherwise the intent module would hide the raw prediction from us.
        self._predictor = predictor_cls(model_dir=self.model_dir, threshold=0.0)
        logger.info("mBERT loaded from %s on %s",
                    self.model_dir, self._predictor.device)

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
        # How long the last prediction took, per thread: a voice call reports
        # it as the mBERT stage of its latency.
        self._timing = threading.local()
        self.mapping = mapping or INTENT_MAPPING
        self.use_semantic_fallback = use_semantic_fallback
        self.use_emergency_safety_net = (EMERGENCY_SAFETY_NET if use_emergency_safety_net is None
                                         else use_emergency_safety_net)

    # ------------------------------------------------------------------
    @property
    def last_predict_ms(self) -> float | None:
        """The model's time for the last utterance classified in this thread."""
        return getattr(self._timing, "last_ms", None)

    def route(self, text: str) -> IntentResult:
        """Classify one utterance. Never raises - a model failure is UNKNOWN."""
        self._timing.last_ms = None
        if not text or not text.strip():
            return IntentResult(Intent.UNKNOWN, "empty", 0.0, True)

        started = time.perf_counter()
        try:
            prediction = self.detector.predict(text)
        except Exception as exc:                            # pragma: no cover
            # A model crash must not end the phone call.
            logger.error("intent detection failed: %s", exc)
            return IntentResult(Intent.UNKNOWN, "error", 0.0, True)

        self._timing.last_ms = (time.perf_counter() - started) * 1000
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
            # A known, confident mistake (a farewell read as thanks, a move
            # read as a new booking, availability asked in Urdu script).
            corrected = correct_known_confusion(text, result)
            if corrected is not None:
                logger.info("known confusion: %s (%.2f) -> %s for %r",
                            result.raw_intent, confidence, corrected, text)
                result.intent = corrected
                result.below_threshold = False
                result.corroborated = True
                return result
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
