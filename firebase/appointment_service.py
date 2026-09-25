"""
Appointment service - `appointments` collection.

This layer answers "can this operation actually be performed?" and records the
result. Every fact the assistant speaks (availability, appointment IDs, times)
comes from here, never from the dialog layer and never from a language model.

Cancellation sets `status = "cancelled"`; records are never deleted, so the
clinic keeps its history.
"""
from __future__ import annotations

import logging
import uuid

from config import Collections, Status
from firebase.firebase_config import DatabaseError, SlotConflict, get_repository
from firebase.result import ServiceResult
from firebase.schedule_service import ScheduleService, today_iso

logger = logging.getLogger(__name__)

# Statuses that still occupy a slot in the doctor's diary.
ACTIVE_STATUSES = (Status.PENDING, Status.CONFIRMED, Status.RESCHEDULED)


class AppointmentService:
    def __init__(self, repository=None, schedule_service=None):
        self.repo = repository or get_repository()
        self.schedules = schedule_service or ScheduleService(self.repo)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_appointment(self, appointment_id: str) -> ServiceResult:
        if not appointment_id:
            return ServiceResult.failure("APPOINTMENT_NOT_FOUND", "no id given")
        try:
            doc = self.repo.get(Collections.APPOINTMENTS,
                                str(appointment_id).upper())
        except DatabaseError as exc:
            logger.error("get_appointment failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if doc is None:
            return ServiceResult.failure(
                "APPOINTMENT_NOT_FOUND",
                f"No appointment with ID {appointment_id}")
        return ServiceResult.success(doc)

    def get_patient_appointments(self, patient_id: str,
                                 upcoming_only: bool = True) -> ServiceResult:
        """Appointments for a caller, soonest first."""
        try:
            docs = self.repo.query(Collections.APPOINTMENTS,
                                   [("patient_id", "==", patient_id)])
        except DatabaseError as exc:
            logger.error("get_patient_appointments failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if upcoming_only:
            today = today_iso()
            docs = [d for d in docs
                    if d.get("status") in ACTIVE_STATUSES
                    and str(d.get("date", "")) >= today]
        docs.sort(key=lambda d: (d.get("date", ""), d.get("time", "")))
        return ServiceResult.success(docs)

    def _booked_times(self, doctor_id: str, iso_date: str) -> set[str]:
        try:
            docs = self.repo.query(
                Collections.APPOINTMENTS,
                [("doctor_id", "==", doctor_id), ("date", "==", iso_date)])
        except DatabaseError:
            return set()
        return {d.get("time") for d in docs
                if d.get("status") in ACTIVE_STATUSES}

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    def get_available_slots(self, doctor_id: str,
                            iso_date: str) -> ServiceResult:
        """Slots the doctor offers minus the ones already taken."""
        offered = self.schedules.generate_slots(doctor_id, iso_date)
        if not offered.ok:
            return offered
        taken = self._booked_times(doctor_id, iso_date)
        return ServiceResult.success([s for s in offered.data if s not in taken])

    def check_availability(self, doctor_id: str, iso_date: str,
                           time_str: str) -> ServiceResult:
        """`data` is True/False when the question could be answered at all."""
        available = self.get_available_slots(doctor_id, iso_date)
        if not available.ok:
            return available
        return ServiceResult.success(time_str in available.data)

    def suggest_alternatives(self, doctor_id: str, iso_date: str,
                             around: str | None = None,
                             limit: int = 3) -> ServiceResult:
        """The free slots nearest to the time the caller asked for."""
        available = self.get_available_slots(doctor_id, iso_date)
        if not available.ok:
            return available
        slots = available.data
        if around and slots:
            target = self._minutes(around)
            slots = sorted(slots, key=lambda s: abs(self._minutes(s) - target))
        return ServiceResult.success(slots[:limit])

    @staticmethod
    def _minutes(time_str: str) -> int:
        try:
            hour, minute = str(time_str).split(":")
            return int(hour) * 60 + int(minute)
        except (ValueError, AttributeError):
            return 0

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    @staticmethod
    def _new_id() -> str:
        # Short, readable, and unique enough to be spoken back to a caller.
        return f"APT{uuid.uuid4().hex[:6].upper()}"

    def book_appointment(self, patient_id: str, doctor_id: str, iso_date: str,
                         time_str: str, clinic_id: str | None = None
                         ) -> ServiceResult:
        """
        Create an appointment, refusing to double-book.

        Availability is validated first for a friendly error, then the write
        goes through `reserve_slot`, which re-checks inside a Firestore
        transaction. Two callers racing for the same 4 PM slot cannot both win.
        """
        availability = self.check_availability(doctor_id, iso_date, time_str)
        if not availability.ok:
            return availability
        if not availability.data:
            return ServiceResult.failure("SLOT_TAKEN",
                                         "That slot is not available")

        appointment_id = self._new_id()
        record = {
            "appointment_id": appointment_id,
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "clinic_id": clinic_id,
            "date": iso_date,
            "time": time_str,
            "status": Status.CONFIRMED,
        }
        try:
            saved = self.repo.reserve_slot(
                Collections.APPOINTMENTS, appointment_id, record,
                conflict_filters=[
                    ("doctor_id", "==", doctor_id),
                    ("date", "==", iso_date),
                    ("time", "==", time_str),
                    ("status", "in", list(ACTIVE_STATUSES)),
                ])
        except SlotConflict:
            # Someone booked it between our check and our write.
            return ServiceResult.failure("SLOT_TAKEN",
                                         "That slot was just taken")
        except DatabaseError as exc:
            logger.error("book_appointment failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def cancel_appointment(self, appointment_id: str) -> ServiceResult:
        """Soft-cancel: the record stays, the status changes."""
        found = self.get_appointment(appointment_id)
        if not found.ok:
            return found
        appointment = found.data
        if appointment.get("status") == Status.CANCELLED:
            return ServiceResult.failure("ALREADY_CANCELLED",
                                         "That appointment is already cancelled")
        try:
            updated = self.repo.update(Collections.APPOINTMENTS,
                                       appointment["appointment_id"],
                                       {"status": Status.CANCELLED})
        except DatabaseError as exc:
            logger.error("cancel_appointment failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(updated)

    def reschedule_appointment(self, appointment_id: str, iso_date: str,
                               time_str: str) -> ServiceResult:
        found = self.get_appointment(appointment_id)
        if not found.ok:
            return found
        appointment = found.data
        if appointment.get("status") == Status.CANCELLED:
            return ServiceResult.failure(
                "ALREADY_CANCELLED",
                "That appointment was cancelled and cannot be moved")

        doctor_id = appointment["doctor_id"]
        availability = self.check_availability(doctor_id, iso_date, time_str)
        if not availability.ok:
            return availability
        if not availability.data:
            return ServiceResult.failure("SLOT_TAKEN",
                                         "That new slot is not available")
        try:
            updated = self.repo.update(
                Collections.APPOINTMENTS, appointment["appointment_id"],
                {
                    "date": iso_date,
                    "time": time_str,
                    "status": Status.RESCHEDULED,
                    # Keep an audit trail of where it moved from.
                    "previous_date": appointment.get("date"),
                    "previous_time": appointment.get("time"),
                })
        except DatabaseError as exc:
            logger.error("reschedule_appointment failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(updated)
