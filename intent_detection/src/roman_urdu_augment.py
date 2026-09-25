"""
Controlled Roman-Urdu augmentation.

The reported failure:

    "Mje docter ke pas jana h"  ->  change_doctor (0.41)  ->  unknown_intent

The meaning is plain to any Pakistani speaker - "I want to go to the doctor" -
but every training example spelled it "mujhe", "doctor", "ke paas", "jana hai".
The model learned the spelling, not the meaning.

This module expands a small set of MEANING patterns across the ways people
actually type Roman Urdu: dropped vowels ("mje", "docter"), dropped final
vowels ("h" for "hai"), and inconsistent spacing ("k pas" / "ke paas").

Two rules keep it honest:

  * Only linguistically plausible forms. Nobody writes "mjjhe" or "doktr ka
    pass", so those are not generated - noise would teach the model nothing.
  * Every generated sentence is checked against the OTHER intents' vocabulary,
    so an augmented booking phrase can never accidentally read as a fee or
    availability question.
"""
from __future__ import annotations

import random
import re

SEED = 42

# --------------------------------------------------------------------------
# The building blocks people actually vary
# --------------------------------------------------------------------------
PRONOUNS = ["mujhe", "mje", "muje", "mjhe", "mjy", "mujhy", ""]
DOCTOR = ["doctor", "docter", "dokter", "dr", "dctr", "daktar", "doctor sahab",
          "doctor sahib"]
# "to the doctor" - the connector is the most-mangled part of the phrase.
TO_DOCTOR = ["ke paas", "ke pas", "k pas", "k paas", "ke pass", "kay pas"]
# "is/was" - the final vowel is the first thing dropped when typing.
IS = ["hai", "h", "hy", "he", "hai na", "tha", "th"]

# --------------------------------------------------------------------------
# Meaning patterns. {p}=pronoun {d}=doctor {c}=connector {is}=is/was
#
# Each of these means "I want to see a doctor" and nothing else. There is no
# fee, no qualification, no availability question and no doctor-change here -
# which is exactly what makes them safe to label book_appointment.
# --------------------------------------------------------------------------
BOOK_PATTERNS = [
    # going to the doctor
    "{p} {d} {c} jana {is}",
    "{p} {d} {c} ana {is}",
    "{d} {c} jana {is}",
    "{p} {d} {c} jana chahta hoon",
    "{p} {d} {c} jana chahti hoon",
    # showing yourself to the doctor
    "{p} {d} ko dikhana {is}",
    "{d} ko dikhana {is}",
    "{p} apne aap ko {d} ko dikhana {is}",
    "{p} bachay ko {d} ko dikhana {is}",
    # meeting the doctor
    "{p} {d} se milna {is}",
    "{d} se milna {is}",
    "{p} {d} se milna chahta hoon",
    # getting checked
    "{p} {d} se check karwana {is}",
    "{p} {d} se checkup karwana {is}",
    "{d} se check karwana {is}",
    "{p} apna checkup karwana {is}",
    # asking for time
    "{p} {d} ka time chahiye",
    "{d} ka time chahiye",
    "{p} {d} ke liye time chahiye",
    "{p} {d} se milne ka time chahiye",
    "{d} se milne ka waqt chahiye",
    # asking for an appointment
    "{p} {d} ki appointment chahiye",
    "{p} appointment chahiye",
    "{p} appointment leni {is}",
    "appointment lena {is}",
    "appointment book karni {is}",
    "{p} {d} ki appointment book karni {is}",
    # "put my time down"
    "time laga dein",
    "mera time laga dein",
    "{d} {c} mera time laga dein",
    "{d} ka appointment laga dein",
    "mera number laga dein",
]

# --------------------------------------------------------------------------
# change_doctor: ONLY an explicit swap.
#
# The model put "doctor ke pas jana" at change_doctor 0.41, which says the
# boundary was never taught. These make the difference explicit: a change needs
# a replacing verb (badalna / change / ki jagah), not merely a doctor.
# --------------------------------------------------------------------------
CHANGE_DOCTOR_PATTERNS = [
    "{p} {d} badalna {is}",
    "{d} badal dein",
    "{d} change karna {is}",
    "{p} {d} change karna {is}",
    "mera {d} change kar dein",
    "{d} change kar dein",
    "Dr Ahmed ki jagah Dr Ali chahiye",
    "Dr Ahmed ki jagah kisi aur {d} se",
    "{p} kisi aur {d} se milna {is}",
    "{p} doosre {d} ke saath kar dein",
    "{d} tabdeel kar dein",
    "I want to change my doctor",
    "can I switch to another doctor",
    "please change the doctor for my appointment",
]

# --------------------------------------------------------------------------
# Anything containing these belongs to a DIFFERENT intent. A generated
# booking sentence that trips one of these is dropped rather than mislabelled.
# --------------------------------------------------------------------------
FOREIGN_MARKERS = re.compile(
    r"\b(fee|fees|charge|paisay|paise|rupay|qualification|degree|parhai|"
    r"taleem|address|pata|kahan|location|clinic|available|dastyab|khulta|"
    r"band|timing|auqat|cancel|mansookh|reschedule|badal|change|jagah|"
    r"specialist|specialization|tajurba|experience)\b", re.I)


def _clean(text: str) -> str:
    """Collapse the double spaces left by an empty pronoun."""
    return re.sub(r"\s+", " ", text).strip()


def _expand(patterns: list[str], limit: int, rng: random.Random,
            allow_foreign: bool = False) -> list[str]:
    """
    Fill a pattern set with random-but-plausible variants.

    Sampling rather than a full cross-product on purpose: 7 pronouns x 8 doctor
    forms x 6 connectors x 7 copulas is ~2400 near-identical sentences per
    pattern, which would swamp every other intent in the corpus.
    """
    seen: set[str] = set()
    attempts = 0
    while len(seen) < limit and attempts < limit * 40:
        attempts += 1
        pattern = rng.choice(patterns)
        text = _clean(pattern.format(
            p=rng.choice(PRONOUNS),
            d=rng.choice(DOCTOR),
            c=rng.choice(TO_DOCTOR),
            **{"is": rng.choice(IS)},
        ))
        if not allow_foreign and FOREIGN_MARKERS.search(text):
            continue                       # would read as another intent
        if len(text.split()) < 2:
            continue
        seen.add(text)

    # Guarantee every pattern appears at least once, in its plainest form.
    for pattern in patterns:
        text = _clean(pattern.format(p="mujhe", d="doctor", c="ke paas",
                                     **{"is": "hai"}))
        if allow_foreign or not FOREIGN_MARKERS.search(text):
            seen.add(text)
    return sorted(seen)


def augmented_rows(book_limit: int = 420,
                   change_limit: int = 60) -> list[tuple[str, str]]:
    """(text, intent) pairs to append to the generated corpus."""
    rng = random.Random(SEED)
    rows: list[tuple[str, str]] = []

    for text in _expand(BOOK_PATTERNS, book_limit, rng):
        rows.append((text, "book_appointment"))
    # change_doctor legitimately contains "change"/"badal", so the foreign
    # marker check is disabled for it.
    for text in _expand(CHANGE_DOCTOR_PATTERNS, change_limit, rng,
                        allow_foreign=True):
        rows.append((text, "change_doctor"))
    return rows


if __name__ == "__main__":                      # pragma: no cover
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    rows = augmented_rows()
    by_intent: dict[str, int] = {}
    for _, intent in rows:
        by_intent[intent] = by_intent.get(intent, 0) + 1
    print(f"{len(rows)} augmented rows: {by_intent}\n")
    for text, intent in rows[:15]:
        print(f"  {intent:20s} {text}")
