"""
Keyterm prompting: the words this clinic's callers are most likely to say
that a general speech model is most likely to get wrong - its doctors' names,
their specialisations, the clinic's name - plus the core appointment words.

Built from the live data every time a call starts, so a doctor the
administrator adds today is recognised on the next call; nothing here is a
fixed list of names.

ElevenLabs limits realtime keyterms to 50 terms of up to 20 characters, and
charges extra for them, which is why config.ELEVENLABS_STT_KEYTERMS switches
them off by default.
"""
from __future__ import annotations

import logging

from config import Collections, CLINIC_ID

logger = logging.getLogger("speech.keyterms")

MAX_TERMS = 50
MAX_CHARS = 20
# Appointment vocabulary callers mix into Urdu, in the order worth keeping.
BASE_TERMS = ["appointment", "cancel", "reschedule", "available", "fee",
              "schedule", "Dr"]
_TITLES = ("dr.", "dr", "doctor", "prof.", "prof")


def _clean_name(name: str) -> str:
    words = str(name or "").split()
    while words and words[0].lower() in _TITLES:
        words = words[1:]
    return " ".join(words)


def build_keyterms(doctors: list[dict], clinic: dict | None = None,
                   limit: int = MAX_TERMS, max_chars: int = MAX_CHARS) -> list[str]:
    """Doctor names first (they matter most), then specialisations, the
    clinic, and the base vocabulary; de-duplicated, within ElevenLabs' limits."""
    candidates: list[str] = []
    active = [d for d in doctors if d.get("active", True) and not d.get("archived")]
    for doctor in active:
        name = _clean_name(doctor.get("name", ""))
        if name:
            candidates.append(name)
            candidates.extend(name.split())             # "Ahmed" alone, too
    for doctor in active:
        if doctor.get("specialization"):
            candidates.append(str(doctor["specialization"]))
    if clinic and clinic.get("name"):
        candidates.append(str(clinic["name"]))
    candidates.extend(BASE_TERMS)

    seen: set[str] = set()
    terms: list[str] = []
    for term in candidates:
        term = " ".join(term.split())
        if len(term) < 2 or len(term) > max_chars:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def keyterms_from_repository(repository) -> list[str]:
    """The current doctors and clinic, read when a call starts."""
    try:
        doctors = repository.query(Collections.DOCTORS)
        clinic = repository.get(Collections.CLINICS, CLINIC_ID)
    except Exception as exc:                            # pragma: no cover
        logger.warning("keyterms unavailable: %s", exc)
        return list(BASE_TERMS)
    return build_keyterms(doctors, clinic)
