"""
Additional templates for the short conversational intents.

WHY THIS FILE EXISTS
--------------------
The conversational intents (`goodbye`, `help`, `thank_you`, `confirm`, ...) are
fixed phrases with no {slots}, so data generation saturates at however many
templates are written - substituting a doctor name cannot invent a new way of
saying "thank you". The first generated corpus gave them only 14-17 rows each,
and on the test set `goodbye` and `help` both scored **F1 0.00**, collapsing
mostly into `confirm` ("Okay bye" looks a lot like "Okay").

The only cure is more genuinely distinct phrasings, which is what this file
adds. `generate_data.py` merges these into its main TEMPLATES table.
"""
from __future__ import annotations

EXTRA_TEMPLATES: dict[str, dict[str, list[str]]] = {

    "goodbye": {
        "en": ["Okay I will hang up", "That is everything, thank you bye",
               "Alright then, see you", "I am done, goodbye",
               "Nothing else, bye", "Take care, bye", "Catch you later",
               "Right, I will go now", "Good night",
               "See you at the clinic", "That is all I needed, bye",
               "Ending the call now", "Okay then, khuda hafiz"],
        "rm": ["Acha ji khuda hafiz", "Bas itna hi tha, allah hafiz",
               "Theek hai phir milte hain", "Main phone rakhta hoon",
               "Aur kuch nahi, bye", "Shab bakhair", "Chalo ji allah hafiz",
               "Bas ho gaya, khuda hafiz", "Milte hain clinic par",
               "Acha ji chalta hoon"],
        "ur": ["اچھا جی خدا حافظ", "بس اتنا ہی تھا، اللہ حافظ",
               "ٹھیک ہے پھر ملتے ہیں", "میں فون رکھتا ہوں",
               "اور کچھ نہیں، الوداع", "شب بخیر", "چلو جی اللہ حافظ",
               "بس ہو گیا، خدا حافظ", "کلینک پر ملتے ہیں",
               "اچھا جی چلتا ہوں"],
    },

    "help": {
        "en": ["I need assistance", "Can you guide me please",
               "What services do you offer", "How do I book with you",
               "I am confused, what should I do",
               "Tell me what I can ask you", "What all can you help with",
               "I am new here, how does this work",
               "Explain the process to me", "Can you walk me through it",
               "I do not understand the system", "What should I say",
               "Please help me out"],
        "rm": ["Mujhe rehnumai chahiye", "Zara samjha dein",
               "Aap kya kya kar sakte hain", "Booking kaise hoti hai",
               "Main confuse hoon, kya karun", "Mujhe tareeqa batayein",
               "Yeh system kaise chalta hai", "Main naya hoon, samjha dein",
               "Kya kya pooch sakta hoon", "Meri thori madad karein"],
        "ur": ["مجھے رہنمائی چاہیے", "ذرا سمجھا دیں",
               "آپ کیا کیا کر سکتے ہیں", "بکنگ کیسے ہوتی ہے",
               "میں الجھن میں ہوں، کیا کروں", "مجھے طریقہ بتائیں",
               "یہ نظام کیسے چلتا ہے", "میں نیا ہوں، سمجھا دیں",
               "میں کیا کیا پوچھ سکتا ہوں", "میری تھوڑی مدد کریں"],
    },

    "thank_you": {
        "en": ["Thanks a million", "I really appreciate your help",
               "That is very helpful, thank you", "Bless you, thanks",
               "Thank you for your time", "Cheers, thanks",
               "You have been very helpful", "Grateful for your help",
               "Thanks, that answers it", "Perfect, thank you",
               "Thank you very much indeed"],
        "rm": ["Bohot bohot shukriya", "Aap ne bohot madad ki",
               "Allah aap ko khush rakhe, shukriya",
               "Waqt dene ka shukriya", "Bari meherbani hui",
               "Shukriya ji bohot", "Aap ka ehsan hai",
               "Jazak Allah, shukriya"],
        "ur": ["بہت بہت شکریہ", "آپ نے بہت مدد کی",
               "اللہ آپ کو خوش رکھے، شکریہ", "وقت دینے کا شکریہ",
               "بڑی مہربانی ہوئی", "آپ کا احسان ہے", "جزاک اللہ",
               "بہت نوازش آپ کی"],
    },

    "repeat_information": {
        "en": ["Sorry I missed that", "Could you say it slowly",
               "One more time please", "What did you say",
               "The line broke, repeat please",
               "I did not understand, again", "Please repeat the address",
               "Say the timing again", "Can you spell that out",
               "Louder please", "Sorry, come again"],
        "rm": ["Maaf kijiye, sun nahi paya", "Zara aahista boliye",
               "Ek aur baar", "Aap ne kya kaha",
               "Line kat gayi thi, phir kahein",
               "Samajh nahi aaya, dobara", "Pata dobara batayein",
               "Waqt phir se batayein", "Zara zor se boliye"],
        "ur": ["معاف کیجیے، سن نہیں پایا", "ذرا آہستہ بولیے",
               "ایک اور بار", "آپ نے کیا کہا",
               "لائن کٹ گئی تھی، پھر کہیں", "سمجھ نہیں آیا، دوبارہ",
               "پتہ دوبارہ بتائیں", "وقت پھر سے بتائیں",
               "ذرا زور سے بولیے"],
    },

    "unclear_request": {
        "en": ["Uhh", "Hmm let me see", "Wait wait", "One moment",
               "Actually hold on", "I mean, the thing is",
               "How do I put this", "Er I am not certain",
               "Can I think about it", "Not sure really", "Umm okay so",
               "It is sort of complicated"],
        "rm": ["Ummm", "Zara rukiye", "Ek minute", "Acha wo kya tha",
               "Matlab, baat yeh hai", "Kaise batao",
               "Pata nahi kya kahoon", "Sochne do zara",
               "Hmm dekhte hain", "Thora mushkil hai batana"],
        "ur": ["ہممم", "ذرا رکیے", "ایک منٹ", "اچھا وہ کیا تھا",
               "مطلب، بات یہ ہے", "کیسے بتاؤں",
               "پتہ نہیں کیا کہوں", "سوچنے دیں ذرا",
               "تھوڑا مشکل ہے بتانا"],
    },

    "confirm": {
        "en": ["Yes that works", "Perfect", "That is fine by me",
               "Yeah go ahead", "Alright do that", "Yes please do",
               "That suits me", "Agreed", "Fine", "Yes correct",
               "Ok that is good", "Sounds right"],
        "rm": ["Haan yehi theek hai", "Bilkul sahi", "Ji karein",
               "Acha theek hai ji", "Han han", "Manzoor hai",
               "Yehi rakh dein", "Ji sahi kaha", "Bilkul theek"],
        "ur": ["ہاں یہی ٹھیک ہے", "بالکل صحیح", "جی کریں",
               "اچھا ٹھیک ہے جی", "ہاں ہاں", "منظور ہے",
               "یہی رکھ دیں", "جی صحیح کہا", "بالکل ٹھیک"],
    },

    "deny": {
        "en": ["No do not do that", "That is incorrect",
               "No I changed my mind", "Definitely not", "I disagree",
               "That is not right at all", "No, something else",
               "Not that one", "Wrong", "No no", "Please do not"],
        "rm": ["Nahi aisa mat karein", "Yeh ghalat hai",
               "Nahi mera irada badal gaya", "Hargiz nahi",
               "Main muttafiq nahi", "Wo nahi, koi aur",
               "Yeh wala nahi", "Ghalat hai", "Nahi ji bilkul nahi"],
        "ur": ["نہیں ایسا مت کریں", "یہ غلط ہے",
               "نہیں میرا ارادہ بدل گیا", "ہرگز نہیں",
               "میں متفق نہیں", "وہ نہیں، کوئی اور",
               "یہ والا نہیں", "غلط ہے", "نہیں جی بالکل نہیں"],
    },

    "greeting": {
        "en": ["Hello are you there", "Hi good day", "Morning",
               "Hello hello", "Is anyone there",
               "Hi, I am calling about an appointment",
               "Hello, can you help me"],
        "rm": ["Salam bhai", "Assalam o alaikum ji", "Hello koi hai",
               "Salam, main baat kar sakta hoon", "Aap kaise hain",
               "Subah bakhair", "Salam ji"],
        "ur": ["سلام بھائی", "السلام علیکم جی", "ہیلو کوئی ہے",
               "کیا میں بات کر سکتا ہوں", "آپ کیسے ہیں",
               "صبح بخیر", "سلام جی"],
    },

    "clinic_timing": {
        "en": ["Until what time is the clinic open",
               "What are your working hours", "When can I come",
               "Is the doctor there in the morning",
               "Do you sit after Maghrib", "How late are you open",
               "What is the closing time", "Opening time please"],
        "rm": ["Kis waqt tak khula rehta hai",
               "Aap ke working hours kya hain",
               "Main kis waqt aa sakta hoon", "Subah doctor hote hain kya",
               "Maghrib ke baad baithte hain",
               "Band hone ka waqt kya hai", "Khulne ka time batayein"],
        "ur": ["کس وقت تک کھلا رہتا ہے", "آپ کے اوقات کار کیا ہیں",
               "میں کس وقت آ سکتا ہوں", "صبح ڈاکٹر ہوتے ہیں کیا",
               "مغرب کے بعد بیٹھتے ہیں", "بند ہونے کا وقت کیا ہے",
               "کھلنے کا وقت بتائیں"],
    },

    "appointment_confirmation": {
        "en": ["Yes book that slot for me", "Please confirm it now",
               "Go ahead with that appointment", "Lock it in",
               "That date works, confirm please", "Yes finalise it",
               "Book it, that is fine"],
        "rm": ["Ji wohi slot book kar dein", "Abhi confirm kar dein",
               "Wohi appointment rakh dein", "Pakka kar dein",
               "Wo date theek hai, confirm karein", "Ji final kar dein"],
        "ur": ["جی وہی وقت بک کر دیں", "ابھی کنفرم کر دیں",
               "وہی اپائنٹمنٹ رکھ دیں", "پکا کر دیں",
               "وہ تاریخ ٹھیک ہے، کنفرم کریں", "جی فائنل کر دیں"],
    },
}
