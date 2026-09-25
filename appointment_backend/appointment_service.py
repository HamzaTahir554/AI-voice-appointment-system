"""
The Appointment Backend facade.

One object the Dialog Manager (and the API) calls for every appointment
operation. It answers exactly one question:

    "Can this appointment actually be booked / cancelled / rescheduled?"

It is deterministic and database-driven. No language model participates in any
decision made here - the LLM only rephrases the result afterwards.
"""
from __future__ import annotations

import logging
from datetime import datetime

from config import Collections, ErrorCode, Status, TIMEZONE
from appointment_backend.availability_service import AvailabilityService
from appointment_backend.booking_service import BookingService
from appointment_backend.cancellation_service import CancellationService
from appointment_backend.reschedule_service import RescheduleService
from appointment_backend.result import OperationResult
from appointment_backend.validation import (
    check_date, check_mutable, check_ownership, today_iso,
)
from firebase.appointment_service import ACTIVE_STATUSES
from firebase.clinic_service import ClinicService
from firebase.doctor_service import DoctorService
from firebase.firebase_config import DatabaseError, get_repository

logger = logging.getLogger(__name__)


class AppointmentBackend:
    """Deterministic business rules over the Firestore data."""

    def __init__(self, repository=None):
        self.repo = repository or get_repository()
        self.availability = AvailabilityService(self.repo)
        self.booking = BookingService(self.repo, self.availability)
        self.cancellation = CancellationService(self.repo)
        self.rescheduling = RescheduleService(self.repo, self.availability)
        self.doctors = DoctorService(self.repo)
        self.clinics = ClinicService(self.repo)

    # ==================================================================
    # Mutations
    # ==================================================================
    def book_appointment(self, patient_id: str, doctor_id: str, date: str,
                         time: str, clinic_id: str | None = None,
                         validate_patient: bool = True) -> OperationResult:
        return self.booking.book(patient_id, doctor_id, date, time, clinic_id,
                                 validate_patient=validate_patient)

    def cancel_appointment(self, appointment_id: str,
                           patient_id: str | None = None) -> OperationResult:
        return self.cancellation.cancel(appointment_id, patient_id)

    def clinic_cancel_appointment(self, appointment_id: str,
                                  reason: str = "Cancelled by the clinic"
                                  ) -> OperationResult:
        """Cancellation made by the clinic rather than by the patient: same
        rules, honest reason, and the patient is added to the message queue."""
        return self.cancellation.cancel_by_clinic(appointment_id, reason)

    def reschedule_appointment(self, appointment_id: str, new_date: str,
                               new_time: str,
                               patient_id: str | None = None) -> OperationResult:
        return self.rescheduling.reschedule(appointment_id, new_date,
                                            new_time, patient_id)

    def mark_doctor_unavailable(self, doctor_id: str, date: str,
                                reason: str = "Doctor unavailable"
                                ) -> OperationResult:
        """Doctor dashboard: block a day and cancel everything on it."""
        return self.cancellation.cancel_day_for_doctor(doctor_id, date, reason)

    def complete_appointment(self, appointment_id: str,
                             doctor_id: str | None = None) -> OperationResult:
        """
        Mark a consultation as finished (doctor dashboard).

        Added for the dashboard: the voice assistant books, cancels and
        reschedules, but only the doctor can say a consultation happened.
        Nothing is deleted and an already cancelled or completed appointment
        is refused by the same rule that guards cancel and reschedule.

        `doctor_id` scopes the operation: a doctor may only close their own
        appointments.
        """
        operation = "complete"
        if not appointment_id:
            return OperationResult.fail(operation, ErrorCode.APPOINTMENT_NOT_FOUND,
                                        "No appointment ID was given.")
        try:
            doc = self.repo.get(Collections.APPOINTMENTS, str(appointment_id).upper())
        except DatabaseError as exc:
            logger.error("complete_appointment failed: %s", exc)
            return OperationResult.fail(operation, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not reach the appointment system.")
        if doc is None:
            return OperationResult.fail(operation, ErrorCode.APPOINTMENT_NOT_FOUND,
                                        f"No appointment with ID {appointment_id}.")
        if doctor_id and doc.get("doctor_id") != doctor_id:
            return OperationResult.fail(operation, ErrorCode.NOT_YOUR_APPOINTMENT,
                                        "That appointment belongs to another doctor.")

        failure = check_mutable(doc, operation)
        if failure is not None:
            return failure

        try:
            updated = self.repo.update(
                Collections.APPOINTMENTS, doc["appointment_id"],
                {"status": Status.COMPLETED,
                 "completed_at": datetime.now(TIMEZONE).isoformat()})
        except DatabaseError as exc:
            logger.error("complete_appointment write failed: %s", exc)
            return OperationResult.fail(operation, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not update the appointment.")
        return OperationResult.ok(operation, appointment_id=updated["appointment_id"],
                                  status=Status.COMPLETED, appointment=updated)

    # ==================================================================
    # Reads
    # ==================================================================
    def get_appointment(self, appointment_id: str,
                        patient_id: str | None = None) -> OperationResult:
        operation = "check"
        if not appointment_id:
            return OperationResult.fail(operation,
                                        ErrorCode.APPOINTMENT_NOT_FOUND,
                                        "No appointment ID was given.")
        try:
            doc = self.repo.get(Collections.APPOINTMENTS,
                                str(appointment_id).upper())
        except DatabaseError as exc:
            logger.error("get_appointment failed: %s", exc)
            return OperationResult.fail(operation, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not reach the appointment system.")
        if doc is None:
            return OperationResult.fail(
                operation, ErrorCode.APPOINTMENT_NOT_FOUND,
                f"No appointment with ID {appointment_id}.")

        failure = check_ownership(doc, patient_id, operation)
        if failure is not None:
            return failure

        return OperationResult.ok(
            operation, appointment_id=doc["appointment_id"],
            status=doc.get("status"), appointment=doc,
            doctor_name=self._doctor_name(doc.get("doctor_id")),
            clinic_name=self._clinic_name(doc.get("clinic_id")),
            date=doc.get("date"), time=doc.get("time"))

    def get_patient_appointments(self, patient_id: str,
                                 upcoming_only: bool = True) -> OperationResult:
        operation = "check"
        try:
            rows = self.repo.query(Collections.APPOINTMENTS,
                                   [("patient_id", "==", patient_id)])
        except DatabaseError as exc:
            logger.error("get_patient_appointments failed: %s", exc)
            return OperationResult.fail(operation, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not reach the appointment system.")
        if upcoming_only:
            today = today_iso()
            rows = [r for r in rows
                    if r.get("status") in ACTIVE_STATUSES
                    and str(r.get("date", "")) >= today]
        rows.sort(key=lambda r: (r.get("date", ""), r.get("time", "")))

        # Attach the doctor's name so the caller never has to look it up.
        for row in rows:
            row["doctor_name"] = self._doctor_name(row.get("doctor_id"))

        return OperationResult.ok(operation, appointments=rows,
                                  count=len(rows), patient_id=patient_id)

    def get_doctor_appointments(self, doctor_id: str,
                                date: str | None = None) -> OperationResult:
        operation = "check"
        filters = [("doctor_id", "==", doctor_id)]
        if date:
            filters.append(("date", "==", date))
        try:
            rows = self.repo.query(Collections.APPOINTMENTS, filters)
        except DatabaseError as exc:
            return OperationResult.fail(operation, ErrorCode.BACKEND_UNAVAILABLE,
                                        str(exc))
        rows.sort(key=lambda r: (r.get("date", ""), r.get("time", "")))
        return OperationResult.ok(operation, appointments=rows,
                                  count=len(rows), doctor_id=doctor_id)

    def get_availability(self, doctor_id: str, date: str) -> OperationResult:
        return self.availability.get_availability(doctor_id, date)

    # ==================================================================
    # Dry run
    # ==================================================================
    def check_appointment(self, doctor_id: str, date: str,
                          time: str) -> OperationResult:
        """
        "Could this be booked?" without writing anything (POST /appointments/check).

        The Dialog Manager uses this to confirm details with the caller before
        asking them to say yes.
        """
        operation = "check"
        failure = check_date(date, operation)
        if failure is not None:
            return failure

        problem, is_free = self.availability.is_slot_free(
            doctor_id, date, time, operation)
        if problem is not None:
            return problem
        if not is_free:
            return OperationResult.fail(
                operation, ErrorCode.SLOT_UNAVAILABLE,
                f"{time} on {date} is already booked.",
                doctor_id=doctor_id, date=date, requested_time=time,
                alternative_slots=self.availability.alternatives(
                    doctor_id, date, around=time))
        return OperationResult.ok(
            operation, status=Status.PENDING, bookable=True,
            doctor_id=doctor_id, doctor_name=self._doctor_name(doctor_id),
            date=date, time=time)

    # ==================================================================
    def _doctor_name(self, doctor_id: str | None) -> str:
        if not doctor_id:
            return "the doctor"
        found = self.doctors.get_doctor(doctor_id)
        return found.data.get("name", "the doctor") if found.ok else "the doctor"

    def _clinic_name(self, clinic_id: str | None) -> str | None:
        """The clinic's CURRENT name, whatever id the record was written with.

        There is one clinic, so an appointment booked before it was renamed
        still shows the name patients are told today.
        """
        found = self.clinics.primary()
        return found.data.get("name") if found.ok else None
