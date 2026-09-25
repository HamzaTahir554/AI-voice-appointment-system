"""
FastAPI service exposing the intent classifier to the rest of the FYP.

    pip install fastapi uvicorn
    python src/api.py                 # http://127.0.0.1:8000
    # or: uvicorn api:app --reload --app-dir src

Endpoints
    GET  /                 service metadata
    GET  /health           readiness probe (model loaded? which device?)
    GET  /intents          the 14 intents this model can return
    POST /predict-intent   {"text": "..."} -> {"intent": ..., "confidence": ...}
    POST /predict-batch    {"texts": ["...", "..."]} -> list of predictions

The model is loaded once during application start-up (FastAPI `lifespan`), not
per request - loading mBERT takes seconds and would otherwise be paid on every
single call from the Dialog Manager.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from config import CONFIDENCE_THRESHOLD, MODEL_DIR
from inference import IntentPredictor

# Filled in by the lifespan handler below.
predictor: IntentPredictor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model once at boot, release it at shutdown."""
    global predictor
    try:
        predictor = IntentPredictor()
        print(f"[api] model loaded from {MODEL_DIR} on {predictor.device}")
    except FileNotFoundError as exc:
        # Start anyway so /health can report the problem instead of crash-looping.
        print(f"[api] WARNING: {exc}")
        predictor = None
    yield
    predictor = None


app = FastAPI(
    title="AI Voice Appointment System - Intent Detection",
    description="Fine-tuned mBERT intent classifier for English, Roman Urdu "
                "and Urdu patient utterances.",
    version="1.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class PredictRequest(BaseModel):
    text: str = Field(..., min_length=1,
                      json_schema_extra={"example": "Mujhe doctor se appointment leni hai"})
    threshold: float | None = Field(
        None, ge=0.0, le=1.0,
        description="Override the default confidence threshold for this call.")


class BatchRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1)
    threshold: float | None = Field(None, ge=0.0, le=1.0)


class TopK(BaseModel):
    intent: str
    confidence: float


class PredictResponse(BaseModel):
    intent: str
    confidence: float
    raw_intent: str
    below_threshold: bool
    threshold: float
    top_k: list[TopK]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _require_model() -> IntentPredictor:
    if predictor is None:
        raise HTTPException(
            status_code=503,
            detail="Model not loaded. Train it first with `python src/train.py`.",
        )
    return predictor


def _with_threshold(model: IntentPredictor, override: float | None):
    """Temporarily swap the threshold for a single request."""
    original = model.threshold
    if override is not None:
        model.threshold = override
    return original


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/")
def root() -> dict:
    return {
        "service": "intent-detection",
        "model": "bert-base-multilingual-cased (fine-tuned)",
        "default_threshold": CONFIDENCE_THRESHOLD,
        "endpoints": ["/health", "/intents", "/predict-intent", "/predict-batch"],
    }


@app.get("/health")
def health() -> dict:
    if predictor is None:
        return {"status": "degraded", "model_loaded": False,
                "detail": "run src/train.py to create the model"}
    return {
        "status": "ok",
        "model_loaded": True,
        "device": str(predictor.device),
        "num_intents": len(predictor.labels),
    }


@app.get("/intents")
def intents() -> dict:
    model = _require_model()
    return {"intents": model.labels, "count": len(model.labels)}


@app.post("/predict-intent", response_model=PredictResponse)
def predict_intent_endpoint(request: PredictRequest) -> dict:
    """Classify one patient utterance coming from the STT stage."""
    model = _require_model()
    original = _with_threshold(model, request.threshold)
    try:
        return model.predict(request.text)
    finally:
        model.threshold = original


@app.post("/predict-batch")
def predict_batch_endpoint(request: BatchRequest) -> dict:
    """Classify several utterances in a single forward pass."""
    model = _require_model()
    original = _with_threshold(model, request.threshold)
    try:
        return {"predictions": model.predict_batch(request.texts)}
    finally:
        model.threshold = original


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
