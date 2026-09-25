"""
Generate a balanced, multilingual training corpus for the full intent taxonomy.

    python src/generate_data.py

Writes `Data/synthetic_intents.csv` with columns `user_input,intent` so that
`preprocess.py` picks it up automatically alongside the real CSVs.

WHY THIS EXISTS
---------------
18 of the 31 intents in the target taxonomy have zero rows in the collected
data, and most of the rest have fewer than 25 near-identical rows. A classifier
cannot learn a class it has never seen, so the only way to cover the taxonomy is
to write training examples for it.

HOW IT AVOIDS THE ORIGINAL DATA'S MISTAKE
-----------------------------------------
The supplied CSVs repeat a handful of sentences thousands of times, which is why
`emergency` scored F1 1.00 on the test set yet failed on any real phrasing. Here
every intent gets MANY DISTINCT TEMPLATES per language (not one template with
different names substituted), and deliberately includes paraphrases that avoid
the intent's giveaway keyword - e.g. `emergency` examples that never say
"emergency", and `doctor_fee` examples that never say "fee".

This is synthetic data and must be declared as such in the report. It teaches
phrasing variety; it does not replace real recorded calls.
"""
from __future__ import annotations

import csv
import random
import sys
from pathlib import Path

from config import RAW_DATA_DIR, SEED, use_utf8_stdout

OUT_PATH = RAW_DATA_DIR / "synthetic_intents.csv"

# --------------------------------------------------------------------------
# Slot values - substituted into the templates below
# --------------------------------------------------------------------------
SLOTS = {
    "doctor": ["Dr Ahmed", "Dr Asim", "Dr Hamza", "Dr Sara", "Dr Bilal",
               "Dr Ayesha", "Dr Khan", "Dr Fatima", "Dr Usman", "Dr Zainab"],
    "doctor_ur": ["ڈاکٹر احمد", "ڈاکٹر عاصم", "ڈاکٹر حمزہ", "ڈاکٹر سارہ",
                  "ڈاکٹر بلال", "ڈاکٹر عائشہ", "ڈاکٹر خان", "ڈاکٹر فاطمہ"],
    "day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
            "tomorrow", "today", "next Monday", "this Friday"],
    "day_ur": ["پیر", "منگل", "بدھ", "جمعرات", "جمعہ", "ہفتہ", "کل", "آج",
               "اگلے پیر"],
    "day_rm": ["Monday", "Tuesday", "Friday", "Saturday", "kal", "aaj",
               "agle hafte", "parso"],
    "time": ["9 AM", "10 AM", "11 AM", "2 PM", "3 PM", "5 PM", "6 PM",
             "morning", "evening", "afternoon"],
    "time_ur": ["صبح نو بجے", "صبح دس بجے", "دوپہر دو بجے", "شام پانچ بجے",
                "شام چھ بجے", "صبح", "شام"],
    "time_rm": ["9 baje", "10 baje", "2 baje", "5 baje", "subah", "shaam",
                "dopahar"],
    "name": ["Ali", "Ahmed", "Fatima", "Ayesha", "Bilal", "Sara", "Usman",
             "Hina", "Imran", "Zainab", "Hassan", "Maryam"],
    "name_ur": ["علی", "احمد", "فاطمہ", "عائشہ", "بلال", "سارہ", "عثمان",
                "حنا", "عمران", "زینب"],
    "phone": ["0300 1234567", "0321 9876543", "0333 5551234", "0345 7778888",
              "0301 2223333", "0312 4445555", "0308 6667777"],
    "age": ["25", "30", "35", "42", "18", "55", "60", "27", "48", "33"],
    "age_ur": ["پچیس", "تیس", "پینتیس", "بیالیس", "اٹھارہ", "پچپن", "ساٹھ"],
    "spec": ["cardiologist", "dermatologist", "dentist", "child specialist",
             "eye specialist", "orthopedic surgeon", "gynecologist",
             "ENT specialist", "neurologist"],
    "spec_ur": ["امراض قلب کے ماہر", "جلد کے ماہر", "دانتوں کے ڈاکٹر",
                "بچوں کے ڈاکٹر", "آنکھوں کے ماہر", "ہڈیوں کے ڈاکٹر",
                "زنانہ امراض کی ڈاکٹر", "ناک کان گلے کے ماہر"],
}

# --------------------------------------------------------------------------
# Templates: intent -> language -> list of patterns
#   en    = English
#   rm    = Roman Urdu
#   ur    = Urdu script
# --------------------------------------------------------------------------
TEMPLATES: dict[str, dict[str, list[str]]] = {

    # ================= APPOINTMENT =================
    "book_appointment": {
        "en": [
            "I want to book an appointment with {doctor}",
            "Can I get an appointment for {day}",
            "Please schedule me with {doctor} on {day}",
            "I need to see {doctor}",
            "Book me in for {time} on {day}",
            "I would like to make an appointment",
            "Can you fix a meeting with {doctor}",
            "I need to visit the doctor {day}",
            "Please give me a slot on {day}",
            "Register my appointment with {doctor}",
            "I want to consult {doctor} about my back pain",
            "Set up a visit for me {day} at {time}",
        ],
        "rm": [
            "Mujhe {doctor} se appointment leni hai",
            "{day_rm} ka appointment mil sakta hai",
            "Mujhe doctor ko dikhana hai",
            "{doctor} ke saath meeting fix kar dein",
            "Mera appointment {day_rm} ko rakh dein",
            "Appointment banwani hai",
            "Mujhe {time_rm} ka slot chahiye",
            "Doctor se milna hai {day_rm} ko",
            "Meri appointment likh lein",
            "Kya {doctor} se waqt mil sakta hai",
        ],
        "ur": [
            "مجھے {doctor_ur} سے اپائنٹمنٹ لینی ہے",
            "کیا {day_ur} کا وقت مل سکتا ہے",
            "مجھے ڈاکٹر کو دکھانا ہے",
            "{doctor_ur} کے ساتھ ملاقات طے کر دیں",
            "میری اپائنٹمنٹ {day_ur} کو رکھ دیں",
            "مجھے اپائنٹمنٹ بنوانی ہے",
            "مجھے {time_ur} کا وقت چاہیے",
            "ڈاکٹر سے ملنا ہے",
            "میرا نام اپائنٹمنٹ میں لکھ لیں",
        ],
    },

    "check_availability": {
        "en": [
            "Is {doctor} available on {day}",
            "Are there any free slots {day}",
            "Is the doctor free at {time}",
            "Do you have an opening on {day}",
            "Can I come {day} or is it full",
            "What slots are open this week",
            "Is {doctor} taking patients {day}",
            "Any appointment available for {time}",
        ],
        "rm": [
            "Kya {doctor} {day_rm} ko available hain",
            "{day_rm} ko koi slot khali hai",
            "Doctor {time_rm} par free hain kya",
            "Kya {day_rm} ki jagah hai",
            "Is hafte koi waqt khali hai",
            "Kya main {day_rm} ko aa sakta hoon",
        ],
        "ur": [
            "کیا {doctor_ur} {day_ur} کو دستیاب ہیں",
            "{day_ur} کو کوئی وقت خالی ہے",
            "کیا ڈاکٹر صاحب {time_ur} کو فارغ ہیں",
            "کیا {day_ur} کی جگہ ہے",
            "اس ہفتے کوئی وقت خالی ہے",
        ],
    },

    "appointment_status": {
        "en": [
            "What is the status of my appointment",
            "Is my appointment confirmed",
            "Can you check my booking",
            "Do I have an appointment scheduled",
            "When is my appointment",
            "Please check my appointment details",
            "Has my booking gone through",
            "What time was my appointment again",
            "Tell me about my existing appointment",
        ],
        "rm": [
            "Meri appointment ka kya status hai",
            "Kya meri appointment confirm ho gayi",
            "Zara meri booking check karein",
            "Meri appointment kab hai",
            "Kya mera appointment lag gaya hai",
            "Meri appointment ki tafseel batayein",
        ],
        "ur": [
            "میری اپائنٹمنٹ کا کیا ہوا",
            "کیا میری اپائنٹمنٹ پکی ہو گئی",
            "ذرا میری بکنگ چیک کریں",
            "میری اپائنٹمنٹ کب ہے",
            "میری اپائنٹمنٹ کی تفصیل بتائیں",
        ],
    },

    "appointment_confirmation": {
        "en": [
            "Yes please confirm my appointment",
            "Go ahead and confirm that booking",
            "Please finalise the appointment",
            "Confirm it for {day} at {time}",
            "That works, please book it",
            "Yes lock that slot for me",
            "Confirm my visit with {doctor}",
        ],
        "rm": [
            "Ji haan appointment confirm kar dein",
            "Booking pakki kar dein",
            "{day_rm} ka appointment final kar dein",
            "Theek hai, book kar dein",
            "Haan wohi slot rakh dein",
        ],
        "ur": [
            "جی ہاں اپائنٹمنٹ پکی کر دیں",
            "بکنگ کنفرم کر دیں",
            "{day_ur} کا وقت فائنل کر دیں",
            "ٹھیک ہے، بک کر دیں",
            "ہاں وہی وقت رکھ دیں",
        ],
    },

    "cancel_appointment": {
        "en": [
            "Please cancel my appointment",
            "I want to cancel my booking",
            "I cannot come, remove my appointment",
            "Delete my appointment with {doctor}",
            "Cancel the {day} appointment please",
            "I no longer need the appointment",
            "Something came up, please cancel",
            "Take my name off the list",
        ],
        "rm": [
            "Meri appointment cancel kar dein",
            "Mujhe booking cancel karani hai",
            "Main nahi aa sakta, cancel kar dein",
            "{day_rm} wali appointment hata dein",
            "Ab zaroorat nahi, cancel kar dein",
            "Mera naam list se nikal dein",
        ],
        "ur": [
            "میری اپائنٹمنٹ منسوخ کر دیں",
            "مجھے بکنگ منسوخ کرانی ہے",
            "میں نہیں آ سکتا، منسوخ کر دیں",
            "{day_ur} والی اپائنٹمنٹ ہٹا دیں",
            "اب ضرورت نہیں، کینسل کر دیں",
            "میرا نام فہرست سے نکال دیں",
        ],
    },

    "reschedule_appointment": {
        "en": [
            "I want to reschedule my appointment",
            "Can we move my appointment",
            "Please shift my booking to another day",
            "Change my appointment to {day}",
            "I need a different slot",
            "Postpone my appointment please",
            "Can my visit be moved later",
        ],
        "rm": [
            "Meri appointment reschedule karni hai",
            "Appointment aage barha dein",
            "Meri booking kisi aur din kar dein",
            "Appointment {day_rm} par shift kar dein",
            "Mujhe doosra waqt chahiye",
        ],
        "ur": [
            "میری اپائنٹمنٹ دوبارہ مقرر کرنی ہے",
            "اپائنٹمنٹ آگے بڑھا دیں",
            "میری بکنگ کسی اور دن کر دیں",
            "اپائنٹمنٹ {day_ur} پر منتقل کر دیں",
            "مجھے دوسرا وقت چاہیے",
        ],
    },

    # ================= DOCTOR =================
    "find_doctor": {
        "en": [
            "Which doctors do you have",
            "I am looking for a {spec}",
            "Do you have a {spec} at this clinic",
            "Can you suggest a good doctor",
            "Who can I see for my problem",
            "Give me a list of your doctors",
            "I need a doctor for my child",
            "Which specialist should I see",
        ],
        "rm": [
            "Aap ke paas kaunse doctor hain",
            "Mujhe {spec} chahiye",
            "Kya yahan koi achha doctor hai",
            "Kis doctor se milna chahiye",
            "Doctors ki list de dein",
            "Bachon ka doctor hai kya",
        ],
        "ur": [
            "آپ کے پاس کون سے ڈاکٹر ہیں",
            "مجھے {spec_ur} چاہیے",
            "کیا یہاں کوئی اچھا ڈاکٹر ہے",
            "کس ڈاکٹر سے ملنا چاہیے",
            "ڈاکٹروں کی فہرست دے دیں",
            "بچوں کا ڈاکٹر ہے کیا",
        ],
    },

    "doctor_information": {
        "en": [
            "Tell me about {doctor}",
            "Who is {doctor}",
            "I want details about the doctor",
            "Give me some information on {doctor}",
            "What can you tell me about {doctor}",
            "Is {doctor} any good",
            "How long has {doctor} been practising",
            "{doctor} ke baare mein batayein",
        ],
        "rm": [
            "{doctor} ke baare mein batayein",
            "{doctor} kaun hain",
            "Doctor ki maloomat chahiye",
            "Doctor sahib kaise hain",
            "{doctor} ka experience kitna hai",
            "Doctor kitne saal se kaam kar rahe hain",
        ],
        "ur": [
            "{doctor_ur} کے بارے میں بتائیں",
            "{doctor_ur} کون ہیں",
            "ڈاکٹر کی معلومات چاہیے",
            "ڈاکٹر صاحب کیسے ہیں",
            "{doctor_ur} کا تجربہ کتنا ہے",
            "ڈاکٹر صاحب کتنے سال سے کام کر رہے ہیں",
        ],
    },

    "doctor_qualifications": {
        "en": [
            "What is {doctor} qualification",
            "What degree does the doctor have",
            "Where did {doctor} study",
            "Is the doctor MBBS or FCPS",
            "What are the doctor's credentials",
            "Tell me about the doctor's education",
            "Which university did {doctor} graduate from",
            "How many years has {doctor} been practising",
        ],
        "rm": [
            "Doctor ki qualification kya hai",
            "Doctor ne kya parha hai",
            "{doctor} ki degree kya hai",
            "Doctor kahan se parhe hain",
            "Doctor sahib ki taleem kya hai",
            "Doctor ka experience kitna hai",
            "Kitne saal ka tajurba hai",
        ],
        "ur": [
            "ڈاکٹر کی اہلیت کیا ہے",
            "ڈاکٹر نے کیا پڑھا ہے",
            "{doctor_ur} کی ڈگری کیا ہے",
            "ڈاکٹر کہاں سے پڑھے ہیں",
            "ڈاکٹر صاحب کی تعلیم کیا ہے",
            "ڈاکٹر صاحب کا تجربہ کتنا ہے",
            "کتنے سال کا تجربہ ہے",
        ],
    },

    "doctor_specialization": {
        "en": [
            "What is {doctor} specialty",
            "Which field does the doctor specialise in",
            "Is {doctor} a heart specialist",
            "What kind of doctor is {doctor}",
            "Does {doctor} treat skin problems",
            "Which diseases does the doctor handle",
            "Is the doctor a {spec}",
        ],
        "rm": [
            "Doctor ki specialty kya hai",
            "{doctor} kis cheez ke specialist hain",
            "Kya doctor dil ke specialist hain",
            "Doctor kis type ke hain",
            "Kya yeh {spec} hain",
            "Doctor kaunsi bimari dekhte hain",
        ],
        "ur": [
            "ڈاکٹر کی تخصیص کیا ہے",
            "{doctor_ur} کس چیز کے ماہر ہیں",
            "کیا ڈاکٹر دل کے ماہر ہیں",
            "ڈاکٹر کس قسم کے ہیں",
            "کیا یہ {spec_ur} ہیں",
            "ڈاکٹر کون سی بیماری دیکھتے ہیں",
        ],
    },

    "doctor_fee": {
        "en": [
            "What is the consultation fee",
            "How much do I have to pay",
            "What are the charges for a visit",
            "How much does {doctor} charge",
            "What is the cost of an appointment",
            "Is it expensive to see the doctor",
            "Tell me the price of a consultation",
            "How much money should I bring",
            "What will this visit cost me",
        ],
        "rm": [
            "Fees kitni hai",
            "Kitne paise dene hain",
            "{doctor} ki fees kya hai",
            "Consultation ka kitna kharcha hai",
            "Kitna kharch aayega",
            "Kitne rupay lagenge",
            "Visit ka rate kya hai",
        ],
        "ur": [
            "فیس کتنی ہے",
            "کتنے پیسے دینے ہیں",
            "{doctor_ur} کی فیس کیا ہے",
            "چیک اپ کا کتنا خرچہ ہے",
            "کتنا خرچ آئے گا",
            "کتنے روپے لگیں گے",
            "معائنے کی قیمت کیا ہے",
        ],
    },

    "doctor_unavailable": {
        "en": [
            "Is the doctor on leave",
            "I heard {doctor} is not coming",
            "Is {doctor} out of station",
            "Why is the doctor not available",
            "Is the doctor off {day}",
            "Has {doctor} gone on holiday",
            "The doctor is not sitting today right",
        ],
        "rm": [
            "Kya doctor chutti par hain",
            "{doctor} nahi aa rahe kya",
            "Doctor sahib bahar gaye hain",
            "Doctor {day_rm} ko nahi baithte",
            "Kya doctor available nahi hain",
        ],
        "ur": [
            "کیا ڈاکٹر چھٹی پر ہیں",
            "{doctor_ur} نہیں آ رہے کیا",
            "ڈاکٹر صاحب باہر گئے ہیں",
            "ڈاکٹر {day_ur} کو نہیں بیٹھتے",
            "کیا ڈاکٹر دستیاب نہیں ہیں",
        ],
    },

    # ================= CLINIC =================
    "clinic_location": {
        "en": [
            "Where is the clinic located",
            "What is the clinic address",
            "How do I reach your clinic",
            "Give me the location please",
            "Which area is the clinic in",
            "Send me the address",
            "Is the clinic near the main bazaar",
            "How far is the clinic from the station",
        ],
        "rm": [
            "Clinic kahan hai",
            "Clinic ka pata batayein",
            "Address kya hai",
            "Kaise pohanchna hai",
            "Kis ilaqe mein hai clinic",
            "Location bhej dein",
        ],
        "ur": [
            "کلینک کہاں ہے",
            "کلینک کا پتہ بتائیں",
            "ایڈریس کیا ہے",
            "وہاں کیسے پہنچنا ہے",
            "کس علاقے میں ہے کلینک",
            "لوکیشن بھیج دیں",
        ],
    },

    "clinic_timing": {
        "en": [
            "What are the clinic timings",
            "At what time does the clinic open",
            "When does the clinic close",
            "What are your opening hours",
            "Till what time is the doctor sitting",
            "Are you open in the evening",
            "What time should I come",
            "From when to when are you open",
        ],
        "rm": [
            "Clinic ke auqat kya hain",
            "Clinic kitne baje khulta hai",
            "Kitne baje band hota hai",
            "Doctor sahib kab baithte hain",
            "Shaam ko khula hota hai kya",
            "Kis waqt aaun",
        ],
        "ur": [
            "کلینک کے اوقات کیا ہیں",
            "کلینک کتنے بجے کھلتا ہے",
            "کتنے بجے بند ہوتا ہے",
            "ڈاکٹر صاحب کب بیٹھتے ہیں",
            "شام کو کھلا ہوتا ہے کیا",
            "کس وقت آؤں",
        ],
    },

    "clinic_closed": {
        "en": [
            "Is the clinic closed today",
            "Are you open on Sunday",
            "Is the clinic shut on public holidays",
            "Will the clinic be closed {day}",
            "Is there a holiday this week",
            "Are you closed for Eid",
        ],
        "rm": [
            "Kya clinic aaj band hai",
            "Sunday ko khula hota hai",
            "Chutti hai kya aaj",
            "{day_rm} ko clinic band rahega",
            "Eid par band hai kya",
        ],
        "ur": [
            "کیا کلینک آج بند ہے",
            "اتوار کو کھلا ہوتا ہے",
            "آج چھٹی ہے کیا",
            "{day_ur} کو کلینک بند رہے گا",
            "عید پر بند ہے کیا",
        ],
    },

    # ================= PATIENT INFORMATION =================
    "provide_patient_name": {
        "en": [
            "My name is {name}",
            "It is {name}",
            "I am {name}",
            "You can write {name}",
            "The patient name is {name}",
            "Name is {name}",
            "Please note it as {name}",
            "{name}, that is my name",
        ],
        "rm": [
            "Mera naam {name} hai",
            "{name} likh lein",
            "Main {name} hoon",
            "Patient ka naam {name} hai",
            "Naam {name} hai",
        ],
        "ur": [
            "میرا نام {name_ur} ہے",
            "{name_ur} لکھ لیں",
            "میں {name_ur} ہوں",
            "مریض کا نام {name_ur} ہے",
            "نام {name_ur} ہے",
        ],
    },

    "provide_patient_phone": {
        "en": [
            "My number is {phone}",
            "You can call me on {phone}",
            "Phone number {phone}",
            "Contact me at {phone}",
            "It is {phone}",
            "Note down {phone}",
            "My mobile is {phone}",
        ],
        "rm": [
            "Mera number {phone} hai",
            "{phone} par call kar lein",
            "Phone number {phone}",
            "Mobile {phone} hai",
            "Number likh lein {phone}",
        ],
        "ur": [
            "میرا نمبر {phone} ہے",
            "{phone} پر کال کر لیں",
            "فون نمبر {phone}",
            "موبائل {phone} ہے",
            "نمبر لکھ لیں {phone}",
        ],
    },

    "provide_patient_age": {
        "en": [
            "I am {age} years old",
            "My age is {age}",
            "Age {age}",
            "The patient is {age}",
            "He is {age} years old",
            "She is {age}",
            "I am {age}",
        ],
        "rm": [
            "Meri umar {age} saal hai",
            "Umar {age} hai",
            "Main {age} saal ka hoon",
            "Patient {age} saal ka hai",
            "{age} saal",
        ],
        "ur": [
            "میری عمر {age_ur} سال ہے",
            "عمر {age_ur} سال",
            "میں {age_ur} سال کا ہوں",
            "مریض {age_ur} سال کا ہے",
        ],
    },

    "provide_patient_gender": {
        "en": [
            "I am male",
            "I am female",
            "The patient is a man",
            "It is for a woman",
            "Gender male",
            "Female patient",
            "He is a boy",
            "She is a girl",
        ],
        "rm": [
            "Main mard hoon",
            "Main aurat hoon",
            "Patient mard hai",
            "Yeh khatoon ke liye hai",
            "Larka hai",
            "Larki hai",
        ],
        "ur": [
            "میں مرد ہوں",
            "میں عورت ہوں",
            "مریض مرد ہے",
            "یہ خاتون کے لیے ہے",
            "لڑکا ہے",
            "لڑکی ہے",
        ],
    },

    # ================= APPOINTMENT MODIFICATION =================
    "change_doctor": {
        "en": [
            "Can I see a different doctor instead",
            "I want to change my doctor to {doctor}",
            "Please switch me to another doctor",
            "Can you put me with {doctor} instead",
            "I would rather see a different specialist",
            "Change the doctor for my appointment",
        ],
        "rm": [
            "Doctor badal dein",
            "Mujhe {doctor} ke paas kar dein",
            "Kisi aur doctor se milna hai",
            "Doctor change kar dein",
            "Doosre doctor ke saath kar dein",
        ],
        "ur": [
            "ڈاکٹر بدل دیں",
            "مجھے {doctor_ur} کے پاس کر دیں",
            "کسی اور ڈاکٹر سے ملنا ہے",
            "ڈاکٹر تبدیل کر دیں",
            "دوسرے ڈاکٹر کے ساتھ کر دیں",
        ],
    },

    "change_date": {
        "en": [
            "Can we change the date to {day}",
            "Please move it to {day}",
            "I want a different date",
            "Shift the day to {day}",
            "Change my appointment date",
            "Make it {day} instead",
        ],
        "rm": [
            "Date badal dein",
            "{day_rm} kar dein",
            "Din change kar dein",
            "Mujhe doosri date chahiye",
            "Tareekh badal dein",
        ],
        "ur": [
            "تاریخ بدل دیں",
            "{day_ur} کر دیں",
            "دن تبدیل کر دیں",
            "مجھے دوسری تاریخ چاہیے",
        ],
    },

    "change_time": {
        "en": [
            "Can we change the time to {time}",
            "Please make it {time} instead",
            "I want a later time",
            "Can I come earlier",
            "Change the timing of my appointment",
            "Shift it to the {time}",
        ],
        "rm": [
            "Time badal dein",
            "{time_rm} kar dein",
            "Mujhe baad ka waqt chahiye",
            "Pehle aa sakta hoon kya",
            "Waqt change kar dein",
        ],
        "ur": [
            "وقت بدل دیں",
            "{time_ur} کر دیں",
            "مجھے بعد کا وقت چاہیے",
            "کیا میں پہلے آ سکتا ہوں",
            "ٹائم تبدیل کر دیں",
        ],
    },

    # ================= CONVERSATION =================
    "greeting": {
        "en": [
            "Hello", "Hi there", "Good morning", "Good afternoon",
            "Good evening", "Hello, is this the clinic",
            "Hi, can you hear me", "Assalam o alaikum",
            "Hello, I need some help", "Hey there",
        ],
        "rm": [
            "Assalam o alaikum", "Salam", "Hello ji", "Kya haal hai",
            "Adab", "Salam, clinic hai kya", "Hello, sun rahe hain",
        ],
        "ur": [
            "السلام علیکم", "سلام", "ہیلو", "آداب",
            "السلام علیکم، کلینک ہے کیا", "کیا حال ہے",
        ],
    },

    "goodbye": {
        "en": [
            "Goodbye", "Bye", "See you later", "That is all, bye",
            "Alright goodbye", "Khuda hafiz", "Talk to you later",
            "I will hang up now", "Okay bye bye",
        ],
        "rm": [
            "Khuda hafiz", "Allah hafiz", "Bye ji", "Chalta hoon",
            "Theek hai, khuda hafiz", "Ok bye",
        ],
        "ur": [
            "خدا حافظ", "اللہ حافظ", "الوداع", "چلتا ہوں",
            "ٹھیک ہے، خدا حافظ",
        ],
    },

    "confirm": {
        "en": [
            "Yes", "Yes please", "That is correct", "Sure", "Okay",
            "Right", "Exactly", "Yes that is fine", "Correct",
            "Sounds good", "Absolutely", "Ji haan",
        ],
        "rm": [
            "Ji haan", "Haan", "Bilkul", "Theek hai", "Sahi hai",
            "Ji bilkul", "Haan ji", "Achha theek",
        ],
        "ur": [
            "جی ہاں", "ہاں", "بالکل", "ٹھیک ہے", "درست ہے",
            "جی بالکل", "ہاں جی",
        ],
    },

    "deny": {
        "en": [
            "No", "No thanks", "That is not correct", "Nope",
            "No that is wrong", "I do not want that", "Not really",
            "No, cancel that", "That is not what I said",
        ],
        "rm": [
            "Nahi", "Ji nahi", "Bilkul nahi", "Yeh sahi nahi hai",
            "Nahi chahiye", "Aisa nahi kaha maine",
        ],
        "ur": [
            "نہیں", "جی نہیں", "بالکل نہیں", "یہ درست نہیں",
            "نہیں چاہیے", "میں نے ایسا نہیں کہا",
        ],
    },

    "help": {
        "en": [
            "Can you help me", "I need some help", "What can you do",
            "How does this work", "I do not know what to do",
            "Please guide me", "What are my options",
            "Help me please", "Can you assist me",
        ],
        "rm": [
            "Meri madad karein", "Mujhe madad chahiye",
            "Aap kya kar sakte hain", "Yeh kaise kaam karta hai",
            "Mujhe samajh nahi aa raha", "Rehnumai karein",
        ],
        "ur": [
            "میری مدد کریں", "مجھے مدد چاہیے",
            "آپ کیا کر سکتے ہیں", "یہ کیسے کام کرتا ہے",
            "مجھے سمجھ نہیں آ رہا", "رہنمائی کریں",
        ],
    },

    "repeat_information": {
        "en": [
            "Can you repeat that", "Say that again please",
            "I did not catch that", "Pardon", "Sorry what was that",
            "Please say it once more", "Come again",
            "I could not hear you", "Repeat please",
        ],
        "rm": [
            "Dobara kahein", "Phir se batayein", "Samajh nahi aaya",
            "Zara dobara boliye", "Awaz nahi aa rahi",
            "Ek baar phir kahein",
        ],
        "ur": [
            "دوبارہ کہیں", "پھر سے بتائیں", "سمجھ نہیں آیا",
            "ذرا دوبارہ بولیے", "آواز نہیں آ رہی",
            "ایک بار پھر کہیں",
        ],
    },

    "unclear_request": {
        "en": [
            "Um I am not sure", "Hmm", "Wait a second",
            "Actually never mind", "I forgot what I wanted to say",
            "Something about the thing", "Uh let me think",
            "I do not know", "Hold on",
        ],
        "rm": [
            "Pata nahi", "Ruko zara", "Bhool gaya kya kehna tha",
            "Hmm sochne do", "Chhorein koi baat nahi",
            "Kuch samajh nahi aa raha",
        ],
        "ur": [
            "پتہ نہیں", "ذرا رکیں", "بھول گیا کیا کہنا تھا",
            "چھوڑیں کوئی بات نہیں", "کچھ سمجھ نہیں آ رہا",
        ],
    },

    "thank_you": {
        "en": [
            "Thank you", "Thanks a lot", "Thank you so much",
            "Much appreciated", "Thanks for your help",
            "That is very kind of you", "Shukriya", "Great, thanks",
        ],
        "rm": [
            "Shukriya", "Bohot shukriya", "Thanks ji",
            "Aap ka bohot shukriya", "Meherbani",
            "Bohot meherbani aap ki",
        ],
        "ur": [
            "شکریہ", "بہت شکریہ", "آپ کا بہت شکریہ",
            "مہربانی", "بہت نوازش",
        ],
    },

    # ================= SAFETY-CRITICAL =================
    # Deliberately kept even though it is absent from the requested list:
    # without it, a medical emergency is classified as `book_appointment`.
    # Note how few of these contain the word "emergency" - that was exactly
    # the flaw in the original data.
    "emergency": {
        "en": [
            "This is an emergency",
            "My father collapsed, we need help now",
            "She is unconscious, what do I do",
            "He is bleeding badly",
            "I am having severe chest pain",
            "The patient cannot breathe",
            "Please send help immediately",
            "It is very urgent, he is not responding",
            "My child has a very high fever and is shaking",
            "I think it is a heart attack",
            "We need a doctor right now, not tomorrow",
            "There has been an accident",
        ],
        "rm": [
            "Emergency hai",
            "Mere abbu gir gaye hain, abhi madad chahiye",
            "Woh behosh ho gayi hai",
            "Bohot khoon beh raha hai",
            "Seene mein bohot dard ho raha hai",
            "Saans nahi aa rahi",
            "Foran doctor chahiye",
            "Bohot urgent hai, jawab nahi de raha",
            "Bachay ko tez bukhar hai aur jhatke lag rahe hain",
            "Accident ho gaya hai",
        ],
        "ur": [
            "یہ ایمرجنسی ہے",
            "میرے ابو گر گئے ہیں، ابھی مدد چاہیے",
            "وہ بے ہوش ہو گئی ہے",
            "بہت خون بہہ رہا ہے",
            "سینے میں شدید درد ہو رہا ہے",
            "سانس نہیں آ رہی",
            "فوراً ڈاکٹر چاہیے",
            "بہت ہنگامی صورتحال ہے",
            "بچے کو تیز بخار ہے اور جھٹکے لگ رہے ہیں",
            "حادثہ ہو گیا ہے",
        ],
    },
}

# --------------------------------------------------------------------------
# Merge in the extra conversational templates.
#
# The short conversational intents have no {slots}, so generation saturates at
# the number of templates written. The first corpus gave them 14-17 rows each
# and `goodbye` / `help` scored F1 0.00 on the test set. `extra_templates.py`
# adds more distinct phrasings for exactly those intents.
# --------------------------------------------------------------------------
from extra_templates import EXTRA_TEMPLATES  # noqa: E402

for _intent, _langs in EXTRA_TEMPLATES.items():
    for _lang, _items in _langs.items():
        TEMPLATES.setdefault(_intent, {}).setdefault(_lang, []).extend(_items)


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
SLOT_FOR_LANG = {
    "en": {"doctor": "doctor", "day": "day", "time": "time", "name": "name",
           "age": "age", "spec": "spec", "phone": "phone"},
    "rm": {"doctor": "doctor", "day_rm": "day_rm", "time_rm": "time_rm",
           "name": "name", "age": "age", "spec": "spec", "phone": "phone"},
    "ur": {"doctor_ur": "doctor_ur", "day_ur": "day_ur", "time_ur": "time_ur",
           "name_ur": "name_ur", "age_ur": "age_ur", "spec_ur": "spec_ur",
           "phone": "phone"},
}


def fill(template: str, rng: random.Random) -> str:
    """Substitute every {slot} in a template with a random value."""
    out = template
    for key, values in SLOTS.items():
        token = "{" + key + "}"
        while token in out:
            out = out.replace(token, rng.choice(values), 1)
    return out


def generate(per_language: int = 22) -> list[tuple[str, str]]:
    """
    Expand the templates into a deduplicated list of (text, intent) rows.

    `per_language` is the target number of DISTINCT sentences per intent per
    language. Templates without slots can only produce one sentence each, so
    the real count is capped by template variety - which is the point.
    """
    rng = random.Random(SEED)
    rows: list[tuple[str, str]] = []

    for intent, langs in TEMPLATES.items():
        for lang, templates in langs.items():
            seen: set[str] = set()
            # Try generously; slot-free templates saturate immediately.
            for _ in range(per_language * 12):
                if len(seen) >= per_language:
                    break
                text = fill(rng.choice(templates), rng)
                if text not in seen:
                    seen.add(text)
                    rows.append((text, intent))
            # Guarantee every hand-written template appears at least once.
            for template in templates:
                text = fill(template, rng)
                if text not in seen:
                    seen.add(text)
                    rows.append((text, intent))
    return rows


def main() -> None:
    use_utf8_stdout()
    rows = generate()

    # Real Pakistani phrasings and STT noise, measured as gaps in the model.
    # These are hand-written rather than template-generated: the whole point is
    # that they do NOT follow a template, which is why the model missed them.
    from natural_phrases import natural_rows
    natural = natural_rows()
    rows.extend(natural)

    # Roman-Urdu spelling / abbreviation variants. The model had learned the
    # SPELLING of a booking request, not its meaning, so "mje docter ke pas
    # jana h" scored change_doctor 0.41.
    from roman_urdu_augment import augmented_rows
    augmented = augmented_rows()
    rows.extend(augmented)

    # Emergencies phrased like bookings ("seene mein dard hai, doctor ke paas
    # jana hai"). Without these the model scored one book_appointment 1.00.
    from emergency_phrases import emergency_rows
    emergency = emergency_rows()
    rows.extend(emergency)

    # Boundaries the system audit measured as confused (see the comments in
    # contrast_phrases.py), plus one labelling contradiction corrected.
    from contrast_phrases import RELABEL, contrast_rows
    contrast = contrast_rows()
    rows.extend(contrast)
    rows = [(text, RELABEL.get(text, intent)) for text, intent in rows]

    # The challenge set is for evaluation only. Template expansion reproduces
    # common phrasings word for word, so filter rather than trust the templates.
    from challenge_set import is_challenge
    before = len(rows)
    rows = [(text, intent) for text, intent in rows if not is_challenge(text)]
    held_out = before - len(rows)

    # Global dedupe: an identical sentence must not carry two intents.
    by_text: dict[str, str] = {}
    conflicts = 0
    for text, intent in rows:
        if text in by_text and by_text[text] != intent:
            conflicts += 1
            continue
        by_text[text] = intent

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["user_input", "intent"])
        for text, intent in sorted(by_text.items(), key=lambda kv: (kv[1], kv[0])):
            writer.writerow([text, intent])

    counts: dict[str, int] = {}
    for intent in by_text.values():
        counts[intent] = counts.get(intent, 0) + 1

    print("=" * 68)
    print("SYNTHETIC DATA GENERATED")
    print("=" * 68)
    for intent in sorted(counts):
        print(f"   {intent:28s} {counts[intent]:4d}")
    print("-" * 68)
    print(f"   {'TOTAL':28s} {len(by_text):4d} unique sentences "
          f"across {len(counts)} intents")
    print(f"   {'(of which hand-written)':28s} {len(natural):4d} natural "
          f"phrasings / STT variants")
    print(f"   {'(of which augmented)':28s} {len(augmented):4d} Roman-Urdu "
          f"spelling variants")
    print(f"   {'(of which emergency)':28s} {len(emergency):4d} booking-shaped "
          f"emergencies")
    print(f"   {'(of which contrast)':28s} {len(contrast):4d} audit-driven "
          f"boundary phrasings")
    print(f"   {'held out':28s} {held_out:4d} rows matching the evaluation sets")
    if conflicts:
        print(f"   dropped {conflicts} rows whose text collided across intents")
    print(f"\nwritten to {OUT_PATH}")


if __name__ == "__main__":
    main()
