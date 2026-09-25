"""
FastAPI service for the AI Voice Appointment System.

    uvicorn api.main:app --reload
    # or: python -m api.main
    # docs: http://127.0.0.1:8000/docs

`POST /dialog/message` is the main entry point - the voice layer (Asterisk ->
STT) posts transcribed text and receives the sentence to speak back. The other
endpoints exist for the doctor dashboard and for testing; the Dialog Manager
remains the orchestration layer for anything a patient does.

The mBERT model and the Firebase connection are created once at start-up, not
per request.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Any

from fastapi import FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import LOG_LEVEL, CONFIDENCE_THRESHOLD, OLLAMA_MODEL, Status
from api.admin import router as admin_router
from api.dashboard import auth_router, router as dashboard_router
from appointment_backend.api import doctor_router, patient_router
from appointment_backend.api import router as appointment_router
from appointment_backend.api import set_backend
from appointment_backend.appointment_service import AppointmentBackend
from voice_pipeline import VoicePipeline
from dialog_manager.dialog_manager import DialogManager
from firebase.clinic_service import ClinicService
from firebase.conversation_service import ConversationService
from firebase.doctor_service import DoctorService
from firebase.firebase_config import init_repository
from firebase.patient_service import PatientService

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s")
logger = logging.getLogger("api")

# Populated by the lifespan handler.
services: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot the database and the Dialog Manager once."""
    repo = init_repository()
    logger.info("database backend: %s", repo.backend)

    if repo.backend == "local":
        # Without Firestore there is no seed data, so load the demo set to
        # keep the service usable out of the box.
        from firebase.seed_data import seed
        seed(repo)
        logger.warning("Using the in-memory database seeded with demo data. "
                       "Configure Firebase in .env for persistence.")

    services["repo"] = repo
    services["doctors"] = DoctorService(repo)
    services["clinics"] = ClinicService(repo)
    services["patients"] = PatientService(repo)
    services["conversations"] = ConversationService(repo)
    backend = AppointmentBackend(repo)
    services["backend"] = backend
    set_backend(backend)

    try:
        services["dialog"] = DialogManager(repository=repo)
        services["pipeline"] = VoicePipeline(
            dialog_manager=services["dialog"], repository=repo)
        logger.info("Dialog Manager ready; Ollama %s",
                    "available" if services["pipeline"].llm_available
                    else "unavailable (deterministic responses)")
    except Exception as exc:                            # pragma: no cover
        # Missing mBERT model: keep the API up so /health can explain why.
        logger.error("Dialog Manager unavailable: %s", exc)
        services["dialog"] = None
        services["pipeline"] = None
        services["dialog_error"] = str(exc)
    yield
    services.clear()


app = FastAPI(
    title="AI Voice Appointment System",
    description="mBERT intent detection + Dialog Manager + Firebase Firestore.",
    version="1.0.0",
    lifespan=lifespan,
)


# The Appointment Backend owns every appointment mutation; these routes expose
# it directly for the voice pipeline and for testing.
app.include_router(appointment_router)
app.include_router(doctor_router)
app.include_router(patient_router)

# Doctor dashboard: signed-in, scoped to one doctor (api/dashboard.py).
app.include_router(auth_router)
app.include_router(dashboard_router)

# Administration: the superadmin account manages the doctors (api/admin.py).
app.include_router(admin_router)

# The dashboard may be served from this API (same origin, no CORS needed) at
# /ui, or opened from disk / another dev server - hence the configurable
# origin list. "null" covers a page opened directly as a file:// URL.
_origins = [o.strip() for o in os.environ.get(
    "DASHBOARD_ORIGINS",
    "http://localhost:8080,http://127.0.0.1:8080,http://localhost:5500,http://127.0.0.1:5500,null"
).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,          # the session travels in an Authorization header
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

_dashboard_dir = FilePath(__file__).resolve().parents[1] / "Dashboard"
if _dashboard_dir.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard-ui")


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class VoiceRequest(BaseModel):
    session_id: str = Field(..., min_length=1,
                            json_schema_extra={"example": "S001"})
    text: str = Field(..., json_schema_extra={
        "example": "Mujhe Dr Ahmed se kal 4 baje appointment chahiye."})
    patient_id: str | None = Field(None, json_schema_extra={"example": "P001"})


class DialogRequest(BaseModel):
    session_id: str = Field(..., min_length=1,
                            json_schema_extra={"example": "session_123"})
    text: str = Field(..., json_schema_extra={
        "example": "Mujhe Dr Ahmed se kal appointment leni hai."})
    patient_id: str | None = Field(None, json_schema_extra={"example": "P001"})


class DialogResponse(BaseModel):
    session_id: str
    intent: str
    confidence: float
    response: str
    action: str
    # Named dialog state: IDLE / APPOINTMENT_BOOKING / DOCTOR_INFORMATION / ...
    state: str
    # A question parked while we collect a missing slot, else null.
    pending_intent: str | None = None
    slot: str | None = None
    raw_intent: str | None = None
    slots: dict[str, Any] = Field(default_factory=dict)
    session_state: dict[str, Any] = Field(default_factory=dict)


class IntentRequest(BaseModel):
    text: str = Field(..., min_length=1)


class SessionRequest(BaseModel):
    session_id: str
    patient_id: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _dialog() -> DialogManager:
    manager = services.get("dialog")
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail=("Dialog Manager unavailable: "
                    f"{services.get('dialog_error', 'not initialised')}. "
                    "Train the intent model first: "
                    "python intent_detection/src/train.py"))
    return manager


def _unwrap(result, not_found_code: int = 404):
    """Turn a failed ServiceResult into a clean HTTP error."""
    if result.ok:
        return result.data
    status = 503 if result.error == "BACKEND_UNAVAILABLE" else not_found_code
    raise HTTPException(status_code=status,
                        detail={"error": result.error,
                                "message": result.message})


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------
@app.get("/")
def root() -> dict:
    return {
        "service": "ai-voice-appointment-system",
        "pipeline": "STT -> mBERT -> Dialog Manager -> Firestore -> TTS",
        "endpoints": ["/health", "/dialog/message", "/intent/predict",
                      "/doctors", "/appointments/book", "/docs",
                      "/ui (doctor dashboard)", "/auth/login", "/dashboard/summary"],
    }


@app.get("/health")
def health() -> dict:
    repo = services.get("repo")
    manager = services.get("dialog")
    return {
        "status": "ok" if manager else "degraded",
        "database": getattr(repo, "backend", "uninitialised"),
        "persistent": getattr(repo, "backend", "") == "firestore",
        "dialog_manager": bool(manager),
        "intent_model_loaded": bool(
            manager and getattr(manager.router.detector, "is_loaded", False)),
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "ollama_model": OLLAMA_MODEL,
        "ollama_available": bool(
            services.get("pipeline") and services["pipeline"].llm_available),
        "detail": services.get("dialog_error"),
    }


# --------------------------------------------------------------------------
# Main dialog endpoint (spec section 31)
# --------------------------------------------------------------------------
@app.post("/dialog/message", response_model=DialogResponse)
def dialog_message(request: DialogRequest) -> dict:
    """
    One turn of conversation.

    Text in (from STT) -> response text out (for TTS), with the intent,
    confidence, next action and full slot state alongside.
    """
    manager = _dialog()
    return manager.process_message(request.session_id, request.text,
                                   request.patient_id)


@app.post("/voice/message", tags=["voice"],
          summary="Full pipeline: mBERT -> Dialog -> Backend -> Ollama -> TTS")
def voice_message(request: VoiceRequest) -> dict:
    """
    The endpoint the telephony layer calls (spec section 36).

    Runs every stage and returns the sentence for TTS, plus the intent, the
    backend result and what the Ollama judge decided - so the whole chain is
    auditable from one response.
    """
    pipeline = services.get("pipeline")
    if pipeline is None:
        raise HTTPException(
            status_code=503,
            detail=("Pipeline unavailable: "
                    f"{services.get('dialog_error', 'not initialised')}"))
    return pipeline.process(request.session_id, request.text,
                            request.patient_id)


@app.post("/dialog/reset")
def dialog_reset(request: SessionRequest) -> dict:
    manager = _dialog()
    state = manager.reset_session(request.session_id, request.patient_id)
    return {"session_id": state.session_id, "status": "reset"}


@app.post("/dialog/end")
def dialog_end(request: SessionRequest) -> dict:
    manager = _dialog()
    ended = manager.end_session(request.session_id)
    return {"session_id": request.session_id, "ended": ended}


@app.get("/dialog/transcript/{session_id}")
def dialog_transcript(session_id: str) -> dict:
    """The stored transcript for one call (phone numbers masked)."""
    messages = _unwrap(services["conversations"].get_messages(session_id))
    return {"session_id": session_id, "messages": messages}


# --------------------------------------------------------------------------
# Intent detection
# --------------------------------------------------------------------------
@app.post("/intent/predict")
def intent_predict(request: IntentRequest) -> dict:
    """Expose the mBERT classifier on its own, for debugging and demos."""
    manager = _dialog()
    result = manager.router.route(request.text)
    return {
        "text": request.text,
        "intent": result.intent,
        "raw_intent": result.raw_intent,
        "confidence": round(result.confidence, 4),
        "below_threshold": result.below_threshold,
        "topic": result.topic,
        "top_k": result.top_k,
    }


# --------------------------------------------------------------------------
# Doctors
# --------------------------------------------------------------------------
@app.get("/doctors")
def list_doctors() -> dict:
    return {"doctors": _unwrap(services["doctors"].list_doctors())}


@app.get("/doctors/{doctor_id}")
def get_doctor(doctor_id: str = Path(...)) -> dict:
    return _unwrap(services["doctors"].get_doctor(doctor_id))


# --------------------------------------------------------------------------
# Appointments and the doctor dashboard
# --------------------------------------------------------------------------
# Served by appointment_backend/api.py (included above): /appointments/book,
# /cancel, /reschedule, /check, /appointments/{id},
# /patients/{id}/appointments, /doctors/{id}/availability,
# /doctors/{id}/appointments and /doctors/unavailability.
#
# This file used to re-declare several of them. The included router is
# registered first, so those copies never ran - and the copy of
# /doctors/unavailability blocked the day WITHOUT cancelling the appointments
# on it or notifying anyone. Removed by the system audit.


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=False)
