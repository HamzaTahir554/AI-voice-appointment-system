"""
Hallucination guard (spec sections 28-29).

The system prompt asks the model not to invent facts. This module checks that
it actually didn't. Every time, date and appointment ID mentioned in the
generated sentence must appear in the backend result; a failed operation must
not be described as a success.

If validation fails, the caller uses the deterministic fallback instead. The
model gets to make the wording nicer - it never gets to change the facts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# "16:00", "4 PM", "4:20 pm", "4 baje", "۴ بجے"
# Negative lookahead: "4:20 PM" is handled by _TIME_12H below, so this must not
# also match the bare "4:20" inside it and misread it as 04:20.
_TIME_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?m\.?)", re.I)
_TIME_12H = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?\b", re.I)
# Negative lookbehind: in "shaam 4:20 baje" the minutes belong to the clock
# time matched above; without this they would also read as a separate "20 baje".
_TIME_OCLOCK = re.compile(r"(?<![:\d])(\d{1,2})\s*(?:baje|bajay|بجے)", re.I)
# Does an "o'clock" word directly follow a H:MM time?
_FOLLOWED_BY_OCLOCK = re.compile(r"\s*(?:baje|bajay|بجے)", re.I)
_APPOINTMENT_ID = re.compile(r"\b(APT[A-Z0-9]{3,10})\b", re.I)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Claims of a completed mutation.
_SUCCESS_WORDS = re.compile(
    r"\b(booked|confirmed|reserved|scheduled|cancelled|canceled|rescheduled|"
    r"moved|ho gayi|ho gaya|kar di|kar diya|confirm ho|ہو گئی|ہو گیا|"
    r"منسوخ کر دی|بک ہو)\b", re.I)
# Words that flip a success claim into a refusal, so "not booked" is not a lie.
_NEGATIONS = re.compile(
    r"\b(not|cannot|can't|could not|couldn't|unable|no longer|nahi|nahin|nhi|"
    r"نہیں)\b", re.I)
# Contexts where a success word describes the SLOT rather than a completed
# action: "that time is already booked" reports a failure, it does not claim one.
_NOT_A_CLAIM = re.compile(
    r"\b(already|pehle se|pehlay se|ho chuk|پہلے سے)\b[\s\w]*$", re.I)


@dataclass
class ValidationReport:
    valid: bool
    problems: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:      # safe here: this type means "is it valid?"
        return self.valid


# --------------------------------------------------------------------------
def _minutes(hour: int, minute: int) -> int:
    return hour * 60 + minute


def extract_times(text: str) -> list[tuple[str, set[int]]]:
    """
    Every clock time mentioned, as (surface form, candidate readings).

    A mention can be AMBIGUOUS: "4 baje" means 04:00 or 16:00, and a caller
    talking about a clinic means the afternoon. We therefore return every
    plausible reading and treat the mention as valid if ANY of them matches the
    backend - otherwise the validator would reject a perfectly correct Roman
    Urdu sentence for saying "4 baje" instead of "16:00".
    """
    mentions: list[tuple[str, set[int]]] = []

    for match in _TIME_24H.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        readings = {_minutes(hour, minute)}
        # "shaam 4:20 baje" is a clinic time said the Urdu way, so 16:20 is
        # just as valid a reading as 04:20. Without both, a correct Roman
        # Urdu sentence would be rejected as inventing a time.
        if _FOLLOWED_BY_OCLOCK.match(text[match.end():]) and 1 <= hour <= 11:
            readings.add(_minutes((hour + 12) % 24, minute))
        mentions.append((match.group(0), readings))

    for match in _TIME_12H.finditer(text):
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "p":
            hour += 12
        mentions.append((match.group(0), {_minutes(hour, minute)}))

    for match in _TIME_OCLOCK.finditer(text):
        hour = int(match.group(1))
        readings = {_minutes(hour, 0)}
        if 1 <= hour <= 11:
            readings.add(_minutes((hour + 12) % 24, 0))
        mentions.append((match.group(0), readings))
    return mentions


def allowed_times(result) -> set[int]:
    """Times the backend actually mentioned - the only ones allowed."""
    data = result.data or {}
    allowed: set[str] = set()
    for key in ("time", "requested_time", "new_time", "old_time",
                "previous_time"):
        if data.get(key):
            allowed.add(str(data[key]))
    for key in ("alternative_slots", "available_slots"):
        allowed.update(str(s) for s in (data.get(key) or []))
    for appointment in (data.get("appointments") or []):
        if appointment.get("time"):
            allowed.add(str(appointment["time"]))
    if (data.get("appointment") or {}).get("time"):
        allowed.add(str(data["appointment"]["time"]))
    for appointment in (data.get("cancelled_appointments") or []):
        if appointment.get("time"):
            allowed.add(str(appointment["time"]))

    minutes: set[int] = set()
    for value in allowed:
        try:
            hour, minute = (int(x) for x in value.split(":"))
        except (ValueError, AttributeError):
            continue
        minutes.add(_minutes(hour, minute))
    return minutes


def allowed_appointment_ids(result) -> set[str]:
    ids: set[str] = set()
    if result.appointment_id:
        ids.add(result.appointment_id.upper())
    data = result.data or {}
    if (data.get("appointment") or {}).get("appointment_id"):
        ids.add(str(data["appointment"]["appointment_id"]).upper())
    for appointment in (data.get("appointments") or []):
        if appointment.get("appointment_id"):
            ids.add(str(appointment["appointment_id"]).upper())
    for appointment in (data.get("cancelled_appointments") or []):
        if appointment.get("appointment_id"):
            ids.add(str(appointment["appointment_id"]).upper())
    if (data.get("existing_appointment") or {}).get("appointment_id"):
        ids.add(str(data["existing_appointment"]["appointment_id"]).upper())
    return ids


def allowed_dates(result) -> set[str]:
    data = result.data or {}
    dates = {str(data[k]) for k in ("date", "new_date", "old_date",
                                    "previous_date") if data.get(k)}
    for appointment in (data.get("appointments") or []):
        if appointment.get("date"):
            dates.add(str(appointment["date"]))
    if (data.get("appointment") or {}).get("date"):
        dates.add(str(data["appointment"]["date"]))
    return dates


# --------------------------------------------------------------------------
# Nobody says "17 baje" in Urdu - it is "shaam 5 baje". A small model reaches
# for the 24-hour clock it saw in the JSON, which is factually right but reads
# like a machine. Treated as a validation failure so the template is used.
# The lookbehind keeps the minutes of "shaam 4:20 baje" from reading as a
# 24-hour "20 baje" - found by the live Ollama test, where it rejected the
# deterministic fallback and every correct LLM sentence with such a time.
_UNNATURAL_24H = re.compile(r"(?<![:\d])\b(1[3-9]|2[0-3])(?::[0-5]\d)?\s*(?:baje|bajay|بجے)", re.I)


# --------------------------------------------------------------------------
# Spoken dates and reply language (added by the system audit)
#
# The live llama3.2 run produced sentences the checks above accepted: "Aapki
# appointment kal 5 baje..." for an appointment eight days away, and Roman
# Urdu replies to Urdu-script and English requests.
# --------------------------------------------------------------------------
_RELATIVE_DAY = re.compile(
    r"\b(the day after tomorrow|day after tomorrow|parso|parson|parsoon|tomorrow|kal|today|aaj)\b"
    r"|\bپرسوں\b|\bکل\b|\bآج\b", re.I)
_RELATIVE_OFFSET = {"the day after tomorrow": 2, "day after tomorrow": 2, "parso": 2, "parson": 2,
                    "parsoon": 2, "پرسوں": 2, "tomorrow": 1, "kal": 1, "کل": 1,
                    "today": 0, "aaj": 0, "آج": 0}
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + "|".join(_MONTHS) + r")\b", re.I)
_MONTH_DAY = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_DAY_TAREEKH = re.compile(r"\b(\d{1,2})\s*(?:tareekh|tarikh|تاریخ)", re.I)


def _weekday_words() -> dict[str, int]:
    from ollama_judge.language import _ROMAN_DAYS, _URDU_DAYS
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    words = {name.lower(): index for index, name in enumerate(names)}
    for index, name in enumerate(names):
        if name == "Saturday":
            continue                       # "hafta" / "ہفتہ" also means "week"
        words[_ROMAN_DAYS[name].lower()] = index
        words[_URDU_DAYS[name]] = index
    words.update({"itwaar": 6, "jumerat": 3, "jummah": 4})
    return words


def _spoken_date_problems(response: str, permitted_dates: set[str]) -> list[str]:
    """A relative day, weekday or day-of-month that is not the backend's date."""
    from datetime import date, datetime
    from config import TIMEZONE

    parsed = set()
    for value in permitted_dates:
        try:
            parsed.add(date.fromisoformat(value))
        except ValueError:
            continue
    if not parsed:
        return []            # nothing to compare with; "another day?" is fine
    today = datetime.now(TIMEZONE).date()
    problems: list[str] = []

    offsets = {(d - today).days for d in parsed}
    for match in _RELATIVE_DAY.finditer(response):
        if _RELATIVE_OFFSET.get(match.group(0).lower()) not in offsets:
            problems.append(f"says {match.group(0)!r} but the backend date is "
                            f"{min(parsed).isoformat()}")
            break

    weekdays = {d.weekday() for d in parsed}
    names = _weekday_words()
    for token in re.findall(r"[A-Za-z]+|[؀-ۿ]+", response):
        index = names.get(token.lower() if token.isascii() else token)
        if index is not None and index not in weekdays:
            problems.append(f"names the weekday {token!r} but the backend date is a different day")
            break

    days, months = {d.day for d in parsed}, {d.month for d in parsed}
    for match in _DAY_MONTH.finditer(response):
        if int(match.group(1)) not in days or _MONTHS.index(match.group(2).lower()) + 1 not in months:
            problems.append(f"mentions the date {match.group(0)!r}, not provided by the backend")
            break
    for match in _MONTH_DAY.finditer(response):
        if int(match.group(2)) not in days or _MONTHS.index(match.group(1).lower()) + 1 not in months:
            problems.append(f"mentions the date {match.group(0)!r}, not provided by the backend")
            break
    for match in _DAY_TAREEKH.finditer(response):
        if int(match.group(1)) not in days:
            problems.append(f"mentions the date {match.group(0)!r}, not provided by the backend")
            break
    return problems


def _language_problem(response: str, language: str) -> str | None:
    """The reply is not in the language the caller is being answered in."""
    from ollama_judge.language import ENGLISH, ROMAN_URDU, URDU, _ROMAN_URDU_WORDS

    letters = [c for c in response if c.isalpha()]
    if not letters:
        return None
    urdu_share = sum(1 for c in letters if "؀" <= c <= "ۿ") / len(letters)
    roman_words = len(_ROMAN_URDU_WORDS.findall(response))
    if language == URDU and urdu_share < 0.5:
        return "reply is not in Urdu script although Urdu was requested"
    if language in (ENGLISH, ROMAN_URDU) and urdu_share > 0.2:
        return f"reply uses Urdu script although {language} was requested"
    if language == ENGLISH and roman_words >= 3:
        return "reply is in Roman Urdu although English was requested"
    if language == ROMAN_URDU and roman_words == 0 and len(response.split()) >= 4:
        return "reply is in English although Roman Urdu was requested"
    return None


def validate_llm_response(response: str, result,
                          language: str | None = None) -> ValidationReport:
    """
    Check a generated sentence against the backend result.

    Returns a report rather than a bare bool so failures can be logged with a
    reason - useful evidence for the FYP report that the guard actually fires.
    """
    problems: list[str] = []

    if not response or not response.strip():
        return ValidationReport(False, ["empty response"])

    # 0. Naturalness: an Urdu reply must use the spoken clock, not 24-hour.
    if language in ("roman_urdu", "urdu"):
        awkward = _UNNATURAL_24H.search(response)
        if awkward:
            problems.append(
                f"unnatural 24-hour time {awkward.group(0)!r} - Urdu says "
                f"'shaam 5 baje', not '17 baje'")
    if len(response) > 600:
        problems.append("response too long for a voice reply")
    # A text-to-speech voice reads "2026-09-22" digit by digit. The backend's
    # ISO date is for machines; the caller hears "kal" / "Mangal 22 tareekh".
    iso = _ISO_DATE.search(response)
    if iso:
        problems.append(f"reads out a raw ISO date {iso.group(1)!r}")

    # 1. Never claim a mutation the backend did not perform.
    if not result.success and not _NEGATIONS.search(response):
        for match in _SUCCESS_WORDS.finditer(response):
            # "that slot is ALREADY booked" reports a failure; it does not
            # claim we booked anything. Only an unqualified claim counts.
            preceding = response[max(0, match.start() - 20):match.start()]
            if _NOT_A_CLAIM.search(preceding):
                continue
            problems.append(
                f"claims success ({match.group(0)!r}) but the backend "
                f"failed with {result.error_code}")
            break

    # 2. Times must come from the backend. A mention is only "invented" when
    #    NONE of its plausible readings appears in the backend result.
    permitted = allowed_times(result)
    mentions = extract_times(response)
    if mentions and not permitted:
        problems.append("mentions a time although the backend provided none")
    else:
        invented = [surface for surface, readings in mentions
                    if not (readings & permitted)]
        if invented:
            problems.append(f"mentions time(s) not provided by the backend: "
                            f"{invented}")

    # 3. Appointment IDs must be real.
    permitted_ids = allowed_appointment_ids(result)
    for match in _APPOINTMENT_ID.finditer(response):
        if match.group(1).upper() not in permitted_ids:
            problems.append(f"invented appointment ID {match.group(1)}")

    # 4. ISO dates must be real (spoken dates like "tomorrow" are fine).
    permitted_dates = allowed_dates(result)
    for match in _ISO_DATE.finditer(response):
        if match.group(1) not in permitted_dates:
            problems.append(f"invented date {match.group(1)}")

    # 5. Spoken dates must match the backend too: "kal" for an appointment
    #    next week is an invented date even without an ISO string.
    problems.extend(_spoken_date_problems(response, permitted_dates))

    # 6. The reply must be in the language the caller is answered in. None
    #    means the caller did not say, so the language is not checked.
    if language is not None:
        mismatch = _language_problem(response, language)
        if mismatch:
            problems.append(mismatch)

    if problems:
        logger.warning("rejected LLM response: %s | text=%.120s",
                       "; ".join(problems), response)
    return ValidationReport(not problems, problems)
