"""
Measured latency of each pipeline stage (system audit section 48).

Only what can be measured on this machine is measured. Stages that do not
exist in the codebase (STT, TTS, telephony) are reported as "not measured"
with the reason, never estimated.

    python scripts/measure_performance.py          # offline stages
    python scripts/measure_performance.py --live   # + read-only Firestore and Ollama

Writes reports/performance.json.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config import INTENT_MODULE_DIR, TIMEZONE  # noqa: E402

REPORT = REPO / "reports" / "performance.json"


def summarise(samples_ms: list[float]) -> dict:
    ordered = sorted(samples_ms)
    return {"calls": len(ordered),
            "median_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[max(int(len(ordered) * 0.95) - 1, 0)], 2),
            "max_ms": round(ordered[-1], 2)}


def timed(fn, *args, **kwargs) -> tuple[float, object]:
    started = time.perf_counter()
    result = fn(*args, **kwargs)
    return (time.perf_counter() - started) * 1000, result


def audit_phrases(limit: int = 60) -> list[str]:
    path = INTENT_MODULE_DIR / "src" / "audit_set.py"
    spec = importlib.util.spec_from_file_location("_perf_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [case[0] for case in module.AUDIT][:limit]


def working_day(offset: int) -> str:
    day = datetime.now(TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


def local_stack(detector=None):
    from dialog_manager.dialog_manager import DialogManager
    from dialog_manager.intent_router import IntentRouter
    from firebase.firebase_config import LocalRepository, reset_repository
    from firebase.seed_data import seed
    from ollama_judge.judge import OllamaJudge
    from voice_pipeline import VoicePipeline
    from tests.test_ollama import FakeService

    repo = LocalRepository()
    reset_repository(repo)
    seed(repo, with_sample_appointment=False)
    router = IntentRouter(detector=detector) if detector else IntentRouter()
    manager = DialogManager(intent_router=router, repository=repo, deterministic_responses=True)
    pipeline = VoicePipeline(dialog_manager=manager,
                             judge=OllamaJudge(service=FakeService(available=False)),
                             repository=repo, use_llm=False, response_language="roman_urdu")
    return repo, manager, pipeline


def conversation(day: str) -> list[str]:
    return ["Assalam o Alaikum", "Mje docter ke pas jana h", "Dr Ahmed", day, "4 baje", "haan"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure pipeline stage latency.")
    parser.add_argument("--live", action="store_true",
                        help="also time read-only Firestore calls and the Ollama judge")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)

    import torch
    from dialog_manager.intent_router import MBertIntentDetector
    from tests.helpers import StubIntentDetector

    report: dict = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "machine": {"cuda": torch.cuda.is_available(),
                                "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None},
                    "stages": {}}
    stages = report["stages"]

    # --- mBERT ---------------------------------------------------------
    detector = MBertIntentDetector()
    load_ms, _ = timed(detector.predict, "warm up")          # includes model load
    phrases = audit_phrases()
    samples = [timed(detector.predict, p)[0] for p in phrases]
    stages["mbert_intent_detection"] = {**summarise(samples), "first_call_including_model_load_ms": round(load_ms, 1)}

    # --- Dialog Manager logic alone (scripted intents) -----------------
    _, manager, _ = build = local_stack(StubIntentDetector())
    samples = []
    for i in range(10):
        for text in conversation(working_day(1 + i % 5)):
            samples.append(timed(manager.process_message, f"logic{i}", text, "P001")[0])
    stages["dialog_manager_logic_in_memory"] = summarise(samples)

    # --- Full turn, no LLM (real mBERT, in-memory database) -------------
    from dialog_manager.intent_router import IntentRouter
    repo, manager, pipeline = local_stack(detector)
    samples = []
    for i in range(5):
        for text in conversation(working_day(1 + i)):
            samples.append(timed(pipeline.process, f"turn{i}", text, "P001")[0])
    stages["pipeline_turn_no_llm_in_memory"] = summarise(samples)

    # --- Appointment backend alone (in-memory) --------------------------
    from appointment_backend.appointment_service import AppointmentBackend
    from firebase.patient_service import PatientService
    backend = AppointmentBackend(repo)
    PatientService(repo).create_patient("Perf", "03001112233", "PPERF")
    samples = []
    for i, minute in enumerate(("16:00", "16:20", "16:40", "17:00", "17:20", "17:40", "18:00", "18:20")):
        samples.append(timed(backend.book_appointment, "PPERF", "D002", working_day(10 + i), minute)[0])
    stages["appointment_backend_book_in_memory"] = summarise(samples)

    # --- Live, read-only --------------------------------------------------
    if args.live:
        from tests.test_firestore_live import firestore_repository
        live_repo = firestore_repository()
        if live_repo is None:
            stages["firestore_read"] = {"status": "not measured", "reason": "Firebase not configured"}
        else:
            live_backend = AppointmentBackend(live_repo)
            day = working_day(140)
            stages["firestore_get_doctor"] = summarise([timed(live_repo.get, "doctors", "D001")[0] for _ in range(5)])
            stages["firestore_availability"] = summarise(
                [timed(live_backend.availability.get_availability, "D001", day)[0] for _ in range(5)])

        from ollama_judge.judge import OllamaJudge
        from tests.test_ollama_live import outcomes
        judge = OllamaJudge()
        if not judge.available:
            stages["ollama_judge"] = {"status": "not measured", "reason": "Ollama not reachable"}
        else:
            result, text, intent = outcomes()["booked"]
            samples = [timed(judge.judge, text, intent, 0.95, {}, result, language="roman_urdu")[0]
                       for _ in range(3)]
            stages["ollama_judge_roman_urdu"] = summarise(samples)

    for name in ("firestore_live.json", "ollama_live.json"):
        path = REPO / "reports" / name
        if path.exists():
            report.setdefault("from_live_test_runs", {})[name] = json.loads(path.read_text(encoding="utf-8"))

    for stage, reason in (("speech_to_text", "no STT integration exists in the codebase "
                                             "(faster-whisper is installed but not wired in)"),
                          ("text_to_speech", "no TTS integration exists in the codebase"),
                          ("telephony_asterisk_sip", "no Asterisk/SIP integration exists in the codebase")):
        stages[stage] = {"status": "not measured", "reason": reason}

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for stage, values in stages.items():
        print(f"  {stage:38s} {values}")
    print(f"\nwritten to {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
