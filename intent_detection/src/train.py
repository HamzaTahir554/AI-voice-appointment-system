"""
Fine-tune mBERT (bert-base-multilingual-cased) for clinic intent detection.

    python src/train.py                    # defaults from config.py
    python src/train.py --epochs 4 --batch-size 8 --no-fp16

The loop is written by hand rather than with `transformers.Trainer` so that
every step required by the FYP spec - gradient accumulation, mixed precision,
gradient clipping, early stopping, best-checkpointing - is visible and
explainable, and so the code does not break when the Trainer API changes
between transformers releases.
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import accuracy_score, f1_score
from transformers import get_linear_schedule_with_warmup

from config import (
    BATCH_SIZE,
    CHECKPOINT_DIR,
    CLASS_WEIGHT_POWER,
    DATA_DIR,
    EARLY_STOPPING_PATIENCE,
    EPOCHS,
    GRAD_ACCUM_STEPS,
    LEARNING_RATE,
    MAX_GRAD_NORM,
    MAX_LENGTH,
    MODEL_DIR,
    MODEL_NAME,
    OUTPUT_DIR,
    SEED,
    SPLITS_CSV,
    USE_CLASS_WEIGHTS,
    USE_FP16,
    WARMUP_RATIO,
    WEIGHT_DECAY,
    set_seed,
    use_utf8_stdout,
)
from dataset import (
    compute_class_weights,
    describe_device,
    load_model_and_tokenizer,
    load_splits,
    make_loader,
    resolve_device,
)
from preprocess import load_label_maps


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fine-tune mBERT for intent detection")
    p.add_argument("--epochs", type=int, default=EPOCHS)
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--grad-accum", type=int, default=GRAD_ACCUM_STEPS)
    p.add_argument("--lr", type=float, default=LEARNING_RATE)
    p.add_argument("--weight-decay", type=float, default=WEIGHT_DECAY)
    p.add_argument("--max-length", type=int, default=MAX_LENGTH)
    p.add_argument("--patience", type=int, default=EARLY_STOPPING_PATIENCE)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--model-name", type=str, default=MODEL_NAME)
    p.add_argument("--no-fp16", action="store_true",
                   help="disable mixed precision even when CUDA is available")
    p.add_argument("--no-class-weights", action="store_true",
                   help="use a plain (unweighted) cross-entropy loss")
    p.add_argument("--class-weight-power", type=float, default=CLASS_WEIGHT_POWER,
                   help="0=uniform, 0.5=sqrt inverse frequency, 1.0=full inverse")
    p.add_argument("--early-stop-metric", choices=("macro_f1", "val_loss"),
                   default="macro_f1",
                   help="metric used for best-checkpointing and early stopping")
    p.add_argument("--cpu", action="store_true", help="force CPU training")
    return p.parse_args()


# --------------------------------------------------------------------------
# One pass over a dataloader
# --------------------------------------------------------------------------
def run_epoch(model, loader, device, loss_fn, optimizer=None, scheduler=None,
              scaler=None, grad_accum: int = 1, train: bool = False,
              max_grad_norm: float = MAX_GRAD_NORM):
    """
    Run a single epoch. Returns (mean_loss, accuracy, macro_f1).

    When `train` is False the whole pass runs under torch.no_grad(), which
    roughly halves memory use during validation.
    """
    model.train() if train else model.eval()

    total_loss, n_batches = 0.0, 0
    all_preds, all_targets = [], []

    use_amp = scaler is not None and device.type == "cuda"

    if train:
        optimizer.zero_grad(set_to_none=True)

    for step, batch in enumerate(loader):
        labels = batch.pop("labels").to(device, non_blocking=True)
        batch = {k: v.to(device, non_blocking=True) for k, v in batch.items()}

        with torch.set_grad_enabled(train):
            # autocast runs the forward pass in fp16 on CUDA, cutting both
            # memory use and step time on a consumer GPU.
            with torch.amp.autocast(device_type=device.type, enabled=use_amp):
                outputs = model(**batch)
                # We compute the loss ourselves (instead of passing `labels`
                # to the model) so we can apply per-class weights.
                loss = loss_fn(outputs.logits, labels)

        if train:
            # Scale down so that accumulating `grad_accum` batches produces the
            # same gradient magnitude as one large batch.
            scaled = loss / grad_accum
            if use_amp:
                scaler.scale(scaled).backward()
            else:
                scaled.backward()

            is_last = (step + 1) == len(loader)
            if (step + 1) % grad_accum == 0 or is_last:
                if use_amp:
                    # Unscale before clipping, otherwise we would clip the
                    # fp16-scaled gradients rather than the real ones.
                    scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)
                    optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)

        total_loss += float(loss.detach().cpu())
        n_batches += 1
        all_preds.append(outputs.logits.detach().argmax(dim=-1).cpu().numpy())
        all_targets.append(labels.detach().cpu().numpy())

    preds = np.concatenate(all_preds)
    targets = np.concatenate(all_targets)
    return (
        total_loss / max(n_batches, 1),
        accuracy_score(targets, preds),
        f1_score(targets, preds, average="macro", zero_division=0),
    )


# --------------------------------------------------------------------------
# Training driver
# --------------------------------------------------------------------------
def train_once(args, batch_size: int) -> dict:
    """Run the full fine-tuning job at a given batch size."""
    set_seed(args.seed)

    label2id, id2label = load_label_maps(DATA_DIR)
    num_labels = len(label2id)

    device = resolve_device(prefer_cuda=not args.cpu)
    describe_device(device)

    model, tokenizer = load_model_and_tokenizer(
        num_labels, id2label, label2id, args.model_name
    )
    model.to(device)

    train_df, val_df, test_df = load_splits(SPLITS_CSV, label2id)
    print(f"train / val / test rows : {len(train_df)} / {len(val_df)} / {len(test_df)}")
    print(f"intents                 : {num_labels}")
    print(f"batch size              : {batch_size} "
          f"(effective {batch_size * args.grad_accum} with accumulation)")

    train_loader = make_loader(train_df, tokenizer, batch_size, args.max_length, True)
    val_loader = make_loader(val_df, tokenizer, batch_size, args.max_length, False)

    # --- loss ------------------------------------------------------------
    if args.no_class_weights or not USE_CLASS_WEIGHTS:
        loss_fn = nn.CrossEntropyLoss()
        print("loss                    : cross-entropy (unweighted)")
    else:
        weights = compute_class_weights(train_df["label_id"].tolist(), num_labels,
                                        power=args.class_weight_power)
        loss_fn = nn.CrossEntropyLoss(weight=weights.to(device))
        spread = float(weights.max() / weights.min())
        print(f"loss                    : cross-entropy (class weights, "
              f"power={args.class_weight_power}, spread={spread:.0f}x)")

    # --- optimiser: no weight decay on biases / LayerNorm ----------------
    no_decay = ("bias", "LayerNorm.weight")
    grouped = [
        {"params": [p for n, p in model.named_parameters()
                    if not any(nd in n for nd in no_decay)],
         "weight_decay": args.weight_decay},
        {"params": [p for n, p in model.named_parameters()
                    if any(nd in n for nd in no_decay)],
         "weight_decay": 0.0},
    ]
    optimizer = torch.optim.AdamW(grouped, lr=args.lr)

    steps_per_epoch = max(1, len(train_loader) // args.grad_accum)
    total_steps = steps_per_epoch * args.epochs
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=int(total_steps * WARMUP_RATIO),
        num_training_steps=total_steps,
    )

    use_amp = USE_FP16 and not args.no_fp16 and device.type == "cuda"
    scaler = torch.amp.GradScaler("cuda") if use_amp else None
    print(f"mixed precision (fp16)  : {use_amp}")
    print("-" * 78)

    history = {"train_loss": [], "val_loss": [], "train_acc": [],
               "val_acc": [], "train_f1": [], "val_f1": []}
    best_score, best_f1, best_epoch, epochs_without_gain = -1e18, -1.0, -1, 0
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    for epoch in range(1, args.epochs + 1):
        started = time.time()
        tr_loss, tr_acc, tr_f1 = run_epoch(
            model, train_loader, device, loss_fn, optimizer, scheduler,
            scaler, args.grad_accum, train=True,
        )
        va_loss, va_acc, va_f1 = run_epoch(model, val_loader, device, loss_fn)

        history["train_loss"].append(tr_loss)
        history["val_loss"].append(va_loss)
        history["train_acc"].append(tr_acc)
        history["val_acc"].append(va_acc)
        history["train_f1"].append(tr_f1)
        history["val_f1"].append(va_f1)

        print(f"epoch {epoch}/{args.epochs} ({time.time() - started:5.1f}s)  "
              f"train loss {tr_loss:.4f} acc {tr_acc:.4f} | "
              f"val loss {va_loss:.4f} acc {va_acc:.4f} macroF1 {va_f1:.4f}")

        # Model selection deliberately avoids plain accuracy: with 76% of the
        # data in one class, accuracy would happily pick a model that ignores
        # the rare-but-critical intents such as `emergency`.
        #
        # macro_f1 is the headline metric, but on a 110-row validation set
        # where several classes have a single sample it is very noisy, so
        # `--early-stop-metric val_loss` is offered as a steadier alternative.
        if args.early_stop_metric == "macro_f1":
            score, label = va_f1, "macro-F1"
        else:
            score, label = -va_loss, "val loss"

        if score > best_score:
            best_score, best_f1, best_epoch = score, va_f1, epoch
            epochs_without_gain = 0
            model.save_pretrained(CHECKPOINT_DIR)
            tokenizer.save_pretrained(CHECKPOINT_DIR)
            shown = va_f1 if args.early_stop_metric == "macro_f1" else va_loss
            print(f"           new best {label} {shown:.4f} -> checkpoint saved")
        else:
            epochs_without_gain += 1
            if epochs_without_gain >= args.patience:
                print(f"           early stopping (no {label} gain for "
                      f"{epochs_without_gain} epochs)")
                break

    # --- restore the best checkpoint and save the final model ------------
    print("-" * 78)
    print(f"best epoch {best_epoch} with validation macro-F1 {best_f1:.4f}")

    best_model, _ = load_model_and_tokenizer(
        num_labels, id2label, label2id, str(CHECKPOINT_DIR)
    )
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    best_model.save_pretrained(MODEL_DIR)
    tokenizer.save_pretrained(MODEL_DIR)

    # Label maps live next to the weights so inference is fully self-contained.
    (MODEL_DIR / "label2id.json").write_text(
        json.dumps(label2id, indent=2, ensure_ascii=False), encoding="utf-8")
    (MODEL_DIR / "id2label.json").write_text(
        json.dumps({str(k): v for k, v in id2label.items()}, indent=2,
                   ensure_ascii=False), encoding="utf-8")

    history_path = OUTPUT_DIR / "training_history.json"
    history_path.write_text(json.dumps(
        {"history": history, "best_epoch": best_epoch, "best_val_macro_f1": best_f1,
         "batch_size": batch_size, "epochs_run": len(history["train_loss"]),
         "device": str(device), "fp16": use_amp},
        indent=2), encoding="utf-8")

    print(f"model saved   -> {MODEL_DIR}")
    print(f"history saved -> {history_path}")
    return history


def main() -> None:
    use_utf8_stdout()
    args = parse_args()

    if not SPLITS_CSV.exists():
        raise SystemExit(
            f"{SPLITS_CSV} not found - run `python src/preprocess.py` first."
        )

    # If the GPU runs out of memory we halve the batch size and retry rather
    # than crashing, which is the behaviour asked for by the spec.
    batch_size = args.batch_size
    while True:
        try:
            train_once(args, batch_size)
            return
        except torch.OutOfMemoryError:
            torch.cuda.empty_cache()
            if batch_size <= 1:
                raise SystemExit(
                    "Out of GPU memory even at batch size 1. Lower "
                    "--max-length (e.g. 64) or train with --cpu."
                )
            batch_size = max(1, batch_size // 2)
            args.grad_accum *= 2      # keep the effective batch size constant
            print(f"\n!! CUDA out of memory - retrying with batch size "
                  f"{batch_size} and grad-accum {args.grad_accum}\n")


if __name__ == "__main__":
    main()
