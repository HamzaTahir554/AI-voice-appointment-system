"""
Appointment statistics: the arithmetic, and the two endpoints that serve it.

The counting functions are pure, so they are tested directly against records
whose contents are known exactly. The endpoints are then tested through the
real FastAPI app to pin down the part that matters most: a doctor is counted
only over their OWN appointments, and the administrator over everybody's.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta

from config import Collections, Status, TIMEZONE

DOCTOR_PASSWORD = "test-dashboard-password"
ADMIN_PASSWORD = "test-admin-password"


def today() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def days_from_today(offset: int) -> str:
    return (datetime.now(TIMEZONE).date() + timedelta(days=offset)).isoformat()


class TestCounting(unittest.TestCase):
    """The pure functions, against records counted by hand."""

    def setUp(self):
        from appointment_backend import statistics

        self.statistics = statistics

    def records(self, **by_status) -> list[dict]:
        rows = []
        for status, count in by_status.items():
            for index in range(count):
                rows.append({"appointment_id": status + str(index),
                             "doctor_id": "D001", "date": today(),
                             "status": status})
        return rows

    def test_every_record_lands_in_exactly_one_bucket(self):
        rows = self.records(pending=1, confirmed=6, rescheduled=2,
                            completed=3, cancelled=4, cancelled_by_doctor=5)
        summary = self.statistics.summarise(rows)

        self.assertEqual(summary["total"], 21)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["confirmed"], 6)
        self.assertEqual(summary["rescheduled"], 2)
        self.assertEqual(summary["completed"], 3)
        self.assertEqual(summary["cancelled"], 9, "both kinds of cancellation")
        self.assertEqual(summary["cancelled_by_patient"], 4)
        self.assertEqual(summary["cancelled_by_doctor"], 5)
        self.assertEqual(summary["live"], 9, "pending + confirmed + rescheduled")

        self.assertEqual(
            summary["pending"] + summary["confirmed"] + summary["rescheduled"]
            + summary["completed"] + summary["cancelled"] + summary["other"],
            summary["total"],
            "the buckets must add up to the total exactly once")

    def test_an_unknown_status_is_counted_not_dropped(self):
        summary = self.statistics.summarise(
            [{"date": today(), "status": "something_new"}])
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["other"], 1)

    def test_nothing_at_all(self):
        summary = self.statistics.summarise([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["cancelled"], 0)

    def test_periods_cover_the_dates_people_mean(self):
        resolve = self.statistics.resolve_period
        friday = "2026-09-25"

        self.assertEqual(resolve("today", today=friday),
                         {"id": "today", "label": "Today",
                          "start": friday, "end": friday})
        week = resolve("week", today=friday)
        self.assertEqual((week["start"], week["end"]), ("2026-09-21", "2026-09-27"))
        month = resolve("month", today=friday)
        self.assertEqual((month["start"], month["end"]), ("2026-09-01", "2026-09-30"))
        self.assertEqual(resolve("all")["start"], None)

    def test_a_month_that_ends_on_the_31st(self):
        month = self.statistics.resolve_period("month", today="2026-12-09")
        self.assertEqual((month["start"], month["end"]), ("2026-12-01", "2026-12-31"))

    def test_february_in_a_leap_year(self):
        month = self.statistics.resolve_period("month", today="2028-02-10")
        self.assertEqual(month["end"], "2028-02-29")

    def test_a_bad_period_is_refused_with_something_readable(self):
        for arguments in [("custom", None, None), ("custom", "2026-09-10", "2026-09-01"),
                          ("nonsense", None, None), ("custom", "not-a-date", "2026-09-01")]:
            with self.assertRaises(ValueError, msg=str(arguments)):
                self.statistics.resolve_period(*arguments)

    def test_only_appointments_inside_the_period_are_counted(self):
        rows = [
            {"date": "2026-09-01", "status": Status.COMPLETED},
            {"date": "2026-09-15", "status": Status.COMPLETED},
            {"date": "2026-10-01", "status": Status.CONFIRMED},
            {"date": "", "status": Status.CONFIRMED},
        ]
        inside = self.statistics.in_period(rows, "2026-09-01", "2026-09-30")
        self.assertEqual(len(inside), 2)
        self.assertEqual(len(self.statistics.in_period(rows, None, None)), 4,
                         "no period means every record")

    def test_every_doctor_gets_a_row_even_with_nothing_booked(self):
        rows = [{"doctor_id": "D001", "date": today(), "status": Status.CONFIRMED},
                {"doctor_id": "D001", "date": today(), "status": Status.COMPLETED},
                {"doctor_id": "D002", "date": today(), "status": Status.PENDING}]
        doctors = [{"doctor_id": "D001", "name": "Dr One"},
                   {"doctor_id": "D002", "name": "Dr Two"},
                   {"doctor_id": "D003", "name": "Dr Three"}]

        per_doctor = self.statistics.per_doctor(rows, doctors)
        self.assertEqual([row["name"] for row in per_doctor],
                         ["Dr One", "Dr Two", "Dr Three"], "busiest first")
        self.assertEqual(per_doctor[0]["total"], 2)
        self.assertEqual(per_doctor[2]["total"], 0)
        self.assertEqual(sum(row["total"] for row in per_doctor), len(rows))


class StatisticsApiTestCase(unittest.TestCase):
    """Both endpoints, over a database whose contents are known exactly."""

    PLAN = {
        # doctor -> (status, how many, day offset)
        "D001": [(Status.CONFIRMED, 6, 0), (Status.COMPLETED, 2, 0),
                 (Status.PENDING, 1, 0), (Status.CANCELLED, 1, 0),
                 (Status.COMPLETED, 3, -40)],
        "D002": [(Status.CONFIRMED, 3, 0), (Status.CANCELLED_BY_DOCTOR, 2, 0)],
    }

    @classmethod
    def setUpClass(cls):
        os.environ["DASHBOARD_PASSWORD"] = DOCTOR_PASSWORD
        os.environ["SUPERADMIN_PASSWORD"] = ADMIN_PASSWORD

        from fastapi.testclient import TestClient
        from api import auth, main
        from firebase.firebase_config import LocalRepository, reset_repository
        from firebase.seed_data import seed

        cls.auth = auth
        cls.repo = LocalRepository()
        reset_repository(cls.repo)
        seed(cls.repo, with_sample_appointment=False)

        cls.client = TestClient(main.app)
        cls.client.__enter__()
        from appointment_backend.api import set_backend
        from appointment_backend.appointment_service import AppointmentBackend
        set_backend(AppointmentBackend(cls.repo))

        # The app's start-up seeds a demo appointment; clear the collection so
        # the only records are the ones this test writes.
        for existing in cls.repo.query(Collections.APPOINTMENTS):
            cls.repo.delete(Collections.APPOINTMENTS, existing["appointment_id"])

        index = 0
        for doctor_id, plan in cls.PLAN.items():
            for status, count, offset in plan:
                for _ in range(count):
                    index += 1
                    appointment_id = "APTSTAT%03d" % index
                    cls.repo.set(Collections.APPOINTMENTS, appointment_id, {
                        "appointment_id": appointment_id, "patient_id": "P001",
                        "doctor_id": doctor_id, "clinic_id": "C001",
                        "date": days_from_today(offset), "time": "16:00",
                        "status": status})

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.auth.clear_all_sessions()
        os.environ.pop("DASHBOARD_PASSWORD", None)
        os.environ.pop("SUPERADMIN_PASSWORD", None)

    def sign_in(self, user_id, password):
        response = self.client.post("/auth/login",
                                    json={"user_id": user_id, "password": password})
        return response.json()["data"]["token"] if response.status_code == 200 else None

    def headers(self, token):
        return {"Authorization": "Bearer " + token}

    def counted_by_hand(self, doctor_id=None, only_today=False):
        rows = self.repo.query(Collections.APPOINTMENTS)
        if doctor_id:
            rows = [r for r in rows if r["doctor_id"] == doctor_id]
        if only_today:
            rows = [r for r in rows if r["date"] == today()]
        return {
            "total": len(rows),
            "confirmed": len([r for r in rows if r["status"] == Status.CONFIRMED]),
            "pending": len([r for r in rows if r["status"] == Status.PENDING]),
            "completed": len([r for r in rows if r["status"] == Status.COMPLETED]),
            "cancelled": len([r for r in rows if r["status"] in
                              (Status.CANCELLED, Status.CANCELLED_BY_DOCTOR)]),
        }

    def assertMatchesDatabase(self, reported, expected, label=""):
        for key, value in expected.items():
            self.assertEqual(reported[key], value,
                             label + " " + key + " should be " + str(value))


class TestDoctorStatistics(StatisticsApiTestCase):
    def setUp(self):
        self.token = self.sign_in("D001", DOCTOR_PASSWORD)

    def get(self, path, token=None):
        response = self.client.get(path, headers=self.headers(token or self.token))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_the_figures_are_what_the_database_holds(self):
        body = self.get("/dashboard/statistics?period=all")
        self.assertMatchesDatabase(body["totals"], self.counted_by_hand("D001"),
                                   "all time")
        # the specification's own example
        self.assertEqual(body["today"]["total"], 10)
        self.assertEqual(body["today"]["confirmed"], 6)
        self.assertEqual(body["today"]["pending"], 1)
        self.assertEqual(body["today"]["completed"], 2)
        self.assertEqual(body["today"]["cancelled"], 1)

    def test_today_counts_only_today(self):
        body = self.get("/dashboard/statistics?period=today")
        self.assertMatchesDatabase(body["totals"],
                                   self.counted_by_hand("D001", only_today=True),
                                   "today")
        self.assertLess(body["totals"]["total"], body["all_time"]["total"],
                        "older appointments exist outside today")

    def test_a_doctor_is_counted_over_their_own_records_only(self):
        mine = self.get("/dashboard/statistics?period=all")
        theirs = self.get("/dashboard/statistics?period=all",
                          token=self.sign_in("D002", DOCTOR_PASSWORD))
        self.assertEqual(mine["totals"]["total"], self.counted_by_hand("D001")["total"])
        self.assertEqual(theirs["totals"]["total"], self.counted_by_hand("D002")["total"])
        self.assertNotEqual(mine["totals"]["total"], theirs["totals"]["total"])

    def test_a_doctor_cannot_ask_for_somebody_elses_numbers(self):
        """There is no doctor_id parameter: the session decides."""
        body = self.get("/dashboard/statistics?period=all&doctor_id=D002")
        self.assertEqual(body["totals"]["total"], self.counted_by_hand("D001")["total"])

    def test_a_doctor_cannot_reach_the_clinic_wide_figures(self):
        response = self.client.get("/admin/statistics", headers=self.headers(self.token))
        self.assertEqual(response.status_code, 403)

    def test_the_period_filter_changes_the_answer(self):
        month = self.get("/dashboard/statistics?period=month")["totals"]["total"]
        everything = self.get("/dashboard/statistics?period=all")["totals"]["total"]
        old = self.get("/dashboard/statistics?period=custom&start="
                       + days_from_today(-45) + "&end=" + days_from_today(-35))
        self.assertEqual(old["totals"]["total"], 3, "the three older completions")
        self.assertEqual(old["totals"]["completed"], 3)
        self.assertGreaterEqual(everything, month)

    def test_a_backwards_custom_range_is_refused(self):
        response = self.client.get(
            "/dashboard/statistics?period=custom&start=" + days_from_today(5)
            + "&end=" + days_from_today(1), headers=self.headers(self.token))
        self.assertEqual(response.status_code, 400)
        self.assertIn("end date", response.text)

    def test_completing_an_appointment_moves_it_between_the_figures(self):
        before = self.get("/dashboard/statistics?period=today")["totals"]
        pending = next(a for a in self.repo.query(Collections.APPOINTMENTS)
                       if a["doctor_id"] == "D001" and a["status"] == Status.CONFIRMED
                       and a["date"] == today())

        self.addCleanup(self.repo.update, Collections.APPOINTMENTS,
                        pending["appointment_id"], {"status": Status.CONFIRMED})

        done = self.client.post("/dashboard/appointments/"
                                + pending["appointment_id"] + "/complete",
                                headers=self.headers(self.token))
        self.assertEqual(done.status_code, 200, done.text)

        after = self.get("/dashboard/statistics?period=today")["totals"]
        self.assertEqual(after["total"], before["total"], "nothing was deleted")
        self.assertEqual(after["confirmed"], before["confirmed"] - 1)
        self.assertEqual(after["completed"], before["completed"] + 1)


class TestClinicStatistics(StatisticsApiTestCase):
    def setUp(self):
        self.token = self.sign_in("ADMIN", ADMIN_PASSWORD)

    def get(self, path, token=None):
        response = self.client.get(path, headers=self.headers(token or self.token))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_the_clinic_figures_are_every_doctor_added_up(self):
        body = self.get("/admin/statistics?period=all")
        self.assertMatchesDatabase(body["totals"], self.counted_by_hand(), "clinic")
        self.assertEqual(sum(row["total"] for row in body["doctors"]),
                         body["totals"]["total"],
                         "the doctor rows must add up to the clinic total")

    def test_every_doctor_on_the_register_has_a_row(self):
        body = self.get("/admin/statistics?period=all")
        listed = [d["doctor_id"] for d in self.repo.query(Collections.DOCTORS)
                  if not d.get("archived")]
        self.assertEqual(sorted(row["doctor_id"] for row in body["doctors"]),
                         sorted(listed))

    def test_each_doctor_row_matches_that_doctors_records(self):
        body = self.get("/admin/statistics?period=all")
        for row in body["doctors"]:
            self.assertMatchesDatabase(row, self.counted_by_hand(row["doctor_id"]),
                                       row["doctor_id"])

    def test_the_figures_can_be_narrowed_to_one_doctor(self):
        body = self.get("/admin/statistics?period=all&doctor_id=D002")
        self.assertEqual(body["doctor"]["doctor_id"], "D002")
        self.assertMatchesDatabase(body["totals"], self.counted_by_hand("D002"), "D002")
        self.assertEqual(len(body["doctors"]),
                         len([d for d in self.repo.query(Collections.DOCTORS)
                              if not d.get("archived")]),
                         "the comparison table still lists everybody")

    def test_today_for_the_whole_clinic(self):
        body = self.get("/admin/statistics?period=today")
        self.assertMatchesDatabase(body["totals"], self.counted_by_hand(only_today=True),
                                   "today")

    def test_an_unknown_doctor_is_a_404(self):
        response = self.client.get("/admin/statistics?doctor_id=D999",
                                   headers=self.headers(self.token))
        self.assertEqual(response.status_code, 404)

    def test_cancelling_moves_the_clinic_figures(self):
        before = self.get("/admin/statistics?period=today")["totals"]
        live = next(a for a in self.repo.query(Collections.APPOINTMENTS)
                    if a["status"] == Status.CONFIRMED and a["date"] == today())

        self.addCleanup(self.repo.update, Collections.APPOINTMENTS,
                        live["appointment_id"], {"status": Status.CONFIRMED})

        cancelled = self.client.post("/admin/appointments/"
                                     + live["appointment_id"] + "/cancel",
                                     headers=self.headers(self.token),
                                     json={"reason": "Counted in a test"})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)

        after = self.get("/admin/statistics?period=today")["totals"]
        self.assertEqual(after["total"], before["total"], "the record is kept")
        self.assertEqual(after["confirmed"], before["confirmed"] - 1)
        self.assertEqual(after["cancelled"], before["cancelled"] + 1)


if __name__ == "__main__":
    unittest.main()
