"""
Booking - the only code path in the system that creates an appointment.

Runs the full validation gate (spec section 6), then writes through a Firestore
transaction so two callers racing for the same slot cannot both win.
"""
from __future__ import annotations

import logging
import uuid

from config import APPOINTMENT_ID_PREFIX, Collections, ErrorCode, Status
from appointment_backend.availability_service import AvailabilityService
from appointment_backend.result import OperationResult
from appointment_backend.validation import (
    check_clinic,
    check_date,
    check_doctor,
    check_duplicate,
    check_patient,
    check_time,
)
from firebase.appointment_service import ACTIVE_STATUSES
from firebase.clinic_service import ClinicService
from firebase.doctor_service import DoctorService
from firebase.firebase_config import DatabaseError, SlotConflict, get_repository
from firebase.patient_service import PatientService

logger = logging.getLogger(__name__)

OPERATION = "book"


class BookingService:
    def __init__(self, repository=None, availability=None):
        self.repo = repository or get_repository()
        self.availability = availability or AvailabilityService(self.repo)
        self.doctors = DoctorService(self.repo)
        self.clinics = ClinicService(self.repo)
        self.patients = PatientService(self.repo)

    @staticmethod
    def new_appointment_id() -> str:
        # Short and readable enough to be spoken back to a caller.
        return f"{APPOINTMENT_ID_PREFIX}{uuid.uuid4().hex[:6].upper()}"

    # ------------------------------------------------------------------
    def book(self, patient_id: str, doctor_id: str, iso_date: str,
             time_str: str, clinic_id: str | None = None,
             validate_patient: bool = True) -> OperationResult:
        """
        Book an appointment, or explain precisely why it cannot be booked.

        The checks run in the order a receptionist would apply them, so the
        first failure is the most useful thing to say to the caller.
        """
        # --- 1-5: shape of the request ---------------------------------
        if validate_patient:
            failure = check_patient(self.patients, patient_id, OPERATION)
            if failure is not None:
                return failure

        failure, doctor = check_doctor(self.doctors, doctor_id, OPERATION)
        if failure is not None:
            return failure

        failure, clinic = check_clinic(self.clinics, clinic_id, doctor, OPERATION)
        if failure is not None:
            return failure

        failure = check_date(iso_date, OPERATION)
        if failure is not None:
            return failure

        failure = check_time(time_str, OPERATION)
        if failure is not None:
            return failure

        # --- 8: the same caller twice on the same day -------------------
        failure = check_duplicate(self.repo, Collections.APPOINTMENTS,
                                  patient_id, doctor_id, iso_date, OPERATION)
        if failure is not None:
            return failure

        # --- 6-7 + 9: schedule, leave, and whether the slot is free ------
        problem, is_free = self.availability.is_slot_free(
            doctor_id, iso_date, time_str, OPERATION)
        if problem is not None:
            # Whole day unusable (on leave, not working, bad date).
            problem.data.setdefault("doctor_name", doctor.get("name"))
            return problem
        if not is_free:
            # Slot taken: offer the nearest free times instead of a dead end.
            alternatives = self.availability.alternatives(
                doctor_id, iso_date, around=time_str)
            return OperationResult.fail(
                OPERATION, ErrorCode.SLOT_UNAVAILABLE,
                f"{time_str} on {iso_date} is already booked.",
                doctor_id=doctor_id, doctor_name=doctor.get("name"),
                date=iso_date, requested_time=time_str,
                alternative_slots=alternatives)

        # --- 10: write, re-checking inside the transaction ---------------
        appointment_id = self.new_appointment_id()
        record = {
            "appointment_id": appointment_id,
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "clinic_id": clinic.get("clinic_id") if clinic else clinic_id,
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
            # Someone committed between our check and our write. This is the
            # case the transaction exists for.
            alternatives = self.availability.alternatives(
                doctor_id, iso_date, around=time_str)
            return OperationResult.fail(
                OPERATION, ErrorCode.SLOT_UNAVAILABLE,
                f"{time_str} was taken moments ago.",
                doctor_id=doctor_id, doctor_name=doctor.get("name"),
                date=iso_date, requested_time=time_str,
                alternative_slots=alternatives)
        except DatabaseError as exc:
            logger.error("book failed: %s", exc)
            return OperationResult.fail(OPERATION, ErrorCode.BACKEND_UNAVAILABLE,
                                        "Could not reach the appointment system.")

        # --- 11: everything the response layer needs, from the database ---
        return OperationResult.ok(
            OPERATION, appointment_id=appointment_id, status=Status.CONFIRMED,
            appointment=saved,
            doctor_id=doctor_id, doctor_name=doctor.get("name"),
            clinic_name=clinic.get("name") if clinic else None,
            date=iso_date, time=time_str, patient_id=patient_id)
