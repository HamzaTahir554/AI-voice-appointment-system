"""
Reading what the dashboard shows, and nothing more.

Every dashboard and administration endpoint reads Firestore through the
helpers here. They exist because the first version of those endpoints read
whole collections - every patient to put names on five appointments, every
appointment to count one doctor's - and made their reads one after another,
each costing a full round trip to Firestore (0.5-2.3 s measured from the
clinic's connection; see docs/PERFORMANCE.md).

The rules this module follows:

  * Ask Firestore for the records needed, never the collection: filter on
    `doctor_id` and `date` in the query, fetch patients by id in one batch,
    and count with an aggregation instead of downloading.
  * Run reads that do not depend on each other at the same time
    (`parallel`), so a page costs one round trip instead of five.
  * Page through appointments in date order, reading only as far as the
    page being shown (`page_by_date`).
  * Never let a missing Firestore index break a page. A query that needs a
    composite index (`firestore.indexes.json`) is retried without the part
    that needs it and finished here, with the same result; a warning names
    the index to deploy. Correct first, then fast.

The API modules still decide WHAT a caller may see - a doctor's id always
comes from their session - this module only decides how to read it.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from config import Collections
from firebase import cache
from firebase.clinic_service import ClinicService
from firebase.firebase_config import (EQUALITY_OPS, DatabaseError, LocalRepository,
                                      MissingIndex, get_repository, index_needed,
                                      init_repository)

logger = logging.getLogger("api.data")

Filters = list[tuple[str, str, Any]]


def repo():
    return get_repository() or init_repository()


# --------------------------------------------------------------------------
# Running independent reads together
# --------------------------------------------------------------------------
_POOL_PREFIX = "firestore-read"
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix=_POOL_PREFIX)


def parallel(*calls: Callable[[], Any]) -> list:
    """Run independent reads at the same time; results come back in order.

    Each call runs in a copy of the caller's context, so the reads it makes
    are still counted against the request (firebase/metrics.py). A call made
    from inside one of these workers gets a small pool of its own: waiting
    on the shared pool from inside it could exhaust it and deadlock, and
    running the calls one by one would give the round trips back.
    """
    if len(calls) <= 1:
        return [call() for call in calls]
    if threading.current_thread().name.startswith(_POOL_PREFIX):
        with ThreadPoolExecutor(max_workers=min(len(calls), 32),
                                thread_name_prefix=_POOL_PREFIX + "-nested") as pool:
            futures = [pool.submit(contextvars.copy_context().run, call)
                       for call in calls]
            return [future.result() for future in futures]
    futures = [_pool.submit(contextvars.copy_context().run, call) for call in calls]
    return [future.result() for future in futures]


# --------------------------------------------------------------------------
# Queries that survive a missing index
# --------------------------------------------------------------------------
_RECHECK_SECONDS = 600.0
_missing: dict[tuple, float] = {}
_missing_lock = threading.Lock()


def _known_missing(signature) -> bool:
    with _missing_lock:
        seen = _missing.get(signature)
    return seen is not None and time.monotonic() - seen < _RECHECK_SECONDS


def _note_missing(signature, error: Exception) -> None:
    with _missing_lock:
        first = signature not in _missing
        _missing[signature] = time.monotonic()
    if first:
        logger.warning(
            "Firestore index not deployed for %s - answering with a simpler "
            "query instead (same result, more reads). Deploy the indexes in "
            "firestore.indexes.json (docs/PERFORMANCE.md). Firestore said: %s",
            signature, str(error)[:300])


def learn_indexes() -> None:
    """Find out, before anyone is waiting, which declared indexes Firestore
    is missing: one `limit 1` query per index in firestore.indexes.json,
    shaped to need exactly that index, with a value no record has. A
    missing one is logged and remembered, so the first real request goes
    straight to the fallback instead of being refused first."""
    from firebase.firebase_config import declared_indexes

    calls = []
    for collection, fields in declared_indexes():
        *equal, (last, order) = fields
        filters = [(name, "==", "__warm_up__") for name, _ in equal]
        calls.append(lambda c=collection, f=filters, o=last, d=(order == "DESCENDING"):
                     find(c, f, order_by=o, descending=d, limit=1))
    parallel(*calls)


def forget_missing_indexes() -> None:
    """Used by the tests, and after deploying indexes, to try again at once."""
    with _missing_lock:
        _missing.clear()


def _finish_here(rows: list[dict], filters: Filters, order_by: str | None,
                 descending: bool, limit: int | None) -> list[dict]:
    rows = [r for r in rows if all(LocalRepository._matches(r, f) for f in filters)]
    if order_by:
        rows = [r for r in rows if r.get(order_by) is not None]
        rows.sort(key=lambda r: r[order_by], reverse=descending)
    return rows[:limit] if limit is not None else rows


def find(collection: str, filters: Iterable[tuple[str, str, Any]] = (), *,
         order_by: str | None = None, descending: bool = False,
         limit: int | None = None, prefer: str = "equality") -> list[dict]:
    """`repository.query`, with a fallback for a missing composite index.

    The fallback sends Firestore only the filters its automatic single-field
    indexes can serve - the equality filters, or with prefer="range" the
    date range when that is the narrower of the two - and applies the rest
    here. The answer is the same either way.
    """
    filters = list(filters)
    signature = index_needed(collection, filters, order_by, descending) or (
        collection, tuple(f for f, _, _ in filters), order_by, descending)
    if not _known_missing(signature):
        try:
            return repo().query(collection, filters, limit=limit,
                                order_by=order_by, descending=descending)
        except MissingIndex as error:
            _note_missing(signature, error)
    equal = [f for f in filters if f[1] in EQUALITY_OPS]
    rest = [f for f in filters if f[1] not in EQUALITY_OPS]
    ranged = {f for f, _, _ in rest}
    if prefer == "range" and len(ranged) == 1 and order_by in (None, *ranged):
        server, local = rest, equal
    else:
        server, local = equal, rest
    return _finish_here(repo().query(collection, server), local, order_by,
                        descending, limit)


def count(collection: str, filters: Iterable[tuple[str, str, Any]] = (), *,
          prefer: str = "equality") -> int:
    """A count() aggregation - nothing is downloaded - with the same
    missing-index fallback as `find`."""
    filters = list(filters)
    signature = index_needed(collection, filters) or (
        collection, tuple(f for f, _, _ in filters), "count")
    if not _known_missing(signature):
        try:
            return repo().count(collection, filters)
        except MissingIndex as error:
            _note_missing(signature, error)
    return len(find(collection, filters, prefer=prefer))


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------
def appointment_filters(doctor_id: str | None = None, date: str | None = None,
                        start: str | None = None, end: str | None = None,
                        before: str | None = None, after: str | None = None) -> Filters:
    """Firestore filters for the fields every appointment carries. Only
    `doctor_id` and `date` go to the database; status and text filters are
    applied afterwards, so one composite index (doctor_id + date) covers
    every query the dashboard makes."""
    filters: Filters = []
    if doctor_id:
        filters.append(("doctor_id", "==", doctor_id))
    if date:
        filters.append(("date", "==", date))
    else:
        if start:
            filters.append(("date", ">=", start))
        if after:
            filters.append(("date", ">", after))
        if end:
            filters.append(("date", "<=", end))
        if before:
            filters.append(("date", "<", before))
    return filters


def appointments(**window) -> list[dict]:
    return find(Collections.APPOINTMENTS, appointment_filters(**window))


def sort_key(row: dict) -> tuple:
    return (str(row.get("date", "")), str(row.get("time", "")),
            str(row.get("appointment_id", "")))


def in_order(rows: list[dict], descending: bool = False) -> list[dict]:
    """Sort by (date, time), newest first when `descending`; appointments at
    the same moment stay in id order either way, as they always have."""
    rows.sort(key=lambda r: str(r.get("appointment_id", "")))
    rows.sort(key=lambda r: (str(r.get("date", "")), str(r.get("time", ""))),
              reverse=descending)
    return rows


def page_by_date(filters: Filters, *, offset: int = 0, limit: int = 25,
                 descending: bool = False,
                 where: Callable[[dict], bool] | None = None,
                 scan: bool = False,
                 with_total: bool = True) -> tuple[list[dict], bool, int | None]:
    """
    One page of appointments in (date, time) order: (rows, has_more, total).

    Reads in date order and stops once the page is full, instead of
    downloading the whole window. The last date read may be cut part-way by
    the limit, so that one date is completed with an equality query before
    sorting by time - otherwise two pages could disagree about who comes
    first on that day. A `where` filter that throws rows away makes it read
    further (doubling) until the page is full or the window is exhausted.

    `scan=True` reads the whole window at once; a text search has to look
    at every row anyway. `total` is known when nothing had to be filtered
    here (a count() aggregation, run alongside the first read) or when the
    whole window was read.
    """
    offset = max(0, int(offset))
    limit = max(1, int(limit))
    want = offset + limit + 1          # one more than shown: is there a next page?

    def read(size: int) -> list[dict]:
        return find(Collections.APPOINTMENTS, filters, order_by="date",
                    descending=descending, limit=size)

    total = None
    if scan:
        rows, exhausted = find(Collections.APPOINTMENTS, filters), True
        matched = [r for r in rows if where is None or where(r)]
    else:
        size = want
        if where is None and with_total:
            rows, total = parallel(lambda: read(size),
                                   lambda: count(Collections.APPOINTMENTS, filters))
        else:
            rows = read(size)
        while True:
            exhausted = len(rows) < size
            if not exhausted and rows:
                edge = rows[-1].get("date")
                same_day = [f for f in filters if f[0] != "date"] + [("date", "==", edge)]
                rows = [r for r in rows if r.get("date") != edge] + find(
                    Collections.APPOINTMENTS, same_day)
            matched = [r for r in rows if where is None or where(r)]
            if exhausted or len(matched) >= want:
                break
            size *= 2
            rows = read(size)

    in_order(matched, descending)
    page = matched[offset:offset + limit]
    has_more = len(matched) > offset + limit
    if exhausted:
        total = len(matched)
    return page, has_more, total


def count_by_status(filters: Filters = (), statuses: Iterable[str] | None = None) -> dict:
    """{"total": n, "<status>": n, ...} from count() aggregations run
    together - nothing is downloaded, whatever the size of the diary.
    `statuses` limits which statuses are counted (default: all of them)."""
    return counts_by_status_for({"all": list(filters)}, statuses)["all"]


def counts_by_status_for(groups: dict[Any, Filters],
                         statuses: Iterable[str] | None = None) -> dict[Any, dict]:
    """`count_by_status` for several groups at once (one per doctor, say):
    every aggregation for every group goes out in the same wave."""
    from appointment_backend.statistics import ALL_STATUSES

    wanted = tuple(ALL_STATUSES if statuses is None else statuses)
    keys, calls = [], []
    for key, filters in groups.items():
        base = list(filters)
        for status in (None,) + wanted:
            where = base if status is None else base + [("status", "==", status)]
            keys.append((key, status))
            calls.append(lambda w=where: count(Collections.APPOINTMENTS, w))
    out: dict[Any, dict] = {key: {} for key in groups}
    for (key, status), value in zip(keys, parallel(*calls)):
        out[key]["total" if status is None else status] = value
    return out


# --------------------------------------------------------------------------
# Patients - by id, never the whole collection
# --------------------------------------------------------------------------
_BATCH = 300


def patients_by_id(ids: Iterable[str]) -> dict[str, dict]:
    wanted = [str(i) for i in dict.fromkeys(ids) if i]
    if not wanted:
        return {}
    chunks = [wanted[i:i + _BATCH] for i in range(0, len(wanted), _BATCH)]
    found: dict[str, dict] = {}
    for part in parallel(*[(lambda c=chunk: repo().get_many(Collections.PATIENTS, c))
                           for chunk in chunks]):
        for doc_id, record in part.items():
            found[record.get("patient_id") or doc_id] = record
    return found


def with_patients(rows: list[dict], patients: dict | None = None) -> list[dict]:
    """Attach the patient's name and phone: the dashboard shows people."""
    if patients is None:
        patients = patients_by_id(r.get("patient_id") for r in rows)
    out = []
    for row in rows:
        patient = patients.get(row.get("patient_id")) or {}
        out.append({**row,
                    "patient_name": patient.get("name") or "Unknown patient",
                    "patient_phone": patient.get("phone") or ""})
    return out


# --------------------------------------------------------------------------
# The register and the clinic - cached briefly, dropped on every write
# --------------------------------------------------------------------------
def doctors() -> list[dict]:
    return cache.cached(Collections.DOCTORS, "all",
                        lambda: repo().query(Collections.DOCTORS))


def doctor_names() -> dict[str, str]:
    return {d.get("doctor_id"): d.get("name") for d in doctors()}


def clinic() -> dict:
    """The one clinic record (created empty on first use by ClinicService)."""
    def load():
        result = ClinicService(repo()).primary()
        if not result.ok:
            raise DatabaseError(result.message or "The clinic record could not be read.")
        return result.data
    return cache.cached(Collections.CLINICS, "primary", load)


def clinics() -> list[dict]:
    return cache.cached(Collections.CLINICS, "all",
                        lambda: repo().query(Collections.CLINICS))
