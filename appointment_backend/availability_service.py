"""
Availability: what a doctor actually offers, minus what is already taken.

Every "is X free?" question in the system resolves here, so availability is
computed from the database in one place rather than guessed anywhere else.
"""
from __future__ import annotations

import logging

from config import Collections, ErrorCode
from appointment_backend.result import OperationResult
from appointment_backend.validation import check_date, check_doctor, to_minutes
from firebase.appointment_service import ACTIVE_STATUSES
from firebase.doctor_service import DoctorService
from firebase.firebase_config import get_repository
from firebase.schedule_service import ScheduleService

logger = logging.getLogger(__name__)

# Maps the schedule layer's error strings onto the backend's public codes.
_SCHEDULE_ERRORS = {
    "INVALID_DATE": ErrorCode.INVALID_DATE,
    "DATE_IN_PAST": ErrorCode.DATE_IN_PAST,
    "DOCTOR_UNAVAILABLE": ErrorCode.DOCTOR_UNAVAILABLE,
    "NO_SCHEDULE": ErrorCode.DOCTOR_NOT_WORKING,
    "BACKEND_UNAVAILABLE": ErrorCode.BACKEND_UNAVAILABLE,
}


class AvailabilityService:
    def __init__(self, repository=None, schedule_service=None,
                 doctor_service=None):
        self.repo = repository or get_repository()
        self.schedules = schedule_service or ScheduleService(self.repo)
        self.doctors = doctor_service or DoctorService(self.repo)

    # ------------------------------------------------------------------
    def booked_times(self, doctor_id: str, iso_date: str) -> set[str]:
        """Times already taken by a live appointment."""
        try:
            rows = self.repo.query(Collections.APPOINTMENTS, [
                ("doctor_id", "==", doctor_id), ("date", "==", iso_date)])
        except Exception as exc:
            logger.error("booked_times failed: %s", exc)
            return set()
        return {r.get("time") for r in rows
                if r.get("status") in ACTIVE_STATUSES}

    # ------------------------------------------------------------------
    def get_availability(self, doctor_id: str, iso_date: str,
                         operation: str = "availability") -> OperationResult:
        """
        Free slots for a doctor on a date.

        Applies, in order: valid date -> doctor exists -> not on leave ->
        holds clinic that weekday -> minus existing bookings.
        """
        invalid = check_date(iso_date, operation)
        if invalid is not None:
            return invalid

        failure, doctor = check_doctor(self.doctors, doctor_id, operation)
        if failure is not None:
            return failure

        offered = self.schedules.generate_slots(doctor_id, iso_date)
        if not offered.ok:
            code = _SCHEDULE_ERRORS.get(offered.error, ErrorCode.DOCTOR_UNAVAILABLE)
            return OperationResult.fail(
                operation, code,
                offered.message or "The doctor is not available that day.",
                doctor_name=doctor.get("name"), date=iso_date,
                available=False, available_slots=[])

        taken = self.booked_times(doctor_id, iso_date)
        free = [slot for slot in offered.data if slot not in taken]
        return OperationResult.ok(
            operation,
            doctor_id=doctor_id, doctor_name=doctor.get("name"),
            date=iso_date, available=bool(free), available_slots=free,
            total_slots=len(offered.data), booked_slots=len(taken))

    # ------------------------------------------------------------------
    def is_slot_free(self, doctor_id: str, iso_date: str, time_str: str,
                     operation: str = "availability"):
        """
        Returns (failure_result, is_free).

        A failure means we could not even ask the question - a bad date, a
        doctor on leave - which is different from "asked, and it is taken".
        """
        availability = self.get_availability(doctor_id, iso_date, operation)
        if not availability.success:
            return availability, False
        return None, time_str in availability.data.get("available_slots", [])

    # ------------------------------------------------------------------
    def alternatives(self, doctor_id: str, iso_date: str,
                     around: str | None = None, limit: int = 3) -> list[str]:
        """
        Free slots nearest the time the caller asked for (spec section 9).

        Nearest-first matters on a voice call: offering 4:20 to someone who
        asked for 4:00 is far more useful than offering the first free slot of
        the day.
        """
        availability = self.get_availability(doctor_id, iso_date)
        if not availability.success:
            return []
        slots = list(availability.data.get("available_slots", []))
        target = to_minutes(around) if around else None
        if target is not None and slots:
            slots.sort(key=lambda s: abs((to_minutes(s) or 0) - target))
        return slots[:limit]
