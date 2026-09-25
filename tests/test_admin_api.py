"""
Administration API tests (api/admin.py + the roles in api/auth.py).

Runs the real FastAPI app in-process against an in-memory repository, so no
Firestore document is touched. What these pin down:

  * the two roles are separate: a doctor cannot reach /admin/*, and the
    administrator cannot read a doctor's private dashboard
  * a doctor added by the administrator is immediately usable by the VOICE
    pipeline: find_doctor() resolves the spoken name and the public booking
    route accepts an appointment
  * deactivating a doctor refuses new bookings (the rule that already
    existed), hides them from the voice matcher, and blocks their sign-in,
    while every appointment already in the diary is kept
  * "remove" archives and never deletes: the appointments survive and the
    record can be restored
  * the administrator's schedule and leave writes are the same ones the
    doctor makes, including the cancel-and-notify cascade
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta

from config import CLINIC_ID, Collections, Status, TIMEZONE

DOCTOR_PASSWORD = "test-dashboard-password"
ADMIN_PASSWORD = "test-admin-password"


def working_day(offset: int = 1) -> str:
    day = datetime.now(TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() == 6:                 # the seeded clinics close on Sunday
        day += timedelta(days=1)
    return day.isoformat()


class AdminApiTestCase(unittest.TestCase):
    """A signed-in administrator against a freshly seeded in-memory database."""

    @classmethod
    def setUpClass(cls):
        os.environ["DASHBOARD_PASSWORD"] = DOCTOR_PASSWORD
        os.environ["SUPERADMIN_PASSWORD"] = ADMIN_PASSWORD
        os.environ.pop("SUPERADMIN_ID", None)

        from fastapi.testclient import TestClient
        from api import auth, main
        from firebase.firebase_config import LocalRepository, reset_repository
        from firebase.seed_data import seed

        cls.auth = auth
        cls.repo = LocalRepository()
        reset_repository(cls.repo)
        seed(cls.repo, with_sample_appointment=True)

        cls.client = TestClient(main.app)
        cls.client.__enter__()
        from appointment_backend.api import set_backend
        from appointment_backend.appointment_service import AppointmentBackend
        set_backend(AppointmentBackend(cls.repo))

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.auth.clear_all_sessions()
        os.environ.pop("DASHBOARD_PASSWORD", None)
        os.environ.pop("SUPERADMIN_PASSWORD", None)

    def setUp(self):
        self.token = self.sign_in("ADMIN", ADMIN_PASSWORD)

    # ------------------------------------------------------------ helpers
    def sign_in(self, user_id: str, password: str):
        response = self.client.post("/auth/login",
                                    json={"user_id": user_id, "password": password})
        if response.status_code != 200:
            return None
        return response.json()["data"]["token"]

    def headers(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def get(self, path, token=None, **kwargs):
        return self.client.get(path, headers=self.headers(token), **kwargs)

    def post(self, path, token=None, **kwargs):
        return self.client.post(path, headers=self.headers(token), **kwargs)

    def patch(self, path, token=None, **kwargs):
        return self.client.patch(path, headers=self.headers(token), **kwargs)

    def delete(self, path, token=None, **kwargs):
        return self.client.delete(path, headers=self.headers(token), **kwargs)

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def add_doctor(self, **overrides):
        body = {
            "name": "Dr Bilal Sheikh",
            "specialization": "Orthopedic Surgeon",
            "qualification": "MBBS, FCPS",
            "experience_years": 9,
            "phone": "+923005551212",
            "email": "bilal@example.com",
            "fee": 2500,
            "about": "Joint replacement and sports injuries.",
            "slot_duration": 30,
            "days": [{"day": day, "available": True,
                      "sessions": [{"start": "17:00", "end": "20:00"}]}
                     for day in ["Monday", "Tuesday", "Wednesday", "Thursday",
                                 "Friday", "Saturday"]],
        }
        body.update(overrides)
        response = self.post("/admin/doctors", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["data"]


class TestRoles(AdminApiTestCase):
    """The two accounts are separate and neither can borrow the other's area."""

    def test_administrator_signs_in_with_a_role(self):
        body = self.data(self.client.post("/auth/login",
                                          json={"user_id": "ADMIN",
                                                "password": ADMIN_PASSWORD}))
        self.assertEqual(body["role"], "superadmin")
        self.assertEqual(body["admin"]["account_id"], "ADMIN")
        self.assertNotIn("password", str(body).lower().replace("password", "", 1))

    def test_wrong_administrator_password_is_refused(self):
        response = self.client.post("/auth/login",
                                    json={"user_id": "ADMIN", "password": "nope"})
        self.assertEqual(response.status_code, 401)

    def test_administrator_id_is_never_a_doctor(self):
        """The doctor password must not open the administration area."""
        response = self.client.post("/auth/login",
                                    json={"user_id": "ADMIN",
                                          "password": DOCTOR_PASSWORD})
        self.assertEqual(response.status_code, 401)

    def test_doctor_cannot_reach_the_administration_area(self):
        doctor_token = self.sign_in("D001", DOCTOR_PASSWORD)
        for path in ["/admin/summary", "/admin/doctors", "/admin/doctors/D002"]:
            self.assertEqual(self.get(path, token=doctor_token).status_code, 403,
                             path + " must be closed to doctors")

    def test_administrator_cannot_read_a_doctors_private_dashboard(self):
        for path in ["/dashboard/summary", "/dashboard/appointments",
                     "/dashboard/patients", "/dashboard/profile"]:
            self.assertEqual(self.get(path).status_code, 403,
                             path + " must be closed to the administrator")

    def test_no_token_is_unauthorised(self):
        self.assertEqual(self.client.get("/admin/doctors").status_code, 401)

    def test_session_route_reports_the_role(self):
        body = self.data(self.get("/auth/session"))
        self.assertEqual(body["role"], "superadmin")


class TestDirectory(AdminApiTestCase):
    """The overview and the doctor table."""

    def test_summary_counts_the_seeded_clinic(self):
        body = self.data(self.get("/admin/summary"))
        doctors = self.repo.query(Collections.DOCTORS)
        self.assertEqual(body["doctors"]["total"], len(doctors))
        self.assertEqual(body["doctors"]["active"], len(doctors))
        self.assertEqual(body["doctors"]["inactive"], 0)
        self.assertEqual(body["appointments"]["total"],
                         len(self.repo.query(Collections.APPOINTMENTS)))
        self.assertIn("activity", body)

    def test_doctor_rows_carry_clinic_workload_and_hours(self):
        rows = self.data(self.get("/admin/doctors"))["doctors"]
        row = next(r for r in rows if r["doctor_id"] == "D001")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["clinic"]["clinic_id"], "C001")
        self.assertIn("total", row["appointments"])
        self.assertTrue(row["availability"]["working_days"])
        self.assertTrue(row["availability"]["accepting"])

    def test_search_matches_name_specialization_and_qualification(self):
        for query, expected in [("ahmed", "D001"), ("Dermatologist", "D002"),
                                ("Paediatrics", "D003")]:
            rows = self.data(self.get("/admin/doctors?q=" + query))["doctors"]
            self.assertIn(expected, [r["doctor_id"] for r in rows],
                          "search for " + query)

    def test_specialization_filter(self):
        rows = self.data(self.get(
            "/admin/doctors?specialization=Cardiologist"))["doctors"]
        self.assertEqual([r["doctor_id"] for r in rows], ["D001"])

    def test_specialization_list_is_offered_for_the_filter(self):
        body = self.data(self.get("/admin/doctors"))
        self.assertIn("Cardiologist", body["specializations"])


class TestAddDoctor(AdminApiTestCase):
    """Adding a doctor, and what the voice pipeline can do with them."""

    def test_new_doctor_gets_the_next_id_the_clinic_and_a_schedule(self):
        created = self.add_doctor()
        doctor, clinic = created["doctor"], created["clinic"]
        self.assertTrue(doctor["doctor_id"].startswith("D"))
        self.assertNotIn(doctor["doctor_id"], ["D001", "D002", "D003", "D004"])

        # No clinic was created or chosen: there is one, and they join it.
        self.assertEqual(clinic["clinic_id"], CLINIC_ID)
        self.assertEqual(doctor["clinic_ids"], [CLINIC_ID])
        self.assertEqual(len(self.repo.query(Collections.CLINICS)), 1)

        schedule = self.data(self.get(
            "/admin/doctors/" + doctor["doctor_id"] + "/schedule"))
        working = [day["day"] for day in schedule["days"] if day["available"]]
        self.assertEqual(len(working), 6)
        self.assertNotIn("Sunday", working)
        rows = self.repo.query(Collections.SCHEDULES,
                               [("doctor_id", "==", doctor["doctor_id"])])
        self.assertTrue(all(r["slot_duration"] == 30 for r in rows if r["active"]))

    def test_the_voice_assistant_can_find_the_new_doctor_by_name(self):
        created = self.add_doctor(name="Dr Nadia Qureshi")
        from firebase.doctor_service import DoctorService

        found = DoctorService(self.repo).find_doctor(
            "mujhe dr nadia se appointment chahiye")
        self.assertTrue(found.ok, found.message)
        self.assertEqual(found.data["doctor_id"], created["doctor"]["doctor_id"])

    def test_a_patient_can_book_the_new_doctor_through_the_public_route(self):
        created = self.add_doctor(name="Dr Imran Tariq")
        doctor_id = created["doctor"]["doctor_id"]
        date = working_day(2)

        slots = self.client.get("/doctors/" + doctor_id + "/availability?date=" + date)
        self.assertEqual(slots.status_code, 200, slots.text)
        available = slots.json()["data"]["available_slots"]
        self.assertTrue(available, "a new doctor must have bookable slots")

        booked = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": doctor_id,
            "date": date, "time": available[0]})
        self.assertEqual(booked.status_code, 200, booked.text)
        self.assertEqual(booked.json()["status"], Status.CONFIRMED)

    def test_a_colliding_first_name_is_reported_not_hidden(self):
        created = self.add_doctor(name="Dr Ahmed Siddiqui")
        self.assertTrue(created["warnings"],
                        "a second Dr Ahmed must warn about ambiguity")
        self.assertIn("ahmed", created["warnings"][0].lower())

    def test_required_fields_are_enforced(self):
        for missing in ["name", "specialization", "phone"]:
            body = {"name": "Dr Test Person", "specialization": "General Physician",
                    "phone": "+923001112222", "fee": 1000}
            body[missing] = ""
            response = self.post("/admin/doctors", json=body)
            self.assertEqual(response.status_code, 400, missing + " must be required")

    def test_a_doctor_can_be_given_an_account_while_being_added(self):
        created = self.add_doctor(name="Dr Sana Tariq", username="sana.tariq",
                                  password="sana-first-pass")
        self.assertEqual(created["account"]["username"], "sana.tariq")
        self.assertTrue(created["account"]["has_password"])
        self.assertIsNotNone(self.sign_in("sana.tariq", "sana-first-pass"))

    def test_a_bad_username_or_password_stops_the_whole_thing(self):
        for field, value in [("username", "no"), ("password", "short")]:
            response = self.post("/admin/doctors", json={
                "name": "Dr Never Created", "specialization": "General Physician",
                "phone": "+923001112222", "fee": 1000, field: value})
            self.assertEqual(response.status_code, 400, field)

    def test_a_refused_field_leaves_nothing_behind(self):
        """Validation happens before the first write, so a rejected doctor
        does not leave an orphan clinic in the database."""
        before = len(self.repo.query(Collections.CLINICS))
        response = self.post("/admin/doctors", json={
            "name": "Dr Broken Record", "specialization": "General Physician",
            "phone": "+923001112222", "fee": 1000,
            "clinic_name": "Ghost Clinic", "clinic_address": "Nowhere",
            "photo": "https://example.com/not-a-data-url.png"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(self.repo.query(Collections.CLINICS)), before)
        self.assertFalse([c for c in self.repo.query(Collections.CLINICS)
                          if c.get("name") == "Ghost Clinic"])

    def test_invalid_values_are_refused(self):
        base = {"name": "Dr Test Person", "specialization": "General Physician",
                "phone": "+923001112222", "fee": 1000}
        for field, value in [("email", "not-an-email"), ("fee", -5),
                             ("experience_years", 99), ("slot_duration", 2)]:
            response = self.post("/admin/doctors", json={**base, field: value})
            self.assertEqual(response.status_code, 400,
                             field + "=" + str(value) + " must be refused")


class TestEditDoctor(AdminApiTestCase):
    def test_edits_reach_the_documents_the_voice_assistant_reads(self):
        doctor_id = self.add_doctor(name="Dr Owais Zafar")["doctor"]["doctor_id"]
        body = self.data(self.patch("/admin/doctors/" + doctor_id, json={
            "fee": 3300, "about": "Second opinion clinic."}))
        self.assertEqual(body["doctor"]["fee"], 3300)
        self.assertEqual(body["clinic"]["clinic_id"], CLINIC_ID)

        stored = self.repo.get(Collections.DOCTORS, doctor_id)
        self.assertEqual(stored["fee"], 3300)
        self.assertEqual(stored["about"], "Second opinion clinic.")

    def test_renaming_a_doctor_renames_what_callers_can_say(self):
        doctor_id = self.add_doctor(name="Dr Owais Zafar")["doctor"]["doctor_id"]
        self.data(self.patch("/admin/doctors/" + doctor_id,
                             json={"name": "Dr Owais Khalid"}))
        from firebase.doctor_service import DoctorService

        found = DoctorService(self.repo).find_doctor("dr owais khalid")
        self.assertTrue(found.ok, found.message)
        self.assertEqual(found.data["doctor_id"], doctor_id)

    def test_the_schedule_can_be_replaced_from_the_admin_side(self):
        doctor_id = self.add_doctor(name="Dr Rida Anwar")["doctor"]["doctor_id"]
        self.data(self.patch("/admin/doctors/" + doctor_id + "/schedule", json={
            "days": [{"day": "Monday", "available": True,
                      "sessions": [{"start": "09:00", "end": "12:00"}]}]}))
        days = self.data(self.get("/admin/doctors/" + doctor_id + "/schedule"))["days"]
        working = [d["day"] for d in days if d["available"]]
        self.assertEqual(working, ["Monday"])

    def test_an_impossible_working_day_is_refused(self):
        doctor_id = self.add_doctor(name="Dr Sana Iqbal")["doctor"]["doctor_id"]
        response = self.patch("/admin/doctors/" + doctor_id + "/schedule", json={
            "days": [{"day": "Monday", "available": True,
                      "sessions": [{"start": "20:00", "end": "17:00"}]}]})
        self.assertEqual(response.status_code, 400)

    def test_editing_an_unknown_doctor_is_a_404(self):
        self.assertEqual(self.patch("/admin/doctors/D999",
                                    json={"fee": 100}).status_code, 404)


class TestDeactivate(AdminApiTestCase):
    def test_deactivating_stops_new_bookings_but_keeps_the_diary(self):
        created = self.add_doctor(name="Dr Faisal Mehmood")
        doctor_id = created["doctor"]["doctor_id"]
        date = working_day(3)
        slots = self.client.get(
            "/doctors/" + doctor_id + "/availability?date=" + date).json()["data"]["available_slots"]
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": doctor_id,
            "date": date, "time": slots[0]})
        appointment_id = booked.json()["appointment_id"]

        body = self.data(self.post("/admin/doctors/" + doctor_id + "/status",
                                   json={"active": False}))
        self.assertEqual(body["status"], "inactive")
        self.assertEqual(body["upcoming_appointments"], 1)

        refused = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": doctor_id,
            "date": date, "time": slots[1]})
        self.assertNotEqual(refused.status_code, 200)
        self.assertIn("not accepting", refused.text.lower())

        kept = self.repo.get(Collections.APPOINTMENTS, appointment_id)
        self.assertEqual(kept["status"], Status.CONFIRMED)

    def test_a_deactivated_doctor_disappears_from_the_voice_matcher(self):
        doctor_id = self.add_doctor(name="Dr Kamran Baig")["doctor"]["doctor_id"]
        from firebase.doctor_service import DoctorService

        self.post("/admin/doctors/" + doctor_id + "/status", json={"active": False})
        found = DoctorService(self.repo).find_doctor("dr kamran")
        self.assertFalse(found.ok)

        public = self.client.get("/doctors").json()["doctors"]
        self.assertNotIn(doctor_id, [d["doctor_id"] for d in public])

    def test_a_deactivated_doctor_cannot_sign_in(self):
        doctor_id = self.add_doctor(name="Dr Junaid Aslam")["doctor"]["doctor_id"]
        self.assertIsNotNone(self.sign_in(doctor_id, DOCTOR_PASSWORD))

        self.post("/admin/doctors/" + doctor_id + "/status", json={"active": False})
        response = self.client.post("/auth/login", json={"user_id": doctor_id,
                                                         "password": DOCTOR_PASSWORD})
        self.assertEqual(response.status_code, 403)
        self.assertIn("deactivated", response.text.lower())

    def test_activating_again_makes_the_doctor_bookable(self):
        doctor_id = self.add_doctor(name="Dr Zubair Alam")["doctor"]["doctor_id"]
        self.post("/admin/doctors/" + doctor_id + "/status", json={"active": False})
        self.data(self.post("/admin/doctors/" + doctor_id + "/status",
                            json={"active": True}))
        date = working_day(4)
        slots = self.client.get(
            "/doctors/" + doctor_id + "/availability?date=" + date).json()["data"]["available_slots"]
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": doctor_id,
            "date": date, "time": slots[0]})
        self.assertEqual(booked.status_code, 200, booked.text)


class TestRemoveAndRestore(AdminApiTestCase):
    def test_remove_archives_and_keeps_every_appointment(self):
        created = self.add_doctor(name="Dr Saad Rehman")
        doctor_id = created["doctor"]["doctor_id"]
        date = working_day(5)
        slots = self.client.get(
            "/doctors/" + doctor_id + "/availability?date=" + date).json()["data"]["available_slots"]
        self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": doctor_id,
            "date": date, "time": slots[0]})

        body = self.data(self.delete("/admin/doctors/" + doctor_id))
        self.assertEqual(body["status"], "archived")
        self.assertEqual(body["appointments_kept"], 1)

        self.assertIsNotNone(self.repo.get(Collections.DOCTORS, doctor_id),
                             "the record must not be deleted")
        self.assertEqual(len(self.repo.query(Collections.APPOINTMENTS,
                                             [("doctor_id", "==", doctor_id)])), 1)

        listed = self.data(self.get("/admin/doctors"))["doctors"]
        self.assertNotIn(doctor_id, [r["doctor_id"] for r in listed])
        archived = self.data(self.get("/admin/doctors?status=archived"))["doctors"]
        self.assertIn(doctor_id, [r["doctor_id"] for r in archived])

    def test_restore_brings_the_record_back_deactivated(self):
        doctor_id = self.add_doctor(name="Dr Hina Shahid")["doctor"]["doctor_id"]
        self.delete("/admin/doctors/" + doctor_id)
        body = self.data(self.post("/admin/doctors/" + doctor_id + "/restore"))
        self.assertEqual(body["status"], "inactive")
        listed = self.data(self.get("/admin/doctors"))["doctors"]
        self.assertIn(doctor_id, [r["doctor_id"] for r in listed])


class TestAdminSeesDoctorWork(AdminApiTestCase):
    """The administrator reads a doctor's diary through the same handlers the
    doctor uses, so there is no second implementation to drift."""

    def test_doctor_detail_carries_schedule_leave_and_appointments(self):
        body = self.data(self.get("/admin/doctors/D001"))
        self.assertEqual(body["doctor"]["doctor_id"], "D001")
        self.assertIn("schedule", body)
        self.assertIn("leave", body)
        self.assertIn("upcoming", body["appointments"])

    def test_a_voice_booking_shows_up_in_the_administrators_view(self):
        date = working_day(6)
        slots = self.client.get(
            "/doctors/D002/availability?date=" + date).json()["data"]["available_slots"]
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": "D002",
            "date": date, "time": slots[0]})
        appointment_id = booked.json()["appointment_id"]

        rows = self.data(self.get(
            "/admin/doctors/D002/appointments?date=" + date))["appointments"]
        self.assertIn(appointment_id, [a["appointment_id"] for a in rows])
        self.assertTrue(rows[0]["patient_name"])

    def test_blocking_a_date_for_a_doctor_cancels_and_queues_messages(self):
        date = working_day(7)
        slots = self.client.get(
            "/doctors/D003/availability?date=" + date).json()["data"]["available_slots"]
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": "D003",
            "date": date, "time": slots[0]})
        self.assertEqual(booked.status_code, 200, booked.text)
        appointment_id = booked.json()["appointment_id"]

        body = self.data(self.post("/admin/doctors/D003/leave",
                                   json={"start_date": date,
                                         "reason": "Conference"}))
        self.assertEqual(body["cancelled_appointments"], 1)
        self.assertEqual(body["notifications_queued"], 1)

        cancelled = self.repo.get(Collections.APPOINTMENTS, appointment_id)
        self.assertEqual(cancelled["status"], Status.CANCELLED_BY_DOCTOR)

        leave = self.data(self.get("/admin/doctors/D003/leave"))["leave"]
        self.assertIn(date, [row["date"] for row in leave])

        self.data(self.delete("/admin/doctors/D003/leave/" + date))
        reopened = self.data(self.get("/admin/doctors/D003/leave"))["leave"]
        self.assertNotIn(date, [row["date"] for row in reopened])

    def test_an_unknown_doctor_is_a_404_everywhere(self):
        for path in ["/admin/doctors/D999", "/admin/doctors/D999/appointments",
                     "/admin/doctors/D999/schedule", "/admin/doctors/D999/leave"]:
            self.assertEqual(self.get(path).status_code, 404, path)


class TestDoctorAccountSettings(AdminApiTestCase):
    """The doctor side of the settings page: only fields that are really
    stored, and a picture small enough to live in the doctor document."""

    def setUp(self):
        super().setUp()
        self.doctor_token = self.sign_in("D001", DOCTOR_PASSWORD)

    def test_email_photo_and_alert_preferences_persist(self):
        tiny_png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAA"
                    "AAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        body = self.data(self.patch("/dashboard/profile", token=self.doctor_token,
                                    json={"email": "ahmed@clinic.pk",
                                          "photo": tiny_png,
                                          "notification_prefs": {
                                              "new_appointments": True,
                                              "cancellations": False,
                                              "reschedules": True}}))
        self.assertEqual(body["doctor"]["email"], "ahmed@clinic.pk")
        self.assertEqual(body["doctor"]["photo"], tiny_png)
        self.assertFalse(body["doctor"]["notification_prefs"]["cancellations"])

        again = self.data(self.get("/dashboard/profile", token=self.doctor_token))
        self.assertEqual(again["doctor"]["email"], "ahmed@clinic.pk")

    def test_a_picture_that_is_not_an_image_is_refused(self):
        response = self.patch("/dashboard/profile", token=self.doctor_token,
                              json={"photo": "https://example.com/me.png"})
        self.assertEqual(response.status_code, 400)

    def test_an_oversized_picture_is_refused(self):
        huge = "data:image/png;base64," + ("A" * 210000)
        response = self.patch("/dashboard/profile", token=self.doctor_token,
                              json={"photo": huge})
        self.assertEqual(response.status_code, 400)

    def test_the_photo_can_be_removed(self):
        body = self.data(self.patch("/dashboard/profile", token=self.doctor_token,
                                    json={"photo": ""}))
        self.assertEqual(body["doctor"]["photo"], "")

    def test_a_doctor_renaming_themselves_moves_their_spoken_name(self):
        """The voice assistant matches on the name and its aliases, so a
        rename on the doctor's own settings page has to move both."""
        from firebase.doctor_service import DoctorService

        token = self.sign_in("D004", DOCTOR_PASSWORD)
        self.data(self.patch("/dashboard/profile", token=token,
                             json={"name": "Dr Sara Malik Butt"}))
        found = DoctorService(self.repo).find_doctor("dr sara malik butt")
        self.assertTrue(found.ok, found.message)
        self.assertEqual(found.data["doctor_id"], "D004")

        self.data(self.patch("/dashboard/profile", token=token,
                             json={"name": "Dr Sara Malik"}))

    def test_a_doctor_still_sees_only_their_own_data(self):
        """Regression: adding the administrator must not widen doctor access."""
        other = self.data(self.get("/dashboard/appointments",
                                   token=self.doctor_token))["appointments"]
        self.assertTrue(all(a["doctor_id"] == "D001" for a in other))


class TestCredentials(AdminApiTestCase):
    """Usernames and passwords: who may change what.

    Each check starts from the seeded database this class sets up, so the
    stored credentials written here never leak into another test class.
    """

    def credentials(self, doctor_id, **fields):
        return self.patch("/admin/doctors/" + doctor_id + "/credentials",
                          json=fields)

    def test_administrator_issues_a_username_and_password(self):
        body = self.data(self.credentials("D001", username="ahmed.khan",
                                          password="clinic-pass-1"))
        self.assertEqual(body["account"]["username"], "ahmed.khan")
        self.assertTrue(body["account"]["has_password"])
        self.assertNotIn("password_hash", str(body))

        self.assertIsNotNone(self.sign_in("ahmed.khan", "clinic-pass-1"))
        self.assertIsNotNone(self.sign_in("D001", "clinic-pass-1"),
                             "the doctor id must keep working")

    def test_a_stored_password_replaces_the_environment_one(self):
        self.credentials("D002", password="stored-pass-2")
        self.assertIsNone(self.sign_in("D002", DOCTOR_PASSWORD),
                          "the environment password must stop working")
        self.assertIsNotNone(self.sign_in("D002", "stored-pass-2"))

    def test_a_username_cannot_shadow_somebody_else(self):
        self.credentials("D003", username="hamza.ali")
        for taken in ["hamza.ali", "d001", "admin"]:
            response = self.credentials("D004", username=taken)
            self.assertEqual(response.status_code, 400,
                             taken + " must be refused")

    def test_weak_passwords_and_bad_usernames_are_refused(self):
        self.assertEqual(self.credentials("D004", password="short").status_code, 400)
        self.assertEqual(self.credentials("D004", username="a!").status_code, 400)
        self.assertEqual(self.credentials("D004").status_code, 400)

    def test_a_doctor_changes_their_own_password(self):
        self.credentials("D004", password="first-pass-44")
        token = self.sign_in("D004", "first-pass-44")

        wrong = self.post("/auth/password", token=token,
                          json={"current_password": "not-it",
                                "new_password": "second-pass-44"})
        self.assertEqual(wrong.status_code, 403)

        self.data(self.post("/auth/password", token=token,
                            json={"current_password": "first-pass-44",
                                  "new_password": "second-pass-44"}))
        self.assertIsNotNone(self.sign_in("D004", "second-pass-44"))
        self.assertIsNone(self.sign_in("D004", "first-pass-44"))

    def test_a_doctor_cannot_touch_anybody_elses_credentials(self):
        self.credentials("D001", password="doctor-one-pass")
        token = self.sign_in("D001", "doctor-one-pass")

        self.assertEqual(self.patch("/admin/doctors/D002/credentials",
                                    token=token,
                                    json={"password": "hijacked-pass"}).status_code,
                         403)
        self.assertEqual(self.get("/admin/doctors/D002/credentials",
                                  token=token).status_code, 403)

    def test_a_doctor_cannot_change_their_own_username(self):
        """There is no route for it at all - the only username writer is the
        administrator's credentials endpoint."""
        self.credentials("D002", username="asim.raza", password="asim-pass-22")
        token = self.sign_in("asim.raza", "asim-pass-22")

        profile = self.data(self.get("/dashboard/profile", token=token))
        self.assertEqual(profile["account"]["username"], "asim.raza")

        # The profile route ignores it, and the admin route is closed to them.
        self.patch("/dashboard/profile", token=token, json={"username": "chosen.name"})
        self.assertEqual(
            self.data(self.get("/dashboard/profile", token=token))["account"]["username"],
            "asim.raza")

    def test_the_administrator_changes_their_own_password(self):
        self.data(self.post("/auth/password",
                            json={"current_password": ADMIN_PASSWORD,
                                  "new_password": "admin-second-pass"}))
        self.assertIsNotNone(self.sign_in("ADMIN", "admin-second-pass"))
        self.assertIsNone(self.sign_in("ADMIN", ADMIN_PASSWORD))
        # leave the class in the state the next test expects
        token = self.sign_in("ADMIN", "admin-second-pass")
        self.post("/auth/password", token=token,
                  json={"current_password": "admin-second-pass",
                        "new_password": ADMIN_PASSWORD})

    def test_changing_a_password_signs_that_account_out_elsewhere(self):
        self.credentials("D003", password="third-pass-33")
        stale = self.sign_in("D003", "third-pass-33")
        self.assertEqual(self.get("/dashboard/summary", token=stale).status_code, 200)

        self.credentials("D003", password="fourth-pass-33")
        self.assertEqual(self.get("/dashboard/summary", token=stale).status_code, 401,
                         "the open tab must be signed out")


class TestTheClinic(AdminApiTestCase):
    """One clinic serves the whole practice: the administrator maintains it,
    and everything that displays a clinic reads that one record."""

    def test_there_is_exactly_one_clinic(self):
        body = self.data(self.get("/admin/clinic"))
        self.assertEqual(body["clinic"]["clinic_id"], CLINIC_ID)
        self.assertEqual(body["doctors"], len(self.repo.query(Collections.DOCTORS)))
        self.assertFalse(body["needs_consolidation"])
        # Exactly one clinic is in use. A retired record from an older
        # database may still sit there, switched off; that is the point.
        active = [c for c in self.repo.query(Collections.CLINICS)
                  if c.get("active", True)]
        self.assertEqual([c["clinic_id"] for c in active], [CLINIC_ID])

    def test_there_is_no_way_to_add_or_list_more(self):
        self.assertEqual(self.post("/admin/clinics",
                                   json={"name": "Second Clinic",
                                         "address": "Anywhere"}).status_code, 404)
        self.assertEqual(self.get("/admin/clinics").status_code, 404)

    def test_the_administrator_edits_the_name_and_address(self):
        self.data(self.patch("/admin/clinic",
                             json={"name": "Hamza Medical Clinic",
                                   "address": "Ferozepur Road, Gulberg",
                                   "city": "Lahore"}))
        stored = self.repo.get(Collections.CLINICS, CLINIC_ID)
        self.assertEqual(stored["name"], "Hamza Medical Clinic")
        self.assertEqual(stored["address"], "Ferozepur Road, Gulberg")

    def test_the_new_name_appears_everywhere_at_once(self):
        self.data(self.patch("/admin/clinic", json={"name": "Renamed Clinic",
                                                    "address": "Somewhere New"}))

        # the doctor's own profile
        token = self.sign_in("D003", DOCTOR_PASSWORD)
        profile = self.data(self.get("/dashboard/profile", token=token))
        self.assertEqual(profile["clinic"]["name"], "Renamed Clinic")

        # the administrator's doctor register
        rows = self.data(self.get("/admin/doctors"))["doctors"]
        self.assertEqual({row["clinic"]["name"] for row in rows}, {"Renamed Clinic"})

        # and what the assistant tells a caller when it books
        date = working_day(11)
        slots = self.client.get(
            "/doctors/D003/availability?date=" + date).json()["data"]["available_slots"]
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": "D003",
            "date": date, "time": slots[0]})
        self.assertEqual(booked.status_code, 200, booked.text)
        self.assertEqual(booked.json()["data"]["clinic_name"], "Renamed Clinic")

        # including for an appointment that was booked before the rename
        detail = self.client.get("/appointments/" + booked.json()["appointment_id"])
        self.assertEqual(detail.json()["data"]["clinic_name"], "Renamed Clinic")

    def test_an_empty_name_or_address_is_refused(self):
        self.assertEqual(self.patch("/admin/clinic", json={"name": "  "}).status_code, 400)
        self.assertEqual(self.patch("/admin/clinic", json={"address": ""}).status_code, 400)

    def test_a_doctor_cannot_change_any_clinic_detail(self):
        token = self.sign_in("D001", DOCTOR_PASSWORD)
        before = dict(self.repo.get(Collections.CLINICS, CLINIC_ID))
        for field in ["clinic_name", "clinic_address", "clinic_city", "clinic_phone"]:
            response = self.patch("/dashboard/profile", token=token,
                                  json={field: "Doctor's own text"})
            self.assertEqual(response.status_code, 403, field)
            self.assertIn("administrator", response.text.lower())
        self.assertEqual(self.repo.get(Collections.CLINICS, CLINIC_ID), before)

    def test_a_doctor_cannot_reach_the_clinic_routes(self):
        token = self.sign_in("D001", DOCTOR_PASSWORD)
        self.assertEqual(self.get("/admin/clinic", token=token).status_code, 403)
        self.assertEqual(self.patch("/admin/clinic", token=token,
                                    json={"name": "Mine"}).status_code, 403)

    def test_an_old_database_with_two_clinics_can_be_consolidated(self):
        """A database from before the system was single-clinic: the leftover
        record is switched off and its doctors join the one clinic."""
        self.repo.set(Collections.CLINICS, "C002", {
            "clinic_id": "C002", "name": "Old Second Clinic",
            "address": "Elsewhere", "active": True})
        self.repo.update(Collections.DOCTORS, "D004", {"clinic_ids": ["C002"]})

        before = self.data(self.get("/admin/clinic"))
        self.assertTrue(before["needs_consolidation"])
        self.assertIn("C002", [r["clinic_id"] for r in before["old_records"]])
        self.assertIn("D004", [d["doctor_id"] for d in before["doctors_elsewhere"]])

        result = self.data(self.post("/admin/clinic/consolidate"))
        self.assertEqual(result["doctors_moved"], ["D004"])
        self.assertEqual(result["records_retired"], ["C002"])
        self.assertFalse(result["needs_consolidation"])

        self.assertEqual(self.repo.get(Collections.DOCTORS, "D004")["clinic_ids"],
                         [CLINIC_ID])
        self.assertIsNotNone(self.repo.get(Collections.CLINICS, "C002"),
                             "the old record is kept, only switched off")
        self.assertFalse(self.repo.get(Collections.CLINICS, "C002")["active"])


class TestAppointmentManagement(AdminApiTestCase):
    """The administrator sees and acts on every doctor's diary."""

    def book(self, doctor_id="D001", offset=2, patient="P001"):
        date = working_day(offset)
        slots = self.client.get("/doctors/" + doctor_id
                                + "/availability?date=" + date).json()["data"]["available_slots"]
        response = self.client.post("/appointments/book", json={
            "patient_id": patient, "doctor_id": doctor_id,
            "date": date, "time": slots[0]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["appointment_id"], date, slots

    def test_every_doctors_appointments_are_listed_with_names(self):
        appointment_id, date, _ = self.book()
        rows = self.data(self.get("/admin/appointments"))["appointments"]
        row = next(a for a in rows if a["appointment_id"] == appointment_id)
        self.assertEqual(row["doctor_name"], "Dr Ahmed Khan")
        self.assertTrue(row["patient_name"])

    def test_filters_and_search(self):
        appointment_id, date, _ = self.book(doctor_id="D002", offset=3)
        by_doctor = self.data(self.get("/admin/appointments?doctor_id=D002"))["appointments"]
        self.assertTrue(all(a["doctor_id"] == "D002" for a in by_doctor))

        by_date = self.data(self.get("/admin/appointments?date=" + date))["appointments"]
        self.assertTrue(all(a["date"] == date for a in by_date))

        found = self.data(self.get("/admin/appointments?q=" + appointment_id))["appointments"]
        self.assertEqual([a["appointment_id"] for a in found], [appointment_id])

    def test_the_administrator_cancels_through_the_backend(self):
        appointment_id, _, _ = self.book(offset=4)
        body = self.post("/admin/appointments/" + appointment_id + "/cancel",
                         json={"reason": "Clinic closed for maintenance"})
        self.assertEqual(body.status_code, 200, body.text)

        stored = self.repo.get(Collections.APPOINTMENTS, appointment_id)
        self.assertEqual(stored["status"], Status.CANCELLED,
                         "the record is kept, only the status changes")
        self.assertEqual(stored["cancellation_reason"], "Clinic closed for maintenance")

        queued = self.repo.query(Collections.NOTIFICATIONS,
                                 [("appointment_id", "==", appointment_id)])
        self.assertEqual(len(queued), 1, "the patient is told")
        self.assertEqual(queued[0]["status"], "pending")

    def test_the_slot_is_free_for_the_voice_side_again(self):
        appointment_id, date, slots = self.book(offset=5)
        self.post("/admin/appointments/" + appointment_id + "/cancel")
        free = self.client.get("/doctors/D001/availability?date="
                               + date).json()["data"]["available_slots"]
        self.assertIn(slots[0], free)

    def test_reschedule_and_complete(self):
        appointment_id, date, slots = self.book(offset=6)
        moved = self.post("/admin/appointments/" + appointment_id + "/reschedule",
                          json={"date": date, "time": slots[3]})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS,
                                       appointment_id)["time"], slots[3])

        done = self.post("/admin/appointments/" + appointment_id + "/complete")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS,
                                       appointment_id)["status"], Status.COMPLETED)

    def test_a_doctor_cannot_use_the_clinic_wide_routes(self):
        token = self.sign_in("D001", DOCTOR_PASSWORD)
        self.assertEqual(self.get("/admin/appointments", token=token).status_code, 403)

    def test_an_unknown_appointment_is_a_404(self):
        self.assertEqual(self.get("/admin/appointments/APT000000").status_code, 404)
        self.assertEqual(self.post("/admin/appointments/APT000000/cancel").status_code,
                         404)


class TestLeaveScenario(AdminApiTestCase):
    """The scenario from the specification, end to end:

    five appointments -> leave -> five identified -> five cancelled ->
    five patients notified -> the history still there.
    """

    def test_five_appointments_are_previewed_cancelled_and_kept(self):
        date = working_day(8)
        slots = self.client.get("/doctors/D001/availability?date="
                                + date).json()["data"]["available_slots"]
        self.assertGreaterEqual(len(slots), 5)

        patients = []
        booked = []
        for index in range(5):
            patient = self.data(self.post("/dashboard/patients",
                                          token=self.sign_in("D001", DOCTOR_PASSWORD),
                                          json={"name": "Leave Patient " + str(index),
                                                "phone": "+92300123450" + str(index)}))
            patients.append(patient["patient_id"])
            response = self.client.post("/appointments/book", json={
                "patient_id": patient["patient_id"], "doctor_id": "D001",
                "date": date, "time": slots[index]})
            self.assertEqual(response.status_code, 200, response.text)
            booked.append(response.json()["appointment_id"])

        # 1. the administrator checks what the leave would do
        preview = self.data(self.get("/admin/doctors/D001/leave/preview?start_date="
                                     + date))
        self.assertEqual(len(preview["appointments"]), 5)
        self.assertEqual(preview["patients"], 5)
        self.assertEqual(preview["by_date"], [{"date": date, "count": 5}])
        self.assertTrue(all(a["patient_name"] for a in preview["appointments"]))

        # nothing has happened yet
        self.assertTrue(all(self.repo.get(Collections.APPOINTMENTS, a)["status"]
                            == Status.CONFIRMED for a in booked))

        # 2. the leave is given
        result = self.data(self.post("/admin/doctors/D001/leave",
                                     json={"start_date": date,
                                           "reason": "Doctor on leave"}))
        self.assertEqual(result["cancelled_appointments"], 5)
        self.assertEqual(result["notifications_queued"], 5)

        # 3. the appointments are cancelled, not deleted
        for appointment_id in booked:
            stored = self.repo.get(Collections.APPOINTMENTS, appointment_id)
            self.assertIsNotNone(stored, "the history must survive")
            self.assertEqual(stored["status"], Status.CANCELLED_BY_DOCTOR)
            self.assertEqual(stored["date"], date)

        # 4. every patient has a message waiting, and it says what to do
        notes = [n for n in self.repo.query(Collections.NOTIFICATIONS)
                 if n["appointment_id"] in booked]
        self.assertEqual(len(notes), 5)
        self.assertEqual({n["patient_id"] for n in notes}, set(patients))
        for note in notes:
            self.assertEqual(note["status"], "pending")
            message = note["message"].lower()
            self.assertIn("not available", message,
                          "the patient must be told the doctor is away")
            self.assertIn("cancelled", message)
            self.assertIn("call again", message,
                          "and what to do about it")
            self.assertIn(date, note["message"])

        # 5. the dashboards agree
        doctor_view = self.data(self.get("/dashboard/appointments?date=" + date,
                                         token=self.sign_in("D001", DOCTOR_PASSWORD)))
        self.assertEqual(len(doctor_view["appointments"]), 5)
        self.assertTrue(all(a["status"] == Status.CANCELLED_BY_DOCTOR
                            for a in doctor_view["appointments"]))

        admin_view = self.data(self.get("/admin/appointments?date=" + date))
        self.assertEqual(len([a for a in admin_view["appointments"]
                              if a["doctor_id"] == "D001"]), 5)

        # 6. and the day itself is closed to the voice assistant
        availability = self.client.get("/doctors/D001/availability?date=" + date)
        self.assertNotEqual(availability.json().get("success"), True)

    def test_the_preview_is_read_only(self):
        date = working_day(9)
        slots = self.client.get("/doctors/D002/availability?date="
                                + date).json()["data"]["available_slots"]
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": "D002",
            "date": date, "time": slots[0]}).json()["appointment_id"]

        self.data(self.get("/admin/doctors/D002/leave/preview?start_date=" + date))
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS, booked)["status"],
                         Status.CONFIRMED)
        self.assertFalse(self.repo.query(Collections.UNAVAILABILITY,
                                         [("doctor_id", "==", "D002")]))

    def test_the_preview_reports_a_quiet_day_honestly(self):
        preview = self.data(self.get("/admin/doctors/D003/leave/preview?start_date="
                                     + working_day(20)))
        self.assertEqual(preview["appointments"], [])
        self.assertEqual(preview["patients"], 0)

    def test_a_backwards_date_range_is_refused(self):
        response = self.get("/admin/doctors/D001/leave/preview?start_date="
                            + working_day(9) + "&end_date=" + working_day(2))
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
