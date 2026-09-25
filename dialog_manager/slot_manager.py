"""
Slot manager - tracks which information a workflow still needs.

Slot filling is what turns a one-shot classifier into a conversation: the
Dialog Manager keeps asking until every required slot is filled, then acts.
"""
from __future__ import annotations

from typing import Any

from config import OPTIONAL_SLOTS, REQUIRED_SLOTS, SLOT_TO_ACTION, Action
from dialog_manager.state_manager import ConversationState


class SlotManager:
    """Reads and writes slots on a ConversationState."""

    SLOT_LABELS = {
        "doctor_name": "doctor",
        "date": "date",
        "time": "time",
        "appointment_id": "appointment ID",
        "patient_name": "name",
        "patient_phone": "phone number",
    }

    # ------------------------------------------------------------------
    @staticmethod
    def required_slots(intent: str | None) -> list[str]:
        return list(REQUIRED_SLOTS.get(intent, []))

    @staticmethod
    def optional_slots(intent: str | None) -> list[str]:
        return list(OPTIONAL_SLOTS.get(intent, []))

    # ------------------------------------------------------------------
    def update_slots(self, state: ConversationState,
                     entities: dict[str, Any]) -> list[str]:
        """
        Copy recognised entities into the state.

        Returns the slots actually written, which the Dialog Manager uses to
        decide whether this turn made any progress.
        """
        written: list[str] = []
        corrected: list[str] = []
        for slot, value in entities.items():
            if value in (None, ""):
                continue
            if slot not in state.slots:
                continue                       # not a slot we track
            previous = state.get_slot(slot)
            if previous != value:
                # Overwriting a value the caller gave earlier is a CORRECTION,
                # which deserves "koi baat nahi" rather than silence.
                if previous:
                    corrected.append(slot)
                state.set_slot(slot, value)
                written.append(slot)
        state.corrected_slots = corrected
        state.last_turn_corrected = bool(corrected)
        return written

    # ------------------------------------------------------------------
    def get_missing_slots(self, state: ConversationState,
                          intent: str | None = None) -> list[str]:
        intent = intent or state.intent
        return [slot for slot in self.required_slots(intent)
                if not state.get_slot(slot)]

    def is_complete(self, state: ConversationState,
                    intent: str | None = None) -> bool:
        return not self.get_missing_slots(state, intent)

    def next_missing_slot(self, state: ConversationState,
                          intent: str | None = None) -> str | None:
        """
        The single slot to ask about next.

        One question at a time matters on a phone call - a caller cannot hold
        three questions in their head.
        """
        missing = self.get_missing_slots(state, intent)
        return missing[0] if missing else None

    # ------------------------------------------------------------------
    @staticmethod
    def action_for_slot(slot: str) -> str:
        return SLOT_TO_ACTION.get(slot, Action.ASK_FOR_PATIENT_INFO)

    def label(self, slot: str) -> str:
        return self.SLOT_LABELS.get(slot, slot.replace("_", " "))

    def clear_slots(self, state: ConversationState) -> None:
        state.clear_task_slots()

    def filled_slots(self, state: ConversationState,
                     intent: str | None = None) -> dict[str, Any]:
        intent = intent or state.intent
        names = self.required_slots(intent) + self.optional_slots(intent)
        return {slot: state.get_slot(slot) for slot in names
                if state.get_slot(slot)}
