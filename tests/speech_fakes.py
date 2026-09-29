"""
Stand-ins for ElevenLabs, for the offline speech tests.

FakeScribe is a real WebSocket server on 127.0.0.1 that follows ElevenLabs'
documented realtime protocol - the `xi-api-key` header, `session_started`,
`input_audio_chunk` messages, `partial_transcript` while audio arrives,
`committed_transcript` when its silence detector (or `commit: true`) ends the
turn, and the documented error messages - so the real client code in
speech/stt.py is exercised over a real socket. It cannot recognise speech:
each utterance is committed as the next text from the list it was given.

FakeTTS answers the streaming TTS endpoint through httpx's MockTransport.
MemoryOutput is an AudioOutput that records what the caller would hear.
"""
from __future__ import annotations

import asyncio
import base64
import json
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

import httpx

from speech.audio import audio_format, rms

API_KEY = "test-elevenlabs-key-0123456789"


class FakeScribe:
    def __init__(self, transcripts=(), api_key=API_KEY, partial_every=5,
                 script=None, idle_timeout: float | None = None,
                 late_partial: float | None = None):
        self.transcripts = list(transcripts)
        # ElevenLabs closes a session that receives no audio for ~15 s.
        self.idle_timeout = idle_timeout
        self.idle_closes = 0
        # ElevenLabs can send a partial about audio it has already committed:
        # seconds after the commit, or None for never.
        self.late_partial = late_partial
        self.api_key = api_key
        self.partial_every = partial_every
        # per-connection behaviour, e.g. {1: "rate_limited"} or {1: "drop_after_audio"}
        self.script = script or {}
        self.connections = 0
        self.open_connections = 0
        self.paths: list[str] = []
        self.header_keys: list[str | None] = []
        self.audio_bytes = 0
        self.commits_sent = 0
        self.server = None
        self.port = None

    async def __aenter__(self):
        from websockets.asyncio.server import serve
        self.server = await serve(self._handle, "127.0.0.1", 0,
                                  process_request=self._check_key)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _check_key(self, connection, request):
        self.header_keys.append(request.headers.get("xi-api-key"))
        if request.headers.get("xi-api-key") != self.api_key:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid api key\n")
        return None

    async def _handle(self, ws):
        self.connections += 1
        self.open_connections += 1
        number = self.connections
        behaviour = self.script.get(number)
        try:
            path = ws.request.path
            self.paths.append(path)
            query = parse_qs(urlparse(path).query)
            fmt = audio_format(query.get("audio_format", ["pcm_16000"])[0])
            vad_ms = float(query.get("vad_silence_threshold_secs", ["0.8"])[0]) * 1000
            if behaviour in ("rate_limited", "auth_error", "quota_exceeded"):
                await ws.send(json.dumps({"message_type": behaviour, "error": behaviour}))
                await ws.close()
                return
            await ws.send(json.dumps({"message_type": "session_started",
                                      "session_id": f"fake-{number}", "config": {}}))
            voiced, silence_ms, loud_chunks, heard = False, 0.0, 0, 0
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout)
                except asyncio.TimeoutError:
                    self.idle_closes += 1
                    await ws.close()
                    return
                except Exception:
                    return
                message = json.loads(raw)
                audio = base64.b64decode(message.get("audio_base_64") or "")
                self.audio_bytes += len(audio)
                if behaviour == "drop_after_audio" and self.audio_bytes > 0:
                    await ws.close()
                    return
                loud = rms(audio, fmt) > 0.02 if audio else False
                if loud:
                    voiced, silence_ms = True, 0.0
                    loud_chunks += 1
                    heard += 1
                    if loud_chunks % self.partial_every == 0:
                        upcoming = self.transcripts[0] if self.transcripts else ""
                        await ws.send(json.dumps({"message_type": "partial_transcript",
                                                  "text": upcoming[: 4 * heard]}))
                else:
                    silence_ms += fmt.duration_ms(len(audio))
                if voiced and (message.get("commit") or silence_ms >= vad_ms):
                    text = self.transcripts.pop(0) if self.transcripts else ""
                    await ws.send(json.dumps({"message_type": "committed_transcript",
                                              "text": text}))
                    self.commits_sent += 1
                    if self.late_partial is not None and text:
                        await asyncio.sleep(self.late_partial)
                        await ws.send(json.dumps({"message_type": "partial_transcript",
                                                  "text": text}))
                    voiced, silence_ms, loud_chunks, heard = False, 0.0, 0, 0
        finally:
            self.open_connections -= 1


class FakeTTS:
    """The streaming TTS endpoint: records requests, answers with audio."""

    def __init__(self, chunks=3, chunk_bytes=800, delay=0.0, responses=None):
        self.chunks = chunks
        self.chunk_bytes = chunk_bytes
        self.delay = delay
        # queue of per-request behaviours: "ok", 404, 429, 503, "timeout", "break"
        self.responses = list(responses or [])
        self.requests: list[httpx.Request] = []

    def texts(self) -> list[str]:
        return [json.loads(r.content)["text"] for r in self.requests]

    async def _stream(self, broken: bool):
        for index in range(self.chunks):
            if self.delay:
                await asyncio.sleep(self.delay)
            if broken and index == 1:
                raise httpx.ReadError("connection reset")
            yield b"\x7f" * self.chunk_bytes

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        behaviour = self.responses.pop(0) if self.responses else "ok"
        if behaviour == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if behaviour == 404:
            return httpx.Response(404, json={"detail": {"status": "voice_not_found"}})
        if isinstance(behaviour, int):
            return httpx.Response(behaviour, json={"detail": {"status": "error"}})
        return httpx.Response(200, content=self._stream(behaviour == "break"))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class MemoryOutput:
    """What the caller would hear, and what the line was told to do."""

    def __init__(self):
        self.audio = bytearray()
        self.chunks = 0
        self.stops = 0
        self.hung_up = False

    async def play(self, chunk: bytes) -> None:
        self.audio += chunk
        self.chunks += 1

    async def stop(self) -> None:
        self.stops += 1

    async def hang_up(self) -> None:
        self.hung_up = True


class ScribeThread:
    """FakeScribe on its own event loop in a background thread, for tests
    whose client (FastAPI's TestClient) runs on a different loop."""

    def __init__(self, transcripts=(), **options):
        import threading
        self.scribe = FakeScribe(transcripts, **options)
        self._ready = threading.Event()
        self._stop = None
        self.loop = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def main():
            self._stop = asyncio.Event()
            async with self.scribe:
                self._ready.set()
                await self._stop.wait()
        self.loop.run_until_complete(main())
        self.loop.close()

    def __enter__(self):
        self._thread.start()
        self._ready.wait(10)
        return self.scribe

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)


# --------------------------------------------------------------------------
# The sound card (voice_app.py): a stand-in for the `sounddevice` module
# --------------------------------------------------------------------------
class FakePortAudioError(Exception):
    pass


class _FakeStream:
    """Calls its callback from its own thread every block, like PortAudio."""

    def __init__(self, card, kind, samplerate, channels, blocksize, device, callback):
        self.card, self.kind = card, kind
        self.samplerate, self.channels, self.blocksize = samplerate, channels, blocksize
        self.device, self.callback = device, callback
        self.latency = 0.05
        self.active = False
        self.closed = False
        self._thread = None

    def start(self):
        import threading
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        import time
        nbytes = self.blocksize * self.channels * 2
        while self.active:
            if not self.card.frozen:
                if self.kind == "input":
                    self.callback(self.card.next_input(nbytes), self.blocksize, None, None)
                else:
                    buffer = bytearray(nbytes)
                    self.callback(buffer, self.blocksize, None, None)
                    self.card.played += buffer
            time.sleep(self.blocksize / self.samplerate)

    def stop(self):
        self.active = False

    def close(self):
        self.active = False
        self.closed = True


class FakeSoundDevice:
    """Devices, and streams whose callbacks run at real-time pace.

    `say(pcm)` queues what the microphone will hear (16-bit PCM at the rate
    the stream was opened with); otherwise it hears a faint room hiss, or
    exact zeros when `muted`. `frozen` stops every callback (unplugged)."""

    PortAudioError = FakePortAudioError

    def __init__(self, inputs=("Fake Microphone",), outputs=("Fake Speaker",),
                 default=(0, None), native_rate=48000, refuse=(), open_error=None):
        import threading
        from types import SimpleNamespace
        self.devices = []
        for name in inputs:
            self.devices.append({"name": name, "hostapi": 0, "max_input_channels": 2,
                                 "max_output_channels": 0, "default_samplerate": native_rate})
        for name in outputs:
            self.devices.append({"name": name, "hostapi": 0, "max_input_channels": 0,
                                 "max_output_channels": 2, "default_samplerate": native_rate})
        out_default = len(inputs) if default[1] is None and outputs else default[1]
        self.default = SimpleNamespace(device=[default[0], -1 if out_default is None else out_default],
                                       hostapi=0)
        self.refuse = set(refuse)            # (rate, channels) that cannot be opened
        self.open_error = open_error
        self.streams: list[_FakeStream] = []
        self.played = bytearray()
        self.frozen = False
        self.muted = False
        self._heard = bytearray()
        self._lock = threading.Lock()

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return list(self.devices)
        if device is None:
            index = self.default.device[0 if kind == "input" else 1]
            if index is None or index < 0:
                raise FakePortAudioError("no default device")
            return self.devices[index]
        return self.devices[device]

    def say(self, pcm: bytes) -> None:
        with self._lock:
            self._heard += pcm

    def next_input(self, nbytes: int) -> bytes:
        with self._lock:
            data = bytes(self._heard[:nbytes])
            del self._heard[:nbytes]
        if len(data) < nbytes:
            fill = b"\x00\x00" if self.muted else b"\x02\x00\xfe\xff"
            missing = nbytes - len(data)
            data += (fill * (missing // len(fill) + 1))[:missing]
        return data

    def _stream(self, kind, samplerate, channels, dtype, blocksize, device, callback):
        if self.open_error:
            raise FakePortAudioError(self.open_error)
        if (samplerate, channels) in self.refuse:
            raise FakePortAudioError("Invalid sample rate [PaErrorCode -9997]")
        stream = _FakeStream(self, kind, samplerate, channels, blocksize, device, callback)
        self.streams.append(stream)
        return stream

    def RawInputStream(self, samplerate, channels, dtype, blocksize, device, callback,
                       **options):
        return self._stream("input", samplerate, channels, dtype, blocksize, device, callback)

    def RawOutputStream(self, samplerate, channels, dtype, blocksize, device, callback,
                        **options):
        return self._stream("output", samplerate, channels, dtype, blocksize, device, callback)


class FakeOllama:
    """The Ollama service: `reword(reference)` decides what the model 'says'
    for a reply it is asked to reword; judge prompts get no answer."""

    model = "fake-llm"

    def __init__(self, reword=None, available=True, crash=False):
        self.reword = reword or (lambda reference: f"Ji bilkul. {reference}")
        self.available = available
        self.crash = crash
        self.prompts: list[str] = []

    def is_available(self, force=False):
        return self.available

    def chat_json(self, system, user, examples=None):
        self.prompts.append(user)
        if self.crash:
            raise RuntimeError("the model server fell over")
        if user.startswith('Reply to say: "'):
            reference = user[len('Reply to say: "'):user.index('"\n')]
            return {"response": self.reword(reference)}
        return None
