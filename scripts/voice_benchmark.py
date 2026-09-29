"""
Measure the voice layer against the real ElevenLabs service.

    python scripts/voice_benchmark.py tts      # models x languages: first audio, total, samples
    python scripts/voice_benchmark.py stt      # caller sentences -> realtime STT -> mBERT
    python scripts/voice_benchmark.py calls    # whole calls: caller stops -> agent starts
    python scripts/voice_benchmark.py all

Options
    --models eleven_flash_v2_5,eleven_v3_conversational   TTS models to compare
    --caller-voice <voice_id>   voice that plays the CALLER (default: the first
                                account voice that is not ELEVENLABS_VOICE_ID)
    --recordings <dir>          real recordings instead of / as well as synthetic
                                callers: 16-bit WAV files named
                                <expected_intent>__<anything>.wav
    --runs 2                    repetitions per TTS measurement
    --llm                       include the Ollama judge in call turns (off: the
                                deterministic replies, as with OLLAMA_ENABLED=false)

Everything runs against the in-memory database seeded with the demo clinic,
so no real appointment is created. Results go to reports/voice/*.json; the
TTS samples to reports/voice/samples/ for listening (not committed).

Honesty notes, also in docs/VOICE.md:
  * synthetic callers are ElevenLabs voices reading the test sentences - an
    approximation of a patient on a phone line, not a substitute for one;
  * pronunciation is judged by listening to the samples, not by this script;
  * latency is measured from this machine, on its network.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

import config  # noqa: E402
from speech.audio import audio_format, pack_frames, read_wav, rms, wav_bytes  # noqa: E402
from speech.stt import ElevenLabsSTT  # noqa: E402
from speech.tts import ElevenLabsTTS  # noqa: E402

OUT = REPO / "reports" / "voice"
SAMPLES = OUT / "samples"
CALLERS = OUT / "caller_clips"
FMT = audio_format("ulaw_8000")                 # the telephone path
FRAME = FMT.bytes_for(20)

# What the agent says (TTS benchmark)
AGENT_LINES = [
    ("roman_urdu", "Ji bilkul. Kis doctor ke liye appointment chahiye?"),
    ("roman_urdu", "Kal 3 aur 5 baje ke slots available hain."),
    ("roman_urdu", "Aapki appointment successfully confirm ho gayi hai."),
    ("roman_urdu", "Doctor kal clinic mein available nahi hain."),
    ("roman_urdu", "4 baje ka slot booked hai. 5 baje available hai. Kya 5 baje kar doon?"),
    ("urdu", "جی بالکل۔ کس ڈاکٹر کے لیے اپائنٹمنٹ چاہیے؟"),
    ("urdu", "آپ کی اپائنٹمنٹ کامیابی سے کنفرم ہو گئی ہے۔"),
    ("english", "Sure. Which doctor would you like to see?"),
    ("english", "Your appointment with Dr Ahmed Khan is confirmed for tomorrow at 4 PM."),
    ("mixed", "Aapki appointment Dr Ahmed Khan ke saath kal 4 PM confirm ho gayi hai."),
    ("long", "Dr Ahmed Khan kal shaam 4 baje, 4:20 aur 5 baje available hain. Parson "
             "subah 10 baje bhi ek slot khali hai. Aap kaunsa time prefer karenge, ya "
             "main kisi aur doctor ka time dekh loon?"),
]

# What the caller says (STT benchmark): text, expected intent after mBERT
CALLER_LINES = [
    ("english", "I want an appointment with Dr Ahmed.", "book_appointment"),
    ("urdu", "مجھے ڈاکٹر احمد سے اپائنٹمنٹ چاہیے", "book_appointment"),
    ("roman_urdu", "Mujhe Dr Ahmed se appointment chahiye.", "book_appointment"),
    ("informal", "Mje doctor ke pas jana h.", "book_appointment"),
    ("critical", "Mje docter ke pas jana h", "book_appointment"),
    ("mixed", "Dr Ahmed kal available hain?", "doctor_availability"),
    ("mixed", "Meri appointment cancel kar dein.", "cancel_appointment"),
    ("mixed", "Dr Ahmed ki fee kitni hai?", "doctor_information"),
    ("roman_urdu", "Allah Hafiz", "goodbye"),
    ("english", "Thank you, bye", "goodbye"),
]


def day_word() -> str:
    tomorrow = datetime.now(config.TIMEZONE).date() + timedelta(days=1)
    return "kal" if tomorrow.weekday() != 6 else "parson"


CALLS = {
    "booking": [f"Mje Dr Ahmed se {day_word()} appointment chahiye.", "4 baje.",
                "Haan kar dein."],
    "cancellation": ["Meri appointment cancel kar dein.", "Haan ji."],
    "rescheduling": ["Meri appointment Friday ko 5 baje kar dein.", "Haan."],
    "doctor_fee": ["Dr Ahmed ki fee kitni hai?"],
    "availability": [f"Dr Ahmed {day_word()} available hain?"],
    "goodbye": ["Shukriya, Allah Hafiz"],
}

# The same calls with the caller's words written in Urdu script. A caller
# SPEAKS Urdu; Roman Urdu is only a way of writing it, and a synthetic voice
# reading Roman Urdu pronounces it like English. Reading Urdu script, the
# voice pronounces the words as Urdu - a closer stand-in for a real caller.
_KAL = "کل" if day_word() == "kal" else "پرسوں"
CALLS_URDU_SCRIPT = {
    "booking": [f"مجھے ڈاکٹر احمد سے {_KAL} اپائنٹمنٹ چاہیے۔", "چار بجے۔", "ہاں کر دیں۔"],
    "cancellation": ["میری اپائنٹمنٹ کینسل کر دیں۔", "ہاں جی۔"],
    "rescheduling": ["میری اپائنٹمنٹ جمعہ کو پانچ بجے کر دیں۔", "ہاں۔"],
    "doctor_fee": ["ڈاکٹر احمد کی فیس کتنی ہے؟"],
    "availability": [f"ڈاکٹر احمد {_KAL} available ہیں؟"],
    "goodbye": ["شکریہ، اللہ حافظ۔"],
}


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def save(name: str, data) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


async def account_voices() -> list[dict]:
    async with httpx.AsyncClient(timeout=config.ELEVENLABS_TIMEOUT) as client:
        response = await client.get(f"{config.ELEVENLABS_API_URL}/v1/voices",
                                    headers={"xi-api-key": config.ELEVENLABS_API_KEY})
    response.raise_for_status()
    return response.json().get("voices", [])


# --------------------------------------------------------------------------
# TTS
# --------------------------------------------------------------------------
async def bench_tts(models: list[str], runs: int) -> dict:
    stt = ElevenLabsSTT(language="")                 # back-transcribe, language detected
    rows = []
    for model in models:
        tts = ElevenLabsTTS(model=model, output_format=FMT.name)
        for index, (language, text) in enumerate(AGENT_LINES):
            firsts, totals, audio, error = [], [], b"", None
            for _ in range(runs):
                result = await tts.synthesize(text)
                if not result.success:
                    error = result.error
                    break
                firsts.append(result.first_byte_ms)
                totals.append(result.total_ms)
                audio = result.audio
            row = {"model": model, "language": language, "text": text,
                   "characters": len(text), "error": error,
                   "first_audio_ms": median(firsts), "complete_ms": median(totals),
                   "speech_seconds": round(FMT.duration_ms(len(audio)) / 1000, 2)}
            if audio:
                path = SAMPLES / model / f"{index:02d}_{language}.wav"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(wav_bytes(audio, FMT))
                row["sample"] = str(path.relative_to(REPO))
                heard = await stt.transcribe(audio, FMT.name)
                row["heard_back"] = heard.text if heard.success else f"[{heard.error}]"
                row["heard_language"] = heard.language
            rows.append(row)
            print(f"{model:26s} {language:10s} first {row['first_audio_ms']} ms, "
                  f"complete {row['complete_ms']} ms  {error or ''}")
    summary = {}
    for model in models:
        mine = [r for r in rows if r["model"] == model and not r["error"]]
        summary[model] = {"lines": len(mine),
                          "first_audio_ms_median": median(r["first_audio_ms"] for r in mine),
                          "complete_ms_median": median(r["complete_ms"] for r in mine),
                          "failed": [r["language"] for r in rows
                                     if r["model"] == model and r["error"]]}
    return {"format": FMT.name, "voice_id": config.ELEVENLABS_VOICE_ID, "runs": runs,
            "summary": summary, "lines": rows}


# --------------------------------------------------------------------------
# Caller audio (synthetic, cached so it is paid for once) and recordings
# --------------------------------------------------------------------------
async def caller_clip(text: str, voice: str) -> bytes:
    import hashlib
    key = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]
    path = CALLERS / f"{key}.ulaw"
    if path.exists():
        return path.read_bytes()
    # v3 speaks Urdu; the agent's model may not.
    tts = ElevenLabsTTS(model="eleven_v3", voice_id=voice, output_format=FMT.name)
    result = await tts.synthesize(text)
    if not result.success:
        tts = ElevenLabsTTS(model="eleven_multilingual_v2", voice_id=voice,
                            output_format=FMT.name)
        result = await tts.synthesize(text)
    if not result.success:
        raise RuntimeError(f"could not synthesise the caller line: {result.error}")
    CALLERS.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.audio)
    return result.audio


def speech_span(audio: bytes) -> float:
    """Where speech ends in a clip (ms from its start)."""
    last = 0
    for index, frame in enumerate(pack_frames(audio, FRAME)):
        if rms(frame, FMT) > 0.02:
            last = index + 1
    return last * 20.0


async def transcribe_live(audio: bytes, stt: ElevenLabsSTT) -> dict:
    """Stream a clip at real-time pace, then 1.5 s of line silence."""
    started = time.perf_counter()
    session = await stt.open_realtime(FMT.name)
    connect_ms = session.connect_ms
    speech_end_ms = speech_span(audio)
    stream = audio + b"\xff" * FMT.bytes_for(1500)
    t0 = time.perf_counter()
    marks = {"first_partial_ms": None, "final_ms": None}
    final = {"text": "", "language": None, "error": None}

    async def listen():
        while True:
            event = await session.next_event()
            at = (time.perf_counter() - t0) * 1000
            if event.kind == "partial" and marks["first_partial_ms"] is None and event.text:
                marks["first_partial_ms"] = at
            elif event.kind == "final":
                marks["final_ms"] = at
                final.update(text=event.text, language=event.language)
                return
            elif event.kind in ("error", "closed"):
                final["error"] = event.error or event.kind
                return
    listener = asyncio.create_task(listen())
    for piece in pack_frames(stream, FMT.bytes_for(100)):
        if listener.done():
            break
        await session.send(piece)
        await asyncio.sleep(0.1)
    try:
        await asyncio.wait_for(listener, timeout=8)
    except asyncio.TimeoutError:
        final["error"] = final["error"] or "no_commit"
    await session.close()
    return {"connect_ms": round(connect_ms or 0, 1),
            "first_partial_ms": round(marks["first_partial_ms"], 1)
            if marks["first_partial_ms"] is not None else None,
            "speech_end_ms": speech_end_ms,
            "final_after_speech_end_ms": round(marks["final_ms"] - speech_end_ms, 1)
            if marks["final_ms"] is not None else None,
            "transcript": final["text"], "language": final["language"],
            "error": final["error"], "wall_ms": round((time.perf_counter() - started) * 1000)}


async def bench_stt(caller_voice: str, recordings: str | None) -> dict:
    from dialog_manager.intent_router import IntentRouter
    router = IntentRouter()
    stt = ElevenLabsSTT()
    clips = []
    for kind, text, expected in CALLER_LINES:
        clips.append({"source": "synthetic", "kind": kind, "said": text,
                      "expected": expected, "audio": await caller_clip(text, caller_voice)})
    if recordings:
        for path in sorted(Path(recordings).glob("*.wav")):
            expected = path.stem.split("__")[0]
            clips.append({"source": "recording", "kind": "recording", "said": path.name,
                          "expected": expected, "audio": read_wav(path.read_bytes(), FMT)})
    # what must NOT become a turn
    from speech.audio import tone
    clips.append({"source": "synthetic", "kind": "silence", "said": "(2 s of silence)",
                  "expected": None, "audio": tone(FMT, 2000)})
    clips.append({"source": "synthetic", "kind": "noise", "said": "(2 s of line noise)",
                  "expected": None, "audio": tone(FMT, 2000, noise=0.05)})
    rows = []
    for clip in clips:
        measured = await transcribe_live(clip["audio"], stt)
        routed = router.route(measured["transcript"]) if measured["transcript"] else None
        intent = routed.intent if routed else None
        passed = (intent == clip["expected"]) if clip["expected"] else (not measured["transcript"])
        rows.append({k: v for k, v in clip.items() if k != "audio"} | measured
                    | {"intent": intent,
                       "confidence": round(routed.confidence, 3) if routed else None,
                       "pass": passed})
        print(f"{'PASS' if passed else 'FAIL'} {clip['kind']:10s} {clip['said'][:38]:38s} -> "
              f"{measured['transcript'][:40]!r} [{intent}] final +"
              f"{measured['final_after_speech_end_ms']} ms")
    speech_rows = [r for r in rows if r["expected"]]
    return {"model": stt.model, "language": stt.language or "auto",
            "secondary_languages": stt.secondary_languages, "format": FMT.name,
            "silence_secs": stt.silence_secs, "caller_voice": caller_voice,
            "summary": {
                "passed": sum(r["pass"] for r in rows), "total": len(rows),
                "connect_ms_median": median(r["connect_ms"] for r in rows),
                "first_partial_ms_median": median(r["first_partial_ms"] for r in speech_rows),
                "final_after_speech_end_ms_median":
                    median(r["final_after_speech_end_ms"] for r in speech_rows)},
            "clips": rows}


# --------------------------------------------------------------------------
# Whole calls
# --------------------------------------------------------------------------
class TimedOutput:
    """Plays at telephone speed: the next caller turn waits for the reply."""

    def __init__(self):
        self.playing_until = 0.0
        self.audio = 0
        self.stops = 0
        self.hung_up = False

    async def play(self, chunk):
        now = time.monotonic()
        self.playing_until = max(self.playing_until, now) + FMT.duration_ms(len(chunk)) / 1000
        self.audio += len(chunk)

    async def stop(self):
        self.stops += 1
        self.playing_until = 0.0

    async def hang_up(self):
        self.hung_up = True


def warm_up(pipeline, use_llm: bool) -> dict:
    """Load mBERT, and llama3.2 in Ollama, before anything is timed - the
    numbers are for a running system. What loading costs is recorded too."""
    timings = {}
    started = time.perf_counter()
    pipeline.dialog.router.route("warm up")
    timings["mbert_load_ms"] = round((time.perf_counter() - started) * 1000)
    if use_llm:
        started = time.perf_counter()
        try:
            httpx.post(f"{config.OLLAMA_BASE_URL}/api/generate", timeout=120,
                       json={"model": config.OLLAMA_MODEL, "prompt": "ok", "stream": False,
                             "options": {"num_predict": 1}})
        except httpx.HTTPError:
            pass
        timings["ollama_first_request_ms"] = round((time.perf_counter() - started) * 1000)
    return timings


async def bench_calls(caller_voice: str, use_llm: bool, script: str = "roman") -> dict:
    from firebase.appointment_service import AppointmentService
    from firebase.firebase_config import LocalRepository, reset_repository
    from firebase.patient_service import PatientService
    from firebase.seed_data import seed
    from speech.call import VoiceCall
    from voice_pipeline import VoicePipeline

    repo = LocalRepository()
    reset_repository(repo)
    seed(repo, with_sample_appointment=True)
    pipeline = VoicePipeline(repository=repo, use_llm=use_llm)
    warm = warm_up(pipeline, use_llm)
    print("warm-up:", warm)
    patients, appointments = PatientService(repo), AppointmentService(repo)
    stt, tts = ElevenLabsSTT(), ElevenLabsTTS()
    results = {}
    calls = CALLS_URDU_SCRIPT if script == "urdu" else CALLS
    for number, (flow, lines) in enumerate(calls.items(), start=1):
        patient = patients.create_patient(f"Benchmark Caller {number}",
                                          f"+92300888{number:04d}").data["patient_id"]
        if flow in ("cancellation", "rescheduling"):
            tomorrow = datetime.now(config.TIMEZONE).date() + timedelta(days=1)
            if tomorrow.weekday() == 6:
                tomorrow += timedelta(days=1)
            free = appointments.get_available_slots("D001", tomorrow.isoformat()).data
            appointments.book_appointment(patient, "D001", tomorrow.isoformat(), free[-1])
        output = TimedOutput()
        call = VoiceCall(pipeline, stt, tts, output, f"BENCH_{flow.upper()}", patient,
                         FMT.name)
        if not await call.start():
            results[flow] = {"error": call.end_reason}
            continue
        for line in lines:
            audio = await caller_clip(line, caller_voice) + b"\xff" * FMT.bytes_for(1500)
            done = len(call.turns) + 1
            for frame in pack_frames(audio, FRAME * 5):
                await call.feed(frame)
                await asyncio.sleep(0.1)
            deadline = time.monotonic() + 25
            while len(call.turns) < done and not call.ended and time.monotonic() < deadline:
                await call.feed(b"\xff" * FMT.bytes_for(100))     # the line stays open
                await asyncio.sleep(0.1)
            while time.monotonic() < output.playing_until:        # let the reply play
                await asyncio.sleep(0.1)
        await call.end("benchmark_done")
        results[flow] = {"turns": call.turns, "usage": call.usage(),
                         "end_reason": call.end_reason}
        for turn in call.turns:
            print(f"{flow:13s} {turn['transcript'][:34]!r:36s} {turn['action']:30s} "
                  f"stop->speak {turn['latency']['speech_end_to_agent_audio_ms']} ms")
    turns = [t for r in results.values() for t in r.get("turns", [])]
    key = "speech_end_to_agent_audio_ms"
    return {"llm": use_llm, "tts_model": tts.model, "stt_model": stt.model,
            "caller_text": script, "caller_voice": caller_voice, "warm_up": warm,
            "summary": {
                "turns": len(turns),
                "speech_end_to_agent_audio_ms_median": median(t["latency"][key] for t in turns),
                "speech_end_to_agent_audio_ms_max": max(
                    (t["latency"][key] for t in turns if t["latency"][key] is not None),
                    default=None),
                "speech_end_to_transcript_ms_median":
                    median(t["latency"]["speech_end_to_transcript_ms"] for t in turns),
                "pipeline_ms_median": median(t["latency"]["pipeline_ms"] for t in turns),
                "tts_first_byte_ms_median":
                    median(t["latency"]["tts_first_byte_ms"] for t in turns)},
            "calls": results}


# --------------------------------------------------------------------------
async def run(args) -> None:
    if not config.ELEVENLABS_API_KEY:
        raise SystemExit("ELEVENLABS_API_KEY is not set in .env (README, 'ElevenLabs setup').")
    if not config.ELEVENLABS_VOICE_ID:
        raise SystemExit("ELEVENLABS_VOICE_ID is not set: python scripts/voice_check.py --voices")
    caller = args.caller_voice
    if args.part in ("stt", "calls", "all") and not caller:
        others = [v["voice_id"] for v in await account_voices()
                  if v["voice_id"] != config.ELEVENLABS_VOICE_ID]
        if not others:
            raise SystemExit("Need a second voice to play the caller: --caller-voice")
        caller = others[0]
    if args.part in ("tts", "all"):
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        print("saved", save("tts", await bench_tts(models, args.runs)))
    if args.part in ("stt", "all"):
        print("saved", save("stt", await bench_stt(caller, args.recordings)))
    if args.part in ("calls", "all"):
        name = "calls" if args.caller_script == "roman" else "calls_urdu_script"
        print("saved", save(name, await bench_calls(caller, args.llm, args.caller_script)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("part", choices=["tts", "stt", "calls", "all"])
    parser.add_argument("--models", default="eleven_flash_v2_5,eleven_v3_conversational,"
                                            "eleven_multilingual_v2")
    parser.add_argument("--caller-voice")
    parser.add_argument("--recordings")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--caller-script", choices=["roman", "urdu"], default="roman",
                        help="write the simulated caller's words in Roman or Urdu script")
    args = parser.parse_args()
    config.use_utf8_stdout()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
