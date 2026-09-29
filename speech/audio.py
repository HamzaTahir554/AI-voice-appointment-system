"""
Audio as the call carries it, and a gate that decides what is worth sending.

Formats
    The call path never converts audio. ElevenLabs' realtime transcription
    accepts 8 kHz mu-law (`ulaw_8000`) - what a phone line carries - and 16 kHz
    16-bit PCM (`pcm_16000`), and its text-to-speech can answer in the same
    formats, so audio goes from the caller to ElevenLabs and back untouched.
    The only decoding on the call path is reading mu-law sample values to
    measure loudness, which does not change what is sent.

    Conversion (resampling, encoding) exists for test clips and saved
    recordings only, in the functions marked "files only".

The speech gate
    Realtime transcription is billed by the audio sent, and most of a phone
    call is silence while the agent talks or the caller thinks. The gate holds
    audio back until the caller starts speaking (keeping the last ~300 ms so
    the first syllable is not clipped), then sends everything - pauses
    included, because ElevenLabs needs the pause to know the turn has ended -
    until the transcript is committed. It learns the line's background noise
    so a steady hum does not open it.
"""
from __future__ import annotations

import io
import math
import time
import wave
from array import array
from collections import deque
from dataclasses import dataclass
from typing import Callable

# --------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioFormat:
    name: str                 # ElevenLabs' name for it: ulaw_8000, pcm_16000
    encoding: str             # "ulaw" or "pcm_s16le"
    sample_rate: int
    sample_width: int         # bytes per sample

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.sample_width

    def bytes_for(self, ms: float) -> int:
        count = int(self.sample_rate * ms / 1000) * self.sample_width
        return max(self.sample_width, count)

    def duration_ms(self, nbytes: int) -> float:
        return nbytes * 1000.0 / self.bytes_per_second


def audio_format(name: str) -> AudioFormat:
    """`ulaw_8000`, `pcm_8000`, `pcm_16000`, `pcm_22050`, `pcm_24000`, ..."""
    kind, _, rate = str(name or "").partition("_")
    if kind == "ulaw" and rate == "8000":
        return AudioFormat("ulaw_8000", "ulaw", 8000, 1)
    if kind == "pcm" and rate.isdigit():
        return AudioFormat(f"pcm_{rate}", "pcm_s16le", int(rate), 2)
    raise ValueError(f"unsupported audio format {name!r}: use ulaw_8000 or pcm_<rate>")


# --------------------------------------------------------------------------
# G.711 mu-law
# --------------------------------------------------------------------------
def _ulaw_to_sample(value: int) -> int:
    value = ~value & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -sample if sign else sample


_ULAW_DECODE = array("h", (_ulaw_to_sample(v) for v in range(256)))
_BIAS, _CLIP = 0x84, 32635


def _sample_to_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(sample), _CLIP) + _BIAS
    exponent, mask = 7, 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def samples(data: bytes, fmt: AudioFormat) -> array:
    """Linear 16-bit sample values (a copy; the audio itself is untouched)."""
    if fmt.encoding == "ulaw":
        return array("h", (_ULAW_DECODE[b] for b in data))
    usable = len(data) - len(data) % 2
    values = array("h")
    values.frombytes(data[:usable])
    return values


def rms(data: bytes, fmt: AudioFormat) -> float:
    """Loudness, 0.0 (silence) .. 1.0 (full scale)."""
    values = samples(data, fmt)
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values)) / 32768.0


# --------------------------------------------------------------------------
# The speech gate
# --------------------------------------------------------------------------
class SpeechGate:
    """
    Feed it the caller's audio in any chunk size; it returns what to send to
    transcription now (possibly nothing).

    States: idle (holding a short pre-roll) -> in an utterance (sending
    everything) -> idle again when `committed()` is called.
    """

    def __init__(self, fmt: AudioFormat, frame_ms: int = 20, preroll_ms: int = 300,
                 onset_ms: int = 60, threshold: float = 0.015,
                 noise_factor: float = 3.0,
                 clock: Callable[[], float] = time.monotonic):
        self.fmt = fmt
        self.frame_bytes = fmt.bytes_for(frame_ms)
        self.frame_ms = frame_ms
        self.onset_frames = max(1, onset_ms // frame_ms)
        self.threshold = threshold
        self.noise_factor = noise_factor
        self.noise_floor = threshold / noise_factor
        self.clock = clock
        self._pending = b""
        self._preroll: deque[bytes] = deque(maxlen=max(1, preroll_ms // frame_ms))
        self._voiced_run = 0
        self.in_utterance = False
        self.last_voice_at: float | None = None     # wall clock, last voiced frame
        self.utterance_started_at: float | None = None
        self.received_bytes = 0
        self.sent_bytes = 0

    @property
    def level(self) -> float:
        """The loudness a frame needs to count as speech right now."""
        return max(self.threshold, self.noise_floor * self.noise_factor)

    @property
    def held_back_bytes(self) -> int:
        """Audio never sent to transcription - what the gate saved."""
        return self.received_bytes - self.sent_bytes

    def push(self, chunk: bytes) -> list[bytes]:
        out: list[bytes] = []
        self.received_bytes += len(chunk)
        data = self._pending + chunk
        whole = len(data) - len(data) % self.frame_bytes
        self._pending = data[whole:]
        for start in range(0, whole, self.frame_bytes):
            frame = data[start:start + self.frame_bytes]
            loudness = rms(frame, self.fmt)
            voiced = loudness > self.level
            now = self.clock()
            if self.in_utterance:
                out.append(frame)
                if voiced:
                    self.last_voice_at = now
                continue
            # idle: learn the line's noise from what is not speech
            if not voiced:
                self.noise_floor = 0.95 * self.noise_floor + 0.05 * loudness
            self._preroll.append(frame)
            self._voiced_run = self._voiced_run + 1 if voiced else 0
            if self._voiced_run >= self.onset_frames:
                self.in_utterance = True
                self.utterance_started_at = now
                self.last_voice_at = now
                out.extend(self._preroll)
                self._preroll.clear()
                self._voiced_run = 0
        self.sent_bytes += sum(len(f) for f in out)
        return out

    def silence_ms(self) -> float:
        """How long the caller has been quiet inside the current utterance."""
        if not self.in_utterance or self.last_voice_at is None:
            return 0.0
        return (self.clock() - self.last_voice_at) * 1000

    def start_utterance(self) -> None:
        """Push-to-talk: the caller pressed the key, so everything from now on
        is sent, however quiet - the key press, not loudness, decides."""
        if self.in_utterance:
            return
        now = self.clock()
        self.in_utterance = True
        self.utterance_started_at = now
        self.last_voice_at = now
        self._preroll.clear()
        self._voiced_run = 0

    def committed(self) -> None:
        """The transcript for this utterance is in: stop sending until the
        caller speaks again."""
        self.in_utterance = False
        self.utterance_started_at = None
        self._voiced_run = 0
        self._preroll.clear()


# --------------------------------------------------------------------------
# Files only: test clips and saved recordings, never the call path
# --------------------------------------------------------------------------
def to_pcm16(data: bytes, fmt: AudioFormat) -> bytes:
    return samples(data, fmt).tobytes()


def from_pcm16(pcm: bytes, target: AudioFormat) -> bytes:
    values = array("h")
    values.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if target.encoding == "ulaw":
        return bytes(_sample_to_ulaw(v) for v in values)
    return values.tobytes()


def resample_pcm16(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Linear interpolation - good enough for speech test clips."""
    if from_rate == to_rate:
        return pcm
    values = array("h")
    values.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if not values:
        return b""
    count = int(len(values) * to_rate / from_rate)
    out = array("h")
    ratio = from_rate / to_rate
    last = len(values) - 1
    for i in range(count):
        position = i * ratio
        left = int(position)
        right = min(left + 1, last)
        frac = position - left
        out.append(int(values[left] * (1 - frac) + values[right] * frac))
    return out.tobytes()


def wav_bytes(data: bytes, fmt: AudioFormat) -> bytes:
    """A playable WAV (16-bit PCM) of audio in any supported format."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(fmt.sample_rate)
        out.writeframes(to_pcm16(data, fmt))
    return buffer.getvalue()


def read_wav(raw: bytes, target: AudioFormat) -> bytes:
    """A WAV file's audio in `target` format (mono, resampled, encoded)."""
    with wave.open(io.BytesIO(raw), "rb") as source:
        channels, width, rate = (source.getnchannels(), source.getsampwidth(),
                                 source.getframerate())
        frames = source.readframes(source.getnframes())
    if width != 2:
        raise ValueError("only 16-bit PCM WAV files are supported")
    if channels > 1:
        values = array("h")
        values.frombytes(frames)
        frames = array("h", (int(sum(values[i:i + channels]) / channels)
                             for i in range(0, len(values), channels))).tobytes()
    return from_pcm16(resample_pcm16(frames, rate, target.sample_rate), target)


def tone(fmt: AudioFormat, ms: int, amplitude: float = 0.0, frequency: float = 220.0,
         noise: float = 0.0, seed: int = 1) -> bytes:
    """Test audio: silence, a steady tone, or noise, in `fmt`."""
    import random
    rng = random.Random(seed)
    count = int(fmt.sample_rate * ms / 1000)
    values = array("h", (
        int(max(-32767, min(32767, 32767 * (
            amplitude * math.sin(2 * math.pi * frequency * i / fmt.sample_rate)
            + noise * rng.uniform(-1, 1)))))
        for i in range(count)))
    return from_pcm16(values.tobytes(), fmt)


def pack_frames(data: bytes, size: int) -> list[bytes]:
    return [data[i:i + size] for i in range(0, len(data), size)]


__all__ = ["AudioFormat", "audio_format", "rms", "samples", "SpeechGate",
           "to_pcm16", "from_pcm16", "resample_pcm16", "wav_bytes", "read_wav",
           "tone", "pack_frames"]
