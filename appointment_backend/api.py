"""
FastAPI router for the Appointment Backend (spec section 5).

These endpoints expose the deterministic appointment operations directly, for
the doctor dashboard and for testing. Patients never reach them - a caller goes
through `/voice/message`, so the Dialog Manager stays the orchestration layer.

Every route returns the same `OperationResult` shape, with HTTP status codes
chosen so a client can react without parsing the body.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config import ErrorCode
from appointment_backend.appointment_service import AppointmentBackend
from appointment_backend.result import OperationResult
from firebase.firebase_config import get_repository, init_repository

router = APIRouter(prefix="/appointments", tags=["appointments"])
doctor_router = APIRouter(prefix="/doctors", tags=["doctors"])
patient_router = APIRouter(prefix="/patients", tags=["patients"])

_backend: AppointmentBackend | None = None


def backend() -> AppointmentBackend:
    """Lazily create the backend against the process-wide repository."""
    global _backend
    if _backend is None:
        _backend = AppointmentBackend(get_repository() or init_repository())
    return _backend


def set_backend(instance: AppointmentBackend | None) -> None:
    """Inject a backend (used by the app lifespan and by tests)."""
    global _backend
    _backend = instance


# --------------------------------------------------------------------------
# HTTP status for each failure code
# --------------------------------------------------------------------------
_STATUS = {
    ErrorCode.PATIENT_NOT_FOUND: 404,
    ErrorCode.DOCTOR_NOT_FOUND: 404,
    ErrorCode.CLINIC_NOT_FOUND: 404,
    ErrorCode.APPOINTMENT_NOT_FOUND: 404,
    ErrorCode.NOT_YOUR_APPOINTMENT: 403,
    ErrorCode.INVALID_DATE: 400,
    ErrorCode.INVALID_TIME: 400,
    ErrorCode.DATE_IN_PAST: 400,
    ErrorCode.SLOT_UNAVAILABLE: 409,
    ErrorCode.DUPLICATE_APPOINTMENT: 409,
    ErrorCode.ALREADY_CANCELLED: 409,
    ErrorCode.ALREADY_COMPLETED: 409,
    ErrorCode.DOCTOR_UNAVAILABLE: 409,
    ErrorCode.DOCTOR_NOT_WORKING: 409,
    ErrorCode.OUTSIDE_WORKING_HOURS: 409,
    ErrorCode.BACKEND_UNAVAILABLE: 503,
}


def respond(result: OperationResult) -> JSONResponse:
    """
    Return the result verbatim, with a matching status code.

    The body shape is identical for success and failure, which is what makes
    the Dialog Manager and the Ollama judge simple to write.
    """
    status = 200 if result.success else _STATUS.get(result.error_code, 400)
    return JSONResponse(status_code=status, content=result.to_dict())


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class BookRequest(BaseModel):
    patient_id: str = Field(..., json_schema_extra={"example": "P001"})
    doctor_id: str = Field(..., json_schema_extra={"example": "D001"})
    date: str = Field(..., json_schema_extra={"example": "2026-09-15"})
    time: str = Field(..., json_schema_extra={"example": "16:00"})
    clinic_id: str | None = Field(None, json_schema_extra={"example": "C001"})


class CancelRequest(BaseModel):
    appointment_id: str
    # Optional: when given, the appointment must belong to this patient.
    patient_id: str | None = None


class RescheduleRequest(BaseModel):
    appointment_id: str
    new_date: str = Field(..., json_schema_extra={"example": "2026-09-17"})
    new_time: str = Field(..., json_schema_extra={"example": "17:00"})
    patient_id: str | None = None


class CheckRequest(BaseModel):
    doctor_id: str
    date: str
    time: str


class UnavailabilityRequest(BaseModel):
    doctor_id: str
    date: str
    reason: str = "Doctor unavailable"


# --------------------------------------------------------------------------
# Appointment operations
# --------------------------------------------------------------------------
@router.post("/book", summary="Book an appointment")
def book(request: BookRequest) -> Any:
    return respond(backend().book_appointment(
        patient_id=request.patient_id, doctor_id=request.doctor_id,
        date=request.date, time=request.time, clinic_id=request.clinic_id))


@router.post("/cancel", summary="Cancel an appointment (status change only)")
def cancel(request: CancelRequest) -> Any:
    return respond(backend().cancel_appointment(request.appointment_id,
                                                request.patient_id))


@router.post("/reschedule", summary="Move an appointment")
def reschedule(request: RescheduleRequest) -> Any:
    return respond(backend().reschedule_appointment(
        request.appointment_id, request.new_date, request.new_time,
        request.patient_id))


@router.post("/check", summary="Could this be booked? (writes nothing)")
def check(request: CheckRequest) -> Any:
    return respond(backend().check_appointment(request.doctor_id,
                                               request.date, request.time))


@router.get("/{appointment_id}", summary="One appointment")
def get_appointment(appointment_id: str = Path(...)) -> Any:
    return respond(backend().get_appointment(appointment_id))


# --------------------------------------------------------------------------
# Patient view
# --------------------------------------------------------------------------
@patient_router.get("/{patient_id}/appointments",
                    summary="A patient's appointments")
def patient_appointments(patient_id: str = Path(...),
                         upcoming_only: bool = Query(True)) -> Any:
    return respond(backend().get_patient_appointments(patient_id,
                                                      upcoming_only))


# --------------------------------------------------------------------------
# Doctor view / dashboard
# --------------------------------------------------------------------------
@doctor_router.get("/{doctor_id}/availability",
                   summary="Free slots on a date")
def availability(doctor_id: str = Path(...),
                 date: str = Query(..., description="YYYY-MM-DD")) -> Any:
    return respond(backend().get_availability(doctor_id, date))


@doctor_router.get("/{doctor_id}/appointments",
                   summary="A doctor's appointments")
def doctor_appointments(doctor_id: str = Path(...),
                        date: str | None = Query(None)) -> Any:
    return respond(backend().get_doctor_appointments(doctor_id, date))


@doctor_router.post("/unavailability",
                    summary="Block a day and cancel everything on it")
def mark_unavailable(request: UnavailabilityRequest) -> Any:
    """
    Spec section 14. Cancels every live appointment that day with status
    `cancelled_by_doctor` and queues a notification per affected patient.
    Nothing is deleted.
    """
    return respond(backend().mark_doctor_unavailable(
        request.doctor_id, request.date, request.reason))
