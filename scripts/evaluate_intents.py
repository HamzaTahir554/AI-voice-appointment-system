"""
Intent audit - scores the fine-tuned mBERT model on hand-written test sets.

Sets (all held out of training, see intent_detection/src/challenge_set.py):

  challenge  the Roman-Urdu booking phrases and look-alikes from the
             "Mje docter ke pas jana h" fix
  audit      every intent in English, Urdu, Roman Urdu and code-mixed speech,
             including misspellings, abbreviations and similar-intent traps.
             Used to FIND weaknesses.
  holdout    different phrasings, scored only. Its individual failures are
             not printed unless --show-holdout is given, so that the training
             data is never tuned towards it.

Each phrase is scored three ways:

  raw        the mBERT label is one of the acceptable labels
  canonical  after INTENT_MAPPING (what the Dialog Manager acts on)
  router     the full IntentRouter: confidence threshold + semantic layer

    python scripts/evaluate_intents.py --tag before
    python scripts/evaluate_intents.py --tag after --show-holdout

Writes reports/intent_audit/<tag>_{cases,per_intent,confusion}.csv and
<tag>_summary.json.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config import INTENT_MAPPING, INTENT_MODULE_DIR, Intent  # noqa: E402
from dialog_manager.intent_router import IntentRouter, MBertIntentDetector  # noqa: E402

OUT_DIR = REPO / "reports" / "intent_audit"


def load_src(name: str):
    """Import a module from intent_detection/src by path (avoids config clash)."""
    path = INTENT_MODULE_DIR / "src" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_audit_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CachedDetector:
    """Runs mBERT once per text and records the latency of that one call."""

    def __init__(self, inner):
        self.inner = inner
        self.cache: dict[str, tuple[dict, float]] = {}

    def predict(self, text: str) -> dict:
        if text not in self.cache:
            started = time.perf_counter()
            result = self.inner.predict(text)
            self.cache[text] = (result, (time.perf_counter() - started) * 1000)
        return self.cache[text][0]

    def latency_ms(self, text: str) -> float:
        return self.cache[text][1]


def load_corpus(challenge) -> list[tuple[str, str, set[str]]]:
    """(normalised text, split, token set) for every training-corpus row."""
    path = INTENT_MODULE_DIR / "data" / "splits.csv"
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            norm = challenge.normalise(row["text"])
            rows.append((norm, row["split"], set(norm.split())))
    return rows


def nearest_in_corpus(norm: str, corpus) -> tuple[str, float, str]:
    """Exact match, else the most similar corpus row by token Jaccard."""
    tokens = set(norm.split())
    best, best_split, best_score = "", "", 0.0
    for text, split, other in corpus:
        if text == norm:
            return text, 1.0, split
        if not tokens or not other:
            continue
        score = len(tokens & other) / len(tokens | other)
        if score > best_score:
            best, best_split, best_score = text, split, score
    return best, best_score, best_split


def build_cases(challenge, audit) -> list[tuple]:
    cases = [(t, tuple(labels), "ru", "challenge_booking" if "book_appointment" in labels
              else "challenge_lookalike", "challenge")
             for t, labels in ([(b, {"book_appointment"}) for b in challenge.BOOKING]
                               + list(challenge.LOOKALIKES))]
    cases += [(t, labels, lang, cat, "audit") for t, labels, lang, cat in audit.AUDIT]
    cases += [(t, labels, lang, cat, "holdout") for t, labels, lang, cat in audit.HOLDOUT]
    return cases


def per_intent_metrics(rows: list[dict]) -> tuple[list[dict], dict]:
    """Precision / recall / F1 per raw label, crediting any acceptable label."""
    from sklearn.metrics import precision_recall_fscore_support

    y_true, y_pred = [], []
    for row in rows:
        acceptable = row["expected"].split("|")
        predicted = row["predicted_raw"]
        y_true.append(predicted if predicted in acceptable else acceptable[0])
        y_pred.append(predicted)
    labels = sorted(set(y_true) | set(y_pred))
    p, r, f, s = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, zero_division=0)
    table = [{"intent": lab, "precision": round(float(p[i]), 4),
              "recall": round(float(r[i]), 4), "f1": round(float(f[i]), 4),
              "support": int(s[i])} for i, lab in enumerate(labels)]
    supported = [row for row in table if row["support"] > 0]
    total = sum(row["support"] for row in supported) or 1
    summary = {
        "accuracy": round(sum(t == p for t, p in zip(y_true, y_pred)) / max(len(rows), 1), 4),
        "macro_precision": round(sum(x["precision"] for x in supported) / max(len(supported), 1), 4),
        "macro_recall": round(sum(x["recall"] for x in supported) / max(len(supported), 1), 4),
        "macro_f1": round(sum(x["f1"] for x in supported) / max(len(supported), 1), 4),
        "weighted_f1": round(sum(x["f1"] * x["support"] for x in supported) / total, 4),
    }
    return table, {"summary": summary, "y_true": y_true, "y_pred": y_pred, "labels": labels}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--tag", default="latest")
    parser.add_argument("--show-holdout", action="store_true")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    challenge, audit = load_src("challenge_set"), load_src("audit_set")
    corpus = load_corpus(challenge)

    detector = CachedDetector(MBertIntentDetector())
    model_only = IntentRouter(detector=detector, use_semantic_fallback=False)
    full = IntentRouter(detector=detector)

    rows = []
    for text, labels, lang, category, set_name in build_cases(challenge, audit):
        model = model_only.route(text)
        routed = full.route(text)
        expected_canonical = {INTENT_MAPPING.get(label, Intent.UNKNOWN) for label in labels}
        norm = challenge.normalise(text)
        near_text, near_score, near_split = nearest_in_corpus(norm, corpus)
        rows.append({
            "set": set_name, "language": lang, "category": category, "text": text,
            "expected": "|".join(labels),
            "predicted_raw": model.raw_intent,
            "confidence": round(model.confidence, 4),
            "band": model.band,
            "raw_correct": model.raw_intent in labels,
            "expected_canonical": "|".join(sorted(expected_canonical)),
            "canonical_correct": INTENT_MAPPING.get(model.raw_intent) in expected_canonical,
            "router_intent": routed.intent,
            "router_correct": routed.intent in expected_canonical,
            "semantic_override": routed.corroborated,
            "latency_ms": round(detector.latency_ms(text), 2),
            # Exact (normalised) match only - the same words in another order
            # are a near-duplicate, not a leak.
            "in_corpus": near_text == norm,
            "nearest_corpus_similarity": round(near_score, 3),
            "nearest_corpus_split": near_split,
            "nearest_corpus_text": near_text,
        })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.tag
    with open(OUT_DIR / f"{tag}_cases.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    summary: dict = {"tag": tag, "model_dir": str(INTENT_MODULE_DIR / "models" / "mbert_intent_classifier"),
                     "sets": {}}
    for set_name in ("challenge", "audit", "holdout"):
        subset = [r for r in rows if r["set"] == set_name]
        table, extra = per_intent_metrics(subset)
        n = len(subset)
        block = {
            "cases": n,
            "raw_accuracy": round(sum(r["raw_correct"] for r in subset) / n, 4),
            "canonical_accuracy": round(sum(r["canonical_correct"] for r in subset) / n, 4),
            "router_accuracy": round(sum(r["router_correct"] for r in subset) / n, 4),
            "metrics": extra["summary"],
            "leaked_into_corpus": sum(r["in_corpus"] for r in subset),
            "near_duplicates_ge_0_8": sum(r["nearest_corpus_similarity"] >= 0.8 and not r["in_corpus"]
                                          for r in subset),
            "by_language": {}, "by_category": {},
        }
        for key, field in (("by_language", "language"), ("by_category", "category")):
            groups = defaultdict(list)
            for r in subset:
                groups[r[field]].append(r)
            block[key] = {g: {"cases": len(v), "raw_accuracy": round(sum(x["raw_correct"] for x in v) / len(v), 4)}
                          for g, v in sorted(groups.items())}
        summary["sets"][set_name] = block
        if set_name == "audit":
            with open(OUT_DIR / f"{tag}_per_intent.csv", "w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["intent", "precision", "recall", "f1", "support"])
                writer.writeheader()
                writer.writerows(table)
            labels = extra["labels"]
            counts = Counter(zip(extra["y_true"], extra["y_pred"]))
            with open(OUT_DIR / f"{tag}_confusion.csv", "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["actual \\ predicted"] + labels)
                for actual in labels:
                    writer.writerow([actual] + [counts.get((actual, p), 0) for p in labels])

    latencies = sorted(detector.latency_ms(t) for t in detector.cache)
    summary["inference_latency_ms"] = {
        "calls": len(latencies),
        "median": round(latencies[len(latencies) // 2], 2),
        "p95": round(latencies[int(len(latencies) * 0.95) - 1], 2),
        "max": round(latencies[-1], 2),
    }
    (OUT_DIR / f"{tag}_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                                 encoding="utf-8")

    print("=" * 100)
    print(f"INTENT AUDIT [{tag}]")
    print("=" * 100)
    for set_name, block in summary["sets"].items():
        m = block["metrics"]
        print(f"  {set_name:9s} n={block['cases']:3d}  raw {block['raw_accuracy']:.3f}  "
              f"canonical {block['canonical_accuracy']:.3f}  router {block['router_accuracy']:.3f}  "
              f"macroF1 {m['macro_f1']:.3f}  leaked {block['leaked_into_corpus']}  "
              f"near-dup {block['near_duplicates_ge_0_8']}")
        print(f"            by language: " + ", ".join(
            f"{k} {v['raw_accuracy']:.2f} ({v['cases']})" for k, v in block["by_language"].items()))
    print(f"  mBERT latency: {summary['inference_latency_ms']}")

    print("\nFAILURES (raw label not acceptable)")
    for r in rows:
        if r["raw_correct"] or (r["set"] == "holdout" and not args.show_holdout):
            continue
        flag = "" if r["canonical_correct"] else "  CANONICAL-WRONG"
        print(f"  [{r['set'][:5]}|{r['language']}|{r['category'][:10]:10s}] "
              f"{r['expected'][:34]:34s} -> {r['predicted_raw']:24s} {r['confidence']:.2f} "
              f"{r['band']:6s} router={r['router_intent']}{flag} | {r['text']}")
    if not args.show_holdout:
        print("  (holdout failures hidden - rerun with --show-holdout after the final training run)")
    print(f"\nwritten to {OUT_DIR}")
    # An evaluation phrase inside the training corpus makes its score
    # meaningless - fail loudly rather than report an inflated number.
    leaked = sum(block["leaked_into_corpus"] for block in summary["sets"].values())
    if leaked:
        print(f"FAILED: {leaked} evaluation phrases are present in the training corpus")
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
