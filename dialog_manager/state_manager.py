"""
Conversation state and session management.

This is what makes the system a *dialog* manager rather than a stateless
classifier wrapper: everything learned about the caller so far lives here, and
every decision is made by inspecting it.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from config import MAX_HISTORY_TURNS, SESSION_TIMEOUT_MINUTES, DialogState


class ConversationState:
    """One caller's session."""

    # Slots the dialog tracks. Kept flat so `slots` serialises straight to JSON.
    SLOT_NAMES = ("patient_id", "doctor_id", "doctor_name", "clinic_id",
                  "date", "time", "appointment_id",
                  "patient_name", "patient_phone")

    def __init__(self, session_id: str | None = None,
                 patient_id: str | None = None):
        self.session_id = session_id or str(uuid.uuid4())

        self.intent: str | None = None
        self.previous_intent: str | None = None
        self.last_confidence: float = 0.0
        self.last_raw_intent: str | None = None
        # Which specific field an information question was about (fee, address...)
        self.info_topic: str | None = None
        # Where the conversation currently is.
        self.dialog_state: str = DialogState.IDLE
        # The caller's language, held for the whole call. A one-word "yes"
        # carries no language signal and must not flip a Roman-Urdu
        # conversation into English.
        self.language: str = "english"
        self.language_locked: bool = False
        # How many times each question has been asked, so the wording can be
        # rotated. Repeating a sentence verbatim is what makes a system sound
        # like an IVR menu.
        self.phrasing_counter: dict[str, int] = {}
        # Set when the caller has just overwritten something they told us
        # earlier, so the reply can open with "koi baat nahi".
        self.last_turn_corrected: bool = False
        self.corrected_slots: list[str] = []

        # An intent we could not answer yet because a slot was missing.
        # "Doctor ki fee kitni hai?" with no doctor selected parks
        # pending_intent=doctor_information / pending_topic="fee", asks which
        # doctor, and answers the ORIGINAL question once the name arrives -
        # rather than replying with generic doctor information.
        self.pending_intent: str | None = None
        self.pending_topic: str | None = None
        # A medium-confidence guess we asked the caller to confirm.
        self.awaiting_intent_check: str | None = None

        self.slots: dict[str, Any] = {name: None for name in self.SLOT_NAMES}
        self.slots["patient_id"] = patient_id

        # The slot we last asked about - the key to interpreting short replies.
        self.current_slot: str | None = None
        self.confirmation_required: bool = False
        self.pending_action: str | None = None   # what "yes" would execute
        self.offered_slots: list[str] = []       # alternatives we read out

        self.last_action: str | None = None
        self.last_user_message: str | None = None
        self.last_system_message: str | None = None
        self.conversation_history: list[dict[str, Any]] = []

        self.clarification_attempts: int = 0
        self.created_at: float = time.time()
        self.last_active_at: float = time.time()
        self.ended: bool = False

    # ------------------------------------------------------------------
    def next_variant(self, key: str) -> int:
        """Rotation index for a phrasing, advanced on every use."""
        index = self.phrasing_counter.get(key, 0)
        self.phrasing_counter[key] = index + 1
        return index

    def get_slot(self, name: str) -> Any:
        return self.slots.get(name)

    def set_slot(self, name: str, value: Any) -> None:
        self.slots[name] = value

    def clear_task_slots(self) -> None:
        """
        Drop the current task's draft but keep who the caller is.

        Used when an intent switches or a workflow finishes, so the next
        request starts clean without re-asking for the patient's identity.
        """
        keep = {"patient_id", "patient_name", "patient_phone"}
        for name in self.slots:
            if name not in keep:
                self.slots[name] = None
        self.current_slot = None
        self.confirmation_required = False
        self.pending_action = None
        self.pending_intent = None
        self.pending_topic = None
        self.awaiting_intent_check = None
        self.offered_slots = []

    # ------------------------------------------------------------------
    def add_turn(self, speaker: str, text: str, **meta: Any) -> None:
        self.conversation_history.append(
            {"speaker": speaker, "text": text, "timestamp": time.time(), **meta})
        if len(self.conversation_history) > MAX_HISTORY_TURNS:
            self.conversation_history = self.conversation_history[-MAX_HISTORY_TURNS:]
        self.last_active_at = time.time()

    # ------------------------------------------------------------------
    def to_dict(self, include_history: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "intent": self.intent,
            "previous_intent": self.previous_intent,
            "slots": dict(self.slots),
            "state": self.dialog_state,
            "language": self.language,
            "pending_intent": self.pending_intent,
            "pending_topic": self.pending_topic,
            "current_slot": self.current_slot,
            "confirmation_required": self.confirmation_required,
            "pending_action": self.pending_action,
            "offered_slots": list(self.offered_slots),
            "last_action": self.last_action,
            "clarification_attempts": self.clarification_attempts,
            "ended": self.ended,
        }
        if include_history:
            data["conversation_history"] = list(self.conversation_history)
        return data

    def __repr__(self) -> str:                      # pragma: no cover
        return (f"<ConversationState {self.session_id} intent={self.intent} "
                f"asking={self.current_slot} confirm={self.confirmation_required}>")


class SessionManager:
    """
    Thread-safe registry of active conversations.

    A voice system serves many callers at once, so a single global state would
    let patient A's doctor choice leak into patient B's booking.
    """

    def __init__(self, timeout_minutes: int = SESSION_TIMEOUT_MINUTES):
        self._sessions: dict[str, ConversationState] = {}
        self._lock = threading.Lock()
        self.timeout_seconds = timeout_minutes * 60

    def get_or_create(self, session_id: str,
                      patient_id: str | None = None) -> ConversationState:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is not None and self._is_expired(state):
                # An abandoned call must not resume hours later mid-booking.
                del self._sessions[session_id]
                state = None
            if state is None:
                state = ConversationState(session_id, patient_id)
                self._sessions[session_id] = state
            elif patient_id and not state.get_slot("patient_id"):
                state.set_slot("patient_id", patient_id)
            return state

    def get(self, session_id: str) -> ConversationState | None:
        with self._lock:
            return self._sessions.get(session_id)

    def reset(self, session_id: str,
              patient_id: str | None = None) -> ConversationState:
        with self._lock:
            self._sessions.pop(session_id, None)
            state = ConversationState(session_id, patient_id)
            self._sessions[session_id] = state
            return state

    def end(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def _is_expired(self, state: ConversationState) -> bool:
        return (time.time() - state.last_active_at) > self.timeout_seconds

    def cleanup_expired(self) -> int:
        with self._lock:
            stale = [sid for sid, st in self._sessions.items()
                     if self._is_expired(st)]
            for sid in stale:
                del self._sessions[sid]
            return len(stale)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)
