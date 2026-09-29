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

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

from api import auth, data
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


def decorate(appointments: list[dict], patients: dict | None = None) -> list[dict]:
    """Attach the patient's name and phone; the dashboard shows people, not ids.

    Only the patients on these rows are read, in one batch - never the whole
    patients collection."""
    return data.with_patients(appointments, patients)


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


def _unreachable(*results) -> None:
    """A database that cannot be read must not look like a wrong password -
    nor let the starting password stand in for a stored one."""
    if any(r is not None and not r.ok and r.error == "BACKEND_UNAVAILABLE"
           for r in results):
        raise fail(503, "backend_unavailable",
                   "The appointment system database is unreachable. Please try again.")


def resolve_account(typed: str) -> tuple[str, str, dict | None, Any]:
    """(account_id, role, account record, doctor lookup) for whatever
    somebody typed into the ID box: their username, the administrator id, or
    a doctor id.

    The username, the account and the doctor record are read together, in
    one round trip; the account record is then reused for the password check
    and the reply instead of being read three more times.
    """
    text = str(typed or "").strip()
    if not text:
        return "", "", None, None
    upper = text.upper()
    accounts = AccountService(repo())
    calls = [lambda: accounts.find_by_username(text.lower()),
             lambda: accounts.get(upper)]
    if not auth.is_admin_id(text):
        calls.append(lambda: DoctorService(repo()).get_doctor(upper))
    results = data.parallel(*calls)
    by_name, by_id = results[0], results[1]
    doctor = results[2] if len(results) > 2 else None
    _unreachable(by_name, by_id, doctor)

    if by_name.ok:
        account = str(by_name.data.get("account_id") or "").upper()
        role = by_name.data.get("role") or auth.ROLE_DOCTOR
        if role != auth.ROLE_ADMIN and account != upper:
            doctor = DoctorService(repo()).get_doctor(account)
            _unreachable(doctor)
        return account, role, by_name.data, doctor
    record = by_id.data if by_id.ok else None
    if auth.is_admin_id(text):
        return auth.admin_id(), auth.ROLE_ADMIN, record, None
    return upper, auth.ROLE_DOCTOR, record, doctor


@auth_router.post("/login", summary="Sign in to the dashboard")
def login(request: LoginRequest) -> Any:
    account, role, record, doctor = resolve_account(request.typed())

    # The administrator is checked first: the id is reserved and never
    # resolved against the doctors collection.
    if role == auth.ROLE_ADMIN:
        if not auth.verify_admin(account, request.password, record):
            if not auth.admin_configured():
                logger.error("no SUPERADMIN_PASSWORD configured: "
                             "refusing every administrator sign-in")
            raise fail(401, "invalid_credentials", "Invalid ID or password.")
        session = auth.create_session(account, auth.ROLE_ADMIN)
        return ok({**session, "admin": admin_account(account, record)})

    ok_password = auth.verify(account, request.password, record) if account else False

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
               "account": account_public(account, record=record)})


_UNREAD = object()


def _account_record(account_id: str, record) -> dict | None:
    if record is not _UNREAD:
        return record
    found = AccountService(repo()).get(account_id)
    return found.data if found.ok else None


def admin_account(account_id: str, record=_UNREAD) -> dict:
    stored = _account_record(account_id, record) or {}
    return {"account_id": account_id, "name": "Administrator",
            "role": auth.ROLE_ADMIN,
            "username": stored.get("username") or account_id}


def account_public(account_id: str, role: str = auth.ROLE_DOCTOR,
                   record=_UNREAD) -> dict:
    """What the interface may know about a sign-in account: never a hash.
    Pass `record` when the account has already been read."""
    public = AccountService.public(_account_record(account_id, record))
    public["account_id"] = account_id
    public["role"] = public.get("role") or role
    public["username"] = public.get("username") or account_id
    return public


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
    found, stored = data.parallel(
        lambda: DoctorService(repo()).get_doctor(account["account_id"]),
        lambda: AccountService(repo()).get(account["account_id"]))
    doctor = unwrap(found)
    return ok({"role": account["role"],
               "doctor_id": account["account_id"],
               "doctor": with_defaults(doctor),
               "account": account_public(account["account_id"],
                                         record=stored.data if stored.ok else None)})


# --------------------------------------------------------------------------
# Home
# --------------------------------------------------------------------------
def unread_count(doctor_id: str) -> int:
    """Notifications the doctor has not opened, counted without reading
    them: all of theirs minus the ones marked read."""
    mine = [("doctor_id", "==", doctor_id)]
    everything, read = data.parallel(
        lambda: data.count(Collections.NOTIFICATIONS, mine),
        lambda: data.count(Collections.NOTIFICATIONS,
                           mine + [("read_by_doctor", "==", True)]))
    return max(0, everything - read)


def diary_signature(rows: list[dict]) -> str:
    """Changes whenever an appointment from today on is added, moved,
    cancelled or completed, so the page knows when its other figures need
    reading again - without asking the database anything extra."""
    parts = sorted(f"{r.get('appointment_id')}|{r.get('date')}|{r.get('time')}|"
                   f"{r.get('status')}" for r in rows)
    return hashlib.sha1("/".join(parts).encode("utf-8")).hexdigest()[:16]


@router.get("/summary", summary="Counters, today's list and the queue")
def summary(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    """Only today and what is still to come: the query starts at today, so
    the doctor's history is never read here, and the unread-message count
    rides along in the same round trip."""
    today = today_iso()
    ahead, unread = data.parallel(
        lambda: data.appointments(doctor_id=doctor_id, start=today),
        lambda: unread_count(doctor_id))
    ahead.sort(key=data.sort_key)
    todays = [a for a in ahead if a.get("date") == today]
    upcoming = [a for a in ahead if a.get("date", "") > today and is_active(a)]
    patients = data.patients_by_id(a.get("patient_id") for a in todays + upcoming[:8])
    todays = decorate(todays, patients)

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
        "upcoming": decorate(upcoming[:8], patients),
        "unread_notifications": unread,
        "signature": diary_signature(ahead),
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
    way to ask for somebody else's numbers.

    Only the records dated inside the period are read; the all-time figures
    come from count() aggregations, so the doctor's history is never
    downloaded to count it."""
    try:
        window = stats.resolve_period(period, start, end)
    except ValueError as problem:
        raise fail(400, "invalid_period", str(problem))
    return ok(period_statistics(window, doctor_id=doctor_id))


def period_statistics(window: dict, doctor_id: str | None = None) -> dict:
    """statistics() for one doctor (or the whole clinic), read narrowly:
    the period's records, today's records and the all-time counts, together."""
    day = today_iso()
    bounded = bool(window["start"] or window["end"])
    covers_today = bounded and (window["start"] or "") <= day <= (window["end"] or "9999")
    mine = [("doctor_id", "==", doctor_id)] if doctor_id else []

    calls = [lambda: data.count_by_status(mine)]
    if bounded:
        calls.append(lambda: data.appointments(doctor_id=doctor_id,
                                               start=window["start"], end=window["end"]))
    if not covers_today:
        calls.append(lambda: data.appointments(doctor_id=doctor_id, date=day))
    results = data.parallel(*calls)

    counts = results[0]
    period_rows = results[1] if bounded else None
    today_rows = ([a for a in period_rows if a.get("date") == day] if covers_today
                  else results[-1])
    all_time = stats.summarise_counts(counts["total"], counts)
    return stats.statistics_from_parts(window, period_rows, today_rows, all_time, day)


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------
PAGE_MAX = 500
CANCELLED_ANY = (Status.CANCELLED, Status.CANCELLED_BY_DOCTOR)


def page_payload(key: str, rows: list[dict], total: int | None, offset: int,
                 limit: int, has_more: bool) -> dict:
    """One page of a list. `count` and `total` are how many match in all
    (None when that could only be known by reading everything); `rows` is
    just this page."""
    return {key: rows, "count": total, "total": total, "returned": len(rows),
            "offset": offset, "limit": limit, "has_more": has_more}


def list_appointments(doctor_id: str, scope: str = "all", date: str | None = None,
                      status: str | None = None, query: str | None = None,
                      start: str | None = None, end: str | None = None,
                      offset: int = 0, limit: int = 100) -> dict:
    """One page of one doctor's appointments, oldest first.

    The doctor and the dates go to Firestore, so only this doctor's records
    in the requested window are read, and only as far as the page needs.
    Status filters are applied as the rows arrive. A text search has to look
    at every row in the window (Firestore has no substring search), so it
    reads the window once and then pages what matched.
    """
    today = today_iso()
    window = {"doctor_id": doctor_id, "date": date, "start": start, "end": end}
    checks = []
    if scope == "today":
        if date and date != today:
            return page_payload("appointments", [], 0, offset, limit, False)
        window["date"] = today
    elif scope == "upcoming":
        window["start"] = max(start or today, today)
        checks += [lambda a: a.get("date", "") >= today, is_active]
    elif scope == "completed":
        checks.append(lambda a: a.get("status") == Status.COMPLETED)
    elif scope == "cancelled":
        checks.append(lambda a: a.get("status") in CANCELLED_ANY)
    elif scope == "past":
        window["before"] = today
        checks.append(lambda a: a.get("date", "") < today)
    if status:
        checks.append(lambda a: a.get("status") == status)
    where = (lambda a: all(check(a) for check in checks)) if checks else None
    filters = data.appointment_filters(**window)

    if query:
        needle = query.strip().lower()
        rows = decorate([a for a in data.find(Collections.APPOINTMENTS, filters)
                         if where is None or where(a)])
        rows = [a for a in rows if needle in str(a.get("patient_name", "")).lower()
                or needle in str(a.get("appointment_id", "")).lower()
                or needle in str(a.get("date", ""))
                or needle in str(a.get("patient_phone", ""))]
        rows.sort(key=data.sort_key)
        return page_payload("appointments", rows[offset:offset + limit], len(rows),
                            offset, limit, len(rows) > offset + limit)

    page, has_more, total = data.page_by_date(filters, offset=offset, limit=limit,
                                              where=where)
    return page_payload("appointments", decorate(page), total, offset, limit, has_more)


@router.get("/appointments", summary="This doctor's appointments")
def appointments(doctor_id: str = Depends(auth.current_doctor),
                 scope: str = Query("all"),
                 date: str | None = Query(None),
                 status: str | None = Query(None),
                 query: str | None = Query(None),
                 start: str | None = Query(None),
                 end: str | None = Query(None),
                 offset: int = Query(0, ge=0),
                 limit: int = Query(100, ge=1, le=PAGE_MAX)) -> Any:
    return ok(list_appointments(doctor_id, scope=scope, date=date, status=status,
                                query=query, start=start, end=end,
                                offset=offset, limit=limit))


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
             query: str | None = Query(None),
             offset: int = Query(0, ge=0),
             limit: int = Query(100, ge=1, le=PAGE_MAX)) -> Any:
    """A doctor's patients are the people in their diary. Firestore cannot
    group or de-duplicate, so this doctor's appointments are read (nobody
    else's) and then only the patients among them are fetched, in one batch
    - not the clinic's whole patient list."""
    mine = data.appointments(doctor_id=doctor_id)
    today = today_iso()

    grouped: dict[str, list[dict]] = {}
    for appointment in mine:
        grouped.setdefault(appointment.get("patient_id"), []).append(appointment)
    index = data.patients_by_id(grouped.keys())

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
    return ok(page_payload("patients", rows[offset:offset + limit], len(rows),
                           offset, limit, len(rows) > offset + limit))


@router.get("/patients/{patient_id}", summary="One patient and their history with this doctor")
def patient_details(patient_id: str = Path(...),
                    doctor_id: str = Depends(auth.current_doctor)) -> Any:
    found, mine = data.parallel(
        lambda: PatientService(repo()).get_patient(patient_id),
        lambda: data.find(Collections.APPOINTMENTS,
                          [("doctor_id", "==", doctor_id),
                           ("patient_id", "==", patient_id)]))
    record = unwrap(found)
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
def clinic_or_none() -> dict | None:
    try:
        return data.clinic()
    except Exception as exc:                                  # pragma: no cover
        logger.error("could not read the clinic: %s", exc)
        return None


@router.get("/profile", summary="Doctor profile and clinic")
def profile(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    found, stored, clinic = data.parallel(
        lambda: DoctorService(repo()).get_doctor(doctor_id),
        lambda: AccountService(repo()).get(doctor_id),
        clinic_or_none)
    doctor = unwrap(found)
    return ok({"doctor": with_defaults(doctor), "clinic": clinic,
               "account": account_public(doctor_id,
                                         record=stored.data if stored.ok else None),
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
    # There is one clinic and it belongs to the whole practice, not to any
    # one doctor: its name, address and phone are the administrator's to set.
    # Refused before anything is written, so a mixed request changes nothing.
    if any(value is not None for value in [body.clinic_name, body.clinic_address,
                                           body.clinic_city, body.clinic_phone]):
        raise fail(403, "clinic_readonly",
                   "Clinic details are managed by your clinic administrator.")
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
        # The update reads the record back, so it is not read a second time.
        doctor = unwrap(service["doctors"].update_doctor(doctor_id, doctor_fields))
    else:
        doctor = unwrap(service["doctors"].get_doctor(doctor_id))

    clinic, account = data.parallel(clinic_or_none, lambda: account_public(doctor_id))
    return ok({"doctor": with_defaults(doctor), "clinic": clinic,
               "account": account,
               "editable": {"clinic": False}})


# --------------------------------------------------------------------------
# Schedule and leave
# --------------------------------------------------------------------------
@router.get("/schedule", summary="Weekly working pattern")
def schedule(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    return ok({"days": week_from(unwrap(ScheduleService(repo()).get_schedules(doctor_id)))})


def week_from(rows: list[dict]) -> list[dict]:
    """The working week as the schedule screens show it, from the doctor's
    active schedule rows."""
    by_day: dict[str, list[dict]] = {day: [] for day in WEEKDAY_NAMES}
    for row in rows:
        if row.get("day") in by_day:
            by_day[row["day"]].append({
                "start": row.get("start_time"), "end": row.get("end_time"),
                "slot_duration": row.get("slot_duration", 20),
            })
    return [{"day": day,
             "available": bool(by_day[day]),
             "sessions": sorted(by_day[day], key=lambda s: str(s.get("start")))}
            for day in WEEKDAY_NAMES]


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
def notifications(doctor_id: str = Depends(auth.current_doctor),
                  offset: int = Query(0, ge=0),
                  limit: int = Query(50, ge=1, le=200)) -> Any:
    """Newest first, one page at a time. When that page already holds every
    message, the total and the unread figure are taken from it; otherwise
    Firestore counts them (two count() queries) instead of the rest being
    downloaded."""
    mine = [("doctor_id", "==", doctor_id)]
    rows = data.find(Collections.NOTIFICATIONS, mine, order_by="created_at",
                     descending=True, limit=offset + limit + 1)
    has_more = len(rows) > offset + limit
    if has_more:
        total, read = data.parallel(
            lambda: data.count(Collections.NOTIFICATIONS, mine),
            lambda: data.count(Collections.NOTIFICATIONS,
                               mine + [("read_by_doctor", "==", True)]))
        unread = max(0, total - read)
    else:
        total = len(rows)
        unread = sum(1 for r in rows if not r.get("read_by_doctor"))
    return ok({**page_payload("notifications", rows[offset:offset + limit], total,
                              offset, limit, has_more),
               "unread": unread})


@router.post("/notifications/{notification_id}/read", summary="Mark one as read")
def read_notification(notification_id: str = Path(...),
                      doctor_id: str = Depends(auth.current_doctor)) -> Any:
    # One read to prove the message is this doctor's, instead of listing
    # all of theirs to look for it.
    existing = repo().get(Collections.NOTIFICATIONS, notification_id)
    if not existing or existing.get("doctor_id") != doctor_id:
        raise fail(404, "not_found", "No such notification.")
    return ok(unwrap(NotificationService(repo()).mark_read(notification_id)))


@router.post("/notifications/read-all", summary="Mark every notification as read")
def read_all_notifications(doctor_id: str = Depends(auth.current_doctor)) -> Any:
    return ok(unwrap(NotificationService(repo()).mark_all_read(doctor_id)))
