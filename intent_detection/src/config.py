"""
Central configuration for the mBERT intent-detection module.

Every path is derived from the project root so the scripts run identically
whether you launch them from the repo root or from inside `src/`.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent                 # intent_detection/
REPO_ROOT = PROJECT_DIR.parent               # AI VOICE APPOINTMENT SYSTEM/

# The raw CSVs shipped with the FYP live in `<repo root>/Data`.
# Override with the RAW_DATA_DIR environment variable if you move them.
RAW_DATA_DIR = Path(os.environ.get("RAW_DATA_DIR", REPO_ROOT / "Data"))

DATA_DIR = PROJECT_DIR / "data"
PROCESSED_CSV = DATA_DIR / "intents.csv"      # unified, cleaned, deduplicated
SPLITS_CSV = DATA_DIR / "splits.csv"          # same rows + a `split` column

MODELS_DIR = PROJECT_DIR / "models"
MODEL_DIR = MODELS_DIR / "mbert_intent_classifier"   # final saved model
CHECKPOINT_DIR = MODELS_DIR / "checkpoints"          # best-epoch checkpoints

OUTPUT_DIR = PROJECT_DIR / "outputs"

for _d in (DATA_DIR, MODELS_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Model / tokenizer
# --------------------------------------------------------------------------
MODEL_NAME = "bert-base-multilingual-cased"   # mBERT
# Measured on this dataset: the longest utterance is 29 WordPiece tokens
# (p99 = 28). 64 covers every training sample with 2x headroom for longer
# real-world STT transcripts, and trains 2x faster than the usual 128.
MAX_LENGTH = 64

# --------------------------------------------------------------------------
# Training hyper-parameters (tuned for a 4-6 GB consumer GPU)
# --------------------------------------------------------------------------
SEED = 42
BATCH_SIZE = 16              # per-step batch that must fit in VRAM
GRAD_ACCUM_STEPS = 2         # effective batch = BATCH_SIZE * GRAD_ACCUM_STEPS
LEARNING_RATE = 2e-5
WEIGHT_DECAY = 0.01
EPOCHS = 20                  # small dataset; early stopping ends it sooner
WARMUP_RATIO = 0.1
MAX_GRAD_NORM = 1.0          # gradient clipping
EARLY_STOPPING_PATIENCE = 5  # macro-F1 on a 110-row val set is noisy, be patient
USE_FP16 = True              # only honoured when CUDA is present
USE_CLASS_WEIGHTS = True     # counteracts the heavy class imbalance
# Exponent applied to the inverse-frequency weights. 1.0 = full inverse, which
# on this data gives a 166x spread and makes the model over-predict rare
# intents (precision 0.08 / recall 1.00). 0.5 (square root) gives a 13x spread
# and is far more stable. 0.0 disables weighting entirely.
CLASS_WEIGHT_POWER = 0.5

# --------------------------------------------------------------------------
# Split ratios (stratified + group-aware, see preprocess.py)
# --------------------------------------------------------------------------
TRAIN_RATIO, VAL_RATIO, TEST_RATIO = 0.80, 0.10, 0.10

# --------------------------------------------------------------------------
# Inference
# --------------------------------------------------------------------------
CONFIDENCE_THRESHOLD = 0.60
UNKNOWN_INTENT = "unknown_intent"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def set_seed(seed: int = SEED) -> None:
    """Seed every RNG we rely on so runs are reproducible."""
    random.seed(seed)
    np.random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    try:
        import torch

        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    except ImportError:      # torch not needed by every script
        pass


def use_utf8_stdout() -> None:
    """
    Windows consoles default to cp1252 and raise UnicodeEncodeError the moment
    we print Urdu. Force UTF-8 on stdout/stderr instead.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
