"""
The computer's speaker or headphones as the caller's ear (voice_app.py).

`SpeakerOutput` is an `AudioOutput` (speech/call.py) - the same three methods
a telephony bridge implements - so VoiceCall does not know it is talking to a
sound card:

    play(chunk)   ElevenLabs' streamed audio, queued and played as it arrives
                  (the first chunk is heard while the rest is still coming)
    stop()        the caller interrupted: drop what has not been played yet
    hang_up()     let the last sentence finish, then close the device

A PortAudio callback pulls from the queue every 20 ms; when it is empty the
device plays silence. Nothing is written to disk.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from speech.audio import AudioFormat, to_pcm16
from speech.microphone import FAILED, SpeakerError, _classify, _sounddevice

logger = logging.getLogger("speech.speaker")
STALL_SECS = 1.0


class SpeakerOutput:
    def __init__(self, fmt: AudioFormat, device: int | None = None, block_ms: int = 20,
                 sd=None, clock=time.monotonic, drain_timeout: float = 30.0):
        self.fmt = fmt                    # what ElevenLabs sends (pcm_22050, ulaw_8000...)
        self.device = device
        self.block_ms = block_ms
        self._sd = sd
        self.clock = clock
        self.drain_timeout = drain_timeout
        self.stream = None
        self.device_name = "system default"
        self.output_latency_ms: float | None = None   # what the driver reports
        self._buffer = bytearray()        # 16-bit PCM waiting to be played
        self._lock = threading.Lock()
        self._played_at: float | None = None          # last callback that played audio
        self._called_at: float | None = None          # last callback at all
        self.stalled = False              # the device stopped asking for audio
        # per reply, for the latency record
        self.reply_first_chunk_at: float | None = None
        self.reply_first_played_at: float | None = None
        self.played_bytes = 0
        self.underruns = 0                # late callbacks while speech was playing
        self._was_playing = False

    # ------------------------------------------------------------------
    def start(self) -> None:
        sd = _sounddevice(self._sd)
        try:
            info = sd.query_devices(self.device, "output") if self.device is not None \
                else sd.query_devices(kind="output")
            self.device_name = info["name"]
            blocksize = max(1, self.fmt.sample_rate * self.block_ms // 1000)
            # "low": every reply starts this much sooner - the driver's
            # buffering, measured here 200 -> 100 ms
            self.stream = sd.RawOutputStream(samplerate=self.fmt.sample_rate, channels=1,
                                             dtype="int16", blocksize=blocksize,
                                             device=self.device, callback=self._callback,
                                             latency="low")
            self.stream.start()
        except SpeakerError:
            raise
        except Exception as exc:                  # PortAudioError, ValueError
            raise SpeakerError(_classify(exc), str(exc)) from exc
        self._called_at = self.clock()    # a device that never calls back counts as stalled
        latency = getattr(self.stream, "latency", None)
        self.output_latency_ms = None if latency is None else float(latency) * 1000
        logger.info("speaker open: %s at %d Hz", self.device_name, self.fmt.sample_rate)

    # PortAudio's thread: never block, never raise
    def _callback(self, outdata, frames, time_info, status) -> None:
        wanted = len(outdata)
        self._called_at = self.clock()
        with self._lock:
            take = min(wanted, len(self._buffer) - len(self._buffer) % 2)
            outdata[:take] = self._buffer[:take]
            del self._buffer[:take]
        if take < wanted:
            outdata[take:] = b"\x00" * (wanted - take)
        if take:
            now = self.clock()
            self._played_at = now
            self.played_bytes += take
            if self.reply_first_played_at is None and self.reply_first_chunk_at is not None:
                self.reply_first_played_at = now
        # Only a gap in speech is heard; a late callback during silence is not.
        if (status and getattr(status, "output_underflow", False)
                and (take or self._was_playing)):
            self.underruns += 1
        self._was_playing = bool(take)

    # ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        """Audio is queued, or the device is still sounding the last of it."""
        with self._lock:
            if self._buffer:
                # A device that stopped pulling audio (unplugged) must not keep
                # the microphone muted for ever: drop what it will never play.
                last = self._called_at
                if last is not None and self.clock() - last > STALL_SECS:
                    self._buffer.clear()
                    self.stalled = True
                    logger.warning("the speaker stopped playing (disconnected?)")
                    return False
                return True
        if self._played_at is None:
            return False
        tail = (self.output_latency_ms or 0) / 1000 + self.block_ms / 1000
        return self.clock() - self._played_at < tail

    def new_reply(self) -> None:
        """A new sentence is about to be spoken: time it from here."""
        self.reply_first_chunk_at = None
        self.reply_first_played_at = None

    async def play(self, chunk: bytes) -> None:
        if self.stream is None:
            raise SpeakerError(FAILED, "speaker not started")
        if self.fmt.encoding == "ulaw":
            chunk = to_pcm16(chunk, self.fmt)
        if self.reply_first_chunk_at is None:
            self.reply_first_chunk_at = self.clock()
        with self._lock:
            self._buffer += chunk

    async def stop(self) -> None:
        with self._lock:
            self._buffer.clear()

    async def drain(self) -> None:
        """Wait until everything queued has been played."""
        deadline = self.clock() + self.drain_timeout
        while self.busy and self.clock() < deadline:
            await asyncio.sleep(0.05)

    async def hang_up(self) -> None:
        await self.drain()

    def close(self) -> None:
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:                  # pragma: no cover - device already gone
            logger.info("closing the speaker: %s", exc)
