"""
Roman-Urdu intent regression tests.

The reported failure:

    "Mje docter ke pas jana h"  ->  change_doctor (0.41)  ->  unknown_intent

Two layers were added in response, and both are tested here:

  * training-data augmentation, so the classifier itself learns the spelling
    variants (tested against the real model, skipped when it is absent);
  * a semantic corroboration layer, which reads the sentence when the
    classifier is unsure - tested with fakes so it runs everywhere.

The semantic tests matter most: they are the guarantee that the fallback fires
on booking language and, just as importantly, declines on everything else.
"""
from __future__ import annotations

import unittest

from config import Intent
from dialog_manager.intent_router import IntentRouter
from dialog_manager.semantic_fallback import (
    appointment_evidence,
    corroborate_booking,
)


class FakeResult:
    """Minimal stand-in for IntentResult."""

    def __init__(self, intent=Intent.CHANGE_DOCTOR if hasattr(Intent, "CHANGE_DOCTOR")
                 else Intent.RESCHEDULE_APPOINTMENT,
                 confidence=0.41, band="low"):
        self.intent = intent
        self.confidence = confidence
        self._band = band
        self.raw_intent = "change_doctor"

    @property
    def band(self):
        return self._band


class FixedDetector:
    def __init__(self, intent, confidence):
        self.intent, self.confidence = intent, confidence

    def predict(self, text):
        return {"intent": self.intent, "confidence": self.confidence}


# --------------------------------------------------------------------------
# The evidence reader
# --------------------------------------------------------------------------
class TestAppointmentEvidence(unittest.TestCase):
    """Subject + action + no blocker. Never any one of those alone."""

    BOOKING = [
        "Mje docter ke pas jana h",
        "mje doctor ke pas jana h",
        "mujhe doctor ke paas jana hai",
        "mje dr ke pas jana h",
        "muje dr k pas jana h",
        "doctor ke pas jana hai",
        "docter ko dikhana h",
        "mje doctor ko dikhana h",
        "mujhe doctor se milna hai",
        "doctor se milna h",
        "mje doctor se check karwana h",
        "mujhe doctor ka time chahiye",
        "doctor ki appointment chahiye",
        "mje appointment chahiye",
        "mera time laga dein",
    ]

    # Same subject word, different intent - the fallback must stay out.
    NOT_BOOKING = [
        ("doctor ki fee kitni hai?", "fee"),
        ("mje doctor ki fee batao", "fee"),
        ("doctor ki qualification kya hai?", "qualification"),
        ("mje doctor ki degree batao", "qualification"),
        ("doctor available hain?", "availability"),
        ("dr ahmed kal clinic mein honge?", "availability"),
        ("doctor ka address kya hai?", "location"),
        ("doctor change karna hai", "change_doctor"),
        ("mje doctor badalna hai", "change_doctor"),
        ("dr ahmed ki jagah dr ali chahiye", "change_doctor"),
        ("appointment cancel karni hai", "cancel"),
        ("mera appointment cancel kar dein", "cancel"),
        ("appointment reschedule kar dein", "reschedule"),
        ("meri appointment kab hai", "status"),
        # Booking-shaped sentences that are really emergencies.
        ("mujhe abhi doctor ke paas jana hai seene mein dard hai", "emergency"),
        ("doctor ke paas jana hai saans nahi aa rahi", "emergency"),
        ("abbu behosh ho gaye doctor ke paas jana hai", "emergency"),
        ("accident hua hai doctor ko dikhana hai", "emergency"),
    ]

    def test_booking_language_is_recognised(self):
        for text in self.BOOKING:
            with self.subTest(text=text):
                evidence = appointment_evidence(text)
                self.assertTrue(evidence["supports_booking"],
                                f"{text!r} -> {evidence}")

    def test_other_intents_are_blocked(self):
        """
        This is the test that stops the fallback becoming a keyword hack.

        A naive `if "doctor" in text` would claim every one of these.
        """
        for text, expected_blocker in self.NOT_BOOKING:
            with self.subTest(text=text):
                evidence = appointment_evidence(text)
                self.assertFalse(evidence["supports_booking"],
                                 f"{text!r} wrongly read as booking")
                self.assertIn(expected_blocker, evidence["blockers"])

    def test_a_subject_alone_is_not_enough(self):
        self.assertFalse(appointment_evidence("doctor")["supports_booking"])

    def test_an_action_alone_is_not_enough(self):
        self.assertFalse(appointment_evidence("jana hai")["supports_booking"])

    def test_unrelated_text_is_not_booking(self):
        for text in ("hello", "shukriya", "", "asdf qwerty"):
            self.assertFalse(appointment_evidence(text)["supports_booking"])


# --------------------------------------------------------------------------
# When the fallback is allowed to act
# --------------------------------------------------------------------------
class TestCorroborationPolicy(unittest.TestCase):
    def test_low_confidence_is_corrected(self):
        result = FakeResult(Intent.RESCHEDULE_APPOINTMENT, 0.41, "low")
        self.assertEqual(
            corroborate_booking("Mje docter ke pas jana h", result),
            Intent.BOOK_APPOINTMENT)

    def test_medium_confidence_is_corrected(self):
        result = FakeResult(Intent.RESCHEDULE_APPOINTMENT, 0.68, "medium")
        self.assertEqual(
            corroborate_booking("mje doctor ke pas jana h", result),
            Intent.BOOK_APPOINTMENT)

    def test_high_confidence_is_left_alone(self):
        """
        The bands stay in charge. Overriding a confident classifier would make
        the fallback, not the model, the arbiter of intent.
        """
        result = FakeResult(Intent.RESCHEDULE_APPOINTMENT, 0.91, "high")
        self.assertIsNone(
            corroborate_booking("mje doctor ke pas jana h", result))

    def test_emergency_is_never_intercepted(self):
        result = FakeResult(Intent.EMERGENCY, 0.55, "low")
        self.assertIsNone(
            corroborate_booking("doctor ke pas jana hai abhi", result))

    def test_emergency_wording_is_never_turned_into_a_booking(self):
        """
        The classifier may mislabel an emergency as something mundane. The
        fallback must not compound that by booking a routine appointment for
        a caller with chest pain.
        """
        result = FakeResult(Intent.RESCHEDULE_APPOINTMENT, 0.45, "low")
        self.assertIsNone(corroborate_booking(
            "mujhe doctor ke paas jana hai seene mein dard hai", result))

    def test_an_already_correct_prediction_is_untouched(self):
        result = FakeResult(Intent.BOOK_APPOINTMENT, 0.55, "low")
        self.assertIsNone(
            corroborate_booking("mje doctor ke pas jana h", result))

    def test_it_can_be_switched_off(self):
        router = IntentRouter(detector=FixedDetector("change_doctor", 0.41),
                              use_semantic_fallback=False)
        self.assertNotEqual(router.route("mje doctor ke pas jana h").intent,
                            Intent.BOOK_APPOINTMENT)

    def test_the_router_marks_a_corroborated_result(self):
        router = IntentRouter(detector=FixedDetector("change_doctor", 0.41))
        result = router.route("mje doctor ke pas jana h")
        self.assertEqual(result.intent, Intent.BOOK_APPOINTMENT)
        self.assertTrue(result.corroborated)
        # The raw prediction is still reported, for logs and debugging.
        self.assertEqual(result.raw_intent, "change_doctor")


# --------------------------------------------------------------------------
# Against the real model
# --------------------------------------------------------------------------
class TestAgainstTrainedModel(unittest.TestCase):
    """
    Skipped when the model is not present, so the suite still runs on a fresh
    checkout. Loading mBERT takes seconds, so this is one shared instance.
    """

    router = None

    @classmethod
    def setUpClass(cls):
        from config import INTENT_MODEL_DIR
        if not (INTENT_MODEL_DIR / "config.json").exists():
            raise unittest.SkipTest("intent model not trained in this checkout")
        cls.router = IntentRouter()

    def test_the_reported_failure(self):
        """The exact sentence from the bug report."""
        result = self.router.route("Mje docter ke pas jana h")
        self.assertEqual(result.intent, Intent.BOOK_APPOINTMENT)

    def test_roman_urdu_variations(self):
        examples = [
            "mje doctor ke pas jana h",
            "mujhe doctor ke paas jana hai",
            "mje dr ke pas jana h",
            "doctor ko dikhana h",
            "mje doctor se milna h",
            "doctor ka time chahiye",
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(self.router.route(text).intent,
                                 Intent.BOOK_APPOINTMENT)

    def test_neighbouring_intents_are_not_damaged(self):
        """The fix must not drag other intents into book_appointment."""
        cases = [
            ("doctor ki fee kitni hai?", Intent.DOCTOR_INFORMATION),
            ("doctor ki qualification kya hai?", Intent.DOCTOR_INFORMATION),
            ("appointment cancel karni hai", Intent.CANCEL_APPOINTMENT),
            ("mera appointment reschedule kar dein",
             Intent.RESCHEDULE_APPOINTMENT),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.router.route(text).intent, expected)

    def test_change_doctor_is_still_distinguishable(self):
        """
        "doctor ke pas jana" and "doctor badalna" must not collapse together -
        confusing them was the original bug.
        """
        for text in ("doctor change karna hai", "mje doctor badalna hai"):
            with self.subTest(text=text):
                result = self.router.route(text)
                self.assertNotEqual(result.intent, Intent.BOOK_APPOINTMENT,
                                    f"{text!r} became a booking request")


if __name__ == "__main__":
    unittest.main()
