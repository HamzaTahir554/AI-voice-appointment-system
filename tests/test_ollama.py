"""
Ollama judge, response validator and deterministic fallbacks
(spec sections 17-21, 28-30, 34).

These tests never call a real LLM. They inject scripted responses instead, so
the suite runs on a machine with no Ollama installed and, more importantly,
tests exactly the outputs we are worried about - including hallucinations a
real model produces only occasionally.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from config import TIMEZONE, ErrorCode, Status
from appointment_backend.result import OperationResult
from ollama_judge.fallback import build_fallback_response, format_time, list_times
from ollama_judge.judge import OllamaJudge, detect_language
from ollama_judge.language import (
    ENGLISH, ROMAN_URDU, URDU, has_language_signal, render, resolve_language,
    spoken_slots, spoken_time,
)
from ollama_judge.ollama_service import parse_json
from ollama_judge.response_validator import extract_times, validate_llm_response

# Fixture dates are relative: the validator now checks spoken dates such as
# "kal", so a fixed calendar date would make the tests pass on one day only.
TOMORROW = (datetime.now(TIMEZONE).date() + timedelta(days=1)).isoformat()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def booked_result() -> OperationResult:
    return OperationResult.ok(
        "book", appointment_id="APT4F21A0", status=Status.CONFIRMED,
        doctor_name="Dr Ahmed Khan", date=TOMORROW, time="16:00")


def slot_taken_result() -> OperationResult:
    return OperationResult.fail(
        "book", ErrorCode.SLOT_UNAVAILABLE, "16:00 is already booked.",
        doctor_name="Dr Ahmed Khan", date=TOMORROW,
        requested_time="16:00", alternative_slots=["16:20", "17:00"])


class FakeService:
    """Stands in for OllamaService with a scripted reply."""

    def __init__(self, reply: dict | None = None, available: bool = True):
        self.reply = reply
        self.available = available
        self.model = "fake"
        self.calls = 0

    def is_available(self, force: bool = False) -> bool:
        return self.available

    def chat_json(self, system, user, examples=None):
        self.calls += 1
        return self.reply


def judge_with(reply, available=True) -> OllamaJudge:
    return OllamaJudge(service=FakeService(reply, available))


# --------------------------------------------------------------------------
class TestLanguageDetection(unittest.TestCase):
    def test_detects_each_language(self):
        self.assertEqual(detect_language("I want an appointment"), "english")
        self.assertEqual(detect_language("Mujhe appointment chahiye"), "roman_urdu")
        self.assertEqual(detect_language("مجھے اپائنٹمنٹ چاہیے"), "urdu")

    def test_empty_defaults_to_english(self):
        self.assertEqual(detect_language(""), "english")


class TestJsonParsing(unittest.TestCase):
    """Small models wrap JSON in prose and fences even when told not to."""

    def test_plain_json(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_surrounding_prose(self):
        self.assertEqual(parse_json('Sure! {"a": 1} Hope that helps.'), {"a": 1})

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_json("no json at all"))
        self.assertIsNone(parse_json(""))


class TestResponseValidator(unittest.TestCase):
    """Spec sections 28-29: the guard that actually enforces the rules."""

    def test_faithful_success_response_passes(self):
        report = validate_llm_response(
            "Your appointment with Dr Ahmed Khan is booked for 4:00 PM.",
            booked_result())
        self.assertTrue(report.valid, report.problems)

    def test_invented_time_is_rejected(self):
        """The spec's own example: backend says 16:20/17:00, model says 4:30."""
        report = validate_llm_response(
            "Dr Ahmed Khan is available at 4:30 PM.", slot_taken_result())
        self.assertFalse(report.valid)
        self.assertIn("not provided by the backend", report.problems[0])

    def test_offering_the_real_alternatives_passes(self):
        report = validate_llm_response(
            "Sorry, 4 PM is taken. Dr Ahmed Khan is free at 4:20 PM or 5 PM.",
            slot_taken_result())
        self.assertTrue(report.valid, report.problems)

    def test_claiming_success_after_failure_is_rejected(self):
        report = validate_llm_response(
            "Your appointment has been booked for 4 PM.", slot_taken_result())
        self.assertFalse(report.valid)
        self.assertTrue(any("claims success" in p for p in report.problems))

    def test_reporting_a_failure_honestly_passes(self):
        report = validate_llm_response(
            "I could not book 4:00 PM, it is taken. 4:20 PM is free.",
            slot_taken_result())
        self.assertTrue(report.valid, report.problems)

    def test_invented_appointment_id_is_rejected(self):
        report = validate_llm_response(
            "Booked. Your ID is APT999999.", booked_result())
        self.assertFalse(report.valid)
        self.assertTrue(any("invented appointment ID" in p
                            for p in report.problems))

    def test_real_appointment_id_passes(self):
        report = validate_llm_response(
            "Booked at 4:00 PM. Your ID is APT4F21A0.", booked_result())
        self.assertTrue(report.valid, report.problems)

    def test_empty_response_is_rejected(self):
        self.assertFalse(validate_llm_response("", booked_result()).valid)

    def test_roman_urdu_time_is_accepted(self):
        """
        "4 baje" is ambiguous (04:00 or 16:00). Requiring both readings to
        match rejected every correct Roman Urdu sentence, so a mention is only
        invented when NO reading matches.
        """
        report = validate_llm_response(
            "Aapki appointment kal 4 baje confirm ho gayi hai.", booked_result())
        self.assertTrue(report.valid, report.problems)

    def test_ambiguous_time_that_matches_nothing_is_rejected(self):
        report = validate_llm_response(
            "Aapki appointment 7 baje confirm ho gayi hai.", booked_result())
        self.assertFalse(report.valid)

    def test_twelve_hour_form_is_not_double_counted(self):
        """"4:20 PM" must not also register as a bare 04:20."""
        mentions = extract_times("see you at 4:20 PM")
        self.assertEqual(len(mentions), 1)


class TestFallbacks(unittest.TestCase):
    """Spec section 30: the system works with the LLM switched off."""

    def test_booking_success(self):
        text = build_fallback_response(booked_result())
        self.assertIn("Dr Ahmed Khan", text)
        self.assertIn("4:00 PM", text)
        self.assertIn("APT4F21A0", text)

    def test_slot_taken_offers_alternatives(self):
        text = build_fallback_response(slot_taken_result())
        self.assertIn("4:20 PM", text)
        self.assertIn("5:00 PM", text)

    def test_doctor_unavailable(self):
        text = build_fallback_response(OperationResult.fail(
            "book", ErrorCode.DOCTOR_UNAVAILABLE, "on leave",
            doctor_name="Dr Ahmed Khan", date=TOMORROW))
        # Assert on MEANING, not exact words - the phrasing rotates by design.
        self.assertIn("Dr Ahmed Khan", text)
        self.assertIn("day", text.lower())          # offers another day
        self.assertNotIn("booked for", text.lower())

    def test_every_fallback_passes_its_own_validator(self):
        """A deterministic sentence must never trip the hallucination guard."""
        for result in (booked_result(), slot_taken_result(),
                       OperationResult.ok("cancel", appointment_id="APT4F21A0",
                                          status=Status.CANCELLED,
                                          doctor_name="Dr Ahmed Khan",
                                          date=TOMORROW, time="16:00")):
            with self.subTest(operation=result.operation):
                text = build_fallback_response(result)
                report = validate_llm_response(text, result)
                self.assertTrue(report.valid, f"{text} -> {report.problems}")

    def test_formatting_helpers(self):
        self.assertEqual(format_time("16:00"), "4:00 PM")
        self.assertEqual(format_time("09:30"), "9:30 AM")
        self.assertEqual(list_times(["16:20", "17:00"]), "4:20 PM or 5:00 PM")


class TestJudge(unittest.TestCase):
    def test_uses_a_valid_llm_response(self):
        judge = judge_with({
            "decision": "approved",
            "response": "Ji bilkul, aapki appointment 4 baje confirm ho gayi hai.",
            "reason": "backend confirmed"})
        result = judge.judge("Mujhe appointment chahiye", "book_appointment",
                             0.96, {}, booked_result())
        self.assertEqual(result.source, "ollama")
        self.assertIn("4 baje", result.response)

    def test_rejects_a_hallucinated_response(self):
        """Spec section 28: an invented time must never reach the caller."""
        judge = judge_with({
            "decision": "approved",
            "response": "Dr Ahmed Khan is available at 4:30 PM.",
            "reason": "made it up"})
        result = judge.judge("Book at 4", "book_appointment", 0.95, {},
                             slot_taken_result())
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.decision, "needs_correction")
        self.assertTrue(result.validation_problems)
        self.assertIn("4:20 PM", result.response)      # the real alternative

    def test_falls_back_when_ollama_is_down(self):
        """Spec section 30."""
        judge = judge_with(None, available=False)
        result = judge.judge("Book it", "book_appointment", 0.95, {},
                             booked_result())
        self.assertEqual(result.source, "fallback")
        self.assertIn("APT4F21A0", result.response)

    def test_falls_back_on_unparseable_output(self):
        judge = judge_with(None, available=True)
        result = judge.judge("Book it", "book_appointment", 0.95, {},
                             booked_result())
        self.assertEqual(result.source, "fallback")

    def test_model_self_flagging_still_gets_validated(self):
        """
        A 3B model flags needs_correction because it doubts the backend, which
        it has no standing to do. If its text is faithful, we still use it.
        """
        judge = judge_with({
            "decision": "needs_correction",
            "response": "Your appointment with Dr Ahmed Khan is booked for 4:00 PM.",
            "reason": "doctor id looked odd to me"})
        result = judge.judge("Book it", "book_appointment", 0.95, {},
                             booked_result())
        self.assertEqual(result.source, "ollama")

    def test_llm_cannot_turn_a_failure_into_a_success(self):
        """The single most important guarantee in the module."""
        judge = judge_with({
            "decision": "approved",
            "response": "Great news, your appointment is booked!",
            "reason": "lying"})
        result = judge.judge("Book at 4", "book_appointment", 0.95, {},
                             slot_taken_result())
        self.assertEqual(result.source, "fallback")
        # The wording rotates, so assert the OUTCOME: it must offer the real
        # alternatives and must not claim the booking succeeded.
        self.assertIn("4:20 PM", result.response)
        self.assertNotIn("all done", result.response.lower())
        self.assertTrue(validate_llm_response(result.response,
                                              slot_taken_result()).valid)

    def test_no_backend_operation_skips_the_llm(self):
        service = FakeService({"decision": "approved", "response": "hi"})
        judge = OllamaJudge(service=service)
        result = judge.judge("hello", "greeting", 0.9, {}, None)
        self.assertEqual(service.calls, 0)
        self.assertEqual(result.source, "fallback")


class TestLanguageStickiness(unittest.TestCase):
    """
    Language belongs to the CONVERSATION, not to one utterance.

    A caller speaking Roman Urdu who answers "yes" must keep hearing Roman
    Urdu - the reason the assistant used to switch to English mid-booking.
    """

    def test_short_replies_carry_no_signal(self):
        for reply in ("yes", "ok", "4", "APT123"):
            self.assertFalse(has_language_signal(reply), reply)

    def test_urdu_script_is_always_a_signal(self):
        self.assertTrue(has_language_signal("جی"))

    def test_roman_urdu_keywords_are_a_signal(self):
        self.assertTrue(has_language_signal("haan ji"))

    def test_english_confirmation_does_not_switch_the_language(self):
        self.assertEqual(
            resolve_language("yes conformed", current=ROMAN_URDU), ROMAN_URDU)

    def test_a_full_english_sentence_does_switch(self):
        self.assertEqual(
            resolve_language("I would like to book an appointment please",
                             current=ROMAN_URDU), ENGLISH)

    def test_override_wins_outright(self):
        self.assertEqual(
            resolve_language("I want an appointment", current=ENGLISH,
                             override=URDU), URDU)


class TestMultilingualTemplates(unittest.TestCase):
    """The system is multilingual WITHOUT the LLM (spec section 23)."""

    def test_booking_confirmation_in_every_language(self):
        for language in (ENGLISH, ROMAN_URDU, URDU):
            with self.subTest(language=language):
                text = render("booked", language, doctor="Dr Ahmed Khan",
                              date="kal", time="4 baje", id="APT123")
                # Every phrasing must carry the three facts, whatever the words.
                self.assertIn("Dr Ahmed Khan", text)
                self.assertIn("4 baje", text)
                self.assertIn("APT123", text)

    def test_spoken_time_per_language(self):
        self.assertEqual(spoken_time("16:00", ENGLISH), "4:00 PM")
        self.assertEqual(spoken_time("16:00", ROMAN_URDU), "shaam 4 baje")
        self.assertEqual(spoken_time("09:00", ROMAN_URDU), "subah 9 baje")
        self.assertIn("شام", spoken_time("16:00", URDU))

    def test_slot_lists_use_the_right_conjunction(self):
        self.assertIn(" or ", spoken_slots(["16:20", "17:00"], ENGLISH))
        self.assertIn(" ya ", spoken_slots(["16:20", "17:00"], ROMAN_URDU))

    def test_fallbacks_are_produced_in_the_callers_language(self):
        text = build_fallback_response(booked_result(), ROMAN_URDU)
        self.assertIn("confirm ho gayi", text)
        self.assertIn("APT4F21A0", text)

    def test_translated_fallbacks_still_pass_the_validator(self):
        """A Roman Urdu sentence must not trip the hallucination guard."""
        for language in (ENGLISH, ROMAN_URDU, URDU):
            with self.subTest(language=language):
                for result in (booked_result(), slot_taken_result()):
                    text = build_fallback_response(result, language)
                    report = validate_llm_response(text, result)
                    self.assertTrue(report.valid,
                                    f"{language}: {text} -> {report.problems}")


class TestValidatorWithLanguage(unittest.TestCase):
    """
    Found by the live Ollama test: with a language given, "shaam 4:20 baje"
    was rejected as an unnatural 24-hour "20 baje". The offline tests called
    the validator without a language, so the check never ran there.
    """

    def test_translated_fallbacks_pass_with_the_language_given(self):
        for language in (ENGLISH, ROMAN_URDU, URDU):
            for result in (booked_result(), slot_taken_result()):
                with self.subTest(language=language, operation=result.operation):
                    text = build_fallback_response(result, language)
                    report = validate_llm_response(text, result, language)
                    self.assertTrue(report.valid, f"{language}: {text} -> {report.problems}")

    def test_a_real_24_hour_baje_is_still_rejected(self):
        report = validate_llm_response("Aap ki appointment 17 baje confirm hai.", booked_result(), ROMAN_URDU)
        self.assertFalse(report.valid)


class TestSpokenDatesAndLanguage(unittest.TestCase):
    """
    Found by the live Ollama run: "Aapki appointment kal 5 baje..." for a date
    eight days away passed the validator, and so did Roman-Urdu replies to
    Urdu-script and English requests.
    """

    @staticmethod
    def _day(days_ahead):
        return datetime.now(TIMEZONE).date() + timedelta(days=days_ahead)

    def _results(self, days_ahead):
        day = self._day(days_ahead).isoformat()
        after = self._day(days_ahead + 1).isoformat()
        return [
            OperationResult.ok("book", appointment_id="APT4F21A0", status=Status.CONFIRMED,
                               doctor_name="Dr Ahmed Khan", date=day, time="16:00"),
            OperationResult.ok("cancel", appointment_id="APT4F21A0", status=Status.CANCELLED,
                               doctor_name="Dr Ahmed Khan", date=day, time="16:00"),
            OperationResult.ok("reschedule", appointment_id="APT4F21A0", status=Status.CONFIRMED,
                               doctor_name="Dr Ahmed Khan", date=day, time="16:00",
                               new_date=after, new_time="17:00"),
            OperationResult.fail("book", ErrorCode.SLOT_UNAVAILABLE, "16:00 is already booked.",
                                 doctor_name="Dr Ahmed Khan", date=day, requested_time="16:00",
                                 alternative_slots=["16:20", "17:00"]),
            OperationResult.fail("book", ErrorCode.DOCTOR_UNAVAILABLE, "Doctor on leave.",
                                 doctor_name="Dr Ahmed Khan", date=day),
        ]

    def test_kal_for_a_date_next_week_is_rejected(self):
        result = self._results(8)[0]
        report = validate_llm_response("Aapki appointment kal 4 baje confirm ho gayi hai.", result)
        self.assertFalse(report.valid)
        self.assertTrue(any("kal" in p for p in report.problems), report.problems)

    def test_kal_for_tomorrow_is_accepted(self):
        report = validate_llm_response("Aapki appointment kal 4 baje confirm ho gayi hai.",
                                       self._results(1)[0])
        self.assertTrue(report.valid, report.problems)

    def test_a_wrong_weekday_is_rejected(self):
        target = self._day(8)
        wrong = (target + timedelta(days=1)).strftime("%A")
        result = self._results(8)[0]
        self.assertFalse(validate_llm_response(f"You are booked for {wrong} at 4 PM.", result).valid)
        self.assertTrue(validate_llm_response(f"You are booked for {target.strftime('%A')} at 4 PM.",
                                              result).valid)

    def test_a_wrong_day_of_month_is_rejected(self):
        target = self._day(8)
        month = target.strftime("%B")
        wrong_day = (target + timedelta(days=1)).day
        result = self._results(8)[0]
        self.assertFalse(validate_llm_response(f"You are booked on {wrong_day} {month} at 4 PM.", result).valid)
        self.assertTrue(validate_llm_response(f"You are booked on {target.day} {month} at 4 PM.", result).valid)

    def test_the_urdu_word_for_clinic_is_not_kal(self):
        report = validate_llm_response("آپ کی اپائنٹمنٹ کلینک میں شام 4 بجے ہے", self._results(8)[0], URDU)
        self.assertTrue(report.valid, report.problems)

    def test_replies_in_the_wrong_language_are_rejected(self):
        result = self._results(1)[0]
        roman = "Ji bilkul, aapki appointment kal 4 baje confirm ho gayi hai."
        english = "Your appointment with Dr Ahmed Khan is confirmed for tomorrow at 4 PM."
        self.assertFalse(validate_llm_response(roman, result, URDU).valid)
        self.assertFalse(validate_llm_response(roman, result, ENGLISH).valid)
        self.assertFalse(validate_llm_response(english, result, ROMAN_URDU).valid)
        self.assertTrue(validate_llm_response(roman, result, ROMAN_URDU).valid)
        self.assertTrue(validate_llm_response(english, result, ENGLISH).valid)

    def test_raw_iso_dates_and_24_hour_baje_are_not_spoken(self):
        """Seen live: a Roman-Urdu reply that read out 2026-09-22 and 17:00 baje."""
        result = self._results(8)[2]
        iso = self._day(9).isoformat()
        self.assertFalse(validate_llm_response(f"Aapki appointment {iso} ko shaam 5 baje hai.", result,
                                               ROMAN_URDU).valid)
        self.assertFalse(validate_llm_response("Aapki appointment 17:00 baje kar di hai.", result,
                                               ROMAN_URDU).valid)

    def test_a_slot_that_is_already_taken_is_not_a_success_claim(self):
        """Seen in the exhaustive fallback test: "4:00 PM is booked" read as a booking."""
        result = self._results(1)[3]
        self.assertTrue(validate_llm_response("Sorry, 4:00 PM is already taken. 4:20 PM is free.",
                                              result, ENGLISH).valid)
        self.assertFalse(validate_llm_response("Your appointment is booked for 4:20 PM.",
                                               result, ENGLISH).valid)

    def test_every_deterministic_fallback_passes_in_its_language(self):
        for days_ahead in (0, 1, 2, 8):
            for result in self._results(days_ahead):
                for language in (ENGLISH, ROMAN_URDU, URDU):
                    for variant in (0, 1):
                        with self.subTest(days=days_ahead, operation=result.operation,
                                          error=result.error_code, language=language, variant=variant):
                            text = build_fallback_response(result, language, variant=variant)
                            report = validate_llm_response(text, result, language)
                            self.assertTrue(report.valid, f"{text} -> {report.problems}")


if __name__ == "__main__":
    unittest.main()
