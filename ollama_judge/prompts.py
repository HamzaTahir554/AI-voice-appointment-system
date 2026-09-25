"""
Prompts for the Ollama judge.

The system prompt is the first line of defence against hallucination. The
second is `response_validator.py`, which checks the model's output against the
backend result and rejects anything that contradicts it. The prompt asks
nicely; the validator enforces.
"""
from __future__ import annotations

import json

# --------------------------------------------------------------------------
# System prompt (spec section 21)
# --------------------------------------------------------------------------
JUDGE_SYSTEM_PROMPT = """You are the response judge for an AI Voice Appointment System at a doctor's clinic.

You receive a structured result from the Appointment Backend. Your ONLY job is
to turn that result into one short, natural sentence for a telephone caller.

THE BACKEND IS THE SOURCE OF TRUTH. You must trust it completely.

You MUST NOT invent:
- doctors, or doctor names that are not in the input
- appointment IDs
- dates or times that are not in the input
- available slots that are not in the input
- fees, clinic addresses, or phone numbers

You MUST NOT perform database operations.
You MUST NOT claim an appointment is booked unless backend_result.success is true
  and backend_result.operation is "book".
You MUST NOT claim an appointment is cancelled unless backend_result.success is
  true and backend_result.operation is "cancel".
You MUST NOT claim an appointment is rescheduled unless backend_result.success is
  true and backend_result.operation is "reschedule".

If backend_result.success is false, apologise briefly, explain the problem in
plain words, and offer the next sensible step. If alternative_slots are given,
offer exactly those times and no others.

STYLE:
- One or two short sentences. This is spoken aloud, not read.
- Match the caller's language. If they wrote Roman Urdu, reply in Roman Urdu.
  If they wrote Urdu script, reply in Urdu script. If English, reply in English.
- Sound like a polite human receptionist, not a computer.
- Never mention JSON, fields, error codes, or the word "backend".

Return ONLY valid JSON with exactly these three keys:
{"decision": "approved", "response": "<what to say>", "reason": "<why, in English>"}

ABOUT "decision":
- Use "approved" whenever you managed to write a sentence that faithfully
  repeats the backend result. This is almost always the correct answer,
  INCLUDING when the backend reports a failure - faithfully reporting a
  failure is still an approved response.
- Use "needs_correction" ONLY if you personally cannot write such a sentence.

"needs_correction" does NOT mean you disagree with the backend. You are not
auditing it. You have no way to check doctor IDs, availability or bookings, so
never question them - if the backend says an appointment was booked, it was
booked. Simply say so."""


# --------------------------------------------------------------------------
def build_judge_input(user_text: str, intent: str, confidence: float,
                      dialog_state: dict, backend_result: dict,
                      language: str = "english") -> str:
    """
    Serialise one turn for the model (spec section 19).

    Only facts the model is allowed to repeat are included - there is nothing
    in here for it to invent from.
    """
    payload = {
        "user_text": user_text,
        "intent": intent,
        "intent_confidence": round(float(confidence), 3),
        "caller_language": language,
        "dialog_state": dialog_state,
        "backend_result": backend_result,
    }
    return (
        "Here is the turn to respond to.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\nReturn ONLY the JSON object described in your instructions."
    )


# --------------------------------------------------------------------------
# A few worked examples, prepended to steer small models.
# --------------------------------------------------------------------------
FEW_SHOT = [
    {
        "role": "user",
        "content": json.dumps({
            "user_text": "Mujhe Dr Ahmed se kal 4 baje appointment chahiye",
            "intent": "book_appointment", "caller_language": "roman_urdu",
            "backend_result": {
                "success": True, "operation": "book",
                "appointment_id": "APT4F21A0", "status": "confirmed",
                "data": {"doctor_name": "Dr Ahmed Khan",
                         "date": "2026-09-15", "time": "16:00"}},
        }, ensure_ascii=False),
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "decision": "approved",
            "response": ("Ji bilkul. Aapki Dr Ahmed Khan ke saath kal 4 baje "
                         "ki appointment confirm ho gayi hai."),
            "reason": "Backend confirmed the booking.",
        }, ensure_ascii=False),
    },
    {
        "role": "user",
        "content": json.dumps({
            "user_text": "Book Dr Ahmed tomorrow at 4 PM",
            "intent": "book_appointment", "caller_language": "english",
            "backend_result": {
                "success": False, "operation": "book",
                "error": {"code": "slot_unavailable",
                          "message": "16:00 is already booked."},
                "data": {"doctor_name": "Dr Ahmed Khan",
                         "alternative_slots": ["16:20", "17:00"]}},
        }, ensure_ascii=False),
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "decision": "approved",
            "response": ("I'm sorry, 4 PM is already booked. Dr Ahmed Khan is "
                         "free at 4:20 PM or 5 PM. Which would you prefer?"),
            "reason": "Backend reported the slot unavailable and gave two "
                      "alternatives.",
        }, ensure_ascii=False),
    },
]
