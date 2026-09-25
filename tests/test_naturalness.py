"""
Conversational quality: the system should sound like a receptionist, not a form.

These test the things that make a caller feel they are talking to a person -
varied wording, acknowledgements, corrections handled gracefully, and never
asking for something already said. They assert on BEHAVIOUR, not exact
sentences, because the wording deliberately varies.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from config import Action, TIMEZONE
from dialog_manager.dialog_manager import DialogManager
from dialog_manager.intent_router import IntentRouter
from firebase.firebase_config import LocalRepository, reset_repository
from firebase.doctor_service import DoctorService
from firebase.seed_data import seed
from ollama_judge.judge import OllamaJudge
from ollama_judge.language import ROMAN_URDU, acknowledge, render
from tests.helpers import StubIntentDetector
from tests.test_ollama import FakeService
from voice_pipeline import VoicePipeline


def next_working_day(offset: int = 1) -> str:
    day = datetime.now(TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


def build(language: str = ROMAN_URDU):
    """A pipeline with an empty diary and the LLM off, so wording is ours."""
    repo = LocalRepository()
    reset_repository(repo)
    seed(repo, with_sample_appointment=False)
    dialog = DialogManager(
        intent_router=IntentRouter(detector=StubIntentDetector()),
        repository=repo, deterministic_responses=True)
    pipeline = VoicePipeline(
        dialog_manager=dialog, judge=OllamaJudge(service=FakeService(None, False)),
        repository=repo, response_language=language)
    return pipeline, repo


class TestNoRepetition(unittest.TestCase):
    """Spec section 8: the same question must not come out word for word."""

    def test_asking_three_times_gives_three_wordings(self):
        pipeline, _ = build()
        replies = [pipeline.process("s", "Mujhe appointment chahiye", "P001")["response"]]
        for _ in range(2):
            replies.append(pipeline.process("s", "hmm", "P001")["response"])
        self.assertEqual(len(set(replies)), 3,
                         f"expected three distinct phrasings, got {replies}")

    def test_every_question_has_more_than_one_phrasing(self):
        for key in ("ask_doctor", "ask_date", "ask_time"):
            with self.subTest(key=key):
                variants = {render(key, ROMAN_URDU, variant=i) for i in range(3)}
                self.assertGreater(len(variants), 1,
                                   f"{key} needs more than one phrasing")

    def test_confirmations_vary_too(self):
        first = render("booked", ROMAN_URDU, 0, doctor="Dr A", date="kal",
                       time="4 baje", id="APT1")
        second = render("booked", ROMAN_URDU, 1, doctor="Dr A", date="kal",
                        time="4 baje", id="APT1")
        self.assertNotEqual(first, second)


class TestAcknowledgements(unittest.TestCase):
    """Spec sections 5 and 13: a human says "theek hai" before moving on."""

    def test_reply_is_acknowledged_after_the_caller_gives_information(self):
        pipeline, _ = build()
        pipeline.process("s", "Mujhe Dr Ahmed se appointment leni hai", "P001")
        reply = pipeline.process("s", next_working_day(), "P001")["response"]
        openers = ("Bilkul", "Theek hai", "Ji", "Acha")
        self.assertTrue(reply.startswith(openers),
                        f"expected an acknowledgement, got {reply!r}")

    def test_the_opening_question_is_not_acknowledged(self):
        """Nothing has been said yet, so "sure" would be meaningless."""
        pipeline, _ = build()
        reply = pipeline.process("s", "Mujhe appointment chahiye", "P001")["response"]
        self.assertFalse(reply.startswith(("Bilkul", "Theek hai", "Acha")))

    def test_acknowledgement_wording_rotates(self):
        pool = {acknowledge(ROMAN_URDU, index=i) for i in range(4)}
        self.assertGreater(len(pool), 1)


class TestCorrections(unittest.TestCase):
    """Spec section 5: corrections get "koi baat nahi", never an error."""

    def test_changing_the_date_is_acknowledged_as_a_correction(self):
        pipeline, _ = build()
        pipeline.process("s", "Dr Ahmed ka appointment chahiye", "P001")
        pipeline.process("s", "Tuesday", "P001")
        reply = pipeline.process("s", "Sorry, Wednesday", "P001")["response"]
        self.assertTrue(reply.startswith(("Koi baat nahi", "Theek hai ji")),
                        f"expected a correction acknowledgement, got {reply!r}")

    def test_the_corrected_value_actually_replaces_the_old_one(self):
        pipeline, _ = build()
        pipeline.process("s", "Dr Ahmed ka appointment chahiye", "P001")
        first = pipeline.process("s", "Tuesday", "P001")["slots"]["date"]
        second = pipeline.process("s", "Sorry, Wednesday", "P001")["slots"]["date"]
        self.assertNotEqual(first, second)

    def test_a_correction_never_produces_an_error(self):
        pipeline, _ = build()
        pipeline.process("s", "Dr Ahmed ka appointment chahiye", "P001")
        pipeline.process("s", "Tuesday", "P001")
        result = pipeline.process("s", "Sorry, Wednesday", "P001")
        self.assertNotEqual(result["action"], Action.ERROR)
        self.assertNotIn("invalid", result["response"].lower())


class TestDoctorReplacement(unittest.TestCase):
    """
    "Dr Ahmed nahi, Dr Asim se karna hai".

    Plain longest-alias matching picks "ahmed" (the longer alias) and silently
    ignores the correction - so the caller is booked with the doctor they
    just rejected.
    """

    def setUp(self):
        repo = LocalRepository()
        seed(repo)
        self.doctors = DoctorService(repo)

    def test_roman_urdu_replacement(self):
        result = self.doctors.find_doctor("Actually Dr Ahmed nahi, Dr Asim se")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["doctor_id"], "D002")

    def test_english_replacement_has_the_opposite_word_order(self):
        result = self.doctors.find_doctor("not Dr Ahmed, Dr Sara please")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["doctor_id"], "D004")

    def test_urdu_script_replacement(self):
        result = self.doctors.find_doctor("ڈاکٹر احمد نہیں، ڈاکٹر حمزہ")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["doctor_id"], "D003")

    def test_a_plain_request_is_unaffected(self):
        result = self.doctors.find_doctor("Dr Ahmed ka appointment chahiye")
        self.assertEqual(result.data["doctor_id"], "D001")

    def test_a_bare_refusal_is_not_a_replacement(self):
        self.assertFalse(self.doctors.find_doctor("nahi").ok)

    def test_the_swap_carries_through_to_the_booking(self):
        pipeline, repo = build()
        pipeline.process("s", "Mujhe Dr Ahmed ka appointment chahiye", "P001")
        result = pipeline.process(
            "s", "Actually Dr Ahmed nahi, Dr Asim se karna hai", "P001")
        self.assertEqual(result["slots"]["doctor_id"], "D002")


class TestDoesNotReAsk(unittest.TestCase):
    """Spec sections 2-3: never ask for something already said."""

    def test_everything_in_one_sentence_goes_straight_to_confirmation(self):
        pipeline, _ = build()
        result = pipeline.process(
            "s", "Mujhe Dr Ahmed se kal 4 baje appointment chahiye", "P001")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)

    def test_two_details_at_once_leaves_only_one_question(self):
        pipeline, _ = build()
        result = pipeline.process(
            "s", "Mujhe Dr Ahmed se kal appointment chahiye", "P001")
        self.assertEqual(result["action"], Action.ASK_FOR_TIME)
        self.assertEqual(result["slots"]["doctor_id"], "D001")


class TestVoiceFriendly(unittest.TestCase):
    """Spec section 15: one or two short sentences, no jargon."""

    FORBIDDEN = ("error", "invalid", "database", "operation", "json", "null",
                 "backend", "exception", "field", "parameter", "intent=")

    def test_no_technical_words_reach_the_caller(self):
        pipeline, _ = build()
        turns = ["Assalam o Alaikum", "Mujhe Dr Ahmed se appointment leni hai",
                 next_working_day(), "4 baje", "ji haan", "shukriya",
                 "zxcvbn qwerty", "APTZZZZ"]
        for turn in turns:
            reply = pipeline.process("s", turn, "P001")["response"].lower()
            for word in self.FORBIDDEN:
                self.assertNotIn(word, reply,
                                 f"{word!r} leaked into: {reply!r}")

    def test_replies_stay_short_enough_to_speak(self):
        pipeline, _ = build()
        for turn in ["Mujhe Dr Ahmed se kal 4 baje appointment chahiye",
                     "ji haan"]:
            reply = pipeline.process("s", turn, "P001")["response"]
            self.assertLessEqual(len(reply), 220,
                                 f"too long for a voice reply: {reply!r}")


if __name__ == "__main__":
    unittest.main()
