"""
Dialog Manager behaviour: multi-turn context, slot filling, intent switching,
confirmation and the complete example conversation (spec sections 6-9, 25-26, 37).
"""
from __future__ import annotations

import unittest

from config import Action, DialogState, Intent
from tests.helpers import build_manager, tomorrow_iso


class TestMultiTurnBooking(unittest.TestCase):
    """Spec section 6: the system must not re-ask for what it already knows."""

    def test_slots_are_collected_one_turn_at_a_time(self):
        manager, _, _ = build_manager()

        first = manager.process_message("s1", "I want an appointment with Dr Ahmed")
        self.assertEqual(first["action"], Action.ASK_FOR_DATE)
        self.assertEqual(first["slots"]["doctor_id"], "D001")

        second = manager.process_message("s1", "Tomorrow")
        self.assertEqual(second["action"], Action.ASK_FOR_TIME)
        self.assertEqual(second["slots"]["date"], tomorrow_iso())
        # The doctor from turn 1 is still remembered.
        self.assertEqual(second["slots"]["doctor_id"], "D001")

        third = manager.process_message("s1", "4 PM")
        self.assertEqual(third["action"], Action.ASK_CONFIRMATION)
        self.assertEqual(third["slots"]["time"], "16:00")

    def test_never_asks_twice_for_a_known_slot(self):
        manager, _, _ = build_manager()
        result = manager.process_message(
            "s1", "Book me with Dr Ahmed tomorrow at 4 PM")
        # Everything arrived in one sentence, so we go straight to confirming.
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)

    def test_full_conversation_books_an_appointment(self):
        """The exact flow from spec section 37."""
        manager, repo, _ = build_manager()

        greet = manager.process_message("s1", "Hello")
        self.assertEqual(greet["action"], Action.GREET)

        manager.process_message("s1", "Mujhe Dr Ahmed se appointment leni hai")
        manager.process_message("s1", "Kal")
        confirm = manager.process_message("s1", "4 baje")
        self.assertEqual(confirm["action"], Action.ASK_CONFIRMATION)

        done = manager.process_message("s1", "Ji haan")
        self.assertEqual(done["action"], Action.CREATE_APPOINTMENT)
        self.assertIn("booked", done["response"].lower())

        booked = [a for a in repo.query("appointments")
                  if a["time"] == "16:00" and a["date"] == tomorrow_iso()]
        self.assertEqual(len(booked), 1)
        self.assertEqual(booked[0]["doctor_id"], "D001")


class TestContextAwareness(unittest.TestCase):
    """Spec section 9: a bare reply is interpreted against the open question."""

    def test_bare_doctor_name_fills_the_awaited_slot(self):
        manager, _, _ = build_manager()
        asked = manager.process_message("s1", "I want an appointment")
        self.assertEqual(asked["action"], Action.ASK_FOR_DOCTOR)

        # "Dr Ahmed" alone carries no intent; state supplies the meaning.
        reply = manager.process_message("s1", "Dr Ahmed")
        self.assertEqual(reply["slots"]["doctor_id"], "D001")
        self.assertEqual(reply["action"], Action.ASK_FOR_DATE)

    def test_bare_date_word_fills_the_date_slot(self):
        manager, _, _ = build_manager()
        manager.process_message("s1", "I want an appointment with Dr Ahmed")
        reply = manager.process_message("s1", "Kal")
        self.assertEqual(reply["slots"]["date"], tomorrow_iso())

    def test_bare_number_fills_the_time_slot(self):
        manager, _, _ = build_manager()
        manager.process_message("s1", "I want an appointment with Dr Ahmed")
        manager.process_message("s1", "tomorrow")
        reply = manager.process_message("s1", "4")
        self.assertEqual(reply["slots"]["time"], "16:00")


class TestIntentSwitching(unittest.TestCase):
    """Spec section 8: a confident new task abandons the old one."""

    def test_switch_from_booking_to_cancelling(self):
        manager, _, _ = build_manager()
        manager.process_message("s1", "I want to book with Dr Ahmed")
        switched = manager.process_message(
            "s1", "Actually I want to cancel my appointment")
        self.assertEqual(switched["session_state"]["intent"],
                         Intent.CANCEL_APPOINTMENT)
        # The half-finished booking draft is discarded.
        self.assertIsNone(switched["slots"]["date"])

    def test_low_confidence_does_not_switch_intent(self):
        manager, _, _ = build_manager()
        manager.process_message("s1", "I want an appointment with Dr Ahmed")
        # Unrecognised mumbling must not abandon the booking.
        reply = manager.process_message("s1", "zzz qqq vvv")
        self.assertEqual(reply["session_state"]["intent"],
                         Intent.BOOK_APPOINTMENT)


class TestConfirmation(unittest.TestCase):
    """Spec sections 25-26."""

    def _reach_confirmation(self):
        manager, repo, _ = build_manager()
        manager.process_message("s1", "Book with Dr Ahmed tomorrow at 4 PM")
        return manager, repo

    def test_nothing_is_written_before_confirmation(self):
        manager, repo = self._reach_confirmation()
        fresh = [a for a in repo.query("appointments") if a["time"] == "16:00"]
        self.assertEqual(fresh, [])

    def test_yes_books_the_appointment(self):
        manager, repo = self._reach_confirmation()
        result = manager.process_message("s1", "yes")
        self.assertEqual(result["action"], Action.CREATE_APPOINTMENT)

    def test_urdu_yes_variants_are_understood(self):
        for word in ["haan", "ji haan", "jee", "bilkul", "ٹھیک ہے", "جی ہاں"]:
            with self.subTest(word=word):
                manager, repo = self._reach_confirmation()
                result = manager.process_message("s1", word)
                self.assertEqual(result["action"], Action.CREATE_APPOINTMENT,
                                 f"{word!r} should confirm")

    def test_no_aborts_without_writing(self):
        for word in ["no", "nahi", "نہیں", "actually no", "changed my mind"]:
            with self.subTest(word=word):
                manager, repo = self._reach_confirmation()
                result = manager.process_message("s1", word)
                self.assertEqual(result["action"], Action.ABORT)
                fresh = [a for a in repo.query("appointments")
                         if a["time"] == "16:00"]
                self.assertEqual(fresh, [], f"{word!r} must not book")

    def test_unclear_reply_re_asks_rather_than_assuming(self):
        manager, _ = self._reach_confirmation()
        result = manager.process_message("s1", "hmm what")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)


class TestSessionIsolation(unittest.TestCase):
    """Spec section 7: two callers must never share state."""

    def test_two_sessions_do_not_leak(self):
        manager, _, _ = build_manager()
        manager.process_message("a", "I want an appointment with Dr Ahmed")
        other = manager.process_message("b", "I want an appointment")
        self.assertIsNone(other["slots"]["doctor_id"])
        self.assertEqual(other["action"], Action.ASK_FOR_DOCTOR)

    def test_reset_clears_the_session(self):
        manager, _, _ = build_manager()
        manager.process_message("a", "I want an appointment with Dr Ahmed")
        manager.reset_session("a")
        after = manager.process_message("a", "I want an appointment")
        self.assertIsNone(after["slots"]["doctor_id"])


class TestConversationalIntents(unittest.TestCase):
    def test_greeting(self):
        manager, _, _ = build_manager()
        result = manager.process_message("s", "Assalam o Alaikum")
        self.assertEqual(result["action"], Action.GREET)

    def test_goodbye_ends_the_conversation(self):
        manager, _, _ = build_manager()
        result = manager.process_message("s", "Goodbye")
        self.assertEqual(result["action"], Action.END_CONVERSATION)

    def test_emergency_escalates_and_never_books(self):
        manager, repo, _ = build_manager()
        before = len(repo.query("appointments"))
        result = manager.process_message("s", "My father collapsed, emergency!")
        self.assertEqual(result["action"], Action.ESCALATE)
        self.assertEqual(len(repo.query("appointments")), before)

    def test_empty_input_is_handled_gracefully(self):
        manager, _, _ = build_manager()
        result = manager.process_message("s", "   ")
        self.assertEqual(result["action"], Action.CLARIFY)
        self.assertIn("could not hear", result["response"].lower())


class TestMultilingualInput(unittest.TestCase):
    """Spec section 38: English, Urdu, Roman Urdu and mixed."""

    CASES = [
        ("I want to book an appointment", Action.ASK_FOR_DOCTOR),
        ("Mujhe doctor se appointment leni hai", Action.ASK_FOR_DOCTOR),
        ("Book me with Dr Ahmed tomorrow", Action.ASK_FOR_TIME),
        ("Kal Dr Ahmed ke paas jana hai", Action.ASK_FOR_TIME),
    ]

    def test_booking_phrasings(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                manager, _, _ = build_manager()
                result = manager.process_message("s", text)
                self.assertEqual(result["action"], expected)

    def test_urdu_script_booking(self):
        manager, _, _ = build_manager(
            script=[("اپائنٹمنٹ", "book_appointment", 0.94)])
        result = manager.process_message(
            "s", "مجھے ڈاکٹر احمد سے اپائنٹمنٹ لینی ہے")
        # The Urdu alias resolves to the same database record.
        self.assertEqual(result["slots"]["doctor_id"], "D001")


class TestConversationLogging(unittest.TestCase):
    """Spec section 33."""

    def test_messages_are_stored_for_both_speakers(self):
        manager, repo, _ = build_manager()
        manager.process_message("s1", "Hello")
        messages = repo.query("conversation_messages",
                              [("session_id", "==", "s1")])
        speakers = {m["speaker"] for m in messages}
        self.assertEqual(speakers, {"patient", "agent"})

    def test_session_document_is_created(self):
        manager, repo, _ = build_manager()
        manager.process_message("s1", "Hello")
        self.assertIsNotNone(repo.get("conversation_sessions", "s1"))

    def test_phone_numbers_are_masked_in_the_transcript(self):
        manager, repo, _ = build_manager()
        manager.process_message("s1", "My number is 03001234567")
        messages = repo.query("conversation_messages",
                              [("session_id", "==", "s1")])
        patient = [m for m in messages if m["speaker"] == "patient"][0]
        self.assertNotIn("03001234567", patient["text"])


class TestPendingIntent(unittest.TestCase):
    """
    A question parked for a missing slot must be answered once the slot
    arrives - not replaced by whatever the classifier makes of the answer.
    """

    FEE_SCRIPT = [("fees", "doctor_fee", 0.93),
                  ("dr ahmed", "doctor_information", 0.80)]
    QUAL_SCRIPT = [("qualification", "doctor_qualifications", 0.92),
                   ("dr ahmed", "doctor_information", 0.80)]

    def test_fee_question_is_resumed_after_naming_the_doctor(self):
        manager, _, _ = build_manager(self.FEE_SCRIPT)
        asked = manager.process_message("s", "Doctor ki fees kitni hai?")
        self.assertEqual(asked["action"], Action.ASK_FOR_DOCTOR)
        self.assertEqual(asked["pending_intent"], Intent.DOCTOR_INFORMATION)
        # The question wording reflects what was actually asked.
        self.assertIn("fee", asked["response"].lower())

        answered = manager.process_message("s", "Dr Ahmed")
        self.assertEqual(answered["action"], Action.PROVIDE_DOCTOR_FEE)
        self.assertIn("2000", answered["response"])
        self.assertIsNone(answered["pending_intent"])

    def test_qualification_question_is_resumed(self):
        manager, _, _ = build_manager(self.QUAL_SCRIPT)
        manager.process_message("s", "Doctor ke qualification kya hain?")
        answered = manager.process_message("s", "Dr Ahmed")
        self.assertEqual(answered["action"],
                         Action.PROVIDE_DOCTOR_QUALIFICATION)
        self.assertIn("MBBS", answered["response"])

    def test_topic_switch_discards_the_parked_question(self):
        """Changing subject must not later answer the abandoned question."""
        manager, _, _ = build_manager(
            [("fees", "doctor_fee", 0.93),
             ("appointment leni", "book_appointment", 0.95)])
        manager.process_message("s", "Doctor ki fees kitni hai?")
        switched = manager.process_message("s", "Mujhe appointment leni hai")
        self.assertEqual(switched["state"], DialogState.APPOINTMENT_BOOKING)
        self.assertIsNone(switched["pending_intent"])

    def test_doctor_already_known_answers_immediately(self):
        """Spec: never ask for a slot that is already filled."""
        manager, _, _ = build_manager(self.FEE_SCRIPT)
        result = manager.process_message("s", "Dr Ahmed ki fees kitni hai?")
        self.assertEqual(result["action"], Action.PROVIDE_DOCTOR_FEE)
        self.assertIn("2000", result["response"])


class TestDialogStateTracking(unittest.TestCase):
    """The named state reported to the voice layer."""

    def test_state_starts_idle_and_follows_the_intent(self):
        manager, _, _ = build_manager()
        greet = manager.process_message("s", "Hello")
        self.assertEqual(greet["state"], DialogState.IDLE)

        booking = manager.process_message("s", "I want an appointment")
        self.assertEqual(booking["state"], DialogState.APPOINTMENT_BOOKING)

    def test_confirmation_has_its_own_state(self):
        manager, _, _ = build_manager()
        result = manager.process_message(
            "s", "Book with Dr Ahmed tomorrow at 4 PM")
        self.assertEqual(result["action"], Action.ASK_CONFIRMATION)
        self.assertEqual(result["state"], DialogState.APPOINTMENT_CONFIRMATION)

    def test_cancel_and_information_states(self):
        manager, _, _ = build_manager()
        cancel = manager.process_message("s", "cancel my appointment")
        self.assertEqual(cancel["state"], DialogState.APPOINTMENT_CANCEL)

        # A question asked with no task open reports the information state.
        info = manager.process_message("other", "What is Dr Ahmed's fee?")
        self.assertEqual(info["state"], DialogState.DOCTOR_INFORMATION)

    def test_a_question_during_a_task_resumes_the_task(self):
        """
        Updated by the system audit. This test used to ask the fee question in
        the SAME session as the cancellation and expect DOCTOR_INFORMATION -
        which encoded the old behaviour of silently dropping the cancellation.
        The question is now answered and the cancellation picked back up.
        """
        manager, _, _ = build_manager()
        manager.process_message("s", "cancel my appointment")
        info = manager.process_message("s", "What is Dr Ahmed's fee?")
        self.assertIn("fee", info["response"].lower())
        self.assertEqual(info["state"], DialogState.APPOINTMENT_CANCEL)
        self.assertEqual(info["session_state"]["intent"], Intent.CANCEL_APPOINTMENT)


class TestNoDeadEnds(unittest.TestCase):
    """
    Two traps from a live interactive session: a doctor's name given
    mid-booking was read as change_doctor and turned the booking into a
    reschedule, and "nahi" to the appointment-ID question was re-asked forever.
    """

    def test_doctor_name_read_as_change_doctor_stays_in_the_booking(self):
        from tests.helpers import StubIntentDetector
        script = ([("dr ahmad", "change_doctor", 0.87)]
                  + StubIntentDetector.DEFAULT_SCRIPT)
        manager, _, _ = build_manager(script)
        asked = manager.process_message("s1", "I want an appointment", "P001")
        self.assertEqual(asked["action"], Action.ASK_FOR_DOCTOR)

        reply = manager.process_message("s1", "dr ahmad", "P001")
        self.assertEqual(reply["session_state"]["intent"],
                         Intent.BOOK_APPOINTMENT)
        self.assertEqual(reply["slots"]["doctor_id"], "D001")
        self.assertEqual(reply["action"], Action.ASK_FOR_DATE)

    def test_a_real_reschedule_request_still_switches(self):
        """Only the one-detail edit labels are absorbed, not a reschedule."""
        manager, _, _ = build_manager()
        manager.process_message("s1", "I want an appointment with Dr Ahmed", "P001")
        switched = manager.process_message(
            "s1", "I want to reschedule my appointment", "P001")
        self.assertEqual(switched["session_state"]["intent"],
                         Intent.RESCHEDULE_APPOINTMENT)

    def test_no_to_the_id_question_ends_the_loop(self):
        from tests.helpers import StubIntentDetector
        # The stub matches substrings, and "nahi" contains "hi " (greeting).
        # The real model reads it as deny (0.98), so script exactly that.
        script = ([("nahi", "deny", 0.98)]
                  + StubIntentDetector.DEFAULT_SCRIPT)
        manager, repo, _ = build_manager(script)
        repo.delete("appointments", "APT123")      # this caller has no bookings
        asked = manager.process_message("s1", "cancel my appointment", "P001")
        self.assertEqual(asked["action"], Action.ASK_FOR_APPOINTMENT_ID)

        reply = manager.process_message("s1", "nahi", "P001")
        self.assertNotEqual(reply["action"], Action.ASK_FOR_APPOINTMENT_ID)
        self.assertIn("could not find any upcoming appointments",
                      reply["response"].lower())
        self.assertIsNone(reply["session_state"]["intent"])


if __name__ == "__main__":
    unittest.main()
