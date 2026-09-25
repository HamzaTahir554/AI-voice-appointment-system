"""
Live Ollama tests - the real local model, not a scripted fake.

Skipped unless RUN_LIVE_TESTS=1 and an Ollama server is reachable:

    python scripts/run_tests.py --live --category ollama

test_ollama.py already proves the guard logic with fakes. These prove that
whatever the REAL model says still reaches the caller only after passing the
validator, in all three languages, and they record how long the model takes
(written to reports/ollama_live.json for the test report).

The strict assertions are the safety properties. How often the model's own
wording is accepted (rather than the deterministic fallback) is measured and
reported, not asserted - it is a quality number, not a correctness one.
"""
from __future__ import annotations

import json
import os
import re
import time
import unittest
from pathlib import Path

from appointment_backend.result import OperationResult
from config import ErrorCode, Status
from ollama_judge.fallback import build_fallback_response
from ollama_judge.judge import OllamaJudge
from ollama_judge.ollama_service import OllamaService
from ollama_judge.response_validator import validate_llm_response

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"
REPORT = Path(__file__).resolve().parents[1] / "reports" / "ollama_live.json"
LANGUAGES = ("english", "roman_urdu", "urdu")
TECHNICAL = re.compile(r"\b(json|backend|database|firestore|null|exception|"
                       r"error code|operation|status code|traceback)\b", re.I)
SUCCESS_CLAIM = re.compile(r"(has been booked|is confirmed|booked for you|"
                           r"confirm ho gayi|book ho gayi|بک ہو گئی|کنفرم ہو گئی)", re.I)


def outcomes() -> dict[str, tuple[OperationResult, str, str]]:
    """(backend result, what the caller said, intent) per scenario."""
    return {
        "booked": (OperationResult.ok(
            "book", appointment_id="APT7C3D91", status=Status.CONFIRMED,
            doctor_name="Dr Ahmed Khan", date="2026-09-21", time="16:00"),
            "ji haan book kar dein", "book_appointment"),
        "slot_taken": (OperationResult.fail(
            "book", ErrorCode.SLOT_UNAVAILABLE, "16:00 is already booked.",
            doctor_name="Dr Ahmed Khan", date="2026-09-21",
            requested_time="16:00", alternative_slots=["16:20", "17:00"]),
            "Dr Ahmed se 4 baje appointment chahiye", "book_appointment"),
        "doctor_on_leave": (OperationResult.fail(
            "book", ErrorCode.DOCTOR_UNAVAILABLE,
            "Dr Ahmed Khan is not available on 2026-09-21.",
            doctor_name="Dr Ahmed Khan", date="2026-09-21"),
            "Dr Ahmed se Monday ko milna hai", "book_appointment"),
        "cancelled": (OperationResult.ok(
            "cancel", appointment_id="APT7C3D91", status=Status.CANCELLED,
            doctor_name="Dr Ahmed Khan", date="2026-09-21", time="16:00"),
            "meri appointment cancel kar dein", "cancel_appointment"),
        "rescheduled": (OperationResult.ok(
            "reschedule", appointment_id="APT7C3D91", status=Status.CONFIRMED,
            doctor_name="Dr Ahmed Khan", date="2026-09-21", time="16:00",
            new_date="2026-09-22", new_time="17:00"),
            "appointment kal 5 baje kar dein", "reschedule_appointment"),
    }


@unittest.skipUnless(LIVE, "set RUN_LIVE_TESTS=1 to run against the real Ollama server")
class TestOllamaLive(unittest.TestCase):
    calls: list[dict] = []

    @classmethod
    def setUpClass(cls):
        cls.service = OllamaService()
        if not cls.service.is_available(force=True):
            raise unittest.SkipTest("Ollama server is not reachable")
        cls.judge = OllamaJudge(service=cls.service)
        cls.calls = []

    @classmethod
    def tearDownClass(cls):
        if not cls.calls:
            return
        latencies = sorted(c["seconds"] for c in cls.calls)
        accepted = sum(c["source"] == "ollama" for c in cls.calls)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({
            "model": cls.service.model,
            "calls": len(cls.calls),
            "llm_wording_accepted": accepted,
            "fallback_used": len(cls.calls) - accepted,
            "latency_seconds": {
                "median": round(latencies[len(latencies) // 2], 2),
                "max": round(latencies[-1], 2),
                "min": round(latencies[0], 2),
            },
            "responses": cls.calls,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    def _judge(self, name, language):
        result, text, intent = outcomes()[name]
        started = time.perf_counter()
        verdict = self.judge.judge(text, intent, 0.95, {}, result, language=language)
        seconds = time.perf_counter() - started
        self.calls.append({"scenario": name, "language": language,
                           "source": verdict.source, "decision": verdict.decision,
                           "reason": verdict.reason,
                           "seconds": round(seconds, 2), "response": verdict.response,
                           "rejected_llm_text": verdict.llm_raw if verdict.source == "fallback" else None,
                           "problems": verdict.validation_problems})
        return result, verdict

    def test_every_outcome_in_every_language_is_faithful(self):
        """Whatever the model wrote, the caller hears only validated facts."""
        for name in outcomes():
            for language in LANGUAGES:
                with self.subTest(scenario=name, language=language):
                    result, verdict = self._judge(name, language)
                    self.assertTrue(verdict.response.strip())
                    report = validate_llm_response(verdict.response, result, language)
                    self.assertTrue(report.valid, f"{verdict.response!r}: {report.problems}")
                    self.assertIsNone(TECHNICAL.search(verdict.response),
                                      f"technical wording reached the caller: {verdict.response!r}")
                    self.assertLessEqual(len(verdict.response.split()), 45,
                                         "too long to speak comfortably")

    def test_a_failure_is_never_spoken_as_a_success(self):
        for name in ("slot_taken", "doctor_on_leave"):
            for language in LANGUAGES:
                with self.subTest(scenario=name, language=language):
                    _, verdict = self._judge(name, language)
                    self.assertIsNone(SUCCESS_CLAIM.search(verdict.response),
                                      f"failure reported as success: {verdict.response!r}")

    def test_a_timeout_falls_back_to_the_deterministic_sentence(self):
        result, text, intent = outcomes()["booked"]
        impatient = OllamaJudge(service=OllamaService(timeout=0.001))
        verdict = impatient.judge(text, intent, 0.95, {}, result, language="roman_urdu")
        self.assertEqual(verdict.source, "fallback")
        self.assertEqual(verdict.response, build_fallback_response(result, "roman_urdu"))


if __name__ == "__main__":
    unittest.main()
