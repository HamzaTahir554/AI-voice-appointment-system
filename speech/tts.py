"""
Text-to-speech: ElevenLabs, streamed.

It says the text it is given - the reply the Dialog Manager and the judge
have already approved - and nothing else: no rewording, no added facts. It
never sees an unvalidated reply, because it is called only with the pipeline's
final `response`.

Streaming: the audio is played as it arrives (`stream()`), so the caller
hears the first word while the rest is still being generated. That first
chunk is what the call's latency is measured to.

Failures: a temporary one (timeout, rate limit, 5xx) is retried a bounded
number of times - but only BEFORE any audio has gone to the caller; a retry
after that would repeat words they have already heard. An invalid voice, key
or model is never retried. When speech cannot be produced the call keeps the
text (the fallback in docs/VOICE.md) instead of crashing.

Caching: only phrases the caller of this module marks as fixed (the greeting,
"please say that again", the goodbye) are kept, keyed on the exact text and
every voice setting, so nothing dynamic - a doctor, a date, a patient - can
ever be served from an old recording.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import AsyncIterator, Callable

import httpx

import config
from speech import results as R
from speech.stt import _redact, http_error_code

logger = logging.getLogger("speech.tts")

CACHE_ENTRIES = 64
CACHE_MAX_BYTES = 400_000            # per phrase; a long reply is never cached
REPLAY_CHUNK = 3200                  # cached audio is handed out in pieces like a stream


class TextToSpeechService(ABC):
    """What the rest of the system uses; ElevenLabs is one implementation."""

    output_format: str

    @abstractmethod
    def stream(self, text: str, language: str | None = None,
               cacheable: bool = False) -> AsyncIterator[bytes]:
        ...

    async def synthesize(self, text: str, language: str | None = None,
                         cacheable: bool = False) -> R.TTSResult:
        """The whole clip at once (tests, files)."""
        started = time.perf_counter()
        chunks: list[bytes] = []
        first = None
        try:
            async for chunk in self.stream(text, language, cacheable):
                if first is None:
                    first = (time.perf_counter() - started) * 1000
                chunks.append(chunk)
        except R.SpeechError as error:
            return R.TTSResult(False, b"".join(chunks), self.output_format, len(text or ""),
                               first, (time.perf_counter() - started) * 1000,
                               error=error.code, attempts=getattr(self, "last_attempts", 0))
        audio = b"".join(chunks)
        return R.TTSResult(bool(audio), audio, self.output_format, len(text),
                           first, (time.perf_counter() - started) * 1000,
                           from_cache=getattr(self, "last_from_cache", False),
                           error=None if audio else R.UNAVAILABLE,
                           attempts=getattr(self, "last_attempts", 0))


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return " ".join(str(detail.get(k) or "") for k in ("code", "status", "message")).strip()
    return str(detail or "")


def _error_for(response: httpx.Response, detail: str) -> str:
    lowered = detail.lower()
    if "paid_plan" in lowered or "payment_required" in lowered:
        return R.PLAN_REQUIRED
    if "voice" in lowered and ("not_found" in lowered or "not found" in lowered):
        return R.INVALID_VOICE
    if "quota" in lowered or "credits" in lowered:
        return R.QUOTA_EXCEEDED
    if "concurrent" in lowered:
        return R.RATE_LIMITED
    return http_error_code(response.status_code)


class ElevenLabsTTS(TextToSpeechService):
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, voice_id: str | None = None,
                 output_format: str | None = None, language: str | None = None,
                 timeout: float | None = None, max_retries: int | None = None,
                 enable_logging: bool | None = None,
                 voice_settings: dict | None = None,
                 http_client: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.perf_counter):
        self.api_key = config.ELEVENLABS_API_KEY if api_key is None else api_key
        self.base_url = (base_url or config.ELEVENLABS_API_URL).rstrip("/")
        self.model = model or config.ELEVENLABS_TTS_MODEL
        self.voice_id = config.ELEVENLABS_VOICE_ID if voice_id is None else voice_id
        self.output_format = output_format or config.ELEVENLABS_TTS_OUTPUT_FORMAT
        self.language = config.ELEVENLABS_TTS_LANGUAGE if language is None else language
        self.timeout = config.ELEVENLABS_TIMEOUT if timeout is None else timeout
        self.max_retries = config.ELEVENLABS_MAX_RETRIES if max_retries is None else max_retries
        self.enable_logging = (config.ELEVENLABS_ENABLE_LOGGING if enable_logging is None
                               else enable_logging)
        self.voice_settings = voice_settings
        self._http = http_client
        self._clock = clock
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        # the last request, for the caller's latency record
        self.last_first_byte_ms: float | None = None
        self.last_total_ms: float | None = None
        self.last_attempts = 0
        self.last_from_cache = False
        # usage, for cost logging (never includes the key)
        self.characters_sent = 0
        self.requests = 0
        self.cache_hits = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.voice_id)

    def _key(self, text: str, language: str | None) -> str:
        parts = [text, self.voice_id, self.model, self.output_format,
                 language or self.language or "", repr(sorted((self.voice_settings or {}).items()))]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def _body(self, text: str, language: str | None) -> dict:
        body: dict = {"text": text, "model_id": self.model}
        code = language or self.language
        if code:
            body["language_code"] = code
        if self.voice_settings:
            body["voice_settings"] = self.voice_settings
        return body

    async def stream(self, text: str, language: str | None = None,
                     cacheable: bool = False) -> AsyncIterator[bytes]:
        text = (text or "").strip()
        self.last_first_byte_ms = self.last_total_ms = None
        self.last_attempts = 0
        self.last_from_cache = False
        if not text:
            raise R.SpeechError(R.EMPTY_TEXT)
        if not self.api_key:
            raise R.SpeechError(R.NOT_CONFIGURED, "ELEVENLABS_API_KEY is not set")
        if not self.voice_id:
            raise R.SpeechError(R.INVALID_VOICE, "ELEVENLABS_VOICE_ID is not set")

        key = self._key(text, language) if cacheable else None
        if key and key in self._cache:
            self._cache.move_to_end(key)
            self.cache_hits += 1
            self.last_from_cache = True
            self.last_first_byte_ms = self.last_total_ms = 0.0
            audio = self._cache[key]
            for start in range(0, len(audio), REPLAY_CHUNK):
                yield audio[start:start + REPLAY_CHUNK]
            return

        params = {"output_format": self.output_format}
        if not self.enable_logging:
            params["enable_logging"] = "false"
        url = f"{self.base_url}/v1/text-to-speech/{self.voice_id}/stream"
        client = self._http or httpx.AsyncClient(timeout=self.timeout)
        started = self._clock()
        kept: list[bytes] = []
        try:
            while True:
                self.last_attempts += 1
                self.requests += 1
                self.characters_sent += len(text)
                code = None
                try:
                    async with client.stream("POST", url, params=params,
                                             headers={"xi-api-key": self.api_key},
                                             json=self._body(text, language),
                                             timeout=self.timeout) as response:
                        if response.status_code != 200:
                            await response.aread()
                            detail = _detail(response)
                            code = _error_for(response, detail)
                            logger.warning("TTS refused (%s %s): %s", response.status_code,
                                           code, _redact(detail))
                        else:
                            async for chunk in response.aiter_bytes():
                                if not chunk:
                                    continue
                                if self.last_first_byte_ms is None:
                                    self.last_first_byte_ms = (self._clock() - started) * 1000
                                if key is not None:
                                    kept.append(chunk)
                                yield chunk
                except httpx.TimeoutException:
                    code = R.TIMEOUT
                except httpx.HTTPError as exc:
                    code = R.DISCONNECTED if self.last_first_byte_ms else R.UNAVAILABLE
                    logger.warning("TTS connection problem: %s", type(exc).__name__)

                if code is None:
                    self.last_total_ms = (self._clock() - started) * 1000
                    if self.last_first_byte_ms is None:
                        raise R.SpeechError(R.UNAVAILABLE, "empty audio")
                    if key is not None:
                        audio = b"".join(kept)
                        if len(audio) <= CACHE_MAX_BYTES:
                            self._cache[key] = audio
                            while len(self._cache) > CACHE_ENTRIES:
                                self._cache.popitem(last=False)
                    return
                # Audio already reached the caller: never start the sentence again.
                if self.last_first_byte_ms is not None:
                    raise R.SpeechError(R.DISCONNECTED, "stream broke after audio started")
                if not R.retryable(code) or self.last_attempts > self.max_retries:
                    raise R.SpeechError(code)
                await asyncio.sleep(0.4 * self.last_attempts)
        finally:
            if self._http is None:
                await client.aclose()
