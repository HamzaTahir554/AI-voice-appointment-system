"""
Deterministic responses built straight from the backend result.

Used whenever Ollama is unavailable, times out, returns unparseable output, or
produces something the validator rejects. These sentences are plain but always
true, so the appointment system keeps working with the LLM switched off
entirely (spec section 30).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from config import ErrorCode, TIMEZONE
from ollama_judge.language import (
    ENGLISH, render, spoken_date, spoken_slots, spoken_time,
)


# --------------------------------------------------------------------------
# Speech-friendly formatting
# --------------------------------------------------------------------------
def format_time(time_str: str | None) -> str:
    if not time_str:
        return ""
    try:
        hour, minute = (int(x) for x in str(time_str).split(":"))
    except (ValueError, AttributeError):
        return str(time_str)
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def format_date(iso_date: str | None) -> str:
    if not iso_date:
        return ""
    try:
        parsed = datetime.strptime(str(iso_date), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return str(iso_date)
    today = datetime.now(TIMEZONE).date()
    if parsed == today:
        return "today"
    if parsed == today + timedelta(days=1):
        return "tomorrow"
    return parsed.strftime("%A, %d %B").replace(" 0", " ")


def list_times(slots: list[str]) -> str:
    spoken = [format_time(s) for s in slots if s]
    if not spoken:
        return ""
    if len(spoken) == 1:
        return spoken[0]
    return ", ".join(spoken[:-1]) + f" or {spoken[-1]}"


# --------------------------------------------------------------------------
# Failure wording, keyed by the backend's error code
# --------------------------------------------------------------------------
def _failure_sentence(result, language: str, variant: int = 0) -> str:
    code = result.error_code
    data = result.data
    doctor = data.get("doctor_name") or "the doctor"
    date = spoken_date(data.get("date"), language)
    alternatives = data.get("alternative_slots") or []

    if code == ErrorCode.SLOT_UNAVAILABLE:
        asked = spoken_time(data.get("requested_time"), language)
        if alternatives:
            return render("slot_taken", language, variant, doctor=doctor, time=asked,
                          slots=spoken_slots(alternatives, language), date=date)
        return render("no_slots", language, variant, doctor=doctor, date=date)

    if code in (ErrorCode.DOCTOR_UNAVAILABLE, ErrorCode.DOCTOR_NOT_WORKING):
        return render("doctor_unavailable", language, variant, doctor=doctor, date=date)
    if code in (ErrorCode.APPOINTMENT_NOT_FOUND, ErrorCode.NOT_YOUR_APPOINTMENT):
        return render("not_found", language)
    if code == ErrorCode.BACKEND_UNAVAILABLE:
        return render("backend_error", language)

    if code == ErrorCode.DUPLICATE_APPOINTMENT:
        return render("duplicate", language, variant, doctor=doctor)
    if code in (ErrorCode.ALREADY_CANCELLED, ErrorCode.ALREADY_COMPLETED):
        return render("already_cancelled", language, variant)
    if code == ErrorCode.DOCTOR_NOT_FOUND:
        return render("doctor_not_found", language, variant)
    if code in (ErrorCode.INVALID_DATE, ErrorCode.DATE_IN_PAST):
        return render("bad_date", language, variant)
    if code == ErrorCode.INVALID_TIME:
        return render("bad_time", language, variant)
    if code == ErrorCode.PATIENT_NOT_FOUND:
        return render("clarify", language, variant)
    return render("clarify", language, variant)


# --------------------------------------------------------------------------
def build_fallback_response(result, language: str = ENGLISH,
                            variant: int = 0) -> str:
    """
    Turn any OperationResult into a sentence that is guaranteed true.

    Templates exist in English, Roman Urdu and Urdu, so the caller is answered
    in their own language even with the LLM switched off entirely.
    """
    if result is None:
        return render("clarify", language, variant)

    data = result.data
    doctor = data.get("doctor_name") or "the doctor"

    if not result.success:
        return _failure_sentence(result, language, variant)

    operation = result.operation

    if operation == "book":
        return render("booked", language, variant, doctor=doctor,
                      date=spoken_date(data.get("date"), language),
                      time=spoken_time(data.get("time"), language),
                      id=result.appointment_id)

    if operation == "cancel":
        return render("cancelled", language, variant, doctor=doctor,
                      date=spoken_date(data.get("date"), language),
                      time=spoken_time(data.get("time"), language))

    if operation == "reschedule":
        if data.get("unchanged"):
            return render("appointment_details", language, variant, doctor=doctor,
                          date=spoken_date(data.get("date"), language),
                          time=spoken_time(data.get("time"), language))
        return render("rescheduled", language, variant, doctor=doctor,
                      date=spoken_date(data.get("new_date"), language),
                      time=spoken_time(data.get("new_time"), language))

    if operation == "availability":
        slots = data.get("available_slots") or []
        if not slots:
            return render("no_slots", language, variant, doctor=doctor,
                          date=spoken_date(data.get("date"), language))
        return render("availability", language, variant, doctor=doctor,
                      date=spoken_date(data.get("date"), language),
                      slots=spoken_slots(slots[:3], language))

    if operation == "doctor_unavailable":
        count = data.get("cancelled_count", 0)
        return (f"{doctor} has been marked unavailable on "
                f"{spoken_date(data.get('date'), ENGLISH)}. {count} "
                f"appointment{'s' if count != 1 else ''} were cancelled and "
                f"those patients will be notified.")

    if operation == "check":
        appointments = data.get("appointments")
        if appointments is not None:
            if not appointments:
                return render("no_appointments", language, variant)
            first = appointments[0]
            return render("appointment_details", language, variant,
                          doctor=first.get("doctor_name", doctor),
                          date=spoken_date(first.get("date"), language),
                          time=spoken_time(first.get("time"), language))
        if data.get("bookable"):
            return render("confirm_booking", language, variant, doctor=doctor,
                          date=spoken_date(data.get("date"), language),
                          time=spoken_time(data.get("time"), language))
        return render("appointment_details", language, variant, doctor=doctor,
                      date=spoken_date(data.get("date"), language),
                      time=spoken_time(data.get("time"), language))

    return "That is done."
