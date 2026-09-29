"""
One phone call: the caller's audio in, the agent's audio out, and the
existing text pipeline - unchanged - in between.

    caller audio -> SpeechGate -> ElevenLabs realtime STT
                 -> COMMITTED transcript
                 -> VoicePipeline.process (mBERT -> Dialog -> Backend ->
                    Firestore -> judge/validator)        [voice_pipeline.py]
                 -> approved reply text
                 -> ElevenLabs streaming TTS -> AudioOutput -> caller

What this module decides, and what it leaves alone
    It decides WHEN a turn happens: only a committed transcript is a turn,
    partial ones are shown and used to notice the caller talking over the
    agent, never sent to the dialogue. Turns run one at a time, in order, in
    one dialogue session for the whole call - the Dialog Manager keeps its
    state, slots and confirmations exactly as it does for text.
    It never decides WHAT to say: the reply is the pipeline's `response`,
    already checked against the backend result by the validator, and it is
    spoken as is.

Telephony
    Audio leaves through `AudioOutput`, three methods any telephony bridge can
    implement: play a chunk, stop (the caller interrupted - drop what has not
    been played) and hang up. There is no Asterisk integration in this
    project yet; api/voice_ws.py implements AudioOutput over a WebSocket, and
    docs/VOICE.md describes what an Asterisk bridge has to do.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

import config
from config import Action
from speech import results as R
from speech.audio import SpeechGate, audio_format
from speech.stt import RealtimeTranscriber, SpeechToTextService
from speech.tts import TextToSpeechService

logger = logging.getLogger("speech.call")

# Replies that are the same words for every caller - safe to keep as audio.
_FIXED_REPLY_ACTIONS = {Action.GREET, Action.END_CONVERSATION}
# ElevenLabs' language code for the pipeline's reply language, used when
# ELEVENLABS_TTS_LANGUAGE is "auto". Roman Urdu has no code of its own.
_TTS_LANGUAGE = {"english": "en", "urdu": "ur"}
MAX_RECONNECTS = 2                 # per call
UTTERANCE_BUFFER_MS = 30_000       # kept to resend once after a dropped connection
# ElevenLabs closes a realtime session that receives no audio for ~15 s
# (measured: a clean close at 15.3 s). The gate holds silence back between
# turns, so a short piece of silence is sent whenever this long passes
# with nothing sent - about 2 % extra audio, instead of all of it.
KEEPALIVE_SECS = 5.0
KEEPALIVE_MS = 100
# Half duplex (a microphone next to a loudspeaker): the room echoes the
# agent's last words for a moment after playback ends, so listening resumes
# this much later.
ECHO_TAIL_MS = 300


class AudioOutput(Protocol):
    """Where the agent's voice goes. A telephony bridge implements this."""

    async def play(self, chunk: bytes) -> None: ...

    async def stop(self) -> None:
        """The caller is talking over the agent: drop audio not yet played."""

    async def hang_up(self) -> None:
        """The conversation is over (a no-op where the line cannot be closed)."""


def tts_language(language: str | None) -> str | None:
    setting = config.ELEVENLABS_TTS_LANGUAGE
    if setting.lower() == "auto":
        return _TTS_LANGUAGE.get(language or "")
    return setting or None


class VoiceCall:
    def __init__(self, pipeline, stt: SpeechToTextService, tts: TextToSpeechService,
                 output: AudioOutput, session_id: str, patient_id: str | None = None,
                 audio_format_name: str | None = None, keyterms: list[str] | None = None,
                 greeting: str | None = None,
                 on_event: Callable[[dict], Any] | None = None,
                 max_silence_ms: int = 3000, barge_in_min_chars: int = 3,
                 chunk_ms: int | None = None, keepalive_secs: float = KEEPALIVE_SECS,
                 listen_while_speaking: bool = True, echo_tail_ms: int = ECHO_TAIL_MS,
                 clock: Callable[[], float] = time.monotonic):
        self.pipeline = pipeline
        self.stt = stt
        self.tts = tts
        self.output = output
        self.session_id = session_id
        self.patient_id = patient_id
        self.fmt = audio_format(audio_format_name or config.ELEVENLABS_STT_AUDIO_FORMAT)
        self.keyterms = keyterms
        self.greeting = greeting
        self.on_event = on_event
        self.max_silence_ms = max_silence_ms
        self.barge_in_min_chars = barge_in_min_chars
        self.chunk_bytes = self.fmt.bytes_for(chunk_ms or config.ELEVENLABS_STT_CHUNK_MS)
        self.keepalive_secs = keepalive_secs
        # False: the caller's audio is replaced by silence while the agent can
        # be heard, so a microphone never transcribes the agent's own voice.
        # A phone line keeps listening (the caller may interrupt).
        self.listen_while_speaking = listen_while_speaking
        self.echo_tail_ms = echo_tail_ms
        self._deaf_until = 0.0
        self.clock = clock
        self._last_sent_at = clock()
        self._keepalive: asyncio.Task | None = None
        self._resent_to = None             # the session the current sentence was re-sent to

        self.gate = SpeechGate(self.fmt, clock=clock)
        self.session: RealtimeTranscriber | None = None
        self._outgoing = b""
        self._utterance_audio = bytearray()
        self._commit_sent = False
        self._turns: asyncio.Queue = asyncio.Queue()
        self._listener: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self._speech: asyncio.Task | None = None
        self._playing_until = 0.0          # when the phone finishes playing what it was sent
        try:
            self._out_fmt = audio_format(getattr(tts, "output_format", ""))
        except ValueError:
            self._out_fmt = None           # mp3 etc.: duration unknown from bytes
        self._barged_in = False
        self._empty_finals = 0
        self._reconnects = 0
        self._recover_lock = asyncio.Lock()
        self._current: R.TurnLatency = R.TurnLatency()
        self.language: str | None = None
        self.ended = False
        self.end_reason: str | None = None
        self.turns: list[dict] = []           # what happened, turn by turn
        self.last_tts_error: str | None = None

    # ------------------------------------------------------------------ events
    async def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            outcome = self.on_event(event)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:                          # pragma: no cover
            logger.warning("event handler failed: %s", exc)

    @property
    def speaking(self) -> bool:
        """Still being generated, or already sent but still playing: ElevenLabs
        streams faster than real time, so the audio of a four-second reply can
        arrive in one second and the caller is still hearing it after that."""
        generating = self._speech is not None and not self._speech.done()
        return generating or self.clock() < self._playing_until

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> bool:
        """Open transcription and greet the caller. False when the call
        cannot go ahead (it has then apologised and hung up)."""
        try:
            self.session = await self.stt.open_realtime(self.fmt.name, self.keyterms)
        except R.SpeechError as error:
            logger.error("[%s] speech recognition unavailable: %s", self.session_id, error.code)
            await self._emit({"type": "error", "code": error.code})
            await self._say(R.technical_problem(self.language), cacheable=True)
            await self.end("stt_unavailable")
            return False
        await self._emit({"type": "stt_open", "connect_ms": self.session.connect_ms})
        logger.info("[%s] call started, STT connected in %.0f ms", self.session_id,
                    self.session.connect_ms or 0)
        self._last_sent_at = self.clock()
        self._listener = asyncio.create_task(self._listen(), name=f"stt-{self.session_id}")
        self._worker = asyncio.create_task(self._run_turns(), name=f"turns-{self.session_id}")
        self._keepalive = asyncio.create_task(self._keep_alive(), name=f"stt-keepalive-{self.session_id}")
        if self.greeting:
            # In the background: the caller may start talking straight away.
            self._speech = asyncio.create_task(self._play(self.greeting, True, None),
                                               name=f"tts-{self.session_id}")
        return True

    @property
    def agent_audible(self) -> bool:
        """The agent is talking, or the output device is still playing it."""
        return self.speaking or bool(getattr(self.output, "busy", False))

    def _silence(self, nbytes: int) -> bytes:
        return (b"\xff" if self.fmt.encoding == "ulaw" else b"\x00") * nbytes

    async def feed(self, chunk: bytes) -> None:
        """The caller's audio, in any chunk size, as it arrives."""
        if self.ended or not chunk:
            return
        if not self.listen_while_speaking:
            if self.agent_audible:
                self._deaf_until = self.clock() + self.echo_tail_ms / 1000
            if self.clock() < self._deaf_until:
                # Silence, not nothing: the clock keeps running, so a sentence
                # the caller was in the middle of still ends normally.
                chunk = self._silence(len(chunk))
        for frame in self.gate.push(chunk):
            self._outgoing += frame
            if len(self._utterance_audio) < self.fmt.bytes_for(UTTERANCE_BUFFER_MS):
                self._utterance_audio += frame
        if len(self._outgoing) >= self.chunk_bytes:
            await self._flush()
        # ElevenLabs' own silence detector ends the turn; this is only a
        # backstop for a caller who goes quiet without it noticing.
        if (self.gate.in_utterance and not self._commit_sent
                and self.gate.silence_ms() > self.max_silence_ms and self.session):
            self._commit_sent = True
            await self._flush()
            session = self.session
            try:
                await session.commit()
            except R.SpeechError as error:
                await self._recover(error.code, session)

    def begin_utterance(self) -> None:
        """Push-to-talk: the caller pressed the key and is about to speak."""
        if not self.ended:
            self.gate.start_utterance()

    async def end_utterance(self, ended_at: float | None = None) -> bool:
        """Push-to-talk: the caller says they have finished. Commits what was
        sent; False when nothing that sounded like speech was heard."""
        if self.ended or self.session is None or not self.gate.in_utterance:
            return False
        # The key press, not the last loud frame, is when the caller stopped.
        self.gate.last_voice_at = ended_at if ended_at is not None else self.clock()
        self._commit_sent = True
        await self._flush()
        session = self.session
        try:
            await session.commit()
        except R.SpeechError as error:
            await self._recover(error.code, session)
        return True

    async def _flush(self) -> None:
        if not self._outgoing or self.session is None:
            return
        data, self._outgoing = self._outgoing, b""
        session = self.session
        try:
            await session.send(data)
            self._last_sent_at = self.clock()
        except R.SpeechError as error:
            await self._recover(error.code, session)

    async def _keep_alive(self) -> None:
        silence = (b"\xff" if self.fmt.encoding == "ulaw" else b"\x00\x00") \
            * (self.fmt.bytes_for(KEEPALIVE_MS) // self.fmt.sample_width)
        while not self.ended:
            await asyncio.sleep(min(1.0, self.keepalive_secs / 2))
            if self.ended or self.session is None or self.gate.in_utterance:
                continue
            if self.clock() - self._last_sent_at < self.keepalive_secs:
                continue
            session = self.session
            try:
                await session.send(silence)
                self._last_sent_at = self.clock()
            except R.SpeechError as error:
                await self._recover(error.code, session)

    async def end(self, reason: str = "caller_hung_up") -> None:
        """Close everything this call opened. Safe to call more than once."""
        if self.ended:
            return
        self.ended = True
        self.end_reason = reason
        current = asyncio.current_task()
        for task in (self._listener, self._worker, self._keepalive):
            if task is not None and task is not current and not task.done():
                task.cancel()
        if self._speech is not None and self._speech is not current and not self._speech.done():
            self._speech.cancel()
        if self.session is not None:
            await self.session.close()
        try:
            await self.output.hang_up()
        except Exception as exc:                          # pragma: no cover
            logger.warning("hang up failed: %s", exc)
        try:
            self.pipeline.end(self.session_id)
        except Exception:                                 # pragma: no cover
            pass
        usage = self.usage()
        logger.info("[%s] call ended (%s): %s", self.session_id, reason, usage)
        await self._emit({"type": "call_ended", "reason": reason, "usage": usage})

    def usage(self) -> dict:
        """What this call cost, in the units ElevenLabs bills."""
        return {
            "turns": len(self.turns),
            "stt_audio_ms_sent": round(self.session.audio_ms_sent if self.session else 0),
            "stt_audio_ms_held_back": round(self.fmt.duration_ms(self.gate.held_back_bytes)),
            "tts_characters": getattr(self.tts, "characters_sent", None),
            "tts_requests": getattr(self.tts, "requests", None),
            "tts_cache_hits": getattr(self.tts, "cache_hits", None),
            "stt_reconnects": self._reconnects,
        }

    # ---------------------------------------------------------- transcription
    async def _listen(self) -> None:
        while not self.ended:
            session = self.session
            event = await session.next_event()
            if session is not self.session:
                continue                                   # from a replaced session
            if event.kind == "partial":
                if self._current.first_partial_at is None:
                    self._current.first_partial_at = event.at
                await self._emit({"type": "partial", "text": event.text})
                # Barge-in needs the caller to be making sound NOW (the gate
                # opened on new audio), not just text: a late partial about
                # audio already committed - seen in live calls, where it cut
                # the agent's reply off - or an echo of the agent itself must
                # not stop the agent.
                if (self.speaking and self.gate.in_utterance
                        and len(event.text.strip()) >= self.barge_in_min_chars):
                    await self._barge_in()
            elif event.kind == "final":
                await self._committed(event)
            elif event.kind == "error":
                if event.error == R.NO_SPEECH:
                    # Keep-alive silence can draw this too; it only matters
                    # while the caller is actually speaking.
                    if self.gate.in_utterance:
                        await self._committed(event)
                else:
                    await self._recover(event.error, session)
            elif event.kind == "closed" and not self.ended and self.session is session:
                await self._recover(R.DISCONNECTED, session)

    async def _committed(self, event) -> None:
        latency, self._current = self._current, R.TurnLatency()
        latency.speech_end_at = self.gate.last_voice_at
        latency.committed_at = event.at or self.clock()
        self.gate.committed()
        self._commit_sent = False
        self._utterance_audio.clear()
        text = (event.text or "").strip()
        if not text:
            # Noise or a cough opened the gate but there was nothing to hear.
            # Ask again only if it keeps happening - not after every rustle.
            self._empty_finals += 1
            await self._emit({"type": "no_speech"})
            if self._empty_finals >= 2 and not self.speaking:
                self._empty_finals = 0
                await self._say(R.say_again(self.language), cacheable=True)
            return
        self._empty_finals = 0
        logger.info("[%s] transcript committed (%d chars, language %s)", self.session_id,
                    len(text), event.language or "not reported")
        logger.debug("[%s] transcript: %s", self.session_id, text)
        await self._emit({"type": "final", "text": text, "language": event.language})
        await self._turns.put((text, event.language, latency))

    async def _recover(self, code: str | None, failed=None) -> None:
        """A transcription failure mid-call: reconnect when it is worth it,
        otherwise apologise and end the call - never crash it.

        A dropped socket is noticed twice (the reader sees it close, the next
        send fails); the lock and the `failed` check make that one reconnect."""
        async with self._recover_lock:
            if self.ended:
                return
            if failed is not None and failed is not self.session:
                # Another path already reconnected: make sure the sentence
                # the caller is in the middle of reached the new session.
                await self._resend_utterance()
                return
            await self._recover_locked(code)

    async def _resend_utterance(self) -> None:
        if (self.gate.in_utterance and self._utterance_audio and self.session is not None
                and self._resent_to is not self.session):
            self._resent_to = self.session
            try:
                await self.session.send(bytes(self._utterance_audio))
                self._last_sent_at = self.clock()
            except R.SpeechError:
                pass

    async def _recover_locked(self, code: str | None) -> None:
        if R.retryable(code) and self._reconnects < MAX_RECONNECTS:
            self._reconnects += 1
            logger.warning("[%s] STT %s - reconnecting (%d/%d)", self.session_id, code,
                           self._reconnects, MAX_RECONNECTS)
            old = self.session
            try:
                self.session = await self.stt.open_realtime(self.fmt.name, self.keyterms)
            except R.SpeechError as error:
                code = error.code
            else:
                if old is not None:
                    await old.close()
                self._last_sent_at = self.clock()
                await self._emit({"type": "stt_reconnected"})
                # The caller was mid-sentence: resend what they said, once.
                await self._resend_utterance()
                return
        logger.error("[%s] speech recognition failed: %s", self.session_id, code)
        await self._emit({"type": "error", "code": code})
        await self._say(R.technical_problem(self.language), cacheable=True)
        await self.end("stt_failed")

    # ------------------------------------------------------------------ turns
    async def _run_turns(self) -> None:
        while not self.ended:
            text, language, latency = await self._turns.get()
            await self._turn(text, latency)

    async def _turn(self, text: str, latency: R.TurnLatency) -> None:
        started = self.clock()
        try:
            result = await asyncio.to_thread(self.pipeline.process, self.session_id, text,
                                             self.patient_id)
        except Exception as exc:                           # pragma: no cover
            logger.error("[%s] pipeline failed: %s", self.session_id, exc)
            await self._say(R.technical_problem(self.language), cacheable=True)
            return
        latency.pipeline_ms = (self.clock() - started) * 1000
        latency.stages = dict(result.get("timings") or {})
        self.language = result.get("language") or self.language
        reply = result.get("response") or ""
        action = result.get("action")
        judge = result.get("judge") or {}
        await self._emit({"type": "turn", "transcript": text, "intent": result.get("intent"),
                          "raw_intent": result.get("raw_intent"),
                          "confidence": result.get("confidence"), "action": action,
                          "response": reply, "language": self.language,
                          "response_source": judge.get("source"),
                          "validation_problems": judge.get("validation_problems") or [],
                          "backend_result": result.get("backend_result"),
                          "appointment_id": result.get("appointment_id")})
        logger.info("[%s] turn: intent=%s action=%s pipeline %.0f ms", self.session_id,
                    result.get("intent"), action, latency.pipeline_ms)

        spoken = await self._say(reply, cacheable=action in _FIXED_REPLY_ACTIONS,
                                 latency=latency)
        summary = latency.summary()
        record = {"transcript": text, "intent": result.get("intent"), "action": action,
                  "response": reply, "response_source": judge.get("source"),
                  "spoken": spoken, "tts_error": self.last_tts_error,
                  "latency": summary,
                  "appointment_id": result.get("appointment_id")}
        self.turns.append(record)
        await self._emit({"type": "latency", **summary})
        logger.info("[%s] latency %s", self.session_id, summary)
        if action == Action.END_CONVERSATION:
            await self.end("goodbye")

    # ----------------------------------------------------------------- speech
    async def _say(self, text: str, cacheable: bool = False,
                   latency: R.TurnLatency | None = None) -> bool:
        """Speak `text`; True when it was all played. On failure the text
        is still sent as an event (the fallback), and the call carries on."""
        if not text:
            return False
        self._barged_in = False
        self.last_tts_error = None
        self._speech = asyncio.create_task(self._play(text, cacheable, latency),
                                           name=f"tts-{self.session_id}")
        try:
            return await self._speech
        except asyncio.CancelledError:
            if self._barged_in and not self.ended:
                return False                               # the caller interrupted
            raise

    async def _play(self, text: str, cacheable: bool,
                    latency: R.TurnLatency | None) -> bool:
        await self._emit({"type": "tts_start", "text": text})
        try:
            async for chunk in self.tts.stream(text, tts_language(self.language), cacheable):
                now = self.clock()
                if latency is not None and latency.agent_audio_at is None:
                    latency.agent_audio_at = now
                if self._out_fmt is not None:
                    self._playing_until = (max(self._playing_until, now)
                                           + self._out_fmt.duration_ms(len(chunk)) / 1000)
                await self.output.play(chunk)
        except R.SpeechError as error:
            self.last_tts_error = error.code
            logger.error("[%s] TTS failed (%s); the reply is sent as text", self.session_id,
                         error.code)
            await self._emit({"type": "tts_failed", "code": error.code, "text": text})
            return False
        finally:
            if latency is not None:
                latency.tts_first_byte_ms = getattr(self.tts, "last_first_byte_ms", None)
                latency.tts_total_ms = getattr(self.tts, "last_total_ms", None)
        await self._emit({"type": "tts_done"})
        return True

    async def interrupt(self) -> None:
        """Stop the agent now: the caller pressed push-to-talk while it spoke."""
        self._deaf_until = 0.0
        if self.speaking:
            await self._barge_in()
        else:
            await self.output.stop()

    async def _barge_in(self) -> None:
        """Stop talking: the caller has started speaking over the agent."""
        if not self.speaking:
            return
        self._barged_in = True
        if self._speech is not None and not self._speech.done():
            self._speech.cancel()
        self._playing_until = 0.0
        try:
            await self.output.stop()
        except Exception as exc:                          # pragma: no cover
            logger.warning("stop playback failed: %s", exc)
        logger.info("[%s] barge-in: playback stopped", self.session_id)
        await self._emit({"type": "barge_in"})
