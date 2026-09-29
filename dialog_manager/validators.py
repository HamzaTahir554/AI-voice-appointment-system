"""
Entity extraction and validation.

Deliberately RULE-BASED and deterministic: for appointment booking "kal 4 baje"
must always resolve to the same date and time. A probabilistic extractor that
is right 90 % of the time would book real patients on the wrong day.

The public surface is `EntityExtractor.extract_entities(text)`, so a trained
NER model can replace the internals later without touching the Dialog Manager.

Covers English, Roman Urdu and Urdu script, including mixed sentences.
All dates resolve in Asia/Karachi, never UTC.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from config import BARE_HOUR_PM_RANGE, TIMEZONE

# --------------------------------------------------------------------------
# Digit + number vocabulary
# --------------------------------------------------------------------------
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "aik": 1, "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4,
    "panch": 5, "paanch": 5, "che": 6, "chay": 6, "cheh": 6, "saat": 7,
    "aath": 8, "nau": 9, "das": 10, "gyara": 11, "baara": 12, "bara": 12,
    "ایک": 1, "دو": 2, "تین": 3, "چار": 4, "پانچ": 5, "چھ": 6, "چھے": 6,
    "سات": 7, "آٹھ": 8, "نو": 9, "دس": 10, "گیارہ": 11, "بارہ": 12,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "peer": 0, "pir": 0, "somwar": 0, "پیر": 0,
    "tuesday": 1, "tue": 1, "mangal": 1, "منگل": 1,
    "wednesday": 2, "wed": 2, "budh": 2, "بدھ": 2,
    "thursday": 3, "thu": 3, "jumeraat": 3, "jumerat": 3, "جمعرات": 3,
    "friday": 4, "fri": 4, "jumma": 4, "juma": 4, "جمعہ": 4,
    "saturday": 5, "sat": 5, "hafta": 5, "ہفتہ": 5,
    "sunday": 6, "sun": 6, "itwar": 6, "atwar": 6, "اتوار": 6,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_TODAY_WORDS = ("today", "aaj", "aj", "آج")
# "kal"/"کل" means yesterday AND tomorrow; in booking it is always the future.
_TOMORROW_WORDS = ("tomorrow", "kal", "kl", "کل")
_DAY_AFTER_WORDS = ("day after tomorrow", "parso", "parsoon", "پرسوں")

_AM_WORDS = ("am", "a.m", "morning", "subah", "subh", "sawere", "صبح")
_PM_WORDS = ("pm", "p.m", "evening", "afternoon", "night", "shaam", "sham",
             "dopahar", "dopeher", "raat", "شام", "دوپہر", "رات")
_OCLOCK_WORDS = ("baje", "bajay", "bje", "بجے")


def normalize_digits(text: str) -> str:
    return str(text).translate(_DIGIT_MAP)


def today() -> date:
    """Today in Asia/Karachi."""
    return datetime.now(TIMEZONE).date()


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------
def is_valid_date(iso_date: str) -> bool:
    try:
        datetime.strptime(iso_date, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def is_past_date(iso_date: str) -> bool:
    if not is_valid_date(iso_date):
        return False
    return datetime.strptime(iso_date, "%Y-%m-%d").date() < today()


def is_valid_time(time_str: str) -> bool:
    if not isinstance(time_str, str) or ":" not in time_str:
        return False
    try:
        hour, minute = (int(x) for x in time_str.split(":"))
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def is_valid_appointment_id(value: str) -> bool:
    return bool(re.fullmatch(r"APT[A-Z0-9]{3,10}", str(value or "").upper()))


# --------------------------------------------------------------------------
# Yes / no detection (spec sections 25-26)
# --------------------------------------------------------------------------
# Deliberately NOT delegated to mBERT: "haan" or "ji" is a one-word reply a
# 31-class classifier can easily mis-file, and a wrong answer here books or
# cancels a real appointment. A lexicon is safer and auditable.
_YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "okey", "alright",
    "right", "correct", "confirm", "confirmed", "yes please", "go ahead",
    "definitely", "absolutely", "fine", "perfect", "book it", "do it",
    "haan", "han", "haa", "ha", "ji", "jee", "ji haan", "jee haan", "ji han",
    "bilkul", "theek", "theek hai", "thik hai", "acha", "achha", "manzoor",
    "ہاں", "جی", "جی ہاں", "بالکل", "ٹھیک", "ٹھیک ہے", "منظور", "درست",
}
_NO_WORDS = {
    "no", "nope", "nah", "naa", "never", "dont", "don't", "do not",
    "not now", "wrong", "incorrect", "cancel that", "changed my mind",
    "actually no", "no thanks", "forget it",
    "nahi", "nahin", "nai", "nhi", "na", "hargiz nahi", "ghalat", "rehne do",
    "نہیں", "نہ", "غلط", "ہرگز نہیں", "جی نہیں", "رہنے دو",
}

_WORD_BOUNDARY = r"(?<![\w؀-ۿ]){}(?![\w؀-ۿ])"


def detect_yes_no(text: str) -> str | None:
    """Return "yes", "no" or None."""
    if not text:
        return None
    cleaned = unicodedata.normalize("NFKC", text).strip().lower()
    # Urdu punctuation too: the Urdu comma and question mark sit inside the
    # Arabic-script block, so left in place they glue onto the word - a
    # transcript's "ہاں، کر دیں" was missed as a "yes" in a live voice call.
    cleaned = re.sub(r"[!.,?;:۔،؟؛]+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    if cleaned in _YES_WORDS:
        return "yes"
    if cleaned in _NO_WORDS:
        return "no"

    # Negation wins: "no that is ok" is a refusal, not an agreement.
    for word in sorted(_NO_WORDS, key=len, reverse=True):
        if re.search(_WORD_BOUNDARY.format(re.escape(word)), cleaned):
            return "no"
    for word in sorted(_YES_WORDS, key=len, reverse=True):
        if re.search(_WORD_BOUNDARY.format(re.escape(word)), cleaned):
            return "yes"
    return None


# --------------------------------------------------------------------------
# Entity extractor
# --------------------------------------------------------------------------
class EntityExtractor:
    """
    Extracts appointment entities from an utterance.

    `doctor_service` is injected rather than imported so the extractor never
    invents a doctor: it asks the database to resolve the name, keeping
    Firestore the single source of truth for who the doctors are.
    """

    def __init__(self, doctor_service=None, today_override: date | None = None):
        self.doctor_service = doctor_service
        self._today = today_override

    @property
    def today(self) -> date:
        return self._today or today()

    # ------------------------------------------------------------------
    def extract_entities(self, text: str) -> dict[str, Any]:
        """Only keys actually found are present in the result."""
        if not text or not text.strip():
            return {}

        raw = unicodedata.normalize("NFKC", text)
        norm = normalize_digits(raw)
        lower = norm.lower()
        entities: dict[str, Any] = {}

        # IDs and phone numbers first, so their digits are not later read
        # as a clock time.
        appointment_id = self._appointment_id(norm)
        if appointment_id:
            entities["appointment_id"] = appointment_id
            lower = lower.replace(appointment_id.lower(), " ")

        phone = self._phone(norm)
        if phone:
            entities["patient_phone"] = phone
            lower = re.sub(r"[\d\s\-+()]{7,}", " ", lower)

        doctor = self._doctor(raw)
        if doctor:
            entities["doctor_id"] = doctor.get("doctor_id")
            entities["doctor_name"] = doctor.get("name")
            clinic_ids = doctor.get("clinic_ids") or []
            if clinic_ids:
                entities["clinic_id"] = clinic_ids[0]

        parsed_date = self._date(lower)
        if parsed_date:
            entities["date"] = parsed_date

        parsed_time = self._time(lower)
        if parsed_time:
            entities["time"] = parsed_time

        name = self._patient_name(raw)
        if name:
            entities["patient_name"] = name
        return entities

    # ------------------------------------------------------------------
    _APT_RE = re.compile(r"\b(APT[-\s]?[A-Z0-9]{3,10})\b", re.I)

    def _appointment_id(self, text: str) -> str | None:
        match = self._APT_RE.search(text)
        if not match:
            return None
        return re.sub(r"[-\s]", "", match.group(1)).upper()

    _PHONE_RE = re.compile(r"(\+?92[\s-]?3\d{2}[\s-]?\d{7}|\b03\d{2}[\s-]?\d{7}\b)")

    def _phone(self, text: str) -> str | None:
        match = self._PHONE_RE.search(text)
        if not match:
            return None
        digits = re.sub(r"\D", "", match.group(1))
        if digits.startswith("92"):
            digits = "0" + digits[2:]
        return digits if len(digits) == 11 else None

    # ------------------------------------------------------------------
    def _doctor(self, text: str) -> dict | None:
        """Resolve a doctor mention against the database, or None."""
        if self.doctor_service is None:
            return None
        result = self.doctor_service.find_doctor(text)
        return result.data if result.ok else None

    # ------------------------------------------------------------------
    _ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
    _DMY_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
    _DAY_MONTH_RE = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]+)\b")

    def _date(self, lower: str) -> str | None:
        base = self.today

        m = self._ISO_RE.search(lower)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)),
                            int(m.group(3))).isoformat()
            except ValueError:
                return None

        m = self._DAY_MONTH_RE.search(lower)
        if m and m.group(2) in _MONTHS:
            day, month = int(m.group(1)), _MONTHS[m.group(2)]
            try:
                candidate = date(base.year, month, day)
                if candidate < base:                 # that month already passed
                    candidate = date(base.year + 1, month, day)
                return candidate.isoformat()
            except ValueError:
                return None

        # Longest phrase first: "day after tomorrow" contains "tomorrow".
        if any(w in lower for w in _DAY_AFTER_WORDS):
            return (base + timedelta(days=2)).isoformat()
        if any(re.search(_WORD_BOUNDARY.format(re.escape(w)), lower)
               for w in _TOMORROW_WORDS):
            return (base + timedelta(days=1)).isoformat()
        if any(re.search(_WORD_BOUNDARY.format(re.escape(w)), lower)
               for w in _TODAY_WORDS):
            return base.isoformat()

        for word, weekday in _WEEKDAYS.items():
            if not re.search(_WORD_BOUNDARY.format(re.escape(word)), lower):
                continue
            days_ahead = (weekday - base.weekday()) % 7
            # "next Friday" on a Friday means a week away.
            if days_ahead == 0 and ("next" in lower or "agle" in lower
                                    or "اگلے" in lower):
                days_ahead = 7
            return (base + timedelta(days=days_ahead)).isoformat()

        m = self._DMY_RE.search(lower)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else base.year
            if year < 100:
                year += 2000
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------
    _HHMM_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?")
    _HOUR_AMPM_RE = re.compile(r"\b(\d{1,2})\s*(am|pm)\b")
    _HOUR_OCLOCK_RE = re.compile(
        r"\b(\d{1,2})\s*(?:" + "|".join(_OCLOCK_WORDS) + r")")
    _WORD_OCLOCK_RE = re.compile(
        r"([a-z؀-ۿ]+)\s*(?:" + "|".join(_OCLOCK_WORDS) + r")")
    # "at 4", "around 5" - a bare hour with no "o'clock" marker.
    _BARE_HOUR_RE = re.compile(
        r"\b(?:at|around|about|by|taqreeban|tqrbn)\s+(\d{1,2})\b")

    def _time(self, lower: str) -> str | None:
        has_am = any(w in lower for w in _AM_WORDS)
        has_pm = any(w in lower for w in _PM_WORDS)

        m = self._HHMM_RE.search(lower)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            marker = m.group(3)
            is_24h = marker is None and not has_am and not has_pm and hour > 12
            hour = self._meridiem(hour, marker, has_am, has_pm, is_24h)
            return self._format(hour, minute)

        m = self._HOUR_AMPM_RE.search(lower)
        if m:
            hour = self._meridiem(int(m.group(1)), m.group(2), has_am, has_pm)
            return self._format(hour, 0)

        m = self._HOUR_OCLOCK_RE.search(lower)
        if m:
            hour = self._meridiem(int(m.group(1)), None, has_am, has_pm)
            return self._format(hour, 0)

        m = self._WORD_OCLOCK_RE.search(lower)
        if m and m.group(1) in _NUMBER_WORDS:
            hour = self._meridiem(_NUMBER_WORDS[m.group(1)], None, has_am, has_pm)
            return self._format(hour, 0)

        m = self._BARE_HOUR_RE.search(lower)
        if m:
            hour = self._meridiem(int(m.group(1)), None, has_am, has_pm)
            return self._format(hour, 0)

        # A lone number, e.g. the caller answering "what time?" with "4".
        stripped = lower.strip()
        if re.fullmatch(r"\d{1,2}", stripped):
            return self._format(
                self._meridiem(int(stripped), None, has_am, has_pm), 0)
        if stripped in _NUMBER_WORDS:
            return self._format(
                self._meridiem(_NUMBER_WORDS[stripped], None, has_am, has_pm), 0)
        return None

    @staticmethod
    def _meridiem(hour: int, marker: str | None, has_am: bool, has_pm: bool,
                  is_24h: bool = False) -> int:
        """
        Decide AM or PM for a bare hour.

        With no marker we use a clinic-hours heuristic: a patient who says
        "4 o'clock" about a doctor's surgery means 16:00, not 04:00.
        """
        if is_24h:
            return hour
        if marker == "pm" or (marker is None and has_pm):
            return hour if hour == 12 else (hour + 12) % 24
        if marker == "am" or (marker is None and has_am):
            return 0 if hour == 12 else hour
        low, high = BARE_HOUR_PM_RANGE
        return hour + 12 if low <= hour <= high else hour

    @staticmethod
    def _format(hour: int, minute: int) -> str | None:
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return f"{hour:02d}:{minute:02d}"

    # ------------------------------------------------------------------
    _NAME_RE = re.compile(
        r"(?:my name is|name is|i am|this is|mera naam|mera nam|"
        r"میرا نام)\s+([A-Za-z؀-ۿ]+(?:\s+[A-Za-z؀-ۿ]+)?)", re.I)
    _NAME_STOP = {"hai", "he", "ha", "hoon", "hun", "is", "ہے", "ہوں",
                  "calling", "dr", "doctor", "ڈاکٹر"}

    def _patient_name(self, text: str) -> str | None:
        m = self._NAME_RE.search(text)
        if not m:
            return None
        words = [w for w in m.group(1).split()
                 if w.lower() not in self._NAME_STOP]
        if not words:
            return None
        return " ".join(words).title() if words[0].isascii() else " ".join(words)
