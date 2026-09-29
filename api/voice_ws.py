"""
/voice/call - one phone call over a WebSocket.

This is the socket a telephony bridge connects to (docs/VOICE.md describes an
Asterisk one; none exists in this project yet), and what the test tools use
to place a simulated call.

    connect  ws://host/voice/call?session_id=CALL_001&patient_id=P001
                                  &format=ulaw_8000&token=<VOICE_GATEWAY_TOKEN>
    send     binary frames: the caller's audio in `format`, as it arrives
             text frame {"type": "end"} when the caller hangs up
    receive  binary frames: the agent's voice, in ELEVENLABS_TTS_OUTPUT_FORMAT
             text frames: events - partial / final transcripts, the turn
             (intent, action, reply), latency, {"type": "stop_playback"} when
             the caller talks over the agent, {"type": "hangup"} at the end

Security: the ElevenLabs key stays on this server; the bridge only ever holds
VOICE_GATEWAY_TOKEN. With no token configured, only this machine may connect.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import config
from speech import results as R
from speech.audio import audio_format
from speech.call import VoiceCall
from speech.keyterms import keyterms_from_repository
from speech.stt import ElevenLabsSTT
from speech.tts import ElevenLabsTTS

logger = logging.getLogger("api.voice")
router = APIRouter(tags=["voice"])

GREETING = R.GREETING
_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}

# Built once: the TTS keeps its fixed-phrase cache across calls.
_services: dict = {}
_pipeline_provider = None


def use_pipeline(provider) -> None:
    """api/main.py hands over the running VoicePipeline."""
    global _pipeline_provider
    _pipeline_provider = provider


def speech_services() -> tuple[ElevenLabsSTT, ElevenLabsTTS]:
    if "stt" not in _services:
        _services["stt"] = ElevenLabsSTT()
        _services["tts"] = ElevenLabsTTS()
    return _services["stt"], _services["tts"]


def _allowed(websocket: WebSocket) -> bool:
    expected = config.VOICE_GATEWAY_TOKEN
    given = websocket.query_params.get("token") or ""
    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    if expected:
        return hmac.compare_digest(given, expected)
    host = websocket.client.host if websocket.client else ""
    return host in _LOCAL


class WebSocketOutput:
    """AudioOutput over the call's own socket."""

    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.closed = False

    async def play(self, chunk: bytes) -> None:
        if not self.closed:
            await self.ws.send_bytes(chunk)

    async def stop(self) -> None:
        if not self.closed:
            await self.ws.send_json({"type": "stop_playback"})

    async def hang_up(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.ws.send_json({"type": "hangup"})
            except Exception:                             # pragma: no cover
                pass


@router.websocket("/voice/call")
async def voice_call(websocket: WebSocket):
    if not _allowed(websocket):
        await websocket.close(code=4401, reason="not allowed")
        return
    await websocket.accept()

    stt, tts = speech_services()
    pipeline = _pipeline_provider() if _pipeline_provider else None
    if pipeline is None or not stt.configured or not tts.configured:
        missing = ("the dialogue pipeline" if pipeline is None
                   else "ELEVENLABS_API_KEY" if not stt.configured
                   else "ELEVENLABS_VOICE_ID")
        await websocket.send_json({"type": "error", "code": R.NOT_CONFIGURED,
                                   "message": f"Voice is not configured ({missing})."})
        await websocket.close(code=1011)
        return

    params = websocket.query_params
    try:
        fmt = audio_format(params.get("format") or config.ELEVENLABS_STT_AUDIO_FORMAT)
    except ValueError:
        await websocket.send_json({"type": "error", "code": R.INVALID_AUDIO,
                                   "message": "Unsupported audio format."})
        await websocket.close(code=1003)
        return

    session_id = params.get("session_id") or f"CALL_{uuid.uuid4().hex[:8].upper()}"
    keyterms = (keyterms_from_repository(pipeline.repo)
                if config.ELEVENLABS_STT_KEYTERMS else None)
    output = WebSocketOutput(websocket)

    async def send_event(event: dict) -> None:
        if not output.closed or event.get("type") == "call_ended":
            try:
                await websocket.send_json(event)
            except Exception:
                pass

    call = VoiceCall(pipeline, stt, tts, output, session_id,
                     patient_id=params.get("patient_id") or None,
                     audio_format_name=fmt.name, keyterms=keyterms,
                     greeting=params.get("greeting") or GREETING, on_event=send_event)
    await send_event({"type": "session", "session_id": session_id,
                      "input_format": fmt.name, "output_format": tts.output_format})
    if not await call.start():
        await websocket.close()
        return
    try:
        while not call.ended:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes"):
                await call.feed(message["bytes"])
            elif message.get("text"):
                if '"end"' in message["text"]:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await call.end(call.end_reason or "caller_hung_up")
        try:
            await websocket.close()
        except Exception:
            pass
        await asyncio.sleep(0)
