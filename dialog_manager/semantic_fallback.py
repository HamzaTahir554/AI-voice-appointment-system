"""
Semantic corroboration for low-confidence intents (spec sections 8-10).

The reported failure:

    "Mje docter ke pas jana h"  ->  change_doctor (0.41)  ->  unknown_intent

The classifier was unsure, so the caller was asked to repeat themselves - even
though the sentence contains unmistakable appointment-seeking language. This
module reads that language and, when the evidence is unambiguous, overrides a
LOW or MEDIUM confidence guess.

WHAT THIS IS NOT
----------------
It is not `if "doctor" in text: book_appointment`. That would swallow "doctor
ki fee kya hai", "doctor available hain" and "doctor change karna hai". The
rule needs THREE things to fire:

    1. a subject   - doctor / dr / appointment / time
    2. an action   - jana / milna / dikhana / check / chahiye / laga dein
    3. no blocker  - no fee, address, availability, change, cancel or
                     reschedule vocabulary anywhere in the sentence,
                     and no sign of an emergency

Any blocker present and the module declines, leaving the classifier's answer
alone. It also never touches a HIGH-confidence prediction and never intercepts
an emergency.
"""
from __future__ import annotations

import re
import unicodedata

from config import Intent

# --------------------------------------------------------------------------
# 1. Subject: what the sentence is about
# --------------------------------------------------------------------------
_SUBJECT = re.compile(
    r"\b(doctor|docter|dokter|daktar|dactar|dr|dctr|drs|"
    r"appointment|apointment|appointmnt|apoinment|"
    r"time|waqt|slot|checkup|check up)\b|ڈاکٹر|اپائنٹمنٹ|وقت", re.I)

# --------------------------------------------------------------------------
# 2. Action: wanting to go / be seen / be given a time
# --------------------------------------------------------------------------
_ACTION = re.compile(
    r"\b(jana|jaana|jane|ana|aana|milna|milne|milni|"
    r"dikhana|dikhane|dikhna|dikhwana|"
    r"karwana|karana|karwani|checkup|"
    r"chahiye|chahie|chaiye|chahyi|"
    r"leni|lena|lene|"
    r"laga|lagwana|lagana|"
    r"book|banwani|banana|"
    r"visit|see|consult)\b|جانا|ملنا|دکھانا|چاہیے|لینا|لگا", re.I)

# --------------------------------------------------------------------------
# 3. Blockers: vocabulary that belongs to a DIFFERENT intent
#
# Each of these is the giveaway word of another intent, so its presence means
# the sentence is not a plain booking request - whatever else it contains.
# --------------------------------------------------------------------------
_BLOCKERS = {
    # Checked FIRST and never negotiable. "doctor ke paas jana hai" is booking
    # language, but "seene mein dard hai, doctor ke paas jana hai" is a caller
    # in danger. The classifier can mislabel that; this layer must never
    # compound the mistake by turning it into a routine appointment.
    "emergency": re.compile(
        r"\b(seena|seene|seeney|chest|saans|sans|breath|breathe|breathing|"
        r"behosh|behoshi|unconscious|faint|fainted|khoon|bleeding|accident|"
        r"daura|attack|stroke|emergency|emergancy|urgent|urgently|foran|"
        r"fauran|turant|zakhmi|injured|zeher|poison|ambulance)\b"
        r"|سینے|سانس|بے ?ہوش|خون|ایمرجنسی|فوری|دورہ", re.I),
    "fee": re.compile(r"\b(fee|fees|charge|charges|kitna|kitni|paisay|paise|"
                      r"rupay|rupee|cost|price)\b|فیس|کتنے|کتنی", re.I),
    "qualification": re.compile(
        r"\b(qualification|qualifications|degree|parhai|parhayi|taleem|"
        r"education|mbbs|fcps|tajurba|experience)\b|قابلیت|ڈگری|تجربہ", re.I),
    "location": re.compile(r"\b(address|pata|kahan|kahaan|location|kidhar|"
                           r"map|area)\b|پتہ|کہاں", re.I),
    # "clinic mein honge?" is an availability question, not a booking - it asks
    # whether the doctor WILL BE somewhere, rather than asking to be seen.
    "availability": re.compile(
        r"\b(available|availability|dastyab|khali|free|khulta|khulti|band|"
        r"timing|timings|auqat|chutti|leave|honge|hongi|hongay)\b"
        r"|\bclinic\s+(?:mein|main|me)\b|دستیاب|خالی|اوقات", re.I),
    "cancel": re.compile(r"\b(cancel|cancle|cancl|mansookh|khatam|hata|"
                         r"nikal)\b|منسوخ", re.I),
    "reschedule": re.compile(r"\b(reschedule|resedule|postpone|shift|"
                             r"aage|barha)\b|ملتوی", re.I),
    "change_doctor": re.compile(r"\b(badal|badalna|badlna|change|tabdeel|"
                                r"jagah|switch|replace|doosre|dusre|"
                                r"kisi aur)\b|بدل|تبدیل|جگہ", re.I),
    "specialization": re.compile(r"\b(specialist|specialization|speciality|"
                                 r"specialty|maahir)\b|ماہر", re.I),
    "status": re.compile(r"\b(kab hai|kab h|kab thi|confirm hai|status|"
                         r"meri appointment kab)\b|کب ہے", re.I),
}


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def appointment_evidence(text: str) -> dict:
    """
    What the sentence says about wanting an appointment.

    Returned so the decision is inspectable in logs and tests, rather than a
    bare boolean nobody can debug.
    """
    normalized = _normalize(text)
    blockers = [name for name, pattern in _BLOCKERS.items()
                if pattern.search(normalized)]
    subject = bool(_SUBJECT.search(normalized))
    action = bool(_ACTION.search(normalized))
    return {
        "subject": subject,
        "action": action,
        "blockers": blockers,
        # All three conditions, never any one alone.
        "supports_booking": subject and action and not blockers,
    }


# --------------------------------------------------------------------------
# Emergency safety net
#
# Each pattern is a PHRASE describing something happening now, never a single
# ambiguous word: "khoon" alone is also a blood test, "dard" alone is a routine
# complaint. A routine marker (test, report, checkup, an old / long-standing
# complaint) stands the net down, so "seene mein purana dard hai, checkup
# karwana hai" is still a booking.
# --------------------------------------------------------------------------
_ACUTE = re.compile(
    r"(saans\s+(nahi|nahin|nhi|ruk|band|phool|nahi\s+le)|cannot\s+breathe|can\s*not\s+breathe|"
    r"not\s+breathing|dum\s+ghut|behosh|be\s+hosh|unconscious|fainted|collapsed|"
    r"khoon\s+(beh|bah|nahi\s+ruk|ruk\s+nahi)|bleeding|heart\s+attack|dil\s+ka\s+daura|"
    r"daura\s+par|jhatke|seizure|zeher|poison|accident|critical\s+condition|"
    r"halat\s+(bohat\s+|bohot\s+)?(kharab|nazuk|bigar)|"
    r"seen[ae]y?\s+(mein|me|main)\s+(shadeed\s+|bohat\s+|bohot\s+|tez\s+)?dard|chest\s+pain|"
    r"\bemergency\b|\bemergancy\b)"
    r"|\u0627\u06cc\u0645\u0631\u062c\u0646\u0633\u06cc"          # emergency
    r"|\u0633\u0627\u0646\u0633\s+\u0646\u06c1\u06cc\u06ba"      # saans nahin
    r"|\u0628\u06d2\s*\u06c1\u0648\u0634"                          # behosh
    r"|\u062e\u0648\u0646\s+\u0628\u06c1"                          # khoon beh
    r"|\u0633\u06cc\u0646\u06d2\s+\u0645\u06cc\u06ba\s+(\u0634\u062f\u06cc\u062f\s+)?\u062f\u0631\u062f"  # seene mein (shadeed) dard
    r"|\u062f\u0644\s+\u06a9\u0627\s+\u062f\u0648\u0631\u06c1"  # dil ka daura
    r"|\u062d\u0627\u062f\u062b\u06c1"                              # haadsa
    r"|\u0632\u06c1\u0631"                                            # zehar
    r"|\u062c\u06be\u0679\u06a9\u06d2",                             # jhatke
    re.I)
_ROUTINE = re.compile(
    r"\b(test|tests|report|reports|checkup|check\s+up|purana|purani|pichle|kai\s+din|"
    r"mahine|saal\s+se|history|last\s+year|old)\b|\u0679\u06cc\u0633\u0679|\u0631\u067e\u0648\u0631\u0679",
    re.I)


def emergency_evidence(text: str) -> bool:
    """An acute danger sign, not framed as a routine visit or an old complaint."""
    normalized = _normalize(text)
    return bool(_ACUTE.search(normalized)) and not _ROUTINE.search(normalized)


def escalate_emergency(text: str, intent_result) -> str | None:
    """
    Return `emergency` when the words describe an acute danger the classifier
    did not label as one, else None.

    Unlike corroborate_booking this ignores the confidence band on purpose: a
    confident WRONG answer ("Bohot khoon beh raha hai" -> thank_you 0.97) is
    exactly the failure it exists for, and escalating costs a caller a short
    safety message, while missing costs far more. It never adds a
    confirmation question.
    """
    if intent_result is None or intent_result.intent == Intent.EMERGENCY:
        return None
    return Intent.EMERGENCY if emergency_evidence(text) else None


def corroborate_booking(text: str, intent_result) -> str | None:
    """
    Return `book_appointment` when the words plainly say so and the classifier
    was unsure. Otherwise None, leaving the classifier's answer untouched.

    Deliberately conservative:
      * a HIGH-confidence prediction is never overridden - the bands stay in
        charge, exactly as the existing policy requires;
      * an emergency is never intercepted, at any confidence;
      * a prediction that is ALREADY book_appointment is left alone, so this
        cannot inflate confidence in something already correct.
    """
    if intent_result is None:
        return None
    if intent_result.band == "high":
        return None
    if intent_result.intent == Intent.EMERGENCY:
        return None                      # never delay an emergency
    if intent_result.intent == Intent.BOOK_APPOINTMENT:
        return None                      # already right

    evidence = appointment_evidence(text)
    return Intent.BOOK_APPOINTMENT if evidence["supports_booking"] else None
