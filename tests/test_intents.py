"""Intent routing, mapping and the confidence policy (spec sections 4, 23)."""
from __future__ import annotations

import unittest

from config import CONFIDENCE_THRESHOLD, INTENT_MAPPING, Action, Intent
from dialog_manager.intent_router import IntentRouter
from tests.helpers import StubIntentDetector, build_manager


class FixedDetector:
    """Always returns the same label and confidence."""

    def __init__(self, intent: str, confidence: float):
        self.intent, self.confidence = intent, confidence

    def predict(self, text):
        return {"intent": self.intent, "confidence": self.confidence}


class TestIntentMapping(unittest.TestCase):
    def test_every_trained_label_is_mapped(self):
        """
        The 31 labels the model emits must all appear in INTENT_MAPPING,
        otherwise a real prediction would silently become UNKNOWN.
        """
        import json
        from pathlib import Path
        from config import INTENT_MODULE_DIR

        label_file = INTENT_MODULE_DIR / "data" / "label2id.json"
        if not label_file.exists():
            self.skipTest("intent model not trained in this checkout")
        labels = json.loads(label_file.read_text(encoding="utf-8"))
        unmapped = sorted(set(labels) - set(INTENT_MAPPING))
        self.assertEqual(unmapped, [], f"labels missing from mapping: {unmapped}")

    def test_mbert_label_maps_to_canonical_intent(self):
        router = IntentRouter(detector=FixedDetector("appointment_status", 0.9))
        self.assertEqual(router.route("when is it").intent,
                         Intent.CHECK_APPOINTMENT)

    def test_change_date_maps_to_reschedule(self):
        router = IntentRouter(detector=FixedDetector("change_date", 0.9))
        self.assertEqual(router.route("move it").intent,
                         Intent.RESCHEDULE_APPOINTMENT)

    def test_unmapped_label_becomes_unknown_not_a_crash(self):
        router = IntentRouter(detector=FixedDetector("brand_new_label", 0.99))
        self.assertEqual(router.route("hello").intent, Intent.UNKNOWN)

    def test_topic_is_exposed_for_information_intents(self):
        router = IntentRouter(detector=FixedDetector("doctor_fee", 0.9))
        self.assertEqual(router.route("what is the fee").topic, "fee")


class TestConfidencePolicy(unittest.TestCase):
    def test_below_threshold_is_unknown(self):
        router = IntentRouter(detector=FixedDetector("book_appointment", 0.4))
        result = router.route("mumble mumble")
        self.assertTrue(result.below_threshold)
        self.assertEqual(result.intent, Intent.UNKNOWN)
        # The raw prediction is still reported, for logging and debugging.
        self.assertEqual(result.raw_intent, "book_appointment")

    def test_above_threshold_is_used(self):
        router = IntentRouter(
            detector=FixedDetector("book_appointment", CONFIDENCE_THRESHOLD + 0.1))
        self.assertEqual(router.route("book me in").intent,
                         Intent.BOOK_APPOINTMENT)

    def test_model_crash_degrades_to_unknown(self):
        class Broken:
            def predict(self, text):
                raise RuntimeError("model exploded")

        router = IntentRouter(detector=Broken())
        result = router.route("hello")
        self.assertEqual(result.intent, Intent.UNKNOWN)
        self.assertEqual(result.confidence, 0.0)

    def test_empty_text_is_unknown(self):
        router = IntentRouter(detector=StubIntentDetector())
        self.assertEqual(router.route("   ").intent, Intent.UNKNOWN)


class TestLowConfidenceBehaviour(unittest.TestCase):
    """Spec section 23: never touch the database on a low-confidence turn."""

    def test_low_confidence_asks_for_clarification(self):
        manager, repo, _ = build_manager()
        result = manager.process_message("s", "asdf qwerty zxcv")
        self.assertEqual(result["action"], Action.CLARIFY)
        self.assertEqual(result["intent"], Intent.UNKNOWN)

    def test_low_confidence_creates_no_appointment(self):
        manager, repo, _ = build_manager()
        before = len(repo.query("appointments"))
        manager.process_message("s", "blah blah nonsense")
        self.assertEqual(len(repo.query("appointments")), before)

    def test_repeated_failure_falls_back_to_an_explicit_menu(self):
        manager, _, _ = build_manager()
        manager.process_message("s", "nonsense one")
        manager.process_message("s", "nonsense two")
        third = manager.process_message("s", "nonsense three")
        # After repeated failures the wording becomes an explicit list.
        self.assertIn("book an appointment", third["response"].lower())


class TestConfidenceBands(unittest.TestCase):
    """
    Three bands, not one cut-off (spec section 15).

    A single threshold means a 0.61 guess is acted on exactly as readily as a
    0.99 one - which is how a caller ends up with an appointment cancelled on
    the strength of a coin flip.
    """

    def test_bands_are_reported(self):
        for confidence, expected in ((0.95, "high"), (0.72, "medium"),
                                     (0.45, "low")):
            with self.subTest(confidence=confidence):
                router = IntentRouter(
                    detector=FixedDetector("book_appointment", confidence))
                self.assertEqual(router.route("x").band, expected)

    def test_only_the_high_band_is_confident(self):
        high = IntentRouter(detector=FixedDetector("book_appointment", 0.9))
        medium = IntentRouter(detector=FixedDetector("book_appointment", 0.7))
        self.assertTrue(high.route("x").is_confident)
        self.assertFalse(medium.route("x").is_confident)
        # Medium is still a KNOWN intent - it is above the action threshold.
        self.assertTrue(medium.route("x").is_known)


class TestMediumConfidencePolicy(unittest.TestCase):
    """A mid-band guess is checked against context before it is acted on."""

    def _manager(self, intent, confidence):
        from firebase.firebase_config import LocalRepository, reset_repository
        from firebase.seed_data import seed
        from dialog_manager.dialog_manager import DialogManager

        repo = LocalRepository()
        reset_repository(repo)
        seed(repo, with_sample_appointment=False)
        return DialogManager(
            intent_router=IntentRouter(detector=FixedDetector(intent, confidence)),
            repository=repo, deterministic_responses=True), repo

    def test_high_confidence_acts_immediately(self):
        manager, _ = self._manager("cancel_appointment", 0.93)
        result = manager.process_message("s", "kuch karna hai", "P001")
        self.assertEqual(result["action"], Action.ASK_FOR_APPOINTMENT_ID)

    def test_medium_confidence_without_context_asks_first(self):
        manager, _ = self._manager("cancel_appointment", 0.68)
        result = manager.process_message("s", "kuch karna hai", "P001")
        self.assertEqual(result["action"], Action.CONFIRM_INTENT)

    def test_medium_confidence_with_context_proceeds(self):
        """An entity in the utterance is corroboration enough."""
        manager, _ = self._manager("book_appointment", 0.68)
        result = manager.process_message("s", "Dr Ahmed se kal 4 baje", "P001")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)

    def test_saying_yes_to_the_check_starts_the_task(self):
        manager, _ = self._manager("cancel_appointment", 0.68)
        manager.process_message("s", "kuch karna hai", "P001")
        result = manager.process_message("s", "haan", "P001")
        self.assertEqual(result["action"], Action.ASK_FOR_APPOINTMENT_ID)

    def test_saying_no_to_the_check_abandons_the_guess(self):
        manager, repo = self._manager("cancel_appointment", 0.68)
        manager.process_message("s", "kuch karna hai", "P001")
        result = manager.process_message("s", "nahi", "P001")
        self.assertEqual(result["action"], Action.CLARIFY)
        self.assertIsNone(result["session_state"]["intent"])

    def test_an_emergency_is_never_delayed_by_a_check(self):
        """Confirming an emergency guess would waste the seconds that matter."""
        manager, _ = self._manager("emergency", 0.68)
        result = manager.process_message("s", "jaldi", "P001")
        self.assertEqual(result["action"], Action.ESCALATE)

    def test_medium_confidence_writes_nothing_to_the_database(self):
        manager, repo = self._manager("cancel_appointment", 0.68)
        before = len(repo.query("appointments"))
        manager.process_message("s", "kuch karna hai", "P001")
        self.assertEqual(len(repo.query("appointments")), before)


if __name__ == "__main__":
    unittest.main()
