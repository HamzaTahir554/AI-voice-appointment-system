"""
Torch dataset, tokenisation and shared model/metric helpers.

Kept separate from `train.py` so that `evaluate.py` and `inference.py` can
reuse exactly the same tokenisation settings - a mismatch between training and
inference tokenisation is one of the most common silent bugs in NLP projects.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader, Dataset

from config import MAX_LENGTH, MODEL_NAME


class IntentDataset(Dataset):
    """
    Wraps (text, label_id) pairs and tokenises them for mBERT.

    Tokenisation happens once in __init__ rather than per __getitem__ because
    the dataset is small (about 1k rows) and this keeps every training epoch
    free of tokeniser overhead.
    """

    def __init__(self, texts, labels, tokenizer, max_length: int = MAX_LENGTH):
        self.texts = list(texts)
        self.labels = list(labels)

        # padding="max_length" gives every sample an identical shape, which is
        # what the requirement asks for and what makes memory use predictable.
        encoded = tokenizer(
            self.texts,
            max_length=max_length,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        self.input_ids = encoded["input_ids"]
        self.attention_mask = encoded["attention_mask"]
        # mBERT is a single-sentence classifier here, but keep token_type_ids
        # when the tokenizer produces them so the model signature matches.
        self.token_type_ids = encoded.get("token_type_ids")

    def __len__(self) -> int:
        return len(self.texts)

    def __getitem__(self, idx: int) -> dict:
        item = {
            "input_ids": self.input_ids[idx],
            "attention_mask": self.attention_mask[idx],
            "labels": torch.tensor(self.labels[idx], dtype=torch.long),
        }
        if self.token_type_ids is not None:
            item["token_type_ids"] = self.token_type_ids[idx]
        return item


def load_splits(splits_csv: Path, label2id: dict):
    """Read splits.csv and return the three dataframes with an added label_id."""
    data = pd.read_csv(splits_csv, encoding="utf-8")
    missing = set(data["label"]) - set(label2id)
    if missing:
        raise ValueError(f"splits.csv contains labels absent from label2id: {missing}")
    data["label_id"] = data["label"].map(label2id)

    return (
        data[data["split"] == "train"].reset_index(drop=True),
        data[data["split"] == "val"].reset_index(drop=True),
        data[data["split"] == "test"].reset_index(drop=True),
    )


def make_loader(frame: pd.DataFrame, tokenizer, batch_size: int,
                max_length: int, shuffle: bool) -> DataLoader:
    """Build a DataLoader for one split."""
    dataset = IntentDataset(
        frame["text"].tolist(), frame["label_id"].tolist(), tokenizer, max_length
    )
    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        # num_workers=0 keeps things safe and fast on Windows, where spawning
        # worker processes is expensive and often breaks in notebooks.
        num_workers=0,
        pin_memory=torch.cuda.is_available(),
    )


def compute_class_weights(label_ids, num_labels: int,
                          power: float = 1.0) -> torch.Tensor:
    """
    Softened inverse-frequency class weights for the loss function.

    Our data is 76% `book_appointment`, so an unweighted model can score 76%
    accuracy by always predicting that one intent. Weighting the loss makes a
    mistake on a rare intent cost proportionally more.

    `power` tempers how hard we push. Full inverse frequency (power=1.0) spans
    166x on this dataset, which over-corrects so badly that the model predicts
    rare intents for almost everything (measured: `doctor_fee` precision 0.08,
    recall 1.00). power=0.5 (square root) spans 13x and is much more stable.
    power=0.0 returns uniform weights.
    """
    counts = np.bincount(np.asarray(label_ids), minlength=num_labels).astype(np.float64)
    counts[counts == 0] = 1.0                     # avoid divide-by-zero
    weights = counts.sum() / (num_labels * counts)
    weights = weights ** power
    # Re-centre so the average weight is 1.0; this keeps the loss magnitude
    # (and therefore a sensible learning rate) comparable across settings.
    weights = weights / weights.mean()
    return torch.tensor(weights, dtype=torch.float)


def resolve_device(prefer_cuda: bool = True) -> torch.device:
    """Pick CUDA when it is genuinely usable, otherwise fall back to CPU."""
    if prefer_cuda and torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def describe_device(device: torch.device) -> None:
    """Print a clear report of what we are training on."""
    print("-" * 78)
    print(f"torch version        : {torch.__version__}")
    print(f"CUDA available       : {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        print(f"GPU name             : {props.name}")
        print(f"GPU memory           : {props.total_memory / 1024 ** 3:.1f} GB")
    else:
        # This is the single most common surprise on a laptop with an NVIDIA
        # card: a CPU-only wheel was installed, so the GPU is simply invisible.
        cuda_build = torch.version.cuda
        if cuda_build is None:
            print("GPU name             : (none visible - this is a CPU-only "
                  "torch build)")
            print("                       reinstall torch with a CUDA wheel to "
                  "use your GPU")
    print(f"training device      : {device}")
    print("-" * 78)


def load_model_and_tokenizer(num_labels: int, id2label: dict, label2id: dict,
                             model_name: str = MODEL_NAME):
    """
    Load mBERT with a freshly initialised classification head.

    `AutoModelForSequenceClassification` attaches a linear layer on top of the
    pooled [CLS] representation and sizes it to `num_labels`. The whole stack
    (encoder + head) is trainable: mBERT is fine-tuned, not frozen.
    """
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name,
        num_labels=num_labels,
        id2label=id2label,
        label2id=label2id,
    )
    return model, tokenizer
