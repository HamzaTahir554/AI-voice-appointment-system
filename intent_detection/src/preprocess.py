"""
Dataset inspection, cleaning, label unification and leakage-free splitting.

Run directly to regenerate `data/intents.csv` and `data/splits.csv`:

    python src/preprocess.py

The three raw CSVs shipped with this FYP use three *different* schemas and
three *different* label vocabularies, so this module:

  1. auto-detects the input-text column and the intent column of each file,
  2. harvests parallel Urdu-script columns as extra multilingual samples,
  3. maps every raw label onto one canonical 31-intent taxonomy,
  4. normalises whitespace / Unicode while preserving Urdu + Roman Urdu,
  5. removes exact duplicates,
  6. splits 80/10/10 with stratification AND grouping (no leakage).
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.api import types as pt

from config import (
    DATA_DIR,
    PROCESSED_CSV,
    RAW_DATA_DIR,
    SEED,
    SPLITS_CSV,
    TEST_RATIO,
    VAL_RATIO,
    set_seed,
    use_utf8_stdout,
)

# --------------------------------------------------------------------------
# Column auto-detection hints
# --------------------------------------------------------------------------
# Words that mark a column as "something the *patient* said" (our model input).
INPUT_HINTS = ("query", "input", "message", "utterance", "text", "sentence",
               "question", "patient", "user")
# Words that mark a column as a *system* reply -> never a model input.
RESPONSE_HINTS = ("response", "answer", "reply", "assistant", "ai_", "bot", "output")
# Words that mark a column as the intent/label.
LABEL_HINTS = ("intent", "label", "class", "category", "feature", "tag", "type")
# Words marking a non-Latin parallel translation of the same utterance.
SCRIPT_HINTS = ("urdu", "roman", "translit", "hindi", "native")

# --------------------------------------------------------------------------
# Canonical intent taxonomy
# --------------------------------------------------------------------------
# Keys are lower-cased raw labels exactly as they appear in the CSVs; values
# are the canonical intent names the model is actually trained on.
CANONICAL_INTENT_MAP = {
    # ---- appointment ----------------------------------------------------
    "book": "book_appointment",
    "booking": "book_appointment",
    "book_appointment": "book_appointment",
    "check_availability": "check_availability",
    "ask_availability": "check_availability",
    "appointment_status": "appointment_status",
    "confirmation": "appointment_confirmation",
    "appointment_confirmation": "appointment_confirmation",
    "cancel": "cancel_appointment",
    "cancellation": "cancel_appointment",
    "cancel_appointment": "cancel_appointment",
    "reschedule": "reschedule_appointment",
    "rescheduling": "reschedule_appointment",
    "reschedule_appointment": "reschedule_appointment",
    # ---- doctor ---------------------------------------------------------
    "find_doctor": "find_doctor",
    "doctor_info_inquiry": "doctor_information",
    "doctor_info": "doctor_information",
    "doctor_information": "doctor_information",
    # `doctor_experience` is folded into qualifications: the requested
    # taxonomy has no separate experience intent, and both questions are
    # answered from the same profile fields.
    "exp": "doctor_qualifications",
    "doctor_experience_inquiry": "doctor_qualifications",
    "doctor_experience": "doctor_qualifications",
    "qual": "doctor_qualifications",
    "doctor_qualification_inquiry": "doctor_qualifications",
    "doctor_qualification": "doctor_qualifications",
    "doctor_qualifications": "doctor_qualifications",
    "doctor_specialization": "doctor_specialization",
    "fee": "doctor_fee",
    "doctor_fee_inquiry": "doctor_fee",
    "doctor_fee": "doctor_fee",
    "doctor_unavailable": "doctor_unavailable",
    # ---- clinic ---------------------------------------------------------
    "address": "clinic_location",
    "clinic_address_inquiry": "clinic_location",
    "clinic_address": "clinic_location",
    "clinic_location": "clinic_location",
    "hours": "clinic_timing",
    "clinic_hours_inquiry": "clinic_timing",
    "clinic_timing": "clinic_timing",
    "clinic_closed": "clinic_closed",
    # ---- patient information --------------------------------------------
    "provide_patient_name": "provide_patient_name",
    "provide_patient_phone": "provide_patient_phone",
    "provide_patient_age": "provide_patient_age",
    "provide_patient_gender": "provide_patient_gender",
    # ---- appointment modification ---------------------------------------
    "change_doctor": "change_doctor",
    "change_date": "change_date",
    "change_time": "change_time",
    # ---- conversation ---------------------------------------------------
    "greeting": "greeting",
    "goodbye": "goodbye",
    "confirm": "confirm",
    "deny": "deny",
    "help": "help",
    "repeat_information": "repeat_information",
    "unclear_request": "unclear_request",
    "thank_you": "thank_you",
    # ---- safety-critical (kept deliberately) ----------------------------
    "emergency": "emergency",
}


# --------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------
# Zero-width joiners and bidirectional control marks travel with copy-pasted
# Urdu and would otherwise create "different" strings that look identical.
_INVISIBLES = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")
_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: object) -> str:
    """
    Clean an utterance without destroying meaning.

    We deliberately DO NOT lower-case (mBERT is a *cased* model), do not strip
    stopwords, do not stem and do not lemmatise - a fine-tuned transformer
    wants the sentence as the patient actually said it. Punctuation is kept
    because a question mark is a genuine signal for the inquiry intents.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value)
    text = unicodedata.normalize("NFKC", text)   # canonical Unicode form
    text = _INVISIBLES.sub("", text)
    text = _WHITESPACE.sub(" ", text)            # collapse runs of whitespace
    return text.strip()


# --------------------------------------------------------------------------
# Script detection (English / Roman Urdu / Urdu)
# --------------------------------------------------------------------------
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Frequent Roman-Urdu function words - enough to separate it from plain English.
_ROMAN_URDU = re.compile(
    r"\b(mujhe|mujhy|mera|meri|kya|kaisay|kaise|hai|hain|karna|karni|karo|"
    r"chahiye|chahie|kab|kahan|kitna|kitni|nahi|nhi|acha|theek|sahib|"
    r"batao|dena|deni|lena|leni|baithte|milna|waqt|din|apna|apni)\b", re.I)


def detect_script(value: object) -> str:
    """Label an utterance as urdu / devanagari / roman_urdu / english."""
    text = str(value)
    if _ARABIC.search(text):
        return "urdu"
    if _DEVANAGARI.search(text):
        return "devanagari"
    if _ROMAN_URDU.search(text):
        return "roman_urdu"
    return "english"


# --------------------------------------------------------------------------
# Column detection
# --------------------------------------------------------------------------
def _is_texty(series: pd.Series) -> bool:
    """True for genuine string columns (pandas 3 uses a dedicated str dtype)."""
    return pt.is_string_dtype(series) and not pt.is_numeric_dtype(series)


def _name_has(name: str, hints: tuple) -> bool:
    low = str(name).lower()
    return any(h in low for h in hints)


def _text_score(name: str, series: pd.Series) -> float:
    """Score how much a column looks like the patient's utterance."""
    if not _is_texty(series):
        return -1e9
    values = series.dropna().astype(str)
    if values.empty:
        return -1e9
    avg_words = float(values.str.split().str.len().mean())
    uniq_ratio = values.nunique() / max(len(values), 1)
    score = avg_words * 0.5 + uniq_ratio * 2.0
    if _name_has(name, INPUT_HINTS):
        score += 5.0
    if _name_has(name, RESPONSE_HINTS):      # a reply is never the model input
        score -= 20.0
    if _name_has(name, SCRIPT_HINTS):        # prefer the primary column first
        score -= 1.0
    return score


def _label_purity(df: pd.DataFrame, text_col: str, cand: str) -> float:
    """Fraction of distinct utterances that map to exactly one label value."""
    pair = df[[text_col, cand]].dropna().astype(str)
    if pair.empty:
        return 0.0
    return float((pair.groupby(text_col)[cand].nunique() == 1).mean())


def _label_score(name: str, series: pd.Series, purity: float) -> float:
    """Score how much a column looks like the intent label."""
    values = series.dropna().astype(str)
    avg_words = float(values.str.split().str.len().mean())
    score = purity * 10.0
    score -= avg_words                       # labels are short, sentences are not
    if _name_has(name, LABEL_HINTS):
        score += 5.0
    if _name_has(name, RESPONSE_HINTS) or _name_has(name, SCRIPT_HINTS):
        score -= 8.0                         # e.g. a translated copy of the label
    return score


def detect_columns(df: pd.DataFrame) -> dict:
    """
    Work out which column holds the patient's text, which holds the intent,
    and which (if any) hold a parallel translation of the same utterance.

    Nothing here is hard-coded to a particular CSV: the decision is made from
    the dtypes, the cardinality, the average length and the column names of
    whatever file is passed in.
    """
    text_scores = {c: _text_score(c, df[c]) for c in df.columns}
    text_col = max(text_scores, key=text_scores.get)
    if text_scores[text_col] <= -1e8:
        raise ValueError("No usable text column found in this file.")

    # Parallel columns: another *input* column written in a different script.
    parallel = [
        c for c in df.columns
        if c != text_col
        and _is_texty(df[c])
        and _name_has(c, SCRIPT_HINTS)
        and _name_has(c, INPUT_HINTS)
        and not _name_has(c, RESPONSE_HINTS)
    ]

    # Label candidates: low-cardinality columns that are not the text itself.
    label_scores = {}
    for c in df.columns:
        if c == text_col or c in parallel:
            continue
        n_unique = int(df[c].nunique())
        if not (2 <= n_unique <= 100):        # too few / too many to be intents
            continue
        label_scores[c] = _label_score(c, df[c], _label_purity(df, text_col, c))
    if not label_scores:
        raise ValueError("No usable intent column found in this file.")
    label_col = max(label_scores, key=label_scores.get)

    return {
        "text_col": text_col,
        "label_col": label_col,
        "parallel_cols": parallel,
        "text_scores": text_scores,
        "label_scores": label_scores,
    }


# --------------------------------------------------------------------------
# Inspection report (requirement section 2)
# --------------------------------------------------------------------------
def inspect_file(path: Path) -> dict:
    """Print the full inspection report for one raw CSV and return its schema."""
    df = pd.read_csv(path, encoding="utf-8")
    schema = detect_columns(df)
    text_col, label_col = schema["text_col"], schema["label_col"]

    print("=" * 78)
    print("FILE: " + path.name)
    print("=" * 78)
    print(f"1. Shape                : {df.shape[0]} rows x {df.shape[1]} columns")
    print(f"3. Columns              : {list(df.columns)}")
    print(f"   -> detected TEXT     : {text_col}")
    print(f"   -> detected INTENT   : {label_col}")
    print(f"   -> parallel (script) : {schema['parallel_cols'] or 'none'}")

    print("\n2. First 5 rows (detected columns only):")
    preview = df[[text_col, label_col]].head(5)
    for i, row in preview.iterrows():
        print(f"   [{i}] {str(row[text_col])[:64]!r} -> {row[label_col]}")

    print(f"\n4. Unique intent labels ({df[label_col].nunique()}):")
    print("   " + str(sorted(df[label_col].astype(str).unique())))

    print("\n5./8. Samples per intent (class distribution):")
    counts = df[label_col].value_counts()
    for lab, n in counts.items():
        bar = "#" * max(1, int(40 * n / counts.max()))
        print(f"   {str(lab):32s} {n:5d}  {bar}")
    imbalance = counts.max() / counts.min()
    verdict = "IMBALANCED" if imbalance > 3 else "roughly balanced"
    print(f"   imbalance ratio (max/min) = {imbalance:.1f}x  ->  {verdict}")

    print("\n6. Missing values per column:")
    nulls = df.isna().sum()
    for c in df.columns:
        flag = "   <-- model column" if c in (text_col, label_col) else ""
        print(f"   {c:32s} {nulls[c]:5d}{flag}")

    print("\n7. Duplicate statistics:")
    print(f"   fully duplicated rows        : {int(df.duplicated().sum())}")
    print(f"   duplicated (text,intent)     : "
          f"{int(df.duplicated([text_col, label_col]).sum())}")
    print(f"   distinct utterances          : {df[text_col].nunique()} of {len(df)}")

    print("\n9. Language / script distribution of the text column:")
    for script, n in df[text_col].map(detect_script).value_counts().items():
        print(f"   {script:12s} {n:5d}")
    print()
    schema["df"] = df
    return schema


# --------------------------------------------------------------------------
# Union-find, used to keep near-identical rows inside one split
# --------------------------------------------------------------------------
class _UnionFind:
    """Tiny disjoint-set structure for merging leakage groups."""

    def __init__(self) -> None:
        self.parent = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]   # path compression
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# --------------------------------------------------------------------------
# Build the unified dataset
# --------------------------------------------------------------------------
def build_dataset(verbose: bool = True) -> pd.DataFrame:
    """Read every raw CSV, unify it and return the cleaned, deduplicated frame."""
    csv_paths = sorted(RAW_DATA_DIR.glob("*.csv"))
    if not csv_paths:
        raise FileNotFoundError(f"No CSV files found in {RAW_DATA_DIR}")

    records = []
    unmapped = defaultdict(int)

    for path in csv_paths:
        if verbose:
            schema = inspect_file(path)
        else:
            df_tmp = pd.read_csv(path, encoding="utf-8")
            schema = detect_columns(df_tmp)
            schema["df"] = df_tmp

        df = schema["df"]
        text_col = schema["text_col"]
        label_col = schema["label_col"]
        parallel = schema["parallel_cols"]

        for idx, row in df.iterrows():
            raw_label = normalize_text(row[label_col])
            canonical = CANONICAL_INTENT_MAP.get(raw_label.lower())
            if canonical is None:
                unmapped[raw_label] += 1
                continue

            # One group id per *source row*: the Urdu translation of an
            # utterance must never land in a different split than its
            # Roman-Urdu twin, or the test score would be inflated.
            group = f"{path.stem}#{idx}"

            for col in [text_col] + list(parallel):
                text = normalize_text(row[col])
                if not text:                       # missing / empty text
                    continue
                records.append({
                    "text": text,
                    "label": canonical,
                    "raw_label": raw_label,
                    "source": path.name,
                    "group": group,
                    "script": detect_script(text),
                })

    if unmapped:
        raise ValueError(
            "These raw labels are missing from CANONICAL_INTENT_MAP - add them "
            f"before training: {dict(unmapped)}"
        )

    data = pd.DataFrame.from_records(records)
    n_raw = len(data)

    # --- cleaning --------------------------------------------------------
    data = data.dropna(subset=["text", "label"])         # missing text/label
    data = data[data["text"].str.len() > 0]              # completely empty text
    n_after_empty = len(data)

    # --- exact duplicate removal ----------------------------------------
    data = data.drop_duplicates(subset=["text", "label"], keep="first")

    # Evaluation phrases (challenge, audit and holdout sets) must never be
    # trained on, whichever raw CSV they happen to appear in.
    from challenge_set import is_challenge
    held_out = data["text"].map(is_challenge)
    if verbose:
        print(f"   held out {int(held_out.sum())} rows matching the evaluation sets")
    data = data[~held_out].copy()
    n_after_dedupe = len(data)

    # --- merge groups that share a case-insensitive utterance ------------
    # Prevents "Book appointment" / "book appointment" straddling two splits.
    uf = _UnionFind()
    by_key = {}
    for _, row in data.iterrows():
        key = row["text"].casefold()
        if key in by_key:
            uf.union(by_key[key], row["group"])
        else:
            by_key[key] = row["group"]
    data["group"] = data["group"].map(uf.find)
    data = data.reset_index(drop=True)

    if verbose:
        print("=" * 78)
        print("UNIFIED DATASET")
        print("=" * 78)
        print(f"rows harvested (incl. parallel Urdu) : {n_raw}")
        print(f"after dropping empty/missing text    : {n_after_empty}")
        print(f"after exact duplicate removal        : {n_after_dedupe}")
        print(f"leakage-proof groups                 : {data['group'].nunique()}")
        print(f"canonical intents                    : {data['label'].nunique()}")
        print("\nFinal class distribution:")
        counts = data["label"].value_counts()
        for lab, n in counts.items():
            bar = "#" * max(1, int(40 * n / counts.max()))
            print(f"   {lab:24s} {n:5d} ({100 * n / len(data):5.1f}%)  {bar}")
        print("\nScript distribution:")
        for script, n in data["script"].value_counts().items():
            print(f"   {script:12s} {n:5d} ({100 * n / len(data):5.1f}%)")
    return data


# --------------------------------------------------------------------------
# Label encoding
# --------------------------------------------------------------------------
def build_label_maps(data: pd.DataFrame):
    """Create deterministic label2id / id2label mappings from the data."""
    labels = sorted(data["label"].unique())          # sorted -> reproducible
    label2id = {lab: i for i, lab in enumerate(labels)}
    id2label = {i: lab for lab, i in label2id.items()}
    return label2id, id2label


def save_label_maps(label2id: dict, id2label: dict, out_dir: Path) -> None:
    """Persist the mappings so inference never has to guess the label order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "label2id.json").write_text(
        json.dumps(label2id, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "id2label.json").write_text(
        json.dumps({str(k): v for k, v in id2label.items()}, indent=2,
                   ensure_ascii=False), encoding="utf-8")


def load_label_maps(out_dir: Path):
    """Read the mappings back (used by train/evaluate/inference)."""
    label2id = json.loads((out_dir / "label2id.json").read_text(encoding="utf-8"))
    id2label = {int(k): v for k, v in
                json.loads((out_dir / "id2label.json").read_text(encoding="utf-8")).items()}
    return label2id, id2label


# --------------------------------------------------------------------------
# Stratified + group-aware split
# --------------------------------------------------------------------------
def split_dataset(data: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    """
    Assign every row to train / val / test.

    Splitting happens at *group* level, never at row level, so parallel
    translations and case-variants of one utterance stay together. Within each
    intent the groups are shuffled with a fixed seed and dealt out 80/10/10,
    which keeps the split stratified.

    Intents with fewer than 3 distinct groups cannot appear in all three
    splits; those go entirely to train and are reported as not evaluable.
    """
    assignment = {}
    not_evaluable = []

    for label, chunk in data.groupby("label"):
        groups = sorted(chunk["group"].unique())      # sorted -> reproducible
        rs = np.random.RandomState(SEED)
        rs.shuffle(groups)
        n = len(groups)

        if n < 3:
            # Too few distinct utterances to hold any out.
            for g in groups:
                assignment[g] = "train"
            plural = "s" if n != 1 else ""
            not_evaluable.append(f"{label} ({n} group{plural})")
            continue

        # Guarantee at least one group in val and one in test.
        n_test = max(1, int(round(n * TEST_RATIO)))
        n_val = max(1, int(round(n * VAL_RATIO)))
        if n_test + n_val >= n:                       # keep train non-empty
            n_test, n_val = 1, 1
        for g in groups[:n_test]:
            assignment[g] = "test"
        for g in groups[n_test:n_test + n_val]:
            assignment[g] = "val"
        for g in groups[n_test + n_val:]:
            assignment[g] = "train"

    data = data.copy()
    data["split"] = data["group"].map(assignment)

    if verbose:
        print("\n" + "=" * 78)
        print("TRAIN / VALIDATION / TEST SPLIT (stratified, group-aware)")
        print("=" * 78)
        for split in ("train", "val", "test"):
            n = int((data["split"] == split).sum())
            print(f"   {split:6s} {n:5d} rows ({100 * n / len(data):5.1f}%)")

        # Leakage assertions: a group must never span two splits and no exact
        # utterance may appear in both train and test.
        spanning = data.groupby("group")["split"].nunique()
        assert (spanning == 1).all(), "LEAKAGE: a group spans multiple splits"
        train_texts = set(data[data.split == "train"].text.str.casefold())
        test_texts = set(data[data.split == "test"].text.str.casefold())
        overlap = train_texts & test_texts
        print(f"   train/test text overlap: {len(overlap)} (must be 0)")
        assert not overlap, "LEAKAGE: identical text in train and test"

        print("\nPer-intent split counts:")
        pivot = pd.crosstab(data["label"], data["split"])
        for col in ("train", "val", "test"):
            if col not in pivot.columns:
                pivot[col] = 0
        print(pivot[["train", "val", "test"]].to_string())

        if not_evaluable:
            print("\n   !! WARNING - too few distinct utterances to evaluate:")
            for item in not_evaluable:
                print(f"      - {item}: all rows sent to TRAIN, absent from val/test")
    return data


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    use_utf8_stdout()
    set_seed()

    data = build_dataset(verbose=True)
    data = split_dataset(data, verbose=True)

    label2id, id2label = build_label_maps(data)
    save_label_maps(label2id, id2label, DATA_DIR)

    data.to_csv(PROCESSED_CSV, index=False, encoding="utf-8")
    data.to_csv(SPLITS_CSV, index=False, encoding="utf-8")

    print("\n" + "=" * 78)
    print("SAVED")
    print("=" * 78)
    print(f"   label2id  -> {DATA_DIR / 'label2id.json'}")
    print(f"   id2label  -> {DATA_DIR / 'id2label.json'}")
    print(f"   processed -> {PROCESSED_CSV}")
    print(f"   splits    -> {SPLITS_CSV}")
    print(f"\n   {len(label2id)} intents: {list(label2id)}")


if __name__ == "__main__":
    main()
