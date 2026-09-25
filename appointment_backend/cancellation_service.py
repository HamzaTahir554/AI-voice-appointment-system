"""
Cancellation - by the patient, or in bulk when a doctor blocks a day.

Nothing is ever deleted. A cancelled appointment keeps its document and simply
changes status, so the clinic retains a full history (spec sections 10 and 14).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from config import Collections, ErrorCode, Status, TIMEZONE
from appointment_backend.result import OperationResult
from appointment_backend.validation import check_mutable, check_ownership
from firebase.appointment_service import ACTIVE_STATUSES
from firebase.doctor_service import DoctorService
from firebase.firebase_config import DatabaseError, get_repository
from firebase.schedule_service import ScheduleService

logger = logging.getLogger(__name__)

OPERATION = "cancel"


class CancellationService:
    def __init__(self, repository=None):
        self.repo = repository or get_repository()
        self.doctors = DoctorService(self.repo)
        self.schedules = ScheduleService(self.repo)

    # ------------------------------------------------------------------
    def _get(self, appointment_id: str):
        """Returns (failure_result, appointment)."""
        if not appointment_id:
            return OperationResult.fail(
                OPERATION, ErrorCode.APPOINTMENT_NOT_FOUND,
                "No appointment ID was given."), None
        try:
            doc = self.repo.get(Collections.APPOINTMENTS,
                                str(appointment_id).upper())
        except DatabaseError as exc:
            logger.error("lookup failed: %s", exc)
            return OperationResult.fail(
                OPERATION, ErrorCode.BACKEND_UNAVAILABLE,
                "Could not reach the appointment system."), None
        if doc is None:
            return OperationResult.fail(
                OPERATION, ErrorCode.APPOINTMENT_NOT_FOUND,
                f"No appointment with ID {appointment_id}."), None
        return None, doc

    # ------------------------------------------------------------------
    def cancel(self, appointment_id: str,
               patient_id: str | None = None) -> OperationResult:
        """Cancel one appointment on the caller's request."""
        failure, appointment = self._get(appointment_id)
        if failure is not None:
            return failure

        # A caller may only cancel their own booking.
        failure = check_ownership(appointment, patient_id, OPERATION)
        if failure is not None:
            return failure

        failure = check_mutable(appointment, OPERATION)
        if failure is not None:
            return failure

        doctor_name = self._doctor_name(appointment.get("doctor_id"))
        try:
            updated = self.repo.update(
                Collections.APPOINTMENTS, appointment["appointment_id"],
                {"status": Status.CANCELLED,
                 "cancelled_at": datetime.now(TIMEZONE).isoformat(),
                 "cancellation_reason": "Cancelled by patient"})
        except DatabaseError as exc:
            logger.error("cancel failed: %s", exc)
            return OperationResult.fail(OPERATION, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not reach the appointment system.")

        return OperationResult.ok(
            OPERATION, appointment_id=appointment["appointment_id"],
            status=Status.CANCELLED, appointment=updated,
            doctor_name=doctor_name, date=appointment.get("date"),
            time=appointment.get("time"))

    # ------------------------------------------------------------------
    def cancel_by_clinic(self, appointment_id: str,
                         reason: str = "Cancelled by the clinic") -> OperationResult:
        """
        The clinic cancels one appointment (administration dashboard).

        The rules are the patient's own cancel - the record is kept and only
        the status changes - but the stored reason says who really did it,
        and the patient is told through the same queue the doctor-unavailable
        cascade uses. Sending those messages is still not implemented.
        """
        result = self.cancel(appointment_id)
        if not result.success:
            return result

        appointment = (result.data or {}).get("appointment") or {}
        now = datetime.now(TIMEZONE).isoformat()
        try:
            updated = self.repo.update(
                Collections.APPOINTMENTS, appointment_id,
                {"cancellation_reason": reason})
        except DatabaseError as exc:
            logger.error("could not record the cancellation reason: %s", exc)
            updated = appointment

        notification = None
        doctor_id = appointment.get("doctor_id")
        found = self.doctors.get_doctor(doctor_id) if doctor_id else None
        if found is not None and found.ok:
            notification = self._queue_notification(appointment, found.data,
                                                    reason, now,
                                                    kind="clinic_cancelled")

        return OperationResult.ok(
            result.operation, appointment_id=appointment_id,
            status=Status.CANCELLED, appointment=updated,
            doctor_name=(result.data or {}).get("doctor_name"),
            date=appointment.get("date"), time=appointment.get("time"),
            notifications=[notification] if notification else [])

    # ------------------------------------------------------------------
    def cancel_day_for_doctor(self, doctor_id: str, iso_date: str,
                              reason: str = "Doctor unavailable"
                              ) -> OperationResult:
        """
        The doctor blocks a whole day from their dashboard (spec section 14).

        Marks the date unavailable, cancels every live appointment on it with
        status `cancelled_by_doctor`, and queues a notification per patient so
        the SMS/voice layer can tell them. Records are never deleted.
        """
        operation = "doctor_unavailable"

        failure, doctor = self._doctor_record(doctor_id, operation)
        if failure is not None:
            return failure

        blocked = self.schedules.mark_unavailable(doctor_id, iso_date, reason)
        if not blocked.ok:
            return OperationResult.fail(
                operation, ErrorCode.BACKEND_UNAVAILABLE,
                blocked.message or "Could not record the doctor's leave.")

        try:
            affected = self.repo.query(Collections.APPOINTMENTS, [
                ("doctor_id", "==", doctor_id), ("date", "==", iso_date)])
        except DatabaseError as exc:
            logger.error("cancel_day query failed: %s", exc)
            return OperationResult.fail(operation, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not read the affected appointments.")

        cancelled, notifications = [], []
        now = datetime.now(TIMEZONE).isoformat()
        for appointment in affected:
            if appointment.get("status") not in ACTIVE_STATUSES:
                continue
            try:
                updated = self.repo.update(
                    Collections.APPOINTMENTS, appointment["appointment_id"],
                    {"status": Status.CANCELLED_BY_DOCTOR,
                     "cancelled_at": now, "cancellation_reason": reason})
            except DatabaseError as exc:
                # One bad write must not abandon the remaining patients.
                logger.error("could not cancel %s: %s",
                             appointment.get("appointment_id"), exc)
                continue
            cancelled.append(updated)
            notifications.append(
                self._queue_notification(updated, doctor, reason, now))

        return OperationResult.ok(
            operation, status=Status.CANCELLED_BY_DOCTOR,
            doctor_id=doctor_id, doctor_name=doctor.get("name"),
            date=iso_date, reason=reason,
            cancelled_count=len(cancelled),
            cancelled_appointments=cancelled,
            notifications=notifications)

    # ------------------------------------------------------------------
    def _queue_notification(self, appointment: dict, doctor: dict,
                            reason: str, now: str,
                            kind: str = "doctor_unavailable") -> dict:
        """
        Write a notification record for the SMS / voice-call service.

        The backend does not send anything itself - it records what needs
        sending, so delivery can be retried independently. The wording says
        three things the patient has to know: the appointment is gone, why,
        and that rebooking means calling again.
        """
        notification_id = f"N{uuid.uuid4().hex[:8].upper()}"
        name = doctor.get("name", "the doctor")
        date, time = appointment.get("date"), appointment.get("time")
        if kind == "doctor_unavailable":
            message = (f"{name} is not available on {date}, so your "
                       f"appointment at {time} has been cancelled "
                       f"({reason}). Please call again to book another date.")
        else:
            message = (f"Your appointment with {name} on {date} at {time} "
                       f"has been cancelled ({reason}). Please call again to "
                       f"book another time.")
        record = {
            "notification_id": notification_id,
            "patient_id": appointment.get("patient_id"),
            "appointment_id": appointment.get("appointment_id"),
            "doctor_id": appointment.get("doctor_id"),
            "type": ("appointment_cancelled_by_doctor"
                     if kind == "doctor_unavailable"
                     else "appointment_cancelled_by_clinic"),
            "channel": "sms+voice",
            "status": "pending",
            "date": date,
            "time": time,
            "message": message,
            "created_at": now,
        }
        try:
            self.repo.set(Collections.NOTIFICATIONS, notification_id, record)
        except DatabaseError as exc:
            logger.error("could not queue notification: %s", exc)
        return record

    def _doctor_record(self, doctor_id: str, operation: str):
        result = self.doctors.get_doctor(doctor_id)
        if result.ok:
            return None, result.data
        return OperationResult.fail(
            operation, ErrorCode.DOCTOR_NOT_FOUND,
            f"No doctor with ID {doctor_id}."), None

    def _doctor_name(self, doctor_id: str | None) -> str:
        if not doctor_id:
            return "the doctor"
        found = self.doctors.get_doctor(doctor_id)
        return found.data.get("name", "the doctor") if found.ok else "the doctor"
