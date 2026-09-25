"""
Emergencies phrased like a booking request.

Measured gap that produced this file, after the Roman-Urdu augmentation run:

    "mujhe abhi doctor ke paas jana hai seene mein dard hai"
        -> book_appointment 1.00        (expected: emergency)

The corpus had 321 booking rows built on "ke paas jana / dikhana / milna" and
only ONE emergency row using that phrasing. Nearly every emergency example
announced itself with "Emergency!" or "right now", so the model learned that
"doctor ke paas jana hai" always means a routine appointment - even when the
same sentence says the caller cannot breathe.

These rows carry the booking-shaped phrase AND an acute sign (chest pain,
breathlessness, unconsciousness, bleeding, poisoning, seizure, stroke), so the
only thing separating them from a booking is the danger itself.

The challenge-set sentence is deliberately not here, nor a near-copy of it.
"""
from __future__ import annotations

_ROMAN = [
    "Seene mein bohat dard hai, doctor ke paas jana hai abhi",
    "Abbu ke seene mein dard ho raha hai, foran doctor chahiye",
    "Ammi ko saans nahi aa rahi, doctor ko dikhana hai jaldi",
    "Saans lene mein mushkil ho rahi hai, doctor se milna hai foran",
    "Nana behosh ho gaye hain, doctor ke paas le jana hai",
    "Bhai gir kar behosh ho gaya, doctor ko dikhana hai abhi",
    "Accident ho gaya hai, khoon beh raha hai, doctor chahiye",
    "Bachay ka khoon nahi ruk raha, doctor ke pas jana h",
    "Dil ka daura lag raha hai, doctor ke paas jana hai",
    "Lagta hai heart attack hai, doctor ko dikhana h",
    "Bachay ne zeher pee liya hai, doctor ke paas le jana hai",
    "Ammi ko daura para hai, doctor se abhi milna hai",
    "Chehra ek taraf latak gaya hai aur bola nahi ja raha, doctor chahiye",
    "Sar pe chot lagi hai aur behoshi aa rahi hai, doctor ke paas jana hai",
    "Bohat tez bleeding ho rahi hai, doctor ko dikhana hai",
    "Dard bardasht nahi ho raha, isi waqt doctor ke paas jana hai",
    "Pregnancy mein bohat khoon aa raha hai, doctor ke pas jana h",
    "Bachay ko jhatke lag rahe hain, doctor ko foran dikhana hai",
    "Dum ghut raha hai, doctor chahiye foran",
    "Dada ji saans nahi le pa rahe, dr ke paas le jana hai",
    "Mje chakkar aa ke gir gaya tha, seene me dard h, dr chahiye",
    "Beti ne dawai ki poori sheeshi kha li hai, doctor ke paas jana hai",
]

_URDU = [
    "سینے میں شدید درد ہے، ڈاکٹر کے پاس جانا ہے",
    "سانس نہیں آ رہی، ابھی ڈاکٹر کو دکھانا ہے",
    "ابو بے ہوش ہو گئے ہیں، ڈاکٹر کے پاس لے جانا ہے",
    "حادثہ ہو گیا ہے، خون بہہ رہا ہے، ڈاکٹر چاہیے",
    "بچے نے زہر پی لیا ہے، فوراً ڈاکٹر کے پاس جانا ہے",
    "دل کا دورہ لگ رہا ہے، ڈاکٹر سے ملنا ہے",
    "بچے کو جھٹکے لگ رہے ہیں، فوری ڈاکٹر کو دکھانا ہے",
    "خون نہیں رک رہا، ڈاکٹر کے پاس جانا ہے",
]

_ENGLISH = [
    "I need to see a doctor right now, my chest hurts badly",
    "My father collapsed, we have to get him to a doctor",
    "I can't breathe properly, I need to see the doctor immediately",
    "There's been an accident and he is bleeding a lot, we need a doctor",
    "My child swallowed something poisonous, we need a doctor now",
    "I think I'm having a heart attack, I need the doctor",
    "She is having a seizure, we need to see a doctor",
    "His face is drooping and he can't speak, we need a doctor",
]


def emergency_rows() -> list[tuple[str, str]]:
    """(text, intent) pairs to append to the generated corpus."""
    return [(text, "emergency") for text in _ROMAN + _URDU + _ENGLISH]


if __name__ == "__main__":                      # pragma: no cover
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    rows = emergency_rows()
    print(f"{len(rows)} emergency rows")
    for text, _ in rows:
        print("  ", text)
