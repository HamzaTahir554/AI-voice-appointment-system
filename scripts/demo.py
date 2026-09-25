"""
Run the example conversations end to end, against the real mBERT model.

    python scripts/demo.py                 # scripted conversations
    python scripts/demo.py --interactive   # type your own turns
    python scripts/demo.py --stub          # no mBERT, uses a scripted stub (fast)

Uses the in-memory database unless Firebase is configured in `.env`, so it
works on a fresh checkout with nothing set up.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from scripts/

from config import use_utf8_stdout
from dialog_manager.dialog_manager import DialogManager
from dialog_manager.intent_router import IntentRouter
from firebase.firebase_config import init_repository
from firebase.seed_data import reset_appointments, seed

# --------------------------------------------------------------------------
CONVERSATIONS = {
    "Booking (spec section 37)": [
        "Assalam o Alaikum",
        "Mujhe Dr Ahmed se appointment leni hai",
        "Kal",
        "4 baje",
        "Ji haan",
    ],
    "Slot taken -> alternatives offered": [
        "I want to book with Dr Ahmed tomorrow at 6 PM",
        "4:20 PM",
        "yes",
    ],
    "Cancellation": [
        "I want to cancel my appointment",
        "APT123",
        "haan",
    ],
    "Rescheduling": [
        "I want to reschedule my appointment",
        "APT123",
        "Friday",
        "5 PM",
        "yes",
    ],
    "Checking an appointment": [
        "When is my appointment?",
        "APT123",
    ],
    "Doctor information (from the database)": [
        "What is Dr Ahmed's fee?",
        "Where is the clinic?",
    ],
    "Intent switch mid-booking": [
        "I want to book an appointment with Dr Ahmed",
        "Actually, cancel my existing appointment instead",
    ],
    "Low confidence -> clarify, no database write": [
        "asdfgh qwerty zxcvb",
    ],
    "Urdu script": [
        "مجھے ڈاکٹر احمد سے اپائنٹمنٹ لینی ہے",
        "کل",
        "شام پانچ بجے",
        "جی ہاں",
    ],
}


def run_conversation(manager: DialogManager, title: str, turns: list[str],
                     session_id: str, patient_id: str | None = "P001") -> None:
    print("=" * 78)
    print(title)
    print("=" * 78)
    for turn in turns:
        result = manager.process_message(session_id, turn, patient_id)
        print(f"  PATIENT : {turn}")
        print(f"    mBERT : {result['raw_intent']} "
              f"({result['confidence']:.2f}) -> {result['intent']}")
        print(f"    AGENT : {result['response']}")
        print(f"    action: {result['action']}")
        print()


def interactive(manager: DialogManager) -> None:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print("=" * 78)
    print("INTERACTIVE MODE - type 'quit' to leave, 'reset' to start over")
    print("=" * 78)
    session = "interactive"
    while True:
        try:
            text = input("\nPATIENT > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        if not text:
            continue
        if text.lower() in ("quit", "exit"):
            return
        if text.lower() == "reset":
            manager.reset_session(session)
            print("  (session reset)")
            continue
        result = manager.process_message(session, text, "P001")
        print(f"  AGENT   : {result['response']}")
        print(f"  [{result['raw_intent']} {result['confidence']:.2f} "
              f"-> {result['intent']} | {result['action']}]")
        filled = {k: v for k, v in result["slots"].items() if v}
        if filled:
            print(f"  slots   : {filled}")


def main() -> None:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description="Dialog Manager demo")
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--stub", action="store_true",
                        help="use a scripted stub instead of loading mBERT")
    parser.add_argument("--firestore", action="store_true",
                        help="use the REAL Firestore project - every appointment "
                             "in it is reset between scenarios")
    args = parser.parse_args()

    # In-memory by default. The scenarios reset the appointment diary between
    # runs, which on Firestore deletes every real booking - the system audit
    # found the demo doing exactly that whenever credentials were configured.
    repo = init_repository(force_local=not args.firestore)
    if args.firestore:
        print("WARNING: --firestore resets ALL appointments in the real database.\n")
    seed(repo)
    print(f"database backend: {repo.backend}\n")

    router = None
    if args.stub:
        from tests.helpers import StubIntentDetector
        router = IntentRouter(detector=StubIntentDetector())

    manager = DialogManager(intent_router=router, repository=repo,
                            deterministic_responses=True)

    if args.interactive:
        interactive(manager)
        return

    for index, (title, turns) in enumerate(CONVERSATIONS.items()):
        # Reset only the appointments between scenarios. Firestore persists,
        # so without this the booking demo would find 4 PM already taken by
        # the previous run, and the reschedule demo would hit the appointment
        # the cancellation demo just cancelled.
        reset_appointments(repo)
        run_conversation(manager, title, turns, session_id=f"demo{index}")


if __name__ == "__main__":
    main()
