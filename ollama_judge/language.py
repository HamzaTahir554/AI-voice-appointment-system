"""
Language handling for spoken responses.

Three jobs:

1. Work out which language the caller is using, *stickily*. A one-word "yes"
   in the middle of a Roman-Urdu call carries no language signal and must not
   switch the assistant to English.

2. Provide deterministic response templates in English, Roman Urdu and Urdu, so
   the caller is answered in their own language even when Ollama is down. The
   LLM makes the wording nicer; it is not what makes it multilingual.

3. Keep the wording VARIED. Each template holds several phrasings and the
   caller rotates through them, because a receptionist who repeats herself word
   for word is the fastest way to sound like an IVR menu.
"""
from __future__ import annotations

import re
import unicodedata

ENGLISH = "english"
ROMAN_URDU = "roman_urdu"
URDU = "urdu"

_ARABIC = re.compile(r"[؀-ۿ]")
_ROMAN_URDU_WORDS = re.compile(
    r"\b(mujhe|mujhy|mera|meri|kya|kaise|kaisay|hai|hain|karna|karni|karo|"
    r"kar|chahiye|chahie|kab|kahan|kitna|kitni|nahi|nhi|acha|achha|theek|"
    r"baje|bajay|kal|aaj|parso|ji|haan|han|sahib|batao|dein|dena|deni|leni|"
    r"lena|milna|waqt|din|apna|apni|bilkul|shukriya|salam|assalam)\b", re.I)

# Replies too short to carry a language signal: "yes", "ok", "4 baje".
_MIN_SIGNAL_WORDS = 3


def detect_utterance_language(text: str) -> str:
    """The language of one utterance, ignoring how confident we are."""
    if not text:
        return ENGLISH
    normalized = unicodedata.normalize("NFKC", text)
    if _ARABIC.search(normalized):
        return URDU
    if _ROMAN_URDU_WORDS.search(normalized):
        return ROMAN_URDU
    return ENGLISH


def has_language_signal(text: str) -> bool:
    """
    Is this utterance long enough to tell us anything?

    Urdu script is always a signal, however short. Latin text needs a few
    words, otherwise "yes" would look like a deliberate switch to English.
    """
    if not text:
        return False
    normalized = unicodedata.normalize("NFKC", text)
    if _ARABIC.search(normalized):
        return True
    if _ROMAN_URDU_WORDS.search(normalized):
        return True
    return len(normalized.split()) >= _MIN_SIGNAL_WORDS


def resolve_language(text: str, current: str = ENGLISH,
                     locked: bool = False, override: str = "auto") -> str:
    """
    Decide which language to reply in.

    `override` from config wins outright. Otherwise the conversation keeps its
    established language unless this utterance genuinely indicates another one.
    """
    if override and override != "auto":
        return override
    if locked:
        return current
    if not has_language_signal(text):
        return current
    return detect_utterance_language(text)


# --------------------------------------------------------------------------
# Deterministic templates
# --------------------------------------------------------------------------
# Each entry is a LIST of phrasings. `render` rotates through them, so asking
# the same thing twice never produces the identical sentence twice.
#
# Placeholders: {doctor} {date} {time} {id} {slots}
TEMPLATES = {
    "booked": {
        ENGLISH: [
            "Done. Your appointment with {doctor} is confirmed for {date} at "
            "{time}. The appointment ID is {id}.",
            "That is booked - {doctor}, {date} at {time}. "
            "Your appointment ID is {id}.",
        ],
        ROMAN_URDU: [
            "Done ji. Aap ki appointment {doctor} ke saath {date} {time} "
            "confirm ho gayi hai. Appointment ID {id} hai.",
            "Bilkul, ho gayi. {doctor} ke saath {date} {time} ki appointment "
            "confirm hai. ID {id}.",
        ],
        URDU: [
            "جی ہو گئی۔ آپ کی اپائنٹمنٹ {doctor} کے ساتھ {date} {time} کنفرم "
            "ہو گئی ہے۔ آئی ڈی {id} ہے۔",
            "بالکل، ہو گئی۔ {doctor} کے ساتھ {date} {time} کی اپائنٹمنٹ کنفرم "
            "ہے۔ آئی ڈی {id}۔",
        ],
    },
    "confirm_booking": {
        ENGLISH: [
            "{doctor} is free {date} at {time}. Shall I book it?",
            "I can do {date} at {time} with {doctor}. Book it?",
        ],
        ROMAN_URDU: [
            "{doctor} ke paas {date} {time} ka slot khali hai. Book kar doon?",
            "{date} {time} {doctor} available hain. Main confirm kar doon?",
        ],
        URDU: [
            "{doctor} کے پاس {date} {time} کا وقت خالی ہے۔ بک کر دوں؟",
            "{date} {time} {doctor} دستیاب ہیں۔ کیا میں کنفرم کر دوں؟",
        ],
    },
    "cancelled": {
        ENGLISH: [
            "Done, I have cancelled your {date} appointment with {doctor}.",
            "That is cancelled - {doctor}, {date} at {time}.",
        ],
        ROMAN_URDU: [
            "Ji, aap ki {date} wali appointment {doctor} ke saath cancel kar "
            "di hai.",
            "Ho gayi cancel. {doctor} ke saath {date} {time} wali appointment.",
        ],
        URDU: [
            "جی، آپ کی {date} والی اپائنٹمنٹ {doctor} کے ساتھ منسوخ کر دی ہے۔",
            "منسوخ ہو گئی۔ {doctor} کے ساتھ {date} {time} والی اپائنٹمنٹ۔",
        ],
    },
    "rescheduled": {
        ENGLISH: [
            "Done, I have moved it to {date} at {time}.",
            "That is changed - {doctor}, {date} at {time} now.",
        ],
        ROMAN_URDU: [
            "Ho gaya, appointment {date} {time} par shift kar di hai.",
            "Theek hai, ab {doctor} ke saath {date} {time} ki appointment hai.",
        ],
        URDU: [
            "ہو گیا، اپائنٹمنٹ {date} {time} پر منتقل کر دی ہے۔",
            "ٹھیک ہے، اب {doctor} کے ساتھ {date} {time} کی اپائنٹمنٹ ہے۔",
        ],
    },
    "slot_taken": {
        ENGLISH: [
            "{time} is already taken. {doctor} has {slots} free - which suits?",
            "Sorry, {time} is already taken. I can do {slots}. Which would you like?",
        ],
        ROMAN_URDU: [
            "{time} ka slot already booked hai. {slots} available hai. "
            "Aap kaunsa prefer karenge?",
            "Maazrat, {time} to book ho chuka. {doctor} {slots} free hain. "
            "Kaunsa rakh doon?",
        ],
        URDU: [
            "{time} کا وقت پہلے سے بک ہے۔ {slots} خالی ہیں۔ "
            "آپ کون سا پسند کریں گے؟",
            "معذرت، {time} پہلے سے بک ہے۔ {doctor} {slots} فارغ ہیں۔ کون سا رکھ دوں؟",
        ],
    },
    "no_slots": {
        ENGLISH: [
            "{doctor} is fully booked {date}. Shall I try another day?",
            "Nothing free with {doctor} {date}, I am afraid. Another day?",
        ],
        ROMAN_URDU: [
            "{doctor} ke paas {date} koi slot khali nahi. Koi aur din dekh loon?",
            "{date} to {doctor} full hain. Aap kisi aur din aa sakte hain?",
        ],
        URDU: [
            "{doctor} کے پاس {date} کوئی وقت خالی نہیں۔ کوئی اور دن دیکھ لوں؟",
            "{date} تو {doctor} مصروف ہیں۔ کیا کسی اور دن آ سکتے ہیں؟",
        ],
    },
    "doctor_unavailable": {
        ENGLISH: [
            "Sorry, {doctor} is not at the clinic {date}. Another day?",
            "{doctor} is not seeing patients {date}. Shall I check another day?",
        ],
        ROMAN_URDU: [
            "Maazrat, {doctor} {date} clinic nahi aa rahe. Koi aur din dekhein?",
            "{doctor} {date} available nahi hain. Kisi aur din ka dekh loon?",
        ],
        URDU: [
            "معذرت، {doctor} {date} کلینک نہیں آ رہے۔ کوئی اور دن دیکھیں؟",
            "{doctor} {date} دستیاب نہیں ہیں۔ کسی اور دن کا دیکھ لوں؟",
        ],
    },
    "availability": {
        ENGLISH: [
            "{doctor} has {slots} free {date}. Which suits you?",
            "{date} I can do {slots} with {doctor}. Which would you like?",
        ],
        ROMAN_URDU: [
            "{date} {doctor} ke paas {slots} available hai. "
            "Kaunsa time aap ko suit karega?",
            "{doctor} {date} {slots} par free hain. Aap kaunsa rakhna chahenge?",
        ],
        URDU: [
            "{date} {doctor} کے پاس {slots} دستیاب ہیں۔ "
            "کون سا وقت آپ کو مناسب رہے گا؟",
            "{doctor} {date} {slots} پر فارغ ہیں۔ آپ کون سا رکھنا چاہیں گے؟",
        ],
    },
    "appointment_details": {
        ENGLISH: [
            "Your appointment is with {doctor}, {date} at {time}.",
            "You are booked with {doctor} on {date} at {time}.",
        ],
        ROMAN_URDU: [
            "Aap ki appointment {doctor} ke saath {date} {time} par hai.",
            "{date} {time} {doctor} ke saath aap ki appointment hai.",
        ],
        URDU: [
            "آپ کی اپائنٹمنٹ {doctor} کے ساتھ {date} {time} پر ہے۔",
            "{date} {time} {doctor} کے ساتھ آپ کی اپائنٹمنٹ ہے۔",
        ],
    },
    "ask_doctor": {
        ENGLISH: [
            "Which doctor would you like to see?",
            "Who would you like to see?",
            "Any particular doctor in mind?",
        ],
        ROMAN_URDU: [
            "Kis doctor ke liye appointment chahiye?",
            "Aap kis doctor se milna chahenge?",
            "Konse doctor ke saath rakhoon?",
        ],
        URDU: [
            "کس ڈاکٹر کے لیے اپائنٹمنٹ چاہیے؟",
            "آپ کس ڈاکٹر سے ملنا چاہیں گے؟",
            "کون سے ڈاکٹر کے ساتھ رکھوں؟",
        ],
    },
    "ask_date": {
        ENGLISH: [
            "Which day suits you?",
            "When would you like to come in?",
            "What day works for you?",
        ],
        ROMAN_URDU: [
            "Kis din aana convenient rahega?",
            "Aap kis din aana chahenge?",
            "Konsa din theek rahega aap ke liye?",
        ],
        URDU: [
            "کس دن آنا آسان رہے گا؟",
            "آپ کس دن آنا چاہیں گے؟",
            "کون سا دن ٹھیک رہے گا؟",
        ],
    },
    "ask_time": {
        ENGLISH: [
            "What time suits you?",
            "Any particular time?",
            "What time would you like to come?",
        ],
        ROMAN_URDU: [
            "Kis waqt aana chahenge?",
            "Koi khaas time?",
            "Aap ko konsa time suit karega?",
        ],
        URDU: [
            "کس وقت آنا چاہیں گے؟",
            "کوئی خاص وقت؟",
            "آپ کو کون سا وقت مناسب رہے گا؟",
        ],
    },
    "ask_appointment_id": {
        ENGLISH: [
            "Do you have the appointment ID handy?",
            "Could you tell me the appointment ID?",
        ],
        ROMAN_URDU: [
            "Aap ke paas appointment ID hai?",
            "Zara appointment ID bata dijiye.",
        ],
        URDU: [
            "کیا آپ کے پاس اپائنٹمنٹ آئی ڈی ہے؟",
            "ذرا اپائنٹمنٹ آئی ڈی بتا دیجیے۔",
        ],
    },
    "greeting": {
        ENGLISH: [
            "Hello! How can I help?",
            "Hi there. What can I do for you?",
        ],
        ROMAN_URDU: [
            "Wa alaikum assalam. Kaise help kar sakti hoon?",
            "Ji assalam o alaikum. Kya khidmat kar sakti hoon?",
        ],
        URDU: [
            "وعلیکم السلام۔ کیسے مدد کر سکتی ہوں؟",
            "جی السلام علیکم۔ کیا خدمت کر سکتی ہوں؟",
        ],
    },
    "farewell": {
        ENGLISH: ["Take care. Goodbye.", "Thanks for calling. Take care."],
        ROMAN_URDU: ["Khuda hafiz, apna khayal rakhiye ga.",
                     "Allah hafiz ji, shukriya."],
        URDU: ["خدا حافظ، اپنا خیال رکھیے گا۔", "اللہ حافظ جی، شکریہ۔"],
    },
    "thanks": {
        ENGLISH: ["You are welcome.", "Happy to help. Anything else?"],
        ROMAN_URDU: ["Koi baat nahi ji.", "Khushi hui. Aur kuch?"],
        URDU: ["کوئی بات نہیں جی۔", "خوشی ہوئی۔ اور کچھ؟"],
    },
    "clarify": {
        ENGLISH: [
            "Sorry, I did not catch that. Are you booking, cancelling or "
            "changing an appointment?",
            "I am not quite with you - did you want to book, cancel or change "
            "an appointment?",
        ],
        ROMAN_URDU: [
            "Maazrat, samajh nahi aaya. Appointment book karni hai, cancel "
            "ya change?",
            "Ji zara dobara - book karni hai, cancel karni hai, ya time "
            "badalna hai?",
        ],
        URDU: [
            "معذرت، سمجھ نہیں آیا۔ اپائنٹمنٹ بک کرنی ہے، منسوخ یا تبدیل؟",
            "جی ذرا دوبارہ - بک کرنی ہے، منسوخ کرنی ہے، یا وقت بدلنا ہے؟",
        ],
    },
    "not_found": {
        ENGLISH: [
            "I cannot find that one. Could you read the ID again?",
            "That ID is not coming up. Could you repeat it?",
        ],
        ROMAN_URDU: [
            "Us ID se koi appointment nahi mil rahi. Zara dobara batayein?",
            "Yeh ID system mein nahi hai. Ek baar phir bata dijiye?",
        ],
        URDU: [
            "اس آئی ڈی سے کوئی اپائنٹمنٹ نہیں مل رہی۔ ذرا دوبارہ بتائیں؟",
            "یہ آئی ڈی سسٹم میں نہیں ہے۔ ایک بار پھر بتا دیجیے؟",
        ],
    },
    "duplicate": {
        ENGLISH: [
            "You already have an appointment with {doctor} that day. "
            "Shall I move that one instead?",
        ],
        ROMAN_URDU: [
            "Aap ki us din {doctor} ke saath pehle se appointment hai. "
            "Wohi change kar doon?",
        ],
        URDU: [
            "آپ کی اس دن {doctor} کے ساتھ پہلے سے اپائنٹمنٹ ہے۔ "
            "وہی تبدیل کر دوں؟",
        ],
    },
    "already_cancelled": {
        ENGLISH: ["That one is already cancelled."],
        ROMAN_URDU: ["Woh appointment pehle hi cancel ho chuki hai."],
        URDU: ["وہ اپائنٹمنٹ پہلے ہی منسوخ ہو چکی ہے۔"],
    },
    "doctor_not_found": {
        ENGLISH: ["I could not place that name. Which doctor did you mean?"],
        ROMAN_URDU: ["Yeh naam samajh nahi aaya. Konse doctor ki baat hai?"],
        URDU: ["یہ نام سمجھ نہیں آیا۔ کون سے ڈاکٹر کی بات ہے؟"],
    },
    "bad_date": {
        ENGLISH: ["That date does not look right. Which day did you mean?"],
        ROMAN_URDU: ["Yeh date theek nahi lag rahi. Konsa din chahiye tha?"],
        URDU: ["یہ تاریخ ٹھیک نہیں لگ رہی۔ کون سا دن چاہیے تھا؟"],
    },
    "bad_time": {
        ENGLISH: ["Sorry, what time was that?"],
        ROMAN_URDU: ["Maazrat, waqt dobara bata dijiye?"],
        URDU: ["معذرت، وقت دوبارہ بتا دیجیے؟"],
    },
    "no_appointments": {
        ENGLISH: [
            "I cannot see any upcoming appointments for you. "
            "Shall I book one?",
        ],
        ROMAN_URDU: [
            "Aap ki koi aane wali appointment nahi mil rahi. Book kar doon?",
        ],
        URDU: ["آپ کی کوئی آنے والی اپائنٹمنٹ نہیں مل رہی۔ بک کر دوں؟"],
    },
    "confirm_intent": {
        ENGLISH: ["Just to be sure - you want to {action}?"],
        ROMAN_URDU: ["Ji, aap {action} chahte hain?"],
        URDU: ["جی، آپ {action} چاہتے ہیں؟"],
    },

    "backend_error": {
        ENGLISH: [
            "Sorry, the system is playing up. Could you try again shortly?",
        ],
        ROMAN_URDU: [
            "Maazrat, system mein thori problem aa rahi hai. "
            "Thori der baad try karein?",
        ],
        URDU: [
            "معذرت، سسٹم میں تھوڑی دقت آ رہی ہے۔ تھوڑی دیر بعد کوشش کریں؟",
        ],
    },
}


# --------------------------------------------------------------------------
# Short acknowledgements, prepended when the caller has just told us something.
# A receptionist says "theek hai" before the next question; a form does not.
# --------------------------------------------------------------------------
ACKNOWLEDGEMENTS = {
    ENGLISH: ["Sure.", "Right.", "Okay.", "Got it."],
    ROMAN_URDU: ["Bilkul.", "Theek hai.", "Ji.", "Acha."],
    URDU: ["بالکل۔", "ٹھیک ہے۔", "جی۔", "اچھا۔"],
}

# Said when the caller corrects something they had already told us.
CORRECTION_ACKS = {
    ENGLISH: ["No problem.", "That is fine."],
    ROMAN_URDU: ["Koi baat nahi.", "Theek hai ji."],
    URDU: ["کوئی بات نہیں۔", "ٹھیک ہے جی۔"],
}


# Short descriptions used by the "did you mean...?" question.
INTENT_PHRASES = {
    "book_appointment": {
        ENGLISH: "book an appointment",
        ROMAN_URDU: "appointment lena",
        URDU: "اپائنٹمنٹ لینا",
    },
    "cancel_appointment": {
        ENGLISH: "cancel an appointment",
        ROMAN_URDU: "appointment cancel karna",
        URDU: "اپائنٹمنٹ منسوخ کرنا",
    },
    "reschedule_appointment": {
        ENGLISH: "change an appointment",
        ROMAN_URDU: "appointment ka time change karna",
        URDU: "اپائنٹمنٹ کا وقت تبدیل کرنا",
    },
    "check_appointment": {
        ENGLISH: "check your appointment",
        ROMAN_URDU: "apni appointment check karna",
        URDU: "اپنی اپائنٹمنٹ دیکھنا",
    },
}


def intent_phrase(intent: str, language: str) -> str | None:
    entry = INTENT_PHRASES.get(intent)
    return (entry.get(language) or entry.get(ENGLISH)) if entry else None


def acknowledge(language: str, correction: bool = False, index: int = 0) -> str:
    """A short "sure" / "theek hai" to open a reply with."""
    pool = (CORRECTION_ACKS if correction else ACKNOWLEDGEMENTS).get(
        language, ACKNOWLEDGEMENTS[ENGLISH])
    return pool[index % len(pool)]


def render(key: str, language: str, variant: int = 0, **values) -> str | None:
    """
    Fill a template, rotating through its phrasings.

    `variant` is a counter the caller increments per question, so asking the
    same thing twice never produces the identical sentence.
    """
    entry = TEMPLATES.get(key)
    if not entry:
        return None
    options = entry.get(language) or entry.get(ENGLISH)
    if not options:
        return None
    if isinstance(options, str):            # tolerate a bare string
        options = [options]
    template = options[variant % len(options)]
    try:
        return template.format(**values)
    except KeyError:
        return None


# --------------------------------------------------------------------------
# Speech-friendly date and time, per language
# --------------------------------------------------------------------------
_URDU_DAYS = {
    "Monday": "پیر", "Tuesday": "منگل", "Wednesday": "بدھ",
    "Thursday": "جمعرات", "Friday": "جمعہ", "Saturday": "ہفتہ",
    "Sunday": "اتوار",
}
_ROMAN_DAYS = {
    "Monday": "Peer", "Tuesday": "Mangal", "Wednesday": "Budh",
    "Thursday": "Jumeraat", "Friday": "Jumma", "Saturday": "Hafta",
    "Sunday": "Itwar",
}


def spoken_time(time_str: str | None, language: str) -> str:
    """'16:00' -> '4:00 PM' / 'shaam 4 baje' / 'شام 4 بجے'."""
    if not time_str:
        return ""
    try:
        hour, minute = (int(x) for x in str(time_str).split(":"))
    except (ValueError, AttributeError):
        return str(time_str)

    display = hour % 12 or 12
    if language == ENGLISH:
        return f"{display}:{minute:02d} {'AM' if hour < 12 else 'PM'}"

    # Minutes join with a colon: "shaam 4:20 baje". Writing them as a separate
    # number ("shaam 4 20 baje") is both poor Urdu and ambiguous - the
    # validator would read the stray "20 baje" as a different time.
    clock = f"{display}:{minute:02d}" if minute else f"{display}"
    if language == ROMAN_URDU:
        part = "subah" if hour < 12 else ("dopahar" if hour < 16 else "shaam")
        return f"{part} {clock} baje"
    part = "صبح" if hour < 12 else ("دوپہر" if hour < 16 else "شام")
    return f"{part} {clock} بجے"


def spoken_date(iso_date: str | None, language: str) -> str:
    """'2026-09-11' -> 'tomorrow' / 'kal' / 'کل'."""
    if not iso_date:
        return ""
    from datetime import datetime, timedelta
    from config import TIMEZONE

    try:
        parsed = datetime.strptime(str(iso_date), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return str(iso_date)

    today = datetime.now(TIMEZONE).date()
    offsets = {
        0: {ENGLISH: "today", ROMAN_URDU: "aaj", URDU: "آج"},
        1: {ENGLISH: "tomorrow", ROMAN_URDU: "kal", URDU: "کل"},
        2: {ENGLISH: "the day after tomorrow", ROMAN_URDU: "parso",
            URDU: "پرسوں"},
    }
    delta = (parsed - today).days
    if delta in offsets:
        return offsets[delta][language]

    weekday = parsed.strftime("%A")
    if language == ENGLISH:
        return parsed.strftime("%A, %d %B").replace(" 0", " ")
    if language == ROMAN_URDU:
        return f"{_ROMAN_DAYS.get(weekday, weekday)} {parsed.day} tareekh"
    return f"{_URDU_DAYS.get(weekday, weekday)} {parsed.day} تاریخ"


def spoken_slots(slots: list[str], language: str) -> str:
    """Join times with the right conjunction for the language."""
    spoken = [spoken_time(s, language) for s in slots if s]
    if not spoken:
        return ""
    if len(spoken) == 1:
        return spoken[0]
    joiner = {ENGLISH: " or ", ROMAN_URDU: " ya ", URDU: " یا "}[language]
    return ", ".join(spoken[:-1]) + joiner + spoken[-1]


# Replies that carry facts (appointment details, alternatives, leave, doctor
# and clinic answers) live in their own module; merged here so render() finds
# them like any other template.
from ollama_judge.dialog_templates import DIALOG_TEMPLATES  # noqa: E402

for _key, _entry in DIALOG_TEMPLATES.items():
    TEMPLATES.setdefault(_key, _entry)
