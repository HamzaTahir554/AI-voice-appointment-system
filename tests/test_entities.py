"""
Entity extraction and validation (spec sections 5, 27).

Date and time parsing is the part most likely to book a patient on the wrong
day, so it gets the heaviest coverage: English, Roman Urdu and Urdu script.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from dialog_manager.validators import (
    EntityExtractor,
    detect_yes_no,
    is_past_date,
    is_valid_appointment_id,
    is_valid_date,
    is_valid_time,
)
from firebase.doctor_service import DoctorService
from firebase.firebase_config import LocalRepository
from firebase.seed_data import seed

# A fixed Monday, so every relative-date assertion is deterministic.
FIXED_TODAY = date(2026, 9, 14)


class TestDateParsing(unittest.TestCase):
    def setUp(self):
        self.extractor = EntityExtractor(today_override=FIXED_TODAY)

    def _date(self, text):
        return self.extractor.extract_entities(text).get("date")

    def test_english_relative_dates(self):
        self.assertEqual(self._date("book me today"), "2026-09-14")
        self.assertEqual(self._date("tomorrow please"), "2026-09-15")
        self.assertEqual(self._date("day after tomorrow"), "2026-09-16")

    def test_roman_urdu_relative_dates(self):
        # "kal" is ambiguous in Urdu; in booking it always means tomorrow.
        self.assertEqual(self._date("kal appointment chahiye"), "2026-09-15")
        self.assertEqual(self._date("aaj aa sakta hoon"), "2026-09-14")
        self.assertEqual(self._date("parso"), "2026-09-16")

    def test_urdu_script_relative_dates(self):
        self.assertEqual(self._date("کل اپائنٹمنٹ چاہیے"), "2026-09-15")
        self.assertEqual(self._date("آج"), "2026-09-14")

    def test_weekday_names(self):
        # FIXED_TODAY is a Monday.
        self.assertEqual(self._date("friday"), "2026-09-18")
        self.assertEqual(self._date("jumma ko"), "2026-09-18")
        self.assertEqual(self._date("جمعہ"), "2026-09-18")

    def test_next_weekday_skips_a_week(self):
        self.assertEqual(self._date("monday"), "2026-09-14")     # today
        self.assertEqual(self._date("next monday"), "2026-09-21")

    def test_explicit_dates(self):
        self.assertEqual(self._date("on 2026-10-05"), "2026-10-05")
        self.assertEqual(self._date("5 October"), "2026-10-05")

    def test_no_date_returns_nothing(self):
        self.assertIsNone(self._date("I want an appointment"))


class TestTimeParsing(unittest.TestCase):
    def setUp(self):
        self.extractor = EntityExtractor(today_override=FIXED_TODAY)

    def _time(self, text):
        return self.extractor.extract_entities(text).get("time")

    def test_explicit_am_pm(self):
        self.assertEqual(self._time("4 PM"), "16:00")
        self.assertEqual(self._time("9 am"), "09:00")
        self.assertEqual(self._time("12 pm"), "12:00")
        self.assertEqual(self._time("12 am"), "00:00")

    def test_24_hour_and_minutes(self):
        self.assertEqual(self._time("16:00"), "16:00")
        self.assertEqual(self._time("4:30 pm"), "16:30")

    def test_bare_hour_uses_the_clinic_heuristic(self):
        # A patient saying "4 o'clock" about a clinic means the afternoon.
        self.assertEqual(self._time("at 4"), "16:00")
        self.assertEqual(self._time("around 5"), "17:00")
        # Morning hours stay in the morning.
        self.assertEqual(self._time("at 10"), "10:00")

    def test_roman_urdu_oclock(self):
        self.assertEqual(self._time("4 baje"), "16:00")
        self.assertEqual(self._time("char baje"), "16:00")
        self.assertEqual(self._time("subah 9 baje"), "09:00")
        self.assertEqual(self._time("shaam 5 baje"), "17:00")

    def test_urdu_script_oclock(self):
        self.assertEqual(self._time("چار بجے"), "16:00")
        self.assertEqual(self._time("شام پانچ بجے"), "17:00")
        self.assertEqual(self._time("صبح نو بجے"), "09:00")

    def test_urdu_digits(self):
        self.assertEqual(self._time("۴ بجے"), "16:00")

    def test_lone_number_reply(self):
        # Answering "what time?" with just "4".
        self.assertEqual(self._time("4"), "16:00")

    def test_no_time_returns_nothing(self):
        self.assertIsNone(self._time("I want an appointment tomorrow"))


class TestOtherEntities(unittest.TestCase):
    def setUp(self):
        repo = LocalRepository()
        seed(repo)
        self.extractor = EntityExtractor(doctor_service=DoctorService(repo),
                                         today_override=FIXED_TODAY)

    def test_doctor_resolves_to_a_database_record(self):
        entities = self.extractor.extract_entities("appointment with Dr Ahmed")
        self.assertEqual(entities["doctor_id"], "D001")
        self.assertEqual(entities["doctor_name"], "Dr Ahmed Khan")
        self.assertEqual(entities["clinic_id"], "C001")

    def test_unknown_doctor_is_not_extracted(self):
        entities = self.extractor.extract_entities("appointment with Dr Nobody")
        self.assertNotIn("doctor_id", entities)

    def test_appointment_id(self):
        entities = self.extractor.extract_entities("my id is APT123")
        self.assertEqual(entities["appointment_id"], "APT123")
        spaced = self.extractor.extract_entities("apt 456 please")
        self.assertEqual(spaced["appointment_id"], "APT456")

    def test_phone_number_formats(self):
        for text in ["03001234567", "+92 300 1234567", "0300-1234567"]:
            with self.subTest(text=text):
                entities = self.extractor.extract_entities(f"call me on {text}")
                self.assertEqual(entities["patient_phone"], "03001234567")

    def test_phone_digits_are_not_read_as_a_time(self):
        entities = self.extractor.extract_entities("my number is 03001234567")
        self.assertIsNone(entities.get("time"))

    def test_patient_name(self):
        self.assertEqual(
            self.extractor.extract_entities("my name is Tariq")["patient_name"],
            "Tariq")
        self.assertEqual(
            self.extractor.extract_entities("mera naam Sana hai")["patient_name"],
            "Sana")

    def test_combined_sentence(self):
        entities = self.extractor.extract_entities(
            "Kal 4 baje Dr Ahmed ke saath appointment chahiye")
        self.assertEqual(entities["doctor_name"], "Dr Ahmed Khan")
        self.assertEqual(entities["date"], "2026-09-15")
        self.assertEqual(entities["time"], "16:00")


class TestYesNoDetection(unittest.TestCase):
    """Spec section 21/26 - never left to the classifier."""

    def test_english_yes(self):
        for word in ["yes", "yeah", "sure", "okay", "yes please", "go ahead"]:
            self.assertEqual(detect_yes_no(word), "yes", word)

    def test_urdu_yes(self):
        for word in ["haan", "han", "ji", "jee", "ji haan", "bilkul",
                     "theek hai", "ہاں", "جی ہاں", "بالکل", "ٹھیک ہے"]:
            self.assertEqual(detect_yes_no(word), "yes", word)

    def test_english_no(self):
        for word in ["no", "nope", "not now", "no thanks", "forget it"]:
            self.assertEqual(detect_yes_no(word), "no", word)

    def test_urdu_no(self):
        for word in ["nahi", "nhi", "hargiz nahi", "نہیں", "جی نہیں"]:
            self.assertEqual(detect_yes_no(word), "no", word)

    def test_negation_beats_agreement(self):
        # "no that is ok" is a refusal even though it contains "ok".
        self.assertEqual(detect_yes_no("no that is ok"), "no")

    def test_unrelated_text_is_neither(self):
        self.assertIsNone(detect_yes_no("what time is it"))
        self.assertIsNone(detect_yes_no(""))


class TestValidators(unittest.TestCase):
    def test_date_validation(self):
        self.assertTrue(is_valid_date("2026-09-15"))
        self.assertFalse(is_valid_date("2026-13-45"))
        self.assertFalse(is_valid_date("tomorrow"))

    def test_past_date_detection(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertTrue(is_past_date(yesterday))
        future = (date.today() + timedelta(days=5)).isoformat()
        self.assertFalse(is_past_date(future))

    def test_time_validation(self):
        self.assertTrue(is_valid_time("16:00"))
        self.assertFalse(is_valid_time("25:00"))
        self.assertFalse(is_valid_time("4pm"))

    def test_appointment_id_validation(self):
        self.assertTrue(is_valid_appointment_id("APT123"))
        self.assertTrue(is_valid_appointment_id("apt3b2f1d"))
        self.assertFalse(is_valid_appointment_id("12345"))


if __name__ == "__main__":
    unittest.main()
