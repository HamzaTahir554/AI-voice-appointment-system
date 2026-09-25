"""
End-to-end pipeline tests (spec sections 33-34).

    STT text -> mBERT -> Dialog Manager -> Appointment Backend -> Firestore
             -> Ollama Judge -> Response Validator -> TTS text

Runs against the in-memory database with a scripted intent detector and a
scripted LLM, so the whole chain is exercised deterministically in
milliseconds. The point is to verify the *wiring* and the safety guarantees,
not the accuracy of the model or the eloquence of the LLM.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from config import Action, Collections, Status, TIMEZONE
from dialog_manager.dialog_manager import DialogManager
from dialog_manager.intent_router import IntentRouter
from firebase.firebase_config import LocalRepository, reset_repository
from firebase.patient_service import PatientService
from firebase.seed_data import seed
from ollama_judge.judge import OllamaJudge
from tests.helpers import StubIntentDetector
from tests.test_ollama import FakeService
from voice_pipeline import VoicePipeline


def next_working_day(offset: int = 1) -> str:
    day = datetime.now(TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() == 6:                    # clinics closed on Sunday
        day += timedelta(days=1)
    return day.isoformat()


def build_pipeline(script=None, llm_reply=None, llm_available=False):
    """A whole system wired to fakes, with a clean database."""
    repo = LocalRepository()
    reset_repository(repo)
    seed(repo, with_sample_appointment=False)
    PatientService(repo).create_patient("Sana", "03219876543", "P002")

    dialog = DialogManager(
        intent_router=IntentRouter(detector=StubIntentDetector(script)),
        repository=repo, deterministic_responses=True)
    judge = OllamaJudge(service=FakeService(llm_reply, llm_available))
    # Pin the language policy. These tests assert on wording, so they must not
    # depend on whatever RESPONSE_LANGUAGE the deployment's .env happens to set
    # - otherwise switching the live system to Urdu breaks the test suite.
    pipeline = VoicePipeline(dialog_manager=dialog, judge=judge,
                             repository=repo, response_language="auto")
    return pipeline, repo


class TestFullBookingFlow(unittest.TestCase):
    """Spec section 33: the complete happy path."""

    def test_every_stage_runs_and_the_appointment_reaches_the_database(self):
        pipeline, repo = build_pipeline()
        day = next_working_day()

        # 1. Greeting.
        greet = pipeline.process("S001", "Assalam o Alaikum", "P001")
        self.assertEqual(greet["action"], Action.GREET)

        # 2. Booking request - mBERT detects the intent.
        ask = pipeline.process("S001", "Mujhe Dr Ahmed se appointment leni hai",
                               "P001")
        self.assertEqual(ask["intent"], "book_appointment")
        self.assertEqual(ask["action"], Action.ASK_FOR_DATE)

        # 3-4. Slots fill in over the following turns.
        pipeline.process("S001", day, "P001")
        confirm = pipeline.process("S001", "4 baje", "P001")
        self.assertEqual(confirm["action"], Action.ASK_CONFIRMATION)
        self.assertEqual(confirm["slots"]["time"], "16:00")

        # Nothing written before the caller agrees.
        self.assertEqual(len(repo.query(Collections.APPOINTMENTS)), 0)

        # 5. Confirmation -> backend -> database.
        done = pipeline.process("S001", "Ji haan", "P001")
        self.assertEqual(done["action"], Action.CREATE_APPOINTMENT)
        self.assertTrue(done["success"])
        self.assertIsNotNone(done["appointment_id"])

        # 6. It is really in the database, with the right values.
        stored = repo.get(Collections.APPOINTMENTS, done["appointment_id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["doctor_id"], "D001")
        self.assertEqual(stored["time"], "16:00")
        self.assertEqual(stored["status"], Status.CONFIRMED)

        # 7. The backend result travelled to the response layer.
        self.assertTrue(done["backend_result"]["success"])
        self.assertEqual(done["backend_result"]["operation"], "book")

        # 8-10. A sentence exists for TTS, in the caller's language. The
        # conversation was Roman Urdu, so the confirmation is too.
        self.assertTrue(done["response"])
        self.assertEqual(done["language"], "roman_urdu")
        self.assertIn("confirm ho gayi", done["response"].lower())
        self.assertIn(done["appointment_id"].lower(), done["response"].lower())


class TestScenarios(unittest.TestCase):
    """Spec section 34."""

    def test_missing_doctor_is_asked_for(self):
        pipeline, _ = build_pipeline()
        result = pipeline.process("S", "I want an appointment tomorrow", "P001")
        self.assertEqual(result["action"], Action.ASK_FOR_DOCTOR)

    def test_missing_date_is_asked_for(self):
        pipeline, _ = build_pipeline()
        result = pipeline.process("S", "I want an appointment with Dr Ahmed",
                                  "P001")
        self.assertEqual(result["action"], Action.ASK_FOR_DATE)

    def test_unavailable_slot_offers_alternatives(self):
        pipeline, repo = build_pipeline()
        day = next_working_day()
        # Take 16:00 with a different patient first.
        pipeline.dialog.backend.book_appointment("P002", "D001", day, "16:00")

        pipeline.process("S", "Book Dr Ahmed", "P001")
        pipeline.process("S", day, "P001")
        result = pipeline.process("S", "4 PM", "P001")
        self.assertEqual(result["action"], Action.OFFER_ALTERNATIVES)
        self.assertIn("4:20", result["response"])

    def test_doctor_on_leave_is_reported(self):
        pipeline, _ = build_pipeline()
        day = next_working_day()
        pipeline.dialog.backend.mark_doctor_unavailable("D001", day, "On leave")

        pipeline.process("S", "Book Dr Ahmed", "P001")
        pipeline.process("S", day, "P001")
        result = pipeline.process("S", "4 PM", "P001")
        self.assertIn(result["action"],
                      (Action.ASK_FOR_DATE, Action.ERROR,
                       Action.OFFER_ALTERNATIVES))
        self.assertNotIn("booked for", result["response"].lower())

    def test_cancellation_end_to_end(self):
        pipeline, repo = build_pipeline()
        day = next_working_day()
        booked = pipeline.dialog.backend.book_appointment(
            "P001", "D001", day, "16:00")

        pipeline.process("S", "I want to cancel my appointment", "P001")
        pipeline.process("S", booked.appointment_id, "P001")
        done = pipeline.process("S", "haan", "P001")

        self.assertEqual(done["action"], Action.CANCEL_APPOINTMENT)
        self.assertEqual(
            repo.get(Collections.APPOINTMENTS, booked.appointment_id)["status"],
            Status.CANCELLED)

    def test_low_confidence_clarifies_and_writes_nothing(self):
        pipeline, repo = build_pipeline()
        result = pipeline.process("S", "zxcvbn qwerty asdfgh", "P001")
        self.assertEqual(result["action"], Action.CLARIFY)
        self.assertEqual(len(repo.query(Collections.APPOINTMENTS)), 0)


class TestLLMSafety(unittest.TestCase):
    """The LLM may improve the wording; it may never change the outcome."""

    def _book_to_completion(self, pipeline, day):
        pipeline.process("S", "Book Dr Ahmed", "P001")
        pipeline.process("S", day, "P001")
        pipeline.process("S", "4 PM", "P001")
        return pipeline.process("S", "yes", "P001")

    def test_pipeline_works_with_ollama_down(self):
        """Spec section 30."""
        pipeline, repo = build_pipeline(llm_available=False)
        done = self._book_to_completion(pipeline, next_working_day())
        self.assertEqual(done["action"], Action.CREATE_APPOINTMENT)
        self.assertEqual(done["judge"]["source"], "fallback")
        # These turns are in English, so the English template is used.
        self.assertEqual(done["language"], "english")
        # Phrasing rotates; assert the facts instead of a fixed word.
        self.assertIn(done["appointment_id"], done["response"])
        self.assertIn("Dr Ahmed Khan", done["response"])

    def test_good_llm_wording_is_used(self):
        pipeline, _ = build_pipeline(
            llm_reply={"decision": "approved",
                       # English, like the caller: the validator now rejects
                       # a reply in a different language (system audit).
                       "response": "Done - you are booked with Dr Ahmed Khan "
                                   "at 4 PM.",
                       "reason": "backend confirmed"},
            llm_available=True)
        done = self._book_to_completion(pipeline, next_working_day())
        self.assertEqual(done["judge"]["source"], "ollama")
        self.assertIn("4 PM", done["response"])

    def test_hallucinated_llm_wording_is_replaced(self):
        """Spec section 28: an invented time never reaches the caller."""
        pipeline, _ = build_pipeline(
            llm_reply={"decision": "approved",
                       "response": "Booked for 9:45 PM with Dr Nobody.",
                       "reason": "invented"},
            llm_available=True)
        done = self._book_to_completion(pipeline, next_working_day())
        self.assertEqual(done["judge"]["source"], "fallback")
        self.assertNotIn("9:45", done["response"])
        self.assertTrue(done["judge"]["validation_problems"])

    def test_llm_cannot_invent_a_booking_that_did_not_happen(self):
        """The strongest guarantee: the database is the arbiter, not the LLM."""
        pipeline, repo = build_pipeline(
            llm_reply={"decision": "approved",
                       "response": "All done, your appointment is booked!",
                       "reason": "lying"},
            llm_available=True)
        day = next_working_day()
        pipeline.dialog.backend.book_appointment("P002", "D001", day, "16:00")

        pipeline.process("S", "Book Dr Ahmed", "P001")
        pipeline.process("S", day, "P001")
        result = pipeline.process("S", "4 PM", "P001")

        # The claim is rejected and only one appointment exists - P002's.
        self.assertNotIn("all done", result["response"].lower())
        booked = [a for a in repo.query(Collections.APPOINTMENTS)
                  if a["time"] == "16:00" and a["date"] == day]
        self.assertEqual(len(booked), 1)
        self.assertEqual(booked[0]["patient_id"], "P002")


if __name__ == "__main__":
    unittest.main()
