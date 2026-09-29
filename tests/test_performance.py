"""
Performance tests: the dashboard reads only what it shows, and reading less
never changes an answer.

Two kinds of check, against an in-memory database big enough to notice
(three doctors with 240 appointments each spread over seven months, 400
patients, 60 patient messages):

  * READ BUDGETS - every repository call a request makes is recorded, so a
    test can say "the summary never reads the patients collection" or "one
    page of appointments reads a page, not the doctor's history". These are
    what stop the old full-collection reads from coming back.
  * SAME ANSWERS - every optimised endpoint is compared with a reference
    worked out the old way, from every record: the statistics with
    statistics.statistics() over all appointments, each list with the
    filtering and sorting the endpoint used to do, every page of a
    paginated list against the unpaginated whole. And the same requests are
    answered identically when Firestore has none of the composite indexes
    deployed (the fallback path).

The in-memory repository enforces Firestore's index rule against
firestore.indexes.json, so a query here that needs an undeclared index fails
exactly as it would in production.
"""
from __future__ import annotations

import os
import random
import unittest
from datetime import datetime, timedelta
from unittest import mock

from config import Collections, Status, TIMEZONE
from firebase.firebase_config import (DatabaseError, LocalRepository, MissingIndex,
                                      declared_indexes, index_needed)

DOCTOR_PASSWORD = "test-dashboard-password"
ADMIN_PASSWORD = "test-admin-password"
FIXED_NOW = "12:00"

LIVE = (Status.PENDING, Status.CONFIRMED, Status.RESCHEDULED)
CANCELLED = (Status.CANCELLED, Status.CANCELLED_BY_DOCTOR)
STATUSES = LIVE + (Status.COMPLETED,) + CANCELLED
TIMES = ["09:00", "09:20", "09:40", "10:00", "10:20", "11:00", "14:00", "16:40"]
DOCTORS = ("D001", "D002", "D003")


def today() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def iso(offset: int) -> str:
    return (datetime.now(TIMEZONE).date() + timedelta(days=offset)).isoformat()


class RecordingRepository(LocalRepository):
    """The in-memory store, noting every call a request makes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[dict] = []
        self.recording = False

    def _log(self, op, collection, **info):
        if self.recording:
            self.calls.append({"op": op, "collection": collection, **info})

    def get(self, collection, doc_id):
        self._log("get", collection, n=1, filters=[])
        return super().get(collection, doc_id)

    def get_many(self, collection, doc_ids):
        ids = list(doc_ids)
        self._log("get_many", collection, n=len(set(ids)), filters=[])
        return super().get_many(collection, ids)

    def query(self, collection, filters=(), limit=None, order_by=None, descending=False):
        filters = list(filters)
        rows = super().query(collection, filters, limit=limit, order_by=order_by,
                             descending=descending)
        self._log("query", collection, filters=filters, limit=limit,
                  order_by=order_by, n=max(1, len(rows)))
        return rows

    def count(self, collection, filters=()):
        filters = list(filters)
        value = super().count(collection, filters)
        self._log("count", collection, filters=filters, n=1)
        return value


def reads(calls) -> int:
    return sum(call["n"] for call in calls)


def unbounded(calls, *collections) -> list[dict]:
    """Queries that would download a whole collection: no filter, no limit."""
    return [c for c in calls if c["op"] == "query" and c["collection"] in collections
            and not c["filters"] and c.get("limit") is None]


def populate(repo, per_doctor=240, patients=400, notes=60, seed=11) -> None:
    rng = random.Random(seed)
    for i in range(patients):
        pid = f"PX{i:04d}"
        repo.set(Collections.PATIENTS, pid,
                 {"patient_id": pid, "name": f"Patient {i:04d}",
                  "phone": f"+9230{i:08d}"})
    n = 0
    for doctor_id in DOCTORS:
        for _ in range(per_doctor):
            aid = f"APTX{n:05d}"
            n += 1
            repo.set(Collections.APPOINTMENTS, aid, {
                "appointment_id": aid, "doctor_id": doctor_id,
                "patient_id": f"PX{rng.randrange(patients):04d}",
                "clinic_id": "C001", "date": iso(rng.randint(-150, 60)),
                "time": rng.choice(TIMES), "status": rng.choice(STATUSES)})
    for j in range(notes):
        nid = f"NX{j:04d}"
        repo.set(Collections.NOTIFICATIONS, nid, {
            "notification_id": nid, "doctor_id": rng.choice(DOCTORS),
            "type": "appointment_cancelled_by_doctor", "date": iso(rng.randint(0, 30)),
            "message": "test message", "status": "pending",
            "created_at": f"2026-09-{1 + j % 28:02d}T{j % 24:02d}:{j % 60:02d}:00+00:00",
            **({"read_by_doctor": True} if rng.random() < 0.5 else {})})


# --------------------------------------------------------------------------
# References: the answers worked out the old way, from every record
# --------------------------------------------------------------------------
def all_rows(repo, collection):
    return [dict(r) for r in repo._collection(collection).values()]


def with_names(rows, repo):
    patients = {p["patient_id"]: p for p in all_rows(repo, Collections.PATIENTS)}
    out = []
    for row in rows:
        patient = patients.get(row.get("patient_id")) or {}
        out.append({**row, "patient_name": patient.get("name") or "Unknown patient",
                    "patient_phone": patient.get("phone") or ""})
    return out


def ordered(rows, descending=False):
    rows = sorted(rows, key=lambda r: r["appointment_id"])
    return sorted(rows, key=lambda r: (r.get("date", ""), r.get("time", "")),
                  reverse=descending)


def old_doctor_list(repo, doctor_id, scope="all", date=None, status=None, query=None):
    day = today()
    rows = [r for r in all_rows(repo, Collections.APPOINTMENTS)
            if r.get("doctor_id") == doctor_id and (not date or r.get("date") == date)]
    rows = with_names(rows, repo)
    if scope == "today":
        rows = [a for a in rows if a.get("date") == day]
    elif scope == "upcoming":
        rows = [a for a in rows if a.get("date", "") >= day and a.get("status") in LIVE]
    elif scope == "completed":
        rows = [a for a in rows if a.get("status") == Status.COMPLETED]
    elif scope == "cancelled":
        rows = [a for a in rows if a.get("status") in CANCELLED]
    elif scope == "past":
        rows = [a for a in rows if a.get("date", "") < day]
    if status:
        rows = [a for a in rows if a.get("status") == status]
    if query:
        needle = query.strip().lower()
        rows = [a for a in rows if needle in str(a.get("patient_name", "")).lower()
                or needle in str(a.get("appointment_id", "")).lower()
                or needle in str(a.get("date", ""))
                or needle in str(a.get("patient_phone", ""))]
    return ordered(rows)


def old_admin_list(repo, scope="all", doctor_id=None, date=None, status=None, q=None):
    day, now = today(), FIXED_NOW
    names = {d["doctor_id"]: d.get("name") for d in all_rows(repo, Collections.DOCTORS)}
    rows = with_names(all_rows(repo, Collections.APPOINTMENTS), repo)
    for row in rows:
        row["doctor_name"] = names.get(row.get("doctor_id")) or row.get("doctor_id")
    if doctor_id:
        rows = [a for a in rows if a.get("doctor_id") == doctor_id.upper()]
    if date:
        rows = [a for a in rows if a.get("date") == date]
    if status:
        rows = [a for a in rows if a.get("status") == status]
    if scope == "today":
        rows = [a for a in rows if a.get("date") == day]
    elif scope == "upcoming":
        rows = [a for a in rows if a.get("status") in LIVE
                and (str(a.get("date", "")) > day
                     or (a.get("date") == day and str(a.get("time", "")) >= now))]
    elif scope == "past":
        rows = [a for a in rows if str(a.get("date", "")) < day]
    elif scope == "cancelled":
        rows = [a for a in rows if a.get("status") in CANCELLED]
    if q:
        needle = q.strip().lower()
        rows = [a for a in rows if needle in " ".join(str(v or "").lower() for v in [
            a.get("patient_name"), a.get("patient_phone"), a.get("appointment_id"),
            a.get("doctor_name"), a.get("doctor_id"), a.get("date")])]
    return ordered(rows, descending=True)


def ids(rows):
    return [r["appointment_id"] for r in rows]


# --------------------------------------------------------------------------
# The repository and the query helpers on their own
# --------------------------------------------------------------------------
class RepositoryTests(unittest.TestCase):

    def setUp(self):
        self.repo = LocalRepository()
        for i, (doctor, day, status) in enumerate([
                ("D1", "2026-01-02", "confirmed"), ("D1", "2026-01-01", "completed"),
                ("D2", "2026-01-03", "confirmed"), ("D1", "2026-01-05", "cancelled")]):
            self.repo.set("appointments", f"A{i}", {"appointment_id": f"A{i}",
                                                    "doctor_id": doctor, "date": day,
                                                    "status": status})

    def test_get_many_returns_only_what_exists(self):
        found = self.repo.get_many("appointments", ["A0", "A2", "A2", "NOPE", ""])
        self.assertEqual(sorted(found), ["A0", "A2"])

    def test_count_matches_the_query(self):
        self.assertEqual(self.repo.count("appointments", [("doctor_id", "==", "D1")]), 3)
        self.assertEqual(self.repo.count("appointments"), 4)

    def test_order_and_limit(self):
        rows = self.repo.query("appointments", [], order_by="date", descending=True, limit=2)
        self.assertEqual([r["date"] for r in rows], ["2026-01-05", "2026-01-03"])

    def test_undeclared_composite_index_is_refused_like_firestore(self):
        bare = LocalRepository(indexes=set())
        bare.set("appointments", "A", {"doctor_id": "D1", "date": "2026-01-01"})
        with self.assertRaises(MissingIndex):
            bare.query("appointments", [("doctor_id", "==", "D1"), ("date", ">=", "2026")])
        with self.assertRaises(MissingIndex):
            bare.query("appointments", [("doctor_id", "==", "D1")], order_by="date")
        # equality on any fields, or a range on one field alone, never needs one
        bare.query("appointments", [("doctor_id", "==", "D1"), ("date", "==", "x")])
        bare.query("appointments", [("date", ">=", "2026")], order_by="date",
                   descending=True)

    def test_every_index_the_code_needs_is_declared(self):
        declared = declared_indexes()
        shapes = [
            ("appointments", [("doctor_id", "==", "D"), ("date", ">=", "x")], None, False),
            ("appointments", [("doctor_id", "==", "D"), ("date", "<", "x")], "date", True),
            ("appointments", [("doctor_id", "==", "D")], "date", False),
            ("appointments", [("status", "==", "confirmed"), ("date", ">", "x")], None, False),
            ("notifications", [("doctor_id", "==", "D")], "created_at", True),
        ]
        for collection, filters, order_by, descending in shapes:
            needed = index_needed(collection, filters, order_by, descending)
            self.assertIn(needed, declared, f"{needed} is not in firestore.indexes.json")


class QueryHelperTests(unittest.TestCase):
    """find / page_by_date / count_by_status against a plain reference."""

    @classmethod
    def setUpClass(cls):
        from firebase.firebase_config import reset_repository
        cls.repo = LocalRepository()
        reset_repository(cls.repo)
        populate(cls.repo, per_doctor=90, patients=60, notes=5, seed=3)
        from api import data
        cls.data = data

    def everything(self):
        return all_rows(self.repo, Collections.APPOINTMENTS)

    def check_pages(self, filters, where=None, descending=False):
        data = self.data
        reference = [r for r in self.everything()
                     if all(LocalRepository._matches(r, f) for f in filters)
                     and (where is None or where(r))]
        reference = ordered(reference, descending)
        for limit in (1, 4, 13, 400):
            walked, offset = [], 0
            while True:
                page, more, total = data.page_by_date(filters, offset=offset, limit=limit,
                                                      descending=descending, where=where)
                walked += page
                if total is not None:
                    self.assertEqual(total, len(reference))
                if not more:
                    break
                offset += limit
            self.assertEqual(ids(walked), ids(reference), (filters, limit, descending))

    def test_pages_match_the_whole_list(self):
        day = today()
        live = lambda a: a.get("status") in LIVE  # noqa: E731
        for descending in (False, True):
            self.check_pages([("doctor_id", "==", "D001")], descending=descending)
            self.check_pages([("doctor_id", "==", "D002"), ("date", ">=", day)],
                             where=live, descending=descending)
            self.check_pages([("date", "<", day)], descending=descending)
            self.check_pages([], where=lambda a: a.get("status") in CANCELLED,
                             descending=descending)

    def test_same_answers_without_any_index(self):
        data = self.data
        filters = [("doctor_id", "==", "D003"), ("date", ">=", iso(-20))]
        indexed = data.find(Collections.APPOINTMENTS, filters, order_by="date", limit=15)
        self.repo.indexes = set()
        data.forget_missing_indexes()
        try:
            fallback = data.find(Collections.APPOINTMENTS, filters, order_by="date", limit=15)
            self.check_pages([("doctor_id", "==", "D003")], descending=True)
            self.assertEqual(data.count(Collections.APPOINTMENTS, filters),
                             len(data.find(Collections.APPOINTMENTS, filters)))
        finally:
            self.repo.indexes = declared_indexes()
            data.forget_missing_indexes()
        self.assertEqual([r["date"] for r in fallback], [r["date"] for r in indexed])

    def test_start_up_learns_exactly_the_missing_indexes(self):
        data = self.data
        self.repo.indexes = set()
        data.forget_missing_indexes()
        try:
            data.learn_indexes()
            self.assertEqual(set(data._missing), declared_indexes())
        finally:
            self.repo.indexes = declared_indexes()
            data.forget_missing_indexes()
        data.learn_indexes()
        self.assertEqual(data._missing, {})

    def test_status_counts_add_up(self):
        from appointment_backend import statistics as stats
        mine = [r for r in self.everything() if r["doctor_id"] == "D001"]
        counts = self.data.count_by_status([("doctor_id", "==", "D001")])
        self.assertEqual(stats.summarise_counts(counts["total"], counts),
                         stats.summarise(mine))

    def test_patients_are_fetched_in_batches_of_ids(self):
        wanted = [f"PX{i:04d}" for i in range(60)] * 2 + ["NOBODY"]
        found = self.data.patients_by_id(wanted)
        self.assertEqual(len(found), 60)


class StatisticsEquivalenceTests(unittest.TestCase):

    def test_parts_equal_the_reference_for_every_period(self):
        from appointment_backend import statistics as stats
        rng = random.Random(5)
        records = [{"appointment_id": str(i), "doctor_id": rng.choice(DOCTORS),
                    "date": iso(rng.randint(-60, 30)), "status": rng.choice(
                        STATUSES + ("mystery",))} for i in range(500)]
        records.append({"appointment_id": "nodate", "status": "confirmed"})
        by_status = {s: sum(1 for r in records if r.get("status") == s)
                     for s in stats.ALL_STATUSES}
        all_time = stats.summarise_counts(len(records), by_status)
        self.assertEqual(all_time, stats.summarise(records))
        day = today()
        for period, start, end in [("today", None, None), ("week", None, None),
                                   ("month", None, None), ("all", None, None),
                                   ("custom", iso(-40), iso(-10))]:
            window = stats.resolve_period(period, start, end)
            bounded = bool(window["start"] or window["end"])
            rows = stats.in_period(records, window["start"], window["end"]) if bounded else None
            todays = stats.in_period(records, day, day)
            self.assertEqual(
                stats.statistics_from_parts(window, rows, todays, all_time, day),
                stats.statistics(records, period=period, start=start, end=end), period)


# --------------------------------------------------------------------------
# The API, with the repository recording every call
# --------------------------------------------------------------------------
class ApiBudgetTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DASHBOARD_PASSWORD"] = DOCTOR_PASSWORD
        os.environ["SUPERADMIN_PASSWORD"] = ADMIN_PASSWORD
        os.environ.pop("SUPERADMIN_ID", None)

        from fastapi.testclient import TestClient
        from api import auth, data, main
        from firebase import cache
        from firebase.firebase_config import reset_repository
        from firebase.seed_data import seed

        cls.auth, cls.data, cls.cache = auth, data, cache
        cls.repo = RecordingRepository()
        reset_repository(cls.repo)
        seed(cls.repo, with_sample_appointment=True)
        cls.client = TestClient(main.app)
        cls.client.__enter__()
        from appointment_backend.api import set_backend
        from appointment_backend.appointment_service import AppointmentBackend
        set_backend(AppointmentBackend(cls.repo))
        populate(cls.repo)
        cls.patches = [mock.patch("api.admin.now_hhmm", return_value=FIXED_NOW),
                       mock.patch("api.dashboard.now_hhmm", return_value=FIXED_NOW)]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in cls.patches:
            patch.stop()
        cls.client.__exit__(None, None, None)
        cls.auth.clear_all_sessions()
        os.environ.pop("DASHBOARD_PASSWORD", None)
        os.environ.pop("SUPERADMIN_PASSWORD", None)

    def setUp(self):
        self.doctor = self.sign_in("D001", DOCTOR_PASSWORD)
        self.admin = self.sign_in("ADMIN", ADMIN_PASSWORD)

    # ------------------------------------------------------------ helpers
    def sign_in(self, user_id, password):
        response = self.client.post("/auth/login",
                                    json={"user_id": user_id, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]["token"]

    def call(self, method, path, token, **kwargs):
        """(response, repository calls made while answering it)"""
        self.repo.calls = []
        self.repo.recording = True
        try:
            response = self.client.request(method, path, headers={
                "Authorization": "Bearer " + token}, **kwargs)
        finally:
            self.repo.recording = False
        return response, list(self.repo.calls)

    def get(self, path, token):
        response, calls = self.call("GET", path, token)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"], calls

    def walk(self, path, token, limit=7, key="appointments"):
        rows, offset = [], 0
        while True:
            joiner = "&" if "?" in path else "?"
            page, _ = self.get(f"{path}{joiner}offset={offset}&limit={limit}", token)
            rows += page[key]
            if not page["has_more"]:
                return rows, page
            offset += limit

    def appointment_count(self, doctor_id=None):
        return len([r for r in all_rows(self.repo, Collections.APPOINTMENTS)
                    if doctor_id is None or r["doctor_id"] == doctor_id])

    # ------------------------------------------------------------ sign in
    def test_sign_in_reads_the_account_once_in_one_wave(self):
        response, calls = self.call("POST", "/auth/login", "",
                                    json={"user_id": "D001", "password": DOCTOR_PASSWORD})
        self.assertEqual(response.status_code, 200)
        account_reads = [c for c in calls if c["collection"] == Collections.ACCOUNTS
                         and c["op"] == "get"]
        self.assertEqual(len(account_reads), 1, calls)
        self.assertLessEqual(len(calls), 3, calls)

    # ------------------------------------------------------------ summary
    def test_summary_reads_today_onwards_and_only_its_patients(self):
        summary, calls = self.get("/dashboard/summary", self.doctor)
        self.assertEqual(unbounded(calls, Collections.PATIENTS, Collections.APPOINTMENTS,
                                   Collections.NOTIFICATIONS), [])
        self.assertFalse([c for c in calls if c["op"] == "query"
                          and c["collection"] == Collections.PATIENTS])
        for query in [c for c in calls if c["op"] == "query"
                      and c["collection"] == Collections.APPOINTMENTS]:
            self.assertIn(("doctor_id", "==", "D001"), query["filters"])
            self.assertIn(("date", ">=", today()), query["filters"])
        self.assertLess(reads(calls), self.appointment_count("D001"))

        mine = [r for r in all_rows(self.repo, Collections.APPOINTMENTS)
                if r["doctor_id"] == "D001"]
        todays = [r for r in mine if r["date"] == today()]
        later = [r for r in mine if r["date"] > today() and r["status"] in LIVE]
        self.assertEqual(summary["stats"]["total"], len(todays))
        self.assertEqual(summary["stats"]["upcoming_total"], len(later))
        self.assertEqual(ids(summary["upcoming"]), ids(ordered(later))[:8])
        notes = [n for n in all_rows(self.repo, Collections.NOTIFICATIONS)
                 if n.get("doctor_id") == "D001"]
        self.assertEqual(summary["unread_notifications"],
                         len([n for n in notes if not n.get("read_by_doctor")]))

    # --------------------------------------------------------- statistics
    def test_doctor_statistics_match_the_reference_without_the_history(self):
        from appointment_backend import statistics as stats
        mine = [r for r in all_rows(self.repo, Collections.APPOINTMENTS)
                if r["doctor_id"] == "D001"]
        for period in ("today", "week", "month", "all"):
            payload, calls = self.get(f"/dashboard/statistics?period={period}", self.doctor)
            self.assertEqual(payload, stats.statistics(mine, period=period), period)
            # Every appointment query is bounded by date; the all-time
            # figures are counted, never downloaded.
            for query in [c for c in calls if c["op"] == "query"
                          and c["collection"] == Collections.APPOINTMENTS]:
                self.assertTrue(any(f[0] == "date" for f in query["filters"]), query)
            self.assertLess(reads(calls), len(mine))

    def test_admin_statistics_match_the_reference(self):
        from appointment_backend import statistics as stats
        everything = all_rows(self.repo, Collections.APPOINTMENTS)
        doctors = [d for d in all_rows(self.repo, Collections.DOCTORS)
                   if not d.get("archived")]
        for period in ("month", "all"):
            payload, calls = self.get(f"/admin/statistics?period={period}", self.admin)
            expected = stats.statistics(everything, doctors=None, period=period)
            window = expected["period"]
            expected["doctors"] = stats.per_doctor(
                stats.in_period(everything, window["start"], window["end"]), doctors)
            for key in ("period", "totals", "today", "all_time", "doctors"):
                self.assertEqual(payload[key], expected[key], (period, key))
            self.assertEqual(unbounded(calls, Collections.APPOINTMENTS), [])

        payload, _ = self.get("/admin/statistics?period=all&doctor_id=D002", self.admin)
        mine = [r for r in everything if r["doctor_id"] == "D002"]
        self.assertEqual(payload["totals"], stats.summarise(mine))
        self.assertEqual(payload["doctor"]["doctor_id"], "D002")

    # -------------------------------------------------------- appointments
    def test_one_page_reads_a_page_not_the_history(self):
        page, calls = self.get("/dashboard/appointments?limit=20", self.doctor)
        self.assertEqual(ids(page["appointments"]),
                         ids(old_doctor_list(self.repo, "D001"))[:20])
        self.assertEqual(page["total"], self.appointment_count("D001"))
        self.assertLess(reads(calls), self.appointment_count("D001") / 3)

    def test_every_doctor_page_matches_the_old_list(self):
        day = today()
        for scope in ("all", "today", "upcoming", "completed", "cancelled", "past"):
            rows, last = self.walk(f"/dashboard/appointments?scope={scope}", self.doctor)
            expected = old_doctor_list(self.repo, "D001", scope=scope)
            self.assertEqual(ids(rows), ids(expected), scope)
            self.assertIn(last["total"], (None, len(expected)))
        rows, _ = self.walk(f"/dashboard/appointments?scope=past&date={iso(-3)}", self.doctor)
        self.assertEqual(ids(rows), ids(old_doctor_list(self.repo, "D001", "past", iso(-3))))
        rows, _ = self.walk("/dashboard/appointments?query=patient 00", self.doctor)
        self.assertEqual(ids(rows), ids(old_doctor_list(self.repo, "D001", query="patient 00")))
        rows, _ = self.walk(f"/dashboard/appointments?start={day}&end={iso(14)}",
                            self.doctor)
        self.assertEqual(ids(rows), ids([r for r in old_doctor_list(self.repo, "D001")
                                          if day <= r["date"] <= iso(14)]))

    def test_every_admin_page_matches_the_old_list(self):
        for scope in ("upcoming", "today", "past", "cancelled", "all"):
            for doctor in (None, "D002"):
                path = f"/admin/appointments?scope={scope}"
                if doctor:
                    path += f"&doctor_id={doctor}"
                rows, _ = self.walk(path, self.admin, limit=11)
                expected = old_admin_list(self.repo, scope=scope, doctor_id=doctor)
                self.assertEqual(ids(rows), ids(expected), (scope, doctor))
                self.assertEqual([r["doctor_name"] for r in rows],
                                 [r["doctor_name"] for r in expected])
        rows, _ = self.walk("/admin/appointments?scope=all&q=asim", self.admin, limit=50)
        self.assertEqual(ids(rows), ids(old_admin_list(self.repo, "all", q="asim")))

    def test_admin_upcoming_page_does_not_read_the_past(self):
        _, calls = self.get("/admin/appointments?scope=upcoming&limit=25", self.admin)
        self.assertEqual(unbounded(calls, Collections.APPOINTMENTS,
                                   Collections.PATIENTS), [])
        self.assertLess(reads(calls), self.appointment_count() / 4)

    # ----------------------------------------------------- admin overview
    def test_admin_summary_counts_without_whole_collections(self):
        summary, calls = self.get("/admin/summary", self.admin)
        self.assertEqual(unbounded(calls, Collections.APPOINTMENTS, Collections.PATIENTS,
                                   Collections.NOTIFICATIONS,
                                   Collections.UNAVAILABILITY), [])
        everything = all_rows(self.repo, Collections.APPOINTMENTS)
        day = today()
        counts = summary["appointments"]
        self.assertEqual(counts["total"], len(everything))
        self.assertEqual(counts["today"], len([a for a in everything if a["date"] == day]))
        self.assertEqual(counts["upcoming"], len([
            a for a in everything if a["status"] in LIVE
            and (a["date"] > day or (a["date"] == day and a["time"] >= FIXED_NOW))]))
        self.assertEqual(counts["completed"],
                         len([a for a in everything if a["status"] == Status.COMPLETED]))
        self.assertEqual(counts["cancelled"],
                         len([a for a in everything if a["status"] in CANCELLED]))
        self.assertLess(reads(calls), len(everything) / 4)

    def test_doctor_register_counts_match(self):
        listing, calls = self.get("/admin/doctors", self.admin)
        self.assertEqual(unbounded(calls, Collections.APPOINTMENTS,
                                   Collections.SCHEDULES), [])
        everything = all_rows(self.repo, Collections.APPOINTMENTS)
        day = today()
        for row in listing["doctors"]:
            mine = [a for a in everything if a["doctor_id"] == row["doctor_id"]]
            live = [a for a in mine if a["status"] in LIVE]
            self.assertEqual(row["appointments"], {
                "total": len(mine),
                "today": len([a for a in live if a["date"] == day]),
                "upcoming": len([a for a in live if a["date"] >= day])}, row["doctor_id"])
        walked, _ = self.walk("/admin/doctors", self.admin, limit=2, key="doctors")
        self.assertEqual([d["doctor_id"] for d in walked],
                         [d["doctor_id"] for d in listing["doctors"]])

    def test_doctor_detail_reads_only_that_doctor(self):
        detail, calls = self.get("/admin/doctors/D002", self.admin)
        for query in [c for c in calls if c["op"] in ("query", "count")
                      and c["collection"] == Collections.APPOINTMENTS]:
            self.assertIn(("doctor_id", "==", "D002"), query["filters"])
        mine = [a for a in all_rows(self.repo, Collections.APPOINTMENTS)
                if a["doctor_id"] == "D002"]
        day = today()
        upcoming = ordered([a for a in mine if a["date"] >= day and a["status"] in LIVE])
        self.assertEqual(ids(detail["appointments"]["upcoming"]), ids(upcoming)[:20])
        past = ordered([a for a in mine if a["date"] < day], descending=True)
        self.assertEqual({a["date"] for a in detail["appointments"]["recent"]},
                         {a["date"] for a in past[:10]})
        self.assertEqual(detail["appointments"]["counts"]["total"], len(mine))

    # ----------------------------------------------- patients and messages
    def test_patient_and_message_pages(self):
        whole, calls = self.get("/dashboard/patients?limit=500", self.doctor)
        self.assertFalse([c for c in calls if c["op"] == "query"
                          and c["collection"] == Collections.PATIENTS])
        walked, _ = self.walk("/dashboard/patients", self.doctor, limit=9, key="patients")
        self.assertEqual([p["patient_id"] for p in walked],
                         [p["patient_id"] for p in whole["patients"]])
        notes = sorted([n for n in all_rows(self.repo, Collections.NOTIFICATIONS)
                        if n.get("doctor_id") == "D001"],
                       key=lambda n: n["created_at"], reverse=True)
        walked, last = self.walk("/dashboard/notifications", self.doctor, limit=4,
                                 key="notifications")
        self.assertEqual([n["notification_id"] for n in walked],
                         [n["notification_id"] for n in notes])
        self.assertEqual(last["total"], len(notes))

    # -------------------------------------------------- without the indexes
    def test_same_answers_when_no_index_is_deployed(self):
        paths = [("/dashboard/summary", self.doctor),
                 ("/dashboard/statistics?period=month", self.doctor),
                 ("/dashboard/appointments?scope=upcoming&limit=15", self.doctor),
                 ("/dashboard/notifications?limit=5", self.doctor),
                 ("/admin/summary", self.admin),
                 ("/admin/appointments?doctor_id=D003&scope=past&limit=12", self.admin),
                 ("/admin/doctors/D001", self.admin)]

        def answers():
            out = []
            for path, token in paths:
                payload, _ = self.get(path, token)
                if isinstance(payload, dict):
                    payload.pop("generated_at", None)
                out.append(payload)
            return out

        indexed = answers()
        self.repo.indexes = set()
        self.data.forget_missing_indexes()
        try:
            fallback = answers()
        finally:
            self.repo.indexes = declared_indexes()
            self.data.forget_missing_indexes()
        for path, a, b in zip(paths, indexed, fallback):
            self.assertEqual(a, b, path[0])

    # ------------------------------------------------------- invalidation
    def test_a_clinic_change_is_visible_on_the_next_request(self):
        before, _ = self.get("/dashboard/profile", self.doctor)
        self.get("/dashboard/profile", self.doctor)          # now cached
        response, _ = self.call("PATCH", "/admin/clinic", self.admin,
                                json={"name": "Renamed Clinic For Test"})
        self.assertEqual(response.status_code, 200, response.text)
        try:
            after, _ = self.get("/dashboard/profile", self.doctor)
            self.assertEqual(after["clinic"]["name"], "Renamed Clinic For Test")
        finally:
            self.call("PATCH", "/admin/clinic", self.admin,
                      json={"name": before["clinic"]["name"] or "Clinic"})

    def test_a_doctor_rename_is_visible_on_the_next_request(self):
        self.get("/admin/appointments?doctor_id=D003&limit=1", self.admin)   # cached
        response, _ = self.call("PATCH", "/admin/doctors/D003", self.admin,
                                json={"name": "Dr Renamed Tester"})
        self.assertEqual(response.status_code, 200, response.text)
        try:
            page, _ = self.get("/admin/appointments?doctor_id=D003&limit=1", self.admin)
            self.assertEqual(page["appointments"][0]["doctor_name"], "Dr Renamed Tester")
        finally:
            self.call("PATCH", "/admin/doctors/D003", self.admin,
                      json={"name": "Dr Hamza Ali"})

    def test_a_change_made_elsewhere_expires(self):
        loads = []
        value = self.cache.cached(Collections.CLINICS, "ttl-test",
                                  lambda: loads.append(1) or "first", ttl=0.05)
        again = self.cache.cached(Collections.CLINICS, "ttl-test", lambda: "second", ttl=0.05)
        self.assertEqual((value, again), ("first", "first"))
        import time
        time.sleep(0.08)
        self.assertEqual(self.cache.cached(Collections.CLINICS, "ttl-test",
                                           lambda: "second", ttl=0.05), "second")

    # ----------------------------------------------------------- security
    def test_a_doctor_cannot_mark_someone_elses_message(self):
        other = next(n for n in all_rows(self.repo, Collections.NOTIFICATIONS)
                     if n.get("doctor_id") == "D002")
        response, _ = self.call("POST", f"/dashboard/notifications/{other['notification_id']}"
                                "/read", self.doctor)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.repo.get(Collections.NOTIFICATIONS,
                                       other["notification_id"]).get("read_by_doctor")
                         and not other.get("read_by_doctor"))

    def test_paging_never_leaks_another_doctors_rows(self):
        rows, _ = self.walk("/dashboard/appointments?doctor_id=D002", self.doctor, limit=50)
        self.assertEqual({r["doctor_id"] for r in rows}, {"D001"})
        response, _ = self.call("GET", "/admin/appointments", self.doctor)
        self.assertEqual(response.status_code, 403)

    def test_profile_refuses_clinic_fields_before_writing_anything(self):
        before = self.repo.get(Collections.DOCTORS, "D001")["name"]
        response, _ = self.call("PATCH", "/dashboard/profile", self.doctor,
                                json={"name": "Should Not Save", "clinic_name": "X"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.repo.get(Collections.DOCTORS, "D001")["name"], before)

    def test_a_database_failure_is_a_plain_503(self):
        from firebase.firebase_config import reset_repository

        class Broken(RecordingRepository):
            def query(self, *args, **kwargs):
                raise DatabaseError("grpc internal detail 10.0.0.1 should never be shown")

            def count(self, *args, **kwargs):
                raise DatabaseError("grpc internal detail 10.0.0.1 should never be shown")

        reset_repository(Broken())
        try:
            response, _ = self.call("GET", "/dashboard/summary", self.doctor)
        finally:
            reset_repository(self.repo)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("grpc", response.text)
        self.assertIn("unreachable", response.text)


if __name__ == "__main__":
    unittest.main()
