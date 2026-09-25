"""
How Pakistani patients ACTUALLY ask, plus the errors Whisper makes.

Measured gap that produced this file: of six natural booking phrases from the
requirements, the model got five wrong -

    "Mera time laga dein"                -> change_time            (0.80)
    "Appointment laga dein"              -> reschedule_appointment (0.84)
    "Doctor ke paas jana hai"            -> doctor_information     (0.35)
    "Ji doctor ko dikhana tha"           -> find_doctor            (0.21)
    "Bhai doctor ka appointment chahiye" -> find_doctor            (0.30)
    "Mujhe checkup karwana hai"          -> help                   (0.83)

None of these use the word "book", which is why the model missed them: the
generated corpus was built from templates that nearly all did. Real callers say
"dikhana hai", "laga dein", "jana hai", "milna hai" - never "book".

Two other classes of miss are covered here:

  * STT noise. Whisper writes "docter", "apoinment", "dr ahmad". The text the
    classifier sees is transcribed speech, not typing, so misspellings are the
    normal case rather than an edge case.

  * Negated cancellations. "Meri appointment cancel NAHI karni, bas time change
    karna hai" scored cancel_appointment at 0.92 - confidently wrong, and it
    would cancel a real patient's appointment.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Booking, the way people actually say it (no "book" anywhere)
# --------------------------------------------------------------------------
_BOOK_ROMAN = [
    # "show the doctor" - by far the most common phrasing
    "Mujhe doctor ko dikhana hai",
    "Doctor ko dikhana hai",
    "Ji doctor ko dikhana tha",
    "Mujhe doctor ko dikhana tha",
    "Bachay ko doctor ko dikhana hai",
    "Ammi ko doctor ko dikhana hai",
    "Walid sahib ko doctor ko dikhana hai",
    "Mujhe {doctor} ko dikhana hai",
    "{doctor} ko dikhana tha",
    # "go to the doctor"
    "Doctor ke paas jana hai",
    "Mujhe doctor ke paas jana hai",
    "{doctor} ke paas jana hai",
    "Clinic aana hai doctor ke paas",
    # "meet the doctor"
    "Mujhe doctor se milna hai",
    "Doctor se milna hai",
    "{doctor} se milna hai",
    "Mujhe {doctor} se milna tha",
    "Doctor se milne ka time chahiye",
    # "put my time down"
    "Mera time laga dein",
    "Time laga dein",
    "Appointment laga dein",
    "Mera number laga dein",
    "Mera naam laga dein",
    "{doctor} ke saath time laga dein",
    "Zara mera time laga dijiye",
    # "I need time / a slot"
    "Doctor ka time chahiye",
    "Mujhe doctor ka time chahiye",
    "Mujhe {doctor} ka time chahiye",
    "Mujhe slot chahiye",
    "Mujhe doctor ke liye slot chahiye",
    "Kal ka time mil sakta hai",
    "Kal ka time chahiye tha",
    # checkup framing
    "Mujhe checkup karwana hai",
    "Checkup karwana hai",
    "Mujhe doctor se checkup karwana hai",
    "Apna checkup karwana tha",
    "Mujhe apna checkup karana hai",
    # informal openers
    "Bhai doctor ka appointment chahiye",
    "Ji appointment chahiye thi",
    "Baji mujhe doctor ka time chahiye",
    "Sir mujhe doctor se milna hai",
    "Mujhe appointment chahiye thi",
    # explicit but casual
    "Doctor ki appointment karni hai",
    "Appointment karwani hai",
    "Mujhe appointment banwani hai",
    "{doctor} ki appointment chahiye",
]

_BOOK_URDU = [
    "مجھے ڈاکٹر کو دکھانا ہے",
    "ڈاکٹر کو دکھانا ہے",
    "جی ڈاکٹر کو دکھانا تھا",
    "بچے کو ڈاکٹر کو دکھانا ہے",
    "ڈاکٹر کے پاس جانا ہے",
    "مجھے ڈاکٹر کے پاس جانا ہے",
    "مجھے ڈاکٹر سے ملنا ہے",
    "ڈاکٹر سے ملنے کا وقت چاہیے",
    "میرا ٹائم لگا دیں",
    "ٹائم لگا دیں",
    "اپائنٹمنٹ لگا دیں",
    "میرا نمبر لگا دیں",
    "ڈاکٹر کا وقت چاہیے",
    "مجھے ڈاکٹر کا وقت چاہیے",
    "مجھے چیک اپ کروانا ہے",
    "چیک اپ کروانا ہے",
    "کل کا وقت مل سکتا ہے",
    "مجھے اپائنٹمنٹ چاہیے تھی",
    "ڈاکٹر کی اپائنٹمنٹ کرنی ہے",
]

_BOOK_ENGLISH = [
    "I want to see a doctor",
    "I need to see the doctor",
    "I need an appointment",
    "Can you book an appointment",
    "I want a checkup",
    "I would like to see {doctor}",
    "Can I get a slot with {doctor}",
    "Put me down for an appointment",
]

# --------------------------------------------------------------------------
# STT noise. Whisper output is not spelled the way anyone types.
# --------------------------------------------------------------------------
_STT_NOISE = {
    "book_appointment": [
        "mujhe docter ka apoinment chahiye",
        "mujhe doctar se milna hai",
        "apointment chahiye doctor ka",
        "mujhe dr ahmad se milna hai",
        "docter ko dikhana hai",
        "mujhe apointment leni he",
        "doctor ka apointment chaiye",
        "mujhe dactar ko dikhana hai",
        "appointmnt chahiye",
        "mje doctor se milna h",
    ],
    "cancel_appointment": [
        "apointment cancl karni hai",
        "meri apointment cancel kr dein",
        "mera apoinment cancel kardein",
        "appointmnt cancel karni he",
        "meri apointment khatam kar dein",
    ],
    "reschedule_appointment": [
        "apointment resedule karni hai",
        "mera tym change kar dein",
        "appointmnt change karni he",
        "meri apointment kisi or din kar dein",
    ],
    "doctor_fee": [
        "doctar ki fees kitni hy",
        "docter ki fee kitni hai",
        "fees kitni hy",
        "consultaion fee kya hai",
    ],
    "appointment_status": [
        "meri apointment kb hai",
        "mera apoinment kis din hai",
        "meri appointmnt kab he",
    ],
}

# --------------------------------------------------------------------------
# Negated cancellation -> reschedule.
#
# "Cancel NAHI karni, bas time change karna hai" is a reschedule. The model
# scored cancel_appointment 0.92 on it, which would cancel a real appointment.
# --------------------------------------------------------------------------
_NEGATED_CANCEL = [
    "Meri appointment cancel nahi karni, bas time change karna hai",
    "Cancel nahi karni, sirf waqt badalna hai",
    "Appointment cancel mat karein, bas din change kar dein",
    "Cancel nahi, reschedule karni hai",
    "Mujhe cancel nahi karni, doosra time chahiye",
    "Appointment rakhni hai bas time aage karna hai",
    "Cancel na karein, kisi aur din kar dein",
    "Do not cancel it, just move it to another day",
    "Not cancelling, I just want a different time",
    "اپائنٹمنٹ منسوخ نہیں کرنی، بس وقت بدلنا ہے",
    "کینسل نہیں کرنی، صرف دن تبدیل کرنا ہے",
]

# --------------------------------------------------------------------------
# Availability asked as a question, kept distinct from a booking request.
# --------------------------------------------------------------------------
_AVAILABILITY = [
    "Koi slot khali hai",
    "Konsa time khali hai",
    "Kal ka koi slot hai",
    "Koi free time hai",
    "Doctor ke paas kab ka time available hai",
    "Doctor ke available timings kya hain",
    "4 baje ka slot mil sakta hai",
    "Appointment ka koi slot available hai",
    "Kya {doctor} available hain",
    "Doctor aaj clinic mein hain",
    "{doctor} kal available honge",
    "Doctor kis din clinic aate hain",
    "Doctor kab available hote hain",
    "کیا ڈاکٹر دستیاب ہیں",
    "کوئی وقت خالی ہے",
    "کل کا کوئی سلاٹ ہے",
    "ڈاکٹر کس دن کلینک آتے ہیں",
]

_DOCTORS = ["Dr Ahmed", "Dr Asim", "Dr Hamza", "Dr Sara", "Dr Ahmed Khan"]


def _expand(phrases: list[str]) -> list[str]:
    """Fill the {doctor} placeholder, keeping slot-free phrases as they are."""
    out: list[str] = []
    for index, phrase in enumerate(phrases):
        if "{doctor}" in phrase:
            # Vary the name so the model does not tie the phrasing to one doctor.
            out.append(phrase.format(doctor=_DOCTORS[index % len(_DOCTORS)]))
        else:
            out.append(phrase)
    return out


def natural_rows() -> list[tuple[str, str]]:
    """(text, intent) pairs to append to the generated corpus."""
    rows: list[tuple[str, str]] = []

    for phrase in _expand(_BOOK_ROMAN + _BOOK_URDU + _BOOK_ENGLISH):
        rows.append((phrase, "book_appointment"))

    for intent, phrases in _STT_NOISE.items():
        for phrase in phrases:
            rows.append((phrase, intent))

    for phrase in _NEGATED_CANCEL:
        rows.append((phrase, "reschedule_appointment"))

    for phrase in _expand(_AVAILABILITY):
        rows.append((phrase, "check_availability"))

    return rows
