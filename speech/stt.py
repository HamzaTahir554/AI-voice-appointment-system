"""
Speech-to-text: ElevenLabs Scribe.

Two ways in, one provider, and nothing outside this file talks to ElevenLabs'
transcription API:

    RealtimeTranscriber   a live call. Audio streams over a WebSocket to
                          Scribe v2 Realtime; it answers with PARTIAL
                          transcripts while the caller speaks and one
                          COMMITTED transcript when its voice-activity
                          detection decides the turn is over. Only committed
                          text ever reaches the dialogue.
    transcribe()          a whole recording (batch Scribe), for files and tests.

The API is called directly - the official WebSocket and REST endpoints - with
`websockets` and `httpx`. (The official Python SDK cannot be installed on a
Windows machine without long-path support; the endpoints are the same.)

Failures come back as `STTResult(success=False, error=<code>)` or an `error`
event, never as an exception into the call. Temporary ones (timeout, rate
limit, dropped connection, 5xx) are retried a bounded number of times; an
invalid key or an exhausted quota never is.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx

import config
from speech import results as R
from speech.audio import AudioFormat, audio_format, wav_bytes

logger = logging.getLogger("speech.stt")
# The websockets library logs every request header at DEBUG - including
# xi-api-key. Never below INFO, whatever the application's log level.
logging.getLogger("websockets").setLevel(logging.INFO)

# Realtime server messages that end in an error, and what they mean for us.
_SERVER_ERRORS = {
    "auth_error": R.AUTH_FAILED,
    "unaccepted_terms": R.AUTH_FAILED,
    "quota_exceeded": R.QUOTA_EXCEEDED,
    "rate_limited": R.RATE_LIMITED,
    "queue_overflow": R.RATE_LIMITED,
    "resource_exhausted": R.RATE_LIMITED,
    "commit_throttled": R.RATE_LIMITED,
    "session_time_limit_exceeded": R.DISCONNECTED,
    "input_error": R.INVALID_AUDIO,
    "invalid_request": R.INVALID_REQUEST,
    "chunk_size_exceeded": R.INVALID_AUDIO,
    "insufficient_audio_activity": R.NO_SPEECH,
    "transcriber_error": R.UNAVAILABLE,
    "error": R.UNAVAILABLE,
}
_COMMITTED = ("committed_transcript", "committed_transcript_with_timestamps")

MIN_AUDIO_MS = 100          # ElevenLabs' own minimum for a recording


def http_error_code(status: int) -> str:
    if status in (401, 403):
        return R.AUTH_FAILED
    if status == 402:
        return R.QUOTA_EXCEEDED
    if status == 429:
        return R.RATE_LIMITED
    if status in (400, 404, 422):
        return R.INVALID_REQUEST
    if status >= 500:
        return R.UNAVAILABLE
    return R.UNAVAILABLE


def _redact(text: str) -> str:
    """Never let a key-shaped value reach a log line."""
    key = config.ELEVENLABS_API_KEY
    return text.replace(key, "[redacted]") if key else text


@dataclass
class STTEvent:
    kind: str                       # partial | final | error | closed
    text: str = ""
    language: str | None = None
    error: str | None = None
    at: float = 0.0                 # monotonic time it arrived

    def result(self) -> R.STTResult:
        if self.kind == "error":
            return R.STTResult(False, error=self.error)
        return R.STTResult(True, self.text.strip(), self.language,
                           is_final=self.kind == "final")


# --------------------------------------------------------------------------
# One live session
# --------------------------------------------------------------------------
class RealtimeTranscriber:
    """One realtime transcription session - one caller, one call."""

    def __init__(self, url: str, headers: dict[str, str], fmt: AudioFormat,
                 connect: Callable[..., Awaitable[Any]], open_timeout: float,
                 clock: Callable[[], float] = time.monotonic):
        self._url = url
        self._headers = headers
        self.fmt = fmt
        self._connect = connect
        self._open_timeout = open_timeout
        self._clock = clock
        self._ws = None
        self._reader: asyncio.Task | None = None
        self.events: asyncio.Queue[STTEvent] = asyncio.Queue()
        self.session_id: str | None = None
        self.closed = False
        self._closing = False
        self._last_final: tuple[str, str, float] | None = None
        # accounting
        self.connect_ms: float | None = None
        self.bytes_sent = 0
        self.chunks_sent = 0

    @property
    def audio_ms_sent(self) -> float:
        return self.fmt.duration_ms(self.bytes_sent)

    async def open(self) -> None:
        started = self._clock()
        try:
            self._ws = await asyncio.wait_for(
                self._connect(self._url, additional_headers=self._headers,
                              open_timeout=self._open_timeout, max_size=2 ** 22,
                              ping_interval=20),
                timeout=self._open_timeout)
        except asyncio.TimeoutError as exc:
            raise R.SpeechError(R.TIMEOUT, "realtime connection timed out") from exc
        except R.SpeechError:
            raise
        except Exception as exc:                       # handshake refused, DNS, TLS
            status = getattr(getattr(exc, "response", None), "status_code", None)
            code = http_error_code(status) if status else R.UNAVAILABLE
            raise R.SpeechError(code, _redact(f"{type(exc).__name__}: {exc}")) from exc
        try:
            first = json.loads(await asyncio.wait_for(self._ws.recv(),
                                                      timeout=self._open_timeout))
        except asyncio.TimeoutError as exc:
            await self._abort()
            raise R.SpeechError(R.TIMEOUT, "no session_started") from exc
        except Exception as exc:
            await self._abort()
            raise R.SpeechError(R.DISCONNECTED, type(exc).__name__) from exc
        kind = first.get("message_type")
        if kind != "session_started":
            await self._abort()
            raise R.SpeechError(_SERVER_ERRORS.get(kind, R.UNAVAILABLE),
                                _redact(str(first.get("error") or kind)))
        self.session_id = first.get("session_id")
        self.connect_ms = (self._clock() - started) * 1000
        self._reader = asyncio.create_task(self._read(), name="stt-reader")

    async def send(self, chunk: bytes, commit: bool = False) -> None:
        if self.closed or self._ws is None:
            raise R.SpeechError(R.DISCONNECTED, "session is closed")
        message = {"message_type": "input_audio_chunk",
                   "audio_base_64": base64.b64encode(chunk).decode("ascii"),
                   "commit": commit, "sample_rate": self.fmt.sample_rate}
        try:
            await self._ws.send(json.dumps(message))
        except Exception as exc:
            raise R.SpeechError(R.DISCONNECTED, type(exc).__name__) from exc
        self.bytes_sent += len(chunk)
        self.chunks_sent += 1

    async def commit(self) -> None:
        """End the turn now instead of waiting for the silence detector."""
        silence = b"\xff" if self.fmt.encoding == "ulaw" else b"\x00\x00"
        await self.send(silence * (self.fmt.sample_rate // 50), commit=True)

    async def next_event(self, timeout: float | None = None) -> STTEvent:
        return await asyncio.wait_for(self.events.get(), timeout)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                self._handle(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # the socket dropped
            if not self._closing:
                logger.warning("realtime STT connection lost: %s", type(exc).__name__)
                self.events.put_nowait(STTEvent("error", error=R.DISCONNECTED,
                                                at=self._clock()))
        else:
            if not self._closing:
                self.events.put_nowait(STTEvent("error", error=R.DISCONNECTED,
                                                at=self._clock()))
        finally:
            self.closed = True
            self.events.put_nowait(STTEvent("closed", at=self._clock()))

    def _handle(self, data: dict) -> None:
        kind = data.get("message_type")
        now = self._clock()
        if kind == "partial_transcript":
            self.events.put_nowait(STTEvent("partial", data.get("text") or "", at=now))
        elif kind in _COMMITTED:
            text = (data.get("text") or "").strip()
            language = data.get("language_code") or None
            # With timestamps switched on, one commit can arrive as both
            # message types; the second copy is not a new turn. Two commits
            # of the same type are two turns, even with the same text.
            last = self._last_final
            if last and last[0] == text and last[1] != kind and now - last[2] < 1.5:
                return
            self._last_final = (text, kind, now)
            self.events.put_nowait(STTEvent("final", text, language, at=now))
        elif kind in _SERVER_ERRORS:
            code = _SERVER_ERRORS[kind]
            logger.warning("realtime STT error %s (%s)", kind, _redact(str(data.get("error"))))
            self.events.put_nowait(STTEvent("error", error=code, at=now))

    async def _abort(self) -> None:
        self._closing = True
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:                              # pragma: no cover
            pass
        self.closed = True

    async def close(self) -> None:
        """Idempotent; safe from any state."""
        if self._closing and self.closed:
            return
        self._closing = True
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:                              # pragma: no cover
            pass
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader.cancel()
            except Exception:                          # pragma: no cover
                pass
        self.closed = True


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------
class SpeechToTextService(ABC):
    """What the rest of the system uses; ElevenLabs is one implementation."""

    @abstractmethod
    async def open_realtime(self, audio_format_name: str | None = None,
                            keyterms: list[str] | None = None) -> RealtimeTranscriber:
        ...

    @abstractmethod
    async def transcribe(self, audio: bytes, audio_format_name: str,
                         keyterms: list[str] | None = None) -> R.STTResult:
        ...


def _default_connect():
    from websockets.asyncio.client import connect
    return connect


class ElevenLabsSTT(SpeechToTextService):
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, batch_model: str | None = None,
                 language: str | None = None, secondary_languages: list[str] | None = None,
                 silence_secs: float | None = None, audio_format_name: str | None = None,
                 timeout: float | None = None, max_retries: int | None = None,
                 enable_logging: bool | None = None,
                 connect: Callable[..., Awaitable[Any]] | None = None,
                 http_client: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 commit_strategy: str = "vad"):
        self.api_key = config.ELEVENLABS_API_KEY if api_key is None else api_key
        self.base_url = (base_url or config.ELEVENLABS_API_URL).rstrip("/")
        self.model = model or config.ELEVENLABS_STT_MODEL
        self.batch_model = batch_model or config.ELEVENLABS_STT_BATCH_MODEL
        self.language = config.ELEVENLABS_STT_LANGUAGE if language is None else language
        self.secondary_languages = (config.ELEVENLABS_STT_SECONDARY_LANGUAGES
                                    if secondary_languages is None else secondary_languages)
        self.silence_secs = (config.ELEVENLABS_STT_SILENCE_SECS if silence_secs is None
                             else silence_secs)
        self.audio_format_name = audio_format_name or config.ELEVENLABS_STT_AUDIO_FORMAT
        self.timeout = config.ELEVENLABS_TIMEOUT if timeout is None else timeout
        self.max_retries = config.ELEVENLABS_MAX_RETRIES if max_retries is None else max_retries
        self.enable_logging = (config.ELEVENLABS_ENABLE_LOGGING if enable_logging is None
                               else enable_logging)
        # "vad": ElevenLabs ends the sentence after a pause; "manual": only
        # when told to (push-to-talk, where the key press marks the end).
        if commit_strategy not in ("vad", "manual"):
            raise ValueError(f"commit_strategy must be vad or manual, not {commit_strategy!r}")
        self.commit_strategy = commit_strategy
        self._connect = connect
        self._http = http_client
        self._clock = clock
        # usage, for cost logging (never includes the key)
        self.sessions_opened = 0
        self.batch_requests = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.api_key}

    def realtime_url(self, fmt: AudioFormat, keyterms: list[str] | None = None) -> str:
        params: list[tuple[str, str]] = [
            ("model_id", self.model),
            ("audio_format", fmt.name),
            ("commit_strategy", self.commit_strategy),
            ("include_language_detection", "true"),
        ]
        if self.commit_strategy == "vad":
            params.append(("vad_silence_threshold_secs", f"{self.silence_secs:g}"))
        if self.language:
            params.append(("language_code", self.language))
        for code in self.secondary_languages:
            params.append(("secondary_languages", code))
        for term in keyterms or []:
            params.append(("keyterms", term))
        if not self.enable_logging:
            params.append(("enable_logging", "false"))
        ws_base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/v1/speech-to-text/realtime?{urlencode(params)}"

    async def open_realtime(self, audio_format_name: str | None = None,
                            keyterms: list[str] | None = None) -> RealtimeTranscriber:
        """A connected session, or SpeechError after bounded retries."""
        if not self.configured:
            raise R.SpeechError(R.NOT_CONFIGURED, "ELEVENLABS_API_KEY is not set")
        fmt = audio_format(audio_format_name or self.audio_format_name)
        connect = self._connect or _default_connect()
        attempt = 0
        while True:
            attempt += 1
            session = RealtimeTranscriber(self.realtime_url(fmt, keyterms), self._headers(),
                                          fmt, connect, self.timeout, self._clock)
            try:
                await session.open()
                self.sessions_opened += 1
                logger.info("realtime STT session open (%s, %s) in %.0f ms",
                            self.model, fmt.name, session.connect_ms or 0)
                return session
            except R.SpeechError as error:
                if not R.retryable(error.code) or attempt > self.max_retries:
                    logger.error("realtime STT could not open: %s", error.code)
                    raise
                logger.warning("realtime STT open failed (%s); retrying", error.code)
                await asyncio.sleep(0.4 * attempt)

    async def transcribe(self, audio: bytes, audio_format_name: str,
                         keyterms: list[str] | None = None) -> R.STTResult:
        """A whole recording through batch Scribe."""
        if not self.configured:
            return R.STTResult(False, error=R.NOT_CONFIGURED)
        if not audio:
            return R.STTResult(False, error=R.EMPTY_AUDIO)
        try:
            fmt = audio_format(audio_format_name)
        except ValueError:
            return R.STTResult(False, error=R.INVALID_AUDIO)
        if fmt.duration_ms(len(audio)) < MIN_AUDIO_MS:
            return R.STTResult(False, error=R.TOO_SHORT)   # never sent: nothing to hear

        data: dict[str, str | list[str]] = {"model_id": self.batch_model}
        if self.language:
            data["language_code"] = self.language
        if keyterms:
            data["keyterms"] = list(keyterms)
        if not self.enable_logging:
            data["enable_logging"] = "false"
        files = {"file": ("utterance.wav", wav_bytes(audio, fmt), "audio/wav")}
        client = self._http or httpx.AsyncClient(timeout=self.timeout)
        attempt = 0
        try:
            while True:
                attempt += 1
                self.batch_requests += 1
                try:
                    response = await client.post(f"{self.base_url}/v1/speech-to-text",
                                                 headers=self._headers(), data=data,
                                                 files=files, timeout=self.timeout)
                    code = None if response.status_code == 200 else \
                        http_error_code(response.status_code)
                except httpx.TimeoutException:
                    code, response = R.TIMEOUT, None
                except httpx.HTTPError:
                    code, response = R.UNAVAILABLE, None
                if code is None:
                    body = response.json()
                    text = (body.get("text") or "").strip()
                    if not text:
                        return R.STTResult(False, error=R.NO_SPEECH, is_final=True)
                    return R.STTResult(True, text, body.get("language_code") or None,
                                       is_final=True)
                if not R.retryable(code) or attempt > self.max_retries:
                    logger.error("batch STT failed: %s", code)
                    return R.STTResult(False, error=code)
                await asyncio.sleep(0.4 * attempt)
        finally:
            if self._http is None:
                await client.aclose()
