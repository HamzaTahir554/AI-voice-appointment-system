"""
Appointment statistics.

Counting appointment records, and nothing else: every function here is pure,
takes the records it is given and returns numbers. The API layers decide
WHICH records a caller may see - a doctor passes their own, the
administrator passes the clinic's - so the scoping rules stay next to the
sign-in checks and the arithmetic stays here where it can be tested.

The statuses are the backend's own (config.Status) and no new ones are
invented. Two of them need a word of explanation:

    rescheduled          a live appointment that was moved; it is reported
                         separately rather than folded into "confirmed", so
                         the numbers add up to the total exactly once
    cancelled_by_doctor  written by the doctor-unavailable cascade; it is
                         counted as a cancellation, and also reported on its
                         own so "who cancelled this" stays answerable
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from config import Status, TIMEZONE

# Every status a record can really carry, in the order people read them.
ALL_STATUSES = (
    Status.PENDING,
    Status.CONFIRMED,
    Status.RESCHEDULED,
    Status.COMPLETED,
    Status.CANCELLED,
    Status.CANCELLED_BY_DOCTOR,
)

LIVE_STATUSES = (Status.PENDING, Status.CONFIRMED, Status.RESCHEDULED)
CANCELLED_STATUSES = (Status.CANCELLED, Status.CANCELLED_BY_DOCTOR)

PERIODS = ("today", "week", "month", "all", "custom")


def today_iso() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


# --------------------------------------------------------------------------
# Which dates a period covers
# --------------------------------------------------------------------------
def resolve_period(period: str = "month", start: str | None = None,
                   end: str | None = None, today: str | None = None) -> dict:
    """
    Turn a period name into a pair of dates.

    Returns {"id", "label", "start", "end"}; start and end are inclusive
    YYYY-MM-DD strings, or None for "every record ever". A bad date or a
    backwards range raises ValueError with something worth showing a person.
    """
    reference = date.fromisoformat(today) if today else date.fromisoformat(today_iso())
    period = (period or "month").strip().lower()

    if period == "today":
        return {"id": "today", "label": "Today",
                "start": reference.isoformat(), "end": reference.isoformat()}

    if period == "week":
        monday = reference - timedelta(days=reference.weekday())
        sunday = monday + timedelta(days=6)
        return {"id": "week", "label": "This week",
                "start": monday.isoformat(), "end": sunday.isoformat()}

    if period == "month":
        first = reference.replace(day=1)
        next_month = (first + timedelta(days=32)).replace(day=1)
        last = next_month - timedelta(days=1)
        return {"id": "month", "label": "This month",
                "start": first.isoformat(), "end": last.isoformat()}

    if period == "all":
        return {"id": "all", "label": "All time", "start": None, "end": None}

    if period == "custom":
        if not start or not end:
            raise ValueError("Choose both a start and an end date.")
        try:
            first = date.fromisoformat(start)
            last = date.fromisoformat(end)
        except ValueError:
            raise ValueError("Dates must look like YYYY-MM-DD.")
        if last < first:
            raise ValueError("The end date must be on or after the start date.")
        return {"id": "custom",
                "label": ("On " + first.isoformat()) if first == last
                         else (first.isoformat() + " to " + last.isoformat()),
                "start": first.isoformat(), "end": last.isoformat()}

    raise ValueError("Choose one of: " + ", ".join(PERIODS) + ".")


def within(appointment: dict, start: str | None, end: str | None) -> bool:
    """Is this appointment's DATE inside the period?

    The appointment date is what people mean by "appointments this week" -
    not when the record happened to be written.

    With no bounds ("all time") every record counts, including one whose
    date is missing; otherwise such a record could not be placed in any
    period and the totals would disagree with the all-time figure.
    """
    if not start and not end:
        return True
    when = str(appointment.get("date") or "")
    if not when:
        return False
    if start and when < start:
        return False
    if end and when > end:
        return False
    return True


def in_period(appointments: list[dict], start: str | None,
              end: str | None) -> list[dict]:
    return [a for a in appointments if within(a, start, end)]


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------
def summarise(appointments: list[dict]) -> dict:
    """Counts per status, and the headline numbers a dashboard shows.

    Every record lands in exactly one status bucket, so the buckets add up
    to `total`. A record with a status nobody recognises is counted under
    `other` rather than being quietly dropped.
    """
    rows = list(appointments or [])
    by_status = {status: 0 for status in ALL_STATUSES}
    other = 0

    for appointment in rows:
        status = appointment.get("status")
        if status in by_status:
            by_status[status] += 1
        else:
            other += 1

    cancelled = sum(by_status[status] for status in CANCELLED_STATUSES)
    return {
        "total": len(rows),
        "pending": by_status[Status.PENDING],
        "confirmed": by_status[Status.CONFIRMED],
        "rescheduled": by_status[Status.RESCHEDULED],
        "completed": by_status[Status.COMPLETED],
        "cancelled": cancelled,
        "cancelled_by_patient": by_status[Status.CANCELLED],
        "cancelled_by_doctor": by_status[Status.CANCELLED_BY_DOCTOR],
        "live": sum(by_status[status] for status in LIVE_STATUSES),
        "other": other,
        "by_status": dict(by_status),
    }


def summarise_counts(total: int, by_status: dict) -> dict:
    """`summarise()` for when only the counts are known - from Firestore
    count() aggregations, which count without downloading a record.

    Same shape and the same arithmetic: whatever is in `total` but in none of
    the known statuses is `other`, so the buckets still add up to the total.
    """
    counts = {status: int(by_status.get(status, 0) or 0) for status in ALL_STATUSES}
    total = int(total or 0)
    cancelled = sum(counts[status] for status in CANCELLED_STATUSES)
    return {
        "total": total,
        "pending": counts[Status.PENDING],
        "confirmed": counts[Status.CONFIRMED],
        "rescheduled": counts[Status.RESCHEDULED],
        "completed": counts[Status.COMPLETED],
        "cancelled": cancelled,
        "cancelled_by_patient": counts[Status.CANCELLED],
        "cancelled_by_doctor": counts[Status.CANCELLED_BY_DOCTOR],
        "live": sum(counts[status] for status in LIVE_STATUSES),
        # Two counts taken a moment apart can straddle a write; never negative.
        "other": max(0, total - sum(counts.values())),
        "by_status": dict(counts),
    }


def per_doctor(appointments: list[dict], doctors: list[dict]) -> list[dict]:
    """One row per doctor, busiest first.

    Every doctor on the register gets a row, including the ones with nothing
    booked - a zero is an answer, an absent row looks like a bug.
    """
    grouped: dict = {}
    for appointment in appointments or []:
        grouped.setdefault(appointment.get("doctor_id"), []).append(appointment)
    return per_doctor_from_summaries(
        {doctor_id: summarise(rows) for doctor_id, rows in grouped.items()}, doctors)


def per_doctor_from_summaries(summaries: dict, doctors: list[dict]) -> list[dict]:
    """`per_doctor()` from counts already made - one summarise()-shaped dict
    per doctor id, e.g. from count() aggregations for "all time"."""
    rows = []
    for doctor in doctors or []:
        doctor_id = doctor.get("doctor_id")
        counts = summaries.get(doctor_id) or summarise([])
        rows.append({
            "doctor_id": doctor_id,
            "name": doctor.get("name") or doctor_id,
            "specialization": doctor.get("specialization"),
            "active": bool(doctor.get("active", True)),
            **counts,
        })

    rows.sort(key=lambda row: (-row["total"], str(row["name"]).lower()))
    return rows


def statistics(appointments: list[dict], doctors: list[dict] | None = None,
               period: str = "month", start: str | None = None,
               end: str | None = None, today: str | None = None) -> dict:
    """The whole payload a statistics screen needs, from one list of records."""
    window = resolve_period(period, start, end, today)
    day = today or today_iso()
    selected = in_period(appointments, window["start"], window["end"])

    payload = {
        "period": window,
        "totals": summarise(selected),
        "today": {"date": day, **summarise(in_period(appointments, day, day))},
        "all_time": summarise(appointments),
    }
    if doctors is not None:
        payload["doctors"] = per_doctor(selected, doctors)
    return payload


def statistics_from_parts(window: dict, period_rows: list[dict] | None,
                          today_rows: list[dict], all_time: dict,
                          day: str | None = None) -> dict:
    """The same payload as `statistics()`, assembled from narrower reads:

        period_rows  only the records dated inside the window
                     (None when the window is "all time": the totals are
                     then the all-time counts)
        today_rows   only today's records
        all_time     summarise_counts() of every record, from aggregations

    `statistics()` stays the reference; the tests check both agree on the
    same data.
    """
    day = day or today_iso()
    return {
        "period": window,
        "totals": dict(all_time) if period_rows is None else summarise(period_rows),
        "today": {"date": day, **summarise(today_rows)},
        "all_time": dict(all_time),
    }
