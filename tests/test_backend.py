"""
Appointment Backend: validation gates, double booking, doctor leave
(spec sections 6-14, 34).

These are the rules that make the system safe to let an LLM near. Every one of
them runs before anything is written to the database.
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta

from config import Collections, ErrorCode, Status, TIMEZONE
from appointment_backend.appointment_service import AppointmentBackend
from firebase.firebase_config import LocalRepository
from firebase.patient_service import PatientService
from firebase.seed_data import seed


def _day(offset: int) -> str:
    return (datetime.now(TIMEZONE).date() + timedelta(days=offset)).isoformat()


def _next_working_day(offset: int = 1) -> str:
    """Skip Sundays - the seeded clinics are closed then."""
    day = datetime.now(TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


class BackendTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = LocalRepository()
        seed(self.repo, with_sample_appointment=False)
        PatientService(self.repo).create_patient("Sana", "03219876543", "P002")
        self.backend = AppointmentBackend(self.repo)
        self.day = _next_working_day()


class TestBookingValidation(BackendTestCase):
    """Spec section 6: eleven checks before anything is written."""

    def test_successful_booking(self):
        result = self.backend.book_appointment("P001", "D001", self.day, "16:00")
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.status, Status.CONFIRMED)
        self.assertTrue(result.appointment_id.startswith("APT"))
        self.assertEqual(result.operation, "book")

    def test_unknown_patient_is_rejected(self):
        result = self.backend.book_appointment("P_NOBODY", "D001", self.day, "16:00")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, ErrorCode.PATIENT_NOT_FOUND)

    def test_unknown_doctor_is_rejected(self):
        result = self.backend.book_appointment("P001", "D_NOBODY", self.day, "16:00")
        self.assertEqual(result.error_code, ErrorCode.DOCTOR_NOT_FOUND)

    def test_past_date_is_rejected(self):
        result = self.backend.book_appointment("P001", "D001", _day(-5), "16:00")
        self.assertEqual(result.error_code, ErrorCode.DATE_IN_PAST)

    def test_malformed_date_is_rejected(self):
        result = self.backend.book_appointment("P001", "D001", "next tuesday", "16:00")
        self.assertEqual(result.error_code, ErrorCode.INVALID_DATE)

    def test_malformed_time_is_rejected(self):
        result = self.backend.book_appointment("P001", "D001", self.day, "4pm")
        self.assertEqual(result.error_code, ErrorCode.INVALID_TIME)

    def test_time_outside_the_schedule_is_rejected(self):
        # Clinic runs 16:00-20:00; 09:00 is not offered at all.
        result = self.backend.book_appointment("P001", "D001", self.day, "09:00")
        self.assertEqual(result.error_code, ErrorCode.SLOT_UNAVAILABLE)

    def test_duplicate_same_doctor_same_day(self):
        """A caller repeating themselves must not get two appointments."""
        self.backend.book_appointment("P001", "D001", self.day, "16:00")
        again = self.backend.book_appointment("P001", "D001", self.day, "16:20")
        self.assertEqual(again.error_code, ErrorCode.DUPLICATE_APPOINTMENT)

    def test_nothing_is_written_when_validation_fails(self):
        before = len(self.repo.query(Collections.APPOINTMENTS))
        self.backend.book_appointment("P_NOBODY", "D001", self.day, "16:00")
        self.backend.book_appointment("P001", "D001", _day(-5), "16:00")
        self.assertEqual(len(self.repo.query(Collections.APPOINTMENTS)), before)


class TestDoubleBooking(BackendTestCase):
    """Spec section 8."""

    def test_second_booking_of_the_same_slot_is_refused(self):
        first = self.backend.book_appointment("P001", "D001", self.day, "16:00")
        self.assertTrue(first.success)
        second = self.backend.book_appointment("P002", "D001", self.day, "16:00")
        self.assertFalse(second.success)
        self.assertEqual(second.error_code, ErrorCode.SLOT_UNAVAILABLE)

    def test_refusal_offers_alternatives(self):
        """Spec section 9."""
        self.backend.book_appointment("P001", "D001", self.day, "16:00")
        second = self.backend.book_appointment("P002", "D001", self.day, "16:00")
        self.assertTrue(second.alternative_slots)
        self.assertNotIn("16:00", second.alternative_slots)
        # Nearest-first: 16:20 should beat a slot hours later.
        self.assertEqual(second.alternative_slots[0], "16:20")

    def test_concurrent_requests_yield_one_winner(self):
        results, barrier = [], threading.Barrier(3)

        def attempt(patient_id):
            barrier.wait()
            results.append(self.backend.book_appointment(
                patient_id, "D002", self.day, "17:00",
                validate_patient=False))

        threads = [threading.Thread(target=attempt, args=(f"PX{i}",))
                   for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r.success]
        self.assertEqual(len(winners), 1, "exactly one booking may succeed")
        stored = [a for a in self.repo.query(Collections.APPOINTMENTS)
                  if a["time"] == "17:00" and a["date"] == self.day]
        self.assertEqual(len(stored), 1)

    def test_different_doctors_share_a_time(self):
        self.backend.book_appointment("P001", "D001", self.day, "16:00")
        other = self.backend.book_appointment("P002", "D002", self.day, "16:00")
        self.assertTrue(other.success)


class TestCancellation(BackendTestCase):
    """Spec section 10."""

    def setUp(self):
        super().setUp()
        booked = self.backend.book_appointment("P001", "D001", self.day, "16:00")
        self.appointment_id = booked.appointment_id

    def test_cancel_changes_status_and_keeps_the_record(self):
        result = self.backend.cancel_appointment(self.appointment_id, "P001")
        self.assertTrue(result.success)
        self.assertEqual(result.status, Status.CANCELLED)
        self.assertIsNotNone(
            self.repo.get(Collections.APPOINTMENTS, self.appointment_id))

    def test_another_patient_cannot_cancel_it(self):
        result = self.backend.cancel_appointment(self.appointment_id, "P002")
        self.assertEqual(result.error_code, ErrorCode.NOT_YOUR_APPOINTMENT)
        stored = self.repo.get(Collections.APPOINTMENTS, self.appointment_id)
        self.assertEqual(stored["status"], Status.CONFIRMED)

    def test_cancelling_twice_is_refused(self):
        self.backend.cancel_appointment(self.appointment_id, "P001")
        again = self.backend.cancel_appointment(self.appointment_id, "P001")
        self.assertEqual(again.error_code, ErrorCode.ALREADY_CANCELLED)

    def test_unknown_id(self):
        result = self.backend.cancel_appointment("APTZZZZZZ", "P001")
        self.assertEqual(result.error_code, ErrorCode.APPOINTMENT_NOT_FOUND)

    def test_cancelled_slot_is_free_again(self):
        self.backend.cancel_appointment(self.appointment_id, "P001")
        availability = self.backend.get_availability("D001", self.day)
        self.assertIn("16:00", availability.data["available_slots"])


class TestRescheduling(BackendTestCase):
    """Spec section 11."""

    def setUp(self):
        super().setUp()
        booked = self.backend.book_appointment("P001", "D001", self.day, "16:00")
        self.appointment_id = booked.appointment_id

    def test_reschedule_records_the_old_slot(self):
        result = self.backend.reschedule_appointment(
            self.appointment_id, self.day, "17:00", "P001")
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.status, Status.RESCHEDULED)
        self.assertEqual(result.data["old_time"], "16:00")
        self.assertEqual(result.data["new_time"], "17:00")

    def test_cannot_move_onto_a_taken_slot(self):
        self.backend.book_appointment("P002", "D001", self.day, "17:00")
        result = self.backend.reschedule_appointment(
            self.appointment_id, self.day, "17:00", "P001")
        self.assertEqual(result.error_code, ErrorCode.SLOT_UNAVAILABLE)

    def test_cannot_move_a_cancelled_appointment(self):
        self.backend.cancel_appointment(self.appointment_id, "P001")
        result = self.backend.reschedule_appointment(
            self.appointment_id, self.day, "17:00", "P001")
        self.assertEqual(result.error_code, ErrorCode.ALREADY_CANCELLED)

    def test_another_patient_cannot_move_it(self):
        result = self.backend.reschedule_appointment(
            self.appointment_id, self.day, "17:00", "P002")
        self.assertEqual(result.error_code, ErrorCode.NOT_YOUR_APPOINTMENT)


class TestDoctorLeave(BackendTestCase):
    """Spec sections 13-14: the flagship dashboard feature."""

    def test_blocking_a_day_cancels_every_appointment_on_it(self):
        first = self.backend.book_appointment("P001", "D001", self.day, "16:00")
        second = self.backend.book_appointment("P002", "D001", self.day, "16:20")
        self.assertTrue(first.success and second.success)

        result = self.backend.mark_doctor_unavailable(
            "D001", self.day, "Emergency surgery")
        self.assertTrue(result.success)
        self.assertEqual(result.data["cancelled_count"], 2)

        for appointment_id in (first.appointment_id, second.appointment_id):
            stored = self.repo.get(Collections.APPOINTMENTS, appointment_id)
            self.assertEqual(stored["status"], Status.CANCELLED_BY_DOCTOR)
            self.assertEqual(stored["cancellation_reason"], "Emergency surgery")

    def test_records_are_not_deleted(self):
        booked = self.backend.book_appointment("P001", "D001", self.day, "16:00")
        self.backend.mark_doctor_unavailable("D001", self.day)
        self.assertIsNotNone(
            self.repo.get(Collections.APPOINTMENTS, booked.appointment_id))

    def test_a_notification_is_queued_per_patient(self):
        self.backend.book_appointment("P001", "D001", self.day, "16:00")
        self.backend.book_appointment("P002", "D001", self.day, "16:20")
        self.backend.mark_doctor_unavailable("D001", self.day, "On leave")

        notifications = self.repo.query(Collections.NOTIFICATIONS)
        self.assertEqual(len(notifications), 2)
        self.assertEqual({n["status"] for n in notifications}, {"pending"})
        self.assertEqual({n["type"] for n in notifications},
                         {"appointment_cancelled_by_doctor"})

    def test_further_bookings_are_refused(self):
        self.backend.mark_doctor_unavailable("D001", self.day, "On leave")
        result = self.backend.book_appointment("P001", "D001", self.day, "17:00")
        self.assertEqual(result.error_code, ErrorCode.DOCTOR_UNAVAILABLE)

    def test_other_doctors_are_unaffected(self):
        self.backend.mark_doctor_unavailable("D001", self.day, "On leave")
        result = self.backend.book_appointment("P001", "D002", self.day, "17:00")
        self.assertTrue(result.success)


class TestAvailability(BackendTestCase):
    """Spec section 12."""

    def test_slots_follow_the_schedule(self):
        result = self.backend.get_availability("D001", self.day)
        self.assertTrue(result.success)
        slots = result.data["available_slots"]
        # 16:00-20:00 in 20-minute steps.
        self.assertEqual(slots[0], "16:00")
        self.assertEqual(slots[1], "16:20")
        self.assertEqual(len(slots), 12)

    def test_booked_times_disappear(self):
        self.backend.book_appointment("P001", "D001", self.day, "16:00")
        result = self.backend.get_availability("D001", self.day)
        self.assertNotIn("16:00", result.data["available_slots"])

    def test_dry_run_check_writes_nothing(self):
        before = len(self.repo.query(Collections.APPOINTMENTS))
        result = self.backend.check_appointment("D001", self.day, "16:00")
        self.assertTrue(result.success)
        self.assertTrue(result.data["bookable"])
        self.assertEqual(len(self.repo.query(Collections.APPOINTMENTS)), before)


class TestResultContract(BackendTestCase):
    """Spec section 15: one shape for success and failure."""

    def test_success_shape(self):
        result = self.backend.book_appointment("P001", "D001", self.day, "16:00")
        payload = result.to_dict()
        self.assertEqual(sorted(payload), ["appointment_id", "data", "error",
                                           "operation", "status", "success"])
        self.assertIsNone(payload["error"])

    def test_failure_shape(self):
        result = self.backend.book_appointment("P001", "D_NOBODY", self.day, "16:00")
        payload = result.to_dict()
        self.assertFalse(payload["success"])
        self.assertIsNone(payload["appointment_id"])
        self.assertEqual(sorted(payload["error"]), ["code", "message"])

    def test_failure_results_are_not_silently_falsy(self):
        """
        Regression guard. `OperationResult.__bool__` returning `self.success`
        made every failure falsy, so `if failure: return failure` guards passed
        straight through and invalid dates reached the database.
        """
        result = self.backend.book_appointment("P001", "D001", _day(-5), "16:00")
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertFalse(hasattr(OperationResultProbe(result), "truthy_trap"))


class OperationResultProbe:
    """Tiny helper so the regression test reads clearly."""

    def __init__(self, result):
        # If OperationResult ever regains a falsy __bool__, this attribute is
        # set and the assertion above fails.
        if not result:
            self.truthy_trap = True


if __name__ == "__main__":
    unittest.main()
