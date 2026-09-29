"""
Talk to the appointment system through the computer's microphone and speaker.

    microphone -> ElevenLabs realtime STT -> committed transcript
      -> mBERT -> Dialog Manager -> Appointment Backend -> Firestore
      -> Ollama (wording) -> response validator -> approved reply
      -> ElevenLabs streaming TTS -> speaker / headphones

This is the development stand-in for a phone line (Asterisk/SIP is not
connected yet). It adds only the sound card: the call itself is
speech/call.py's VoiceCall - the same object a telephony bridge drives - and
the conversation is voice_pipeline.VoicePipeline, unchanged.

    python voice_app.py                  # speak naturally (VOICE_INPUT_MODE=vad)
    python voice_app.py --ptt            # push-to-talk: SPACE to start, SPACE to stop
    python voice_app.py --list-devices   # microphones and speakers
    python voice_app.py --mic-test       # check the microphone level (sends nothing)
    python voice_app.py --sound-test     # a short tone on the speaker (no credits)
    python voice_app.py --local          # in-memory demo clinic, not Firestore
    python voice_app.py --no-llm         # replies without Ollama (templates)
    python voice_app.py --debug          # every stage and its timing

Stop with q, Esc or Ctrl+C. Headphones are best: through loudspeakers the
microphone is muted while the agent speaks (so it never transcribes itself),
which means the agent cannot be interrupted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import config
from config import use_utf8_stdout
from speech import microphone as mic_errors
from speech import results as R
from speech.audio import SpeechGate, audio_format, rms, tone
from speech.microphone import MicrophoneError, MicrophoneInput, list_devices, resolve_device
from speech.speaker import SpeakerOutput

logger = logging.getLogger("voice_app")
PTT_WAIT_SECS = 8.0              # longest wait for a push-to-talk transcript


# --------------------------------------------------------------------------
# Terminal
# --------------------------------------------------------------------------
class Console:
    """What the person at the microphone sees. Never the key, never secrets."""

    def __init__(self, debug: bool, speaker: SpeakerOutput | None = None):
        self.debug = debug
        self.speaker = speaker
        self._partial = False

    def line(self, text: str = "") -> None:
        if self._partial:
            print("\r" + " " * 100 + "\r", end="")
            self._partial = False
        print(text, flush=True)

    def partial(self, text: str) -> None:
        if not self.debug:
            return
        shown = text if len(text) <= 90 else "..." + text[-87:]
        print("\r  ... " + shown.ljust(94), end="", flush=True)
        self._partial = True

    def listening(self, mode: str) -> None:
        if mode == "push_to_talk":
            self.line("\nREADY - press SPACE and speak, SPACE again when done (q to quit)")
        else:
            self.line("\nLISTENING... (speak; q or Ctrl+C to stop)")

    def turn(self, event: dict) -> None:
        if not self.debug:
            return
        self.line(f"  mBERT   : {event.get('raw_intent')} ({(event.get('confidence') or 0):.2f})"
                  f" -> {event.get('intent')}")
        self.line(f"  DIALOG  : {event.get('action')}")
        backend = event.get("backend_result")
        if backend:
            error = (backend.get("error") or {}).get("code")
            self.line(f"  BACKEND : {backend.get('operation')} "
                      f"{'ok' if backend.get('success') else 'refused: ' + str(error)}"
                      + (f" ({event['appointment_id']})" if event.get("appointment_id") else ""))
        source = event.get("response_source")
        if source == "ollama":
            self.line("  OLLAMA  : worded the reply, validator approved")
        elif event.get("validation_problems"):
            self.line(f"  OLLAMA  : REJECTED by the validator {event['validation_problems']}"
                      " - the Dialog Manager's sentence is spoken")
        else:
            self.line(f"  OLLAMA  : not used ({source or 'template'})")

    def timing(self, latency: dict) -> None:
        if not self.debug:
            return
        def ms(key):
            value = latency.get(key)
            return "-" if value is None else f"{value:.0f} ms"
        self.line(f"  timing  : you stopped -> transcript {ms('speech_end_to_transcript_ms')}"
                  f" | mBERT {ms('mbert_ms')} | dialog {ms('dialog_ms')}"
                  f" | database {ms('database_ms')} | Ollama {ms('llm_ms')}"
                  f" | TTS first byte {ms('tts_first_byte_ms')}")
        self.line(f"            you stopped -> agent audio {ms('speech_end_to_agent_audio_ms')}"
                  f" (+ speaker {ms('speaker_start_ms')})")


# --------------------------------------------------------------------------
# Keys: SPACE (push-to-talk), q / Esc / Ctrl+C (stop)
# --------------------------------------------------------------------------
def _read_keys(loop: asyncio.AbstractEventLoop, on_key) -> None:
    try:
        import msvcrt                                   # Windows console
    except ImportError:                                 # pragma: no cover
        msvcrt = None
    while True:
        try:
            if msvcrt is not None:
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):             # arrow / function key prefix
                    msvcrt.getwch()
                    continue
            else:                                       # pragma: no cover
                line = sys.stdin.readline()
                if not line:
                    return
                key = line.strip()[:1] or " "           # Enter = SPACE
        except (EOFError, OSError):                     # pragma: no cover
            return
        loop.call_soon_threadsafe(on_key, key)


# --------------------------------------------------------------------------
# Set-up
# --------------------------------------------------------------------------
def _repository(local: bool):
    from firebase.firebase_config import LocalRepository, init_repository, reset_repository
    from firebase.seed_data import seed
    # No sample appointment: APT123 is with Dr Ahmed tomorrow, and the
    # duplicate rule would refuse the usual "Dr Ahmed, kal, 4 baje" booking.
    if local:
        repo = LocalRepository()
        reset_repository(repo)
        seed(repo, with_sample_appointment=False)
        return repo
    repo = init_repository()
    if repo.backend == "local":
        # Like the API: without Firestore, the demo clinic. Firestore itself
        # is never seeded here - that would overwrite the doctors, schedules
        # and patients edited in the dashboard.
        seed(repo, with_sample_appointment=False)
    return repo


def _warm_up(pipeline, console: Console) -> dict:
    """Load mBERT and the Ollama model now, not on the caller's first turn."""
    timings = {}
    started = time.perf_counter()
    console.line("Loading mBERT ...")
    pipeline.dialog.router.route("assalam o alaikum")
    timings["mbert_load_ms"] = round((time.perf_counter() - started) * 1000)
    if pipeline.use_llm and pipeline.judge.available:
        started = time.perf_counter()
        console.line("Loading the Ollama model ...")
        pipeline.judge.phrase(R.GREETING, "Assalam o alaikum", "roman_urdu")
        timings["ollama_load_ms"] = round((time.perf_counter() - started) * 1000)
    return timings


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values)) if values else None


# --------------------------------------------------------------------------
# The conversation
# --------------------------------------------------------------------------
async def converse(args, *, sd=None, mic_sd=None, stt=None, tts=None, pipeline=None,
                   read_keys: bool = True) -> int:
    """The conversation. The keyword arguments replace the real sound card
    (`mic_sd`: a different one for the microphone, e.g. a recorded caller),
    ElevenLabs services and pipeline in tests; the command line passes none."""
    mic_sd = mic_sd if mic_sd is not None else sd
    import httpx
    from speech.call import VoiceCall
    from speech.keyterms import keyterms_from_repository
    from speech.stt import ElevenLabsSTT
    from speech.tts import ElevenLabsTTS
    from voice_pipeline import VoicePipeline

    console = Console(args.debug)
    mode = "push_to_talk" if args.ptt else config.VOICE_INPUT_MODE
    if mode not in ("vad", "push_to_talk"):
        console.line(f"VOICE_INPUT_MODE must be vad or push_to_talk, not {mode!r}")
        return 2
    if stt is None and not config.ELEVENLABS_API_KEY:
        console.line("ELEVENLABS_API_KEY is not set in .env (README: Set up ElevenLabs).")
        return 2
    if tts is None and not config.ELEVENLABS_VOICE_ID:
        console.line("ELEVENLABS_VOICE_ID is not set in .env: python scripts/voice_check.py --voices")
        return 2

    # Devices first: no point loading models without a microphone.
    mic_fmt = audio_format(config.VOICE_MIC_FORMAT)
    out_fmt = audio_format(config.VOICE_TTS_OUTPUT_FORMAT)
    try:
        mic_device = resolve_device(args.mic if args.mic is not None else config.MICROPHONE_DEVICE,
                                    "input", mic_sd)
        speaker_device = resolve_device(
            args.speaker if args.speaker is not None else config.SPEAKER_DEVICE, "output", sd)
    except MicrophoneError as error:
        console.line(error.message)
        return 2
    mic = MicrophoneInput(mic_fmt, mic_device, sd=mic_sd)
    speaker = SpeakerOutput(out_fmt, speaker_device, sd=sd)
    console.speaker = speaker

    http = httpx.AsyncClient(timeout=config.ELEVENLABS_TIMEOUT)   # one TLS connection for every reply
    call, loaded = None, {}
    try:
        if pipeline is None:
            pipeline = VoicePipeline(repository=_repository(args.local),
                                     use_llm=not args.no_llm,
                                     phrase_every_turn=config.VOICE_OLLAMA_EVERY_TURN)
        repo = pipeline.repo
        loaded = await asyncio.to_thread(_warm_up, pipeline, console)
        # Opened after the models: importing torch starves a running audio
        # stream (the speaker counted underruns during the warm-up).
        try:
            speaker.start()
            mic.start()
        except MicrophoneError as error:
            console.line(error.message)
            return 2
        if not pipeline.use_llm:
            llm = "off (--no-llm): template replies"
        elif not pipeline.llm_available:
            llm = "UNAVAILABLE - template replies"
        else:
            llm = (f"{pipeline.judge.service.model}, "
                   + ("every reply" if config.VOICE_OLLAMA_EVERY_TURN else "database results only"))
        console.line("=" * 78)
        console.line("AI VOICE APPOINTMENT SYSTEM - microphone mode")
        console.line("=" * 78)
        console.line(f"microphone : {mic.device_name} ({mic.device_rate} Hz"
                     f"{', converted to ' + str(mic_fmt.sample_rate) if mic._convert else ''})")
        console.line(f"speaker    : {speaker.device_name} ({out_fmt.name})")
        console.line(f"input      : {'push-to-talk' if mode == 'push_to_talk' else 'voice activity'}"
                     f"; {'listening while the agent speaks (barge-in)' if config.VOICE_BARGE_IN else 'microphone muted while the agent speaks'}")
        console.line(f"STT / TTS  : {config.ELEVENLABS_STT_MODEL} / {config.ELEVENLABS_TTS_MODEL}")
        console.line(f"Ollama     : {llm}")
        console.line(f"database   : {repo.backend}   patient: {args.patient}")
        if args.debug:
            console.line(f"warm-up    : {loaded}")
        console.line("=" * 78)

        stt = stt or ElevenLabsSTT(audio_format_name=mic_fmt.name,
                                   commit_strategy="manual" if mode == "push_to_talk" else "vad")
        tts = tts or ElevenLabsTTS(output_format=out_fmt.name, http_client=http)
        keyterms = keyterms_from_repository(repo) if config.ELEVENLABS_STT_KEYTERMS else None
        session_id = "VOICE_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        state = {"recording": False, "waiting_since": None, "quit": False}

        async def on_event(event: dict) -> None:
            kind = event.get("type")
            if kind == "partial":
                console.partial(event.get("text", ""))
            elif kind == "final":
                state["waiting_since"] = None
                console.line(f"PATIENT : {event.get('text')}")
            elif kind == "no_speech":
                state["waiting_since"] = None
                if mode == "push_to_talk":
                    console.line("  (no words were recognised - try again)")
                    console.listening(mode)
                elif args.debug:
                    console.line("  (sound, but no words)")
            elif kind == "turn":
                console.turn(event)
            elif kind == "tts_start":
                speaker.new_reply()
                console.line(f"AGENT   : {event.get('text')}")
                if args.debug:
                    console.line("  TTS     : playing...")
            elif kind == "tts_failed":
                console.line(f"  (could not be spoken: {event.get('code')} - the reply is above)")
            elif kind == "latency":
                record = call.turns[-1]["latency"] if call and call.turns else event
                if speaker.reply_first_played_at and speaker.reply_first_chunk_at:
                    record["speaker_start_ms"] = round(
                        (speaker.reply_first_played_at - speaker.reply_first_chunk_at) * 1000, 1)
                record["speaker_output_latency_ms"] = (
                    None if speaker.output_latency_ms is None else round(speaker.output_latency_ms, 1))
                console.timing(record)
            elif kind == "barge_in":
                console.line("  (interrupted)")
            elif kind == "stt_reconnected":
                console.line("  (speech recognition reconnected)")
            elif kind == "error":
                state["waiting_since"] = None
                console.line(f"  speech error: {event.get('code')}")

        call = VoiceCall(pipeline, stt, tts, speaker, session_id, args.patient,
                         audio_format_name=mic_fmt.name, keyterms=keyterms,
                         greeting=R.GREETING, on_event=on_event,
                         listen_while_speaking=config.VOICE_BARGE_IN,
                         # push-to-talk: the key ends the sentence, not a pause
                         max_silence_ms=600_000 if mode == "push_to_talk" else 3000)
        if not await call.start():
            console.line("Speech recognition could not start (see the message above).")
            return 1

        loop = asyncio.get_running_loop()

        async def toggle() -> None:
            if state["recording"]:
                state["recording"] = False
                if await call.end_utterance():
                    state["waiting_since"] = time.monotonic()
                    console.line("  (processing ...)")
                else:
                    console.line("  (nothing heard)")
                    console.listening(mode)
                return
            if state["waiting_since"] is not None:
                if time.monotonic() - state["waiting_since"] < PTT_WAIT_SECS:
                    console.line("  (still working on your last sentence)")
                    return
                state["waiting_since"] = None
            if call.agent_audible:
                await call.interrupt()
            call.begin_utterance()
            state["recording"] = True
            console.line("RECORDING ... press SPACE when you have finished")

        def on_key(key: str) -> None:
            if key in ("q", "Q", "\x1b", "\x03"):
                state["quit"] = True
            elif key == " " and mode == "push_to_talk":
                loop.create_task(toggle())

        if read_keys:
            threading.Thread(target=_read_keys, args=(loop, on_key), daemon=True).start()

        while call.agent_audible and not call.ended:  # let the greeting finish
            await asyncio.sleep(0.05)
        mic.drain()                                  # audio from the warm-up and the greeting
        console.listening(mode)
        was_audible, warned_blocked = False, 0.0
        while not call.ended and not state["quit"]:
            try:
                chunk = await mic.read()
            except MicrophoneError as error:
                if error.code == mic_errors.BLOCKED:
                    if time.monotonic() - warned_blocked > 30:
                        console.line(error.message)
                        warned_blocked = time.monotonic()
                    mic._zeros_since = None
                    continue
                console.line(error.message)
                if not await _reopen(mic, console, state):
                    await call.end("microphone_lost")
                    break
                continue
            audible = call.agent_audible
            if was_audible and not audible and not call.ended:
                console.listening(mode)             # the agent has finished
            was_audible = audible
            if mode == "push_to_talk" and not state["recording"]:
                continue                            # the key is up: nothing is sent
            await call.feed(chunk)
        if not call.ended:
            await call.end("stopped_by_user")
        await speaker.drain()
        _summary(call, console, args, loaded, mic, speaker, mode)
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        if call is not None and not call.ended:
            await call.end("stopped_by_user")
            _summary(call, console, args, loaded, mic, speaker, mode)
        return 0
    finally:
        mic.close()
        speaker.close()
        await http.aclose()


async def _reopen(mic: MicrophoneInput, console: Console, state: dict,
                  attempts: int = 15) -> bool:
    """The microphone went away: wait for it (or another default) to come back."""
    mic.close()
    for _ in range(attempts):
        if state["quit"]:
            return False
        await asyncio.sleep(2.0)
        try:
            mic.device = resolve_device(config.MICROPHONE_DEVICE if mic.device is not None else "",
                                        "input", mic._sd)
            mic.start()
        except MicrophoneError:
            continue
        console.line(f"  (microphone back: {mic.device_name})")
        return True
    console.line("The microphone did not come back - ending the conversation.")
    return False


def _summary(call, console: Console, args, loaded, mic, speaker, mode) -> None:
    turns = call.turns
    console.line("\n" + "=" * 78)
    console.line(f"conversation ended ({call.end_reason}), {len(turns)} turn(s)")
    if turns:
        def med(key):
            return _median([t["latency"].get(key) for t in turns])
        console.line(f"median: you stopped -> agent audio {med('speech_end_to_agent_audio_ms')} ms"
                     f" | transcript {med('speech_end_to_transcript_ms')} ms"
                     f" | Ollama {med('llm_ms')} ms | TTS first byte {med('tts_first_byte_ms')} ms")
    console.line(f"usage : {call.usage()}")
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_id": call.session_id, "mode": mode, "end_reason": call.end_reason,
            "microphone": {"device": mic.device_name, "rate": mic.device_rate,
                           "overflows": mic.overflows},
            "speaker": {"device": speaker.device_name, "format": speaker.fmt.name,
                        "output_latency_ms": speaker.output_latency_ms,
                        "underruns": speaker.underruns},
            "models": {"stt": config.ELEVENLABS_STT_MODEL, "tts": config.ELEVENLABS_TTS_MODEL,
                       "ollama": None if args.no_llm else config.OLLAMA_MODEL,
                       "ollama_every_turn": config.VOICE_OLLAMA_EVERY_TURN},
            "warm_up": loaded, "turns": turns, "usage": call.usage(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        console.line(f"report: {path} - it contains what was said; keep real conversations in reports/voice/private/, which Git ignores")


# --------------------------------------------------------------------------
# Checks that need no API credits
# --------------------------------------------------------------------------
def print_devices() -> int:
    try:
        rows = list_devices()
    except MicrophoneError as error:
        print(error.message)
        return 2
    for title, key, default in (("Microphones", "inputs", rows["default_input"]),
                                ("Speakers / headphones", "outputs", rows["default_output"])):
        print(title)
        for row in rows[key]:
            mark = "*" if row["index"] == default else " "
            print(f"  {mark} {row['index']:>3}  {row['name']}  ({row['sample_rate']} Hz)")
    print("\n* = system default. Choose with MICROPHONE_DEVICE / SPEAKER_DEVICE in .env"
          " (the number or part of the name).")
    return 0


async def mic_test(args, seconds: float = 6.0) -> int:
    """A level meter: is the microphone working and loud enough? Nothing is
    sent anywhere and nothing is saved."""
    fmt = audio_format(config.VOICE_MIC_FORMAT)
    try:
        device = resolve_device(args.mic if args.mic is not None else config.MICROPHONE_DEVICE,
                                "input")
        mic = MicrophoneInput(fmt, device)
        mic.start()
    except MicrophoneError as error:
        print(error.message)
        return 2
    gate = SpeechGate(fmt)            # only for its settings: threshold, noise factor
    print(f"microphone: {mic.device_name} - speak for {seconds:g} seconds ...")
    levels: list[float] = []
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                chunk = await mic.read()
            except MicrophoneError as error:
                print("\n" + error.message)
                return 2
            levels.append(rms(chunk, fmt))
            if len(levels) % 5 == 0:                  # redraw every 100 ms
                level = max(levels[-5:])
                bar = "#" * min(50, int(level * 500))
                print(f"\r  level {level:.3f} |{bar:<50}|", end="", flush=True)
    finally:
        mic.close()
    if not levels:
        print("\nNo audio arrived from the microphone.")
        return 2
    # The same rule as the call's speech gate: loud enough, and clearly above
    # the room's own noise (its quietest tenth).
    noise = sorted(levels)[len(levels) // 10]
    threshold = max(gate.threshold, noise * gate.noise_factor)
    loudest = max(levels)
    speech = sum(1 for level in levels if level > threshold)
    print(f"\nloudest {loudest:.3f}; background noise {noise:.4f}; speech threshold "
          f"{threshold:.3f}; {speech} of {len(levels)} blocks above it")
    if loudest < threshold:
        print("Too quiet to be detected as speech: move closer, raise the input volume "
              "in Windows, or use push-to-talk (--ptt).")
    else:
        print("The microphone works and speech can be detected.")
    return 0


async def sound_test(args) -> int:
    """A one-second tone on the speaker - no ElevenLabs credits."""
    fmt = audio_format(config.VOICE_TTS_OUTPUT_FORMAT)
    try:
        device = resolve_device(args.speaker if args.speaker is not None
                                else config.SPEAKER_DEVICE, "output")
        speaker = SpeakerOutput(fmt, device)
        speaker.start()
    except MicrophoneError as error:
        print(error.message)
        return 2
    print(f"speaker: {speaker.device_name} - playing a tone ...")
    try:
        await speaker.play(tone(fmt, 1000, amplitude=0.2, frequency=440.0))
        await speaker.drain()
    finally:
        speaker.close()
    print(f"played {speaker.played_bytes} bytes; driver output latency "
          f"{speaker.output_latency_ms or 0:.0f} ms. Did you hear it?")
    return 0


# --------------------------------------------------------------------------
def main() -> int:                                          # pragma: no cover
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description="Talk to the appointment system by voice.")
    parser.add_argument("--ptt", action="store_true", help="push-to-talk (SPACE)")
    parser.add_argument("--mic", help="microphone: number or part of the name")
    parser.add_argument("--speaker", help="speaker: number or part of the name")
    parser.add_argument("--patient", default="P001", help="patient_id (default P001)")
    parser.add_argument("--local", action="store_true",
                        help="in-memory demo clinic instead of Firestore")
    parser.add_argument("--no-llm", action="store_true", help="replies without Ollama")
    parser.add_argument("--debug", action="store_true", default=config.VOICE_DEBUG,
                        help="show every stage (default: VOICE_DEBUG)")
    parser.add_argument("--verbose", action="store_true", help="INFO logs from every module")
    parser.add_argument("--report", help="save the turns and timings as JSON - it contains "
                        "what was said: use reports/voice/private/ (ignored by Git)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--mic-test", action="store_true")
    parser.add_argument("--sound-test", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)-7s %(name)s | %(message)s")
    if args.list_devices:
        return print_devices()
    if args.mic_test:
        return asyncio.run(mic_test(args))
    if args.sound_test:
        return asyncio.run(sound_test(args))
    try:
        return asyncio.run(converse(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":                                  # pragma: no cover
    sys.exit(main())
