"""
The Roman-Urdu challenge set - evaluation only, never training.

These are the phrases the requirements name as the acceptance test for the
"Mje docter ke pas jana h" fix, plus the look-alikes that must NOT become
bookings. They are an honest measure only if the model has never seen them, so
`generate_data.py` drops every generated sentence that matches one.

The first retrain did not have this filter. Template expansion happened to
produce 8 of the 15 booking phrases word for word (6 landed in train), which
would have inflated the score. Matching is done on a normalised form so a
trailing "?" or different capitalisation cannot slip one back in.
"""
from __future__ import annotations

import re

BOOKING = [
    "Mje docter ke pas jana h",
    "mje doctor ke pas jana h",
    "mujhe doctor ke paas jana hai",
    "mje dr ke pas jana h",
    "doctor ke pas jana hai",
    "docter ko dikhana h",
    "mje doctor ko dikhana h",
    "mujhe doctor se milna hai",
    "doctor se milna h",
    "mje doctor se check karwana h",
    "mujhe doctor ka time chahiye",
    "doctor ki appointment chahiye",
    "mje appointment chahiye",
    "appointment book karni h",
    "mera time laga dein",
]

# (text, acceptable raw labels) - same subject words, different intent.
LOOKALIKES = [
    ("doctor ki fee kitni hai?", {"doctor_fee"}),
    ("mje doctor ki fee batao", {"doctor_fee"}),
    ("doctor ki qualification kya hai?", {"doctor_qualifications", "doctor_information"}),
    ("mje doctor ki degree batao", {"doctor_qualifications", "doctor_information"}),
    ("doctor available hain?", {"check_availability"}),
    ("dr ahmed kal clinic mein honge?", {"check_availability", "doctor_unavailable"}),
    ("doctor ka address kya hai?", {"clinic_location"}),
    ("doctor change karna hai", {"change_doctor"}),
    ("mje doctor badalna hai", {"change_doctor"}),
    ("dr ahmed ki jagah dr ali chahiye", {"change_doctor"}),
    ("appointment cancel karni hai", {"cancel_appointment"}),
    ("mera appointment reschedule kar dein",
     {"reschedule_appointment", "change_date", "change_time"}),
    ("meri appointment kab hai", {"appointment_status"}),
    ("mujhe abhi doctor ke paas jana hai seene mein dard hai", {"emergency"}),
]


def normalise(text: str) -> str:
    """Lower-case, drop punctuation, collapse whitespace."""
    text = re.sub(r"[^\w\s]", " ", str(text).lower())
    return " ".join(text.split())


def _audit_texts() -> list[str]:
    """The system-audit sets are held out the same way (see audit_set.py)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("audit_set.py")
    if not path.exists():
        return []
    spec = importlib.util.spec_from_file_location("_audit_set", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.all_texts()


_HELD_OUT = ({normalise(t) for t in BOOKING}
             | {normalise(t) for t, _ in LOOKALIKES}
             | {normalise(t) for t in _audit_texts()})


def is_challenge(text: str) -> bool:
    """True when `text` is one of the held-out evaluation phrases."""
    return normalise(text) in _HELD_OUT
