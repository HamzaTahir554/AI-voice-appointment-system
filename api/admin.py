"""
Administration API: the person who runs the clinic manages the doctors.

Two accounts exist in this system and they do not overlap (api/auth.py):

    doctor      /dashboard/*   own appointments, own schedule, own profile
    superadmin  /admin/*       the doctors themselves - add, edit, switch off

Nothing here reimplements a business rule. Doctor, clinic and schedule
records are written through the same Firebase services the voice pipeline
reads (`DoctorService`, `ClinicService`, `ScheduleService`), and everything
to do with appointments delegates to the doctor-side handlers in
`api/dashboard.py`, which in turn delegate to `AppointmentBackend`. So a
doctor added here is immediately bookable by telephone, and a doctor
switched off here is refused by the booking rule that already existed
(`validation.check_doctor`) and disappears from `find_doctor`.

Deactivate, remove, restore
    Nothing is ever deleted. `active: false` is the switch the rest of the
    system already understands. "Remove" additionally sets `archived: true`,
    which only hides the doctor from the default list - appointments,
    patients and history stay exactly where they are, and the record can be
    restored.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from api import auth
from api import dashboard as doctor_api
from appointment_backend import statistics as stats
from appointment_backend.api import backend, respond
from api.dashboard import (account_public, clean_photo, decorate, fail,
                           now_hhmm, ok, repo, unwrap, with_defaults)
from config import Collections, Status, TIMEZONE
from firebase.appointment_service import ACTIVE_STATUSES
from firebase.account_service import (AccountService, check_password_rules,
                                      check_username_rules)
from firebase.clinic_service import ClinicService
from firebase.doctor_service import DoctorService, aliases_for
from firebase.schedule_service import WEEKDAY_NAMES, ScheduleService, today_iso

logger = logging.getLogger("api.admin")

router = APIRouter(prefix="/admin", tags=["administration"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+[.][^@\s]+$")


# --------------------------------------------------------------------------
# Reading collections
# --------------------------------------------------------------------------
def read(collection: str, filters: list | None = None) -> list[dict]:
    try:
        return list(repo().query(collection, filters) if filters
                    else repo().query(collection))
    except Exception as exc:                                   # pragma: no cover
        logger.error("could not read %s: %s", collection, exc)
        raise fail(503, "backend_unavailable",
                   "The appointment system database is unreachable.")


def doctor_or_404(doctor_id: str) -> dict:
    result = DoctorService(repo()).get_doctor(doctor_id)
    if not result.ok:
        status = 503 if result.error == "BACKEND_UNAVAILABLE" else 404
        raise fail(status, str(result.error).lower(),
                   result.message or "No such doctor.")
    return result.data


def now_stamp() -> str:
    """Only for fields the repository does not keep itself; `created_at` and
    `updated_at` are written by the data layer for every collection."""
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Ids and aliases
# --------------------------------------------------------------------------
def next_id(existing: list[str], prefix: str) -> str:
    """D001, D002, ... continuing after the highest id already in use."""
    highest = 0
    for value in existing:
        text = str(value or "")
        if text.upper().startswith(prefix) and text[len(prefix):].isdigit():
            highest = max(highest, int(text[len(prefix):]))
    return prefix + str(highest + 1).zfill(3)


def alias_warnings(name: str, doctor_id: str | None,
                   doctors: list[dict]) -> list[str]:
    """Tell the administrator when the voice assistant will have to ask
    "which doctor?" because two names collide."""
    mine = set(aliases_for(name))
    warnings = []
    for other in doctors:
        if other.get("doctor_id") == doctor_id or other.get("archived"):
            continue
        if not other.get("active", True):
            continue
        shared = mine & set(aliases_for(other.get("name", ""),
                                        other.get("aliases")))
        if shared:
            warnings.append(
                'A caller who says "' + sorted(shared)[0] + '" could mean '
                + str(other.get("name")) + " as well, so the assistant will "
                "have to ask which doctor they want.")
    return warnings


# --------------------------------------------------------------------------
# Joining a doctor to everything around them
# --------------------------------------------------------------------------
def hours_of(rows: list[dict]) -> dict:
    """The working pattern as the doctor table shows it."""
    days, starts, ends, durations = [], [], [], []
    for row in rows:
        if not row.get("active", True):
            continue
        if row.get("day") in WEEKDAY_NAMES and row.get("day") not in days:
            days.append(row["day"])
        if row.get("start_time"):
            starts.append(str(row["start_time"]))
        if row.get("end_time"):
            ends.append(str(row["end_time"]))
        if row.get("slot_duration"):
            durations.append(int(row["slot_duration"]))
    days.sort(key=WEEKDAY_NAMES.index)
    return {
        "working_days": days,
        "start_time": min(starts) if starts else None,
        "end_time": max(ends) if ends else None,
        "slot_duration": durations[0] if durations else None,
    }


def status_of(doctor: dict) -> str:
    if doctor.get("archived"):
        return "archived"
    return "active" if doctor.get("active", True) else "inactive"


def blocked_today_ids() -> set:
    today = today_iso()
    rows = read(Collections.UNAVAILABILITY)
    return {r.get("doctor_id") for r in rows
            if r.get("date") == today and r.get("active", True)}


def doctor_row(doctor: dict, clinic: dict | None, appointments: list[dict],
               schedules: list[dict], blocked: set) -> dict:
    today = today_iso()
    doctor_id = doctor.get("doctor_id")
    mine = [a for a in appointments if a.get("doctor_id") == doctor_id]
    live = [a for a in mine if a.get("status") in ACTIVE_STATUSES]
    hours = hours_of([s for s in schedules if s.get("doctor_id") == doctor_id])
    return {
        **with_defaults(doctor),
        "status": status_of(doctor),
        "clinic": clinic,
        "appointments": {
            "total": len(mine),
            "today": len([a for a in live if a.get("date") == today]),
            "upcoming": len([a for a in live
                             if str(a.get("date", "")) >= today]),
        },
        "availability": {
            **hours,
            "on_leave_today": doctor_id in blocked,
            "accepting": bool(doctor.get("active", True))
                         and not doctor.get("archived")
                         and bool(hours["working_days"]),
        },
    }


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------
def _much_later(created: str | None, updated: str | None) -> bool:
    """True when an update is its own event, not the write that created the
    record a moment earlier."""
    if not created or not updated or created == updated:
        return False
    try:
        gap = datetime.fromisoformat(updated) - datetime.fromisoformat(created)
    except ValueError:
        return True
    return gap.total_seconds() > 120


def recent_activity(doctors: list[dict], limit: int = 8) -> list[dict]:
    """What has changed lately.

    Only events the database actually records are listed: doctors carry
    `created_at` / `updated_at` from the moment they are managed here, and
    every patient message the cancellation cascade queues carries its own
    `created_at`. Appointment records have no creation timestamp, so no
    "booked at" entry is invented for them.
    """
    events: list[dict] = []
    for doctor in doctors:
        name = doctor.get("name") or doctor.get("doctor_id")
        if doctor.get("created_at"):
            events.append({"at": doctor["created_at"], "kind": "doctor_added",
                           "doctor_id": doctor.get("doctor_id"),
                           "text": str(name) + " was added"})
        updated = doctor.get("updated_at")
        if updated and _much_later(doctor.get("created_at"), updated):
            events.append({"at": updated, "kind": "doctor_updated",
                           "doctor_id": doctor.get("doctor_id"),
                           "text": str(name) + " was updated"})

    names = {d.get("doctor_id"): d.get("name") for d in doctors}
    for note in read(Collections.NOTIFICATIONS):
        if not note.get("created_at"):
            continue
        events.append({
            "at": note["created_at"], "kind": "patient_notified",
            "doctor_id": note.get("doctor_id"),
            "text": "A patient of "
                    + str(names.get(note.get("doctor_id"), "a doctor"))
                    + " was told their " + str(note.get("date", ""))
                    + " appointment was cancelled",
        })

    events.sort(key=lambda event: str(event["at"]), reverse=True)
    return events[:limit]


@router.get("/summary", summary="Clinic-wide counters and recent activity")
def summary(admin: str = Depends(auth.current_admin)) -> Any:
    doctors = read(Collections.DOCTORS)
    appointments = read(Collections.APPOINTMENTS)
    today, now = today_iso(), now_hhmm()

    listed = [d for d in doctors if not d.get("archived")]
    live = [a for a in appointments if a.get("status") in ACTIVE_STATUSES]
    today_rows = [a for a in appointments if a.get("date") == today]

    return ok({
        "date": today,
        "doctors": {
            "total": len(listed),
            "active": len([d for d in listed if d.get("active", True)]),
            "inactive": len([d for d in listed if not d.get("active", True)]),
            "archived": len([d for d in doctors if d.get("archived")]),
            "on_leave_today": len(blocked_today_ids()),
        },
        "appointments": {
            "total": len(appointments),
            "today": len(today_rows),
            "today_active": len([a for a in today_rows
                                 if a.get("status") in ACTIVE_STATUSES]),
            "upcoming": len([a for a in live
                             if str(a.get("date", "")) > today
                             or (a.get("date") == today
                                 and str(a.get("time", "")) >= now)]),
            "completed": len([a for a in appointments
                              if a.get("status") == Status.COMPLETED]),
            "cancelled": len([a for a in appointments
                              if a.get("status") in (Status.CANCELLED,
                                                     Status.CANCELLED_BY_DOCTOR)]),
        },
        "today": sorted(decorate(today_rows),
                        key=lambda a: str(a.get("time", ""))),
        "activity": recent_activity(doctors),
    })


# --------------------------------------------------------------------------
# The doctor list
# --------------------------------------------------------------------------
@router.get("/doctors", summary="Every doctor, with clinic and workload")
def list_all_doctors(admin: str = Depends(auth.current_admin),
                     q: str | None = Query(None),
                     status: str = Query("listed"),
                     specialization: str | None = Query(None)) -> Any:
    doctors = read(Collections.DOCTORS)
    clinic = unwrap(ClinicService(repo()).primary())
    appointments = read(Collections.APPOINTMENTS)
    schedules = read(Collections.SCHEDULES)
    blocked = blocked_today_ids()

    rows = [doctor_row(d, clinic, appointments, schedules, blocked)
            for d in doctors]

    if status == "listed":
        rows = [r for r in rows if r["status"] != "archived"]
    elif status in ("active", "inactive", "archived"):
        rows = [r for r in rows if r["status"] == status]

    if specialization:
        wanted = specialization.strip().lower()
        rows = [r for r in rows
                if str(r.get("specialization", "")).lower() == wanted]

    if q:
        needle = q.strip().lower()

        def matches(row: dict) -> bool:
            haystack = " ".join(str(value or "").lower() for value in [
                row.get("name"), row.get("doctor_id"), row.get("specialization"),
                row.get("qualification"), row.get("phone"), row.get("email")])
            return needle in haystack

        rows = [r for r in rows if matches(r)]

    rows.sort(key=lambda row: str(row.get("name", "")).lower())
    known = sorted({str(d.get("specialization")) for d in doctors
                    if d.get("specialization")})
    return ok({"doctors": rows, "count": len(rows), "specializations": known})


# --------------------------------------------------------------------------
# Adding and editing a doctor
# --------------------------------------------------------------------------
class SessionBody(BaseModel):
    start: str
    end: str


class DayBody(BaseModel):
    day: str
    available: bool = True
    sessions: list[SessionBody] = Field(default_factory=list)


class DoctorBody(BaseModel):
    """Everything the administrator maintains about one doctor.

    The field names are the ones already in the `doctors` and `clinics`
    documents; nothing is duplicated under a new name.
    """
    name: str | None = None
    specialization: str | None = None
    qualification: str | None = None
    experience_years: int | None = None
    phone: str | None = None
    email: str | None = None
    fee: int | None = None
    about: str | None = None
    photo: str | None = None
    active: bool | None = None

    days: list[DayBody] | None = None
    slot_duration: int | None = None

    # Sign-in details, so a doctor can be given an account in the same step
    # as their profile. Both optional: without them the doctor signs in with
    # their id and the starting password.
    username: str | None = None
    password: str | None = None


def required(value, field: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise fail(400, "invalid_" + field, label + " is required.")
    return text


def check_email(value: str | None) -> str:
    email = str(value or "").strip()
    if email and not EMAIL_RE.match(email):
        raise fail(400, "invalid_email", "Enter a valid email address.")
    return email


def check_fee(value) -> int:
    try:
        fee = int(value)
    except (TypeError, ValueError):
        raise fail(400, "invalid_fee", "The consultation fee must be a number.")
    if fee < 0:
        raise fail(400, "invalid_fee", "The consultation fee cannot be negative.")
    return fee


def check_years(value) -> int:
    if value in (None, ""):
        return 0
    try:
        years = int(value)
    except (TypeError, ValueError):
        raise fail(400, "invalid_experience", "Years of experience must be a number.")
    if years < 0 or years > 70:
        raise fail(400, "invalid_experience",
                   "Years of experience must be between 0 and 70.")
    return years


def check_duration(value) -> int:
    if value in (None, ""):
        return 20
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise fail(400, "invalid_duration",
                   "The appointment length must be a number of minutes.")
    if minutes < 5 or minutes > 180:
        raise fail(400, "invalid_duration",
                   "An appointment must be between 5 and 180 minutes.")
    return minutes


def days_payload(days: list[DayBody] | None, slot_duration: int | None) -> list[dict]:
    payload = []
    for day in days or []:
        if day.day not in WEEKDAY_NAMES:
            raise fail(400, "invalid_day", str(day.day) + " is not a weekday.")
        sessions = [{"start": s.start, "end": s.end} for s in day.sessions]
        if slot_duration:
            for session in sessions:
                session["slot_duration"] = slot_duration
        payload.append({"day": day.day, "available": day.available,
                        "sessions": sessions})
    return payload


@router.post("/doctors", status_code=201, summary="Add a doctor")
def create_doctor(body: DoctorBody, admin: str = Depends(auth.current_admin)) -> Any:
    doctors = read(Collections.DOCTORS)

    name = required(body.name, "name", "The doctor's name")
    specialization = required(body.specialization, "specialization",
                              "A specialization")
    phone = required(body.phone, "phone", "A contact number")
    email = check_email(body.email)
    fee = check_fee(body.fee)
    years = check_years(body.experience_years)
    duration = check_duration(body.slot_duration)
    # Everything is validated BEFORE the first write, so a rejected field
    # cannot leave half a doctor behind.
    photo = clean_photo(body.photo)
    days = days_payload(body.days, duration) if body.days else None

    username = str(body.username or "").strip().lower()
    if username:
        problem = check_username_rules(username)
        if problem:
            raise fail(400, "invalid_username", problem)
    if body.password:
        problem = check_password_rules(body.password)
        if problem:
            raise fail(400, "invalid_password", problem)

    doctor_id = next_id([d.get("doctor_id") for d in doctors], "D")

    # Every doctor works at the one clinic; there is nothing to choose.
    clinic = unwrap(ClinicService(repo()).primary())
    clinic_id = clinic["clinic_id"]

    doctor = unwrap(DoctorService(repo()).create_doctor({
        "doctor_id": doctor_id,
        "name": name,
        "specialization": specialization,
        "qualification": str(body.qualification or "").strip(),
        "experience_years": years,
        "phone": phone,
        "email": email,
        "fee": fee,
        "about": str(body.about or "").strip(),
        "photo": photo,
        "clinic_ids": [clinic_id],
        "aliases": aliases_for(name),
        "active": True if body.active is None else bool(body.active),
        "archived": False,
    }), not_found=400)

    if days:
        unwrap(ScheduleService(repo()).set_weekly_schedule(doctor_id, days,
                                                           clinic_id),
               not_found=400)

    accounts = AccountService(repo())
    if username:
        unwrap(accounts.set_username(doctor_id, username,
                                     role=auth.ROLE_DOCTOR,
                                     reserved=reserved_names() - {doctor_id}),
               not_found=400)
    if body.password:
        unwrap(accounts.set_password(doctor_id, body.password,
                                     role=auth.ROLE_DOCTOR), not_found=400)

    logger.info("doctor %s added by %s", doctor_id, admin)
    return ok({"doctor": with_defaults(doctor), "clinic": clinic,
               "account": account_public(doctor_id),
               "warnings": alias_warnings(name, doctor_id, doctors)})


@router.patch("/doctors/{doctor_id}", summary="Edit a doctor")
def update_doctor(body: DoctorBody, doctor_id: str = Path(...),
                  admin: str = Depends(auth.current_admin)) -> Any:
    existing = doctor_or_404(doctor_id)
    doctors = read(Collections.DOCTORS)

    fields: dict = {}
    if body.name is not None:
        fields["name"] = required(body.name, "name", "The doctor's name")
        # The voice assistant matches on these, so they follow the name.
        fields["aliases"] = aliases_for(fields["name"], existing.get("aliases"))
    if body.specialization is not None:
        fields["specialization"] = required(body.specialization, "specialization",
                                            "A specialization")
    if body.phone is not None:
        fields["phone"] = required(body.phone, "phone", "A contact number")
    if body.qualification is not None:
        fields["qualification"] = str(body.qualification).strip()
    if body.about is not None:
        fields["about"] = str(body.about).strip()
    if body.email is not None:
        fields["email"] = check_email(body.email)
    if body.fee is not None:
        fields["fee"] = check_fee(body.fee)
    if body.experience_years is not None:
        fields["experience_years"] = check_years(body.experience_years)
    if body.photo is not None:
        fields["photo"] = clean_photo(body.photo)
    if body.active is not None:
        fields["active"] = bool(body.active)

    if fields:
        unwrap(DoctorService(repo()).update_doctor(doctor_id, fields),
               not_found=400)

    if body.days is not None:
        clinic_id = unwrap(ClinicService(repo()).primary())["clinic_id"]
        unwrap(ScheduleService(repo()).set_weekly_schedule(
            doctor_id, days_payload(body.days, check_duration(body.slot_duration)),
            clinic_id), not_found=400)

    doctor = doctor_or_404(doctor_id)
    logger.info("doctor %s updated by %s", doctor_id, admin)
    return ok({"doctor": with_defaults(doctor),
               "clinic": unwrap(ClinicService(repo()).primary()),
               "warnings": alias_warnings(doctor.get("name", ""), doctor_id,
                                          doctors)})


# --------------------------------------------------------------------------
# Switching a doctor on and off
# --------------------------------------------------------------------------
class StatusBody(BaseModel):
    active: bool


@router.post("/doctors/{doctor_id}/status", summary="Activate or deactivate")
def set_status(body: StatusBody, doctor_id: str = Path(...),
               admin: str = Depends(auth.current_admin)) -> Any:
    """`active: false` is what the booking rules already read: the voice
    assistant stops offering the doctor and refuses new appointments for
    them. Appointments already in the diary are left alone - use the
    doctor's leave dates to cancel those, which notifies the patients."""
    doctor = doctor_or_404(doctor_id)
    fields = {"active": bool(body.active)}
    if body.active:
        fields["archived"] = False
    unwrap(DoctorService(repo()).update_doctor(doctor_id, fields), not_found=400)

    updated = doctor_or_404(doctor_id)
    upcoming = [a for a in read(Collections.APPOINTMENTS,
                                [("doctor_id", "==", doctor_id)])
                if a.get("status") in ACTIVE_STATUSES
                and str(a.get("date", "")) >= today_iso()]
    logger.info("doctor %s set active=%s by %s", doctor_id, body.active, admin)
    return ok({"doctor": with_defaults(updated),
               "status": status_of(updated),
               "upcoming_appointments": len(upcoming),
               "note": ("New bookings are refused; "
                        + str(len(upcoming)) + " appointment(s) already in the "
                        "diary were not touched.") if not body.active
                       else str(doctor.get("name")) + " is bookable again."})


@router.delete("/doctors/{doctor_id}", summary="Remove a doctor (archive)")
def remove_doctor(doctor_id: str = Path(...),
                  admin: str = Depends(auth.current_admin)) -> Any:
    """Archives the doctor: hidden from the list and unbookable, with every
    appointment, patient and message kept. Deleting the record outright
    would orphan the appointments that point at it."""
    doctor_or_404(doctor_id)
    unwrap(DoctorService(repo()).update_doctor(doctor_id, {
        "active": False, "archived": True, "archived_at": now_stamp()}),
        not_found=400)
    kept = len(read(Collections.APPOINTMENTS, [("doctor_id", "==", doctor_id)]))
    logger.info("doctor %s archived by %s", doctor_id, admin)
    return ok({"doctor_id": doctor_id, "status": "archived",
               "appointments_kept": kept,
               "note": "The record is archived, not deleted. "
                       "Restore it at any time."})


@router.post("/doctors/{doctor_id}/restore", summary="Restore an archived doctor")
def restore_doctor(doctor_id: str = Path(...),
                   admin: str = Depends(auth.current_admin)) -> Any:
    doctor_or_404(doctor_id)
    unwrap(DoctorService(repo()).update_doctor(doctor_id, {
        "archived": False}), not_found=400)
    doctor = doctor_or_404(doctor_id)
    logger.info("doctor %s restored by %s", doctor_id, admin)
    return ok({"doctor": with_defaults(doctor), "status": status_of(doctor),
               "note": "Restored, still deactivated. Activate the doctor to "
                       "make them bookable again."})


# --------------------------------------------------------------------------
# One doctor in detail - the same data the doctor sees, read by the
# administrator through the doctor-side handlers (no second implementation).
# --------------------------------------------------------------------------
def _cancelled_per_date(appointments: list[dict]) -> dict:
    """date -> how many of this doctor's appointments a leave cancelled."""
    counts: dict = {}
    for appointment in appointments:
        if appointment.get("status") == Status.CANCELLED_BY_DOCTOR:
            date = appointment.get("date")
            counts[date] = counts.get(date, 0) + 1
    return counts


@router.get("/doctors/{doctor_id}", summary="One doctor, with schedule and workload")
def doctor_detail(doctor_id: str = Path(...),
                  admin: str = Depends(auth.current_admin)) -> Any:
    doctor = doctor_or_404(doctor_id)
    clinic = unwrap(ClinicService(repo()).primary())
    appointments = read(Collections.APPOINTMENTS)
    schedules = read(Collections.SCHEDULES)
    row = doctor_row(doctor, clinic, appointments, schedules, blocked_today_ids())

    mine = decorate([a for a in appointments if a.get("doctor_id") == doctor_id])
    mine.sort(key=lambda a: (str(a.get("date", "")), str(a.get("time", ""))))
    today = today_iso()

    return ok({
        "doctor": row,
        "clinic": row["clinic"],
        "schedule": doctor_api.schedule(doctor_id)["data"]["days"],
        "leave": doctor_api.leave(doctor_id)["data"]["leave"],
        "appointments": {
            "upcoming": [a for a in mine if str(a.get("date", "")) >= today
                         and a.get("status") in ACTIVE_STATUSES][:20],
            "recent": list(reversed([a for a in mine
                                     if str(a.get("date", "")) < today]))[:10],
            "counts": row["appointments"],
            # what each blocked date actually cancelled, for the leave list
            "on_leave_dates": _cancelled_per_date(mine),
        },
    })


@router.get("/doctors/{doctor_id}/appointments", summary="A doctor's diary")
def doctor_appointments(doctor_id: str = Path(...),
                        scope: str = Query("all"),
                        date: str | None = Query(None),
                        status: str | None = Query(None),
                        q: str | None = Query(None),
                        admin: str = Depends(auth.current_admin)) -> Any:
    doctor_or_404(doctor_id)
    return doctor_api.appointments(doctor_id=doctor_id, scope=scope, date=date,
                                   status=status, query=q)


@router.get("/doctors/{doctor_id}/availability", summary="Free slots for a date")
def doctor_availability(doctor_id: str = Path(...), date: str = Query(...),
                        admin: str = Depends(auth.current_admin)) -> Any:
    """Used when the administrator moves an appointment, so only slots the
    booking engine would really accept are offered."""
    doctor_or_404(doctor_id)
    return doctor_api.availability(date=date, doctor_id=doctor_id)


@router.get("/doctors/{doctor_id}/schedule", summary="A doctor's working week")
def doctor_schedule(doctor_id: str = Path(...),
                    admin: str = Depends(auth.current_admin)) -> Any:
    doctor_or_404(doctor_id)
    return doctor_api.schedule(doctor_id)


@router.patch("/doctors/{doctor_id}/schedule", summary="Set a doctor's working week")
def set_doctor_schedule(body: doctor_api.ScheduleBody, doctor_id: str = Path(...),
                        admin: str = Depends(auth.current_admin)) -> Any:
    doctor_or_404(doctor_id)
    return doctor_api.update_schedule(body, doctor_id)


@router.get("/doctors/{doctor_id}/leave", summary="Dates a doctor is away")
def doctor_leave(doctor_id: str = Path(...),
                 admin: str = Depends(auth.current_admin)) -> Any:
    doctor_or_404(doctor_id)
    return doctor_api.leave(doctor_id)


@router.post("/doctors/{doctor_id}/leave", summary="Block dates for a doctor")
def add_doctor_leave(body: doctor_api.LeaveBody, doctor_id: str = Path(...),
                     admin: str = Depends(auth.current_admin)) -> Any:
    """Runs the existing cascade: the appointments on those dates become
    `cancelled_by_doctor` and one message per patient is queued."""
    doctor_or_404(doctor_id)
    return doctor_api.add_leave(body, doctor_id)


@router.delete("/doctors/{doctor_id}/leave/{date}", summary="Re-open a blocked date")
def remove_doctor_leave(doctor_id: str = Path(...), date: str = Path(...),
                        admin: str = Depends(auth.current_admin)) -> Any:
    doctor_or_404(doctor_id)
    return doctor_api.remove_leave(date, doctor_id)


# --------------------------------------------------------------------------
# Sign-in credentials
#
# The administrator issues and resets them; a doctor can only change their
# own password (POST /auth/password). Password hashes live in
# `dashboard_accounts` and never leave firebase/account_service.py.
# --------------------------------------------------------------------------
class CredentialsBody(BaseModel):
    username: str | None = None
    password: str | None = None


def reserved_names() -> set:
    """Ids somebody can already sign in with, so a username cannot shadow one."""
    names = {auth.admin_id()}
    for doctor in read(Collections.DOCTORS):
        if doctor.get("doctor_id"):
            names.add(str(doctor["doctor_id"]))
    return names


@router.get("/doctors/{doctor_id}/credentials", summary="A doctor's sign-in details")
def doctor_credentials(doctor_id: str = Path(...),
                       admin: str = Depends(auth.current_admin)) -> Any:
    doctor_or_404(doctor_id)
    return ok(account_public(doctor_id))


@router.patch("/doctors/{doctor_id}/credentials",
              summary="Set a doctor's username or password")
def set_doctor_credentials(body: CredentialsBody, doctor_id: str = Path(...),
                           admin: str = Depends(auth.current_admin)) -> Any:
    doctor = doctor_or_404(doctor_id)
    service = AccountService(repo())
    changed = []

    if body.username is not None:
        unwrap(service.set_username(doctor_id, body.username,
                                    role=auth.ROLE_DOCTOR,
                                    reserved=reserved_names() - {doctor_id}),
               not_found=400)
        changed.append("username")

    if body.password is not None:
        problem = check_password_rules(body.password)
        if problem:
            raise fail(400, "invalid_password", problem)
        unwrap(service.set_password(doctor_id, body.password,
                                    role=auth.ROLE_DOCTOR), not_found=400)
        changed.append("password")

    if not changed:
        raise fail(400, "nothing_to_do", "Give a username, a password, or both.")

    # A password change signs that doctor out of any tab they left open.
    ended = auth.end_sessions_for(doctor_id) if "password" in changed else 0
    logger.info("%s changed the %s of %s", admin, " and ".join(changed), doctor_id)
    return ok({"account": account_public(doctor_id), "changed": changed,
               "sessions_ended": ended,
               "note": str(doctor.get("name")) + " signs in with their username "
                       "or their doctor ID."})


# --------------------------------------------------------------------------
# The clinic
#
# There is one. Its name and address are what the assistant reads to callers
# and what every screen shows, so they are edited here and nowhere else - a
# doctor's own profile page cannot touch them.
# --------------------------------------------------------------------------
class ClinicBody(BaseModel):
    name: str | None = None
    address: str | None = None
    city: str | None = None
    phone: str | None = None


def clinic_payload() -> dict:
    """The clinic, plus anything left over from before it was centralised."""
    clinic = unwrap(ClinicService(repo()).primary())
    doctors = [d for d in read(Collections.DOCTORS) if not d.get("archived")]
    strays = [c for c in read(Collections.CLINICS)
              if c.get("clinic_id") != clinic.get("clinic_id")
              and c.get("active", True)]
    misfiled = [d for d in doctors
                if clinic.get("clinic_id") not in (d.get("clinic_ids") or [])]
    return {
        "clinic": clinic,
        "doctors": len(doctors),
        "needs_consolidation": bool(strays or misfiled),
        "old_records": [{"clinic_id": c.get("clinic_id"), "name": c.get("name")}
                        for c in strays],
        "doctors_elsewhere": [{"doctor_id": d.get("doctor_id"), "name": d.get("name")}
                              for d in misfiled],
    }


@router.get("/clinic", summary="The clinic every doctor works at")
def get_clinic(admin: str = Depends(auth.current_admin)) -> Any:
    return ok(clinic_payload())


@router.patch("/clinic", summary="Edit the clinic")
def update_clinic(body: ClinicBody, admin: str = Depends(auth.current_admin)) -> Any:
    clinic = unwrap(ClinicService(repo()).primary())
    fields: dict = {}
    if body.name is not None:
        fields["name"] = required(body.name, "name", "The clinic name")
    if body.address is not None:
        fields["address"] = required(body.address, "address", "The clinic address")
    if body.city is not None:
        fields["city"] = str(body.city).strip()
    if body.phone is not None:
        fields["phone"] = str(body.phone).strip()
    if not fields:
        raise fail(400, "nothing_to_do", "Nothing was changed.")

    unwrap(ClinicService(repo()).update_clinic(clinic["clinic_id"], fields),
           not_found=400)
    logger.info("clinic updated by %s", admin)
    return ok(clinic_payload())


@router.post("/clinic/consolidate",
             summary="Move every doctor onto the one clinic record")
def consolidate_clinic(admin: str = Depends(auth.current_admin)) -> Any:
    """For a database that still has more than one clinic from an earlier
    version: point every doctor at the one clinic and switch the leftover
    records off. Nothing is deleted, and appointments keep the id they were
    written with - their clinic name is read centrally anyway."""
    clinic = unwrap(ClinicService(repo()).primary())
    clinic_id = clinic["clinic_id"]
    moved, retired = [], []

    for doctor in read(Collections.DOCTORS):
        if clinic_id not in (doctor.get("clinic_ids") or []):
            unwrap(DoctorService(repo()).update_doctor(
                doctor["doctor_id"], {"clinic_ids": [clinic_id]}), not_found=400)
            moved.append(doctor.get("doctor_id"))

    for record in read(Collections.CLINICS):
        if record.get("clinic_id") != clinic_id and record.get("active", True):
            unwrap(ClinicService(repo()).update_clinic(record["clinic_id"],
                                                       {"active": False}),
                   not_found=400)
            retired.append(record.get("clinic_id"))

    logger.info("clinic consolidated by %s: %d doctor(s) moved, %d record(s) retired",
                admin, len(moved), len(retired))
    return ok({**clinic_payload(), "doctors_moved": moved,
               "records_retired": retired})


# --------------------------------------------------------------------------
# Every doctor's appointments
# --------------------------------------------------------------------------
@router.get("/appointments", summary="Appointments across the whole clinic")
def all_appointments(admin: str = Depends(auth.current_admin),
                     doctor_id: str | None = Query(None),
                     date: str | None = Query(None),
                     status: str | None = Query(None),
                     scope: str = Query("all"),
                     q: str | None = Query(None),
                     limit: int = Query(200, ge=1, le=1000)) -> Any:
    today, now = today_iso(), now_hhmm()
    names = {d.get("doctor_id"): d.get("name") for d in read(Collections.DOCTORS)}
    rows = decorate(read(Collections.APPOINTMENTS))
    for row in rows:
        row["doctor_name"] = names.get(row.get("doctor_id")) or row.get("doctor_id")

    if doctor_id:
        rows = [a for a in rows if a.get("doctor_id") == doctor_id.upper()]
    if date:
        rows = [a for a in rows if a.get("date") == date]
    if status:
        rows = [a for a in rows if a.get("status") == status]

    if scope == "today":
        rows = [a for a in rows if a.get("date") == today]
    elif scope == "upcoming":
        rows = [a for a in rows
                if a.get("status") in ACTIVE_STATUSES
                and (str(a.get("date", "")) > today
                     or (a.get("date") == today and str(a.get("time", "")) >= now))]
    elif scope == "past":
        rows = [a for a in rows if str(a.get("date", "")) < today]
    elif scope == "cancelled":
        rows = [a for a in rows if a.get("status") in (Status.CANCELLED,
                                                       Status.CANCELLED_BY_DOCTOR)]

    if q:
        needle = q.strip().lower()
        rows = [a for a in rows
                if needle in " ".join(str(value or "").lower() for value in [
                    a.get("patient_name"), a.get("patient_phone"),
                    a.get("appointment_id"), a.get("doctor_name"),
                    a.get("doctor_id"), a.get("date")])]

    rows.sort(key=lambda a: (str(a.get("date", "")), str(a.get("time", ""))),
              reverse=True)
    return ok({"appointments": rows[:limit], "count": len(rows),
               "doctors": [{"doctor_id": key, "name": value}
                           for key, value in sorted(names.items(),
                                                    key=lambda pair: str(pair[1]))]})


@router.get("/statistics", summary="Appointment counts for the whole clinic")
def clinic_statistics(admin: str = Depends(auth.current_admin),
                      period: str = Query("month"),
                      start: str | None = Query(None),
                      end: str | None = Query(None),
                      doctor_id: str | None = Query(None)) -> Any:
    """The clinic's numbers, and the same breakdown per doctor.

    With `doctor_id` the totals narrow to that one doctor while the
    doctor-wise table still lists everybody, so the administrator can compare
    one against the rest.
    """
    doctors = [d for d in read(Collections.DOCTORS) if not d.get("archived")]
    appointments = read(Collections.APPOINTMENTS)

    selected = None
    if doctor_id:
        selected = doctor_or_404(doctor_id)
        scoped = [a for a in appointments if a.get("doctor_id") == doctor_id.upper()]
    else:
        scoped = appointments

    try:
        payload = stats.statistics(scoped, doctors=None, period=period,
                                   start=start, end=end)
        window = payload["period"]
        payload["doctors"] = stats.per_doctor(
            stats.in_period(appointments, window["start"], window["end"]), doctors)
    except ValueError as problem:
        raise fail(400, "invalid_period", str(problem))

    payload["doctor"] = ({"doctor_id": selected.get("doctor_id"),
                          "name": selected.get("name")} if selected else None)
    payload["doctors_counted"] = len(doctors)
    return ok(payload)


@router.get("/appointments/{appointment_id}", summary="One appointment in full")
def appointment_details(appointment_id: str = Path(...),
                        admin: str = Depends(auth.current_admin)) -> Any:
    result = backend().get_appointment(appointment_id)
    if not result.success:
        raise fail(404, "not_found", "No appointment with that ID.")
    appointment = (result.data or {}).get("appointment") or result.data
    row = decorate([appointment])[0]
    names = {d.get("doctor_id"): d.get("name") for d in read(Collections.DOCTORS)}
    row["doctor_name"] = names.get(row.get("doctor_id"))
    return ok(row)


class CancelBody(BaseModel):
    reason: str = "Cancelled by the clinic"


@router.post("/appointments/{appointment_id}/cancel",
             summary="Cancel an appointment (status change only)")
def cancel_appointment(appointment_id: str = Path(...),
                       body: CancelBody | None = None,
                       admin: str = Depends(auth.current_admin)) -> Any:
    """Runs the backend's own cancellation, records who cancelled it, and
    queues a message for the patient. The record is kept."""
    reason = (body.reason if body else None) or "Cancelled by the clinic"
    logger.info("appointment %s cancelled by %s", appointment_id, admin)
    return respond(backend().clinic_cancel_appointment(appointment_id, reason))


@router.post("/appointments/{appointment_id}/reschedule",
             summary="Move an appointment")
def reschedule_appointment(body: doctor_api.RescheduleBody,
                           appointment_id: str = Path(...),
                           admin: str = Depends(auth.current_admin)) -> Any:
    return respond(backend().reschedule_appointment(appointment_id, body.date,
                                                    body.time))


@router.post("/appointments/{appointment_id}/complete",
             summary="Mark a consultation finished")
def complete_appointment(appointment_id: str = Path(...),
                         admin: str = Depends(auth.current_admin)) -> Any:
    return respond(backend().complete_appointment(appointment_id))


# --------------------------------------------------------------------------
# What blocking a date would do, before it is done
# --------------------------------------------------------------------------
@router.get("/doctors/{doctor_id}/leave/preview",
            summary="Which appointments blocking these dates would cancel")
def leave_preview(doctor_id: str = Path(...),
                  start_date: str = Query(...),
                  end_date: str | None = Query(None),
                  admin: str = Depends(auth.current_admin)) -> Any:
    doctor = doctor_or_404(doctor_id)
    start = start_date
    end = end_date or start_date
    if end < start:
        raise fail(400, "invalid_date",
                   "The end date must be on or after the start date.")
    try:
        first = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise fail(400, "invalid_date", "Dates must look like YYYY-MM-DD.")
    if (last - first).days > 60:
        raise fail(400, "invalid_date", "Block at most 60 days at a time.")

    wanted = {(first + timedelta(days=offset)).isoformat()
              for offset in range((last - first).days + 1)}
    affected = [a for a in decorate(read(Collections.APPOINTMENTS,
                                         [("doctor_id", "==", doctor_id)]))
                if a.get("date") in wanted and a.get("status") in ACTIVE_STATUSES]
    affected.sort(key=lambda a: (str(a.get("date", "")), str(a.get("time", ""))))

    by_date: dict = {}
    for appointment in affected:
        by_date.setdefault(appointment["date"], []).append(appointment)

    return ok({
        "doctor_id": doctor_id,
        "doctor_name": doctor.get("name"),
        "dates": sorted(wanted),
        "appointments": affected,
        "patients": len({a.get("patient_id") for a in affected}),
        "by_date": [{"date": date, "count": len(rows)}
                    for date, rows in sorted(by_date.items())],
        "note": "Blocking these dates cancels these appointments and queues "
                "one message per patient. Nothing is deleted.",
    })
