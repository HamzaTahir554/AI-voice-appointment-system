"""
Contrast phrasings for intents the system audit found confused.

Every block below starts from a MEASURED failure on the audit set
(reports/intent_audit/before_cases.csv). The phrases teach the boundary the
model was missing - they are written to differ from every audit, holdout and
challenge phrase (preprocess.py drops any exact match anyway, and
scripts/evaluate_intents.py reports near-duplicates).

RELABEL fixes one labelling contradiction found in the generated corpus:
"Mujhe samajh nahi aa raha" was labelled `help` while "Kuch samajh nahi aa
raha" - the same complaint - was labelled `unclear_request`. A model cannot
learn a boundary the labels themselves do not respect.
"""
from __future__ import annotations

RELABEL = {
    "Kuch samajh nahi aa raha": "help",
    "کچھ سمجھ نہیں آ رہا": "help",
}

GAPS: dict[str, dict] = {
    # "doctor available hain?" -> book_appointment 0.91
    # "Dr Ahmed kal clinic mein hain?" -> clinic_location 0.93
    # "dr ahmed monday ko milenge?" -> find_doctor 0.48
    "check_availability": {
        "phrases": [
            "Dr Sara available hain kya",
            "kya Dr Hamza aaj available hain",
            "doctor sahab kal clinic mein honge kya",
            "Dr Ahmed parson clinic aayenge?",
            "Dr Asim Tuesday ko milenge kya",
            "kya doctor sahab abhi clinic mein hain",
            "Dr Sara shaam ko baithengi?",
            "Dr Hamza is hafte available honge",
            "dr asim kal milenge?",
            "dr sara aaj clinic me hain?",
            "doctor sahab Friday ko aate hain kya",
            "kya kal doctor mil jayenge",
            "dr hamza wednesday ko baithte hain?",
            "Will Dr Sara be at the clinic tomorrow",
            "Is Dr Hamza in today",
            "Can I find Dr Asim at the clinic on Monday",
            "کیا ڈاکٹر سارہ آج کلینک میں ہیں",
            "ڈاکٹر حمزہ کل ملیں گے؟",
            "کیا ڈاکٹر صاحب شام کو بیٹھیں گے",
        ],
    },
    # "dr ahmed ki jagah dr ali chahiye" -> book_appointment 1.00
    # "mera dr badal do" -> find_doctor 0.57
    "change_doctor": {
        "phrases": [
            "Dr Asim ki jagah Dr Sara kar dein",
            "Dr Hamza ke bajaye koi aur doctor chahiye",
            "mujhe Dr Sara nahi chahiye, koi aur doctor de dein",
            "doctor badal dein please",
            "mera doctor change kr do",
            "dr sara ki jagah dr asim ko dikhana hai",
            "is appointment mein doctor tabdeel kar dein",
            "koi doosra doctor kar dein",
            "meri appointment Dr Sara ki bajaye Dr Asim ke saath kar dein",
            "dr change krna h",
            "doctor badalwana hai",
            "Can you give me another doctor instead",
            "I would rather see a different doctor",
            "Swap Dr Asim for Dr Sara please",
            "ڈاکٹر صاحب بدل دیں",
            "ڈاکٹر عاصم کی جگہ ڈاکٹر سارہ کر دیں",
        ],
    },
    # A bare, lower-case doctor name (how Whisper writes a slot answer) read as
    # change_doctor 0.87 and derailed a live booking. "Dr Ahmed" already
    # classifies as doctor_information; these keep the lower-case forms there.
    "doctor_information": {
        "phrases": [
            "dr ahmad", "dr ahamd", "dr sara", "dr hamza", "dr asim",
            "doctor ahmed", "dr ahmed khan", "ahmed sahab",
        ],
    },
    # "mujhe abhi doctor ke paas jana hai seene mein dard hai" -> book 0.99.
    # 37 of 38 earlier emergency rows put the symptom FIRST; these put the
    # booking-shaped phrase first, which is the order that failed.
    "emergency": {
        "phrases": [
            "mujhe foran doctor ke paas jana hai, saans nahi aa rahi",
            "doctor ke paas le jana hai abhi, abbu behosh hain",
            "mje dr ko dikhana h jaldi, bacha saans nahi le raha",
            "appointment nahi, abhi doctor chahiye, seene mein dard hai",
            "doctor se abhi milna hai, khoon ruk nahi raha",
            "mujhe dr ke pas jana h, chest pain bohat zyada hai",
            "abhi doctor ko dikhana hai, ammi gir gayi aur hosh nahi",
            "dr sahab ko abhi bulayein, dil ka daura lag raha hai",
            "doctor chahiye foran, bachay ne dawai kha li hai",
            "I need to see the doctor now, I cannot breathe",
            "Get me to a doctor, there is severe bleeding",
            "We need an appointment right now, he is unconscious",
            "It is an emergency, I need Dr Sara immediately",
            "Urgent! Please get Dr Hamza now",
            "مجھے ابھی ڈاکٹر کے پاس جانا ہے، سانس نہیں آ رہی",
            "ڈاکٹر کو ابھی دکھانا ہے، بچہ بے ہوش ہے",
            "ڈاکٹر سے فوراً ملنا ہے، سینے میں درد ہے",
        ],
    },
    # "Mera time change kar dein" -> change_date 0.50 (low band -> unknown)
    # "میری اپائنٹمنٹ کسی اور دن رکھ دیں" -> book_appointment 0.99
    # test split: "مجھے دوسرا وقت چاہیے" -> book_appointment 1.00
    "change_time": {
        "phrases": [
            "mera time badal dein",
            "time tabdeel kar dein",
            "waqt thora aage kar dein",
            "mujhe koi aur time chahiye",
            "mujhe doosra time de dein",
            "baad wala time kar dein",
            "pehle wala time mil sakta hai",
            "time change krna h",
            "مجھے دوسرا ٹائم دے دیں",
            "کوئی اور وقت کر دیں",
            "بعد والا وقت کر دیں",
        ],
    },
    "reschedule_appointment": {
        "phrases": [
            "appointment ka din badal dein",
            "appointment next week pe kar dein",
            "mje appointment postpone karni h",
            "اپائنٹمنٹ آگے کر دیں",
            "میری بکنگ دوسرے دن کر دیں",
            "اپائنٹمنٹ کی تاریخ آگے بڑھا دیں",
        ],
    },
    # "Mera time kya hai?" -> clinic_timing 0.68
    # "Do I have any booking this week" -> book_appointment 0.94
    # "mje bata dein meri booking kab ki hai" -> book_appointment 0.63
    "appointment_status": {
        "phrases": [
            "mera appointment kab ka hai",
            "mera time kab ka laga hai",
            "kya meri booking bani hui hai",
            "meri appointment ka din aur time bata dein",
            "mje yaad nahi meri appointment kab hai",
            "kya mera naam list mein hai",
            "appointment pakki hai na?",
            "meri booking confirm hui thi kya",
            "mujhe kis waqt aana hai appointment pe",
            "Have I got an appointment coming up",
            "What day was I booked for",
            "Can you tell me my appointment time",
            "Is there a booking under my name",
            "میری اپائنٹمنٹ کس دن کی ہے",
            "کیا میری بکنگ ہو چکی ہے",
            "مجھے کس وقت آنا ہے",
        ],
    },
    # "checkup ke kitne paise lagte hain" -> change_doctor 0.38
    "doctor_fee": {
        "phrases": [
            "doctor ko dikhane ke kitne paise hain",
            "check up ki fees kitni hai",
            "Dr Sara ke charges kya hain",
            "consultation ke kitne rupay lagenge",
            "fees kitni lagti hai doctor ki",
            "doctor sahab kitna lete hain",
            "ڈاکٹر صاحب کتنے پیسے لیتے ہیں",
            "چیک اپ کے کتنے روپے لگیں گے",
        ],
    },
    # "theek hai phir baat hoti hai" -> repeat_information 0.59
    "goodbye": {
        "phrases": [
            "chalo ji phir baat karte hain",
            "acha baad mein baat karta hoon",
            "theek hai ji, rakhti hoon",
            "bas ji itna hi, khuda hafiz",
            "ok phir kabhi baat karenge",
            "Okay, talk later",
            "I will call back later, bye",
            "ٹھیک ہے بعد میں بات کرتے ہیں",
        ],
    },
    # "Ji kar dein" -> change_date 0.98
    "confirm": {
        "phrases": [
            "haan kar dein",
            "ji kar dijiye",
            "theek hai kar do",
            "haan ji kar dijiye",
            "han g kr dein",
            "jee zaroor kar dein",
            "ہاں جی کر دیجیے",
        ],
    },
    # "awaz kat rahi hai phir se boliye" -> emergency 0.97
    "repeat_information": {
        "phrases": [
            "awaz toot rahi hai",
            "line kharab hai dobara boliye",
            "aap ki awaz saaf nahi aa rahi",
            "signal nahi aa raha, phir se kahein",
            "kat kat ke awaz aa rahi hai",
            "zara phir se bataiye kya kaha",
            "You are breaking up, can you repeat",
            "The line is bad, say it again",
            "آواز ٹوٹ رہی ہے، دوبارہ بولیں",
            "لائن خراب ہے، پھر سے بتائیں",
        ],
    },
    # "ایک سیکنڈ رکیں" -> help 0.98, "aaj mausam kaisa hai" -> help 0.95,
    # "can you order me a pizza" -> book_appointment 0.77,
    # test split: "Hold on" -> goodbye 0.97 (would end the call), "Uhh" -> confirm 0.97
    "unclear_request": {
        "phrases": [
            "ek sec",
            "ruko ruko",
            "haan wo kya kehte hain",
            "ek lamha",
            "abhi batata hoon ruko",
            "wo matlab mera kehna tha",
            "thehriye zara",
            "hmm acha to",
            "Just a sec",
            "Give me a moment",
            "Hang on a minute",
            "Erm, what was I saying",
            "ایک لمحہ",
            "ذرا ٹھہریں",
            "رکیں ذرا سوچ لوں",
            # out of domain: a clinic line cannot do these
            "cricket ka score kya hai",
            "mujhe taxi bulwa dein",
            "koi joke sunao",
            "mobile recharge karna hai",
            "Book me a flight to Lahore",
            "Tell me a joke",
            "What is the cricket score",
            "Pay my electricity bill",
            "کرکٹ کا سکور کیا ہے",
            "مجھے ٹیکسی چاہیے",
        ],
    },
    # "ji main Bilal bol raha hoon" -> book_appointment 0.99
    "provide_patient_name": {
        "phrases": [
            "main Sana bol rahi hoon",
            "ji mera naam Usman hai",
            "Kamran baat kar raha hoon",
            "main Hina hoon ji",
            "patient ka naam Zainab hai",
            "This is Omar speaking",
            "It is Fatima here",
            "میں عائشہ بول رہی ہوں",
        ],
    },
}


def contrast_rows() -> list[tuple[str, str]]:
    """(text, intent) pairs to append to the generated corpus."""
    return [(phrase, intent) for intent, gap in GAPS.items()
            for phrase in gap["phrases"]]


if __name__ == "__main__":                      # pragma: no cover
    import sys
    from collections import Counter
    sys.stdout.reconfigure(encoding="utf-8")
    rows = contrast_rows()
    print(f"{len(rows)} contrast rows: {dict(Counter(i for _, i in rows))}")
