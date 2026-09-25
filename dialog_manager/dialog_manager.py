"""
Dialog Manager - the brain of the conversation.

mBERT answers "what does the user want?"; this module answers "what should the
system do next?". It owns conversation state, slot filling, validation,
confirmation and the calls into Firestore.

Nothing here invents appointment facts. Availability, fees, addresses and
appointment IDs all come back from the service layer.
"""
from __future__ import annotations

import logging
from typing import Any

from config import (
    INTENT_SWITCH_THRESHOLD,
    INTENT_TO_STATE,
    MAX_CLARIFICATION_ATTEMPTS,
    WORKFLOW_INTENTS,
    Action,
    DialogState,
    Intent,
)
from dialog_manager.intent_router import IntentResult, IntentRouter
from dialog_manager.responses import ResponseGenerator, format_date, format_time
from dialog_manager.slot_manager import SlotManager
from dialog_manager.state_manager import ConversationState, SessionManager
from dialog_manager.validators import (
    EntityExtractor,
    detect_yes_no,
    is_past_date,
    is_valid_date,
    is_valid_time,
)
from appointment_backend.appointment_service import AppointmentBackend
from firebase.appointment_service import AppointmentService
from firebase.clinic_service import ClinicService
from firebase.conversation_service import ConversationService
from firebase.doctor_service import DoctorService
from firebase.firebase_config import get_repository, init_repository
from firebase.patient_service import PatientService

logger = logging.getLogger(__name__)

# Intents that describe *how* the caller answered rather than a new task.
_CONTEXTUAL_INTENTS = {Intent.CONFIRM, Intent.DENY, Intent.PROVIDE_INFO,
                       Intent.REPEAT, Intent.UNKNOWN}

# mBERT labels that edit ONE detail of an appointment. They map to reschedule,
# but inside a booking draft there is no appointment yet - they edit the draft.
_DRAFT_EDIT_LABELS = frozenset({"change_doctor", "change_date", "change_time"})

# Replies that fully answer an information question.
_ANSWER_ACTIONS = frozenset({
    Action.PROVIDE_DOCTOR_FEE, Action.PROVIDE_DOCTOR_QUALIFICATION,
    Action.PROVIDE_DOCTOR_INFORMATION, Action.PROVIDE_CLINIC_INFORMATION,
})


class DialogManager:
    """Orchestrates one turn of conversation from text to response."""

    def __init__(self, intent_router: IntentRouter | None = None,
                 repository=None, deterministic_responses: bool = False):
        self.repo = repository or get_repository() or init_repository()

        self.doctors = DoctorService(self.repo)
        self.clinics = ClinicService(self.repo)
        self.patients = PatientService(self.repo)
        self.appointments = AppointmentService(self.repo)
        # Every write goes through the Appointment Backend, which owns
        # the business rules. The Dialog Manager decides *what* to try;
        # the backend decides whether it is actually allowed.
        self.backend = AppointmentBackend(self.repo)
        self.conversations = ConversationService(self.repo)

        self.router = intent_router or IntentRouter()
        self.extractor = EntityExtractor(doctor_service=self.doctors)
        self.slots = SlotManager()
        self.responses = ResponseGenerator(deterministic=deterministic_responses)
        self.sessions = SessionManager()

    # ==================================================================
    # Entry point
    # ==================================================================
    def process_message(self, session_id: str, text: str,
                        patient_id: str | None = None) -> dict[str, Any]:
        """
        Handle one caller utterance and return the structured dialog result.

        Pipeline: state -> mBERT -> confidence -> entities -> slots ->
        action -> Firestore -> response -> logs.
        """
        if not text or not text.strip():
            return self._empty_input(session_id, patient_id)

        state = self.sessions.get_or_create(session_id, patient_id)
        is_new = not state.conversation_history
        state.last_user_message = text
        state.add_turn("patient", text)

        if is_new:
            self.conversations.start_session(session_id, patient_id)

        # --- perception -------------------------------------------------
        intent_result = self.router.route(text)
        entities = self.extractor.extract_entities(text)
        yes_no = detect_yes_no(text)

        state.last_confidence = intent_result.confidence
        state.last_raw_intent = intent_result.raw_intent

        logger.info(
            "session=%s intent=%s raw=%s conf=%.3f entities=%s awaiting=%s",
            session_id, intent_result.intent, intent_result.raw_intent,
            intent_result.confidence, sorted(entities), state.current_slot)

        self.conversations.log_message(
            session_id, "patient", text,
            intent=intent_result.raw_intent,
            confidence=intent_result.confidence,
            entities=entities)

        # --- decision ---------------------------------------------------
        try:
            result = self._decide(state, text, intent_result, entities, yes_no)
        except Exception as exc:                       # pragma: no cover
            # A bug in one handler must not drop the call.
            logger.exception("dialog failure in session %s: %s", session_id, exc)
            result = self._reply(
                state, self.responses.error("BACKEND_UNAVAILABLE"),
                Action.ERROR, intent_result)

        # --- bookkeeping -------------------------------------------------
        state.last_system_message = result["response"]
        state.last_action = result["action"]
        state.last_template = result.get("template")
        state.add_turn("agent", result["response"], action=result["action"])
        self.conversations.log_message(
            session_id, "agent", result["response"],
            intent=result["intent"], action=result["action"])

        if result["action"] == Action.END_CONVERSATION:
            state.ended = True
            self.conversations.end_session(session_id)
        return result

    # ==================================================================
    # Intent selection
    # ==================================================================
    def _decide(self, state: ConversationState, text: str,
                intent_result: IntentResult, entities: dict,
                yes_no: str | None) -> dict[str, Any]:
        """Route the turn, honouring confirmation and context first."""

        # 1. A pending confirmation outranks everything: the caller is
        #    answering a yes/no question we just asked.
        if state.confirmation_required:
            return self._handle_confirmation(state, text, intent_result,
                                             entities, yes_no)

        # 2. Choose the effective intent (context-aware, with switching).
        interrupted = self._side_question_task(state, intent_result, entities)
        effective = self._effective_intent(state, intent_result, entities)

        # 2b. A MEDIUM-confidence guess that would start a brand-new workflow,
        #     with nothing in the utterance to corroborate it, is checked with
        #     one short question rather than acted on. Acting on a 0.65 guess
        #     is how a caller ends up with a cancelled appointment they never
        #     asked to cancel.
        if self._needs_intent_check(state, intent_result, effective, entities):
            state.pending_intent = effective
            state.awaiting_intent_check = effective
            return self._reply(state,
                               self._intent_check_question(state, effective),
                               Action.CONFIRM_INTENT, intent_result)

        # The caller answered that check.
        if getattr(state, "awaiting_intent_check", None):
            guessed = state.awaiting_intent_check
            state.awaiting_intent_check = None
            state.pending_intent = None
            if yes_no == "yes":
                effective = guessed
            elif yes_no == "no":
                state.intent = None
                self.slots.clear_slots(state)
                return self._reply(state, self.responses.clarify(),
                                   Action.CLARIFY, intent_result)

        # 3. An intent switch abandons the previous task's draft slots.
        if (effective != state.intent and effective in WORKFLOW_INTENTS
                and state.intent is not None):
            logger.info("intent switch %s -> %s", state.intent, effective)
            state.previous_intent = state.intent
            self.slots.clear_slots(state)

        if effective not in _CONTEXTUAL_INTENTS:
            state.intent = effective
        # A resumed question keeps its ORIGINAL topic: answering "Dr Ahmed"
        # classifies as generic doctor_information, which must not overwrite
        # the "fee" the caller actually asked about.
        if effective == state.pending_intent and state.pending_topic:
            state.info_topic = state.pending_topic
        elif intent_result.topic:
            state.info_topic = intent_result.topic

        # Keep the named dialog state in step with the intent.
        if effective in INTENT_TO_STATE:
            state.dialog_state = INTENT_TO_STATE[effective]

        # 4. Store whatever the caller just told us. A side question about a
        #    DIFFERENT doctor must not change the doctor being booked.
        kept_doctor = ({k: state.get_slot(k) for k in ("doctor_id", "doctor_name", "clinic_id")}
                       if interrupted is not None and state.get_slot("doctor_id") else None)
        self.slots.update_slots(state, entities)

        # 4b. Asked for an appointment ID, the caller said "nahi". There is no
        #     ID to give, so asking again loops forever (observed live).
        if (yes_no == "no" and state.current_slot == "appointment_id"
                and not state.get_slot("appointment_id")
                and effective in (Intent.CANCEL_APPOINTMENT,
                                  Intent.RESCHEDULE_APPOINTMENT,
                                  Intent.CHECK_APPOINTMENT)):
            return self._no_appointment_id(state, intent_result)

        # 5. Dispatch.
        handlers = {
            Intent.GREETING: self._handle_greeting,
            Intent.GOODBYE: self._handle_goodbye,
            Intent.THANKS: self._handle_thanks,
            Intent.HELP: self._handle_help,
            Intent.EMERGENCY: self._handle_emergency,
            Intent.BOOK_APPOINTMENT: self._handle_book,
            Intent.CANCEL_APPOINTMENT: self._handle_cancel,
            Intent.RESCHEDULE_APPOINTMENT: self._handle_reschedule,
            Intent.CHECK_APPOINTMENT: self._handle_check,
            Intent.DOCTOR_AVAILABILITY: self._handle_availability,
            Intent.DOCTOR_INFORMATION: self._handle_doctor_info,
            Intent.CLINIC_INFORMATION: self._handle_clinic_info,
            Intent.REPEAT: self._handle_repeat,
        }
        handler = handlers.get(effective)
        if handler is None:
            return self._handle_unknown(state, intent_result, entities)
        reply = handler(state, intent_result, entities)
        if interrupted is not None and effective != interrupted:
            if kept_doctor:
                for key, value in kept_doctor.items():
                    state.set_slot(key, value)
            if reply.get("action") in _ANSWER_ACTIONS:
                return self._resume_task(state, reply, interrupted, intent_result)
            state.intent = interrupted
        return reply

    def _side_question_task(self, state: ConversationState,
                            intent_result: IntentResult,
                            entities: dict) -> str | None:
        """
        The task a confident information question interrupts, or None.

        Only a SPECIFIC question counts (fee, qualification, address ...). A
        bare doctor name classifies as doctor_information with topic
        "general" and is a slot answer, not a question. A doctor question also
        needs a known doctor, so it can be answered at once and the task
        resumed in the same reply.
        """
        if state.intent not in WORKFLOW_INTENTS or state.intent == Intent.EMERGENCY:
            return None
        if intent_result.intent not in (Intent.DOCTOR_INFORMATION,
                                        Intent.CLINIC_INFORMATION):
            return None
        if intent_result.band != "high" or intent_result.topic in (None, "general", "list"):
            return None
        if (intent_result.intent == Intent.DOCTOR_INFORMATION
                and not (entities.get("doctor_id") or state.get_slot("doctor_id"))):
            return None
        return state.intent

    def _resume_task(self, state: ConversationState, answer: dict, task: str,
                     intent_result: IntentResult) -> dict[str, Any]:
        """The side question is answered; pick the interrupted task back up."""
        state.intent = task
        if task in INTENT_TO_STATE:
            state.dialog_state = INTENT_TO_STATE[task]
        missing = self.slots.get_missing_slots(state, task)
        if not missing:
            return answer
        question = self._ask_for(state, missing[0], intent_result)
        answer.update({
            "response": f"{answer['response']} {question['response']}",
            "resume_action": question["action"],
            "slot": question["slot"],
            "slots": question["slots"],
            "session_state": question["session_state"],
            "state": question["state"],
            "intent": task,
        })
        return answer

    def _needs_intent_check(self, state: ConversationState,
                            intent_result: IntentResult, effective: str,
                            entities: dict) -> bool:
        """
        Should we confirm the intent before acting on it?

        Only when ALL of these hold: the model is in the middle band, the guess
        would start a new workflow, nothing else is already in progress, and
        the utterance carried no entity to corroborate it.
        """
        if getattr(state, "awaiting_intent_check", None):
            return False                       # already asking
        if intent_result.band != "medium":
            return False
        if effective not in WORKFLOW_INTENTS or effective == Intent.EMERGENCY:
            return False                       # never delay an emergency
        if state.intent is not None:
            return False                       # a task is already running
        # A doctor, date, time or appointment ID is corroboration enough.
        return not any(entities.get(k) for k in
                       ("doctor_id", "date", "time", "appointment_id"))

    def _intent_check_question(self, state: ConversationState,
                               effective: str) -> str:
        from ollama_judge.language import intent_phrase, render

        language = getattr(state, "language", "english")
        phrase = intent_phrase(effective, language)
        if not phrase:
            return self.responses.clarify()
        question = render("confirm_intent", language, action=phrase)
        return question or self.responses.clarify()

    def _effective_intent(self, state: ConversationState,
                          intent_result: IntentResult,
                          entities: dict) -> str:
        """
        Decide which intent this turn belongs to.

        The hard case is a short contextual reply. When we asked "which day?"
        and the caller says "kal", mBERT may return anything at all - but the
        conversation state says we are mid-booking and a date was extracted,
        so the turn continues the booking rather than starting something new.
        """
        predicted = intent_result.intent

        # The emergency safety net fired: nothing else is considered.
        if getattr(intent_result, "safety_override", False):
            return Intent.EMERGENCY

        # Inside a booking DRAFT, "change the doctor / date / time" edits the
        # draft. Treating it as a reschedule abandons the booking and asks for
        # an appointment ID the caller does not have. Observed live: "which
        # doctor?" -> "dr ahmad" -> change_doctor 0.87 -> "Please provide your
        # appointment ID", repeated until the caller gave up.
        if (state.intent == Intent.BOOK_APPOINTMENT
                and intent_result.raw_intent in _DRAFT_EDIT_LABELS):
            return state.intent

        # A specific QUESTION in the middle of a task ("Dr Ahmed ki fee kitni
        # hai?" while booking) is answered first; _decide then resumes the
        # task. Before this, the doctor name in the question counted as a
        # booking slot and the question was silently ignored.
        if self._side_question_task(state, intent_result, entities) is not None:
            return predicted

        # A confident workflow intent may interrupt anything.
        if (predicted in WORKFLOW_INTENTS
                and intent_result.confidence >= INTENT_SWITCH_THRESHOLD):
            return predicted

        # A parked question wins as soon as its missing slot arrives.
        # "Which doctor?" -> "Dr Ahmed" must answer the ORIGINAL question
        # (the fee), not whatever the classifier makes of "Dr Ahmed".
        if state.pending_intent and state.current_slot:
            supplied = entities.get(state.current_slot) or (
                state.current_slot == "doctor_name" and entities.get("doctor_id"))
            if supplied:
                return state.pending_intent

        # Mid-task: did the caller supply the slot we asked for?
        if state.intent and state.current_slot:
            if entities.get(state.current_slot) or (
                    state.current_slot == "doctor_name" and entities.get("doctor_id")):
                return state.intent
            # Any relevant slot at all still counts as continuing the task.
            if any(slot in entities
                   for slot in self.slots.required_slots(state.intent)):
                return state.intent

        # A known, non-contextual intent starts or replaces the task.
        if intent_result.is_known and predicted not in _CONTEXTUAL_INTENTS:
            return predicted

        # Otherwise keep the task running if one is open.
        if state.intent and predicted in _CONTEXTUAL_INTENTS:
            return state.intent
        return predicted

    # ==================================================================
    # Confirmation
    # ==================================================================
    def _handle_confirmation(self, state: ConversationState, text: str,
                             intent_result: IntentResult, entities: dict,
                             yes_no: str | None) -> dict[str, Any]:
        # A confident new workflow intent cancels the pending confirmation.
        if (intent_result.intent in WORKFLOW_INTENTS
                and (intent_result.confidence >= INTENT_SWITCH_THRESHOLD
                     or getattr(intent_result, "safety_override", False))
                and intent_result.intent != state.intent):
            state.confirmation_required = False
            state.pending_action = None
            return self._decide(state, text, intent_result, entities, None)

        if yes_no == "yes":
            return self._execute_pending(state, intent_result)

        if yes_no == "no":
            # Spec section 26: never perform the action after a refusal.
            state.confirmation_required = False
            state.pending_action = None
            self.slots.clear_slots(state)
            state.intent = None
            return self._reply(state, self.responses.aborted(),
                               Action.ABORT, intent_result,
                               template=("aborted", {}))

        # Not a yes/no: perhaps they picked one of the times we offered,
        # or corrected a detail. Absorb it and re-confirm.
        written = self.slots.update_slots(state, entities)
        if written:
            state.confirmation_required = False
            return self._decide(state, text, intent_result, entities, None)

        return self._reply(state, "Sorry, should I go ahead? Please say yes or no.",
                           Action.ASK_CONFIRMATION, intent_result,
                           template=("say_yes_or_no", {}))

    def _execute_pending(self, state: ConversationState,
                         intent_result: IntentResult) -> dict[str, Any]:
        """Carry out whatever the caller just agreed to."""
        pending = state.pending_action
        state.confirmation_required = False
        state.pending_action = None

        if pending == Action.CREATE_APPOINTMENT:
            return self._commit_booking(state, intent_result)
        if pending == Action.CANCEL_APPOINTMENT:
            return self._commit_cancellation(state, intent_result)
        if pending == Action.RESCHEDULE_APPOINTMENT:
            return self._commit_reschedule(state, intent_result)

        return self._reply(state, self.responses.clarify(),
                           Action.CLARIFY, intent_result)

    # ==================================================================
    # Booking workflow
    # ==================================================================
    def _handle_book(self, state: ConversationState,
                     intent_result: IntentResult,
                     entities: dict) -> dict[str, Any]:
        # If we offered alternatives and they named one, take it.
        self._absorb_offered_slot(state, entities)

        missing = self.slots.get_missing_slots(state, Intent.BOOK_APPOINTMENT)
        if missing:
            return self._ask_for(state, missing[0], intent_result)

        doctor_id = state.get_slot("doctor_id")
        doctor_name = state.get_slot("doctor_name")
        iso_date = state.get_slot("date")
        time_str = state.get_slot("time")

        invalid = self._validate_date_time(state, iso_date, time_str, intent_result)
        if invalid:
            return invalid

        # Check the doctor really is free before promising anything.
        availability = self.appointments.check_availability(
            doctor_id, iso_date, time_str)
        if not availability.ok:
            return self._availability_problem(state, availability, iso_date,
                                              intent_result, doctor_name)
        if not availability.data:
            return self._offer_alternatives(state, doctor_id, doctor_name,
                                            iso_date, time_str, intent_result)

        # Slot is free: ask the caller to confirm before writing anything.
        state.confirmation_required = True
        state.dialog_state = DialogState.APPOINTMENT_CONFIRMATION
        state.pending_action = Action.CREATE_APPOINTMENT
        state.current_slot = None
        return self._reply(
            state,
            self.responses.confirm_booking(doctor_name, iso_date, time_str),
            Action.ASK_CONFIRMATION, intent_result,
            template=("confirm_booking", {"doctor": doctor_name, "doctor_id": doctor_id,
                                          "date": iso_date, "time": time_str}))

    def _commit_booking(self, state: ConversationState,
                        intent_result: IntentResult) -> dict[str, Any]:
        doctor_id = state.get_slot("doctor_id")
        doctor_name = state.get_slot("doctor_name")
        iso_date = state.get_slot("date")
        time_str = state.get_slot("time")

        patient_id = self._resolve_patient(state)
        result = self.backend.book_appointment(
            patient_id=patient_id, doctor_id=doctor_id, date=iso_date,
            time=time_str, clinic_id=state.get_slot("clinic_id"),
            validate_patient=False)   # the caller was just registered above

        if not result.success:
            if result.error_code == "slot_unavailable":
                # Lost the race: offer what is still free.
                return self._offer_alternatives(state, doctor_id, doctor_name,
                                                iso_date, time_str, intent_result)
            return self._backend_error(state, result, intent_result)

        message = self.responses.booked(doctor_name, iso_date, time_str,
                                        result.appointment_id)
        self.slots.clear_slots(state)
        state.intent = None
        return self._reply(state, message, Action.CREATE_APPOINTMENT,
                           intent_result, backend_result=result,
                           extra={"appointment": result.data.get("appointment")})

    # ==================================================================
    # Cancellation workflow
    # ==================================================================
    def _handle_cancel(self, state: ConversationState,
                       intent_result: IntentResult,
                       entities: dict) -> dict[str, Any]:
        appointment_id = state.get_slot("appointment_id")

        if not appointment_id:
            # Try to find the caller's booking without making them recite an ID.
            resolved = self._resolve_single_appointment(state, intent_result)
            if resolved is not None:
                return resolved
            appointment_id = state.get_slot("appointment_id")
            if not appointment_id:
                return self._ask_for(state, "appointment_id", intent_result)

        found = self.appointments.get_appointment(appointment_id)
        if not found.ok:
            state.set_slot("appointment_id", None)
            return self._reply(state,
                               self.responses.error(found.error, found.message),
                               Action.ERROR, intent_result,
                               template=self._lookup_error_template(found.error))

        appointment = found.data
        doctor_name = self._doctor_name(appointment.get("doctor_id"))
        state.confirmation_required = True
        state.pending_action = Action.CANCEL_APPOINTMENT
        state.current_slot = None
        return self._reply(
            state,
            self.responses.confirm_cancellation(
                doctor_name, appointment.get("date"), appointment.get("time")),
            Action.ASK_CONFIRMATION, intent_result,
            template=("confirm_cancel", {"doctor": doctor_name,
                                         "doctor_id": appointment.get("doctor_id"),
                                         "date": appointment.get("date"),
                                         "time": appointment.get("time")}))

    def _commit_cancellation(self, state: ConversationState,
                             intent_result: IntentResult) -> dict[str, Any]:
        appointment_id = state.get_slot("appointment_id")
        found = self.appointments.get_appointment(appointment_id)
        if not found.ok:
            return self._reply(state,
                               self.responses.error(found.error, found.message),
                               Action.ERROR, intent_result)
        appointment = found.data
        doctor_name = self._doctor_name(appointment.get("doctor_id"))

        result = self.backend.cancel_appointment(
            appointment_id, patient_id=state.get_slot("patient_id"))
        if not result.success:
            return self._backend_error(state, result, intent_result)

        message = self.responses.cancelled(doctor_name, appointment.get("date"),
                                           appointment.get("time"))
        self.slots.clear_slots(state)
        state.intent = None
        return self._reply(state, message, Action.CANCEL_APPOINTMENT,
                           intent_result, backend_result=result,
                           extra={"appointment": result.data.get("appointment")})

    # ==================================================================
    # Reschedule workflow
    # ==================================================================
    def _handle_reschedule(self, state: ConversationState,
                           intent_result: IntentResult,
                           entities: dict) -> dict[str, Any]:
        self._absorb_offered_slot(state, entities)

        if not state.get_slot("appointment_id"):
            resolved = self._resolve_single_appointment(state, intent_result)
            if resolved is not None:
                return resolved
            if not state.get_slot("appointment_id"):
                return self._ask_for(state, "appointment_id", intent_result)

        found = self.appointments.get_appointment(state.get_slot("appointment_id"))
        if not found.ok:
            state.set_slot("appointment_id", None)
            return self._reply(state,
                               self.responses.error(found.error, found.message),
                               Action.ERROR, intent_result)
        appointment = found.data

        # "Mera time 5 baje kar dein" moves the TIME only - the day stays. The
        # audit found this asking "which day?" and looping on "haan".
        if state.get_slot("time") and not state.get_slot("date"):
            state.set_slot("date", appointment.get("date"))

        for slot in ("date", "time"):
            if not state.get_slot(slot):
                return self._ask_for(state, slot, intent_result)

        iso_date, time_str = state.get_slot("date"), state.get_slot("time")
        invalid = self._validate_date_time(state, iso_date, time_str, intent_result)
        if invalid:
            return invalid

        doctor_id = appointment["doctor_id"]
        doctor_name = self._doctor_name(doctor_id)
        availability = self.appointments.check_availability(
            doctor_id, iso_date, time_str)
        if not availability.ok:
            return self._availability_problem(state, availability, iso_date,
                                              intent_result, doctor_name)
        if not availability.data:
            return self._offer_alternatives(state, doctor_id, doctor_name,
                                            iso_date, time_str, intent_result)

        state.confirmation_required = True
        state.pending_action = Action.RESCHEDULE_APPOINTMENT
        state.current_slot = None
        return self._reply(
            state,
            self.responses.confirm_reschedule(doctor_name, iso_date, time_str),
            Action.ASK_CONFIRMATION, intent_result,
            template=("confirm_reschedule", {"doctor": doctor_name, "doctor_id": doctor_id,
                                             "date": iso_date, "time": time_str}))

    def _commit_reschedule(self, state: ConversationState,
                           intent_result: IntentResult) -> dict[str, Any]:
        appointment_id = state.get_slot("appointment_id")
        iso_date, time_str = state.get_slot("date"), state.get_slot("time")

        result = self.backend.reschedule_appointment(
            appointment_id, iso_date, time_str,
            patient_id=state.get_slot("patient_id"))
        if not result.success:
            return self._backend_error(state, result, intent_result)

        doctor_name = result.data.get("doctor_name", "the doctor")
        message = self.responses.rescheduled(doctor_name, iso_date, time_str)
        self.slots.clear_slots(state)
        state.intent = None
        return self._reply(state, message, Action.RESCHEDULE_APPOINTMENT,
                           intent_result, backend_result=result,
                           extra={"appointment": result.data.get("appointment")})

    # ==================================================================
    # Check appointment
    # ==================================================================
    def _handle_check(self, state: ConversationState,
                      intent_result: IntentResult,
                      entities: dict) -> dict[str, Any]:
        appointment_id = state.get_slot("appointment_id")
        if appointment_id:
            found = self.appointments.get_appointment(appointment_id)
            if not found.ok:
                state.set_slot("appointment_id", None)
                return self._reply(state,
                                   self.responses.error(found.error, found.message),
                                   Action.ERROR, intent_result)
            return self._show_appointment(state, found.data, intent_result)

        patient_id = state.get_slot("patient_id")
        if not patient_id:
            return self._ask_for(state, "appointment_id", intent_result)

        listing = self.appointments.get_patient_appointments(patient_id)
        if not listing.ok:
            return self._reply(state,
                               self.responses.error(listing.error, listing.message),
                               Action.ERROR, intent_result)
        appointments = listing.data
        if not appointments:
            return self._reply(state, self.responses.no_appointments(),
                               Action.CHECK_APPOINTMENT_STATUS, intent_result,
                               template=("no_appointments", {}))
        if len(appointments) == 1:
            return self._show_appointment(state, appointments[0], intent_result)

        names = {a["doctor_id"]: self._doctor_name(a["doctor_id"])
                 for a in appointments}
        state.current_slot = "appointment_id"
        return self._reply(state,
                           self.responses.which_appointment(appointments, names),
                           Action.ASK_FOR_APPOINTMENT_ID, intent_result,
                           template=self._listing_template(appointments, names))

    def _show_appointment(self, state: ConversationState, appointment: dict,
                          intent_result: IntentResult) -> dict[str, Any]:
        doctor_name = self._doctor_name(appointment.get("doctor_id"))
        clinic = None
        if appointment.get("clinic_id"):
            clinic_result = self.clinics.get_clinic(appointment["clinic_id"])
            clinic = clinic_result.data if clinic_result.ok else None
        message = self.responses.appointment_details(
            doctor_name, appointment.get("date"), appointment.get("time"),
            clinic, appointment.get("status", "confirmed"))
        state.intent = None
        state.current_slot = None
        cancelled = str(appointment.get("status", "")).startswith("cancelled")
        return self._reply(state, message, Action.CHECK_APPOINTMENT_STATUS,
                           intent_result, extra={"appointment": appointment},
                           template=("appointment_was_cancelled" if cancelled
                                     else "appointment_details",
                                     {"doctor": doctor_name,
                                      "doctor_id": appointment.get("doctor_id"),
                                      "date": appointment.get("date"),
                                      "time": appointment.get("time")}))

    # ==================================================================
    # Information intents
    # ==================================================================
    def _handle_availability(self, state: ConversationState,
                             intent_result: IntentResult,
                             entities: dict) -> dict[str, Any]:
        missing = self.slots.get_missing_slots(state, Intent.DOCTOR_AVAILABILITY)
        if missing:
            return self._ask_for(state, missing[0], intent_result)

        doctor_id = state.get_slot("doctor_id")
        doctor_name = state.get_slot("doctor_name")
        iso_date = state.get_slot("date")

        available = self.appointments.get_available_slots(doctor_id, iso_date)
        if not available.ok:
            return self._availability_problem(state, available, iso_date,
                                              intent_result, doctor_name)

        state.offered_slots = available.data[:3]
        state.current_slot = None
        # Keep the doctor/date so "yes, book one" flows straight into booking.
        values = {"doctor": doctor_name, "doctor_id": doctor_id, "date": iso_date,
                  "slots": available.data[:3]}
        return self._reply(
            state,
            self.responses.availability_answer(doctor_name, iso_date,
                                               available.data),
            Action.PROVIDE_DOCTOR_AVAILABILITY, intent_result,
            extra={"available_slots": available.data},
            template=("availability" if available.data else "no_slots", values))

    def _handle_doctor_info(self, state: ConversationState,
                            intent_result: IntentResult,
                            entities: dict) -> dict[str, Any]:
        # A parked topic outranks this turn's classification. Answering
        # "Dr Ahmed" to "which doctor?" comes back as generic
        # doctor_information (topic "general"), which must not overwrite the
        # "fee" the caller originally asked about.
        topic = state.pending_topic or intent_result.topic or state.info_topic

        if topic == "list" and not state.get_slot("doctor_id"):
            listing = self.doctors.list_doctors()
            if not listing.ok:
                return self._reply(state,
                                   self.responses.error(listing.error),
                                   Action.ERROR, intent_result)
            state.intent = None
            return self._reply(state, self.responses.doctor_list(listing.data),
                               Action.PROVIDE_DOCTOR_INFORMATION, intent_result,
                               template=("doctor_list", {"doctors": [
                                   d.get("name") for d in listing.data if d.get("name")]}))

        doctor_id = state.get_slot("doctor_id")
        if not doctor_id:
            # Park the question so the answer to "which doctor?" resumes it.
            state.pending_intent = Intent.DOCTOR_INFORMATION
            state.pending_topic = topic
            return self._ask_for(state, "doctor_name", intent_result)

        found = self.doctors.get_doctor(doctor_id)
        if not found.ok:
            return self._reply(state, self.responses.error(found.error),
                               Action.ERROR, intent_result)
        doctor = found.data
        clinic_result = self.clinics.get_clinic_for_doctor(doctor)
        clinic = clinic_result.data if clinic_result.ok else None

        # Question answered: nothing is parked any more.
        state.pending_intent = None
        state.pending_topic = None
        state.intent = None
        state.current_slot = None
        action = {
            "fee": Action.PROVIDE_DOCTOR_FEE,
            "qualification": Action.PROVIDE_DOCTOR_QUALIFICATION,
        }.get(topic, Action.PROVIDE_DOCTOR_INFORMATION)
        values = {"doctor": doctor.get("name", "the doctor"), "doctor_id": doctor_id,
                  "fee": doctor.get("fee"), "qualification": doctor.get("qualification"),
                  "experience": doctor.get("experience_years"),
                  "specialization": doctor.get("specialization"),
                  "clinic": (clinic or {}).get("name", "the clinic")}
        key = {"fee": "doctor_fee", "qualification": "doctor_qualification",
               "specialization": "doctor_specialization"}.get(topic, "doctor_profile")
        return self._reply(
            state, self.responses.doctor_information(doctor, clinic, topic),
            action, intent_result, template=(key, values))

    def _handle_clinic_info(self, state: ConversationState,
                            intent_result: IntentResult,
                            entities: dict) -> dict[str, Any]:
        topic = state.pending_topic or intent_result.topic or state.info_topic
        doctor = None
        clinic = None

        doctor_id = state.get_slot("doctor_id")
        if doctor_id:
            found = self.doctors.get_doctor(doctor_id)
            if found.ok:
                doctor = found.data
                clinic_result = self.clinics.get_clinic_for_doctor(doctor)
                clinic = clinic_result.data if clinic_result.ok else None

        if clinic is None:
            listing = self.clinics.list_clinics()
            if listing.ok and listing.data:
                clinic = listing.data[0]

        state.intent = None
        state.current_slot = None
        template = None
        if clinic is not None:
            key = ("clinic_address" if topic == "address"
                   else "clinic_hours" if topic in ("timing", "closed")
                   else "clinic_general")
            template = (key, {"clinic": clinic.get("name"), "address": clinic.get("address"),
                              "phone": clinic.get("phone")})
        return self._reply(
            state, self.responses.clinic_information(clinic, topic, doctor),
            Action.PROVIDE_CLINIC_INFORMATION, intent_result, template=template)

    # ==================================================================
    # Conversational intents
    # ==================================================================
    def _handle_greeting(self, state, intent_result, entities):
        state.clarification_attempts = 0
        return self._reply(state, self.responses.greeting(),
                           Action.GREET, intent_result)

    def _handle_goodbye(self, state, intent_result, entities):
        return self._reply(state, self.responses.farewell(),
                           Action.END_CONVERSATION, intent_result)

    def _handle_thanks(self, state, intent_result, entities):
        return self._reply(state, self.responses.thanks(),
                           Action.INFORM, intent_result)

    def _handle_help(self, state, intent_result, entities):
        # CLARIFY re-rendered as "sorry, I did not understand" - wrong for a
        # caller who asked for help. The template says what the line can do.
        return self._reply(state, self.responses.help(),
                           Action.CLARIFY, intent_result,
                           template=("help_menu", {}))

    def _handle_repeat(self, state, intent_result, entities):
        history = [t for t in state.conversation_history
                   if t["speaker"] == "agent"]
        last = history[-1]["text"] if history else None
        return self._reply(state, self.responses.repeat(last),
                           state.last_action or Action.CLARIFY, intent_result,
                           template=getattr(state, "last_template", None))

    def _handle_emergency(self, state, intent_result, entities):
        """Never book: hand the caller straight to a human."""
        clinic = None
        listing = self.clinics.list_clinics()
        if listing.ok and listing.data:
            clinic = listing.data[0]
        template = (("emergency", {"clinic": clinic.get("name"),
                                   "address": clinic.get("address"),
                                   "phone": clinic.get("phone")})
                    if clinic else ("emergency_no_clinic", {}))
        return self._reply(state, self.responses.emergency(clinic),
                           Action.ESCALATE, intent_result, template=template)

    def _handle_unknown(self, state: ConversationState,
                        intent_result: IntentResult,
                        entities: dict) -> dict[str, Any]:
        """
        Low confidence or an unmapped label: never touch the database.

        A clarification counter stops the assistant looping forever on a noisy
        line - after two failures it reads an explicit menu instead.
        """
        state.clarification_attempts += 1
        message = self.responses.clarify(state.clarification_attempts)
        if state.clarification_attempts > MAX_CLARIFICATION_ATTEMPTS:
            state.clarification_attempts = 0
        return self._reply(state, message, Action.CLARIFY, intent_result)

    # ==================================================================
    # Shared helpers
    # ==================================================================
    def _ask_for(self, state: ConversationState, slot: str,
                 intent_result: IntentResult) -> dict[str, Any]:
        """Ask for one missing slot and remember that we asked."""
        state.current_slot = slot
        message = self.responses.ask_slot(slot, state.get_slot("doctor_name"),
                                          topic=state.pending_topic)
        return self._reply(state, message, self.slots.action_for_slot(slot),
                           intent_result, slot=slot)

    def _absorb_offered_slot(self, state: ConversationState,
                             entities: dict) -> None:
        """If we read out alternative times and they picked one, accept it."""
        if state.offered_slots and entities.get("time") in state.offered_slots:
            state.set_slot("time", entities["time"])
            state.offered_slots = []

    def _validate_date_time(self, state: ConversationState, iso_date: str,
                            time_str: str,
                            intent_result: IntentResult) -> dict | None:
        """Reject impossible dates/times before hitting the database."""
        if not is_valid_date(iso_date):
            state.set_slot("date", None)
            state.current_slot = "date"
            return self._reply(state, self.responses.error("INVALID_DATE"),
                               Action.ASK_FOR_DATE, intent_result, slot="date",
                               template=("bad_date", {}))
        if is_past_date(iso_date):
            state.set_slot("date", None)
            state.current_slot = "date"
            return self._reply(state, self.responses.error("DATE_IN_PAST"),
                               Action.ASK_FOR_DATE, intent_result, slot="date",
                               template=("date_in_past", {}))
        if not is_valid_time(time_str):
            state.set_slot("time", None)
            state.current_slot = "time"
            return self._reply(state, self.responses.error("INVALID_TIME"),
                               Action.ASK_FOR_TIME, intent_result, slot="time",
                               template=("bad_time", {}))
        return None

    def _availability_problem(self, state: ConversationState, result,
                              iso_date: str, intent_result: IntentResult,
                              doctor_name: str | None = None) -> dict[str, Any]:
        """The whole day is unusable (closed, blocked, past)."""
        doctor_name = doctor_name or state.get_slot("doctor_name") or "the doctor"
        key = {"DOCTOR_UNAVAILABLE": "doctor_unavailable",
               "NO_SCHEDULE": "no_schedule",
               "DATE_IN_PAST": "date_in_past",
               "INVALID_DATE": "bad_date"}.get(result.error)
        state.set_slot("date", None)
        state.set_slot("time", None)
        state.current_slot = "date"
        # The template carries the REASON; replacing it with a bare "which
        # day?" left callers not knowing the doctor was on leave.
        return self._reply(state,
                           self.responses.error(result.error, result.message),
                           Action.ASK_FOR_DATE, intent_result, slot="date",
                           template=(key, {"doctor": doctor_name, "date": iso_date})
                           if key else None)

    def _offer_alternatives(self, state: ConversationState, doctor_id: str,
                            doctor_name: str, iso_date: str, time_str: str,
                            intent_result: IntentResult) -> dict[str, Any]:
        suggestions = self.appointments.suggest_alternatives(
            doctor_id, iso_date, around=time_str)
        alternatives = suggestions.data if suggestions.ok else []
        state.offered_slots = alternatives
        state.set_slot("time", None)
        state.current_slot = "time"
        state.confirmation_required = False
        return self._reply(
            state,
            self.responses.offer_alternatives(doctor_name, iso_date,
                                              time_str, alternatives),
            Action.OFFER_ALTERNATIVES, intent_result, slot="time",
            extra={"available_slots": alternatives},
            template=("slot_taken" if alternatives else "no_slots",
                      {"doctor": doctor_name, "doctor_id": doctor_id, "date": iso_date,
                       "time": time_str, "slots": alternatives}))

    def _no_appointment_id(self, state: ConversationState,
                           intent_result: IntentResult) -> dict[str, Any]:
        """
        The caller was asked for an appointment ID and said no.

        Tell them what is on file instead of asking again: the bookings to
        choose from, or that there are none (and offer to book one).
        """
        patient_id = state.get_slot("patient_id")
        listing = (self.appointments.get_patient_appointments(patient_id)
                   if patient_id else None)
        if listing is not None and listing.ok and listing.data:
            appointments = listing.data
            names = {a["doctor_id"]: self._doctor_name(a["doctor_id"])
                     for a in appointments}
            return self._reply(state,
                               self.responses.which_appointment(appointments, names),
                               Action.ASK_FOR_APPOINTMENT_ID, intent_result,
                               slot="appointment_id",
                               template=self._listing_template(appointments, names))

        self.slots.clear_slots(state)
        state.intent = None
        state.current_slot = None
        if listing is not None and listing.ok:
            return self._reply(state, self.responses.no_appointments(),
                               Action.CHECK_APPOINTMENT_STATUS, intent_result,
                               template=("no_appointments", {}))
        return self._reply(state, self.responses.aborted(),
                           Action.ABORT, intent_result,
                           template=("aborted", {}))

    @staticmethod
    def _listing_template(appointments: list[dict], names: dict) -> tuple:
        return ("which_appointment", {"appointments": [
            {"doctor": names.get(a.get("doctor_id"), "the doctor"),
             "doctor_id": a.get("doctor_id"), "date": a.get("date"),
             "time": a.get("time"), "id": a.get("appointment_id")}
            for a in appointments]})

    def _resolve_single_appointment(self, state: ConversationState,
                                    intent_result: IntentResult):
        """
        Find the caller's booking without making them recite an ID.

        Returns a reply dict when the caller must choose, otherwise None with
        `appointment_id` filled in.
        """
        patient_id = state.get_slot("patient_id")
        if not patient_id:
            return None
        listing = self.appointments.get_patient_appointments(patient_id)
        if not listing.ok or not listing.data:
            return None
        appointments = listing.data
        if len(appointments) == 1:
            state.set_slot("appointment_id", appointments[0]["appointment_id"])
            return None
        names = {a["doctor_id"]: self._doctor_name(a["doctor_id"])
                 for a in appointments}
        state.current_slot = "appointment_id"
        return self._reply(state,
                           self.responses.which_appointment(appointments, names),
                           Action.ASK_FOR_APPOINTMENT_ID, intent_result,
                           slot="appointment_id",
                           template=self._listing_template(appointments, names))

    def _resolve_patient(self, state: ConversationState) -> str:
        """Identify the caller, registering them if this is a first call."""
        patient_id = state.get_slot("patient_id")
        if patient_id:
            return patient_id
        result = self.patients.get_or_create(
            name=state.get_slot("patient_name"),
            phone=state.get_slot("patient_phone"))
        if result.ok:
            patient_id = result.data["patient_id"]
            state.set_slot("patient_id", patient_id)
            return patient_id
        return "P_UNKNOWN"

    @staticmethod
    def _lookup_error_template(code: str | None) -> tuple:
        key = {"APPOINTMENT_NOT_FOUND": "not_found",
               "ALREADY_CANCELLED": "already_cancelled",
               "DOCTOR_NOT_FOUND": "doctor_not_found"}.get(code, "backend_error")
        return (key, {})

    def _doctor_name(self, doctor_id: str | None) -> str:
        if not doctor_id:
            return "the doctor"
        found = self.doctors.get_doctor(doctor_id)
        return found.data.get("name", "the doctor") if found.ok else "the doctor"

    def _backend_error(self, state: ConversationState, result,
                       intent_result: IntentResult) -> dict[str, Any]:
        """
        Turn a backend refusal into a spoken message.

        The backend's lower-snake error codes are mapped onto the response
        generator's wording; anything unmapped still gets a polite sentence.
        """
        wording = {
            "doctor_not_found": "DOCTOR_NOT_FOUND",
            "patient_not_found": "PATIENT_NOT_FOUND",
            "appointment_not_found": "APPOINTMENT_NOT_FOUND",
            "not_your_appointment": "APPOINTMENT_NOT_FOUND",
            "already_cancelled": "ALREADY_CANCELLED",
            "slot_unavailable": "SLOT_TAKEN",
            "invalid_date": "INVALID_DATE",
            "date_in_past": "DATE_IN_PAST",
            "invalid_time": "INVALID_TIME",
            "doctor_unavailable": "DOCTOR_UNAVAILABLE",
            "doctor_not_working": "NO_SCHEDULE",
            "backend_unavailable": "BACKEND_UNAVAILABLE",
        }.get(result.error_code, "BACKEND_UNAVAILABLE")
        message = self.responses.error(wording, result.error_message)
        return self._reply(state, message, Action.ERROR, intent_result,
                           backend_result=result)

    def _reply(self, state: ConversationState, message: str, action: str,
               intent_result: IntentResult, slot: str | None = None,
               extra: dict | None = None, backend_result=None,
               template=None) -> dict[str, Any]:
        """Build the structured response (spec section 32)."""
        if action not in (Action.ASK_FOR_DOCTOR, Action.ASK_FOR_DATE,
                          Action.ASK_FOR_TIME, Action.ASK_FOR_APPOINTMENT_ID,
                          Action.ASK_FOR_PATIENT_INFO,
                          Action.OFFER_ALTERNATIVES,
                          Action.ASK_CONFIRMATION):
            state.current_slot = None

        payload: dict[str, Any] = {
            "session_id": state.session_id,
            "response": message,
            "state": state.dialog_state,
            "pending_intent": state.pending_intent,
            "intent": state.intent or intent_result.intent,
            "raw_intent": intent_result.raw_intent,
            "confidence": round(intent_result.confidence, 4),
            "action": action,
            "slot": slot,
            "slots": dict(state.slots),
            "session_state": state.to_dict(),
        }
        # The facts behind the English sentence, so the voice layer can say the
        # same thing in Roman Urdu or Urdu (see ollama_judge/dialog_templates.py).
        if template is not None:
            if isinstance(template, dict):
                payload["template"] = template
            else:
                payload["template"] = {"key": template[0], "values": template[1]}
        if extra:
            payload.update(extra)
        # The raw backend result travels with the reply so the Ollama judge can
        # rephrase it without ever querying the database itself.
        payload["backend_result"] = (backend_result.to_dict()
                                     if backend_result is not None else None)
        payload["_backend_result_obj"] = backend_result
        return payload

    def _empty_input(self, session_id: str,
                     patient_id: str | None) -> dict[str, Any]:
        state = self.sessions.get_or_create(session_id, patient_id)
        message = self.responses.empty_input()
        state.last_system_message = message
        return {
            "session_id": session_id,
            "response": message,
            "intent": Intent.UNKNOWN,
            "raw_intent": "empty",
            "confidence": 0.0,
            "action": Action.CLARIFY,
            "slot": None,
            "slots": dict(state.slots),
            "session_state": state.to_dict(),
        }

    # ==================================================================
    # Session control
    # ==================================================================
    def reset_session(self, session_id: str,
                      patient_id: str | None = None) -> ConversationState:
        return self.sessions.reset(session_id, patient_id)

    def end_session(self, session_id: str) -> bool:
        self.conversations.end_session(session_id)
        return self.sessions.end(session_id)
