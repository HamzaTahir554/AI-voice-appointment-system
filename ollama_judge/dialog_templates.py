"""
Spoken templates for Dialog Manager replies that carry facts.

The Dialog Manager writes one English sentence per reply. On a Roman-Urdu or
Urdu call, voice_pipeline.py re-renders that reply from these templates, filled
with the SAME values the English sentence was built from (doctor, date, time,
slots, fee, address ...). No template adds a fact of its own.

Found by the system audit: appointment look-ups, cancel and reschedule
confirmations, alternative slots, doctor leave, emergencies and doctor / clinic
answers all reached Roman-Urdu callers in English, and the doctor-leave
explanation was replaced by a bare "which day?".

Every entry has at least two phrasings so repeated questions vary.

Placeholders: {doctor} {date} {time} {slots} {fee} {qualification}
{experience} {specialization} {clinic} {address} {phone} {doctors} {listing}
"""
from __future__ import annotations

EN, RU, UR = "english", "roman_urdu", "urdu"

DIALOG_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "confirm_cancel": {
        EN: ["You have an appointment with {doctor}, {date} at {time}. Shall I cancel it?",
             "That is {doctor}, {date} at {time}. Do you want it cancelled?"],
        RU: ["Aap ki appointment {doctor} ke saath {date} {time} hai. Cancel kar doon?",
             "{doctor} ke saath {date} {time} wali appointment mili hai. Kya cancel karni hai?"],
        UR: ["آپ کی اپائنٹمنٹ {doctor} کے ساتھ {date} {time} ہے۔ منسوخ کر دوں؟",
             "{doctor} کے ساتھ {date} {time} والی اپائنٹمنٹ ملی ہے۔ کیا منسوخ کرنی ہے؟"],
    },
    "confirm_reschedule": {
        EN: ["{doctor} is free {date} at {time}. Shall I move your appointment there?",
             "I can move it to {date} at {time} with {doctor}. Shall I go ahead?"],
        RU: ["{doctor} {date} {time} free hain. Appointment wahan shift kar doon?",
             "{doctor} ke paas {date} {time} ka slot khali hai. Appointment is par kar doon?"],
        UR: ["{doctor} {date} {time} فارغ ہیں۔ اپائنٹمنٹ وہاں منتقل کر دوں؟",
             "{doctor} کے پاس {date} {time} کا وقت خالی ہے۔ اپائنٹمنٹ اس پر کر دوں؟"],
    },
    "appointment_was_cancelled": {
        EN: ["Your appointment with {doctor}, {date}, was cancelled.",
             "The appointment with {doctor} for {date} has been cancelled."],
        RU: ["{doctor} ke saath {date} wali appointment cancel ho chuki hai.",
             "Aap ki {date} wali appointment {doctor} ke saath cancel hai."],
        UR: ["{doctor} کے ساتھ {date} والی اپائنٹمنٹ منسوخ ہو چکی ہے۔",
             "آپ کی {date} والی اپائنٹمنٹ {doctor} کے ساتھ منسوخ ہے۔"],
    },
    "no_schedule": {
        EN: ["{doctor} does not hold clinic {date}. Would another day suit you?",
             "{doctor} is not in {date}. Shall I look at another day?"],
        RU: ["{doctor} {date} clinic mein nahi baithte. Koi aur din dekh loon?",
             "{date} {doctor} ka clinic nahi hota. Kisi aur din ka dekhein?"],
        UR: ["{doctor} {date} کلینک میں نہیں بیٹھتے۔ کوئی اور دن دیکھ لوں؟",
             "{date} {doctor} کا کلینک نہیں ہوتا۔ کسی اور دن کا دیکھیں؟"],
    },
    "date_in_past": {
        EN: ["That day has already passed. Which upcoming day would you like?",
             "That date is behind us. Which day coming up suits you?"],
        RU: ["Yeh din to guzar chuka hai. Aane wala konsa din theek rahega?",
             "Woh tareekh nikal chuki hai. Aage konsa din chahiye?"],
        UR: ["یہ دن تو گزر چکا ہے۔ آنے والا کون سا دن ٹھیک رہے گا؟",
             "وہ تاریخ نکل چکی ہے۔ آگے کون سا دن چاہیے؟"],
    },
    "emergency": {
        EN: ["This sounds like an emergency. Please call emergency services or go to "
             "the nearest hospital now. {clinic} is at {address}, phone {phone}.",
             "Please do not wait - this sounds like an emergency. Go to the nearest "
             "hospital or call emergency services. {clinic}: {address}, {phone}."],
        RU: ["Yeh emergency lag rahi hai. Foran emergency services ko call karein ya "
             "qareebi hospital jayein. {clinic} {address} par hai, number {phone} hai.",
             "Please der na karein, yeh emergency hai. Abhi qareebi hospital jayein ya "
             "emergency services ko call karein. {clinic} ka pata {address}, phone {phone}."],
        UR: ["یہ ایمرجنسی لگ رہی ہے۔ فوراً ایمرجنسی سروسز کو کال کریں یا قریبی ہسپتال "
             "جائیں۔ {clinic} {address} پر ہے، نمبر {phone} ہے۔",
             "براہ کرم دیر نہ کریں، یہ ایمرجنسی ہے۔ ابھی قریبی ہسپتال جائیں یا ایمرجنسی "
             "سروسز کو کال کریں۔ {clinic} کا پتہ {address}، فون {phone}۔"],
    },
    "emergency_no_clinic": {
        EN: ["This sounds like an emergency. Please call emergency services or go to "
             "the nearest hospital now.",
             "Please do not wait - go to the nearest hospital or call emergency services."],
        RU: ["Yeh emergency lag rahi hai. Foran emergency services ko call karein ya "
             "qareebi hospital jayein.",
             "Please der na karein. Abhi qareebi hospital jayein ya emergency services "
             "ko call karein."],
        UR: ["یہ ایمرجنسی لگ رہی ہے۔ فوراً ایمرجنسی سروسز کو کال کریں یا قریبی ہسپتال جائیں۔",
             "براہ کرم دیر نہ کریں۔ ابھی قریبی ہسپتال جائیں یا ایمرجنسی سروسز کو کال کریں۔"],
    },
    "doctor_fee": {
        EN: ["The consultation fee for {doctor} is {fee} rupees.",
             "A consultation with {doctor} is {fee} rupees."],
        RU: ["{doctor} ki consultation fee {fee} rupay hai.",
             "{doctor} ko dikhane ki fee {fee} rupay hai."],
        UR: ["{doctor} کی فیس {fee} روپے ہے۔",
             "{doctor} کو دکھانے کی فیس {fee} روپے ہے۔"],
    },
    "doctor_qualification": {
        EN: ["{doctor} holds {qualification} and has {experience} years of experience.",
             "{doctor} is qualified with {qualification}, with {experience} years in practice."],
        RU: ["{doctor} ki qualification {qualification} hai, aur {experience} saal ka tajurba hai.",
             "{doctor} ne {qualification} kiya hai aur {experience} saal se practice kar rahe hain."],
        UR: ["{doctor} کی تعلیم {qualification} ہے، اور {experience} سال کا تجربہ ہے۔",
             "{doctor} نے {qualification} کیا ہے اور {experience} سال سے پریکٹس کر رہے ہیں۔"],
    },
    "doctor_specialization": {
        EN: ["{doctor} is a {specialization}.",
             "{doctor} specialises as a {specialization}."],
        RU: ["{doctor} {specialization} hain.",
             "{doctor} ki specialty {specialization} hai."],
        UR: ["{doctor} {specialization} ہیں۔",
             "{doctor} کی مہارت {specialization} ہے۔"],
    },
    "doctor_profile": {
        EN: ["{doctor} is a {specialization} with {experience} years of experience at "
             "{clinic}. The consultation fee is {fee} rupees.",
             "{doctor}: {specialization}, {experience} years of experience, sees patients "
             "at {clinic}. The fee is {fee} rupees."],
        RU: ["{doctor} {specialization} hain, {experience} saal ka tajurba hai, aur {clinic} "
             "mein baithte hain. Fee {fee} rupay hai.",
             "{doctor} {clinic} mein {specialization} hain. {experience} saal ka tajurba, "
             "aur fee {fee} rupay."],
        UR: ["{doctor} {specialization} ہیں، {experience} سال کا تجربہ ہے، اور {clinic} "
             "میں بیٹھتے ہیں۔ فیس {fee} روپے ہے۔",
             "{doctor} {clinic} میں {specialization} ہیں۔ {experience} سال کا تجربہ، اور "
             "فیس {fee} روپے۔"],
    },
    "doctor_list": {
        EN: ["Our doctors are {doctors}. Who would you like to see?",
             "At the clinic we have {doctors}. Which doctor would you like?"],
        RU: ["Hamare clinic mein {doctors} hain. Aap kis doctor ko dikhana chahenge?",
             "{doctors} hamare paas baithte hain. Kis doctor se milna hai?"],
        UR: ["ہمارے کلینک میں {doctors} ہیں۔ آپ کس ڈاکٹر کو دکھانا چاہیں گے؟",
             "{doctors} ہمارے پاس بیٹھتے ہیں۔ کس ڈاکٹر سے ملنا ہے؟"],
    },
    "clinic_address": {
        EN: ["{clinic} is at {address}.",
             "You will find {clinic} at {address}."],
        RU: ["{clinic} {address} par hai.",
             "{clinic} ka pata {address} hai."],
        UR: ["{clinic} {address} پر ہے۔",
             "{clinic} کا پتہ {address} ہے۔"],
    },
    "clinic_hours": {
        EN: ["{clinic} is open Monday to Saturday and closed on Sunday. The number is {phone}.",
             "{clinic} opens Monday to Saturday; Sunday is closed. You can call {phone}."],
        RU: ["{clinic} Monday se Saturday khula hota hai, Sunday ko band. Number {phone} hai.",
             "{clinic} Itwaar ko band hota hai, baqi din khula. Phone {phone} hai."],
        UR: ["{clinic} پیر سے ہفتہ کھلا ہوتا ہے، اتوار کو بند۔ نمبر {phone} ہے۔",
             "{clinic} اتوار کو بند ہوتا ہے، باقی دن کھلا۔ فون {phone} ہے۔"],
    },
    "clinic_general": {
        EN: ["{clinic} is at {address}. You can call them on {phone}.",
             "{clinic}, {address}. Their number is {phone}."],
        RU: ["{clinic} {address} par hai. Number {phone} hai.",
             "{clinic} ka pata {address} hai, aur phone {phone}."],
        UR: ["{clinic} {address} پر ہے۔ نمبر {phone} ہے۔",
             "{clinic} کا پتہ {address} ہے، اور فون {phone}۔"],
    },
    "which_appointment": {
        EN: ["You have {listing}. Which one do you mean?",
             "I can see {listing}. Which one should I use?"],
        RU: ["Aap ki yeh appointments hain: {listing}. Kaunsi wali?",
             "Mujhe {listing} nazar aa rahi hain. Kis ki baat kar rahe hain?"],
        UR: ["آپ کی یہ اپائنٹمنٹس ہیں: {listing}۔ کون سی والی؟",
             "مجھے {listing} نظر آ رہی ہیں۔ کس کی بات کر رہے ہیں؟"],
    },
    "aborted": {
        EN: ["No problem, I have not changed anything. What would you like to do?",
             "Alright, nothing has been changed. How else can I help?"],
        RU: ["Koi baat nahi, maine kuch change nahi kiya. Aur kya karna hai?",
             "Theek hai, kuch nahi badla. Aur kaise madad karoon?"],
        UR: ["کوئی بات نہیں، میں نے کچھ تبدیل نہیں کیا۔ اور کیا کرنا ہے؟",
             "ٹھیک ہے، کچھ نہیں بدلا۔ اور کیسے مدد کروں؟"],
    },
    "say_yes_or_no": {
        EN: ["Sorry, shall I go ahead? Just say yes or no.",
             "Should I do it? A yes or no is fine."],
        RU: ["Maazrat, kar doon? Bas haan ya nahi bata dein.",
             "Ji, aage barhoon? Haan ya nahi?"],
        UR: ["معذرت، کر دوں؟ بس ہاں یا نہیں بتا دیں۔",
             "جی، آگے بڑھوں؟ ہاں یا نہیں؟"],
    },
    "help_menu": {
        EN: ["I can book, cancel or change an appointment, and tell you about our doctors, "
             "fees and clinic timings. What would you like?",
             "You can ask me to book, cancel or move an appointment, or ask about a doctor "
             "or the clinic. How can I help?"],
        RU: ["Main appointment book, cancel ya change kar sakti hoon, aur doctors, fees ya "
             "clinic timing bata sakti hoon. Aap kya karna chahenge?",
             "Aap appointment book, cancel ya shift karwa sakte hain, ya kisi doctor ya "
             "clinic ke baare mein pooch sakte hain. Batayein kaise madad karoon?"],
        UR: ["میں اپائنٹمنٹ بک، منسوخ یا تبدیل کر سکتی ہوں، اور ڈاکٹروں، فیس یا کلینک کے "
             "اوقات بتا سکتی ہوں۔ آپ کیا کرنا چاہیں گے؟",
             "آپ اپائنٹمنٹ بک، منسوخ یا منتقل کروا سکتے ہیں، یا کسی ڈاکٹر یا کلینک کے بارے "
             "میں پوچھ سکتے ہیں۔ بتائیں کیسے مدد کروں؟"],
    },
}
