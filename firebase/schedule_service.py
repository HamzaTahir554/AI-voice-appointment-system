"""
Schedule service - `schedules` and `doctor_unavailability` collections.

Turns a doctor's weekly working pattern into concrete bookable times, and
enforces the days a doctor has blocked out from the dashboard.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from config import Collections, TIMEZONE
from firebase.firebase_config import DatabaseError, get_repository
from firebase.result import ServiceResult

logger = logging.getLogger(__name__)

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]


def weekday_of(iso_date: str) -> str | None:
    """'2026-09-15' -> 'Tuesday'. None if the date is malformed."""
    try:
        return WEEKDAY_NAMES[datetime.strptime(iso_date, "%Y-%m-%d").weekday()]
    except (ValueError, TypeError):
        return None


def today_iso() -> str:
    """Today in Asia/Karachi - never the server's UTC date."""
    return datetime.now(TIMEZONE).date().isoformat()


def _to_minutes(time_str: str) -> int | None:
    try:
        hour, minute = str(time_str).split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None


def _to_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class ScheduleService:
    def __init__(self, repository=None):
        self.repo = repository or get_repository()

    # ------------------------------------------------------------------
    def get_schedules(self, doctor_id: str,
                      weekday: str | None = None) -> ServiceResult:
        filters = [("doctor_id", "==", doctor_id), ("active", "==", True)]
        if weekday:
            filters.append(("day", "==", weekday))
        try:
            docs = self.repo.query(Collections.SCHEDULES, filters)
        except DatabaseError as exc:
            logger.error("get_schedules failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(docs)

    # ------------------------------------------------------------------
    def is_doctor_unavailable(self, doctor_id: str,
                              iso_date: str) -> ServiceResult:
        """
        Has the doctor blocked this date from their dashboard?

        `data` is the blocking record, or None when the doctor is working.
        """
        try:
            docs = self.repo.query(
                Collections.UNAVAILABILITY,
                [("doctor_id", "==", doctor_id), ("date", "==", iso_date),
                 ("active", "==", True)], limit=1)
        except DatabaseError as exc:
            logger.error("is_doctor_unavailable failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(docs[0] if docs else None)

    def mark_unavailable(self, doctor_id: str, iso_date: str,
                         reason: str = "Doctor unavailable") -> ServiceResult:
        doc_id = f"{doctor_id}_{iso_date}"
        try:
            saved = self.repo.set(Collections.UNAVAILABILITY, doc_id, {
                "doctor_id": doctor_id, "date": iso_date,
                "reason": reason, "active": True,
            })
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def list_unavailable(self, doctor_id: str,
                         from_date: str | None = None) -> ServiceResult:
        """Blocked dates for a doctor, soonest first (dashboard leave list)."""
        try:
            docs = self.repo.query(
                Collections.UNAVAILABILITY,
                [("doctor_id", "==", doctor_id), ("active", "==", True)])
        except DatabaseError as exc:
            logger.error("list_unavailable failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        start = from_date or today_iso()
        rows = [d for d in docs if str(d.get("date", "")) >= start]
        rows.sort(key=lambda d: d.get("date", ""))
        return ServiceResult.success(rows)

    def clear_unavailable(self, doctor_id: str, iso_date: str) -> ServiceResult:
        """
        Re-open a blocked date.

        Appointments cancelled when the date was blocked stay cancelled: the
        patients were told. They must rebook, which is what the voice
        assistant is for.
        """
        doc_id = f"{doctor_id}_{iso_date}"
        try:
            existing = self.repo.get(Collections.UNAVAILABILITY, doc_id)
            if existing is None:
                return ServiceResult.failure(
                    "NOT_FOUND", f"{iso_date} is not blocked for {doctor_id}")
            saved = self.repo.update(Collections.UNAVAILABILITY, doc_id,
                                     {"active": False})
        except DatabaseError as exc:
            logger.error("clear_unavailable failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    # ------------------------------------------------------------------
    def set_weekly_schedule(self, doctor_id: str, days: list[dict],
                            clinic_id: str | None = None) -> ServiceResult:
        """
        Replace a doctor's weekly working pattern (dashboard schedule page).

        `days` is [{"day": "Monday", "available": true,
                    "sessions": [{"start": "09:00", "end": "13:00"}]}, ...].

        Rows are written with deterministic ids ({doctor}_{DAY}_{n}) so saving
        again overwrites the same documents instead of piling up new ones, and
        rows that are no longer used are deactivated rather than deleted.
        Slot duration is carried over from the doctor's existing schedule.
        """
        if not doctor_id:
            return ServiceResult.failure("INVALID_INPUT", "doctor_id required")

        try:
            existing = self.repo.query(Collections.SCHEDULES,
                                       [("doctor_id", "==", doctor_id)])
        except DatabaseError as exc:
            logger.error("set_weekly_schedule read failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))

        duration = 20
        for row in existing:
            if row.get("slot_duration"):
                duration = int(row["slot_duration"])
                break
        if clinic_id is None:
            clinic_id = next((r.get("clinic_id") for r in existing if r.get("clinic_id")), None)

        written, keep = [], set()
        for entry in days or []:
            day = entry.get("day")
            if day not in WEEKDAY_NAMES:
                return ServiceResult.failure("INVALID_INPUT", f"unknown day '{day}'")
            if not entry.get("available"):
                continue
            for index, session in enumerate(entry.get("sessions") or [], start=1):
                start, end = session.get("start"), session.get("end")
                start_min, end_min = _to_minutes(start), _to_minutes(end)
                if start_min is None or end_min is None:
                    return ServiceResult.failure("INVALID_INPUT",
                                                 f"{day}: '{start}' to '{end}' is not a valid time range")
                if end_min <= start_min:
                    return ServiceResult.failure("INVALID_INPUT",
                                                 f"{day}: the end time must be after the start time")
                schedule_id = f"{doctor_id}_{day.upper()[:3]}_{index}"
                keep.add(schedule_id)
                record = {
                    "schedule_id": schedule_id, "doctor_id": doctor_id,
                    "clinic_id": clinic_id, "day": day,
                    "start_time": start, "end_time": end,
                    "slot_duration": int(session.get("slot_duration") or duration),
                    "active": True,
                }
                try:
                    written.append(self.repo.set(Collections.SCHEDULES, schedule_id, record))
                except DatabaseError as exc:
                    logger.error("set_weekly_schedule write failed: %s", exc)
                    return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))

        # Anything the doctor removed (or the old seeded rows) is switched off.
        for row in existing:
            row_id = row.get("schedule_id")
            if row_id and row_id not in keep and row.get("active"):
                try:
                    self.repo.update(Collections.SCHEDULES, row_id, {"active": False})
                except DatabaseError as exc:
                    logger.error("could not deactivate schedule %s: %s", row_id, exc)

        return ServiceResult.success(written)

    # ------------------------------------------------------------------
    def generate_slots(self, doctor_id: str, iso_date: str) -> ServiceResult:
        """
        Every slot the doctor *offers* on this date, before bookings.

        Applies, in order: valid date -> not in the past -> doctor not blocked
        -> a schedule exists for that weekday. Slot times are generated from
        `start_time`, `end_time` and `slot_duration`.
        """
        weekday = weekday_of(iso_date)
        if weekday is None:
            return ServiceResult.failure("INVALID_DATE",
                                         f"'{iso_date}' is not a valid date")
        if iso_date < today_iso():
            return ServiceResult.failure("DATE_IN_PAST",
                                         "That date has already passed")

        blocked = self.is_doctor_unavailable(doctor_id, iso_date)
        if not blocked.ok:
            return blocked
        if blocked.data:
            return ServiceResult.failure(
                "DOCTOR_UNAVAILABLE",
                blocked.data.get("reason", "Doctor unavailable that day"))

        schedules = self.get_schedules(doctor_id, weekday)
        if not schedules.ok:
            return schedules
        if not schedules.data:
            return ServiceResult.failure(
                "NO_SCHEDULE", f"The doctor does not hold clinic on {weekday}")

        slots: list[str] = []
        for schedule in schedules.data:
            start = _to_minutes(schedule.get("start_time"))
            end = _to_minutes(schedule.get("end_time"))
            duration = int(schedule.get("slot_duration", 20) or 20)
            if start is None or end is None or duration <= 0:
                logger.warning("skipping malformed schedule %s", schedule)
                continue
            current = start
            while current + duration <= end:
                slots.append(_to_time(current))
                current += duration
        return ServiceResult.success(sorted(set(slots)))

    # ------------------------------------------------------------------
    def create_schedule(self, schedule: dict) -> ServiceResult:
        schedule_id = schedule.get("schedule_id")
        if not schedule_id:
            return ServiceResult.failure("INVALID_INPUT", "schedule_id required")
        try:
            saved = self.repo.set(Collections.SCHEDULES, schedule_id,
                                  {"active": True, **schedule})
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)
