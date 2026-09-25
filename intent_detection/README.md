# Intent Detection with mBERT — AI Voice Appointment System

Fine-tunes **`bert-base-multilingual-cased` (mBERT)** to classify what a patient
wants, from the text of what they said.

```
Patient text → [ mBERT Intent Detection ] → Dialog Manager → Appointment Backend
```

> **Where the text comes from.** The system is designed for speech: an STT
> stage would transcribe the caller and hand the words to this module. That
> STT stage is **not implemented in this codebase** - today the text arrives
> from `voice_pipeline.py` (typed turns) or the `/dialog/message` API. Mentions
> of STT below describe the intended input, not a component that exists.

**Input:** `"Mujhe kal Dr Ahmed ke saath appointment chahiye."`
**Output:** `{"intent": "book_appointment", "confidence": 0.94}`

---

## 1. Quick start (Windows)

```powershell
# 1. install dependencies  (see the GPU note in section 2 first!)
pip install -r requirements.txt

# 2. inspect + clean the data, build the splits and label maps
python src/preprocess.py

# 3. fine-tune mBERT
python src/train.py

# 4. evaluate on the untouched test set, write plots + reports
python src/evaluate.py

# 5. try it out
python src/inference.py
python src/inference.py --text "Meri appointment cancel kar dein"

# 6. serve it
python src/api.py            # http://127.0.0.1:8000/docs
```

Scripts are run from the project root (`intent_detection/`) and resolve every
path themselves, so no `PYTHONPATH` juggling is needed.

---

## 2. GPU setup — read this before training

Your machine has an **NVIDIA GeForce GTX 1660 SUPER (6 GB)**, but the currently
installed PyTorch is a **CPU-only build**, so training falls back to the CPU:

```
torch 2.13.0+cpu     torch.cuda.is_available() → False
```

Training still works on CPU (roughly 5 minutes per epoch on this dataset), but
to actually use the GPU, reinstall PyTorch from the CUDA index:

```powershell
pip uninstall -y torch
pip install torch --index-url https://download.pytorch.org/whl/cu126
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

That must print `True NVIDIA GeForce GTX 1660 SUPER`. Once it does, `train.py`
picks the GPU up automatically and enables FP16 mixed precision.

> Do **not** reinstall the old `torchvision 0.28.0+cu126` / `torchaudio
> 2.11.0+cu126` wheels — they were ABI-mismatched with this torch and broke
> `transformers` imports. Install versions matching whatever torch you end up
> with, and only when the STT stage needs them.

### Fitting in limited VRAM

`train.py` already includes every memory optimisation the spec asks for:

| Technique | Flag / setting | Effect |
|---|---|---|
| Configurable batch size | `--batch-size 8` | fewer samples in VRAM at once |
| Gradient accumulation | `--grad-accum 4` | keeps the *effective* batch large |
| Mixed precision (FP16) | automatic on CUDA, `--no-fp16` to disable | ~40 % less memory |
| Gradient clipping | `MAX_GRAD_NORM = 1.0` | stops exploding gradients |
| Configurable sequence length | `--max-length 64` | memory scales with length |
| OOM auto-recovery | built in | halves the batch and retries instead of crashing |

A safe 6 GB recipe (this is the exact command that produced the results below):

```powershell
python src/train.py --epochs 20 --max-length 64 --batch-size 16 --patience 5 --class-weight-power 0.5
```

If you ever do hit an out-of-memory error, the script halves the batch size and
retries automatically; you can also force CPU with `--cpu`.

---

## 3. What the dataset actually contains

`src/preprocess.py` inspects the raw CSVs in `../Data/` and prints a full
report. The three files have **three different schemas and three different
label vocabularies**, all of which are detected automatically — no column name
is hard-coded.

| File | Rows | Detected text column | Detected intent column | Parallel Urdu |
|---|---:|---|---|---|
| `dataset.csv` | 405 | `User_Query` | `Intent` (9 labels) | `Urdu_Query` ✅ |
| `dataset2.csv` | 2159 | `Patient_Message` | `Feature` (12 labels) | — (its `Urdu_Query` holds row **IDs**, not Urdu) |
| `dataset3.csv` | 1500 | `user_input` | `intent` (13 labels) | `Urdu_Query` ✅ |

The genuine Urdu-script columns are harvested as **extra training samples** for
the same intent, which is where the model's Urdu ability comes from.

### After unification and cleaning

* 5 969 rows harvested → **1 091 unique samples** after exact-duplicate removal
  (the raw files repeat the same sentence many times over)
* **14 canonical intents**
* Scripts: 42.7 % Urdu, 38.4 % Roman Urdu, 18.8 % English

### The 14 intents and their class distribution

| Intent | Samples | Share |
|---|---:|---:|
| `book_appointment` | 835 | 76.5 % |
| `reschedule_appointment` | 113 | 10.4 % |
| `emergency` | 24 | 2.2 % |
| `doctor_fee` | 20 | 1.8 % |
| `cancel_appointment` | 20 | 1.8 % |
| `clinic_address` | 14 | 1.3 % |
| `clinic_timing` | 13 | 1.2 % |
| `doctor_info` | 11 | 1.0 % |
| `doctor_experience` | 9 | 0.8 % |
| `doctor_qualification` | 9 | 0.8 % |
| `check_availability` | 9 | 0.8 % |
| `confirm_appointment` | 6 | 0.5 % |
| `greeting` | 4 | 0.4 % |
| `goodbye` | 4 | 0.4 % |

### ⚠️ Known limitations of this data (be honest about these in your viva)

1. **Severe class imbalance.** `book_appointment` is 76.5 % of the data. A model
   that always answers `book_appointment` would already score 76 % accuracy —
   which is why this project reports **macro-F1**, not accuracy, and trains with
   **square-root inverse-frequency class weights** (`--class-weight-power 0.5`;
   full inverse weighting over-corrects badly on this data - see section 3b).
2. **Very few distinct utterances per rare intent.** `greeting` and `goodbye`
   have only 2 distinct source utterances each, so they cannot appear in
   validation *and* test; `preprocess.py` sends them entirely to train and
   reports them as **not evaluable**. Their test scores are absent, not zero.
3. **Low lexical diversity.** After dedupe, `doctor_experience` comes from a
   single sentence pattern. The model may be matching surface wording rather
   than meaning for these classes, so test scores on them are optimistic.

The right fix is more data: collect or write 30–50 genuinely different phrasings
per intent, in all three scripts. The pipeline picks new rows up automatically —
just drop another CSV into `Data/` and re-run `preprocess.py`.

---

## 3b. Results actually obtained

Final model: **31 intents**, trained on GPU (GTX 1660 SUPER, FP16), 16 epochs
with early stopping, best epoch 11. Reproduce with:

```powershell
python src/generate_data.py     # writes Data/synthetic_intents.csv
python src/preprocess.py        # unify + split
python src/train.py --epochs 20 --max-length 64 --batch-size 16 --patience 5 --class-weight-power 0.5
python src/evaluate.py
```

### Test-set headline (272 unseen rows, 31 intents)

| Metric | Value |
|---|---:|
| Accuracy | **0.9191** |
| Weighted F1 | **0.9188** |
| **Macro F1** | **0.8745** |
| Macro precision / recall | 0.8996 / 0.8694 |

### How it got here

| Stage | Intents | Rows | Macro F1 | Notes |
|---|---:|---:|---:|---|
| 1. Raw data, first run | 14 | 1091 | 0.2112 | unusable: 0 % of calls passed the 0.60 threshold |
| 2. Tuned training | 14 | 1091 | 0.5796 | fixed early stopping, class weights, max_length |
| 3. + synthetic corpus | 31 | 2415 | 0.7592 | 18 empty intents filled |
| 4. + conversational fix | 31 | **2682** | **0.8745** | `goodbye`/`help` rescued from F1 0.00 |

Stages 3 and 4 changed **no hyper-parameters at all** - the entire gain came
from fixing the data. That is the single most important lesson of this module.

### Confidence threshold behaviour

| Threshold | Calls kept | Accuracy on kept |
|---:|---:|---:|
| 0.50 | 96.0 % | 0.9425 |
| **0.60** | **94.1 %** | **0.9531** |
| 0.70 | 91.9 % | 0.9600 |
| **0.80** | 86.4 % | **0.9702** |
| 0.90 | 75.4 % | 0.9902 |

Mean confidence is 0.9252 when correct and 0.6430 when wrong. **0.80 is the
recommended production setting**: it answers 86 % of calls at 97 % accuracy and
routes the rest to a clarifying question.

### Per-intent F1 (test set) - no intent scores 0.00 any more

**Perfect (F1 1.00):** `change_date`, `clinic_closed`, `check_availability`,
`provide_patient_age`, `greeting`, `doctor_unavailable`,
`doctor_specialization`, `thank_you`, `provide_patient_phone`,
`reschedule_appointment`

**Strong (0.87-0.98):** `book_appointment` 0.9735, `cancel_appointment` 0.9412,
`provide_patient_name` 0.9231, `confirm` 0.9091, `goodbye` 0.9091,
`repeat_information` 0.9091, `change_doctor` 0.8889, `clinic_timing` 0.8889,
`doctor_fee` 0.8750, `appointment_confirmation` 0.8571

**Weakest:** `unclear_request` 0.4000, `help` 0.6667, `appointment_status`
0.6667, `deny` 0.7500, `change_time` 0.7500, `emergency` 0.7692

`unclear_request` is inherently fuzzy - it is defined as "the caller said
something vague", which overlaps with `deny` and `help` by construction. In
production the confidence threshold covers much of this ground anyway.

### Generalisation check on unseen phrasings

Hand-written sentences that appear nowhere in the corpus: **18/20 correct**,
all three scripts, confidence 0.88-0.96. The two failures are informative
rather than alarming:

* `"Thank you, goodbye"` -> `thank_you` (0.90). Genuinely ambiguous; the
  sentence contains both intents.
* `"Saans nahi aa rahi"` ("cannot breathe") -> `repeat_information` (0.63).
  A real miss on a safety-critical intent - see the caveat below.

### Before / after on the original complaint

The model previously could not recognise a fee question phrased any other way.
It now handles paraphrases that never use the word "fee", in every script:

| Input | Before | After |
|---|---|---|
| "How much do I have to pay" | wrong | `doctor_fee` **0.93** |
| "Kitne paise dene hain" | wrong | `doctor_fee` **0.92** |
| "کتنے پیسے دینے ہیں" | wrong | `doctor_fee` **0.93** |
| "My father collapsed, we need help now" | `book_appointment` 0.94 | `emergency` **0.88** |
| "میرے ابو گر گئے ہیں، ابھی مدد چاہیے" | `book_appointment` 0.99 | `emergency` **0.93** |

### ⚠️ Honest caveats - state these in the viva

1. **Most of this corpus is synthetic.** 1596 of 2682 rows were written by
   template in `src/generate_data.py`. It teaches phrasing variety; it is not
   evidence of real-world accuracy on live patient calls. The defensible claim
   is *"the taxonomy is fully covered and the model generalises across
   paraphrases and three scripts"* - **not** *"92 % accurate on real patients"*.
2. **`emergency` is still the weak link** (F1 0.77, and it missed "saans nahi
   aa rahi"). For a medical system this intent deserves a lower threshold, a
   keyword safety-net, or both - never rely on the classifier alone here.
3. **Test data shares generators with training data.** Splits are group-aware
   and leakage-free, but synthetic test rows come from the same template pool,
   so scores remain somewhat optimistic. Real recorded calls are the only true
   test set.

---

## 4. How leakage is prevented

This matters because the raw data is so repetitive that a naive random split
would put the same sentence in train *and* test, producing a fake-high score.

* Splitting happens at **group** level, not row level.
* One group = one source row, so an utterance and its **Urdu translation always
  land in the same split**.
* Groups are additionally **merged when two rows share the same text**
  (case-insensitive), via a union-find structure.
* Within each intent, groups are shuffled with `SEED = 42` and dealt out
  80 / 10 / 10 → the split is **stratified and reproducible**.
* `preprocess.py` then **asserts** that no group spans two splits and that
  train/test text overlap is exactly `0`.

Resulting split: **866 train / 110 val / 115 test** (79.4 % / 10.1 % / 10.5 %).

---

## 5. Project structure

```
intent_detection/
├── data/
│   ├── intents.csv              # unified, cleaned, deduplicated
│   ├── splits.csv               # same rows + train/val/test column
│   ├── label2id.json
│   └── id2label.json
├── models/
│   ├── mbert_intent_classifier/ # final model (reloadable, self-contained)
│   └── checkpoints/             # best-epoch checkpoint during training
├── outputs/
│   ├── classification_report.txt
│   ├── per_intent_metrics.csv
│   ├── test_metrics.json
│   ├── training_history.json
│   ├── confusion_matrix.png
│   ├── confusion_matrix_normalized.png
│   ├── training_loss.png
│   ├── validation_loss.png
│   ├── training_accuracy.png
│   └── validation_accuracy.png
├── src/
│   ├── config.py       # paths, hyper-parameters, seed
│   ├── preprocess.py   # inspection, cleaning, label mapping, splitting
│   ├── dataset.py      # torch Dataset, tokenisation, class weights, device
│   ├── train.py        # fine-tuning loop (AMP, accumulation, early stopping)
│   ├── evaluate.py     # test-set metrics, confusion matrix, curves
│   ├── inference.py    # predict_intent() + demo table
│   └── api.py          # FastAPI service
├── requirements.txt
└── README.md
```

---

## 6. Using the model from the Dialog Manager

```python
from inference import IntentPredictor

predictor = IntentPredictor()          # load ONCE at start-up
result = predictor.predict("Mujhe kal Dr Ahmed ke saath appointment chahiye.")

# {
#   "intent": "book_appointment",
#   "confidence": 0.94,
#   "raw_intent": "book_appointment",
#   "below_threshold": False,
#   "threshold": 0.6,
#   "top_k": [ ... ]
# }
```

Or the simple helper the spec asks for:

```python
from inference import predict_intent
intent, confidence = predict_intent("I want to book an appointment with Dr Ahmed")
```

### REST API

```bash
curl -X POST http://127.0.0.1:8000/predict-intent \
     -H "Content-Type: application/json" \
     -d "{\"text\": \"Mujhe doctor se appointment leni hai\"}"
```

```json
{ "intent": "book_appointment", "confidence": 0.94, "below_threshold": false, "top_k": [...] }
```

The model is loaded once in the FastAPI `lifespan` handler, never per request.

---

## 7. The confidence threshold

```python
CONFIDENCE_THRESHOLD = 0.60      # config.py
if confidence < CONFIDENCE_THRESHOLD:
    return "unknown_intent", confidence
```

**Why this matters in a voice system:** STT output is noisy, and callers say
things the model was never trained on. Without a threshold the classifier
*always* returns its best guess — so "I want to change my medicine" could be
classified as `cancel_appointment` with 35 % confidence and silently **cancel a
real appointment**. Returning `unknown_intent` lets the Dialog Manager fall back
to a clarifying question ("Sorry, did you want to book, cancel or reschedule?")
or hand off to a human. A slightly annoying re-prompt is far cheaper than a
wrongly cancelled appointment.

`evaluate.py` prints how much traffic each threshold keeps and the accuracy on
what it keeps, so you can tune the number with evidence rather than by guessing.

---

## 8. Concepts explained (for the FYP viva)

**1. What is mBERT?** Multilingual BERT: a transformer encoder pre-trained on
Wikipedia in 104 languages, including Urdu. It has 12 layers, 768 hidden units
and ~178 M parameters, and produces contextual vector representations of text.

**2. Why is it suitable here?** Patients speak English, Urdu and Roman Urdu,
often mixed within a single sentence. mBERT shares one vocabulary and one
embedding space across all of them, so a single model handles every case — and
knowledge learned from English examples transfers to Urdu ones.

**3. Why `bert-base-multilingual-cased`?** The *cased* variant preserves
capitalisation ("Dr Ahmed"), which carries real signal, and it is the variant
Google recommends over the uncased one, whose normalisation damages non-Latin
scripts. Verified on this data: it tokenises English, Roman Urdu and Urdu with
**zero `[UNK]` tokens**.

**4. What happens during tokenization?** Text is split into WordPiece subword
units, mapped to integer IDs, wrapped in `[CLS] … [SEP]`, padded/truncated to
128 tokens, and paired with an attention mask marking real tokens vs padding.
`"Mujhe"` becomes `['Mu', '##j', '##he']` — subwords let the model handle Roman
Urdu it never saw as whole words.

**5. What is the `[CLS]` token?** A special token prepended to every input. Its
final-layer vector is trained to summarise the **whole sentence**, so it is the
natural place to attach a sentence-level classifier.

**6. How does mBERT understand the sentence?** Through **self-attention**: at
every layer each token looks at every other token and updates its representation
accordingly. So in "cancel my appointment with Dr Ahmed", the word *cancel*
influences the `[CLS]` vector strongly, and the model learns word order and
context rather than isolated keywords.

**7. What is fine-tuning?** Starting from pre-trained weights and continuing
training on *our* labelled data, updating **all** layers with a small learning
rate (2e-5). We do **not** freeze mBERT — the encoder itself adapts to clinic
language.

**8. What is the classification head?** A single linear layer (768 → 14) on top
of the `[CLS]` vector, added by `AutoModelForSequenceClassification`. It is
randomly initialised and learns from scratch, producing one raw score (logit)
per intent.

**9. How are intents converted to numbers?** `label2id` maps each intent name to
an integer (`book_appointment → 0`, …); `id2label` maps back. Both are saved as
JSON next to the weights so inference never guesses the label order.

**10. What is cross-entropy loss?** It measures the gap between the predicted
probability distribution and the true one-hot label; it is large when the model
is confidently wrong. We use **class-weighted** cross-entropy so mistakes on
rare intents (like `emergency`) cost proportionally more.

**11. How does backpropagation update mBERT?** The loss is differentiated with
respect to every weight (chain rule), producing gradients that say how each
weight should change; **AdamW** then applies those changes. Gradients flow from
the classification head all the way back through all 12 transformer layers.

**12. What are epochs?** One epoch is one full pass over the training set. We
run up to 5, with early stopping if validation macro-F1 stops improving.

**13. Batch size and learning rate?** Batch size = samples processed before one
weight update (16 here, ×2 gradient accumulation = effective 32). Learning rate
(2e-5) = step size; too high destroys pre-trained knowledge, too low never
converges.

**14. Why validation data?** To pick the best epoch and trigger early stopping
*without* touching the test set. Selecting on test data would make the final
score meaningless.

**15. Why a separate test set?** Because validation influenced our choices, it
is no longer unbiased. The test split is touched exactly once, at the end, to
estimate real-world performance.

**16. Precision, recall, F1?** *Precision* = of everything predicted as this
intent, how much was right. *Recall* = of all true examples of this intent, how
many we caught. *F1* = their harmonic mean. **Macro-F1** averages classes
equally (so rare intents count); **weighted-F1** weights by support.

**17. What is a confusion matrix?** A grid of actual vs predicted intents.
The diagonal is correct predictions; off-diagonal cells show exactly which
intents get mistaken for which — e.g. `reschedule_appointment` confused with
`book_appointment`, since both mention a doctor, a day and a time.

**18. Why a confidence threshold?** See section 7 — it converts a silent wrong
action into an explicit "I'm not sure", which the Dialog Manager can recover from.

**19. How does the output connect to the Dialog Manager?** The predictor returns
a JSON-serialisable dict (`intent`, `confidence`, `top_k`, `below_threshold`).
The Dialog Manager switches on `intent`, uses `confidence` to decide whether to
confirm or re-prompt, and combines it with the Entity Extraction output (doctor
name, date, time) to call the Appointment Backend.

**20. Where does this fit in the whole system?** It is the decision point
between raw transcribed text and any action: STT produces words, this module
produces *meaning*, and everything downstream (dialog policy, database writes,
TTS response) branches on that meaning.

---

## 9. Presentation summary

**Problem.** Patients phone a clinic and speak naturally, in English, Urdu or
Roman Urdu. Before the system can do anything it must work out *what the caller
wants*.

**Solution.** Fine-tune multilingual BERT as a 14-class intent classifier over
the clinic's own transcripts.

**Input.** Text produced by the STT stage.

**Processing.** `Text → mBERT tokenizer → 12 transformer layers → [CLS] vector →
linear head → softmax → intent + confidence`.

**Output.** `{"intent": "book_appointment", "confidence": 0.94}`, gated by a
0.60 confidence threshold.

**Example.**

```
"Mujhe kal doctor se appointment leni hai."
        ↓ STT
   text transcript
        ↓ mBERT intent detection
   book_appointment (0.94)
        ↓ Dialog Manager
   check doctor availability → confirm → write to Appointment DB → TTS reply
```

### Why not simple keyword matching?

| | Keyword matching | Fine-tuned mBERT |
|---|---|---|
| "**Don't** book me an appointment" | sees *book* → books it ❌ | reads negation in context ✅ |
| "Mujhe appointment **cancel** karni hai" | needs a hand-written Roman-Urdu rule for every spelling | learned from data ✅ |
| Urdu script | needs a whole second keyword list | same model, same weights ✅ |
| Spelling variants (*appointmnt*, *apointment*) | miss ❌ | WordPiece subwords still match ✅ |
| "Book" in *"the doctor wrote a book"* | false positive ❌ | context disambiguates ✅ |
| New phrasings | someone must add a rule | generalises from examples ✅ |
| Confidence score | none — it either matches or not | calibrated probability → safe fallback ✅ |

Keyword rules also grow unmaintainably: 14 intents × 3 languages × many
spellings is hundreds of hand-written patterns that conflict with each other.
The transformer learns these distinctions from the data instead.
