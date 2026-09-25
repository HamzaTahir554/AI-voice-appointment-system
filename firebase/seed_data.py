"""
Seed the database with demo doctors, clinics and schedules.

    python -m firebase.seed_data

Safe to run against Firestore too - it writes fixed document IDs, so re-running
updates the same records instead of creating duplicates.
"""
from __future__ import annotations

from config import Collections, Status
from firebase.firebase_config import get_repository, init_repository
from firebase.schedule_service import WEEKDAY_NAMES, today_iso

# One clinic: every doctor works here, and its name and address are what the
# assistant reads to callers (config.CLINIC_ID).
CLINICS = [
    {"clinic_id": "C001", "name": "Ahmed Medical Clinic",
     "address": "Main Bazar Road, Sector G-10, Islamabad",
     "city": "Islamabad",
     "phone": "+925111234567", "active": True},
]

DOCTORS = [
    {"doctor_id": "D001", "name": "Dr Ahmed Khan", "specialization": "Cardiologist",
     "qualification": "MBBS, FCPS (Cardiology)", "fee": 2000,
     "experience_years": 12, "phone": "+923001234567",
     "clinic_ids": ["C001"], "active": True,
     "aliases": ["ahmed", "ahmad", "ahmed khan", "احمد", "ڈاکٹر احمد"]},
    {"doctor_id": "D002", "name": "Dr Asim Raza", "specialization": "Dermatologist",
     "qualification": "MBBS, MD (Dermatology)", "fee": 1500,
     "experience_years": 8, "phone": "+923019876543",
     "clinic_ids": ["C001"], "active": True,
     "aliases": ["asim", "aasim", "asim raza", "عاصم", "ڈاکٹر عاصم"]},
    {"doctor_id": "D003", "name": "Dr Hamza Ali", "specialization": "Child Specialist",
     "qualification": "MBBS, FCPS (Paediatrics)", "fee": 1200,
     "experience_years": 15, "phone": "+923215551234",
     "clinic_ids": ["C001"], "active": True,
     "aliases": ["hamza", "hamza ali", "حمزہ", "ڈاکٹر حمزہ"]},
    {"doctor_id": "D004", "name": "Dr Sara Malik", "specialization": "Gynecologist",
     "qualification": "MBBS, FCPS (Gynaecology)", "fee": 1800,
     "experience_years": 10, "phone": "+923337778888",
     "clinic_ids": ["C001"], "active": True,
     "aliases": ["sara", "sarah", "sara malik", "سارہ", "ڈاکٹر سارہ"]},
]


def _schedules() -> list[dict]:
    """Mon-Sat, 16:00-20:00, 20-minute slots (spec section 14)."""
    rows = []
    counter = 1
    for doctor in DOCTORS:
        clinic_id = doctor["clinic_ids"][0]
        for day in WEEKDAY_NAMES:
            if day == "Sunday":               # clinic closed
                continue
            rows.append({
                "schedule_id": f"S{counter:03d}",
                "doctor_id": doctor["doctor_id"],
                "clinic_id": clinic_id,
                "day": day,
                "start_time": "16:00",
                "end_time": "20:00",
                "slot_duration": 20,
                "active": True,
            })
            counter += 1
    return rows


PATIENTS = [
    {"patient_id": "P001", "name": "Ali Khan", "phone": "03001234567"},
]


def seed(repository=None, with_sample_appointment: bool = True) -> dict:
    """Write all demo data. Returns a count per collection."""
    repo = repository or get_repository() or init_repository()

    for clinic in CLINICS:
        repo.set(Collections.CLINICS, clinic["clinic_id"], clinic)
    for doctor in DOCTORS:
        repo.set(Collections.DOCTORS, doctor["doctor_id"], doctor)
    schedules = _schedules()
    for schedule in schedules:
        repo.set(Collections.SCHEDULES, schedule["schedule_id"], schedule)
    for patient in PATIENTS:
        repo.set(Collections.PATIENTS, patient["patient_id"], patient)

    counts = {
        "clinics": len(CLINICS),
        "doctors": len(DOCTORS),
        "schedules": len(schedules),
        "patients": len(PATIENTS),
        "appointments": 0,
    }

    if with_sample_appointment:
        # One existing booking so cancel / check / reschedule demos work.
        # Placed at 18:00 so the 16:00 booking demo stays free.
        from datetime import datetime, timedelta
        from config import TIMEZONE
        tomorrow = (datetime.now(TIMEZONE).date() + timedelta(days=1)).isoformat()
        repo.set(Collections.APPOINTMENTS, "APT123", {
            "appointment_id": "APT123", "patient_id": "P001",
            "doctor_id": "D001", "clinic_id": "C001",
            "date": tomorrow, "time": "18:00", "status": Status.CONFIRMED,
        })
        counts["appointments"] = 1
    return counts


def reset_appointments(repository=None) -> int:
    """
    Delete every appointment and restore only the demo one.

    Firestore persists between runs, so without this the demo books 16:00 on
    the first run and then fails on the second because the slot is taken.
    Rewriting only the appointments keeps the reset fast: the doctors, clinics
    and schedules are untouched.

    Returns how many appointment documents were removed.
    """
    from datetime import datetime, timedelta
    from config import TIMEZONE

    repo = repository or get_repository() or init_repository()
    existing = repo.query(Collections.APPOINTMENTS)
    for appointment in existing:
        doc_id = appointment.get("appointment_id")
        if doc_id:
            repo.delete(Collections.APPOINTMENTS, doc_id)

    tomorrow = (datetime.now(TIMEZONE).date() + timedelta(days=1)).isoformat()
    repo.set(Collections.APPOINTMENTS, "APT123", {
        "appointment_id": "APT123", "patient_id": "P001",
        "doctor_id": "D001", "clinic_id": "C001",
        "date": tomorrow, "time": "18:00", "status": Status.CONFIRMED,
    })
    return len(existing)


def main() -> None:
    from config import use_utf8_stdout
    use_utf8_stdout()
    repo = init_repository()
    counts = seed(repo)
    print("=" * 60)
    print(f"Seeded the '{repo.backend}' backend")
    print("=" * 60)
    for name, count in counts.items():
        print(f"   {name:16s} {count}")
    if repo.backend == "local":
        print("\nNOTE: the in-memory backend does not persist. Configure "
              "Firebase in .env to write real data.")


if __name__ == "__main__":
    main()
