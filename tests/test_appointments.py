"""
Appointment workflows: booking, cancellation, rescheduling, checking,
availability and double-booking prevention (spec sections 17-22, 28).
"""
from __future__ import annotations

import threading
import unittest

from config import Action, Status
from firebase.appointment_service import AppointmentService
from firebase.schedule_service import ScheduleService
from tests.helpers import build_manager, days_ahead_iso, tomorrow_iso


class TestAvailability(unittest.TestCase):
    def setUp(self):
        self.manager, self.repo, _ = build_manager()
        self.appointments = AppointmentService(self.repo)

    def test_slots_are_generated_from_the_schedule(self):
        """16:00-20:00 in 20-minute steps = 12 slots."""
        result = self.appointments.get_available_slots("D001", tomorrow_iso())
        self.assertTrue(result.ok)
        # 18:00 is taken by the seeded appointment, so 11 remain.
        self.assertEqual(len(result.data), 11)
        self.assertIn("16:00", result.data)
        self.assertNotIn("18:00", result.data)

    def test_sunday_has_no_schedule(self):
        # Find the next Sunday.
        from datetime import datetime, timedelta
        from config import TIMEZONE
        today = datetime.now(TIMEZONE).date()
        sunday = today + timedelta(days=(6 - today.weekday()) % 7 or 7)
        result = self.appointments.get_available_slots("D001", sunday.isoformat())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "NO_SCHEDULE")

    def test_past_dates_are_rejected(self):
        result = self.appointments.get_available_slots("D001", days_ahead_iso(-3))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "DATE_IN_PAST")

    def test_invalid_date_is_rejected(self):
        result = self.appointments.get_available_slots("D001", "not-a-date")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "INVALID_DATE")

    def test_doctor_unavailability_blocks_the_day(self):
        """Spec section 15: a day blocked from the dashboard is unbookable."""
        schedules = ScheduleService(self.repo)
        schedules.mark_unavailable("D001", tomorrow_iso(), "On leave")
        result = self.appointments.get_available_slots("D001", tomorrow_iso())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "DOCTOR_UNAVAILABLE")

    def test_alternatives_are_nearest_to_the_requested_time(self):
        result = self.appointments.suggest_alternatives(
            "D001", tomorrow_iso(), around="18:00", limit=3)
        self.assertTrue(result.ok)
        # 18:00 is taken, so the neighbours come back first.
        self.assertIn("17:40", result.data)


class TestBooking(unittest.TestCase):
    def setUp(self):
        self.manager, self.repo, _ = build_manager()
        self.appointments = AppointmentService(self.repo)

    def test_booking_writes_a_confirmed_record(self):
        result = self.appointments.book_appointment(
            "P001", "D001", tomorrow_iso(), "16:00", "C001")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], Status.CONFIRMED)
        self.assertTrue(result.data["appointment_id"].startswith("APT"))

    def test_double_booking_is_refused(self):
        """Spec section 28: the same slot cannot be sold twice."""
        first = self.appointments.book_appointment(
            "P001", "D001", tomorrow_iso(), "16:00")
        self.assertTrue(first.ok)
        second = self.appointments.book_appointment(
            "P002", "D001", tomorrow_iso(), "16:00")
        self.assertFalse(second.ok)
        self.assertEqual(second.error, "SLOT_TAKEN")

    def test_concurrent_bookings_yield_exactly_one_winner(self):
        """Two callers racing for the same slot: only one may win."""
        results = []
        barrier = threading.Barrier(2)

        def attempt(patient_id):
            barrier.wait()          # maximise the overlap
            results.append(self.appointments.book_appointment(
                patient_id, "D002", tomorrow_iso(), "17:00"))

        threads = [threading.Thread(target=attempt, args=(f"P{i}",))
                   for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r.ok]
        self.assertEqual(len(successes), 1, "exactly one booking must succeed")

    def test_a_different_doctor_may_use_the_same_time(self):
        self.appointments.book_appointment("P001", "D001", tomorrow_iso(), "16:00")
        other = self.appointments.book_appointment(
            "P002", "D002", tomorrow_iso(), "16:00")
        self.assertTrue(other.ok)

    def test_dialog_offers_alternatives_when_the_slot_is_taken(self):
        result = self.manager.process_message(
            "s1", "Book with Dr Ahmed tomorrow at 6 PM")   # 18:00 is seeded
        self.assertEqual(result["action"], Action.OFFER_ALTERNATIVES)
        self.assertIn("available times", result["response"].lower())


class TestCancellation(unittest.TestCase):
    """Spec section 19: cancel by status change, never by deleting."""

    def setUp(self):
        self.manager, self.repo, _ = build_manager()
        self.appointments = AppointmentService(self.repo)

    def test_cancellation_keeps_the_record(self):
        result = self.appointments.cancel_appointment("APT123")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], Status.CANCELLED)
        # The document still exists - history is preserved.
        self.assertIsNotNone(self.repo.get("appointments", "APT123"))

    def test_cancelling_twice_is_refused(self):
        self.appointments.cancel_appointment("APT123")
        again = self.appointments.cancel_appointment("APT123")
        self.assertFalse(again.ok)
        self.assertEqual(again.error, "ALREADY_CANCELLED")

    def test_unknown_id_is_reported_not_crashed(self):
        result = self.appointments.cancel_appointment("APT999999")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "APPOINTMENT_NOT_FOUND")

    def test_cancelled_slot_becomes_available_again(self):
        self.appointments.cancel_appointment("APT123")
        slots = self.appointments.get_available_slots("D001", tomorrow_iso())
        self.assertIn("18:00", slots.data)

    def test_dialog_cancellation_flow(self):
        manager, repo, _ = build_manager()
        first = manager.process_message("s1", "I want to cancel my appointment")
        self.assertEqual(first["action"], Action.ASK_FOR_APPOINTMENT_ID)

        found = manager.process_message("s1", "APT123")
        self.assertEqual(found["action"], Action.ASK_CONFIRMATION)

        done = manager.process_message("s1", "yes")
        self.assertEqual(done["action"], Action.CANCEL_APPOINTMENT)
        self.assertEqual(repo.get("appointments", "APT123")["status"],
                         Status.CANCELLED)

    def test_dialog_finds_the_appointment_from_patient_id(self):
        """A known caller should not have to recite an ID."""
        manager, repo, _ = build_manager()
        result = manager.process_message("s1", "Cancel my appointment",
                                         patient_id="P001")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)


class TestRescheduling(unittest.TestCase):
    def setUp(self):
        self.manager, self.repo, _ = build_manager()
        self.appointments = AppointmentService(self.repo)

    def test_reschedule_moves_the_appointment(self):
        new_date = days_ahead_iso(2)
        if new_date == tomorrow_iso():          # guard against oddities
            new_date = days_ahead_iso(3)
        result = self.appointments.reschedule_appointment(
            "APT123", new_date, "17:00")
        if not result.ok and result.error == "NO_SCHEDULE":
            self.skipTest("the +2 day lands on a Sunday in this run")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["date"], new_date)
        self.assertEqual(result.data["time"], "17:00")
        self.assertEqual(result.data["status"], Status.RESCHEDULED)
        # An audit trail of the old slot is kept.
        self.assertEqual(result.data["previous_time"], "18:00")

    def test_cannot_reschedule_onto_a_taken_slot(self):
        self.appointments.book_appointment("P002", "D001", tomorrow_iso(), "16:00")
        result = self.appointments.reschedule_appointment(
            "APT123", tomorrow_iso(), "16:00")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "SLOT_TAKEN")

    def test_cannot_reschedule_a_cancelled_appointment(self):
        self.appointments.cancel_appointment("APT123")
        result = self.appointments.reschedule_appointment(
            "APT123", tomorrow_iso(), "16:00")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "ALREADY_CANCELLED")

    def test_dialog_reschedule_flow(self):
        manager, repo, _ = build_manager()
        manager.process_message("s1", "I want to reschedule", patient_id="P001")
        manager.process_message("s1", "APT123")
        asked = manager.process_message("s1", "tomorrow")
        self.assertEqual(asked["action"], Action.ASK_FOR_TIME)
        confirm = manager.process_message("s1", "5 PM")
        self.assertEqual(confirm["action"], Action.ASK_CONFIRMATION)
        done = manager.process_message("s1", "haan")
        self.assertEqual(done["action"], Action.RESCHEDULE_APPOINTMENT)


class TestCheckAppointment(unittest.TestCase):
    def test_known_patient_hears_their_appointment(self):
        manager, _, _ = build_manager()
        result = manager.process_message("s1", "When is my appointment?",
                                         patient_id="P001")
        self.assertEqual(result["action"], Action.CHECK_APPOINTMENT_STATUS)
        self.assertIn("Dr Ahmed Khan", result["response"])

    def test_lookup_by_appointment_id(self):
        manager, _, _ = build_manager()
        manager.process_message("s1", "When is my appointment?")
        result = manager.process_message("s1", "APT123")
        self.assertEqual(result["action"], Action.CHECK_APPOINTMENT_STATUS)

    def test_patient_with_no_appointments(self):
        manager, _, _ = build_manager()
        result = manager.process_message("s1", "When is my appointment?",
                                         patient_id="P_NOBODY")
        self.assertEqual(result["action"], Action.CHECK_APPOINTMENT_STATUS)
        self.assertIn("could not find", result["response"].lower())


class TestInformationIntents(unittest.TestCase):
    """Spec sections 12, 13, 22: answers come from the database."""

    def test_doctor_fee_comes_from_the_record(self):
        manager, _, _ = build_manager()
        result = manager.process_message("s1", "What is Dr Ahmed's fee?")
        # Fee questions now report their own action.
        self.assertEqual(result["action"], Action.PROVIDE_DOCTOR_FEE)
        self.assertIn("2000", result["response"])

    def test_clinic_address_comes_from_the_record(self):
        manager, _, _ = build_manager()
        result = manager.process_message("s1", "What is Dr Ahmed's clinic address?")
        self.assertEqual(result["action"], Action.PROVIDE_CLINIC_INFORMATION)
        self.assertIn("Sector G-10", result["response"])

    def test_availability_question_lists_real_slots(self):
        manager, _, _ = build_manager()
        result = manager.process_message(
            "s1", "Is Dr Ahmed available tomorrow?")
        self.assertEqual(result["action"], Action.PROVIDE_DOCTOR_AVAILABILITY)
        self.assertIn("4:00 PM", result["response"])

    def test_unknown_doctor_is_not_invented(self):
        manager, _, _ = build_manager()
        result = manager.process_message("s1", "What is Dr Nobody's fee?")
        # We must ask rather than answer about a doctor who does not exist.
        self.assertEqual(result["action"], Action.ASK_FOR_DOCTOR)


if __name__ == "__main__":
    unittest.main()
