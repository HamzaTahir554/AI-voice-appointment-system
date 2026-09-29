"""
The computer's microphone as the caller's side of the call (voice_app.py).

    microphone -> 16 kHz 16-bit mono PCM (pcm_16000) -> VoiceCall.feed

pcm_16000 is one of the formats ElevenLabs' realtime speech-to-text takes
directly, and every sound card can record it: on Windows the default MME
driver resamples in the driver. Only when a device refuses 16 kHz mono is it
opened at its own rate and channel count and converted here (one linear
interpolation and a channel average - no transcoding library, no FFmpeg).

Failures are turned into one `MicrophoneError` with a sentence a person can
act on ("no microphone", "blocked by Windows privacy settings", ...), so the
app can print it instead of a PortAudio traceback.
"""
from __future__ import annotations

import asyncio
import logging
import time
from array import array
from typing import Any

from speech.audio import AudioFormat

logger = logging.getLogger("speech.microphone")

# Error codes
NO_DEVICE = "no_device"
NOT_FOUND = "device_not_found"
BLOCKED = "blocked"                 # opened, but only digital silence arrives
BUSY = "device_busy"
DISCONNECTED = "disconnected"
FAILED = "failed"
NO_LIBRARY = "no_audio_library"

_MESSAGES = {
    NO_DEVICE: "No microphone found. Connect one and check Windows Settings > System > Sound.",
    NOT_FOUND: "The microphone in MICROPHONE_DEVICE was not found. "
               "Run: python voice_app.py --list-devices",
    BLOCKED: "The microphone records pure silence. Check it is not muted and that "
             "Windows Settings > Privacy > Microphone allows desktop apps.",
    BUSY: "The microphone is in use by another program or cannot be opened.",
    DISCONNECTED: "The microphone stopped sending audio (disconnected?).",
    FAILED: "The microphone could not be used.",
    NO_LIBRARY: "The sounddevice package is missing: pip install sounddevice",
}
# How long without any audio before the device counts as gone, and how long
# of exact zeros before it counts as blocked (a real microphone always hiss).
STALL_SECS = 2.0
BLOCKED_SECS = 3.0


class MicrophoneError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    @property
    def message(self) -> str:
        return _MESSAGES.get(self.code, _MESSAGES[FAILED])


def _sounddevice(module=None):
    if module is not None:
        return module
    try:
        import sounddevice
    except (ImportError, OSError) as exc:        # OSError: PortAudio library missing
        raise MicrophoneError(NO_LIBRARY, str(exc)) from exc
    return sounddevice


def _classify(exc: Exception) -> str:
    text = str(exc).lower()
    if "unavailable" in text or "busy" in text or "-9985" in text:
        return BUSY
    if "invalid device" in text or "-9996" in text or "no default" in text:
        return NO_DEVICE
    return FAILED


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
def list_devices(sd=None) -> dict[str, Any]:
    """Every input and output device, on the host API Windows uses by default."""
    sd = _sounddevice(sd)
    devices = list(sd.query_devices())
    default_in, default_out = (list(sd.default.device) + [-1, -1])[:2]
    try:
        default_api = sd.default.hostapi
    except AttributeError:                       # older sounddevice
        default_api = 0
    rows = {"inputs": [], "outputs": [], "default_input": default_in,
            "default_output": default_out}
    for index, device in enumerate(devices):
        if device.get("hostapi", 0) != default_api:
            continue
        entry = {"index": index, "name": device["name"],
                 "sample_rate": int(device.get("default_samplerate") or 0)}
        if device.get("max_input_channels", 0) > 0:
            rows["inputs"].append(entry)
        if device.get("max_output_channels", 0) > 0:
            rows["outputs"].append(entry)
    return rows


def resolve_device(setting: str | int | None, kind: str = "input", sd=None) -> int | None:
    """
    The device to open: None for the system default, else an index.

    `setting` is MICROPHONE_DEVICE / SPEAKER_DEVICE: empty, a device number,
    or part of a device name ("FANTECH", "Realtek").
    """
    sd = _sounddevice(sd)
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    error = MicrophoneError if kind == "input" else SpeakerError
    devices = list(sd.query_devices())
    text = "" if setting is None else str(setting).strip()
    if not text:
        default = (list(sd.default.device) + [-1, -1])[0 if kind == "input" else 1]
        if default is None or default < 0 or default >= len(devices) \
                or devices[default].get(channels_key, 0) <= 0:
            raise error(NO_DEVICE, f"no default {kind} device")
        return None
    if text.lstrip("-").isdigit():
        index = int(text)
        if not 0 <= index < len(devices) or devices[index].get(channels_key, 0) <= 0:
            raise error(NOT_FOUND, f"{kind} device {index}")
        return index
    wanted = text.lower()
    rows = list_devices(sd)["inputs" if kind == "input" else "outputs"]
    for row in rows:                             # the default host API first
        if wanted in row["name"].lower():
            return row["index"]
    for index, device in enumerate(devices):
        if wanted in device["name"].lower() and device.get(channels_key, 0) > 0:
            return index
    raise error(NOT_FOUND, f"no {kind} device named like {text!r}")


class SpeakerError(MicrophoneError):
    """The same codes, for the output device."""

    @property
    def message(self) -> str:
        return {
            NO_DEVICE: "No speaker or headphones found.",
            NOT_FOUND: "The device in SPEAKER_DEVICE was not found. "
                       "Run: python voice_app.py --list-devices",
            BUSY: "The speaker is in use by another program or cannot be opened.",
            NO_LIBRARY: _MESSAGES[NO_LIBRARY],
        }.get(self.code, "The speaker could not be used.")


# --------------------------------------------------------------------------
# Conversion, only for a device that refuses 16 kHz mono
# --------------------------------------------------------------------------
class _Converter:
    """Device audio (any rate, 1-2 channels, int16) to the target rate, mono."""

    def __init__(self, from_rate: int, to_rate: int, channels: int):
        self.ratio = from_rate / to_rate
        self.channels = channels
        # Blocks are joined seamlessly: each one is read as the previous
        # block's last sample followed by its own, and `position` is where the
        # next output sample falls in that sequence.
        self.position = 0.0
        self.last = 0

    def __call__(self, data: bytes) -> bytes:
        values = array("h")
        values.frombytes(data[:len(data) - len(data) % (2 * self.channels)])
        if self.channels > 1:
            step = self.channels
            values = array("h", (sum(values[i:i + step]) // step
                                 for i in range(0, len(values), step)))
        if self.ratio == 1.0 or not values:
            return values.tobytes()
        joined = array("h", [self.last]) + values
        end = len(joined) - 1
        out = array("h")
        position = self.position
        while position < end:
            left = int(position)
            frac = position - left
            out.append(int(joined[left] * (1 - frac) + joined[left + 1] * frac))
            position += self.ratio
        self.position = position - end
        self.last = joined[-1]
        return out.tobytes()


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------
class MicrophoneInput:
    """
    Opens the microphone and hands out its audio as `fmt` (pcm_16000).

        mic = MicrophoneInput(fmt, device)
        mic.start()
        chunk = await mic.read()      # 20 ms of audio
        mic.close()
    """

    def __init__(self, fmt: AudioFormat, device: int | None = None, block_ms: int = 20,
                 sd=None, clock=time.monotonic, stall_secs: float = STALL_SECS,
                 blocked_secs: float = BLOCKED_SECS):
        if fmt.encoding != "pcm_s16le":
            raise ValueError("the microphone records PCM (pcm_16000)")
        self.fmt = fmt
        self.device = device
        self.block_ms = block_ms
        self._sd = sd
        self.clock = clock
        self.stall_secs = stall_secs
        self.blocked_secs = blocked_secs
        self.stream = None
        self.device_name = "system default"
        self.device_rate = fmt.sample_rate
        self.device_channels = 1
        self._convert = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._zeros_since: float | None = None
        self.overflows = 0
        self.received_bytes = 0

    def _open(self, sd, rate: int, channels: int):
        blocksize = max(1, rate * self.block_ms // 1000)
        # "low": the driver's own buffering, measured here 40 -> 20 ms
        stream = sd.RawInputStream(samplerate=rate, channels=channels, dtype="int16",
                                   blocksize=blocksize, device=self.device,
                                   callback=self._callback, latency="low")
        stream.start()
        return stream

    def start(self) -> None:
        sd = _sounddevice(self._sd)
        self._loop = asyncio.get_running_loop()
        info = sd.query_devices(self.device, "input") if self.device is not None \
            else sd.query_devices(kind="input")
        self.device_name = info["name"]
        attempts = [(self.fmt.sample_rate, 1)]
        native = int(info.get("default_samplerate") or self.fmt.sample_rate)
        if native != self.fmt.sample_rate:
            attempts.append((native, 1))
        if info.get("max_input_channels", 1) >= 2:
            attempts.append((native, 2))
        last: Exception | None = None
        for rate, channels in attempts:
            # Ready before the stream starts: its first callback can come at once.
            self._convert = (None if (rate, channels) == (self.fmt.sample_rate, 1)
                             else _Converter(rate, self.fmt.sample_rate, channels))
            try:
                self.stream = self._open(sd, rate, channels)
            except Exception as exc:              # PortAudioError, ValueError
                last = exc
                logger.info("microphone refused %d Hz x%d: %s", rate, channels, exc)
                continue
            self.device_rate, self.device_channels = rate, channels
            logger.info("microphone open: %s at %d Hz x%d", self.device_name, rate, channels)
            self._zeros_since = None
            return
        raise MicrophoneError(_classify(last), str(last)) from last

    # PortAudio's thread: never block, never raise
    def _callback(self, indata, frames, time_info, status) -> None:
        if status and getattr(status, "input_overflow", False):
            self.overflows += 1
        data = bytes(indata)
        if self._convert is not None:
            data = self._convert(data)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, data)

    async def read(self) -> bytes:
        """The next piece of audio; MicrophoneError when the device stops
        delivering (unplugged) or delivers only digital silence (blocked)."""
        if self.stream is None:
            raise MicrophoneError(FAILED, "not started")
        try:
            data = await asyncio.wait_for(self._queue.get(), self.stall_secs)
        except asyncio.TimeoutError:
            raise MicrophoneError(DISCONNECTED, f"no audio for {self.stall_secs:g} s") from None
        self.received_bytes += len(data)
        if data and not data.strip(b"\x00"):
            now = self.clock()
            if self._zeros_since is None:
                self._zeros_since = now
            elif now - self._zeros_since >= self.blocked_secs:
                raise MicrophoneError(BLOCKED, "only digital silence")
        else:
            self._zeros_since = None
        return data

    def drain(self) -> None:
        """Drop audio that queued up while nobody was reading."""
        while not self._queue.empty():
            self._queue.get_nowait()

    def close(self) -> None:
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:                  # pragma: no cover - device already gone
            logger.info("closing the microphone: %s", exc)
