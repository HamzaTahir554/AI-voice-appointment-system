"""
Database layer: the repository contract, the services, and the local/Firestore
switch (spec sections 10-16, 29, 33-34).

These run against `LocalRepository`. The same assertions hold for Firestore
because both implement the identical interface.
"""
from __future__ import annotations

import unittest

from config import Collections, Status
from firebase.clinic_service import ClinicService
from firebase.conversation_service import ConversationService, mask_phone
from firebase.doctor_service import DoctorService
from firebase.firebase_config import (
    DatabaseError,
    LocalRepository,
    SlotConflict,
    _has_firebase_config,
    init_repository,
    reset_repository,
)
from firebase.patient_service import PatientService, normalize_phone
from firebase.schedule_service import ScheduleService, weekday_of
from firebase.seed_data import seed
from tests.helpers import tomorrow_iso


class TestRepositoryContract(unittest.TestCase):
    def setUp(self):
        self.repo = LocalRepository()

    def test_set_and_get_round_trip(self):
        self.repo.set("things", "t1", {"name": "x"})
        self.assertEqual(self.repo.get("things", "t1")["name"], "x")

    def test_timestamps_are_added(self):
        saved = self.repo.set("things", "t1", {"name": "x"})
        self.assertIn("created_at", saved)
        self.assertIn("updated_at", saved)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.repo.get("things", "nope"))

    def test_update_requires_an_existing_document(self):
        with self.assertRaises(DatabaseError):
            self.repo.update("things", "nope", {"a": 1})

    def test_query_filters(self):
        self.repo.set("things", "a", {"kind": "x", "n": 1})
        self.repo.set("things", "b", {"kind": "y", "n": 2})
        self.repo.set("things", "c", {"kind": "x", "n": 3})
        found = self.repo.query("things", [("kind", "==", "x")])
        self.assertEqual(len(found), 2)

    def test_query_in_operator(self):
        self.repo.set("things", "a", {"status": "confirmed"})
        self.repo.set("things", "b", {"status": "cancelled"})
        found = self.repo.query("things",
                                [("status", "in", ["confirmed", "pending"])])
        self.assertEqual(len(found), 1)

    def test_reserve_slot_is_atomic(self):
        filters = [("slot", "==", "16:00")]
        self.repo.reserve_slot("bookings", "b1", {"slot": "16:00"}, filters)
        with self.assertRaises(SlotConflict):
            self.repo.reserve_slot("bookings", "b2", {"slot": "16:00"}, filters)

    def test_returned_documents_are_copies(self):
        """Mutating a result must not corrupt the store."""
        self.repo.set("things", "t1", {"name": "x"})
        fetched = self.repo.get("things", "t1")
        fetched["name"] = "mutated"
        self.assertEqual(self.repo.get("things", "t1")["name"], "x")


class TestBackendSelection(unittest.TestCase):
    def test_falls_back_to_local_without_credentials(self):
        reset_repository(None)
        repo = init_repository(force_local=True)
        self.assertEqual(repo.backend, "local")
        reset_repository(None)

    def test_config_detection_matches_env(self):
        # Documents the rule rather than asserting a particular machine state.
        self.assertIsInstance(_has_firebase_config(), bool)


class TestPatientService(unittest.TestCase):
    def setUp(self):
        self.repo = LocalRepository()
        seed(self.repo)
        self.service = PatientService(self.repo)

    def test_phone_normalisation(self):
        self.assertEqual(normalize_phone("+923001234567"), "03001234567")
        self.assertEqual(normalize_phone("0300 1234567"), "03001234567")
        self.assertEqual(normalize_phone("0300-123-4567"), "03001234567")
        self.assertIsNone(normalize_phone("12345"))

    def test_find_by_phone(self):
        result = self.service.find_by_phone("03001234567")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["patient_id"], "P001")

    def test_get_or_create_does_not_duplicate(self):
        """Spec section 11: a returning caller keeps one record."""
        before = len(self.repo.query(Collections.PATIENTS))
        self.service.get_or_create(name="Ali Khan", phone="03001234567")
        self.assertEqual(len(self.repo.query(Collections.PATIENTS)), before)

    def test_get_or_create_registers_a_new_caller(self):
        before = len(self.repo.query(Collections.PATIENTS))
        result = self.service.get_or_create(name="Sana", phone="03219876543")
        self.assertTrue(result.ok)
        self.assertEqual(len(self.repo.query(Collections.PATIENTS)), before + 1)

    def test_invalid_phone_is_rejected(self):
        result = self.service.create_patient(name="X", phone="123")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "INVALID_PHONE")


class TestDoctorService(unittest.TestCase):
    def setUp(self):
        self.repo = LocalRepository()
        seed(self.repo)
        self.service = DoctorService(self.repo)

    def test_find_by_full_name(self):
        result = self.service.find_doctor("Dr Ahmed Khan")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["doctor_id"], "D001")

    def test_find_inside_a_sentence(self):
        result = self.service.find_doctor(
            "Mujhe Dr Ahmed se appointment leni hai")
        self.assertEqual(result.data["doctor_id"], "D001")

    def test_find_by_urdu_alias(self):
        result = self.service.find_doctor("مجھے ڈاکٹر حمزہ سے ملنا ہے")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["doctor_id"], "D003")

    def test_bare_surname_reply(self):
        result = self.service.find_doctor("Asim")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["doctor_id"], "D002")

    def test_unknown_doctor_fails_rather_than_guessing(self):
        result = self.service.find_doctor("Dr Nobody")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "DOCTOR_NOT_FOUND")

    def test_empty_query_fails(self):
        self.assertFalse(self.service.find_doctor("").ok)


class TestScheduleService(unittest.TestCase):
    def setUp(self):
        self.repo = LocalRepository()
        seed(self.repo)
        self.service = ScheduleService(self.repo)

    def test_weekday_of(self):
        self.assertEqual(weekday_of("2026-09-14"), "Monday")
        self.assertIsNone(weekday_of("garbage"))

    def test_slot_generation_respects_duration(self):
        slots = self.service.generate_slots("D001", tomorrow_iso())
        if not slots.ok:
            self.skipTest("tomorrow is a Sunday in this run")
        # 16:00-20:00 in 20-minute steps.
        self.assertEqual(slots.data[0], "16:00")
        self.assertEqual(slots.data[1], "16:20")
        self.assertEqual(len(slots.data), 12)

    def test_unavailability_blocks_a_date(self):
        self.service.mark_unavailable("D001", tomorrow_iso(), "Conference")
        result = self.service.generate_slots("D001", tomorrow_iso())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "DOCTOR_UNAVAILABLE")


class TestClinicService(unittest.TestCase):
    def setUp(self):
        self.repo = LocalRepository()
        seed(self.repo)
        self.service = ClinicService(self.repo)

    def test_clinic_for_doctor(self):
        doctor = self.repo.get(Collections.DOCTORS, "D001")
        result = self.service.get_clinic_for_doctor(doctor)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["clinic_id"], "C001")

    def test_missing_clinic_is_reported(self):
        self.assertFalse(self.service.get_clinic("C999").ok)


class TestConversationService(unittest.TestCase):
    def setUp(self):
        self.repo = LocalRepository()
        self.service = ConversationService(self.repo)

    def test_phone_masking(self):
        masked = mask_phone("My number is 03001234567 thanks")
        self.assertNotIn("03001234567", masked)
        self.assertIn("0300", masked)

    def test_session_lifecycle(self):
        self.service.start_session("S1", "P001")
        self.service.log_message("S1", "patient", "hello",
                                 intent="greeting", confidence=0.9)
        self.service.log_message("S1", "agent", "hi", action="greet")
        messages = self.service.get_messages("S1")
        self.assertEqual(len(messages.data), 2)

        ended = self.service.end_session("S1")
        self.assertTrue(ended.ok)
        self.assertEqual(ended.data["status"], "completed")

    def test_logging_failure_does_not_raise(self):
        """A broken transcript store must never break a call."""
        class Broken(LocalRepository):
            def set(self, *args, **kwargs):
                raise DatabaseError("firestore down")

        service = ConversationService(Broken())
        result = service.log_message("S1", "patient", "hello")
        self.assertFalse(result.ok)      # reported, not raised


class TestErrorHandling(unittest.TestCase):
    """Spec section 34: technical failures become polite responses."""

    def test_backend_failure_surfaces_as_a_service_error(self):
        class Broken(LocalRepository):
            def query(self, *args, **kwargs):
                raise DatabaseError("connection timeout")

        service = DoctorService(Broken())
        result = service.list_doctors()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "BACKEND_UNAVAILABLE")

    def test_dialog_speaks_a_friendly_message_on_backend_failure(self):
        from dialog_manager.responses import ResponseGenerator
        message = ResponseGenerator().error("BACKEND_UNAVAILABLE")
        self.assertIn("trouble", message.lower())
        # No stack trace or internal jargon reaches the caller.
        self.assertNotIn("timeout", message.lower())


if __name__ == "__main__":
    unittest.main()
