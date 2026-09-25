"""
Validation gates that run before ANY appointment is written.

This is the module that makes "never trust the LLM" real (spec section 7). Even
if the language model announces that an appointment is booked, nothing reaches
Firestore until every one of these checks has passed against the database.
"""
from __future__ import annotations

import re
from datetime import datetime

from config import ErrorCode, Status, TIMEZONE
from appointment_backend.result import OperationResult

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def today_iso() -> str:
    """Today in Asia/Karachi - never the server's UTC date."""
    return datetime.now(TIMEZONE).date().isoformat()


def parse_date(iso_date: str):
    try:
        return datetime.strptime(str(iso_date), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def to_minutes(time_str: str) -> int | None:
    if not _TIME_RE.match(str(time_str or "")):
        return None
    hour, minute = str(time_str).split(":")
    return int(hour) * 60 + int(minute)


# --------------------------------------------------------------------------
# Individual checks. Each returns None when fine, or a failed OperationResult.
# --------------------------------------------------------------------------
def check_date(iso_date: str, operation: str,
               allow_past: bool = False) -> OperationResult | None:
    parsed = parse_date(iso_date)
    if parsed is None:
        return OperationResult.fail(
            operation, ErrorCode.INVALID_DATE,
            f"'{iso_date}' is not a valid date. Expected YYYY-MM-DD.")
    if not allow_past and parsed.isoformat() < today_iso():
        return OperationResult.fail(
            operation, ErrorCode.DATE_IN_PAST,
            f"{iso_date} has already passed.")
    return None


def check_time(time_str: str, operation: str) -> OperationResult | None:
    if to_minutes(time_str) is None:
        return OperationResult.fail(
            operation, ErrorCode.INVALID_TIME,
            f"'{time_str}' is not a valid time. Expected 24-hour HH:MM.")
    return None


def check_patient(patient_service, patient_id: str,
                  operation: str) -> OperationResult | None:
    result = patient_service.get_patient(patient_id)
    if result.ok:
        return None
    if result.error == "BACKEND_UNAVAILABLE":
        return OperationResult.fail(operation, ErrorCode.BACKEND_UNAVAILABLE,
                                    result.message or "Database unreachable.")
    return OperationResult.fail(operation, ErrorCode.PATIENT_NOT_FOUND,
                                f"No patient with ID {patient_id}.")


def check_doctor(doctor_service, doctor_id: str, operation: str):
    """Returns (failure_result, doctor_record) - exactly one is not None."""
    result = doctor_service.get_doctor(doctor_id)
    if result.ok:
        if not result.data.get("active", True):
            return OperationResult.fail(
                operation, ErrorCode.DOCTOR_NOT_FOUND,
                f"{result.data.get('name', doctor_id)} is not accepting "
                f"appointments."), None
        return None, result.data
    if result.error == "BACKEND_UNAVAILABLE":
        return OperationResult.fail(operation, ErrorCode.BACKEND_UNAVAILABLE,
                                    result.message or "Database unreachable."), None
    return OperationResult.fail(operation, ErrorCode.DOCTOR_NOT_FOUND,
                                f"No doctor with ID {doctor_id}."), None


def check_clinic(clinic_service, clinic_id: str | None, doctor: dict,
                 operation: str):
    """
    Resolve the clinic. A missing clinic_id is not an error - we fall back to
    the doctor's own clinic, which is what a caller means in practice.
    """
    if clinic_id:
        result = clinic_service.get_clinic(clinic_id)
        if result.ok:
            return None, result.data
        return OperationResult.fail(
            operation, ErrorCode.CLINIC_NOT_FOUND,
            f"No clinic with ID {clinic_id}."), None

    result = clinic_service.get_clinic_for_doctor(doctor)
    if result.ok:
        return None, result.data
    return OperationResult.fail(
        operation, ErrorCode.CLINIC_NOT_FOUND,
        f"{doctor.get('name')} has no clinic on record."), None


def check_ownership(appointment: dict, patient_id: str | None,
                    operation: str) -> OperationResult | None:
    """
    A caller may only touch their own appointment (spec section 10).

    `patient_id` of None means the operation was invoked without an identified
    caller (an internal/dashboard call), which we allow.
    """
    if patient_id is None:
        return None
    if appointment.get("patient_id") != patient_id:
        return OperationResult.fail(
            operation, ErrorCode.NOT_YOUR_APPOINTMENT,
            "That appointment belongs to a different patient.")
    return None


def check_mutable(appointment: dict, operation: str) -> OperationResult | None:
    """Cancelled or completed appointments cannot be changed again."""
    status = appointment.get("status")
    if status in (Status.CANCELLED, Status.CANCELLED_BY_DOCTOR):
        return OperationResult.fail(
            operation, ErrorCode.ALREADY_CANCELLED,
            "That appointment has already been cancelled.")
    if status == Status.COMPLETED:
        return OperationResult.fail(
            operation, ErrorCode.ALREADY_COMPLETED,
            "That appointment has already taken place.")
    return None


def check_duplicate(repository, collection: str, patient_id: str,
                    doctor_id: str, iso_date: str,
                    operation: str) -> OperationResult | None:
    """
    Stop the same patient booking the same doctor twice on one day.

    A caller who repeats themselves - common on a noisy phone line - should not
    end up with two appointments.
    """
    from firebase.appointment_service import ACTIVE_STATUSES

    try:
        existing = repository.query(collection, [
            ("patient_id", "==", patient_id),
            ("doctor_id", "==", doctor_id),
            ("date", "==", iso_date),
        ])
    except Exception:
        return None                     # availability checks still protect us
    for appointment in existing:
        if appointment.get("status") in ACTIVE_STATUSES:
            return OperationResult.fail(
                operation, ErrorCode.DUPLICATE_APPOINTMENT,
                f"You already have an appointment with that doctor on "
                f"{iso_date} at {appointment.get('time')}.",
                existing_appointment=appointment)
    return None
