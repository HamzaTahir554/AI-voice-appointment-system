"""
Rescheduling - move an existing appointment to a new date/time.

The new slot is validated exactly as strictly as a fresh booking, and the old
date/time is kept on the record as an audit trail (spec section 11).
"""
from __future__ import annotations

import logging
from datetime import datetime

from config import Collections, ErrorCode, Status, TIMEZONE
from appointment_backend.availability_service import AvailabilityService
from appointment_backend.result import OperationResult
from appointment_backend.validation import (
    check_date,
    check_mutable,
    check_ownership,
    check_time,
)
from firebase.doctor_service import DoctorService
from firebase.firebase_config import DatabaseError, get_repository

logger = logging.getLogger(__name__)

OPERATION = "reschedule"


class RescheduleService:
    def __init__(self, repository=None, availability=None):
        self.repo = repository or get_repository()
        self.availability = availability or AvailabilityService(self.repo)
        self.doctors = DoctorService(self.repo)

    # ------------------------------------------------------------------
    def reschedule(self, appointment_id: str, new_date: str, new_time: str,
                   patient_id: str | None = None) -> OperationResult:
        if not appointment_id:
            return OperationResult.fail(
                OPERATION, ErrorCode.APPOINTMENT_NOT_FOUND,
                "No appointment ID was given.")
        try:
            appointment = self.repo.get(Collections.APPOINTMENTS,
                                        str(appointment_id).upper())
        except DatabaseError as exc:
            logger.error("lookup failed: %s", exc)
            return OperationResult.fail(OPERATION, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not reach the appointment system.")
        if appointment is None:
            return OperationResult.fail(
                OPERATION, ErrorCode.APPOINTMENT_NOT_FOUND,
                f"No appointment with ID {appointment_id}.")

        failure = check_ownership(appointment, patient_id, OPERATION)
        if failure is not None:
            return failure
        failure = check_mutable(appointment, OPERATION)
        if failure is not None:
            return failure
        failure = check_date(new_date, OPERATION)
        if failure is not None:
            return failure
        failure = check_time(new_time, OPERATION)
        if failure is not None:
            return failure

        old_date, old_time = appointment.get("date"), appointment.get("time")
        if (new_date, new_time) == (old_date, old_time):
            # Nothing to do; say so rather than writing a pointless update.
            return OperationResult.ok(
                OPERATION, appointment_id=appointment["appointment_id"],
                status=appointment.get("status"), appointment=appointment,
                unchanged=True, date=new_date, time=new_time,
                doctor_name=self._doctor_name(appointment.get("doctor_id")))

        doctor_id = appointment["doctor_id"]
        doctor_name = self._doctor_name(doctor_id)

        # The new slot faces exactly the same gate as a new booking.
        problem, is_free = self.availability.is_slot_free(
            doctor_id, new_date, new_time, OPERATION)
        if problem is not None:
            problem.data.setdefault("doctor_name", doctor_name)
            return problem
        if not is_free:
            alternatives = self.availability.alternatives(
                doctor_id, new_date, around=new_time)
            return OperationResult.fail(
                OPERATION, ErrorCode.SLOT_UNAVAILABLE,
                f"{new_time} on {new_date} is already booked.",
                doctor_id=doctor_id, doctor_name=doctor_name,
                date=new_date, requested_time=new_time,
                alternative_slots=alternatives)

        try:
            updated = self.repo.update(
                Collections.APPOINTMENTS, appointment["appointment_id"], {
                    "date": new_date,
                    "time": new_time,
                    "status": Status.RESCHEDULED,
                    # Audit trail: where it moved from.
                    "previous_date": old_date,
                    "previous_time": old_time,
                    "rescheduled_at": datetime.now(TIMEZONE).isoformat(),
                })
        except DatabaseError as exc:
            logger.error("reschedule failed: %s", exc)
            return OperationResult.fail(OPERATION, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not reach the appointment system.")

        return OperationResult.ok(
            OPERATION, appointment_id=appointment["appointment_id"],
            status=Status.RESCHEDULED, appointment=updated,
            doctor_id=doctor_id, doctor_name=doctor_name,
            old_date=old_date, old_time=old_time,
            new_date=new_date, new_time=new_time,
            date=new_date, time=new_time)

    # ------------------------------------------------------------------
    def _doctor_name(self, doctor_id: str | None) -> str:
        if not doctor_id:
            return "the doctor"
        found = self.doctors.get_doctor(doctor_id)
        return found.data.get("name", "the doctor") if found.ok else "the doctor"
