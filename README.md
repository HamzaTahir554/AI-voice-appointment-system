# AI Voice Appointment System

A multilingual conversational assistant that books, changes and cancels doctor
appointments for a clinic in Pakistan. It understands **English, Urdu, Roman
Urdu and mixed speech**, holds a real conversation to collect the doctor, date
and time, applies the clinic's rules, and stores everything in **Firebase
Firestore**. Doctors manage their day in a web dashboard, and a clinic
administrator manages the doctors, the clinic and every appointment.

> **Scope.** The system is designed for telephone calls. The telephone layer -
> **Asterisk/SIP, speech-to-text and text-to-speech - is not implemented** in
> this repository. The implemented pipeline takes the patient's words as
> **text** and returns the reply as **text**, through an HTTP API or an
> interactive console.

The full write-up is in
[`docs/AI_Voice_Appointment_System_Final_Report.pdf`](docs/AI_Voice_Appointment_System_Final_Report.pdf).

---

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Technologies Used](#technologies-used)
- [Requirements](#requirements)
- [Required Libraries](#required-libraries)
- [Installation Guide](#installation-guide)
- [Environment Variables](#environment-variables)
- [Using the system](#using-the-system)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [Development Tools](#development-tools)
- [Security](#security)
- [Limitations and future work](#limitations-and-future-work)

---

## Features

**Conversation**
- Intent detection with a **fine-tuned mBERT** model: 31 intents, test
  accuracy 0.898, macro-F1 0.836 on 354 held-out examples.
- A **Dialogue Manager** that tracks each session, asks only for missing
  details, confirms before acting, absorbs corrections ("Dr Ahmed nahi, Dr
  Asim"), answers side questions mid-booking, and asks when a doctor's name is
  ambiguous.
- Confidence bands (act at 0.80+, corroborate at 0.60-0.79, clarify below 0.60)
  and a phrase-based **emergency safety net**.
- Replies in **English, Roman Urdu or Urdu**, following the caller's language.
- A local LLM (**Ollama, `llama3.2`**) phrases replies; a **response
  validator** rejects any wording whose times, dates or IDs differ from the
  booking result and falls back to a template.

**Appointments**
- Booking, cancellation, rescheduling, completion and availability through one
  **Appointment Backend** with validation gates and transactional
  double-booking protection.
- **Doctor leave**: blocking a date cancels that day's appointments
  (`cancelled_by_doctor`), keeps their history, and queues one message per
  patient - with a preview of who will be affected first.
- **Appointment statistics** by status for today, this week, this month, all
  time or a custom range - for each doctor and for the whole clinic.

**Doctor Dashboard** (`/ui`)
- Today's appointments, who is due now, and what is coming up.
- Appointments, patients, weekly working hours, leave, profile, notifications.
- Own statistics, own password, alert preferences, light and dark themes.

**SuperAdmin Dashboard** - a **single-clinic** system: one clinic, many doctors
- Add, edit, deactivate, remove (archive) and restore doctors.
- Set each doctor's **username and password**, including when adding them.
- Manage any doctor's working hours and leave.
- View, search, cancel, move and complete **every** appointment.
- Edit the **one clinic's** name, address and phone - the change reaches every
  screen and what the assistant says.
- Clinic-wide and per-doctor statistics.

**Access control**
- Two roles enforced on the server; a doctor sees only their own data.
- Passwords stored as PBKDF2-HMAC-SHA256 hashes.

---

## How it works

```text
Patient text ──> /dialog/message  or  /voice/message  or  voice_pipeline.py
                        │
                        ▼
              mBERT intent detection          intent + confidence
                        │
                        ▼
                Dialogue Manager              state, slots, confirmation
                        │
                        ▼
              Appointment Backend  <────────  Doctor / SuperAdmin dashboards
                        │                         (/dashboard/*, /admin/*)
                        ▼
                Firebase Firestore
                        │
                        ▼
      Ollama judge + response validator      wording, never facts
                        │
                        ▼
                  Reply text out

Not implemented:  Asterisk/SIP ─> Speech-to-Text  ...  Text-to-Speech
```

The language model runs **last**: by the time it phrases the reply, the
appointment has already been booked or refused by deterministic code.

---

## Technologies Used

| Category | Technology |
|---|---|
| Programming languages | Python 3.12; JavaScript, HTML, CSS |
| NLP model | mBERT (`bert-base-multilingual-cased`) fine-tuned for 31 intents |
| Deep learning | PyTorch, Hugging Face Transformers |
| Data and metrics | pandas, NumPy, scikit-learn, Matplotlib |
| LLM | Ollama running `llama3.2` (local, called over HTTP with httpx) |
| STT | Not implemented |
| TTS | Not implemented |
| Backend | FastAPI, Uvicorn, Pydantic, python-dotenv |
| Database | Google Cloud Firestore through the Firebase Admin SDK |
| Telephony | Not implemented (Asterisk/SIP planned) |
| Frontend | HTML, CSS and JavaScript - no framework and no build step |
| Testing | Python `unittest`; headless Chrome for browser tests; pypdf |
| Version control | Git, GitHub |

---

## Requirements

### Hardware

| Purpose | Requirement |
|---|---|
| Training the intent model | An NVIDIA GPU is strongly recommended. Tested on a **GTX 1660 SUPER, 6 GB VRAM**: about 233 s per epoch, 20 epochs. The batch size (16, effective 32) was chosen to fit 6 GB. |
| Running the system | A GPU is optional. Intent detection took a median of 10 ms per request on the GPU above; CPU-only inference works but was not measured. |
| Disk | About 700 MB for the trained model, 2.0 GB for the `llama3.2` Ollama model, plus the Python packages (PyTorch with CUDA is several GB). |

### Software

| Software | Version | Needed for |
|---|---|---|
| Python | 3.12 (tested 3.12.10) | Everything |
| Git | any recent | Cloning |
| Ollama | tested 0.34.4, with `llama3.2` pulled | Natural reply wording (optional: `OLLAMA_ENABLED=false` uses templates) |
| Firebase project with Firestore | - | Persistent data (optional for trying the system: an in-memory database is used when no credentials are set) |
| Google Chrome or Microsoft Edge | any recent | Browser tests, and rebuilding the PDF report |
| Node.js | 22 or newer (tested 24.16) | Browser tests only |
| NVIDIA driver with CUDA 12.6 support | - | GPU training/inference only |

Asterisk is **not** required: the telephony layer is not implemented.

---

## Required Libraries

All Python dependencies are in [`requirements.txt`](requirements.txt). It was
audited against every import in the project.

| Package | Minimum | Tested | Used for |
|---|---|---|---|
| torch | 2.4 | 2.14.0 (CUDA 12.6) | Running and training mBERT |
| transformers | 4.44 | 5.15.0 | mBERT model and tokenizer |
| pandas | 2.2 | 3.0.5 | Dataset preparation |
| numpy | 1.26 | 2.5.1 | Numerics |
| scikit-learn | 1.5 | 1.9.0 | Metrics, splits |
| matplotlib | 3.8 | 3.11.1 | Training plots |
| firebase-admin | 6.5 | 7.5.0 | Firestore access |
| google-cloud-firestore | 2.16 | 2.30.0 | Firestore client |
| httpx | 0.27 | 0.28.1 | Ollama client; FastAPI test client |
| fastapi | 0.115 | 0.141.1 | HTTP API |
| uvicorn[standard] | 0.30 | 0.52.3 | ASGI server |
| pydantic | 2.8 | 2.13.4 | Request validation |
| python-dotenv | 1.0 | 1.2.2 | Loading `.env` |
| pypdf | 4.0 | 6.16.0 | Checking the PDF report for secrets |

`intent_detection/requirements.txt` is the subset needed to work on the intent
model alone. The dashboard has **no** JavaScript dependencies, and the browser
tests use only Node's built-in modules.

---

## Installation Guide

These steps are written for Windows PowerShell; the same commands work in a
Unix shell with `source .venv/bin/activate`.

### 1. Clone the repository

```powershell
git clone https://github.com/HamzaTahir554/AI-voice-appointment-system.git
cd AI-voice-appointment-system
```

### 2. Create a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install the Python dependencies

For an NVIDIA GPU, install PyTorch from the CUDA index **first** (the default
PyPI wheel is CPU-only):

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
```

Check the GPU is visible (optional):

```powershell
python -c "import torch; print(torch.cuda.is_available())"
```

### 4. Get the trained intent model

The model weights (679 MB) are larger than GitHub allows in a repository, so
they are **not** in Git. Either:

- **Download** `mbert_intent_classifier.zip` from this repository's
  [Releases](https://github.com/HamzaTahir554/AI-voice-appointment-system/releases)
  page and unzip it so that this file exists:
  `intent_detection/models/mbert_intent_classifier/model.safetensors`
- **or train it yourself** (GPU recommended, roughly 80 minutes on a GTX 1660
  SUPER):

  ```powershell
  cd intent_detection
  python src/preprocess.py     # builds data/intents.csv and data/splits.csv from ../Data
  python src/train.py          # writes models/mbert_intent_classifier/
  python src/evaluate.py       # writes outputs/ (metrics, confusion matrix)
  cd ..
  ```

### 5. Configure environment variables

```powershell
copy .env.example .env
```

Then edit `.env`. At minimum, set the two starting passwords:

```text
DASHBOARD_PASSWORD=choose-a-doctor-password
SUPERADMIN_PASSWORD=choose-an-admin-password
```

See [Environment Variables](#environment-variables) for everything else.

### 6. Configure Firebase (optional)

Without Firebase credentials the system uses an **in-memory database seeded
with demo doctors**, which is enough to try everything. For persistent data:

1. In the Firebase console, create a project and enable **Firestore**.
2. Go to *Project settings -> Service accounts -> Generate new private key*.
3. Keep the downloaded JSON file **outside** this folder, and point `.env` at
   it:
   ```text
   FIREBASE_CREDENTIALS_FILE=C:/secure/serviceAccountKey.json
   ```
   (or paste its three values into `FIREBASE_PROJECT_ID`,
   `FIREBASE_CLIENT_EMAIL` and `FIREBASE_PRIVATE_KEY`).
4. Check the connection:
   ```powershell
   python scripts/verify_firebase.py
   ```

### 7. Configure Ollama (optional)

Install Ollama from <https://ollama.com>, then:

```powershell
ollama pull llama3.2
```

Ollama listens on `http://localhost:11434` by default (`OLLAMA_BASE_URL`). To
run without it, set `OLLAMA_ENABLED=false`; replies then come from templates in
all three languages.

### 8. Start the backend

```powershell
uvicorn api.main:app --port 8000
```

- API documentation: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>

### 9. Open the dashboard

The API serves it; there is nothing to build or install:

- <http://127.0.0.1:8000/ui/>

Sign in as a **doctor** with a doctor ID (the demo data has `D001` to `D004`)
and `DASHBOARD_PASSWORD`, or as the **administrator** with `ADMIN` and
`SUPERADMIN_PASSWORD`. Those two values are starting passwords: once a password
is changed in the dashboard it is stored hashed in Firestore and the `.env`
value stops working for that account.

### 10. Run the tests

```powershell
python scripts/run_tests.py
```

---

## Environment Variables

Names only - never commit real values. The full template, with comments, is
[`.env.example`](.env.example).

```text
# Firebase (option A: values, option B: key file)
FIREBASE_PROJECT_ID=
FIREBASE_CLIENT_EMAIL=
FIREBASE_PRIVATE_KEY=
FIREBASE_CREDENTIALS_FILE=
USE_LOCAL_DB=

# Intent confidence and dialogue
CONFIDENCE_THRESHOLD=
HIGH_CONFIDENCE=
INTENT_SWITCH_THRESHOLD=
EMERGENCY_SAFETY_NET=
SESSION_TIMEOUT_MINUTES=
RESPONSE_LANGUAGE=

# Ollama
OLLAMA_BASE_URL=
OLLAMA_MODEL=
OLLAMA_TIMEOUT=
OLLAMA_TEMPERATURE=
OLLAMA_MAX_TOKENS=
OLLAMA_ENABLED=
OLLAMA_TRANSLATE=

# Dashboards
DASHBOARD_PASSWORD=
DASHBOARD_PASSWORD_<DOCTORID>=
DASHBOARD_SESSION_HOURS=
DASHBOARD_ORIGINS=
SUPERADMIN_ID=
SUPERADMIN_PASSWORD=

# Clinic and general
CLINIC_ID=
NOTIFICATION_PROVIDER=
APPOINTMENT_ID_PREFIX=
TIMEZONE=
LOG_LEVEL=
INTENT_MODULE_DIR=
```

---

## Using the system

### Talk to the assistant in the console

```powershell
python voice_pipeline.py --interactive          # type your own turns
python voice_pipeline.py -i --no-llm            # templates only, no Ollama
python voice_pipeline.py                        # a scripted booking
```

`--fresh` **deletes every appointment** before starting; do not use it against
real data.

### Talk to it over HTTP

```powershell
curl -X POST http://127.0.0.1:8000/dialog/message `
     -H "Content-Type: application/json" `
     -d '{\"session_id\": \"demo1\", \"text\": \"Mujhe Dr Ahmed se kal 4 baje appointment chahiye\", \"patient_id\": \"P001\"}'
```

The response carries the reply text, the intent and its confidence, the next
action and the slot state. `POST /voice/message` runs the same pipeline and is
the endpoint a telephony layer would call.

### Main API groups

| Prefix | Who | What |
|---|---|---|
| `/dialog/*`, `/voice/message`, `/intent/predict` | pipeline | Conversation turns, classifier |
| `/appointments/*`, `/doctors/*`, `/patients/*` | pipeline | Appointment Backend |
| `/auth/*` | both roles | Sign in, sign out, own password |
| `/dashboard/*` | doctor | Own appointments, patients, schedule, leave, profile, notifications, statistics |
| `/admin/*` | administrator | Doctors, credentials, the clinic, every appointment, statistics |

All 72 routes are listed in Appendix A of the report, and live at `/docs`.

---

## Project Structure

```text
AI-voice-appointment-system/
├── api/                    FastAPI app, authentication, doctor and admin APIs
│   ├── main.py             application, dialogue and voice endpoints, /ui
│   ├── auth.py             sessions and roles
│   ├── dashboard.py        /auth/*, /dashboard/*  (doctor, own data only)
│   └── admin.py            /admin/*               (administrator)
├── appointment_backend/    the only code that creates or changes appointments
│   ├── appointment_service.py   facade
│   ├── booking_service.py, cancellation_service.py, reschedule_service.py
│   ├── availability_service.py, validation.py, result.py
│   ├── statistics.py       counts by status and period
│   └── api.py              public appointment routes
├── dialog_manager/         conversation state, slots, entities, fallback
├── ollama_judge/           LLM client, prompts, languages, templates, validator
├── firebase/               Firestore repository and one service per collection
├── intent_detection/       mBERT intent model
│   ├── src/                preprocess, train, evaluate, inference, data builders
│   ├── data/               label maps (processed CSVs are regenerated)
│   ├── outputs/            test metrics, classification report, training plots
│   └── models/             trained weights (not in Git - see step 4)
├── Dashboard/              doctor and administrator web app (HTML/CSS/JS)
├── Data/                   raw intent datasets
├── docs/
│   ├── AI_Voice_Appointment_System_Final_Report.pdf
│   └── report/final_report.html   source of the report
├── reports/                evaluation outputs (intent audit, latency, live runs)
├── scripts/                run_tests, build_report, demo, evaluate_intents,
│                           measure_performance, verify_firebase
├── tests/                  unittest suite
│   └── browser/            headless-Chrome dashboard checks
├── config.py               every setting, from environment variables
├── voice_pipeline.py       interactive console pipeline
├── requirements.txt
├── .env.example
└── .gitignore
```

Component documentation:
[`intent_detection/README.md`](intent_detection/README.md) and
[`Dashboard/README.md`](Dashboard/README.md).

---

## Testing

```powershell
python scripts/run_tests.py                       # everything offline
python scripts/run_tests.py --category dashboard  # one component
python scripts/run_tests.py --live                # + real Firestore and Ollama
```

Offline tests run against the in-memory database and never touch Firestore.
Results on the submitted code:

| Category | Tests | Passed | Skipped |
|---|---:|---:|---:|
| Intent detection | 45 | 45 | 0 |
| Dialogue | 112 | 112 | 0 |
| Appointment backend | 61 | 60 | 1 |
| Firebase | 40 | 31 | 9 |
| Ollama / wording | 54 | 51 | 3 |
| End-to-end | 25 | 25 | 0 |
| Dashboards | 122 | 121 | 1 |
| Security | 11 | 11 | 0 |
| **Total** | **470** | **456** | **14** |

No test fails. The skipped tests are the live Firestore and Ollama checks,
which need `--live`.

Without the trained model (a fresh clone before step 4) the suite still
passes: the four test classes that exercise the real model skip themselves,
giving 462 tests, 444 passed, 18 skipped, 0 failed. The dialogue tests use a
scripted stand-in classifier on purpose (`tests/helpers.py`), so they test
the dialogue logic on its own; the running system always uses mBERT.

**Browser checks** drive the real dashboard in headless Chrome - 59 and 51
checks, all passing. See [`tests/browser/README.md`](tests/browser/README.md).

**Rebuilding the report** after editing its source:

```powershell
python scripts/build_report.py
```

---

## Development Tools

- Visual Studio Code
- Git, GitHub and the GitHub CLI
- PowerShell
- Python 3.12
- Ollama
- Firebase console
- Google Chrome (browser tests, PDF rendering) and Node.js (browser tests)
- NVIDIA CUDA (model training)

---

## Security

- Secrets live only in `.env` or the environment; `.env` and Firebase
  service-account files are ignored by Git, and so is every JSON file that is
  not explicitly listed.
- The browser holds no Firebase credential or server address, and never talks to
  Firebase.
- Dashboard passwords are hashed (PBKDF2-HMAC-SHA256, 120,000 iterations, random
  salt) and never returned by the API.
- Roles are enforced on the server: a doctor's requests are scoped to their own
  ID from the session, and each role is refused by the other's routes.
- `tests/test_security.py` scans the code, the reports and the PDF for keys, and
  checks that the secret files are ignored.

**Known limits:** sessions live in the API process (a restart signs everyone
out), there is no sign-in rate limiting, and the voice-pipeline routes are
unauthenticated because they are meant to sit behind a telephony layer on a
private network.

---

## Limitations and future work

| Area | Today | Next step |
|---|---|---|
| Telephony and voice | Not implemented; text in, text out | Asterisk bridge, Urdu-capable STT, TTS around `/voice/message` |
| Patient notifications | Queued in Firestore and shown; not sent | Implement the `NotificationSender` interface for an SMS or voice provider |
| Intent model | Macro-F1 0.836; emergency recall 0.55 (backed by the safety net) | More natural examples per intent and a fresh held-out set |
| LLM wording | `llama3.2` wording accepted for a minority of replies; templates cover the rest | A larger local model |
| Authentication | Prototype sessions | Firebase Authentication and rate limiting |
| Speed | Several seconds per Firestore-backed turn | Fewer round trips; cache doctor and clinic records |
