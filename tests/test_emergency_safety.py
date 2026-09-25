"""
Emergency safety net (system audit).

The finding: on sentences it had never seen, the intent model missed
emergencies - "Bohot khoon beh raha hai" came back thank_you 0.97, the Urdu
"this is an emergency" as confirm 0.36, and "ammi ki saans ruk rahi hai jaldi
kuch karein" fell into the low band, which would have answered a caller whose
mother had stopped breathing with "sorry, I did not understand".

The net escalates on acute danger PHRASES regardless of the label or its
confidence, never adds a confirmation question, and stands down for routine
framings (a blood test, an old pain, a checkup). Both directions are pinned.
"""
from __future__ import annotations

import unittest

from config import Action, Intent
from dialog_manager.intent_router import IntentRouter
from dialog_manager.semantic_fallback import emergency_evidence
from tests.helpers import build_manager


class Fixed:
    def __init__(self, label, confidence):
        self.label, self.confidence = label, confidence

    def predict(self, text):
        return {"intent": self.label, "confidence": self.confidence}


ACUTE = [
    "Bohot khoon beh raha hai",
    "ammi ki saans ruk rahi hai jaldi kuch karein",
    "saans nahi aa rahi",
    "abbu behosh ho gaye hain",
    "mujhe abhi doctor ke paas jana hai seene mein dard hai",
    "My father collapsed, he is not breathing",
    "I think it is a heart attack",
    "bachay ne zeher pee liya",
    "accident ho gaya hai",
    "Critical condition, need the doctor now",
    "یہ ایمرجنسی ہے",
    "سانس نہیں آ رہی",
    "سینے میں شدید درد ہے",
]

ROUTINE = [
    "khoon ka test karwana hai",
    "seene mein purana dard hai, checkup karwana hai",
    "mujhe sar dard hai doctor ko dikhana hai",
    "awaz kat rahi hai phir se boliye",
    "doctor ki fee kitni hai?",
    "meri report dikhani hai doctor ko",
    "Mje docter ke pas jana h",
    "shukriya",
]


class TestEvidence(unittest.TestCase):
    def test_acute_danger_phrases_are_recognised(self):
        for text in ACUTE:
            with self.subTest(text=text):
                self.assertTrue(emergency_evidence(text))

    def test_routine_sentences_are_not(self):
        for text in ROUTINE:
            with self.subTest(text=text):
                self.assertFalse(emergency_evidence(text))


class TestRouterOverride(unittest.TestCase):
    def test_a_confident_wrong_label_is_overridden(self):
        router = IntentRouter(detector=Fixed("thank_you", 0.97))
        result = router.route("Bohot khoon beh raha hai")
        self.assertEqual(result.intent, Intent.EMERGENCY)
        self.assertTrue(result.safety_override)
        self.assertEqual(result.raw_intent, "thank_you")      # still logged honestly

    def test_a_low_confidence_guess_is_overridden(self):
        router = IntentRouter(detector=Fixed("repeat_information", 0.48))
        self.assertEqual(router.route("ammi ki saans ruk rahi hai jaldi kuch karein").intent,
                         Intent.EMERGENCY)

    def test_routine_sentences_keep_the_model_answer(self):
        router = IntentRouter(detector=Fixed("book_appointment", 0.95))
        for text in ROUTINE:
            with self.subTest(text=text):
                self.assertFalse(router.route(text).safety_override)

    def test_it_can_be_switched_off(self):
        router = IntentRouter(detector=Fixed("thank_you", 0.97), use_emergency_safety_net=False)
        self.assertEqual(router.route("Bohot khoon beh raha hai").intent, Intent.THANKS)


class TestDialogueEscalates(unittest.TestCase):
    def test_escalation_is_immediate(self):
        script = [("khoon", "thank_you", 0.97)]
        manager, _, _ = build_manager(script)
        result = manager.process_message("s", "Bohot khoon beh raha hai", "P001")
        self.assertEqual(result["action"], Action.ESCALATE)

    def test_it_interrupts_a_pending_confirmation(self):
        """Mid-booking, at the 'shall I book it?' question, an emergency wins."""
        from tests.helpers import StubIntentDetector
        script = [("saans", "repeat_information", 0.45)] + StubIntentDetector.DEFAULT_SCRIPT
        manager, repo, _ = build_manager(script)
        manager.process_message("s", "Book with Dr Ahmed tomorrow at 4 PM", "P001")
        before = len(repo.query("appointments"))
        result = manager.process_message("s", "ruko, abbu ki saans nahi aa rahi", "P001")
        self.assertEqual(result["action"], Action.ESCALATE)
        self.assertEqual(len(repo.query("appointments")), before, "nothing may be booked")


if __name__ == "__main__":
    unittest.main()
