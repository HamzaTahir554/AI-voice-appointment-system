"""
Shared test helpers.

The real mBERT model takes seconds to load, so the tests inject a scripted
stub detector instead. That keeps the suite fast AND makes it test the Dialog
Manager's own logic rather than the classifier's accuracy - the two are
separate concerns and deserve separate tests.
"""
from __future__ import annotations

from datetime import timedelta

from config import TIMEZONE
from dialog_manager.dialog_manager import DialogManager
from dialog_manager.intent_router import IntentRouter
from firebase.firebase_config import LocalRepository, reset_repository
from firebase.seed_data import seed


class StubIntentDetector:
    """
    Returns a scripted intent for any utterance containing a keyword.

    `script` is an ordered list of (substring, mBERT label, confidence).
    Anything unmatched comes back as a low-confidence `unclear_request`, which
    is exactly what the real model does on out-of-domain speech.
    """

    DEFAULT_SCRIPT = [
        ("assalam", "greeting", 0.97),
        ("hello", "greeting", 0.98),
        ("hi ", "greeting", 0.95),
        ("thank", "thank_you", 0.94),
        ("shukriya", "thank_you", 0.93),
        ("goodbye", "goodbye", 0.96),
        ("khuda hafiz", "goodbye", 0.95),
        ("help", "help", 0.92),
        # booking
        ("book", "book_appointment", 0.95),
        ("appointment leni", "book_appointment", 0.95),
        ("appointment chahiye", "book_appointment", 0.94),
        ("jana hai", "book_appointment", 0.90),
        ("want an appointment", "book_appointment", 0.93),
        ("see dr", "book_appointment", 0.92),
        # cancelling
        ("cancel", "cancel_appointment", 0.94),
        ("don't want my appointment", "cancel_appointment", 0.90),
        # rescheduling
        ("reschedule", "reschedule_appointment", 0.93),
        ("change my appointment", "reschedule_appointment", 0.92),
        # checking
        ("when is my appointment", "appointment_status", 0.93),
        ("appointment kab", "appointment_status", 0.92),
        # availability + information
        ("available", "check_availability", 0.91),
        ("fee", "doctor_fee", 0.93),
        ("fees", "doctor_fee", 0.93),
        ("qualification", "doctor_qualifications", 0.92),
        ("address", "clinic_location", 0.92),
        ("kahan hai", "clinic_location", 0.90),
        # emergency
        ("emergency", "emergency", 0.95),
        ("collapsed", "emergency", 0.93),
    ]

    def __init__(self, script=None):
        self.script = script if script is not None else list(self.DEFAULT_SCRIPT)
        self.calls: list[str] = []

    def predict(self, text: str) -> dict:
        self.calls.append(text)
        lowered = f" {text.lower()} "
        for needle, intent, confidence in self.script:
            if needle.lower() in lowered:
                return {"intent": intent, "confidence": confidence}
        # Unrecognised: low confidence, exactly like the real model.
        return {"intent": "unclear_request", "confidence": 0.22}


def build_manager(script=None, seed_db: bool = True):
    """A DialogManager wired to a fresh in-memory database and a stub model."""
    repo = LocalRepository()
    reset_repository(repo)
    if seed_db:
        seed(repo)
    detector = StubIntentDetector(script)
    manager = DialogManager(
        intent_router=IntentRouter(detector=detector),
        repository=repo,
        deterministic_responses=True)
    return manager, repo, detector


def tomorrow_iso() -> str:
    from datetime import datetime
    return (datetime.now(TIMEZONE).date() + timedelta(days=1)).isoformat()


def days_ahead_iso(days: int) -> str:
    from datetime import datetime
    return (datetime.now(TIMEZONE).date() + timedelta(days=days)).isoformat()


def next_weekday_iso(weekday: int) -> str:
    """Next occurrence of a weekday (0=Monday), today included."""
    from datetime import datetime
    today = datetime.now(TIMEZONE).date()
    return (today + timedelta(days=(weekday - today.weekday()) % 7)).isoformat()
