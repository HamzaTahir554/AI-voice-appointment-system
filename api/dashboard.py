"""
Doctor dashboard API.

Everything the dashboard needs, scoped to the signed-in doctor. The business
rules are NOT reimplemented here: each operation delegates to the existing
AppointmentBackend (booking, cancellation, reschedule, the doctor-unavailable
cascade) and to the existing Firebase services. This module only adds

  * a session (api/auth.py) so one doctor cannot read another's appointments,
  * joins the dashboard needs (patient names on appointments, clinic details),
  * counters computed from the real appointment records,
  * and the schedule / profile / leave writes the voice pipeline never needed.

Response shape
    reads      {"success": true, "data": ...}
    mutations  the OperationResult envelope the rest of the backend uses
    failures   {"success": false, "error": {"code", "message"}} + HTTP status

The public /appointments and /doctors routes are untouched: the voice
pipeline keeps using them exactly as before.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

from api import auth
from appointment_backend import statistics as stats
from appointment_backend.api import backend, respond
from config import Collections, Status, TIMEZONE
from firebase.appointment_service import ACTIVE_STATUSES
from firebase.account_service import (AccountService,
                                      check_password_rules)
from firebase.clinic_service import ClinicService
from firebase.doctor_service import DoctorService, aliases_for
from firebase.firebase_config import get_repository, init_repository
from firebase.notification_service import NotificationService
from firebase.patient_service import PatientService
from firebase.schedule_service import WEEKDAY_NAMES, ScheduleService, today_iso

logger = logging.getLogger("api.dashboard")

auth_router = APIRouter(prefix="/auth", tags=["dashboard"])
router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def repo():
    return get_repository() or init_repository()


def services() -> dict:
    repository = repo()
    return {
        "doctors": DoctorService(repository),
        "clinics": ClinicService(repository),
        "patients": PatientService(repository),
        "schedules": ScheduleService(repository),
        "notifications": NotificationService(repository),
    }


def ok(data: Any) -> dict:
    return {"success": True, "data": data}


def fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status,
                         detail={"success": False, "error": {"code": code, "message": message}})


def unwrap(result, not_found: int = 404):
    """ServiceResult -> value, or an HTTP error with the same code."""
    if result.ok:
        return result.data
    status = 503 if result.error == "BACKEND_UNAVAILABLE" else not_found
    raise fail(status, str(result.error).lower(), result.message or "Request failed")


def now_hhmm() -> str:
    return datetime.now(TIMEZONE).strftime("%H:%M")


def patient_index() -> dict:
    """patient_id -> record, read once per request rather than per row."""
    try:
        return {p.get("patient_id"): p for p in repo().query(Collections.PATIENTS)}
    except Exception as exc:                                  # pragma: no cover
        logger.error("could not read patients: %s", exc)
        return {}


def decorate(appointments: list[dict], patients: dict | None = None) -> list[dict]:
    """Attach the patient's name and phone; the dashboard shows people, not ids."""
    patients = patients if patients is not None else patient_index()
    rows = []
    for appointment in appointments:
        patient = patients.get(appointment.get("patient_id")) or {}
        rows.append({**appointment,
                     "patient_name": patient.get("name") or "Unknown patient",
                     "patient_phone": patient.get("phone") or ""})
    return rows


def doctor_appointments(doctor_id: str, date: str | None = None) -> list[dict]:
    result = backend().get_doctor_appointments(doctor_id, date)
    if not result.success:
        raise fail(503, result.error_code or "backend_unavailable",
                   result.error_message or "Could not read appointments.")
    return list(result.data.get("appointments", []))


def is_active(appointment: dict) -> bool:
    return appointment.get("status") in ACTIVE_STATUSES


# Alerts the dashboard raises while it is open. Each one corresponds to
# something the voice assistant can actually do to the diary, so a switch
# here always changes real behaviour.
ALERT_PREFS = {
    "new_appointments": True,
    "cancellations": True,
    "reschedules": True,
}

PHOTO_TYPES = ("data:image/png;base64,", "data:image/jpeg;base64,",
               "data:image/webp;base64,")
PHOTO_MAX_CHARS = 200_000          # ~150 KB, well inside a Firestore document


def clean_photo(value: str | None) -> str:
    """A small inline profile picture, or "" to remove it.

    The project has no file storage service, so the picture is stored with
    the doctor record as a data URL. The dashboard shrinks it to 256px
    before sending; anything larger than that is refused rather than
    silently truncated.
    """
    photo = (value or "").strip()
    if not photo:
        return ""
    if not photo.startswith(PHOTO_TYPES):
        raise fail(400, "invalid_photo",
                   "Upload a PNG, JPEG or WebP image.")
    if len(photo) > PHOTO_MAX_CHARS:
        raise fail(400, "photo_too_large",
                   "That picture is too large. Choose a smaller image.")
    return photo


def clean_prefs(value: dict | None) -> dict:
    given = value or {}
    return {key: bool(given.get(key, default))
            for key, default in ALERT_PREFS.items()}


def with_defaults(doctor: dict) -> dict:
    """Fill in the fields added after the first version of the schema, so the
    dashboard never has to guess what a missing key means."""
    record = dict(doctor or {})
    record["notification_prefs"] = clean_prefs(record.get("notification_prefs"))
    record.setdefault("email", "")
    record.setdefault("photo", "")
    record.setdefault("about", "")
    return record


# --------------------------------------------------------------------------
# Sign in
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """`user_id` is a doctor id (D001) or the administrator id."""
    user_id: str | None = Field(default=None, json_schema_extra={"example": "D001"})
    doctor_id: str | None = None          # the original field name, still accepted
    password: str

    def typed(self) -> str:
        return str(self.user_id or self.doctor_id or "").strip()


def resolve_account(typed: str) -> tuple[str, str]:
    """(account_id, role) for whatever somebody typed into the ID box: their
    username, the administrator id, or a doctor id."""
    text = str(typed or "").strip()
    if not text:
        return "", ""
    found = AccountService(repo()).find_by_username(text.lower())
    if found.ok:
        return (str(found.data.get("account_id") or "").upper(),
                found.data.get("role") or auth.ROLE_DOCTOR)
    if auth.is_admin_id(text):
        return auth.admin_id(), auth.ROLE_ADMIN
    return text.upper(), auth.ROLE_DOCTOR


@auth_router.post("/login", summary="Sign in to the dashboard")
def login(request: LoginRequest) -> Any:
    account, role = resolve_account(request.typed())

    # The administrator is checked first: the id is reserved and never
    # resolved against the doctors collection.
    if role == auth.ROLE_ADMIN:
        if not auth.verify_admin(account, request.password):
            if not auth.admin_configured():
                logger.error("no SUPERADMIN_PASSWORD configured: "
                             "refusing every administrator sign-in")
            raise fail(401, "invalid_credentials", "Invalid ID or password.")
        session = auth.create_session(account, auth.ROLE_ADMIN)
        return ok({**session, "admin": admin_account(account)})

    doctor = DoctorService(repo()).get_doctor(account) if account else None
    ok_password = auth.verify(account, request.password) if account else False

    # The same answer whether the id or the password was wrong, so the form
    # cannot be used to discover which doctor ids exist.
    if not doctor or not doctor.ok or not ok_password:
        if not auth.password_configured():
            logger.error("no DASHBOARD_PASSWORD configured: refusing every sign-in")
        raise fail(401, "invalid_credentials", "Invalid ID or password.")

    # A doctor the administrator has deactivated cannot sign in either.
    if not doctor.data.get("active", True):
        raise fail(403, "account_inactive",
                   "This account has been deactivated. "
                   "Contact your clinic administrator.")

    session = auth.create_session(account, auth.ROLE_DOCTOR)
    return ok({**session, "doctor": with_defaults(doctor.data),
               "account": account_public(account)})


def admin_account(account_id: str) -> dict:
    return {"account_id": account_id, "name": "Administrator",
            "role": auth.ROLE_ADMIN,
            "username": AccountService(repo()).username_of(account_id) or account_id}


def account_public(account_id: str, role: str = auth.ROLE_DOCTOR) -> dict:
    """What the interface may know about a sign-in account: never a hash."""
    service = AccountService(repo())
    found = service.get(account_id)
    record = AccountService.public(found.data if found.ok else None)
    record["account_id"] = account_id
    record["role"] = record.get("role") or role
    record["username"] = record.get("username") or account_id
    return record


@auth_router.post("/logout", summary="End the session")
def logout(authorization: str | None = Header(default=None)) -> Any:
    if authorization and authorization.lower().startswith("bearer "):
        auth.end_session(authorization[7:].strip())
    return ok({"signed_out": True})


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


@auth_router.post("/password", summary="Change your own password")
def change_own_password(body: PasswordBody,
                        authorization: str | None = Header(default=None),
                        account: dict = Depends(auth.current_account)) -> Any:
    """Anyone may change their own password, and nobody else's. The current
    one has to be given, so a borrowed open tab cannot lock the owner out."""
    account_id, role = account["account_id"], account["role"]
    if not auth.verify_account(account_id, role, body.current_password):
        raise fail(403, "wrong_password", "Your current password is not correct.")

    problem = check_password_rules(body.new_password)
    if problem:
        raise fail(400, "invalid_password", problem)
    if body.new_password == body.current_password:
        raise fail(400, "invalid_password", "Choose a password you have not used here before.")

    unwrap(AccountService(repo()).set_password(account_id, body.new_password, role),
           not_found=400)
    ended = auth.end_sessions_for(account_id,
                                  keep=auth.token_from_header(authorization))
    return ok({"changed": True, "other_sessions_ended": ended,
               "account": account_public(account_id, role)})


@auth_router.get("/session", summary="Who is signed in")
def session(account: dict = Depends(auth.current_account)) -> Any:
    if account["role"] == auth.ROLE_ADMIN:
        return ok({"role": account["role"],
                   "admin": admin_account(account["account_id"])})
    doctor = unwrap(DoctorService(repo()).get_doctor(account["account_id"]))
    return ok({"role": account["role"],
               "doctor_id": account["account_id"],
               "doctor": with_defaults(doctor),
               "account": account_public(account["account_id"])})


# --------------------------------------------------------------------------
# Home
# --------------------------------------------------------------------------
@router.get("/summary", summary="Counters, today's list and the queue")
def summary(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    today = today_iso()
    patients = patient_index()
    todays = decorate(doctor_appointments(doctor_id, today), patients)
    upcoming = [a for a in decorate(doctor_appointments(doctor_id), patients)
                if a.get("date", "") > today and is_active(a)]

    now = now_hhmm()
    # The backend has no check-in state, so "waiting" is derived: an active
    # appointment today whose slot time has already started.
    waiting = [a for a in todays if is_active(a) and str(a.get("time", "")) <= now]
    later_today = [a for a in todays if is_active(a) and str(a.get("time", "")) > now]

    stats = {
        "total": len(todays),
        "completed": sum(1 for a in todays if a.get("status") == Status.COMPLETED),
        "upcoming": len(later_today),
        "waiting": len(waiting),
        "cancelled": sum(1 for a in todays if a.get("status") in
                         (Status.CANCELLED, Status.CANCELLED_BY_DOCTOR)),
        "upcoming_total": len(upcoming),
    }
    return ok({
        "date": today,
        "stats": stats,
        "today": todays,
        "queue": sorted(waiting, key=lambda a: a.get("time", "")),
        "upcoming": upcoming[:8],
        "generated_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
    })


# --------------------------------------------------------------------------
# Statistics - this doctor's own numbers, nobody else's
# --------------------------------------------------------------------------
@router.get("/statistics", summary="Appointment counts for the signed-in doctor")
def doctor_statistics(doctor_id: str = Depends(auth.current_doctor),
                      period: str = Query("month"),
                      start: str | None = Query(None),
                      end: str | None = Query(None)) -> Any:
    """Counted from this doctor's own appointment records. The signed-in
    doctor id comes from the session, never from the request, so there is no
    way to ask for somebody else's numbers."""
    try:
        return ok(stats.statistics(doctor_appointments(doctor_id),
                                   period=period, start=start, end=end))
    except ValueError as problem:
        raise fail(400, "invalid_period", str(problem))


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------
@router.get("/appointments", summary="This doctor's appointments")
def appointments(doctor_id: str = Depends(auth.current_doctor),
                 scope: str = Query("all"),
                 date: str | None = Query(None),
                 status: str | None = Query(None),
                 query: str | None = Query(None)) -> Any:
    today = today_iso()
    rows = decorate(doctor_appointments(doctor_id, date))

    if scope == "today":
        rows = [a for a in rows if a.get("date") == today]
    elif scope == "upcoming":
        rows = [a for a in rows if a.get("date", "") >= today and is_active(a)]
    elif scope == "completed":
        rows = [a for a in rows if a.get("status") == Status.COMPLETED]
    elif scope == "cancelled":
        rows = [a for a in rows if a.get("status") in (Status.CANCELLED, Status.CANCELLED_BY_DOCTOR)]
    elif scope == "past":
        rows = [a for a in rows if a.get("date", "") < today]

    if status:
        rows = [a for a in rows if a.get("status") == status]

    if query:
        needle = query.strip().lower()
        rows = [a for a in rows if needle in str(a.get("patient_name", "")).lower()
                or needle in str(a.get("appointment_id", "")).lower()
                or needle in str(a.get("date", ""))
                or needle in str(a.get("patient_phone", ""))]

    rows.sort(key=lambda a: (a.get("date", ""), a.get("time", "")))
    return ok({"appointments": rows, "count": len(rows)})


@router.get("/appointments/{appointment_id}", summary="One appointment")
def appointment_details(appointment_id: str = Path(...),
                        doctor_id: str = Depends(auth.current_doctor)) -> Any:
    result = backend().get_appointment(appointment_id)
    if not result.success:
        raise fail(404 if result.error_code == "appointment_not_found" else 503,
                   result.error_code or "error", result.error_message or "Not found")
    appointment = result.data.get("appointment", {})
    if appointment.get("doctor_id") != doctor_id:
        raise fail(403, "not_your_appointment", "That appointment belongs to another doctor.")
    return ok({**decorate([appointment])[0],
               "clinic_name": result.data.get("clinic_name"),
               "doctor_name": result.data.get("doctor_name")})


class BookBody(BaseModel):
    patient_id: str
    date: str
    time: str
    clinic_id: str | None = None


@router.post("/appointments", summary="Book an appointment from the dashboard")
def create_appointment(body: BookBody, doctor_id: str = Depends(auth.current_doctor)) -> Any:
    return respond(backend().book_appointment(body.patient_id, doctor_id,
                                              body.date, body.time, body.clinic_id))


def owned(appointment_id: str, doctor_id: str) -> dict:
    result = backend().get_appointment(appointment_id)
    if not result.success:
        raise fail(404, result.error_code or "appointment_not_found",
                   result.error_message or "Appointment not found.")
    appointment = result.data.get("appointment", {})
    if appointment.get("doctor_id") != doctor_id:
        raise fail(403, "not_your_appointment", "That appointment belongs to another doctor.")
    return appointment


@router.post("/appointments/{appointment_id}/cancel", summary="Cancel (status change only)")
def cancel(appointment_id: str = Path(...), doctor_id: str = Depends(auth.current_doctor)) -> Any:
    owned(appointment_id, doctor_id)
    return respond(backend().cancel_appointment(appointment_id))


class RescheduleBody(BaseModel):
    date: str
    time: str


@router.post("/appointments/{appointment_id}/reschedule", summary="Move an appointment")
def reschedule(body: RescheduleBody, appointment_id: str = Path(...),
               doctor_id: str = Depends(auth.current_doctor)) -> Any:
    owned(appointment_id, doctor_id)
    return respond(backend().reschedule_appointment(appointment_id, body.date, body.time))


@router.post("/appointments/{appointment_id}/complete", summary="Mark a consultation finished")
def complete(appointment_id: str = Path(...), doctor_id: str = Depends(auth.current_doctor)) -> Any:
    return respond(backend().complete_appointment(appointment_id, doctor_id))


@router.get("/availability", summary="Free slots for a date")
def availability(date: str = Query(...), doctor_id: str = Depends(auth.current_doctor)) -> Any:
    return respond(backend().get_availability(doctor_id, date))


# --------------------------------------------------------------------------
# Patients
# --------------------------------------------------------------------------
@router.get("/patients", summary="Patients this doctor has seen or will see")
def patients(doctor_id: str = Depends(auth.current_doctor),
             query: str | None = Query(None)) -> Any:
    index = patient_index()
    mine = doctor_appointments(doctor_id)
    today = today_iso()

    grouped: dict[str, list[dict]] = {}
    for appointment in mine:
        grouped.setdefault(appointment.get("patient_id"), []).append(appointment)

    rows = []
    for patient_id, appointments_for in grouped.items():
        record = index.get(patient_id) or {}
        appointments_for.sort(key=lambda a: (a.get("date", ""), a.get("time", "")))
        completed = [a for a in appointments_for if a.get("status") == Status.COMPLETED]
        upcoming = [a for a in appointments_for
                    if a.get("date", "") >= today and is_active(a)]
        rows.append({
            "patient_id": patient_id,
            "name": record.get("name") or "Unknown patient",
            "phone": record.get("phone") or "",
            "total_appointments": len(appointments_for),
            "completed_visits": len(completed),
            "last_visit": completed[-1]["date"] if completed else None,
            "next_appointment": upcoming[0] if upcoming else None,
        })

    if query:
        needle = query.strip().lower()
        rows = [r for r in rows if needle in r["name"].lower()
                or needle in str(r["patient_id"]).lower()
                or needle in str(r["phone"])]

    rows.sort(key=lambda r: r["name"])
    return ok({"patients": rows, "count": len(rows)})


@router.get("/patients/{patient_id}", summary="One patient and their history with this doctor")
def patient_details(patient_id: str = Path(...),
                    doctor_id: str = Depends(auth.current_doctor)) -> Any:
    record = unwrap(PatientService(repo()).get_patient(patient_id))
    mine = [a for a in doctor_appointments(doctor_id)
            if a.get("patient_id") == patient_id]
    if not mine:
        raise fail(403, "not_your_patient",
                   "This patient has no appointments with you.")
    mine.sort(key=lambda a: (a.get("date", ""), a.get("time", "")), reverse=True)
    completed = [a for a in mine if a.get("status") == Status.COMPLETED]
    today = today_iso()
    upcoming = [a for a in reversed(mine) if a.get("date", "") >= today and is_active(a)]
    return ok({
        # Only what the doctor needs to run the appointment.
        "patient_id": record.get("patient_id"),
        "name": record.get("name"),
        "phone": record.get("phone"),
        "total_appointments": len(mine),
        "completed_visits": len(completed),
        "last_visit": completed[0]["date"] if completed else None,
        "next_appointment": upcoming[0] if upcoming else None,
        "history": mine,
    })


class PatientBody(BaseModel):
    name: str
    phone: str


@router.post("/patients", summary="Register a patient")
def create_patient(body: PatientBody, doctor_id: str = Depends(auth.current_doctor)) -> Any:
    result = PatientService(repo()).create_patient(body.name.strip(), body.phone.strip())
    if not result.ok:
        raise fail(400, str(result.error).lower(), result.message or "Could not create the patient.")
    return ok(result.data)


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------
def clinic_for(doctor: dict) -> dict | None:
    result = ClinicService(repo()).get_clinic_for_doctor(doctor)
    return result.data if result.ok else None


@router.get("/profile", summary="Doctor profile and clinic")
def profile(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    doctor = unwrap(DoctorService(repo()).get_doctor(doctor_id))
    return ok({"doctor": with_defaults(doctor), "clinic": clinic_for(doctor),
               "account": account_public(doctor_id),
               "editable": {"clinic": False}})


class ProfileBody(BaseModel):
    """What a doctor may change about themselves. The clinic fields are
    listed so an attempt to set one is REFUSED rather than ignored."""
    name: str | None = None
    specialization: str | None = None
    qualification: str | None = None
    experience_years: int | None = None
    phone: str | None = None
    email: str | None = None
    fee: int | None = None
    about: str | None = None
    photo: str | None = None
    notification_prefs: dict | None = None
    clinic_name: str | None = None
    clinic_address: str | None = None
    clinic_city: str | None = None
    clinic_phone: str | None = None


@router.patch("/profile", summary="Update the doctor profile and clinic")
def update_profile(body: ProfileBody, doctor_id: str = Depends(auth.current_doctor)) -> Any:
    service = services()
    doctor_fields = {k: v for k, v in {
        "name": body.name, "specialization": body.specialization,
        "qualification": body.qualification, "experience_years": body.experience_years,
        "phone": body.phone, "email": body.email, "fee": body.fee,
        "about": body.about,
    }.items() if v is not None}

    if body.name is not None:
        # The voice assistant matches callers' words against the name and its
        # aliases, so a rename has to move those too.
        current = unwrap(service["doctors"].get_doctor(doctor_id))
        doctor_fields["aliases"] = aliases_for(body.name, current.get("aliases"))

    if body.photo is not None:
        doctor_fields["photo"] = clean_photo(body.photo)
    if body.notification_prefs is not None:
        doctor_fields["notification_prefs"] = clean_prefs(body.notification_prefs)

    if doctor_fields:
        unwrap(service["doctors"].update_doctor(doctor_id, doctor_fields))

    doctor = unwrap(service["doctors"].get_doctor(doctor_id))
    # There is one clinic and it belongs to the whole practice, not to any
    # one doctor: its name, address and phone are the administrator's to set.
    if any(value is not None for value in [body.clinic_name, body.clinic_address,
                                           body.clinic_city, body.clinic_phone]):
        raise fail(403, "clinic_readonly",
                   "Clinic details are managed by your clinic administrator.")

    clinic = clinic_for(doctor)

    return ok({"doctor": with_defaults(doctor), "clinic": clinic,
               "account": account_public(doctor_id),
               "editable": {"clinic": False}})


# --------------------------------------------------------------------------
# Schedule and leave
# --------------------------------------------------------------------------
@router.get("/schedule", summary="Weekly working pattern")
def schedule(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    rows = unwrap(ScheduleService(repo()).get_schedules(doctor_id))
    by_day: dict[str, list[dict]] = {day: [] for day in WEEKDAY_NAMES}
    for row in rows:
        if row.get("day") in by_day:
            by_day[row["day"]].append({
                "start": row.get("start_time"), "end": row.get("end_time"),
                "slot_duration": row.get("slot_duration", 20),
            })
    days = [{"day": day,
             "available": bool(by_day[day]),
             "sessions": sorted(by_day[day], key=lambda s: str(s.get("start")))}
            for day in WEEKDAY_NAMES]
    return ok({"days": days})


class SessionBody(BaseModel):
    start: str
    end: str


class DayBody(BaseModel):
    day: str
    available: bool = True
    sessions: list[SessionBody] = Field(default_factory=list)


class ScheduleBody(BaseModel):
    days: list[DayBody]


@router.patch("/schedule", summary="Replace the weekly working pattern")
def update_schedule(body: ScheduleBody, doctor_id: str = Depends(auth.current_doctor)) -> Any:
    payload = [{"day": d.day, "available": d.available,
                "sessions": [s.model_dump() for s in d.sessions]} for d in body.days]
    unwrap(ScheduleService(repo()).set_weekly_schedule(doctor_id, payload), not_found=400)
    return schedule(doctor_id)


@router.get("/leave", summary="Blocked dates")
def leave(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    rows = unwrap(ScheduleService(repo()).list_unavailable(doctor_id))
    return ok({"leave": rows})


class LeaveBody(BaseModel):
    start_date: str
    end_date: str | None = None
    reason: str = "Doctor unavailable"


@router.post("/leave", summary="Block dates and cancel the appointments on them")
def add_leave(body: LeaveBody, doctor_id: str = Depends(auth.current_doctor)) -> Any:
    """
    Runs the existing cascade once per date (cancellation_service): every live
    appointment becomes `cancelled_by_doctor` and a notification is queued for
    each patient. Delivery of those notifications is not implemented.
    """
    start = body.start_date
    end = body.end_date or body.start_date
    if end < start:
        raise fail(400, "invalid_date", "The end date must be on or after the start date.")

    try:
        first = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise fail(400, "invalid_date", "Dates must look like YYYY-MM-DD.")
    if (last - first).days > 60:
        raise fail(400, "invalid_date", "Block at most 60 days at a time.")

    blocked, cancelled, notified, failures = [], 0, 0, []
    current = first
    while current <= last:
        iso = current.isoformat()
        result = backend().mark_doctor_unavailable(doctor_id, iso, body.reason)
        if result.success:
            blocked.append(iso)
            cancelled += int(result.data.get("cancelled_count", 0))
            notified += len(result.data.get("notifications", []))
        else:
            failures.append({"date": iso, "message": result.error_message})
        current = current + timedelta(days=1)

    if not blocked:
        raise fail(400, "leave_failed",
                   failures[0]["message"] if failures else "Could not block those dates.")

    return ok({"blocked_dates": blocked, "cancelled_appointments": cancelled,
               "notifications_queued": notified, "failures": failures,
               "delivery": "queued only - SMS and call delivery are not implemented"})


@router.delete("/leave/{date}", summary="Re-open a blocked date")
def remove_leave(date: str = Path(...), doctor_id: str = Depends(auth.current_doctor)) -> Any:
    unwrap(ScheduleService(repo()).clear_unavailable(doctor_id, date))
    return ok({"date": date, "reopened": True,
               "note": "Appointments cancelled earlier stay cancelled; those patients were told."})


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
@router.get("/notifications", summary="Messages queued for this doctor's patients")
def notifications(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    service = NotificationService(repo())
    rows = unwrap(service.list_for_doctor(doctor_id))
    return ok({"notifications": rows,
               "unread": sum(1 for r in rows if not r.get("read_by_doctor"))})


@router.post("/notifications/{notification_id}/read", summary="Mark one as read")
def read_notification(notification_id: str = Path(...),
                      doctor_id: str = Depends(auth.current_doctor)) -> Any:
    service = NotificationService(repo())
    existing = unwrap(service.list_for_doctor(doctor_id, limit=200))
    if not any(r.get("notification_id") == notification_id for r in existing):
        raise fail(404, "not_found", "No such notification.")
    return ok(unwrap(service.mark_read(notification_id)))


@router.post("/notifications/read-all", summary="Mark every notification as read")
def read_all_notifications(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    return ok(unwrap(NotificationService(repo()).mark_all_read(doctor_id)))
