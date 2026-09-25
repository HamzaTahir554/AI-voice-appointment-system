"""
Evaluate the fine-tuned mBERT intent classifier on the held-out TEST split.

    python src/evaluate.py

Produces, in `outputs/`:
    classification_report.txt   accuracy / precision / recall / F1 per intent
    per_intent_metrics.csv      the same numbers as a spreadsheet
    confusion_matrix.png        actual vs predicted heat-map
    training_loss.png, validation_loss.png,
    training_accuracy.png, validation_accuracy.png

Only the test split is touched here - the model never saw it during training
or model selection, so these numbers are an honest estimate of real accuracy.
"""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")            # headless backend; no display needed
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
)

from config import DATA_DIR, MODEL_DIR, OUTPUT_DIR, SPLITS_CSV, set_seed, use_utf8_stdout
from dataset import describe_device, load_splits, make_loader, resolve_device
from preprocess import load_label_maps


# --------------------------------------------------------------------------
# Prediction
# --------------------------------------------------------------------------
@torch.no_grad()
def predict_split(model, loader, device):
    """Return (y_true, y_pred, probabilities) for a whole dataloader."""
    model.eval()
    y_true, y_pred, probs = [], [], []
    for batch in loader:
        labels = batch.pop("labels")
        batch = {k: v.to(device) for k, v in batch.items()}
        logits = model(**batch).logits.float()
        p = torch.softmax(logits, dim=-1).cpu().numpy()
        probs.append(p)
        y_pred.append(p.argmax(axis=1))
        y_true.append(labels.numpy())
    return (np.concatenate(y_true), np.concatenate(y_pred), np.concatenate(probs))


# --------------------------------------------------------------------------
# Plots (matplotlib only - no seaborn)
# --------------------------------------------------------------------------
def plot_confusion_matrix(cm: np.ndarray, labels, path, normalize: bool = False):
    """Draw an actual-vs-predicted matrix that stays readable with 14 intents."""
    data = cm.astype(float)
    if normalize:
        row_sums = data.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1.0
        data = data / row_sums

    n = len(labels)
    fig, ax = plt.subplots(figsize=(max(8, n * 0.75), max(6.5, n * 0.65)))
    im = ax.imshow(data, cmap="Blues", aspect="auto")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04,
                 label="proportion of true class" if normalize else "samples")

    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("Predicted intent")
    ax.set_ylabel("Actual intent")
    ax.set_title("Confusion Matrix - mBERT Intent Classifier (test set)")

    # Annotate every cell; white text on the dark cells keeps it legible.
    threshold = data.max() / 2.0 if data.max() > 0 else 0.5
    for i in range(n):
        for j in range(n):
            value = cm[i, j]
            if value == 0:
                continue
            text = f"{value}" if not normalize else f"{data[i, j]:.2f}"
            ax.text(j, i, text, ha="center", va="center", fontsize=7,
                    color="white" if data[i, j] > threshold else "black")

    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_curve(values, title, ylabel, path, second=None, second_label=None):
    """One metric per figure, as the spec requires."""
    epochs = range(1, len(values) + 1)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(epochs, values, marker="o", linewidth=2, label=ylabel)
    if second is not None:
        ax.plot(epochs, second, marker="s", linewidth=2, linestyle="--",
                label=second_label)
        ax.legend()
    ax.set_xlabel("Epoch")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.set_xticks(list(epochs))
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_training_curves(history: dict) -> None:
    """Write the four separate curve figures listed in the spec."""
    plot_curve(history["train_loss"], "Training Loss vs Epoch", "Training loss",
               OUTPUT_DIR / "training_loss.png")
    plot_curve(history["val_loss"], "Validation Loss vs Epoch", "Validation loss",
               OUTPUT_DIR / "validation_loss.png")
    plot_curve(history["train_acc"], "Training Accuracy vs Epoch",
               "Training accuracy", OUTPUT_DIR / "training_accuracy.png")
    plot_curve(history["val_acc"], "Validation Accuracy vs Epoch",
               "Validation accuracy", OUTPUT_DIR / "validation_accuracy.png")
    # A combined view is handy for the report, even though it is not required.
    plot_curve(history["train_loss"], "Training vs Validation Loss", "Loss",
               OUTPUT_DIR / "loss_comparison.png",
               second=history["val_loss"], second_label="Validation loss")


# --------------------------------------------------------------------------
# Confusion analysis
# --------------------------------------------------------------------------
def top_confusions(cm: np.ndarray, labels, k: int = 8):
    """Most frequent (actual -> predicted) mistakes."""
    pairs = []
    for i in range(len(labels)):
        for j in range(len(labels)):
            if i != j and cm[i, j] > 0:
                support = cm[i].sum()
                pairs.append((labels[i], labels[j], int(cm[i, j]),
                              cm[i, j] / support if support else 0.0))
    pairs.sort(key=lambda x: (-x[2], -x[3]))
    return pairs[:k]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> None:
    use_utf8_stdout()
    set_seed()

    if not (MODEL_DIR / "config.json").exists():
        raise SystemExit(f"No trained model in {MODEL_DIR} - run src/train.py first.")

    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    label2id, id2label = load_label_maps(DATA_DIR)
    labels = [id2label[i] for i in range(len(id2label))]

    device = resolve_device()
    describe_device(device)

    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR).to(device)

    _, _, test_df = load_splits(SPLITS_CSV, label2id)
    print(f"evaluating on {len(test_df)} unseen TEST rows\n")

    max_length = model.config.max_position_embeddings
    max_length = min(128, max_length)
    loader = make_loader(test_df, tokenizer, 16, max_length, shuffle=False)
    y_true, y_pred, probs = predict_split(model, loader, device)

    # --- headline metrics ------------------------------------------------
    accuracy = accuracy_score(y_true, y_pred)
    macro_f1 = f1_score(y_true, y_pred, average="macro", zero_division=0)
    weighted_f1 = f1_score(y_true, y_pred, average="weighted", zero_division=0)
    macro_p, macro_r, _, _ = precision_recall_fscore_support(
        y_true, y_pred, average="macro", zero_division=0)
    w_p, w_r, _, _ = precision_recall_fscore_support(
        y_true, y_pred, average="weighted", zero_division=0)

    # Only intents actually present in the test set can be scored.
    present = sorted(set(y_true.tolist()) | set(y_pred.tolist()))
    present_labels = [labels[i] for i in present]
    missing = [lab for i, lab in enumerate(labels) if i not in set(y_true.tolist())]

    report = classification_report(
        y_true, y_pred, labels=present, target_names=present_labels,
        digits=4, zero_division=0)

    lines = []
    lines.append("=" * 78)
    lines.append("mBERT INTENT CLASSIFIER - TEST SET EVALUATION")
    lines.append("=" * 78)
    lines.append(f"test samples      : {len(y_true)}")
    lines.append(f"intents in taxonomy: {len(labels)}")
    lines.append("")
    lines.append(f"Accuracy          : {accuracy:.4f}")
    lines.append(f"Precision (macro) : {macro_p:.4f}")
    lines.append(f"Recall    (macro) : {macro_r:.4f}")
    lines.append(f"F1        (macro) : {macro_f1:.4f}")
    lines.append(f"Precision (weighted): {w_p:.4f}")
    lines.append(f"Recall    (weighted): {w_r:.4f}")
    lines.append(f"F1        (weighted): {weighted_f1:.4f}")
    lines.append("")
    if missing:
        lines.append("NOT EVALUABLE - these intents have no test samples at all")
        lines.append("(too few distinct utterances in the source data):")
        lines.append("   " + ", ".join(missing))
        lines.append("")
    lines.append("-" * 78)
    lines.append("PER-INTENT CLASSIFICATION REPORT")
    lines.append("-" * 78)
    lines.append(report)

    # --- per-intent table -------------------------------------------------
    p, r, f, s = precision_recall_fscore_support(
        y_true, y_pred, labels=present, zero_division=0)
    per_intent = pd.DataFrame({
        "intent": present_labels,
        "precision": p, "recall": r, "f1": f, "support": s,
    }).sort_values("f1", ascending=False)

    lines.append("-" * 78)
    lines.append("PER-INTENT PERFORMANCE (sorted by F1)")
    lines.append("-" * 78)
    lines.append(f"{'Intent':<26}{'Precision':>10}{'Recall':>10}{'F1':>10}{'Support':>9}")
    for _, row in per_intent.iterrows():
        lines.append(f"{row['intent']:<26}{row['precision']:>10.4f}"
                     f"{row['recall']:>10.4f}{row['f1']:>10.4f}{int(row['support']):>9}")

    scored = per_intent[per_intent["support"] > 0]
    if not scored.empty:
        best = scored.iloc[0]
        worst = scored.iloc[-1]
        lines.append("")
        lines.append(f"Best-performing intent : {best['intent']} "
                     f"(F1 {best['f1']:.4f}, support {int(best['support'])})")
        lines.append(f"Worst-performing intent: {worst['intent']} "
                     f"(F1 {worst['f1']:.4f}, support {int(worst['support'])})")

    # --- confusion matrix -------------------------------------------------
    cm = confusion_matrix(y_true, y_pred, labels=present)
    confusions = top_confusions(cm, present_labels)
    lines.append("")
    lines.append("-" * 78)
    lines.append("MOST COMMON CONFUSIONS (actual -> predicted)")
    lines.append("-" * 78)
    if confusions:
        for actual, predicted, count, share in confusions:
            lines.append(f"   {actual:<24} -> {predicted:<24} "
                         f"{count:3d} ({share:.0%} of that intent)")
    else:
        lines.append("   none - every test sample was classified correctly")

    # --- confidence behaviour --------------------------------------------
    conf = probs.max(axis=1)
    correct = y_pred == y_true
    lines.append("")
    lines.append("-" * 78)
    lines.append("CONFIDENCE (softmax maximum)")
    lines.append("-" * 78)
    lines.append(f"   mean confidence, correct predictions : {conf[correct].mean():.4f}"
                 if correct.any() else "   no correct predictions")
    if (~correct).any():
        lines.append(f"   mean confidence, wrong predictions   : "
                     f"{conf[~correct].mean():.4f}")
    for thr in (0.5, 0.6, 0.7, 0.8, 0.9):
        kept = conf >= thr
        acc_kept = correct[kept].mean() if kept.any() else float("nan")
        lines.append(f"   threshold {thr:.2f}: keeps {kept.mean():6.1%} of calls, "
                     f"accuracy on kept {acc_kept:.4f}")

    text = "\n".join(lines)
    print(text)

    (OUTPUT_DIR / "classification_report.txt").write_text(text, encoding="utf-8")
    per_intent.to_csv(OUTPUT_DIR / "per_intent_metrics.csv", index=False,
                      encoding="utf-8")

    plot_confusion_matrix(cm, present_labels, OUTPUT_DIR / "confusion_matrix.png")
    plot_confusion_matrix(cm, present_labels,
                          OUTPUT_DIR / "confusion_matrix_normalized.png",
                          normalize=True)

    history_path = OUTPUT_DIR / "training_history.json"
    if history_path.exists():
        history = json.loads(history_path.read_text(encoding="utf-8"))["history"]
        plot_training_curves(history)

    # Machine-readable summary for the report / dashboard.
    (OUTPUT_DIR / "test_metrics.json").write_text(json.dumps({
        "accuracy": accuracy, "macro_f1": macro_f1, "weighted_f1": weighted_f1,
        "macro_precision": macro_p, "macro_recall": macro_r,
        "weighted_precision": w_p, "weighted_recall": w_r,
        "n_test": int(len(y_true)), "not_evaluable": missing,
    }, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"artefacts written to {OUTPUT_DIR}")
    print("=" * 78)


if __name__ == "__main__":
    main()
