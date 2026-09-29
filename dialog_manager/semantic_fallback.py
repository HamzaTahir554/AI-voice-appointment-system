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


# --------------------------------------------------------------------------
# Known confusions
#
# Three places where the classifier is confidently wrong on the way callers
# actually speak, found by running the voice layer's test sentences through
# mBERT (docs/VOICE.md, "Language checks"):
#
#   "Allah Hafiz"                                -> thank_you 0.87 (the call never ends)
#   "ڈاکٹر احمد کل دستیاب ہیں؟"                    -> clinic_timing 0.97
#   "Meri appointment Friday ko 5 baje kar dein" -> book_appointment 0.66
#
# The first two are corrected here, and only when the words leave no doubt;
# any competing vocabulary and the rule declines. The third is genuinely
# ambiguous - the training data labels "Mera appointment Saturday ko rakh
# dein" a booking - so it is only evidence (`moves_own_appointment`), and the
# Dialog Manager decides with the caller's diary in hand: someone who already
# has an appointment is moving it, someone who has none is booking one.
# The classifier is not retrained, and the confidence threshold is untouched.
# --------------------------------------------------------------------------
_PUNCTUATION = re.compile(r"[.,!?;:\u061f\u06d4\u060c\"'()-]+")

_FAREWELL = re.compile(
    r"allah\s*ha[fp][ie]z|khuda\s*ha[fp][ie]z|fi\s*amanillah|good\s*bye|bye(\s*bye)?|"
    r"alvida|take\s*care"
    r"|\u0627\u0644\u0644\u06c1\s*\u062d\u0627\u0641\u0638"        # allah hafiz
    r"|\u062e\u062f\u0627\s*\u062d\u0627\u0641\u0638"              # khuda hafiz
    r"|\u0627\u0644\u0648\u062f\u0627\u0639", re.I)                  # alvida
# Words that may sit around a farewell without making it anything else.
_COURTESY = re.compile(
    r"\b(thank\s*you|thanks|thank\s*u|shukriya|shukria|shukriyah|bohat|bohot|bahut|"
    r"buhat|jazak\s*allah(\s*khair)?|ok|okay|theek\s*hai|ji|jee|acha|achha|"
    r"sir|madam|aap\s*ka|ap\s*ka|phir|milte\s*hain|and|aur)\b"
    r"|\u0634\u06a9\u0631\u06cc\u06c1|\u0628\u06c1\u062a|\u062c\u06cc"  # shukriya, bohat, ji
    r"|\u0622\u067e\s*\u06a9\u0627|\u0679\u06be\u06cc\u06a9\s*\u06c1\u06d2",  # aap ka, theek hai
    re.I)

_OWN_APPOINTMENT = re.compile(
    r"\b(meri|mera|mere|meray|hamari|humari|my|our)\s+(appointment|apointment|"
    r"appointmnt|booking)\b"
    r"|\u0645\u06cc\u0631\u06cc\s+(\u0627\u067e\u0627\u0626\u0646\u0679\u0645\u0646\u0679|\u0628\u06a9\u0646\u06af)",
    re.I)
_NEW_WHEN = re.compile(
    r"\b(kal|parson|parso|aaj|aj|today|tomorrow|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|somwar|mangal|budh|jumerat|jumma|juma|hafta|itwar|"
    r"subah|shaam|sham|dopahar|morning|evening|afternoon|agle|next)\b"
    r"|\b\d{1,2}\s*(baje|bje|am|pm)\b|\b\d{1,2}:\d{2}\b"
    r"|\u06a9\u0644|\u067e\u0631\u0633\u0648\u06ba|\u0622\u062c|\u0628\u062c\u06d2"  # kal, parson, aaj, baje
    r"|\u062c\u0645\u0639\u06c1|\u0635\u0628\u062d|\u0634\u0627\u0645",            # jumma, subah, shaam
    re.I)
_MOVE_VERB = re.compile(
    r"\b(kar\s*d[eo]in|kar\s*do|kardo|kardein|kar\s*dijiye|kar\s*den|kr\s*d[eo]|"
    r"rakh\s*d[eo]in|rakh\s*do|shift|move|change|badal\w*|postpone)\b"
    r"|\u06a9\u0631\s*\u062f\u06cc\u06ba|\u06a9\u0631\s*\u062f\u0648"          # kar dein, kar do
    r"|\u0631\u06a9\u06be\s*\u062f\u06cc\u06ba|\u0628\u062f\u0644",             # rakh dein, badal
    re.I)
_NEW_BOOKING_VERB = re.compile(
    r"\b(book|laga\w*|lagwa\w*|banwa\w*|chahiye|chahie|chaiye|leni|lena|nayi|naya|new)\b"
    r"|\u0686\u0627\u06c1\u06cc\u06d2|\u0644\u06af\u0627",                     # chahiye, laga
    re.I)

# "When is the doctor available?" is a question about hours (clinic timing),
# not about one day, so a when-word stands the availability rule down.
_WHEN_QUESTION = re.compile(r"\b(kab|when|kis\s+waqt|kitne\s+baje)\b|\u06a9\u0628", re.I)

_DOCTOR_WORD = re.compile(
    r"\b(doctor|docter|dr|daktar|doc)\b|\u0688\u0627\u06a9\u0679\u0631", re.I)
_AVAILABLE = re.compile(
    r"\bavailab\w*\b"
    r"|\u062f\u0633\u062a\u06cc\u0627\u0628"                                   # dastiyab
    r"|\u0627\u0648\u06cc\u0644\u06cc\u0628\u0644|\u0627\u06cc\u0648\u06cc\u0644\u06cc\u0628\u0644"  # available, transliterated
    r"|\u0627\u0648\u0627\u0626\u0644\u06cc\u0628\u0644",
    re.I)


def _clean(text: str) -> str:
    return " ".join(_PUNCTUATION.sub(" ", _normalize(text)).lower().split())


def correct_known_confusion(text: str, intent_result) -> str | None:
    """
    The intent a known, confident mistake should have been, else None.

    Never touches an emergency, and never changes an answer that is already
    the corrected intent.
    """
    if intent_result is None or intent_result.intent == Intent.EMERGENCY:
        return None
    cleaned = _clean(text)
    if not cleaned:
        return None

    # 1. A farewell, and nothing but courtesy around it, is goodbye - even
    #    when "thank you" is in it, and whatever the capitalisation.
    if intent_result.intent != Intent.GOODBYE and _FAREWELL.search(cleaned):
        rest = _COURTESY.sub(" ", _FAREWELL.sub(" ", cleaned))
        if not rest.strip():
            return Intent.GOODBYE

    # 2. "Is Dr Ahmed available tomorrow?" in Urdu script, or with
    #    "available" written out in Urdu letters, which the classifier reads
    #    as clinic hours or does not recognise at all. A yes/no question about
    #    a particular day - never a "when" question.
    if (intent_result.intent in (Intent.CLINIC_INFORMATION, Intent.UNKNOWN)
            and _DOCTOR_WORD.search(cleaned) and _AVAILABLE.search(cleaned)
            and _NEW_WHEN.search(cleaned)
            and not _WHEN_QUESTION.search(cleaned)
            and not _BLOCKERS["fee"].search(cleaned)
            and not _BLOCKERS["location"].search(cleaned)
            and not _BLOCKERS["cancel"].search(cleaned)):
        return Intent.DOCTOR_AVAILABILITY
    return None


def moves_own_appointment(text: str) -> bool:
    """
    "Meri appointment Friday ko 5 baje kar dein": the caller's own
    appointment, a new day or time, and a verb that puts it there - with no
    word of a NEW booking ("book", "laga", "chahiye") and none of cancelling.

    Evidence only. Whether it means move or book depends on whether the caller
    has an appointment to move, which only the Dialog Manager knows.
    """
    cleaned = _clean(text)
    return bool(cleaned and _OWN_APPOINTMENT.search(cleaned)
                and _NEW_WHEN.search(cleaned) and _MOVE_VERB.search(cleaned)
                and not _NEW_BOOKING_VERB.search(cleaned)
                and not _BLOCKERS["cancel"].search(cleaned))
