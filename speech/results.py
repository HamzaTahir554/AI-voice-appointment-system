"""
What the speech layer hands back: plain results, never exceptions.

A failed transcription or synthesis must not end a phone call, so every
service returns one of these. `error` is a short machine code for logs and
tests; the caller only ever hears a friendly sentence (`say_again`,
`technical_problem`), never the technical reason.

Nothing is invented: ElevenLabs' realtime transcripts carry no confidence
score, so there is no confidence field, and `language` stays None unless
ElevenLabs reported one.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Error codes
# --------------------------------------------------------------------------
NO_SPEECH = "no_speech"                 # silence, noise, or nothing recognisable
EMPTY_AUDIO = "empty_audio"
TOO_SHORT = "audio_too_short"
INVALID_AUDIO = "invalid_audio"
EMPTY_TEXT = "empty_text"
TIMEOUT = "timeout"
UNAVAILABLE = "provider_unavailable"
RATE_LIMITED = "rate_limited"
AUTH_FAILED = "auth_failed"
QUOTA_EXCEEDED = "quota_exceeded"
PLAN_REQUIRED = "paid_plan_required"     # e.g. a Voice Library voice on a free plan
DISCONNECTED = "disconnected"
INVALID_VOICE = "invalid_voice"
INVALID_REQUEST = "invalid_request"
NOT_CONFIGURED = "not_configured"

# Worth one more attempt; everything else would fail the same way again.
RETRYABLE = frozenset({TIMEOUT, UNAVAILABLE, RATE_LIMITED, DISCONNECTED})


def retryable(code: str | None) -> bool:
    return code in RETRYABLE


class SpeechError(Exception):
    """Raised inside the provider code, turned into a result at its edge."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass
class STTResult:
    success: bool
    text: str = ""
    language: str | None = None          # only when ElevenLabs reported it
    is_final: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items()
                if value is not None or key in ("success", "text", "is_final")}


@dataclass
class TTSResult:
    success: bool
    audio: bytes = b""
    audio_format: str = ""
    characters: int = 0                  # what ElevenLabs bills for
    first_byte_ms: float | None = None
    total_ms: float | None = None
    from_cache: bool = False
    error: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["audio_bytes"] = len(data.pop("audio"))
        return data


@dataclass
class TurnLatency:
    """One caller turn, timed from the caller's side of the call."""
    speech_end_at: float | None = None    # last audio with speech in it
    committed_at: float | None = None     # ElevenLabs committed the transcript
    first_partial_at: float | None = None
    pipeline_ms: float | None = None      # mBERT + Dialog + Backend + judge
    tts_first_byte_ms: float | None = None
    tts_total_ms: float | None = None
    agent_audio_at: float | None = None   # first reply audio handed to the call
    marks: dict[str, float] = field(default_factory=dict)
    # Inside the pipeline, ms: mbert_ms, dialog_ms, database_ms, llm_ms
    stages: dict[str, float] = field(default_factory=dict)

    def summary(self) -> dict[str, float | None]:
        def gap(a, b):
            return round((b - a) * 1000, 1) if a is not None and b is not None else None
        return {
            "speech_end_to_transcript_ms": gap(self.speech_end_at, self.committed_at),
            "pipeline_ms": None if self.pipeline_ms is None else round(self.pipeline_ms, 1),
            "tts_first_byte_ms": (None if self.tts_first_byte_ms is None
                                  else round(self.tts_first_byte_ms, 1)),
            # The number a caller feels: they stop talking ... the agent starts.
            "speech_end_to_agent_audio_ms": gap(self.speech_end_at, self.agent_audio_at),
            **{name: None if value is None else round(value, 1)
               for name, value in self.stages.items()},
        }


# --------------------------------------------------------------------------
# Fixed sentences: the same words for every caller, so they are cached as audio
# --------------------------------------------------------------------------
GREETING = "Assalam o Alaikum. Main aap ki kya madad kar sakti hoon?"

# What the caller hears when speech fails (never the technical reason)
_SAY_AGAIN = {
    "roman_urdu": "Maazrat, mujhe clear sunai nahi diya. Dobara bol dein.",
    "urdu": "معذرت، مجھے صاف سنائی نہیں دیا۔ دوبارہ بول دیں۔",
    "english": "Sorry, I didn't catch that clearly. Could you say it again?",
}
_TECHNICAL = {
    "roman_urdu": "Maazrat, is waqt system mein masla hai. Thori dair baad dobara call karein.",
    "urdu": "معذرت، اس وقت سسٹم میں مسئلہ ہے۔ تھوڑی دیر بعد دوبارہ کال کریں۔",
    "english": "Sorry, we are having a technical problem. Please call again in a little while.",
}


def say_again(language: str | None) -> str:
    return _SAY_AGAIN.get(language or "", _SAY_AGAIN["roman_urdu"])


def technical_problem(language: str | None) -> str:
    return _TECHNICAL.get(language or "", _TECHNICAL["roman_urdu"])
