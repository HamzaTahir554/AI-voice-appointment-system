"""
Data-safety regressions: nothing may silently delete real appointments.

Two bugs found on this project, both of which destroyed real bookings in the
Firestore project whenever credentials were configured:

  * `python voice_pipeline.py --interactive` implied `--fresh` and deleted
    every appointment at start-up (15+ real test bookings were lost);
  * `python demo.py` reset the whole appointment diary between its scenarios.

Both now default to keeping data; these tests pin that. They run against the
in-memory repository only.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

from config import Collections, Status
from firebase.firebase_config import LocalRepository, reset_repository
from firebase.seed_data import seed

ROOT = Path(__file__).resolve().parents[1]


def keep_me(repo):
    repo.set(Collections.APPOINTMENTS, "APTKEEP01", {
        "appointment_id": "APTKEEP01", "patient_id": "P001", "doctor_id": "D002",
        "clinic_id": "C001", "date": "2099-01-05", "time": "16:00",
        "status": Status.CONFIRMED})


class TestVoicePipelineStartup(unittest.TestCase):
    def setUp(self):
        self.repo = LocalRepository()
        reset_repository(self.repo)
        seed(self.repo, with_sample_appointment=False)
        keep_me(self.repo)

    def test_interactive_start_keeps_existing_appointments(self):
        import voice_pipeline
        voice_pipeline._prepare(fresh=False, sample_appointment=False)
        self.assertIsNotNone(self.repo.get(Collections.APPOINTMENTS, "APTKEEP01"))

    def test_interactive_start_does_not_plant_the_demo_appointment(self):
        import voice_pipeline
        voice_pipeline._prepare(fresh=False, sample_appointment=False)
        self.assertIsNone(self.repo.get(Collections.APPOINTMENTS, "APT123"))

    def test_only_an_explicit_fresh_start_clears_the_diary(self):
        import voice_pipeline
        voice_pipeline._prepare(fresh=True, sample_appointment=False)
        self.assertEqual(self.repo.query(Collections.APPOINTMENTS), [])


class TestDemoUsesMemoryByDefault(unittest.TestCase):
    def test_demo_without_flags_never_selects_firestore(self):
        spec = importlib.util.spec_from_file_location("_demo_script", ROOT / "scripts" / "demo.py")
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)

        chosen = {}
        real_init = demo.init_repository

        def spy(force_local=False):
            chosen["force_local"] = force_local
            repo = LocalRepository()
            reset_repository(repo)
            return repo

        demo.init_repository = spy
        argv, sys.argv = sys.argv, ["demo.py", "--stub"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                demo.main()
        finally:
            sys.argv = argv
            demo.init_repository = real_init
        self.assertTrue(chosen.get("force_local"),
                        "demo.py must use the in-memory store unless --firestore is given")


if __name__ == "__main__":
    unittest.main()
