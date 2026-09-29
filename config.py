"""
Central configuration for the AI Voice Appointment System.

Everything tunable lives here so the dialog logic stays free of magic numbers:
the mBERT label mapping, slot requirements, thresholds, timezone and Firebase
settings.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

try:                                    # .env is optional
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:                     # pragma: no cover
    pass

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
INTENT_MODULE_DIR = Path(
    os.environ.get("INTENT_MODULE_DIR", REPO_ROOT / "intent_detection"))
INTENT_SRC_DIR = INTENT_MODULE_DIR / "src"
INTENT_MODEL_DIR = INTENT_MODULE_DIR / "models" / "mbert_intent_classifier"


def add_intent_module_to_path() -> None:
    """Make `intent_detection/src` importable (it is a sibling project)."""
    src = str(INTENT_SRC_DIR)
    if src not in sys.path:
        sys.path.insert(0, src)


# --------------------------------------------------------------------------
# Timezone - Pakistan. Never treat appointment dates as UTC.
# --------------------------------------------------------------------------
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Karachi")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

# --------------------------------------------------------------------------
# Firebase
# --------------------------------------------------------------------------
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
FIREBASE_CLIENT_EMAIL = os.environ.get("FIREBASE_CLIENT_EMAIL", "")
# Private keys carry literal "\n" when stored in a single env-var line.
FIREBASE_PRIVATE_KEY = os.environ.get("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")
# Alternative: point at a service-account JSON file kept OUTSIDE the repo.
FIREBASE_CREDENTIALS_FILE = os.environ.get("FIREBASE_CREDENTIALS_FILE", "")
# Force the local in-memory backend even when credentials exist (useful in CI).
USE_LOCAL_DB = os.environ.get("USE_LOCAL_DB", "").lower() in ("1", "true", "yes")

# Firestore collection names (spec section 10)
# --------------------------------------------------------------------------
# The clinic
#
# This is a SINGLE-clinic system: one clinic record serves every doctor, and
# its name and address are what the assistant tells callers. The id below is
# the document that holds it; nothing else creates clinic records.
# --------------------------------------------------------------------------
CLINIC_ID = os.environ.get("CLINIC_ID", "C001")


class Collections:
    PATIENTS = "patients"
    DOCTORS = "doctors"
    CLINICS = "clinics"
    SCHEDULES = "schedules"
    UNAVAILABILITY = "doctor_unavailability"
    APPOINTMENTS = "appointments"
    SESSIONS = "conversation_sessions"
    MESSAGES = "conversation_messages"
    NOTIFICATIONS = "notifications"
    # Dashboard sign-in credentials (hashed); never read by the voice pipeline.
    ACCOUNTS = "dashboard_accounts"


# --------------------------------------------------------------------------
# Ollama (local LLM used as judge + response writer, never as the database)
# --------------------------------------------------------------------------
# 127.0.0.1, not "localhost": on Windows "localhost" is tried as IPv6 (::1)
# first, Ollama listens on IPv4 only, and the refused attempt cost ~2.1 s on
# EVERY request here (measured: 2,236-2,316 ms vs 191 ms for the same call).
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", 30))
# Low temperature: this model rewrites facts it is given, it does not invent.
OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", 0.2))
# A spoken reply is one or two sentences. Uncapped, a small model rambles in
# JSON mode until the request times out.
OLLAMA_MAX_TOKENS = int(os.environ.get("OLLAMA_MAX_TOKENS", 200))
# Turn the LLM off entirely and use deterministic templates.
OLLAMA_ENABLED = os.environ.get("OLLAMA_ENABLED", "true").lower() not in (
    "0", "false", "no")
# Let the LLM translate turns that involve NO database operation (slot
# questions, greetings) into the caller's language. Off by default: a 3B model
# produces broken Roman Urdu, and with no backend result there is nothing for
# the validator to check it against, so a bad translation would reach the
# caller. Turn it on with a stronger model (llama3.1:8b, qwen2.5:7b).
OLLAMA_TRANSLATE = os.environ.get("OLLAMA_TRANSLATE", "false").lower() in (
    "1", "true", "yes")

# Language the assistant replies in: "auto" follows the caller, or force one of
# "english" / "roman_urdu" / "urdu". Auto is sticky per conversation - a short
# "yes" in English must not switch a Roman-Urdu call to English.
RESPONSE_LANGUAGE = os.environ.get("RESPONSE_LANGUAGE", "auto").lower()

# Prefix for generated appointment IDs.
APPOINTMENT_ID_PREFIX = os.environ.get("APPOINTMENT_ID_PREFIX", "APT")

# --------------------------------------------------------------------------
# Speech: ElevenLabs speech-to-text and text-to-speech (speech/)
#
# Server-side only. The key is read here and sent to ElevenLabs in a request
# header; it is never logged, never returned by the API and never reaches the
# dashboard. Every model and voice is chosen here, nowhere else.
# --------------------------------------------------------------------------
def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _codes(name: str, default: str = "") -> list[str]:
    return [c.strip() for c in os.environ.get(name, default).split(",") if c.strip()]


ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_API_URL = os.environ.get("ELEVENLABS_API_URL",
                                    "https://api.elevenlabs.io").rstrip("/")
# Realtime transcription for calls; the batch model for whole recordings.
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v2_realtime")
ELEVENLABS_STT_BATCH_MODEL = os.environ.get("ELEVENLABS_STT_BATCH_MODEL", "scribe_v2")
# ISO 639-1/3 code the caller is expected to speak ("ur"), or empty to let
# ElevenLabs detect it. Secondary languages cover code-switching ("en").
ELEVENLABS_STT_LANGUAGE = os.environ.get("ELEVENLABS_STT_LANGUAGE", "ur").strip()
ELEVENLABS_STT_SECONDARY_LANGUAGES = _codes("ELEVENLABS_STT_SECONDARY_LANGUAGES")
# The caller's audio as it arrives: telephony is 8 kHz mu-law (ulaw_8000);
# pcm_16000 for a microphone or a test file.
ELEVENLABS_STT_AUDIO_FORMAT = os.environ.get("ELEVENLABS_STT_AUDIO_FORMAT", "ulaw_8000")
# End of turn: how long a pause (seconds) ElevenLabs waits before committing
# what the caller said. Long enough that "Mujhe Dr Ahmed... se kal" stays one
# sentence, short enough that the reply is not held up.
ELEVENLABS_STT_SILENCE_SECS = float(os.environ.get("ELEVENLABS_STT_SILENCE_SECS", 0.8))
# Audio is sent in chunks of this length (ms): small enough not to add delay,
# large enough not to flood the socket with 20 ms telephony frames.
ELEVENLABS_STT_CHUNK_MS = int(os.environ.get("ELEVENLABS_STT_CHUNK_MS", 100))
# Keyterm prompting (doctor names, specialisations, the clinic). ElevenLabs
# charges extra for it and allows 50 terms of 20 characters in realtime, so
# it is off unless switched on.
ELEVENLABS_STT_KEYTERMS = _flag("ELEVENLABS_STT_KEYTERMS", False)

# Chosen by measurement (docs/VOICE.md, section 9): the same time to first
# audio as eleven_flash_v2_5 (~0.82 s), Urdu support, and the times and
# negations in its speech were heard back correctly where Flash's were not.
ELEVENLABS_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_v3_conversational")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
# ulaw_8000 plays straight into a phone call; pcm_16000 / mp3_44100_128 for a
# browser or a file.
ELEVENLABS_TTS_OUTPUT_FORMAT = os.environ.get("ELEVENLABS_TTS_OUTPUT_FORMAT", "ulaw_8000")
# Optional language to enforce on the TTS model (ISO 639-1); empty = the
# model decides. Not accepted by the multilingual_v2 models.
ELEVENLABS_TTS_LANGUAGE = os.environ.get("ELEVENLABS_TTS_LANGUAGE", "").strip()

ELEVENLABS_TIMEOUT = float(os.environ.get("ELEVENLABS_TIMEOUT", 15))
# Temporary failures (timeout, rate limit, 5xx, dropped connection) are tried
# again this many times; an invalid key or voice never is.
ELEVENLABS_MAX_RETRIES = int(os.environ.get("ELEVENLABS_MAX_RETRIES", 1))
# false asks ElevenLabs not to retain the request (zero-retention mode, which
# the account has to be allowed to use).
ELEVENLABS_ENABLE_LOGGING = _flag("ELEVENLABS_ENABLE_LOGGING", True)

# Shared secret a telephony bridge presents to /voice/call. When empty, only
# connections from this machine are accepted.
VOICE_GATEWAY_TOKEN = os.environ.get("VOICE_GATEWAY_TOKEN", "").strip()

# --------------------------------------------------------------------------
# Microphone voice mode (voice_app.py): the computer's microphone and speaker
# stand in for the phone line until Asterisk/SIP is connected.
# --------------------------------------------------------------------------
# Input / output device: a number or part of the name from
# `python voice_app.py --list-devices`; empty = the system default.
MICROPHONE_DEVICE = os.environ.get("MICROPHONE_DEVICE", "").strip()
SPEAKER_DEVICE = os.environ.get("SPEAKER_DEVICE", "").strip()
# "vad": speak naturally, the end of a sentence is detected.
# "push_to_talk": press SPACE to start speaking and SPACE again to stop.
VOICE_INPUT_MODE = os.environ.get("VOICE_INPUT_MODE", "vad").strip().lower() or "vad"
# Print every stage of every turn (transcript, intent, action, judge, timings).
VOICE_DEBUG = _flag("VOICE_DEBUG", False)
# What the microphone sends to speech-to-text: 16 kHz 16-bit mono, which
# every sound card records natively or through Windows' own resampler.
VOICE_MIC_FORMAT = os.environ.get("VOICE_MIC_FORMAT", "pcm_16000")
# What the speaker plays: a sound card needs PCM, and 22.05 kHz sounds better
# than the phone line's 8 kHz mu-law.
VOICE_TTS_OUTPUT_FORMAT = os.environ.get("VOICE_TTS_OUTPUT_FORMAT", "pcm_22050")
# Keep listening while the agent speaks, so the caller can interrupt it. Only
# with headphones: through loudspeakers the microphone hears the agent.
# Off: the microphone is muted while the agent speaks (half duplex).
VOICE_BARGE_IN = _flag("VOICE_BARGE_IN", False)
# Every reply of a voice call is worded by Ollama and checked by the
# validator; off = only the turns with a database result go to Ollama (the
# behaviour of the text pipeline).
VOICE_OLLAMA_EVERY_TURN = _flag("VOICE_OLLAMA_EVERY_TURN", True)


# --------------------------------------------------------------------------
# Appointment backend error codes (spec section 15 - lower snake_case)
# --------------------------------------------------------------------------
class ErrorCode:
    PATIENT_NOT_FOUND = "patient_not_found"
    DOCTOR_NOT_FOUND = "doctor_not_found"
    CLINIC_NOT_FOUND = "clinic_not_found"
    APPOINTMENT_NOT_FOUND = "appointment_not_found"
    NOT_YOUR_APPOINTMENT = "not_your_appointment"
    INVALID_DATE = "invalid_date"
    INVALID_TIME = "invalid_time"
    DATE_IN_PAST = "date_in_past"
    DOCTOR_NOT_WORKING = "doctor_not_working"
    DOCTOR_UNAVAILABLE = "doctor_unavailable"
    OUTSIDE_WORKING_HOURS = "outside_working_hours"
    SLOT_UNAVAILABLE = "slot_unavailable"
    DUPLICATE_APPOINTMENT = "duplicate_appointment"
    ALREADY_CANCELLED = "already_cancelled"
    ALREADY_COMPLETED = "already_completed"
    BACKEND_UNAVAILABLE = "backend_unavailable"


# --------------------------------------------------------------------------
# Confidence policy
# --------------------------------------------------------------------------
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", 0.60))
# Three bands rather than a single cut-off:
#   >= HIGH        act on it
#   THRESHOLD..HIGH  believe it only if the conversation supports it, else
#                    ask one short clarifying question
#   <  THRESHOLD   never act; ask what the caller meant
HIGH_CONFIDENCE = float(os.environ.get("HIGH_CONFIDENCE", 0.80))

# Escalate utterances that describe an acute danger sign ("saans nahi aa rahi",
# "khoon beh raha hai", "behosh") even when the classifier missed the
# emergency. Added by the system audit: on unseen rows the model read "Bohot
# khoon beh raha hai" as thank_you 0.97. Set to false to rely on mBERT alone.
EMERGENCY_SAFETY_NET = os.environ.get("EMERGENCY_SAFETY_NET", "true").lower() not in ("0", "false", "no")
# A higher bar for abandoning an in-progress workflow: switching intent
# mid-booking is disruptive, so we only do it when the model is really sure.
INTENT_SWITCH_THRESHOLD = float(os.environ.get("INTENT_SWITCH_THRESHOLD", 0.75))
MAX_CLARIFICATION_ATTEMPTS = 2

# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
SESSION_TIMEOUT_MINUTES = int(os.environ.get("SESSION_TIMEOUT_MINUTES", 15))
MAX_HISTORY_TURNS = 40

# --------------------------------------------------------------------------
# Canonical dialog intents
# --------------------------------------------------------------------------
# Deliberately separate from the mBERT label names, so the model can be
# retrained or renamed without touching any dialog logic.
class Intent:
    BOOK_APPOINTMENT = "book_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    CHECK_APPOINTMENT = "check_appointment"
    DOCTOR_AVAILABILITY = "doctor_availability"
    DOCTOR_INFORMATION = "doctor_information"
    CLINIC_INFORMATION = "clinic_information"
    EMERGENCY = "emergency"
    GREETING = "greeting"
    GOODBYE = "goodbye"
    THANKS = "thanks"
    HELP = "help"
    CONFIRM = "confirm"
    DENY = "deny"
    PROVIDE_INFO = "provide_info"
    REPEAT = "repeat"
    UNKNOWN = "unknown_intent"


# --------------------------------------------------------------------------
# mBERT label -> canonical intent
# --------------------------------------------------------------------------
# LEFT  = the 31 labels the trained model actually emits
#         (intent_detection/data/label2id.json)
# RIGHT = what the Dialog Manager does about it
#
# Retraining with different label names? Edit ONLY this table.
INTENT_MAPPING = {
    # appointment workflows
    "book_appointment": Intent.BOOK_APPOINTMENT,
    "cancel_appointment": Intent.CANCEL_APPOINTMENT,
    "reschedule_appointment": Intent.RESCHEDULE_APPOINTMENT,
    "appointment_status": Intent.CHECK_APPOINTMENT,
    "check_availability": Intent.DOCTOR_AVAILABILITY,
    "appointment_confirmation": Intent.CONFIRM,
    "change_date": Intent.RESCHEDULE_APPOINTMENT,
    "change_time": Intent.RESCHEDULE_APPOINTMENT,
    "change_doctor": Intent.RESCHEDULE_APPOINTMENT,
    # information
    "find_doctor": Intent.DOCTOR_INFORMATION,
    "doctor_information": Intent.DOCTOR_INFORMATION,
    "doctor_fee": Intent.DOCTOR_INFORMATION,
    "doctor_qualifications": Intent.DOCTOR_INFORMATION,
    "doctor_specialization": Intent.DOCTOR_INFORMATION,
    "doctor_unavailable": Intent.DOCTOR_AVAILABILITY,
    "clinic_location": Intent.CLINIC_INFORMATION,
    "clinic_timing": Intent.CLINIC_INFORMATION,
    "clinic_closed": Intent.CLINIC_INFORMATION,
    # conversation
    "greeting": Intent.GREETING,
    "goodbye": Intent.GOODBYE,
    "thank_you": Intent.THANKS,
    "help": Intent.HELP,
    "confirm": Intent.CONFIRM,
    "deny": Intent.DENY,
    "repeat_information": Intent.REPEAT,
    "unclear_request": Intent.UNKNOWN,
    # the caller is answering a question we asked
    "provide_patient_name": Intent.PROVIDE_INFO,
    "provide_patient_phone": Intent.PROVIDE_INFO,
    "provide_patient_age": Intent.PROVIDE_INFO,
    "provide_patient_gender": Intent.PROVIDE_INFO,
    # safety-critical
    "emergency": Intent.EMERGENCY,
}

# Fine-grained sub-topic for information answers. The canonical intent is
# coarse (DOCTOR_INFORMATION); this says which field the caller asked about.
INFO_TOPIC = {
    "doctor_fee": "fee",
    "doctor_qualifications": "qualification",
    "doctor_specialization": "specialization",
    "doctor_information": "general",
    "find_doctor": "list",
    "clinic_location": "address",
    "clinic_timing": "timing",
    "clinic_closed": "closed",
}

# Only these may interrupt an in-progress workflow.
WORKFLOW_INTENTS = {
    Intent.BOOK_APPOINTMENT,
    Intent.CANCEL_APPOINTMENT,
    Intent.RESCHEDULE_APPOINTMENT,
    Intent.CHECK_APPOINTMENT,
    Intent.EMERGENCY,
}

INFORMATION_INTENTS = {
    Intent.DOCTOR_INFORMATION,
    Intent.CLINIC_INFORMATION,
    Intent.DOCTOR_AVAILABILITY,
}

# --------------------------------------------------------------------------
# Slot requirements per workflow (asked in this order)
# --------------------------------------------------------------------------
REQUIRED_SLOTS = {
    Intent.BOOK_APPOINTMENT: ["doctor_name", "date", "time"],
    Intent.CANCEL_APPOINTMENT: ["appointment_id"],
    Intent.RESCHEDULE_APPOINTMENT: ["appointment_id", "date", "time"],
    Intent.CHECK_APPOINTMENT: [],          # resolved from patient_id if known
    Intent.DOCTOR_AVAILABILITY: ["doctor_name", "date"],
}

OPTIONAL_SLOTS = {
    Intent.BOOK_APPOINTMENT: ["patient_name", "patient_phone"],
}

# --------------------------------------------------------------------------
# Dialog actions returned to the caller (spec section 32)
# --------------------------------------------------------------------------
class DialogState:
    """
    Where the conversation currently is.

    Reported to the caller so the voice layer (and a human reading the logs)
    can see the flow, and used to interpret low-confidence utterances.
    """
    IDLE = "IDLE"
    DOCTOR_INFORMATION = "DOCTOR_INFORMATION"
    APPOINTMENT_BOOKING = "APPOINTMENT_BOOKING"
    APPOINTMENT_CONFIRMATION = "APPOINTMENT_CONFIRMATION"
    APPOINTMENT_CANCEL = "APPOINTMENT_CANCEL"
    APPOINTMENT_RESCHEDULE = "APPOINTMENT_RESCHEDULE"
    APPOINTMENT_STATUS = "APPOINTMENT_STATUS"
    DOCTOR_UNAVAILABLE = "DOCTOR_UNAVAILABLE"
    CLARIFICATION = "CLARIFICATION"


# Which dialog state each canonical intent puts us in.
INTENT_TO_STATE = {
    Intent.BOOK_APPOINTMENT: DialogState.APPOINTMENT_BOOKING,
    Intent.CANCEL_APPOINTMENT: DialogState.APPOINTMENT_CANCEL,
    Intent.RESCHEDULE_APPOINTMENT: DialogState.APPOINTMENT_RESCHEDULE,
    Intent.CHECK_APPOINTMENT: DialogState.APPOINTMENT_STATUS,
    Intent.DOCTOR_AVAILABILITY: DialogState.DOCTOR_INFORMATION,
    Intent.DOCTOR_INFORMATION: DialogState.DOCTOR_INFORMATION,
    Intent.CLINIC_INFORMATION: DialogState.DOCTOR_INFORMATION,
    Intent.UNKNOWN: DialogState.CLARIFICATION,
}


class Action:
    ASK_FOR_DOCTOR = "ask_for_doctor"
    ASK_FOR_DATE = "ask_for_date"
    ASK_FOR_TIME = "ask_for_time"
    ASK_FOR_APPOINTMENT_ID = "ask_for_appointment_id"
    ASK_FOR_PATIENT_INFO = "ask_for_patient_info"
    CHECK_AVAILABILITY = "check_availability"
    ASK_CONFIRMATION = "ask_confirmation"
    CREATE_APPOINTMENT = "create_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    CHECK_APPOINTMENT_STATUS = "check_appointment_status"
    PROVIDE_DOCTOR_INFORMATION = "provide_doctor_information"
    PROVIDE_DOCTOR_FEE = "provide_doctor_fee"
    PROVIDE_DOCTOR_QUALIFICATION = "provide_doctor_qualification"
    PROVIDE_DOCTOR_AVAILABILITY = "provide_doctor_availability"
    PROVIDE_CLINIC_INFORMATION = "provide_clinic_information"
    OFFER_ALTERNATIVES = "offer_alternatives"
    GREET = "greet"
    INFORM = "inform"
    CLARIFY = "clarify"
    # "Did you mean X?" for a medium-confidence guess. Distinct from
    # CLARIFY so the response layer does not overwrite the specific
    # question with the generic one.
    CONFIRM_INTENT = "confirm_intent"
    ABORT = "abort"
    ESCALATE = "escalate"
    ERROR = "error"
    NONE = "none"
    END_CONVERSATION = "end_conversation"


# Which action to emit when a given slot is missing.
SLOT_TO_ACTION = {
    "doctor_name": Action.ASK_FOR_DOCTOR,
    "date": Action.ASK_FOR_DATE,
    "time": Action.ASK_FOR_TIME,
    "appointment_id": Action.ASK_FOR_APPOINTMENT_ID,
    "patient_name": Action.ASK_FOR_PATIENT_INFO,
    "patient_phone": Action.ASK_FOR_PATIENT_INFO,
}

# --------------------------------------------------------------------------
# Appointment statuses (spec section 16)
# --------------------------------------------------------------------------
class Status:
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    RESCHEDULED = "rescheduled"
    # Set when a doctor blocks a whole day from the dashboard.
    CANCELLED_BY_DOCTOR = "cancelled_by_doctor"


# --------------------------------------------------------------------------
# Time parsing heuristic
# --------------------------------------------------------------------------
# A patient saying "4 o'clock" about a clinic means 16:00, not 04:00.
BARE_HOUR_PM_RANGE = (1, 8)

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
MASK_SENSITIVE_IN_LOGS = True


def use_utf8_stdout() -> None:
    """Windows consoles are cp1252; Urdu output needs UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
