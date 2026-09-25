"""
Live Firestore tests - the real project, real transactions.

Skipped unless RUN_LIVE_TESTS=1 and Firebase credentials are configured:

    python scripts/run_tests.py --live --category firebase

The in-memory repository tests (test_firebase.py, test_backend.py) prove the
logic. These prove the same guarantees hold against the real database, where
the double-booking guard is a genuine Firestore transaction.

Safety rules these tests follow:
  * every document they create (patients, appointments, notifications, the
    leave record) is recorded and deleted in tearDown;
  * nothing that existed before the run is modified or deleted;
  * the test day is ~4 months ahead and verified empty for the test doctor,
    so no real booking can collide with it.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from config import Collections, ErrorCode, Status, TIMEZONE

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"
DOCTOR = "D001"
REPORT = Path(__file__).resolve().parents[1] / "reports" / "firestore_live.json"
ACTIVE = (Status.CONFIRMED, Status.PENDING, Status.RESCHEDULED)


def firestore_repository():
    """A real FirestoreRepository, independent of the process-wide singleton."""
    from firebase import firebase_config
    if not firebase_config._has_firebase_config():
        return None
    import firebase_admin
    from firebase_admin import firestore
    if not firebase_admin._apps:
        firebase_admin.initialize_app(firebase_config._build_credentials())
    return firebase_config.FirestoreRepository(firestore.client())


@unittest.skipUnless(LIVE, "set RUN_LIVE_TESTS=1 to run against the real Firestore project")
class TestFirestoreLive(unittest.TestCase):
    timings: dict[str, list[float]] = {}

    @classmethod
    def setUpClass(cls):
        from appointment_backend.appointment_service import AppointmentBackend
        from firebase.schedule_service import ScheduleService

        cls.repo = firestore_repository()
        if cls.repo is None:
            raise unittest.SkipTest("Firebase credentials are not configured")
        cls.backend = AppointmentBackend(cls.repo)
        cls.schedules = ScheduleService(cls.repo)
        cls.day, cls.slots = cls._empty_working_day()
        cls.timings = {}

    @classmethod
    def tearDownClass(cls):
        if not cls.timings:
            return
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({
            "test_day": cls.day,
            "operation_seconds": {
                name: {"calls": len(v), "median": round(sorted(v)[len(v) // 2], 3),
                       "max": round(max(v), 3)}
                for name, v in cls.timings.items()},
        }, indent=2), encoding="utf-8")

    @classmethod
    def _empty_working_day(cls):
        start = datetime.now(TIMEZONE).date() + timedelta(days=120)
        for offset in range(60):
            day = (start + timedelta(days=offset)).isoformat()
            slots = cls.schedules.generate_slots(DOCTOR, day)
            if not slots.ok or len(slots.data) < 3:
                continue
            taken = cls.repo.query(Collections.APPOINTMENTS,
                                   [("doctor_id", "==", DOCTOR), ("date", "==", day)])
            blocked = cls.repo.get(Collections.UNAVAILABILITY, f"{DOCTOR}_{day}")
            if not taken and not blocked:
                return day, list(slots.data)
        raise unittest.SkipTest("no empty working day found for the test doctor")

    # ------------------------------------------------------------------
    def setUp(self):
        self.created: list[tuple[str, str]] = []

    def tearDown(self):
        for collection, doc_id in reversed(self.created):
            try:
                self.repo.delete(collection, doc_id)
            except Exception:                      # pragma: no cover
                pass

    def _timed(self, name, fn, *args):
        started = time.perf_counter()
        result = fn(*args)
        self.timings.setdefault(name, []).append(time.perf_counter() - started)
        return result

    def _patient(self) -> str:
        patient_id = f"TEST-P-{uuid.uuid4().hex[:6].upper()}"
        self.repo.set(Collections.PATIENTS, patient_id, {
            "patient_id": patient_id, "name": "Audit Test Patient",
            "phone": "03000000000"})
        self.created.append((Collections.PATIENTS, patient_id))
        return patient_id

    def _keep(self, result):
        if result.success and result.appointment_id:
            self.created.append((Collections.APPOINTMENTS, result.appointment_id))
        return result

    def _book(self, patient, time_str):
        return self._keep(self._timed("book", self.backend.book_appointment,
                                      patient, DOCTOR, self.day, time_str))

    # ------------------------------------------------------------------
    def test_connection_and_seeded_reference_data(self):
        for collection in (Collections.DOCTORS, Collections.CLINICS, Collections.SCHEDULES):
            with self.subTest(collection=collection):
                self.assertTrue(self._timed("query", self.repo.query, collection),
                                f"{collection} is empty")
        doctor = self._timed("get", self.repo.get, Collections.DOCTORS, DOCTOR)
        self.assertIsNotNone(doctor)
        self.assertTrue(doctor.get("name"))

    def test_availability_is_read_from_the_real_schedule(self):
        result = self._timed("availability", self.backend.availability.get_availability,
                             DOCTOR, self.day)
        self.assertTrue(result.success, result.error)

    def test_a_booking_is_persisted_and_read_back(self):
        patient = self._patient()
        result = self._book(patient, self.slots[0])
        self.assertTrue(result.success, result.error)
        stored = self.repo.get(Collections.APPOINTMENTS, result.appointment_id)
        self.assertEqual(stored["status"], Status.CONFIRMED)
        self.assertEqual((stored["patient_id"], stored["doctor_id"], stored["date"], stored["time"]),
                         (patient, DOCTOR, self.day, self.slots[0]))

    def test_concurrent_requests_for_one_slot_yield_one_booking(self):
        """Four callers, one slot, released at the same instant."""
        patients = [self._patient() for _ in range(4)]
        barrier = threading.Barrier(len(patients))
        results, lock = [], threading.Lock()

        def attempt(patient_id):
            barrier.wait()
            outcome = self.backend.book_appointment(patient_id, DOCTOR, self.day, self.slots[1])
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt, args=(p,)) for p in patients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(90)
        for outcome in results:
            self._keep(outcome)

        self.assertEqual(len(results), len(patients))
        winners = [r for r in results if r.success]
        self.assertEqual(len(winners), 1, [r.to_dict() for r in results])
        for outcome in results:
            if not outcome.success:
                self.assertEqual(outcome.error_code, ErrorCode.SLOT_UNAVAILABLE)
        live = [a for a in self.repo.query(Collections.APPOINTMENTS, [
            ("doctor_id", "==", DOCTOR), ("date", "==", self.day), ("time", "==", self.slots[1])])
            if a.get("status") in ACTIVE]
        self.assertEqual(len(live), 1)

    def test_cancellation_changes_status_and_keeps_the_record(self):
        patient = self._patient()
        booked = self._book(patient, self.slots[2])
        self.assertTrue(booked.success, booked.error)
        cancelled = self._timed("cancel", self.backend.cancel_appointment,
                                booked.appointment_id, patient)
        self.assertTrue(cancelled.success, cancelled.error)
        stored = self.repo.get(Collections.APPOINTMENTS, booked.appointment_id)
        self.assertIsNotNone(stored, "a cancellation must never delete the record")
        self.assertEqual(stored["status"], Status.CANCELLED)
        again = self.backend.cancel_appointment(booked.appointment_id, patient)
        self.assertEqual(again.error_code, ErrorCode.ALREADY_CANCELLED)

    def test_another_patient_cannot_cancel_it(self):
        owner, stranger = self._patient(), self._patient()
        booked = self._book(owner, self.slots[0])
        self.assertTrue(booked.success, booked.error)
        refused = self.backend.cancel_appointment(booked.appointment_id, stranger)
        self.assertEqual(refused.error_code, ErrorCode.NOT_YOUR_APPOINTMENT)
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS, booked.appointment_id)["status"],
                         Status.CONFIRMED)

    def test_reschedule_moves_it_and_frees_the_old_slot(self):
        patient = self._patient()
        booked = self._book(patient, self.slots[0])
        self.assertTrue(booked.success, booked.error)
        moved = self._timed("reschedule", self.backend.reschedule_appointment,
                            booked.appointment_id, self.day, self.slots[2], patient)
        self.assertTrue(moved.success, moved.error)
        stored = self.repo.get(Collections.APPOINTMENTS, booked.appointment_id)
        self.assertEqual(stored["time"], self.slots[2])
        other = self._book(self._patient(), self.slots[0])
        self.assertTrue(other.success, "the vacated slot should be bookable again")

    def test_doctor_leave_cancels_the_day_and_queues_notifications(self):
        first, second = self._patient(), self._patient()
        a = self._book(first, self.slots[0])
        b = self._book(second, self.slots[2])
        self.assertTrue(a.success and b.success, (a.error, b.error))

        self.created.append((Collections.UNAVAILABILITY, f"{DOCTOR}_{self.day}"))
        leave = self._timed("doctor_leave", self.backend.mark_doctor_unavailable,
                            DOCTOR, self.day, "Audit test leave")
        for note in leave.data.get("notifications", []):
            self.created.append((Collections.NOTIFICATIONS, note["notification_id"]))
        self.assertTrue(leave.success, leave.error)
        self.assertEqual(leave.data["cancelled_count"], 2)

        for appointment in (a, b):
            stored = self.repo.get(Collections.APPOINTMENTS, appointment.appointment_id)
            self.assertEqual(stored["status"], Status.CANCELLED_BY_DOCTOR)
            self.assertEqual(stored["cancellation_reason"], "Audit test leave")

        notes = [self.repo.get(Collections.NOTIFICATIONS, n["notification_id"])
                 for n in leave.data["notifications"]]
        self.assertEqual(sorted(n["patient_id"] for n in notes), sorted([first, second]))
        # Queued for an SMS / call provider - never recorded as already sent.
        self.assertTrue(all(n["status"] == "pending" for n in notes))

        refused = self._book(self._patient(), self.slots[1])
        self.assertFalse(refused.success)
        self.assertEqual(refused.error_code, ErrorCode.DOCTOR_UNAVAILABLE)

    def test_invalid_requests_write_nothing(self):
        patient = self._patient()
        before = len(self.repo.query(Collections.APPOINTMENTS,
                                     [("doctor_id", "==", DOCTOR), ("date", "==", self.day)]))
        cases = [
            (("TEST-P-DOES-NOT-EXIST", DOCTOR, self.day, self.slots[0]), ErrorCode.PATIENT_NOT_FOUND),
            ((patient, "D999", self.day, self.slots[0]), ErrorCode.DOCTOR_NOT_FOUND),
            ((patient, DOCTOR, "2020-01-06", self.slots[0]), ErrorCode.DATE_IN_PAST),
            ((patient, DOCTOR, "2027-13-45", self.slots[0]), ErrorCode.INVALID_DATE),
            ((patient, DOCTOR, self.day, "25:00"), ErrorCode.INVALID_TIME),
        ]
        for args, code in cases:
            with self.subTest(expected=code):
                result = self._keep(self.backend.book_appointment(*args))
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, code)
        after = len(self.repo.query(Collections.APPOINTMENTS,
                                    [("doctor_id", "==", DOCTOR), ("date", "==", self.day)]))
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
