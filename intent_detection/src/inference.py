"""
Inference for the fine-tuned mBERT intent classifier.

    python src/inference.py                          # run the demo table
    python src/inference.py --text "Mujhe appointment chahiye"
    python src/inference.py --threshold 0.75

The model is loaded exactly once into a module-level singleton, so the API in
`api.py` and the Dialog Manager both pay the start-up cost only at boot.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import torch

from config import (
    CONFIDENCE_THRESHOLD,
    MAX_LENGTH,
    MODEL_DIR,
    UNKNOWN_INTENT,
    use_utf8_stdout,
)


class IntentPredictor:
    """
    Loads mBERT + tokenizer once and turns raw STT text into an intent.

    Usage:
        predictor = IntentPredictor()
        result = predictor.predict("I want to book an appointment")
        # {'intent': 'book_appointment', 'confidence': 0.96, ...}
    """

    def __init__(self, model_dir: Path = MODEL_DIR,
                 threshold: float = CONFIDENCE_THRESHOLD,
                 max_length: int = MAX_LENGTH,
                 device: str | None = None):
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        model_dir = Path(model_dir)
        if not (model_dir / "config.json").exists():
            raise FileNotFoundError(
                f"No trained model at {model_dir}. Run `python src/train.py` first."
            )

        self.threshold = threshold
        self.max_length = max_length
        self.device = torch.device(
            device if device else ("cuda" if torch.cuda.is_available() else "cpu")
        )

        self.tokenizer = AutoTokenizer.from_pretrained(model_dir)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_dir)
        self.model.to(self.device)
        self.model.eval()          # disables dropout - essential for inference

        # Prefer the sidecar JSON written by train.py; fall back to the config.
        map_path = model_dir / "id2label.json"
        if map_path.exists():
            raw = json.loads(map_path.read_text(encoding="utf-8"))
            self.id2label = {int(k): v for k, v in raw.items()}
        else:
            self.id2label = {int(k): v for k, v in self.model.config.id2label.items()}
        self.labels = [self.id2label[i] for i in sorted(self.id2label)]

    # ------------------------------------------------------------------
    @torch.no_grad()
    def predict_batch(self, texts, top_k: int = 3):
        """Classify a list of utterances in one forward pass."""
        cleaned = [(t or "").strip() for t in texts]
        encoded = self.tokenizer(
            cleaned,
            max_length=self.max_length,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        ).to(self.device)

        logits = self.model(**encoded).logits.float()
        # Softmax converts raw scores into a probability distribution that
        # sums to 1, which is what makes the confidence number meaningful.
        probs = torch.softmax(logits, dim=-1).cpu()

        results = []
        for i, text in enumerate(cleaned):
            row = probs[i]
            best_id = int(row.argmax())
            confidence = float(row[best_id])
            predicted = self.id2label[best_id]

            # Below the threshold we refuse to guess. In a voice appointment
            # system a confident wrong answer cancels a real appointment.
            final = predicted if confidence >= self.threshold else UNKNOWN_INTENT

            k = min(top_k, len(self.labels))
            top_scores, top_ids = torch.topk(row, k)
            results.append({
                "text": text,
                "intent": final,
                "confidence": round(confidence, 4),
                "raw_intent": predicted,
                "below_threshold": confidence < self.threshold,
                "threshold": self.threshold,
                "top_k": [
                    {"intent": self.id2label[int(j)], "confidence": round(float(s), 4)}
                    for s, j in zip(top_scores, top_ids)
                ],
            })
        return results

    def predict(self, text: str, top_k: int = 3) -> dict:
        """
        Classify one utterance and return the structured payload that the
        Dialog Manager consumes.
        """
        if not text or not text.strip():
            return {
                "text": text or "",
                "intent": UNKNOWN_INTENT,
                "confidence": 0.0,
                "raw_intent": UNKNOWN_INTENT,
                "below_threshold": True,
                "threshold": self.threshold,
                "top_k": [],
            }
        return self.predict_batch([text], top_k=top_k)[0]


# --------------------------------------------------------------------------
# Module-level singleton + the simple function the spec asks for
# --------------------------------------------------------------------------
_PREDICTOR: IntentPredictor | None = None


def get_predictor(**kwargs) -> IntentPredictor:
    """Return the shared predictor, constructing it on first use."""
    global _PREDICTOR
    if _PREDICTOR is None:
        _PREDICTOR = IntentPredictor(**kwargs)
    return _PREDICTOR


def predict_intent(text: str):
    """
    The headline helper:

        intent, confidence = predict_intent("I want to book an appointment")

    Returns `("unknown_intent", confidence)` when the model is not sure enough.
    """
    result = get_predictor().predict(text)
    return result["intent"], result["confidence"]


# --------------------------------------------------------------------------
# Demo examples - English, Roman Urdu, Urdu and code-mixed
# --------------------------------------------------------------------------
# "Expected" here is a *manually defined* intent: these sentences are written
# by hand for the demo, they are not rows taken from the dataset.
DEMO_EXAMPLES = [
    # --- English ---------------------------------------------------------
    ("I want to book an appointment with Dr Ahmed.", "book_appointment", "English"),
    ("Please cancel my appointment for tomorrow.", "cancel_appointment", "English"),
    ("Can I move my appointment to Saturday?", "reschedule_appointment", "English"),
    ("Is the doctor available on Monday morning?", "check_availability", "English"),
    ("What is the doctor's consultation fee?", "doctor_fee", "English"),
    ("Where is the clinic located?", "clinic_address", "English"),
    ("What are the clinic timings?", "clinic_timing", "English"),
    ("What is the doctor's qualification?", "doctor_qualification", "English"),
    ("How many years of experience does the doctor have?", "doctor_experience", "English"),
    ("This is an emergency, I need the doctor right now!", "emergency", "English"),
    ("Hello, good morning.", "greeting", "English"),
    ("Thank you, goodbye.", "goodbye", "English"),
    # --- Roman Urdu ------------------------------------------------------
    ("Mujhe doctor se appointment leni hai.", "book_appointment", "Roman Urdu"),
    ("Meri appointment cancel kar dein.", "cancel_appointment", "Roman Urdu"),
    ("Doctor sahib ki fees kitni hai?", "doctor_fee", "Roman Urdu"),
    ("Clinic ka pata batao.", "clinic_address", "Roman Urdu"),
    ("Doctor sahib kab baithte hain?", "clinic_timing", "Roman Urdu"),
    ("Doctor sahib ka experience kitna hai?", "doctor_experience", "Roman Urdu"),
    # --- Urdu script -----------------------------------------------------
    ("مجھے ڈاکٹر سے اپائنٹمنٹ لینی ہے۔", "book_appointment", "Urdu"),
    ("میری اپائنٹمنٹ منسوخ کر دیں۔", "cancel_appointment", "Urdu"),
    ("ڈاکٹر صاحب کی فیس کتنی ہے؟", "doctor_fee", "Urdu"),
    ("کلینک کا پتہ کیا ہے؟", "clinic_address", "Urdu"),
    ("یہ ایمرجنسی ہے، مجھے ابھی ڈاکٹر چاہیے۔", "emergency", "Urdu"),
    # --- Code-mixed ------------------------------------------------------
    ("Dr Ahmed ke saath appointment book karni hai.", "book_appointment", "Mixed"),
    ("Kal ki appointment reschedule kar dein please.", "reschedule_appointment", "Mixed"),
    # --- Deliberately out of scope: should fall below the threshold ------
    ("What is the weather like in Lahore today?", "(out of scope)", "English"),
]


def run_demo(predictor: IntentPredictor) -> None:
    """Print the demo table required by the spec."""
    results = predictor.predict_batch([t for t, _, _ in DEMO_EXAMPLES])

    print("=" * 118)
    print("INTENT PREDICTIONS ON MANUALLY WRITTEN EXAMPLES")
    print(f"(confidence threshold = {predictor.threshold}; "
          f"'Actual Intent' is manually defined, not a dataset label)")
    print("=" * 118)
    header = (f"{'Input':<46}{'Language':<12}{'Actual (manual)':<24}"
              f"{'Predicted':<24}{'Conf':>6}")
    print(header)
    print("-" * 118)

    matches = 0
    scored = 0
    for (text, expected, language), result in zip(DEMO_EXAMPLES, results):
        shown = text if len(text) <= 44 else text[:41] + "..."
        predicted = result["intent"]
        if expected != "(out of scope)":
            scored += 1
            matches += int(predicted == expected)
        flag = "" if predicted == expected else "  <-"
        print(f"{shown:<46}{language:<12}{expected:<24}{predicted:<24}"
              f"{result['confidence']:>6.2f}{flag}")

    print("-" * 118)
    if scored:
        print(f"matched {matches}/{scored} of the in-scope manual examples "
              f"({matches / scored:.0%})")
    print("(rows marked <- disagree with the hand-written expectation)")

    # Show the structured payload that goes to the Dialog Manager.
    print("\n" + "=" * 78)
    print("STRUCTURED OUTPUT FOR THE DIALOG MANAGER")
    print("=" * 78)
    sample = predictor.predict("Mujhe kal Dr Ahmed ke saath appointment chahiye.")
    print(json.dumps(sample, indent=2, ensure_ascii=False))


def run_interactive(predictor: IntentPredictor) -> None:
    """
    Read utterances from the keyboard one at a time and classify each.

    Type `quit`, `exit` or press Ctrl+C to leave. `threshold 0.8` changes the
    cut-off without restarting, which is handy during a live demo.
    """
    # Windows consoles hand us cp1252 by default, which cannot represent Urdu.
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    print("=" * 70)
    print("INTERACTIVE INTENT DETECTION")
    print("=" * 70)
    print(f"model      : {predictor.model.config._name_or_path}")
    print(f"device     : {predictor.device}")
    print(f"threshold  : {predictor.threshold}")
    print(f"intents    : {len(predictor.labels)}")
    print()
    print("Type a sentence in English, Roman Urdu or Urdu and press Enter.")
    print("Commands:  quit | exit        leave")
    print("           threshold 0.8      change the confidence cut-off")
    print("           intents            list every intent")
    print("-" * 70)

    while True:
        try:
            text = input("\n>>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return

        if not text:
            continue
        lowered = text.lower()

        if lowered in ("quit", "exit", "q"):
            print("bye")
            return

        if lowered == "intents":
            for name in predictor.labels:
                print(f"   - {name}")
            continue

        if lowered.startswith("threshold"):
            parts = text.split()
            try:
                predictor.threshold = float(parts[1])
                print(f"    threshold is now {predictor.threshold}")
            except (IndexError, ValueError):
                print("    usage: threshold 0.8")
            continue

        result = predictor.predict(text)

        if result["below_threshold"]:
            print(f"    intent     : {result['intent']}  "
                  f"(best guess was '{result['raw_intent']}')")
        else:
            print(f"    intent     : {result['intent']}")
        print(f"    confidence : {result['confidence']:.4f}")

        # Showing the runners-up makes near-misses obvious in a demo.
        others = "   ".join(
            f"{c['intent']} {c['confidence']:.3f}" for c in result["top_k"][1:]
        )
        if others:
            print(f"    runners-up : {others}")


def main() -> None:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description="mBERT intent inference")
    parser.add_argument("--text", type=str, help="classify a single utterance")
    parser.add_argument("--threshold", type=float, default=CONFIDENCE_THRESHOLD)
    parser.add_argument("--model-dir", type=str, default=str(MODEL_DIR))
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="type sentences one by one and classify each")
    args = parser.parse_args()

    predictor = IntentPredictor(model_dir=Path(args.model_dir),
                                threshold=args.threshold)

    if args.interactive:
        run_interactive(predictor)
    elif args.text:
        result = predictor.predict(args.text)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"\nIntent: {result['intent']}")
        print(f"Confidence: {result['confidence']:.2f}")
    else:
        run_demo(predictor)


if __name__ == "__main__":
    main()
