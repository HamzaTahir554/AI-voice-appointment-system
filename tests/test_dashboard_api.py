"""
Doctor dashboard API tests (api/dashboard.py + api/auth.py).

Runs the real FastAPI app in-process against an in-memory repository, so no
Firestore document is touched. What these pin down:

  * a doctor must sign in, and sees ONLY their own appointments and patients
  * the dashboard reuses the existing Appointment Backend rules (cancel keeps
    the record, reschedule validates the slot, completing twice is refused)
  * blocking a day runs the existing cascade: appointments become
    cancelled_by_doctor and a notification is queued per patient
  * an appointment created by the voice pipeline appears in the dashboard,
    and a dashboard change is visible to the voice pipeline
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta

from config import Collections, Status, TIMEZONE

PASSWORD = "test-dashboard-password"


def working_day(offset: int = 1) -> str:
    day = datetime.now(TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() == 6:                 # the seeded clinics close on Sunday
        day += timedelta(days=1)
    return day.isoformat()


class DashboardApiTestCase(unittest.TestCase):
    """One signed-in doctor (D001) against a freshly seeded in-memory database."""

    @classmethod
    def setUpClass(cls):
        os.environ["DASHBOARD_PASSWORD"] = PASSWORD
        from fastapi.testclient import TestClient
        from api import auth, main
        from firebase.firebase_config import LocalRepository, reset_repository
        from firebase.seed_data import seed

        cls.auth = auth
        cls.main = main
        cls.repo = LocalRepository()
        reset_repository(cls.repo)
        seed(cls.repo, with_sample_appointment=True)

        cls.client = TestClient(main.app)
        cls.client.__enter__()
        # The lifespan builds services from the process repository, which is
        # the LocalRepository set above.
        from appointment_backend.api import set_backend
        from appointment_backend.appointment_service import AppointmentBackend
        set_backend(AppointmentBackend(cls.repo))

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.auth.clear_all_sessions()
        os.environ.pop("DASHBOARD_PASSWORD", None)

    def setUp(self):
        self.token = self.sign_in("D001")

    def sign_in(self, doctor_id: str, password: str = PASSWORD):
        response = self.client.post("/auth/login",
                                    json={"doctor_id": doctor_id, "password": password})
        if response.status_code != 200:
            return None
        return response.json()["data"]["token"]

    def headers(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def get(self, path, token=None, **kwargs):
        return self.client.get(path, headers=self.headers(token), **kwargs)

    def post(self, path, token=None, **kwargs):
        return self.client.post(path, headers=self.headers(token), **kwargs)

    def book(self, date=None, time="16:00", patient_id="P001", doctor_id="D001"):
        """Book through the PUBLIC route - the one the voice pipeline uses."""
        return self.client.post("/appointments/book", json={
            "patient_id": patient_id, "doctor_id": doctor_id,
            "date": date or working_day(2), "time": time})


class TestAuthentication(DashboardApiTestCase):
    def test_dashboard_requires_a_session(self):
        for path in ("/dashboard/summary", "/dashboard/appointments",
                     "/dashboard/patients", "/dashboard/profile",
                     "/dashboard/schedule", "/dashboard/notifications"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_wrong_password_is_refused(self):
        response = self.client.post("/auth/login",
                                    json={"doctor_id": "D001", "password": "not-it"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(PASSWORD, response.text)

    def test_unknown_doctor_gets_the_same_answer_as_a_wrong_password(self):
        unknown = self.client.post("/auth/login",
                                   json={"doctor_id": "D999", "password": PASSWORD})
        wrong = self.client.post("/auth/login",
                                 json={"doctor_id": "D001", "password": "nope"})
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(unknown.json()["detail"]["error"]["message"],
                         wrong.json()["detail"]["error"]["message"])

    def test_sign_in_returns_the_doctor_and_the_session_works(self):
        response = self.client.post("/auth/login",
                                    json={"doctor_id": "D001", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["doctor"]["doctor_id"], "D001")
        self.assertTrue(data["token"])
        session = self.get("/auth/session", token=data["token"])
        self.assertEqual(session.json()["data"]["doctor_id"], "D001")

    def test_logout_ends_the_session(self):
        token = self.sign_in("D001")
        self.post("/auth/logout", token=token)
        self.assertEqual(self.get("/dashboard/summary", token=token).status_code, 401)

    def test_a_doctor_cannot_read_another_doctors_appointment(self):
        other = self.book(doctor_id="D003", date=working_day(3), time="17:00")
        self.assertTrue(other.json()["success"], other.text)
        appointment_id = other.json()["appointment_id"]

        response = self.get("/dashboard/appointments/" + appointment_id)
        self.assertEqual(response.status_code, 403)

        listed = self.get("/dashboard/appointments").json()["data"]["appointments"]
        self.assertNotIn(appointment_id, [a["appointment_id"] for a in listed])

    def test_a_doctor_cannot_cancel_another_doctors_appointment(self):
        other = self.book(doctor_id="D003", date=working_day(4), time="17:20")
        appointment_id = other.json()["appointment_id"]
        response = self.post("/dashboard/appointments/" + appointment_id + "/cancel")
        self.assertEqual(response.status_code, 403)
        stored = self.repo.get(Collections.APPOINTMENTS, appointment_id)
        self.assertEqual(stored["status"], Status.CONFIRMED)


class TestDashboardData(DashboardApiTestCase):
    def test_summary_counts_come_from_real_appointments(self):
        today = datetime.now(TIMEZONE).date().isoformat()
        booked = self.book(date=today, time="16:40")
        summary = self.get("/dashboard/summary").json()["data"]

        todays = self.repo.query(Collections.APPOINTMENTS,
                                 [("doctor_id", "==", "D001"), ("date", "==", today)])
        self.assertEqual(summary["stats"]["total"], len(todays))
        if booked.json()["success"]:
            ids = [a["appointment_id"] for a in summary["today"]]
            self.assertIn(booked.json()["appointment_id"], ids)

    def test_appointments_carry_the_patient_name(self):
        self.book(date=working_day(2), time="16:20")
        rows = self.get("/dashboard/appointments").json()["data"]["appointments"]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(appointment=row["appointment_id"]):
                self.assertTrue(row.get("patient_name"))
                self.assertEqual(row["doctor_id"], "D001")

    def test_filters_and_search(self):
        today = datetime.now(TIMEZONE).date().isoformat()
        self.book(date=today, time="17:00")
        todays = self.get("/dashboard/appointments", params={"scope": "today"}).json()["data"]
        self.assertTrue(all(a["date"] == today for a in todays["appointments"]))

        by_name = self.get("/dashboard/appointments", params={"query": "Ali"}).json()["data"]
        self.assertTrue(all("ali" in a["patient_name"].lower()
                            or "ali" in a["appointment_id"].lower()
                            for a in by_name["appointments"]))

        by_date = self.get("/dashboard/appointments", params={"date": today}).json()["data"]
        self.assertTrue(all(a["date"] == today for a in by_date["appointments"]))

    def test_patients_are_limited_to_this_doctors_patients(self):
        self.book(date=working_day(2), time="18:00")
        data = self.get("/dashboard/patients").json()["data"]
        self.assertTrue(data["patients"])
        for patient in data["patients"]:
            with self.subTest(patient=patient["patient_id"]):
                self.assertIn("phone", patient)
                self.assertNotIn("history", patient)      # the list stays light

        detail = self.get("/dashboard/patients/P001").json()["data"]
        self.assertEqual(detail["patient_id"], "P001")
        self.assertTrue(all(a["doctor_id"] == "D001" for a in detail["history"]))

    def test_notifications_start_empty_and_never_leak_other_doctors(self):
        data = self.get("/dashboard/notifications").json()["data"]
        for note in data["notifications"]:
            with self.subTest(note=note["notification_id"]):
                self.assertEqual(note["doctor_id"], "D001")


class TestAppointmentActions(DashboardApiTestCase):
    def test_cancel_keeps_the_record_and_changes_status(self):
        booked = self.book(date=working_day(2), time="19:00")
        appointment_id = booked.json()["appointment_id"]
        before = len(self.repo.query(Collections.APPOINTMENTS))

        response = self.post("/dashboard/appointments/" + appointment_id + "/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["success"])

        after = len(self.repo.query(Collections.APPOINTMENTS))
        self.assertEqual(before, after, "cancelling must not delete the record")
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS, appointment_id)["status"],
                         Status.CANCELLED)

    def test_complete_then_completing_again_is_refused(self):
        booked = self.book(date=working_day(2), time="19:20")
        appointment_id = booked.json()["appointment_id"]

        first = self.post("/dashboard/appointments/" + appointment_id + "/complete")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS, appointment_id)["status"],
                         Status.COMPLETED)

        second = self.post("/dashboard/appointments/" + appointment_id + "/complete")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "already_completed")

    def test_reschedule_moves_the_appointment(self):
        booked = self.book(date=working_day(2), time="16:00")
        appointment_id = booked.json()["appointment_id"]
        new_date = working_day(3)

        response = self.post("/dashboard/appointments/" + appointment_id + "/reschedule",
                             json={"date": new_date, "time": "17:40"})
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.repo.get(Collections.APPOINTMENTS, appointment_id)
        self.assertEqual((stored["date"], stored["time"]), (new_date, "17:40"))

    def test_reschedule_into_a_taken_slot_is_refused_by_the_existing_rules(self):
        first = self.book(date=working_day(5), time="16:00")
        second = self.book(date=working_day(5), time="16:20", patient_id="P001")
        if not second.json()["success"]:            # duplicate-per-day rule
            self.skipTest("the backend refuses two same-day bookings for one patient")
        response = self.post("/dashboard/appointments/" + second.json()["appointment_id"]
                             + "/reschedule", json={"date": working_day(5), "time": "16:00"})
        self.assertEqual(response.status_code, 409)

    def test_booking_from_the_dashboard_uses_the_backend_rules(self):
        date = working_day(6)
        response = self.post("/dashboard/appointments",
                             json={"patient_id": "P001", "date": date, "time": "16:00"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["success"])

        clash = self.post("/dashboard/appointments",
                          json={"patient_id": "P001", "date": date, "time": "16:00"})
        self.assertIn(clash.status_code, (409, 400))
        self.assertFalse(clash.json()["success"])

    def test_appointment_not_found(self):
        response = self.get("/dashboard/appointments/APT-DOES-NOT-EXIST")
        self.assertEqual(response.status_code, 404)


class TestProfileAndSchedule(DashboardApiTestCase):
    def test_profile_update_reaches_the_database(self):
        response = self.client.patch("/dashboard/profile", headers=self.headers(), json={
            "specialization": "Interventional Cardiologist",
            "fee": 2500,
            "about": "Updated from the dashboard test.",
        })
        self.assertEqual(response.status_code, 200, response.text)
        doctor = self.repo.get(Collections.DOCTORS, "D001")
        self.assertEqual(doctor["specialization"], "Interventional Cardiologist")
        self.assertEqual(doctor["fee"], 2500)

        # and the voice pipeline reads the same record
        from firebase.doctor_service import DoctorService
        self.assertEqual(DoctorService(self.repo).get_doctor("D001").data["fee"], 2500)

    def test_a_doctor_cannot_edit_the_clinic(self):
        """One clinic serves the whole practice, so it is the administrator's
        to maintain - the API refuses, it does not merely hide the field."""
        before = dict(self.repo.get(Collections.CLINICS, "C001"))
        for field in ["clinic_name", "clinic_address", "clinic_city", "clinic_phone"]:
            response = self.client.patch("/dashboard/profile",
                                         headers=self.headers(),
                                         json={field: "Changed by the doctor"})
            self.assertEqual(response.status_code, 403, field)
        self.assertEqual(self.repo.get(Collections.CLINICS, "C001"), before)

    def test_the_profile_shows_the_one_clinic(self):
        body = self.get("/dashboard/profile").json()["data"]
        self.assertEqual(body["clinic"]["clinic_id"], "C001")
        self.assertFalse(body["editable"]["clinic"])

    def test_schedule_read_and_replace(self):
        before = self.get("/dashboard/schedule").json()["data"]["days"]
        self.assertEqual([d["day"] for d in before],
                         ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])

        response = self.client.patch("/dashboard/schedule", headers=self.headers(), json={
            "days": [
                {"day": "Monday", "available": True,
                 "sessions": [{"start": "09:00", "end": "13:00"}, {"start": "16:00", "end": "20:00"}]},
                {"day": "Tuesday", "available": False, "sessions": []},
            ]})
        self.assertEqual(response.status_code, 200, response.text)
        days = {d["day"]: d for d in response.json()["data"]["days"]}
        self.assertTrue(days["Monday"]["available"])
        self.assertEqual(len(days["Monday"]["sessions"]), 2)
        self.assertFalse(days["Tuesday"]["available"])

        # the booking engine must now offer the new Monday morning slots
        from firebase.schedule_service import ScheduleService
        monday = next_weekday_iso(0)
        slots = ScheduleService(self.repo).generate_slots("D001", monday)
        self.assertTrue(slots.ok, slots.message)
        self.assertIn("09:00", slots.data)

    def test_invalid_schedule_is_refused(self):
        response = self.client.patch("/dashboard/schedule", headers=self.headers(), json={
            "days": [{"day": "Monday", "available": True,
                      "sessions": [{"start": "18:00", "end": "09:00"}]}]})
        self.assertEqual(response.status_code, 400)


class TestLeaveCascade(DashboardApiTestCase):
    def test_blocking_a_day_cancels_appointments_and_queues_notifications(self):
        date = working_day(7)
        booked = self.book(date=date, time="16:00")
        self.assertTrue(booked.json()["success"], booked.text)
        appointment_id = booked.json()["appointment_id"]

        response = self.post("/dashboard/leave",
                             json={"start_date": date, "reason": "Medical conference"})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["blocked_dates"], [date])
        self.assertGreaterEqual(data["cancelled_appointments"], 1)
        self.assertGreaterEqual(data["notifications_queued"], 1)

        stored = self.repo.get(Collections.APPOINTMENTS, appointment_id)
        self.assertEqual(stored["status"], Status.CANCELLED_BY_DOCTOR)

        queued = self.repo.query(Collections.NOTIFICATIONS,
                                 [("appointment_id", "==", appointment_id)])
        self.assertTrue(queued, "a notification must be queued for the patient")
        self.assertEqual(queued[0]["status"], "pending")

        # the voice pipeline must now refuse to book that day
        retry = self.book(date=date, time="17:00")
        self.assertFalse(retry.json()["success"])
        self.assertEqual(retry.json()["error"]["code"], "doctor_unavailable")

        listed = self.get("/dashboard/leave").json()["data"]["leave"]
        self.assertIn(date, [row["date"] for row in listed])

    def test_removing_leave_reopens_the_day(self):
        date = working_day(8)
        self.post("/dashboard/leave", json={"start_date": date, "reason": "Personal"})
        response = self.client.delete("/dashboard/leave/" + date, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)

        booked = self.book(date=date, time="16:00")
        self.assertTrue(booked.json()["success"], booked.text)

    def test_notifications_appear_for_the_doctor(self):
        date = working_day(9)
        self.book(date=date, time="16:00")
        self.post("/dashboard/leave", json={"start_date": date, "reason": "Leave"})

        data = self.get("/dashboard/notifications").json()["data"]
        self.assertTrue(data["notifications"])
        self.assertGreaterEqual(data["unread"], 1)

        first = data["notifications"][0]["notification_id"]
        self.post("/dashboard/notifications/" + first + "/read")
        after = self.get("/dashboard/notifications").json()["data"]
        marked = [n for n in after["notifications"] if n["notification_id"] == first][0]
        self.assertTrue(marked["read_by_doctor"])


class TestVoiceAndDashboardShareTheSameData(DashboardApiTestCase):
    """The point of the whole integration."""

    def test_voice_booking_appears_in_the_dashboard(self):
        date = working_day(10)
        booked = self.book(date=date, time="16:00")           # public route = voice path
        self.assertTrue(booked.json()["success"], booked.text)
        appointment_id = booked.json()["appointment_id"]

        rows = self.get("/dashboard/appointments", params={"date": date}).json()["data"]["appointments"]
        self.assertIn(appointment_id, [a["appointment_id"] for a in rows])

    def test_dashboard_cancellation_is_visible_to_the_voice_side(self):
        date = working_day(11)
        booked = self.book(date=date, time="16:00")
        appointment_id = booked.json()["appointment_id"]

        self.post("/dashboard/appointments/" + appointment_id + "/cancel")

        # what the voice pipeline sees for that patient
        patient_view = self.client.get("/patients/P001/appointments",
                                       params={"upcoming_only": True}).json()
        ids = [a["appointment_id"] for a in patient_view["data"]["appointments"]]
        self.assertNotIn(appointment_id, ids)

        # and the slot is free again for a new caller
        availability = self.client.get("/doctors/D001/availability", params={"date": date}).json()
        self.assertIn("16:00", availability["data"]["available_slots"])


def next_weekday_iso(weekday: int) -> str:
    """Next occurrence of a weekday (0 = Monday), today included."""
    today = datetime.now(TIMEZONE).date()
    return (today + timedelta(days=(weekday - today.weekday()) % 7)).isoformat()


if __name__ == "__main__":
    unittest.main()
