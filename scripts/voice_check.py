"""
Quick checks against the real ElevenLabs service.

    python scripts/voice_check.py --voices                 # pick ELEVENLABS_VOICE_ID
    python scripts/voice_check.py --tts "Ji bilkul. Kis doctor ke liye appointment chahiye?"
    python scripts/voice_check.py --tts "..." --model eleven_v3_conversational --out hello.wav
    python scripts/voice_check.py --stt recording.wav      # 16-bit PCM WAV, any rate

Reads ELEVENLABS_API_KEY from .env; never prints it. Uses credits: a TTS
check costs its characters, an STT check the length of the recording.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

import config  # noqa: E402
from speech.audio import audio_format, pack_frames, read_wav, wav_bytes  # noqa: E402
from speech.stt import ElevenLabsSTT  # noqa: E402
from speech.tts import ElevenLabsTTS  # noqa: E402


def need_key() -> None:
    if not config.ELEVENLABS_API_KEY:
        raise SystemExit("ELEVENLABS_API_KEY is not set in .env - see README, "
                         "'ElevenLabs setup'.")


async def list_voices() -> None:
    async with httpx.AsyncClient(timeout=config.ELEVENLABS_TIMEOUT) as client:
        response = await client.get(f"{config.ELEVENLABS_API_URL}/v1/voices",
                                    headers={"xi-api-key": config.ELEVENLABS_API_KEY})
    if response.status_code != 200:
        try:
            detail = response.json().get("detail") or {}
        except ValueError:
            detail = {}
        if isinstance(detail, dict) and detail.get("status") == "missing_permissions":
            raise SystemExit(
                "The API key works, but it does not have the 'Voices: Read' permission.\n"
                "Either enable it on the key (ElevenLabs -> Developers -> API keys -> edit),\n"
                "or copy a voice ID from the ElevenLabs Voice Library and put it in .env\n"
                "as ELEVENLABS_VOICE_ID.")
        raise SystemExit(f"ElevenLabs refused the request ({response.status_code}).")
    voices = response.json().get("voices", [])
    configured = config.ELEVENLABS_VOICE_ID
    print(f"{len(voices)} voices available to this account:\n")
    for voice in sorted(voices, key=lambda v: v.get("name", "")):
        labels = voice.get("labels") or {}
        mark = "*" if voice.get("voice_id") == configured else " "
        library = voice.get("category") not in (None, "premade")
        print(f"{mark} {voice.get('voice_id')}  {voice.get('name', ''):22s} "
              + ("[Voice Library - API needs a paid plan] " if library else "")
              + ", ".join(f"{k}={v}" for k, v in labels.items()))
    chosen = next((v for v in voices if v.get("voice_id") == configured), None)
    if chosen:
        print(f"\nELEVENLABS_VOICE_ID is set: {configured} ({chosen.get('name')}), marked *.")
        if chosen.get("category") not in (None, "premade"):
            print("It is a Voice Library voice: on a FREE ElevenLabs plan the API refuses it\n"
                  "(402 paid_plan_required). Upgrade the plan, or choose a voice without\n"
                  "the [Voice Library] mark.")
    elif configured:
        async with httpx.AsyncClient(timeout=config.ELEVENLABS_TIMEOUT) as client:
            shared = await client.get(f"{config.ELEVENLABS_API_URL}/v1/shared-voices",
                                      params={"search": configured, "page_size": 5},
                                      headers={"xi-api-key": config.ELEVENLABS_API_KEY})
        found = next((v for v in (shared.json().get("voices", [])
                                  if shared.status_code == 200 else [])
                      if v.get("voice_id") == configured), None)
        if found:
            print(f"\nELEVENLABS_VOICE_ID is {configured}: '{found.get('name')}' from the "
                  f"public Voice Library (language {found.get('language')}).\n"
                  "Library voices work through the API only on a PAID ElevenLabs plan; on a\n"
                  "free plan choose one of the voices above without the [Voice Library] mark.")
        else:
            print(f"\nELEVENLABS_VOICE_ID is {configured}, which is not a voice in this account "
                  "or in the Voice Library - check it for a typo.")
    else:
        print("\nPut the one you choose in .env as ELEVENLABS_VOICE_ID.")


async def tts(text: str, model: str | None, out: str | None) -> None:
    service = ElevenLabsTTS(model=model or None)
    if not service.voice_id:
        raise SystemExit("ELEVENLABS_VOICE_ID is not set - run with --voices first.")
    result = await service.synthesize(text)
    if not result.success:
        raise SystemExit(f"TTS failed: {result.error}")
    fmt = audio_format(service.output_format) if not service.output_format.startswith("mp3") \
        else None
    path = Path(out or "tts_check.wav" if fmt else out or "tts_check.mp3")
    path.write_bytes(wav_bytes(result.audio, fmt) if fmt else result.audio)
    seconds = fmt.duration_ms(len(result.audio)) / 1000 if fmt else None
    print(f"model {service.model}, voice {service.voice_id}, {len(text)} characters")
    print(f"first audio after {result.first_byte_ms:.0f} ms, complete after "
          f"{result.total_ms:.0f} ms" + (f", {seconds:.1f} s of speech" if seconds else ""))
    print(f"saved {path}")


async def stt(path: str) -> None:
    service = ElevenLabsSTT()
    fmt = audio_format(service.audio_format_name)
    audio = read_wav(Path(path).read_bytes(), fmt)
    session = await service.open_realtime(fmt.name)
    print(f"connected in {session.connect_ms:.0f} ms ({service.model}, {fmt.name}, "
          f"language {service.language or 'auto'})")
    frame = fmt.bytes_for(20)
    started = time.perf_counter()

    async def listen():
        while True:
            event = await session.next_event()
            at = (time.perf_counter() - started) * 1000
            if event.kind == "partial":
                print(f"  {at:7.0f} ms  partial   {event.text}")
            elif event.kind == "final":
                print(f"  {at:7.0f} ms  COMMITTED {event.text!r} (language "
                      f"{event.language or 'not reported'})")
            elif event.kind in ("error", "closed"):
                print(f"  {at:7.0f} ms  {event.kind} {event.error or ''}")
                return
    listener = asyncio.create_task(listen())
    for piece in pack_frames(audio + b"\xff" * fmt.bytes_for(1200) if fmt.encoding == "ulaw"
                             else audio + b"\x00" * fmt.bytes_for(1200), frame * 5):
        await session.send(piece)
        await asyncio.sleep(0.1)              # real time: 100 ms of audio per 100 ms
    print(f"  {(time.perf_counter() - started) * 1000:7.0f} ms  audio finished")
    await asyncio.sleep(3)
    await session.close()
    listener.cancel()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--voices", action="store_true")
    parser.add_argument("--tts")
    parser.add_argument("--model")
    parser.add_argument("--out")
    parser.add_argument("--stt")
    args = parser.parse_args()
    config.use_utf8_stdout()
    need_key()
    if args.voices:
        asyncio.run(list_voices())
    elif args.tts:
        asyncio.run(tts(args.tts, args.model, args.out))
    elif args.stt:
        asyncio.run(stt(args.stt))
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
