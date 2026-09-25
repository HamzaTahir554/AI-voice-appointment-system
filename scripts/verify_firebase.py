"""
Check that Firebase is configured correctly and actually reachable.

    python scripts/verify_firebase.py

Reports which backend was selected and why, then - if Firestore is live -
performs a real write / read / query / delete round-trip so you know the
credentials work before relying on them in a demo.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from scripts/

from config import (
    FIREBASE_CLIENT_EMAIL,
    FIREBASE_CREDENTIALS_FILE,
    FIREBASE_PRIVATE_KEY,
    FIREBASE_PROJECT_ID,
    USE_LOCAL_DB,
    Collections,
    use_utf8_stdout,
)

TICK, CROSS, WARN = "[ OK ]", "[FAIL]", "[WARN]"


def report_config() -> bool:
    """Print what was found in .env without ever printing the key itself."""
    print("=" * 70)
    print("1. CONFIGURATION")
    print("=" * 70)

    ok = True
    if FIREBASE_CREDENTIALS_FILE:
        from pathlib import Path
        exists = Path(FIREBASE_CREDENTIALS_FILE).is_file()
        print(f"  {TICK if exists else CROSS} FIREBASE_CREDENTIALS_FILE = "
              f"{FIREBASE_CREDENTIALS_FILE}")
        if not exists:
            print("         file not found at that path")
            ok = False
        return ok

    checks = [
        ("FIREBASE_PROJECT_ID", FIREBASE_PROJECT_ID),
        ("FIREBASE_CLIENT_EMAIL", FIREBASE_CLIENT_EMAIL),
    ]
    for name, value in checks:
        if value:
            print(f"  {TICK} {name} = {value}")
        else:
            print(f"  {CROSS} {name} is empty")
            ok = False

    # Never print the key. Only prove it is present and correctly shaped.
    if not FIREBASE_PRIVATE_KEY:
        print(f"  {CROSS} FIREBASE_PRIVATE_KEY is empty")
        ok = False
    else:
        has_header = "BEGIN PRIVATE KEY" in FIREBASE_PRIVATE_KEY
        has_newlines = "\n" in FIREBASE_PRIVATE_KEY
        print(f"  {TICK if has_header else CROSS} FIREBASE_PRIVATE_KEY present "
              f"({len(FIREBASE_PRIVATE_KEY)} chars)")
        if not has_header:
            print("         missing the -----BEGIN PRIVATE KEY----- header")
            ok = False
        if not has_newlines:
            print(f"  {CROSS} the key has no real newlines - keep the \\n "
                  "escapes AND wrap the value in double quotes")
            ok = False
        else:
            print(f"  {TICK} newline escapes expanded correctly")

    if USE_LOCAL_DB:
        print(f"  {WARN} USE_LOCAL_DB is set - Firestore will be bypassed. "
              "Clear it in .env to use the real database.")
    return ok


def check_sdk() -> bool:
    print()
    print("=" * 70)
    print("2. SDK")
    print("=" * 70)
    try:
        import firebase_admin
        print(f"  {TICK} firebase-admin {firebase_admin.__version__}")
        return True
    except ImportError:
        print(f"  {CROSS} firebase-admin is not installed")
        print("         run: pip install firebase-admin")
        return False


def check_connection() -> bool:
    print()
    print("=" * 70)
    print("3. CONNECTION")
    print("=" * 70)

    from firebase.firebase_config import init_repository, reset_repository

    reset_repository(None)
    repo = init_repository()
    print(f"  backend selected: {repo.backend}")

    if repo.backend != "firestore":
        print(f"  {CROSS} fell back to the in-memory store - data will NOT "
              "persist")
        print("         fix the configuration problems above, then re-run")
        return False

    # When credentials come from a JSON file, FIREBASE_PROJECT_ID is unset;
    # read the real project id out of the initialised app instead.
    project = FIREBASE_PROJECT_ID
    if not project:
        try:
            import firebase_admin
            project = firebase_admin.get_app().project_id or "(unknown)"
        except Exception:
            project = "(unknown)"
    print(f"  {TICK} connected to Firestore project {project}")

    # Real round-trip against a throwaway document.
    print()
    print("  round-trip test:")
    doc_id = "_verify_connection"
    collection = "_healthcheck"
    try:
        repo.set(collection, doc_id, {"ok": True, "note": "safe to delete"})
        print(f"    {TICK} write")

        fetched = repo.get(collection, doc_id)
        if not fetched or fetched.get("ok") is not True:
            print(f"    {CROSS} read back wrong data: {fetched}")
            return False
        print(f"    {TICK} read")

        found = repo.query(collection, [("ok", "==", True)], limit=1)
        print(f"    {TICK} query ({len(found)} document(s))")

        repo.delete(collection, doc_id)
        print(f"    {TICK} delete (test document cleaned up)")
    except Exception as exc:
        print(f"    {CROSS} {type(exc).__name__}: {exc}")
        print()
        print("  Common causes:")
        print("    - Firestore not enabled: Firebase console -> Build -> "
              "Firestore Database -> Create database")
        print("    - the service-account key was revoked or belongs to "
              "another project")
        print("    - no network access to firestore.googleapis.com")
        return False
    return True


def report_contents() -> None:
    print()
    print("=" * 70)
    print("4. DATABASE CONTENTS")
    print("=" * 70)
    from firebase.firebase_config import get_repository

    repo = get_repository()
    names = [Collections.DOCTORS, Collections.CLINICS, Collections.SCHEDULES,
             Collections.PATIENTS, Collections.APPOINTMENTS,
             Collections.SESSIONS, Collections.MESSAGES]
    empty = True
    for name in names:
        try:
            count = len(repo.query(name))
        except Exception as exc:
            print(f"  {name:26s} error: {exc}")
            continue
        print(f"  {name:26s} {count}")
        if count:
            empty = False
    if empty:
        print()
        print("  Database is empty. Seed the demo data with:")
        print("      python -m firebase.seed_data")


def main() -> int:
    use_utf8_stdout()
    print()
    config_ok = report_config()
    sdk_ok = check_sdk()

    if not sdk_ok:
        print("\nRESULT: install firebase-admin, then re-run.")
        return 1
    if not config_ok:
        print("\nRESULT: fix the .env values above, then re-run.")
        print("        (Remember: credentials go in .env, never .env.example)")
        return 1

    if not check_connection():
        print("\nRESULT: not connected to Firestore.")
        return 1

    report_contents()
    print()
    print("=" * 70)
    print("RESULT: Firestore is configured and working.")
    print("=" * 70)
    print("Next:")
    print("    python -m firebase.seed_data      # load demo doctors/schedules")
    print("    uvicorn api.main:app --reload     # start the API")
    print("    python scripts/demo.py                    # run the conversations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
