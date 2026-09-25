"""
Intent evaluation integrity + the model's safety-critical boundaries.

Integrity (no model needed):
  * no evaluation phrase - challenge, audit or holdout - may sit in the
    training corpus. The first Roman-Urdu retrain leaked 8 of 15 challenge
    phrases through template expansion, and 30 audit phrases were already in
    the corpus before the filter existed;
  * the labelling contradiction found by the audit stays fixed.

Safety boundaries (real model, skipped when it is absent): the confusions that
would do harm if they returned - a booking read into a question, an emergency
read as a booking, a repeat request escalated as an emergency.
"""
from __future__ import annotations

import csv
import importlib.util
import unittest

from config import INTENT_MODEL_DIR, INTENT_MODULE_DIR, Intent

SPLITS = INTENT_MODULE_DIR / "data" / "splits.csv"


def load_src(name):
    spec = importlib.util.spec_from_file_location(f"_t_{name}", INTENT_MODULE_DIR / "src" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEvaluationIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SPLITS.exists():
            raise unittest.SkipTest("training corpus not generated in this checkout")
        with open(SPLITS, encoding="utf-8", newline="") as fh:
            cls.rows = list(csv.DictReader(fh))
        cls.challenge = load_src("challenge_set")

    def test_no_evaluation_phrase_is_in_the_training_corpus(self):
        leaked = [r["text"] for r in self.rows if self.challenge.is_challenge(r["text"])]
        self.assertEqual(leaked, [])

    def test_the_filter_covers_all_three_sets(self):
        audit = load_src("audit_set")
        for text in (self.challenge.BOOKING[0], audit.AUDIT[0][0], audit.HOLDOUT[0][0]):
            with self.subTest(text=text):
                self.assertTrue(self.challenge.is_challenge(text.upper() + " ?"))

    def test_the_samajh_nahi_labels_agree(self):
        """"Kuch samajh nahi aa raha" and "Mujhe samajh nahi aa raha" mean the same."""
        labels = {r["text"]: r["label"] for r in self.rows}
        for text in ("Kuch samajh nahi aa raha", "Mujhe samajh nahi aa raha"):
            if text in labels:
                with self.subTest(text=text):
                    self.assertEqual(labels[text], "help")


class TestSafetyCriticalBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (INTENT_MODEL_DIR / "config.json").exists():
            raise unittest.SkipTest("intent model not trained in this checkout")
        from dialog_manager.intent_router import IntentRouter
        cls.router = IntentRouter()

    def test_questions_are_not_turned_into_bookings(self):
        for text in ("doctor ki fee kitni hai?", "doctor ki qualification kya hai?",
                     "Doctor kis cheez ke specialist hain?", "Clinic kahan hai?",
                     "Clinic ki timing kya hai?", "meri appointment kab hai"):
            with self.subTest(text=text):
                self.assertNotEqual(self.router.route(text).intent, Intent.BOOK_APPOINTMENT)

    def test_emergencies_are_not_booked(self):
        for text in ("Mere abbu behosh ho gaye hain", "seene mein shadeed dard ho raha hai",
                     "My child is not breathing properly"):
            with self.subTest(text=text):
                self.assertEqual(self.router.route(text).intent, Intent.EMERGENCY)

    def test_a_repeat_request_is_not_escalated(self):
        self.assertNotEqual(self.router.route("awaz kat rahi hai phir se boliye").intent,
                            Intent.EMERGENCY)

    def test_natural_roman_urdu_booking(self):
        for text in ("Mje docter ke pas jana h", "muje doctor k pas jana h",
                     "mjhe dr ke paas jana hai", "mje doctor ko dikhana h",
                     "doctor se milna h", "mjy doctor ka time chahiye"):
            with self.subTest(text=text):
                self.assertEqual(self.router.route(text).intent, Intent.BOOK_APPOINTMENT)


if __name__ == "__main__":
    unittest.main()
