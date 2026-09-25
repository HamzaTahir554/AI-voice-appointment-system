"""
Dialogue scenarios from the system audit (spec sections 14-18, 26-27).

Each class pins a behaviour the audit either verified or FIXED. The fixed ones
name the failure that was observed, so the reason for the test survives.

Everything runs through the full VoicePipeline (Dialog Manager + Roman-Urdu
rendering) on an in-memory database with a scripted intent stub. The last
class repeats the natural Roman-Urdu booking with the real mBERT model.
"""
from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta

from config import Action, Intent, Status, TIMEZONE
from dialog_manager.dialog_manager import DialogManager
from dialog_manager.intent_router import IntentRouter
from firebase.firebase_config import LocalRepository, reset_repository
from firebase.seed_data import seed
from ollama_judge.judge import OllamaJudge
from ollama_judge.language import ACKNOWLEDGEMENTS, CORRECTION_ACKS, ROMAN_URDU
from tests.helpers import StubIntentDetector, next_weekday_iso
from tests.test_ollama import FakeService
from voice_pipeline import VoicePipeline

# English sentence fragments that must never reach a Roman-Urdu caller.
ENGLISH_LEAK = re.compile(
    r"\b(Your appointment|Would you like|I found|is not available|This sounds like|"
    r"Which day|What time|Shall I|Do you want|consultation fee is|Available times)\b")


def working_day(offset: int = 1) -> str:
    """A day the seeded clinics are open (they close on Sunday)."""
    day = datetime.now(TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


class Fixed:
    """A detector that always returns one label at one confidence."""

    def __init__(self, label: str, confidence: float):
        self.label, self.confidence = label, confidence

    def predict(self, text):
        return {"intent": self.label, "confidence": self.confidence}


def build(script=None, detector=None, language=ROMAN_URDU):
    repo = LocalRepository()
    reset_repository(repo)
    seed(repo, with_sample_appointment=False)
    router = IntentRouter(detector=detector or StubIntentDetector(script))
    manager = DialogManager(intent_router=router, repository=repo,
                            deterministic_responses=True)
    pipeline = VoicePipeline(dialog_manager=manager,
                             judge=OllamaJudge(service=FakeService(available=False)),
                             repository=repo, use_llm=False, response_language=language)
    return pipeline, repo


def add_appointment(repo, day, time="16:00", patient="P001", doctor="D001",
                    appointment_id="APTTEST1"):
    repo.set("appointments", appointment_id, {
        "appointment_id": appointment_id, "patient_id": patient, "doctor_id": doctor,
        "clinic_id": "C001", "date": day, "time": time, "status": Status.CONFIRMED})
    return appointment_id


def doctor_named(repo, prefix):
    return next(d for d in repo.query("doctors") if d.get("name", "").startswith(prefix))


# --------------------------------------------------------------------------
# Spec 14: the confidence bands
# --------------------------------------------------------------------------
class TestConfidenceScenarios(unittest.TestCase):
    def test_medium_cancel_without_context_asks_in_roman_urdu(self):
        pipeline, _ = build(detector=Fixed("cancel_appointment", 0.68))
        result = pipeline.process("s", "kuch karna hai", "P001")
        self.assertEqual(result["action"], Action.CONFIRM_INTENT)
        self.assertEqual(result["response"], "Ji, aap appointment cancel karna chahte hain?")

    def test_medium_booking_with_details_is_not_second_guessed(self):
        pipeline, _ = build(detector=Fixed("book_appointment", 0.68))
        result = pipeline.process("s", "Dr Ahmed se kal 4 baje appointment chahiye", "P001")
        self.assertNotEqual(result["action"], Action.CONFIRM_INTENT)
        self.assertEqual(result["slots"]["doctor_id"], "D001")


# --------------------------------------------------------------------------
# Spec 15-18: slots, everything at once, corrections, intent switching
# --------------------------------------------------------------------------
class TestSlotsCorrectionsAndSwitching(unittest.TestCase):
    def test_slots_are_remembered_across_turns(self):
        pipeline, _ = build()
        day = working_day()
        self.assertEqual(pipeline.process("s", "Mujhe Dr Ahmed ka appointment chahiye", "P001")["action"],
                         Action.ASK_FOR_DATE)
        self.assertEqual(pipeline.process("s", day, "P001")["action"], Action.ASK_FOR_TIME)
        result = pipeline.process("s", "4 baje", "P001")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)
        self.assertEqual((result["slots"]["doctor_id"], result["slots"]["date"], result["slots"]["time"]),
                         ("D001", day, "16:00"))

    def test_everything_at_once_goes_straight_to_confirmation(self):
        if datetime.now(TIMEZONE).date().weekday() == 5:
            self.skipTest("tomorrow is Sunday, when the seeded clinic is closed")
        pipeline, _ = build()
        result = pipeline.process("s", "Mujhe Dr Ahmed se kal 4 baje appointment chahiye", "P001")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)

    def test_a_correction_updates_the_date_without_restarting(self):
        pipeline, _ = build()
        pipeline.process("s", "Mujhe Friday ka appointment chahiye", "P001")
        pipeline.process("s", "Dr Ahmed", "P001")
        result = pipeline.process("s", "Actually Saturday kar dein", "P001")
        self.assertEqual(result["slots"]["date"], next_weekday_iso(5))
        self.assertEqual(result["slots"]["doctor_id"], "D001")
        self.assertEqual(result["intent"], Intent.BOOK_APPOINTMENT)

    def test_switching_from_booking_to_cancelling(self):
        pipeline, _ = build()
        pipeline.process("s", "Mujhe appointment leni hai", "P001")
        result = pipeline.process("s", "Actually meri purani appointment cancel karni hai", "P001")
        self.assertEqual(result["intent"], Intent.CANCEL_APPOINTMENT)


# --------------------------------------------------------------------------
# FIXED: English reached Roman-Urdu callers
# --------------------------------------------------------------------------
class TestRepliesStayInRomanUrdu(unittest.TestCase):
    """
    Observed: "Your appointment with Dr Ahmed Khan is tomorrow at 4:00 PM",
    "I found your appointment ... on tomorrow ... Do you want me to cancel it?",
    "Dr Ahmed Khan is not available at 5:00 PM tomorrow. Available times are
    4:40 PM, 5:20 PM or 4:20 PM" and the English emergency message - all on a
    call whose replies must be Roman Urdu.
    """

    def assertRomanUrdu(self, text):
        self.assertIsNone(ENGLISH_LEAK.search(text), f"English reached the caller: {text!r}")

    def test_appointment_lookup(self):
        pipeline, repo = build()
        add_appointment(repo, working_day())
        result = pipeline.process("s", "meri appointment kab hai", "P001")
        self.assertEqual(result["action"], Action.CHECK_APPOINTMENT_STATUS)
        self.assertRomanUrdu(result["response"])
        self.assertIn("Dr Ahmed Khan", result["response"])
        self.assertIn("shaam 4 baje", result["response"])

    def test_cancel_confirmation_says_cancel_not_book(self):
        pipeline, repo = build()
        add_appointment(repo, working_day())
        result = pipeline.process("s", "meri appointment cancel kar dein", "P001")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)
        self.assertRomanUrdu(result["response"])
        self.assertIn("cancel", result["response"].lower())
        self.assertNotIn("Book kar doon", result["response"])

    def test_alternatives_are_offered_in_roman_urdu_in_clock_order(self):
        pipeline, repo = build()
        day = working_day()
        add_appointment(repo, day, "16:00", patient="PX", appointment_id="APTX")
        pipeline.process("s", "Dr Ahmed se appointment chahiye", "P001")
        pipeline.process("s", day, "P001")
        result = pipeline.process("s", "4 baje", "P001")
        self.assertEqual(result["action"], Action.OFFER_ALTERNATIVES)
        self.assertRomanUrdu(result["response"])
        times = [int(h) * 60 + int(m) for h, m in re.findall(r"(\d{1,2}):(\d{2})", result["response"])]
        self.assertGreaterEqual(len(times), 2)
        self.assertEqual(times, sorted(times), result["response"])

    def test_emergency(self):
        pipeline, _ = build()
        result = pipeline.process("s", "emergency hai", "P001")
        self.assertEqual(result["action"], Action.ESCALATE)
        self.assertRomanUrdu(result["response"])
        self.assertIn("hospital", result["response"].lower())

    def test_help_is_not_answered_as_a_misunderstanding(self):
        pipeline, _ = build()
        result = pipeline.process("s", "help", "P001")
        self.assertIn("book", result["response"].lower())
        self.assertNotIn("samajh nahi", result["response"].lower())


# --------------------------------------------------------------------------
# FIXED: the doctor-leave explanation was replaced by "which day?"
# --------------------------------------------------------------------------
class TestDoctorLeaveIsExplained(unittest.TestCase):
    def test_the_caller_hears_why_the_day_is_unavailable(self):
        pipeline, _ = build()
        day = working_day()
        pipeline.dialog.backend.mark_doctor_unavailable("D001", day, "On leave")
        pipeline.process("s", "Dr Ahmed se appointment chahiye", "P001")
        pipeline.process("s", day, "P001")
        result = pipeline.process("s", "4 baje", "P001")
        self.assertEqual(result["action"], Action.ASK_FOR_DATE)
        self.assertIn("Dr Ahmed Khan", result["response"])
        self.assertIsNone(ENGLISH_LEAK.search(result["response"]))


# --------------------------------------------------------------------------
# FIXED: "Mera time 5 baje kar dein" asked for the day and looped
# --------------------------------------------------------------------------
class TestTimeOnlyReschedule(unittest.TestCase):
    def test_the_original_day_is_kept(self):
        script = [("time 5 baje kar dein", "change_time", 0.9)] + StubIntentDetector.DEFAULT_SCRIPT
        pipeline, repo = build(script)
        day = working_day()
        appointment_id = add_appointment(repo, day, "16:00")

        result = pipeline.process("s", "Mera time 5 baje kar dein", "P001")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)
        self.assertEqual((result["slots"]["date"], result["slots"]["time"]), (day, "17:00"))
        self.assertNotIn("Book kar doon", result["response"])

        done = pipeline.process("s", "haan", "P001")
        self.assertEqual(done["action"], Action.RESCHEDULE_APPOINTMENT)
        stored = repo.get("appointments", appointment_id)
        self.assertEqual((stored["date"], stored["time"]), (day, "17:00"))


# --------------------------------------------------------------------------
# FIXED: a question asked mid-booking was silently ignored
# --------------------------------------------------------------------------
class TestSideQuestionsDuringATask(unittest.TestCase):
    def test_the_question_is_answered_and_the_booking_resumes(self):
        pipeline, repo = build()
        fee = str(repo.get("doctors", "D001")["fee"])
        pipeline.process("s", "Mujhe Dr Ahmed se appointment leni hai", "P001")
        answer = pipeline.process("s", "Dr Ahmed ki fee kitni hai?", "P001")
        self.assertIn(fee, answer["response"])
        self.assertIn("din", answer["response"].lower(), "the booking question should follow")
        self.assertEqual(answer["intent"], Intent.BOOK_APPOINTMENT)

        day = working_day()
        after = pipeline.process("s", day, "P001")
        self.assertEqual(after["action"], Action.ASK_FOR_TIME)
        self.assertEqual(after["slots"]["doctor_id"], "D001")

    def test_asking_about_another_doctor_does_not_change_the_booking(self):
        pipeline, repo = build()
        sara = doctor_named(repo, "Dr Sara")
        pipeline.process("s", "Mujhe Dr Ahmed se appointment leni hai", "P001")
        answer = pipeline.process("s", "Dr Sara ki fee kitni hai?", "P001")
        self.assertIn(str(sara["fee"]), answer["response"])
        self.assertEqual(answer["slots"]["doctor_id"], "D001")

    def test_a_bare_doctor_name_is_still_a_slot_answer(self):
        script = [("dr sara", "doctor_information", 0.99)] + StubIntentDetector.DEFAULT_SCRIPT
        pipeline, repo = build(script)
        sara = doctor_named(repo, "Dr Sara")
        pipeline.process("s", "Mujhe appointment leni hai", "P001")
        result = pipeline.process("s", "dr sara", "P001")
        self.assertEqual(result["action"], Action.ASK_FOR_DATE)
        self.assertEqual(result["slots"]["doctor_id"], sara["doctor_id"])


# --------------------------------------------------------------------------
# FIXED: "Dobara bata dein" was answered with "Theek hai."
# --------------------------------------------------------------------------
class TestRepeatRequest(unittest.TestCase):
    def test_a_repeat_is_not_acknowledged_as_new_information(self):
        script = [("dobara", "repeat_information", 0.95)] + StubIntentDetector.DEFAULT_SCRIPT
        pipeline, _ = build(script)
        pipeline.process("s", "Mujhe Dr Ahmed se appointment leni hai", "P001")
        result = pipeline.process("s", "dobara bata dein", "P001")
        openers = ACKNOWLEDGEMENTS[ROMAN_URDU] + CORRECTION_ACKS[ROMAN_URDU]
        self.assertFalse(any(result["response"].startswith(o) for o in openers), result["response"])
        self.assertIn("din", result["response"].lower())


# --------------------------------------------------------------------------
# Spec 26: the natural Roman-Urdu booking, with the real model
# --------------------------------------------------------------------------
class TestNaturalRomanUrduBookingWithRealModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from config import INTENT_MODEL_DIR
        if not (INTENT_MODEL_DIR / "config.json").exists():
            raise unittest.SkipTest("intent model not trained in this checkout")
        cls.router = IntentRouter()

    def test_the_whole_conversation_books_and_stays_in_roman_urdu(self):
        repo = LocalRepository()
        reset_repository(repo)
        seed(repo, with_sample_appointment=False)
        manager = DialogManager(intent_router=self.router, repository=repo,
                                deterministic_responses=True)
        pipeline = VoicePipeline(dialog_manager=manager,
                                 judge=OllamaJudge(service=FakeService(available=False)),
                                 repository=repo, use_llm=False, response_language=ROMAN_URDU)
        day = working_day()
        turns = ["Assalam o Alaikum", "Mje docter ke pas jana h", "dr ahmad", day, "4 baje", "haan"]
        results = [pipeline.process("real", text, "P001") for text in turns]
        for text, result in zip(turns, results):
            with self.subTest(turn=text):
                self.assertIsNone(ENGLISH_LEAK.search(result["response"]), result["response"])
        self.assertEqual(results[-1]["action"], Action.CREATE_APPOINTMENT, [r["response"] for r in results])
        booked = [a for a in repo.query("appointments") if a["status"] == Status.CONFIRMED]
        self.assertEqual([(a["doctor_id"], a["date"], a["time"]) for a in booked], [("D001", day, "16:00")])


if __name__ == "__main__":
    unittest.main()
