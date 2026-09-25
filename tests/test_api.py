"""
HTTP API tests - the doctor-dashboard and voice endpoints (api/main.py).

Runs the real FastAPI app in-process (TestClient) against an in-memory
database: the process-wide repository is set to a LocalRepository BEFORE the
app starts, so its start-up hook picks that up instead of connecting to
Firestore. The intent model is replaced by the scripted stub.

The system audit found that api/main.py re-declared several appointment
routes that the Appointment Backend router already served first - including a
/doctors/unavailability that blocked the day WITHOUT cancelling appointments.
These tests pin the correct behaviour of every route a dashboard would call.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from config import Collections, Status, TIMEZONE
from dialog_manager.dialog_manager import DialogManager
from dialog_manager.intent_router import IntentRouter
from firebase.firebase_config import LocalRepository, reset_repository
from firebase.patient_service import PatientService
from firebase.seed_data import seed
from ollama_judge.judge import OllamaJudge
from tests.helpers import StubIntentDetector
from tests.test_ollama import FakeService
from voice_pipeline import VoicePipeline


def working_day(offset: int = 1) -> str:
    day = datetime.now(TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from api import main

        cls.repo = LocalRepository()
        reset_repository(cls.repo)
        seed(cls.repo)
        PatientService(cls.repo).create_patient("Sana", "03219876543", "P002")

        cls.main = main
        cls.client = TestClient(main.app)
        cls.client.__enter__()
        # Scripted intents and no LLM: these tests are about HTTP behaviour.
        manager = DialogManager(intent_router=IntentRouter(detector=StubIntentDetector()),
                                repository=cls.repo, deterministic_responses=True)
        main.services["dialog"] = manager
        main.services["pipeline"] = VoicePipeline(
            dialog_manager=manager, judge=OllamaJudge(service=FakeService(available=False)),
            repository=cls.repo, use_llm=False, response_language="auto")

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def assertNoTraceback(self, response):
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn('File "', response.text)


class TestReadEndpoints(ApiTestCase):
    def test_health(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_doctors_are_listed_and_fetched(self):
        listing = self.client.get("/doctors")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("D001", str(listing.json()))
        one = self.client.get("/doctors/D001")
        self.assertEqual(one.status_code, 200)
        self.assertIn("Ahmed", str(one.json()))

    def test_availability(self):
        response = self.client.get("/doctors/D001/availability", params={"date": working_day(3)})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def test_a_patients_appointments(self):
        response = self.client.get("/patients/P001/appointments")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])


class TestAppointmentEndpoints(ApiTestCase):
    def test_book_then_refuse_the_same_slot(self):
        day = working_day(4)
        first = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": "D001", "date": day, "time": "16:40"})
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertTrue(body["success"])
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS, body["appointment_id"])["status"],
                         Status.CONFIRMED)

        second = self.client.post("/appointments/book", json={
            "patient_id": "P002", "doctor_id": "D001", "date": day, "time": "16:40"})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "slot_unavailable")

    def test_cancel_checks_ownership_and_keeps_the_record(self):
        day = working_day(5)
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": "D001", "date": day, "time": "17:40"}).json()
        stranger = self.client.post("/appointments/cancel", json={
            "appointment_id": booked["appointment_id"], "patient_id": "P002"})
        self.assertEqual(stranger.status_code, 403)

        owner = self.client.post("/appointments/cancel", json={
            "appointment_id": booked["appointment_id"], "patient_id": "P001"})
        self.assertEqual(owner.status_code, 200, owner.text)
        fetched = self.client.get(f"/appointments/{booked['appointment_id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["status"], Status.CANCELLED)

    def test_reschedule(self):
        day = working_day(6)
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P002", "doctor_id": "D001", "date": day, "time": "18:20"}).json()
        moved = self.client.post("/appointments/reschedule", json={
            "appointment_id": booked["appointment_id"], "new_date": day, "new_time": "19:00",
            "patient_id": "P002"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS, booked["appointment_id"])["time"], "19:00")

    def test_dry_run_check_writes_nothing(self):
        before = len(self.repo.query(Collections.APPOINTMENTS))
        response = self.client.post("/appointments/check", json={
            "doctor_id": "D001", "date": working_day(7), "time": "16:00"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.repo.query(Collections.APPOINTMENTS)), before)

    def test_doctor_leave_cancels_and_notifies(self):
        """The dashboard route must CASCADE, not merely block the day."""
        day = working_day(8)
        booked = self.client.post("/appointments/book", json={
            "patient_id": "P002", "doctor_id": "D001", "date": day, "time": "16:20"}).json()
        leave = self.client.post("/doctors/unavailability", json={
            "doctor_id": "D001", "date": day, "reason": "Conference"})
        self.assertEqual(leave.status_code, 200, leave.text)
        self.assertEqual(leave.json()["data"]["cancelled_count"], 1)
        stored = self.repo.get(Collections.APPOINTMENTS, booked["appointment_id"])
        self.assertEqual(stored["status"], Status.CANCELLED_BY_DOCTOR)
        notes = [n for n in self.repo.query(Collections.NOTIFICATIONS)
                 if n.get("appointment_id") == booked["appointment_id"]]
        self.assertEqual(len(notes), 1)
        refused = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": "D001", "date": day, "time": "17:00"})
        self.assertEqual(refused.status_code, 409)


class TestErrorsAreSafe(ApiTestCase):
    def test_a_malformed_request_is_422_without_internals(self):
        response = self.client.post("/appointments/book", json={"patient_id": "P001"})
        self.assertEqual(response.status_code, 422)
        self.assertNoTraceback(response)

    def test_a_bad_date_is_reported_as_a_client_error(self):
        response = self.client.post("/appointments/book", json={
            "patient_id": "P001", "doctor_id": "D001", "date": "2026-13-45", "time": "16:00"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_date")
        self.assertNoTraceback(response)

    def test_an_unknown_appointment_is_404(self):
        response = self.client.get("/appointments/APTNOPE00")
        self.assertEqual(response.status_code, 404)
        self.assertNoTraceback(response)


class TestConversationEndpoints(ApiTestCase):
    def test_dialog_message(self):
        response = self.client.post("/dialog/message", json={
            "session_id": "api-s1", "text": "I want an appointment with Dr Ahmed", "patient_id": "P001"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["action"], "ask_for_date")

    def test_voice_message(self):
        response = self.client.post("/voice/message", json={
            "session_id": "api-v1", "text": "Assalam o alaikum", "patient_id": "P001"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["response"])


if __name__ == "__main__":
    unittest.main()
