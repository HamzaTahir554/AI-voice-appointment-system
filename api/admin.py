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

from api import auth, data
from api import dashboard as doctor_api
from appointment_backend import statistics as stats
from appointment_backend.api import backend, respond
from api.dashboard import (account_public, clean_photo, decorate, fail,
                           now_hhmm, ok, page_payload, repo, unwrap,
                           with_defaults)
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
    """Doctors with today blocked - today's leave records only."""
    rows = data.find(Collections.UNAVAILABILITY, [("date", "==", today_iso())])
    return {r.get("doctor_id") for r in rows if r.get("active", True)}


def workload(doctor_id: str, ahead: list[dict], total: int) -> dict:
    """A doctor's counts: `total` from a count() aggregation, today and
    upcoming from the appointments dated today or later."""
    today = today_iso()
    live = [a for a in ahead if a.get("doctor_id") == doctor_id
            and a.get("status") in ACTIVE_STATUSES]
    return {
        "total": total,
        "today": len([a for a in live if a.get("date") == today]),
        "upcoming": len([a for a in live if str(a.get("date", "")) >= today]),
    }


def doctor_row(doctor: dict, clinic: dict | None, counts: dict,
               schedules: list[dict], blocked: set) -> dict:
    doctor_id = doctor.get("doctor_id")
    hours = hours_of([s for s in schedules if s.get("doctor_id") == doctor_id])
    return {
        **with_defaults(doctor),
        "status": status_of(doctor),
        "clinic": clinic,
        "appointments": counts,
        "availability": {
            **hours,
            "on_leave_today": doctor_id in blocked,
            "accepting": bool(doctor.get("active", True))
                         and not doctor.get("archived")
                         and bool(hours["working_days"]),
        },
    }


def schedules_for(doctor_ids: list[str]) -> list[dict]:
    """Schedule rows for these doctors only (`in` takes 30 values a query)."""
    ids = [i for i in dict.fromkeys(doctor_ids) if i]
    chunks = [ids[i:i + 30] for i in range(0, len(ids), 30)]
    rows: list[dict] = []
    for part in data.parallel(*[(lambda c=chunk: data.find(
            Collections.SCHEDULES, [("doctor_id", "in", c)])) for chunk in chunks]):
        rows.extend(part)
    return rows


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


ACTIVITY_LIMIT = 8


def recent_activity(doctors: list[dict], notes: list[dict],
                    limit: int = ACTIVITY_LIMIT) -> list[dict]:
    """What has changed lately.

    Only events the database actually records are listed: doctors carry
    `created_at` / `updated_at` from the moment they are managed here, and
    every patient message the cancellation cascade queues carries its own
    `created_at`. Appointment records have no creation timestamp, so no
    "booked at" entry is invented for them. `notes` are the newest patient
    messages - the only ones that can make the list.
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
    for note in notes:
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


def live_after(day: str) -> int:
    """Live appointments dated after `day`: one count() aggregation (index:
    status + date). Without the index, one read of the dates after `day`."""
    return data.count(Collections.APPOINTMENTS,
                      [("status", "in", list(ACTIVE_STATUSES)), ("date", ">", day)],
                      prefer="range")


@router.get("/summary", summary="Clinic-wide counters and recent activity")
def summary(admin: str = Depends(auth.current_admin)) -> Any:
    """Counted by Firestore, not by downloading the clinic's appointments:
    the totals are count() aggregations, and only today's appointments, the
    doctors on leave today and the newest few messages are read - together,
    in one round trip."""
    today, now = today_iso(), now_hhmm()
    doctors, counts, today_rows, later, blocked, notes = data.parallel(
        data.doctors,
        lambda: data.count_by_status(statuses=(Status.COMPLETED, Status.CANCELLED,
                                               Status.CANCELLED_BY_DOCTOR)),
        lambda: data.appointments(date=today),
        lambda: live_after(today),
        blocked_today_ids,
        lambda: data.find(Collections.NOTIFICATIONS, order_by="created_at",
                          descending=True, limit=ACTIVITY_LIMIT))

    listed = [d for d in doctors if not d.get("archived")]
    live_today = [a for a in today_rows if a.get("status") in ACTIVE_STATUSES]

    return ok({
        "date": today,
        "doctors": {
            "total": len(listed),
            "active": len([d for d in listed if d.get("active", True)]),
            "inactive": len([d for d in listed if not d.get("active", True)]),
            "archived": len([d for d in doctors if d.get("archived")]),
            "on_leave_today": len(blocked),
        },
        "appointments": {
            "total": counts["total"],
            "today": len(today_rows),
            "today_active": len(live_today),
            "upcoming": later + len([a for a in live_today
                                     if str(a.get("time", "")) >= now]),
            "completed": counts[Status.COMPLETED],
            "cancelled": counts[Status.CANCELLED] + counts[Status.CANCELLED_BY_DOCTOR],
        },
        "today": sorted(decorate(today_rows),
                        key=lambda a: str(a.get("time", ""))),
        "activity": recent_activity(doctors, notes),
    })


# --------------------------------------------------------------------------
# The doctor list
# --------------------------------------------------------------------------
@router.get("/doctors", summary="Every doctor, with clinic and workload")
def list_all_doctors(admin: str = Depends(auth.current_admin),
                     q: str | None = Query(None),
                     status: str = Query("listed"),
                     specialization: str | None = Query(None),
                     offset: int = Query(0, ge=0),
                     limit: int = Query(100, ge=1, le=500)) -> Any:
    """Filtering and searching use the register (read once, cached for a
    minute and dropped on any change to a doctor); the workload columns are
    then worked out only for the doctors on this page."""
    doctors = data.doctors()
    rows = list(doctors)

    if status == "listed":
        rows = [d for d in rows if status_of(d) != "archived"]
    elif status in ("active", "inactive", "archived"):
        rows = [d for d in rows if status_of(d) == status]

    if specialization:
        wanted = specialization.strip().lower()
        rows = [d for d in rows
                if str(d.get("specialization", "")).lower() == wanted]

    if q:
        needle = q.strip().lower()

        def matches(row: dict) -> bool:
            haystack = " ".join(str(value or "").lower() for value in [
                row.get("name"), row.get("doctor_id"), row.get("specialization"),
                row.get("qualification"), row.get("phone"), row.get("email")])
            return needle in haystack

        rows = [d for d in rows if matches(d)]

    rows.sort(key=lambda row: str(row.get("name", "")).lower())
    page = rows[offset:offset + limit]
    ids = [d.get("doctor_id") for d in page]

    clinic, schedules, blocked, ahead, *totals = data.parallel(
        data.clinic,
        lambda: schedules_for(ids),
        blocked_today_ids,
        lambda: data.appointments(start=today_iso()),
        *[(lambda d=doctor_id: data.count(Collections.APPOINTMENTS,
                                          [("doctor_id", "==", d)]))
          for doctor_id in ids])

    listed = [doctor_row(d, clinic, workload(d.get("doctor_id"), ahead, total),
                         schedules, blocked)
              for d, total in zip(page, totals)]
    known = sorted({str(d.get("specialization")) for d in doctors
                    if d.get("specialization")})
    return ok({**page_payload("doctors", listed, len(rows), offset, limit,
                              len(rows) > offset + limit),
               "specializations": known})


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
    upcoming = [a for a in data.appointments(doctor_id=doctor_id, start=today_iso())
                if a.get("status") in ACTIVE_STATUSES]
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
    kept = data.count(Collections.APPOINTMENTS, [("doctor_id", "==", doctor_id)])
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


RECENT_SHOWN = 10
UPCOMING_SHOWN = 20


@router.get("/doctors/{doctor_id}", summary="One doctor, with schedule and workload")
def doctor_detail(doctor_id: str = Path(...),
                  admin: str = Depends(auth.current_admin)) -> Any:
    """Everything the detail screen shows, read in one wave: this doctor's
    record, schedule rows, leave, appointments from today on, the ten most
    recent before today and a count of the rest - nothing of anybody else's.
    """
    today = today_iso()
    found, schedule_rows, leave, ahead, recent, total, clinic = data.parallel(
        lambda: DoctorService(repo()).get_doctor(doctor_id),
        lambda: data.find(Collections.SCHEDULES, [("doctor_id", "==", doctor_id)]),
        lambda: unwrap(ScheduleService(repo()).list_unavailable(doctor_id)),
        lambda: data.appointments(doctor_id=doctor_id, start=today),
        lambda: data.page_by_date(
            data.appointment_filters(doctor_id=doctor_id, before=today),
            descending=True, limit=RECENT_SHOWN, with_total=False)[0],
        lambda: data.count(Collections.APPOINTMENTS, [("doctor_id", "==", doctor_id)]),
        data.clinic)
    if not found.ok:
        status = 503 if found.error == "BACKEND_UNAVAILABLE" else 404
        raise fail(status, str(found.error).lower(), found.message or "No such doctor.")
    doctor = found.data

    blocked = {doctor_id} if any(r.get("date") == today and r.get("active", True)
                                 for r in leave) else set()
    row = doctor_row(doctor, clinic, workload(doctor_id, ahead, total),
                     schedule_rows, blocked)

    ahead.sort(key=data.sort_key)
    upcoming = [a for a in ahead if a.get("status") in ACTIVE_STATUSES][:UPCOMING_SHOWN]
    patients = data.patients_by_id(a.get("patient_id") for a in upcoming + recent)
    active_rows = [r for r in schedule_rows if r.get("active", True)]

    return ok({
        "doctor": row,
        "clinic": row["clinic"],
        "schedule": doctor_api.week_from(active_rows),
        "leave": leave,
        "appointments": {
            "upcoming": decorate(upcoming, patients),
            "recent": decorate(recent, patients),
            "counts": row["appointments"],
            # what each blocked date (today or later) actually cancelled
            "on_leave_dates": _cancelled_per_date(ahead),
        },
    })


@router.get("/doctors/{doctor_id}/appointments", summary="A doctor's diary")
def doctor_appointments(doctor_id: str = Path(...),
                        scope: str = Query("all"),
                        date: str | None = Query(None),
                        status: str | None = Query(None),
                        q: str | None = Query(None),
                        offset: int = Query(0, ge=0),
                        limit: int = Query(100, ge=1, le=doctor_api.PAGE_MAX),
                        admin: str = Depends(auth.current_admin)) -> Any:
    doctor_or_404(doctor_id)
    return ok(doctor_api.list_appointments(doctor_id, scope=scope, date=date,
                                           status=status, query=q,
                                           offset=offset, limit=limit))


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
    clinic, everyone, records = data.parallel(data.clinic, data.doctors, data.clinics)
    doctors = [d for d in everyone if not d.get("archived")]
    strays = [c for c in records
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
                     offset: int = Query(0, ge=0),
                     limit: int = Query(200, ge=1, le=1000)) -> Any:
    """One page of the clinic's diary, newest first.

    The doctor and the date window are Firestore filters and the page is
    read in date order, so opening "Upcoming" reads the next page of
    appointments rather than the clinic's entire history. A text search
    reads the window once and pages what matched.
    """
    today, now = today_iso(), now_hhmm()
    names = data.doctor_names()
    doctor_list = [{"doctor_id": key, "name": value}
                   for key, value in sorted(names.items(), key=lambda pair: str(pair[1]))]
    window = {"doctor_id": doctor_id.upper() if doctor_id else None, "date": date}
    checks = []
    if status:
        checks.append(lambda a: a.get("status") == status)
    if scope == "today":
        if date and date != today:
            return ok({**page_payload("appointments", [], 0, offset, limit, False),
                       "doctors": doctor_list})
        window["date"] = today
    elif scope == "upcoming":
        if not date:
            window["start"] = today
        checks.append(lambda a: a.get("status") in ACTIVE_STATUSES
                      and (str(a.get("date", "")) > today
                           or (a.get("date") == today
                               and str(a.get("time", "")) >= now)))
    elif scope == "past":
        if not date:
            window["before"] = today
        checks.append(lambda a: str(a.get("date", "")) < today)
    elif scope == "cancelled":
        checks.append(lambda a: a.get("status") in (Status.CANCELLED,
                                                    Status.CANCELLED_BY_DOCTOR))
    where = (lambda a: all(check(a) for check in checks)) if checks else None
    filters = data.appointment_filters(**window)

    def named(rows: list[dict]) -> list[dict]:
        for row in rows:
            row["doctor_name"] = names.get(row.get("doctor_id")) or row.get("doctor_id")
        return rows

    if q:
        needle = q.strip().lower()
        rows = named(decorate([a for a in data.find(Collections.APPOINTMENTS, filters)
                               if where is None or where(a)]))
        rows = [a for a in rows
                if needle in " ".join(str(value or "").lower() for value in [
                    a.get("patient_name"), a.get("patient_phone"),
                    a.get("appointment_id"), a.get("doctor_name"),
                    a.get("doctor_id"), a.get("date")])]
        data.in_order(rows, descending=True)
        payload = page_payload("appointments", rows[offset:offset + limit], len(rows),
                               offset, limit, len(rows) > offset + limit)
    else:
        page, has_more, total = data.page_by_date(filters, offset=offset, limit=limit,
                                                  descending=True, where=where)
        payload = page_payload("appointments", named(decorate(page)), total,
                               offset, limit, has_more)

    return ok({**payload, "doctors": doctor_list})


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

    Only the records dated inside the period are read (one date-range
    query). All-time figures - and the per-doctor table for "all time" - are
    count() aggregations, so the clinic's history is never downloaded.
    """
    try:
        window = stats.resolve_period(period, start, end)
    except ValueError as problem:
        raise fail(400, "invalid_period", str(problem))

    wanted = doctor_id.upper() if doctor_id else None
    doctors = [d for d in data.doctors() if not d.get("archived")]
    day = today_iso()
    bounded = bool(window["start"] or window["end"])
    covers_today = bounded and (window["start"] or "") <= day <= (window["end"] or "9999")

    groups = {"__scope__": [("doctor_id", "==", wanted)] if wanted else []}
    if not bounded:
        groups.update({d.get("doctor_id"): [("doctor_id", "==", d.get("doctor_id"))]
                       for d in doctors})
    calls = [lambda: data.counts_by_status_for(groups),
             (lambda: doctor_or_404(wanted)) if wanted else (lambda: None)]
    if bounded:
        calls.append(lambda: data.appointments(start=window["start"], end=window["end"]))
    if not covers_today:
        calls.append(lambda: data.appointments(date=day))
    results = data.parallel(*calls)

    counts, selected = results[0], results[1]
    clinic_rows = results[2] if bounded else None
    today_rows = ([a for a in clinic_rows if a.get("date") == day] if covers_today
                  else results[-1])

    def scoped(rows):
        return [a for a in rows if a.get("doctor_id") == wanted] if wanted else rows

    scope_counts = counts["__scope__"]
    payload = stats.statistics_from_parts(
        window, scoped(clinic_rows) if bounded else None, scoped(today_rows),
        stats.summarise_counts(scope_counts["total"], scope_counts), day)
    if bounded:
        payload["doctors"] = stats.per_doctor(clinic_rows, doctors)
    else:
        payload["doctors"] = stats.per_doctor_from_summaries(
            {key: stats.summarise_counts(value["total"], value)
             for key, value in counts.items() if key != "__scope__"}, doctors)

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
    row["doctor_name"] = data.doctor_names().get(row.get("doctor_id"))
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
    # Only this doctor's appointments on those dates are read.
    doctor, window = data.parallel(
        lambda: doctor_or_404(doctor_id),
        lambda: data.appointments(doctor_id=doctor_id, start=start, end=end))
    affected = decorate([a for a in window if a.get("date") in wanted
                         and a.get("status") in ACTIVE_STATUSES])
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
