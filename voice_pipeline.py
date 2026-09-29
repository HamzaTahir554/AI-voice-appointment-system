"""
The end-to-end voice pipeline (spec section 36).

    STT text
       -> mBERT intent detection
       -> Dialog Manager (state, slots, confirmation)
       -> Appointment Backend (business rules, Firestore)
       -> Ollama Judge (natural wording)
       -> Response Validator (hallucination guard)
       -> text for TTS

Each stage has one job. The LLM is the last stage and touches nothing but the
wording: by the time it runs, the appointment has already been booked (or
refused) by deterministic code, and the validator checks that whatever the
model says matches that outcome.

    python voice_pipeline.py                 # scripted booking
    python voice_pipeline.py --interactive   # type your own turns
    python voice_pipeline.py -i --no-llm     # deterministic responses only
    python voice_pipeline.py -i --fresh      # DELETE all appointments first
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

from config import (
    Action, OLLAMA_TRANSLATE, RESPONSE_LANGUAGE, use_utf8_stdout,
)
from dialog_manager.dialog_manager import DialogManager
from firebase import metrics as db_metrics
from firebase.firebase_config import get_repository, init_repository
from ollama_judge.judge import OllamaJudge
from ollama_judge.language import (
    ENGLISH, ROMAN_URDU, URDU, acknowledge, render, resolve_language,
    spoken_date, spoken_slots, spoken_time,
)

# Dialog Manager actions that have a ready-made template in every language, so
# slot questions and greetings are spoken in the caller's language even with
# the LLM switched off.
_ACTION_TEMPLATES = {
    Action.ASK_FOR_DOCTOR: "ask_doctor",
    Action.ASK_FOR_DATE: "ask_date",
    Action.ASK_FOR_TIME: "ask_time",
    Action.ASK_FOR_APPOINTMENT_ID: "ask_appointment_id",
    Action.GREET: "greeting",
    Action.END_CONVERSATION: "farewell",
    Action.INFORM: "thanks",
    Action.CLARIFY: "clarify",
}

logger = logging.getLogger(__name__)

# Turns where the Dialog Manager performed a real database mutation. These are
# the ones worth sending to the judge, because there is a backend result to
# report faithfully.
_BACKEND_ACTIONS = {
    Action.CREATE_APPOINTMENT,
    Action.CANCEL_APPOINTMENT,
    Action.RESCHEDULE_APPOINTMENT,
    Action.OFFER_ALTERNATIVES,
    Action.PROVIDE_DOCTOR_AVAILABILITY,
    Action.ERROR,
}


class VoicePipeline:
    """One object the telephony layer talks to."""

    def __init__(self, dialog_manager: DialogManager | None = None,
                 judge: OllamaJudge | None = None,
                 repository=None, use_llm: bool = True,
                 translate: bool = OLLAMA_TRANSLATE,
                 response_language: str = RESPONSE_LANGUAGE,
                 phrase_every_turn: bool = False):
        self.repo = repository or get_repository() or init_repository()
        self.dialog = dialog_manager or DialogManager(repository=self.repo)
        self.judge = judge or OllamaJudge()
        self.use_llm = use_llm
        self.translate = translate
        # 'auto' follows the caller; otherwise every reply is forced
        # into this language.
        self.response_language = response_language
        # Voice calls: turns with no database result are worded by the model
        # too, checked against the Dialog Manager's sentence for the turn.
        self.phrase_every_turn = phrase_every_turn

    # ------------------------------------------------------------------
    @property
    def llm_available(self) -> bool:
        return self.use_llm and self.judge.available

    # ------------------------------------------------------------------
    def process(self, session_id: str, text: str,
                patient_id: str | None = None) -> dict[str, Any]:
        """
        Handle one utterance from the caller and return what TTS should say.

        Never raises: any failure below degrades to the Dialog Manager's own
        deterministic wording so the call keeps going.
        """
        # Where the time goes, for the voice latency report: mBERT, the
        # database, the Dialog Manager's own logic, and the LLM.
        started = time.perf_counter()
        counter = db_metrics.current() or db_metrics.begin()
        db_before = counter.seconds
        turn = self.dialog.process_message(session_id, text, patient_id)
        database_ms = (counter.seconds - db_before) * 1000
        mbert_ms = getattr(getattr(self.dialog, "router", None), "last_predict_ms", None)
        dialog_ms = ((time.perf_counter() - started) * 1000 - database_ms
                     - (mbert_ms or 0.0))
        llm_ms = None

        deterministic = turn["response"]
        backend_result = turn.pop("_backend_result_obj", None)

        # Language is a property of the CONVERSATION, not of one utterance:
        # a bare "yes" must not switch a Roman-Urdu call into English.
        state = self.dialog.sessions.get(session_id)
        current = state.language if state else "english"
        language = resolve_language(text, current,
                                    locked=bool(state and state.language_locked),
                                    override=self.response_language)
        if state is not None:
            state.language = language

        # Re-render in the caller's language AND with rotated wording, so the
        # same question asked twice never comes out identically. The Dialog
        # Manager writes one fixed English sentence per action; that is what
        # makes an assistant sound like an IVR menu.
        action = turn.get("action", "")

        def variant_for(key: str) -> int:
            return state.next_variant(key) if state is not None else 0

        templated = False
        if backend_result is not None:
            from ollama_judge.fallback import build_fallback_response
            deterministic = build_fallback_response(
                backend_result, language, variant=variant_for(action))
        elif turn.get("template"):
            # The Dialog Manager says which facts it spoke; say the same facts
            # in the caller's language (dialog_templates.py).
            spoken = self._render_template(turn["template"], language, variant_for)
            if spoken:
                deterministic = spoken
                templated = True
        elif action == Action.CONFIRM_INTENT:
            # "Ji, aap appointment cancel karna chahte hain?" - the specific
            # question, not the generic "what did you want?".
            from ollama_judge.language import intent_phrase
            guessed = (turn.get("session_state") or {}).get("pending_intent")
            phrase = intent_phrase(guessed, language) if guessed else None
            if phrase:
                translated = render("confirm_intent", language,
                                    variant=variant_for("confirm_intent"),
                                    action=phrase)
                if translated:
                    deterministic = translated
        elif action == Action.ASK_CONFIRMATION:
            # "Shall I book it?" needs the details read back, so fill the
            # template from the slots the Dialog Manager has collected.
            slots = turn.get("slots") or {}
            translated = render(
                "confirm_booking", language, variant=variant_for("confirm"),
                doctor=self._doctor_label(slots, language),
                date=spoken_date(slots.get("date"), language),
                time=spoken_time(slots.get("time"), language))
            if translated and slots.get("date") and slots.get("time"):
                deterministic = translated
        else:
            template = _ACTION_TEMPLATES.get(action)
            if template:
                translated = render(template, language,
                                    variant=variant_for(template))
                if translated:
                    deterministic = translated

        # A side question was answered and the task resumed: ask the task's
        # next question in the same language.
        resume = turn.get("resume_action")
        if templated and resume and _ACTION_TEMPLATES.get(resume):
            key = _ACTION_TEMPLATES[resume]
            question = render(key, language, variant=variant_for(key))
            if question:
                deterministic = f"{deterministic} {question}"

        # Open with a short "theek hai" when the caller has just told us
        # something - and "koi baat nahi" when they corrected themselves.
        deterministic = self._with_acknowledgement(
            deterministic, turn, state, language)

        final_response = deterministic
        judge_info: dict[str, Any] = {
            "used": False, "source": "dialog_manager", "decision": "approved",
            "reason": "no LLM stage for this turn", "validation_problems": [],
        }

        if self.use_llm:
            llm_started = time.perf_counter()
            try:
                if backend_result is not None and turn["action"] in _BACKEND_ACTIONS:
                    # A real operation happened: let the judge phrase it, then
                    # verify the phrasing against the backend result.
                    verdict = self.judge.judge(
                        user_text=text,
                        intent=turn.get("intent", ""),
                        confidence=turn.get("confidence", 0.0),
                        dialog_state=turn.get("slots", {}),
                        backend_result=backend_result,
                        language=language)
                    final_response = verdict.response
                    judge_info = {"used": True, **verdict.to_dict(),
                                  "llm_raw": verdict.llm_raw}
                elif self.phrase_every_turn:
                    verdict = self.judge.phrase(deterministic, text, language)
                    final_response = verdict.response
                    judge_info = {"used": verdict.source == "ollama", **verdict.to_dict(),
                                  "llm_raw": verdict.llm_raw}
                elif self.translate and language != "english":
                    # No database operation (a question, a greeting): the only
                    # value the LLM adds is saying it in the caller's language.
                    final_response = self.judge.rephrase(deterministic, text)
                    judge_info = {
                        "used": final_response != deterministic,
                        "source": ("ollama" if final_response != deterministic
                                   else "dialog_manager"),
                        "decision": "approved", "reason": "translation only",
                        "validation_problems": [],
                    }
            except Exception as exc:               # pragma: no cover
                # The LLM must never be able to break the call.
                logger.error("judge stage failed: %s", exc)
                final_response = deterministic
                judge_info = {"used": False, "source": "dialog_manager",
                              "decision": "approved",
                              "reason": f"judge error: {exc}",
                              "validation_problems": []}
            if judge_info["reason"] != "no LLM stage for this turn":
                llm_ms = (time.perf_counter() - llm_started) * 1000

        return {
            "success": bool(backend_result.success) if backend_result else True,
            "session_id": session_id,
            "intent": turn.get("intent"),
            "raw_intent": turn.get("raw_intent"),
            "confidence": turn.get("confidence", 0.0),
            "action": turn.get("action"),
            "state": turn.get("state"),
            "slot": turn.get("slot"),
            "pending_intent": turn.get("pending_intent"),
            "response": final_response,
            "deterministic_response": deterministic,
            "appointment_id": (backend_result.appointment_id
                               if backend_result else None),
            "language": language,
            "slots": turn.get("slots", {}),
            "backend_result": turn.get("backend_result"),
            "judge": judge_info,
            "timings": {"mbert_ms": mbert_ms, "dialog_ms": dialog_ms,
                        "database_ms": database_ms, "llm_ms": llm_ms},
        }

    # ------------------------------------------------------------------
    # Actions that are a QUESTION about something still missing. Only these
    # get an acknowledgement prefix; a completed booking already reads
    # naturally and does not need "sure" bolted on the front.
    _ACK_ACTIONS = {
        Action.ASK_FOR_DOCTOR, Action.ASK_FOR_DATE, Action.ASK_FOR_TIME,
        Action.ASK_FOR_APPOINTMENT_ID, Action.ASK_CONFIRMATION,
    }

    def _with_acknowledgement(self, message: str, turn: dict, state,
                              language: str) -> str:
        """
        Prepend "theek hai" / "koi baat nahi" where a human receptionist would.

        Only when the caller actually told us something this turn - a bare
        "sure" in front of a question they never answered sounds worse than no
        acknowledgement at all.
        """
        if state is None or turn.get("action") not in self._ACK_ACTIONS:
            return message
        # "Dobara bata dein" is not new information, and a reply explaining a
        # problem (doctor on leave, no ID) must not open with "theek hai".
        if turn.get("raw_intent") == "repeat_information":
            return message
        template_key = (turn.get("template") or {}).get("key")
        if template_key and template_key not in self._ACK_TEMPLATES:
            return message
        corrected = getattr(state, "last_turn_corrected", False)
        filled = {k: v for k, v in (turn.get("slots") or {}).items()
                  if v and k != "patient_id"}
        if not corrected and not filled:
            return message
        if not corrected and turn.get("action") == Action.ASK_FOR_DOCTOR:
            return message              # the opening question needs no "sure"
        prefix = acknowledge(language, correction=corrected,
                             index=state.next_variant("ack"))
        return f"{prefix} {message}"

    _ACK_TEMPLATES = {"confirm_booking", "confirm_reschedule", "confirm_cancel"}

    _AND = {ENGLISH: " and ", ROMAN_URDU: " aur ", URDU: " اور "}

    def _join(self, items: list[str], language: str) -> str:
        items = [str(i) for i in items if i]
        if len(items) <= 1:
            return "".join(items)
        return ", ".join(items[:-1]) + self._AND.get(language, " and ") + items[-1]

    def _render_template(self, spec: dict, language: str, variant_for) -> str | None:
        """Fill a Dialog Manager template with values spoken in `language`."""
        key = spec.get("key")
        raw = dict(spec.get("values") or {})
        values: dict[str, Any] = {}
        for name, value in raw.items():
            if name == "date":
                values[name] = spoken_date(value, language)
            elif name == "time":
                values[name] = spoken_time(value, language)
            elif name == "slots":
                # Read out in clock order - "4:20, 4:40 ya 5:20", not nearest-first.
                values[name] = spoken_slots(sorted(value or []), language)
            elif name == "doctors":
                values[name] = self._join(list(value or []), language)
            elif name == "appointments":
                values["listing"] = self._join([
                    f"{self._doctor_label({'doctor_name': a.get('doctor'), 'doctor_id': a.get('doctor_id')}, language)} "
                    f"{spoken_date(a.get('date'), language)} {spoken_time(a.get('time'), language)} "
                    f"(ID {a.get('id')})" for a in value or []], language)
            else:
                values[name] = value
        if "doctor" in raw:
            values["doctor"] = self._doctor_label(
                {"doctor_name": raw.get("doctor"), "doctor_id": raw.get("doctor_id")}, language)
        return render(key, language, variant=variant_for(key), **values)

    def _doctor_label(self, slots: dict, language: str) -> str:
        """
        The doctor's name in the reply language.

        Slots carry the Latin name; for an Urdu reply we look up the Urdu
        spelling, otherwise an Urdu TTS voice reads Latin letters aloud badly.
        """
        latin = slots.get("doctor_name") or "the doctor"
        if language != URDU or not slots.get("doctor_id"):
            return latin
        found = self.dialog.doctors.get_doctor(slots["doctor_id"])
        if found.ok and found.data.get("name_urdu"):
            return found.data["name_urdu"]
        return latin

    # ------------------------------------------------------------------
    def reset(self, session_id: str, patient_id: str | None = None):
        return self.dialog.reset_session(session_id, patient_id)

    def end(self, session_id: str) -> bool:
        return self.dialog.end_session(session_id)


# --------------------------------------------------------------------------
# Command-line demo
# --------------------------------------------------------------------------
def _print_turn(result: dict[str, Any], show_slots: bool = True) -> None:
    """Print one turn with every stage visible - the point of the demo."""
    print(f"  mBERT   : {result['raw_intent']} ({result['confidence']:.2f})"
          f"  ->  {result['intent']}")
    print(f"  dialog  : action={result['action']}  state={result['state']}"
          + (f"  pending={result['pending_intent']}"
             if result.get("pending_intent") else ""))

    if show_slots:
        filled = {k: v for k, v in (result.get("slots") or {}).items() if v}
        if filled:
            print(f"  slots   : {filled}")

    backend = result.get("backend_result")
    if backend:
        code = (backend.get("error") or {}).get("code", "-")
        print(f"  backend : success={backend['success']} "
              f"op={backend['operation']} error={code}")
        alternatives = (backend.get("data") or {}).get("alternative_slots")
        if alternatives:
            print(f"            alternatives={alternatives}")
    else:
        print("  backend : (no database operation this turn)")

    judge = result.get("judge", {})
    print(f"  judge   : source={judge.get('source')} "
          f"decision={judge.get('decision')}")
    if judge.get("validation_problems"):
        # The hallucination guard firing is the most interesting thing the
        # demo can show, so never hide it.
        print(f"            REJECTED: {judge['validation_problems']}")
    print(f"  TTS     : {result['response']}")


def _prepare(fresh: bool, sample_appointment: bool = True):
    """
    Seed the database, optionally clearing the appointment diary.

    Appointments are deleted ONLY when `fresh` is asked for explicitly.
    Interactive mode used to imply it, which silently wiped every real booking
    in Firestore each time the pipeline started.
    """
    from config import Collections
    from firebase.seed_data import seed

    repo = init_repository()
    seed(repo, with_sample_appointment=sample_appointment and not fresh)
    if fresh:
        # The duplicate-appointment rule would otherwise refuse a demo booking
        # because the seeded APT123 is with the same doctor on the same day.
        for appointment in repo.query(Collections.APPOINTMENTS):
            repo.delete(Collections.APPOINTMENTS,
                        appointment["appointment_id"])
    return repo


SCRIPT = [
    "Assalam o Alaikum",
    "Mujhe Dr Ahmed se kal 4 baje appointment chahiye",
    "Ji haan",
]


def _scripted(pipeline: VoicePipeline, patient_id: str) -> None:
    for text in SCRIPT:
        result = pipeline.process("voice_demo", text, patient_id)
        print(f"\nPATIENT  : {text}")
        _print_turn(result)


def _interactive(pipeline: VoicePipeline, patient_id: str) -> None:
    """Type turns yourself and watch every stage react."""
    try:
        sys.stdin.reconfigure(encoding="utf-8")     # Windows consoles are cp1252
    except (AttributeError, ValueError):
        pass

    print("Type a sentence in English, Roman Urdu or Urdu.")
    print("Commands:  quit | reset | slots | llm on | llm off")
    print("           lang auto | lang english | lang roman_urdu | lang urdu")
    print("-" * 78)

    session = "voice_interactive"
    while True:
        try:
            text = input("\nPATIENT > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        if not text:
            continue

        lowered = text.lower()
        if lowered in ("quit", "exit", "q"):
            print("bye")
            return
        if lowered == "reset":
            pipeline.reset(session, patient_id)
            print("  (conversation reset)")
            continue
        if lowered == "slots":
            state = pipeline.dialog.sessions.get(session)
            print(f"  {state.to_dict() if state else '(no session yet)'}")
            continue
        if lowered.startswith("lang"):
            parts = lowered.split()
            choice = parts[1] if len(parts) > 1 else "auto"
            if choice in ("auto", "english", "roman_urdu", "urdu", "roman"):
                pipeline.response_language = (
                    "roman_urdu" if choice == "roman" else choice)
                print(f"  reply language: {pipeline.response_language}")
            else:
                print("  usage: lang auto|english|roman_urdu|urdu")
            continue
        if lowered in ("llm off", "llm on"):
            pipeline.use_llm = lowered.endswith("on")
            print(f"  LLM stage {'enabled' if pipeline.use_llm else 'disabled'}"
                  f" (deterministic responses"
                  f"{' still' if not pipeline.use_llm else ' when it fails'})")
            continue

        _print_turn(pipeline.process(session, text, patient_id))


def main() -> None:                               # pragma: no cover
    use_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Full voice pipeline: mBERT -> Dialog -> Backend -> Ollama")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="type your own turns")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip Ollama, use deterministic responses only")
    parser.add_argument("--patient", default="P001",
                        help="patient_id to run as (default P001)")
    parser.add_argument("--fresh", action="store_true",
                        help="clear all appointments before starting")
    parser.add_argument("--verbose", action="store_true",
                        help="show INFO logs from every stage")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)-7s %(name)s | %(message)s")

    # Interactive mode keeps the caller's real bookings and does not plant the
    # APT123 sample: that sample is with Dr Ahmed tomorrow, so the duplicate
    # rule would refuse the usual "Dr Ahmed, kal, 4 baje" demo booking.
    repo = _prepare(fresh=args.fresh, sample_appointment=not args.interactive)
    pipeline = VoicePipeline(repository=repo, use_llm=not args.no_llm)

    print("=" * 78)
    print("AI VOICE APPOINTMENT SYSTEM - full pipeline")
    print("=" * 78)
    print(f"database : {repo.backend}")
    from config import Collections
    print(f"bookings : {len(repo.query(Collections.APPOINTMENTS))} appointment(s) "
          f"already in the database")
    print(f"intent   : mBERT ({'loaded on first turn'})")
    if args.no_llm:
        print("Ollama   : disabled (--no-llm), deterministic responses")
    else:
        print(f"Ollama   : "
              f"{'available' if pipeline.llm_available else 'UNAVAILABLE'}"
              f" ({pipeline.judge.service.model})"
              f"{'' if pipeline.llm_available else ' - falling back to templates'}")
    print(f"patient  : {args.patient}")
    print("=" * 78)

    if args.interactive:
        _interactive(pipeline, args.patient)
    else:
        _scripted(pipeline, args.patient)


if __name__ == "__main__":                        # pragma: no cover
    main()
