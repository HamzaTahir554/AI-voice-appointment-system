"""
Conversation service - `conversation_sessions` and `conversation_messages`.

Stores a transcript of every call for auditing and for later analysis of where
the intent model or the dialog policy went wrong.

Privacy: phone numbers are masked before they are written, and logging failures
are swallowed - a transcript write must never break a live call.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime

from config import Collections, MASK_SENSITIVE_IN_LOGS, TIMEZONE
from firebase.firebase_config import DatabaseError, get_repository
from firebase.result import ServiceResult

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"\b(\+?92|0)?3\d{2}[\s-]?\d{7}\b")


def mask_phone(text: str) -> str:
    """03001234567 -> 0300*****67, so transcripts stay useful but not sensitive."""
    def _mask(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) < 6:
            return match.group(0)
        return digits[:4] + "*" * (len(digits) - 6) + digits[-2:]
    return _PHONE_RE.sub(_mask, text or "")


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat()


class ConversationService:
    def __init__(self, repository=None):
        self.repo = repository or get_repository()

    # ------------------------------------------------------------------
    def start_session(self, session_id: str,
                      patient_id: str | None = None) -> ServiceResult:
        record = {
            "session_id": session_id,
            "patient_id": patient_id,
            "started_at": _now(),
            "ended_at": None,
            "status": "active",
        }
        try:
            saved = self.repo.set(Collections.SESSIONS, session_id, record)
        except DatabaseError as exc:
            logger.error("start_session failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def end_session(self, session_id: str,
                    status: str = "completed") -> ServiceResult:
        try:
            updated = self.repo.update(Collections.SESSIONS, session_id,
                                       {"ended_at": _now(), "status": status})
        except DatabaseError as exc:
            logger.warning("end_session failed: %s", exc)
            return ServiceResult.failure("SESSION_NOT_FOUND", str(exc))
        return ServiceResult.success(updated)

    def get_session(self, session_id: str) -> ServiceResult:
        try:
            doc = self.repo.get(Collections.SESSIONS, session_id)
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if doc is None:
            return ServiceResult.failure("SESSION_NOT_FOUND", session_id)
        return ServiceResult.success(doc)

    # ------------------------------------------------------------------
    def log_message(self, session_id: str, speaker: str, text: str,
                    intent: str | None = None, confidence: float | None = None,
                    action: str | None = None,
                    entities: dict | None = None) -> ServiceResult:
        """
        Append one utterance to the transcript.

        Never raises: if the transcript cannot be written we log a warning and
        the conversation carries on.
        """
        safe_text = mask_phone(text) if MASK_SENSITIVE_IN_LOGS else text
        message_id = f"{session_id}_{uuid.uuid4().hex[:8]}"
        record = {
            "message_id": message_id,
            "session_id": session_id,
            "speaker": speaker,                 # "patient" | "agent"
            "text": safe_text,
            "intent": intent,
            "confidence": confidence,
            "action": action,
            "entities": entities or {},
            "timestamp": _now(),
        }
        try:
            saved = self.repo.set(Collections.MESSAGES, message_id, record)
        except DatabaseError as exc:
            logger.warning("could not log message: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def get_messages(self, session_id: str) -> ServiceResult:
        try:
            docs = self.repo.query(Collections.MESSAGES,
                                   [("session_id", "==", session_id)])
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        docs.sort(key=lambda d: d.get("timestamp", ""))
        return ServiceResult.success(docs)
