# AI Voice Appointment System

A multilingual conversational assistant that books, changes and cancels doctor
appointments for a clinic in Pakistan. It understands **English, Urdu, Roman
Urdu and mixed speech**, holds a real conversation to collect the doctor, date
and time, applies the clinic's rules, and stores everything in **Firebase
Firestore**. Doctors manage their day in a web dashboard, and a clinic
administrator manages the doctors, the clinic and every appointment.

> **Scope.** The system is designed for telephone calls. **Speech-to-text and
> text-to-speech (ElevenLabs) are implemented and measured against the real
> ElevenLabs API with synthetic callers** - not yet with real patients or a
> phone line (see [docs/VOICE.md](docs/VOICE.md)).
> **Asterisk/SIP is not implemented**: a call runs over a WebSocket that a
> telephony bridge would connect to. Until then, **`python voice_app.py`** runs
> the whole conversation through the computer's **microphone and speaker** -
> measured live with a synthetic caller; a real person speaking into the
> microphone has not been measured yet. The same pipeline also takes the
> patient's words as **text** and returns the reply as **text**, through an
> HTTP API or an interactive console.

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
- [Performance](#performance)
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
Caller audio ──> /voice/call (WebSocket) ──┐
Microphone  ──> voice_app.py ──────────────┴──> ElevenLabs realtime STT
                                                  │ committed transcript
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
                  Reply text out ──> ElevenLabs streaming TTS ──> caller audio
                                                                  or speaker

Not implemented:  Asterisk/SIP (the telephone line itself)
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
| STT | ElevenLabs Scribe v2 Realtime over WebSocket |
| TTS | ElevenLabs streaming TTS (`eleven_v3_conversational`) |
| Microphone and speaker | sounddevice (PortAudio) |
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

Asterisk is **not** required: the telephony layer is not implemented. An
**ElevenLabs account** is needed only for speech (the voice call); everything
else works without one.

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
| httpx | 0.27 | 0.28.1 | Ollama and ElevenLabs TTS client; FastAPI test client |
| websockets | 12 | 15.0.1 | ElevenLabs realtime speech-to-text |
| sounddevice | 0.4 | 0.5.6 | Microphone and speaker (`voice_app.py`) |
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
5. Create the four composite indexes the dashboard's queries use
   (declared in [`firestore.indexes.json`](firestore.indexes.json)):
   ```powershell
   python scripts/firestore_indexes.py            # shows which exist
   python scripts/firestore_indexes.py --create   # builds the missing ones
   ```
   (or `firebase deploy --only firestore:indexes` with the Firebase CLI).
   Without them everything still works - the API notices a missing index,
   answers with a simpler query and logs which index to create - but those
   screens read more documents than they need to.

### 7. Configure Ollama (optional)

Install Ollama from <https://ollama.com>, then:

```powershell
ollama pull llama3.2
```

Ollama listens on `http://127.0.0.1:11434` by default (`OLLAMA_BASE_URL`).
Use `127.0.0.1`, not `localhost`: on Windows `localhost` is tried over IPv6
first and every request waited ~2.1 s for that to fail (measured: 2,236-2,316
ms against 191 ms). To run without Ollama, set `OLLAMA_ENABLED=false`; replies
then come from templates in all three languages.

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

### 10. Set up ElevenLabs (speech - optional)

Only the voice call needs this; typed text works without it. Full details and
the design decisions are in [docs/VOICE.md](docs/VOICE.md).

1. Create an account at <https://elevenlabs.io>.
2. In the ElevenLabs dashboard create an **API key** (Profile -> API keys).
3. Put it in `.env` - never in code, the dashboard, a report or Git:
   ```text
   ELEVENLABS_API_KEY=your-key-here
   ```
4. Speech-to-text: `ELEVENLABS_STT_MODEL=scribe_v2_realtime`, the language
   callers speak `ELEVENLABS_STT_LANGUAGE=ur`, and the line's audio
   `ELEVENLABS_STT_AUDIO_FORMAT=ulaw_8000` (a phone line; `pcm_16000` for a
   microphone).
5. Text-to-speech: `ELEVENLABS_TTS_MODEL=eleven_v3_conversational` (measured:
   same first-audio time as `eleven_flash_v2_5`, speaks Urdu) and
   `ELEVENLABS_TTS_OUTPUT_FORMAT=ulaw_8000`.
6. Choose the receptionist's voice and put its id in `ELEVENLABS_VOICE_ID`.
   The key needs the **Voices: Read** permission for this, and on a **free**
   ElevenLabs plan only the built-in voices work through the API (Voice
   Library voices are refused with `paid_plan_required`; the list marks them):
   ```powershell
   python scripts/voice_check.py --voices
   ```
7. Dependencies are already in `requirements.txt` (httpx, websockets). The
   official `elevenlabs` package is not needed, and FFmpeg is not needed.
8. Test speech-to-text on a recording (16-bit WAV):
   ```powershell
   python scripts/voice_check.py --stt recording.wav
   ```
9. Test text-to-speech:
   ```powershell
   python scripts/voice_check.py --tts "Ji bilkul. Kis doctor ke liye appointment chahiye?"
   ```
10. Run the full voice pipeline - simulated calls, with the latency of every
    turn, and the model comparison:
    ```powershell
    python scripts/voice_benchmark.py all
    ```
    A telephony bridge connects to `ws://<host>:8000/voice/call` (docs/VOICE.md,
    section 5); set `VOICE_GATEWAY_TOKEN` when it is not on the same machine.

These checks use ElevenLabs credits.

### 11. Talk to it by voice (microphone and speaker)

After step 10 - no other setup:

```powershell
python voice_app.py --list-devices   # microphones and speakers; * = default
python voice_app.py --mic-test       # is the microphone loud enough? (sends nothing)
python voice_app.py --sound-test     # a tone on the speaker (no credits)
python voice_app.py                  # talk
```

Speak after "LISTENING..."; the end of a sentence is detected by the pause.
`python voice_app.py --ptt` is push-to-talk instead: SPACE to start, SPACE to
stop. `q`, Esc or Ctrl+C ends the conversation. Use **headphones** where you
can: through loudspeakers the microphone is muted while the agent speaks (so
it never transcribes itself), which also means it cannot be interrupted.
Bookings go to the database in `.env` - Firestore when configured, so they
appear in the dashboard; `--local` uses the in-memory demo clinic instead.
Details: [docs/VOICE.md](docs/VOICE.md), section 10.

### 12. Run the tests

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

# Speech (ElevenLabs) - docs/VOICE.md
ELEVENLABS_API_KEY=
ELEVENLABS_STT_MODEL=
ELEVENLABS_STT_BATCH_MODEL=
ELEVENLABS_STT_LANGUAGE=
ELEVENLABS_STT_SECONDARY_LANGUAGES=
ELEVENLABS_STT_AUDIO_FORMAT=
ELEVENLABS_STT_SILENCE_SECS=
ELEVENLABS_STT_CHUNK_MS=
ELEVENLABS_STT_KEYTERMS=
ELEVENLABS_TTS_MODEL=
ELEVENLABS_VOICE_ID=
ELEVENLABS_TTS_OUTPUT_FORMAT=
ELEVENLABS_TTS_LANGUAGE=
ELEVENLABS_TIMEOUT=
ELEVENLABS_MAX_RETRIES=
ELEVENLABS_ENABLE_LOGGING=
VOICE_GATEWAY_TOKEN=

# Microphone voice mode (voice_app.py)
MICROPHONE_DEVICE=
SPEAKER_DEVICE=
VOICE_INPUT_MODE=
VOICE_DEBUG=
VOICE_MIC_FORMAT=
VOICE_TTS_OUTPUT_FORMAT=
VOICE_BARGE_IN=
VOICE_OLLAMA_EVERY_TURN=

# Development only
PERF_DEBUG=            # 1: log database reads per request, add a Server-Timing header
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
real data. Note that this console re-writes the demo clinic, doctors,
schedules and patients (`firebase/seed_data.py`) into the configured database
each time it starts - with Firestore configured, that overwrites changes made
to those records in the dashboards. `voice_app.py` and the API never do.

### Talk to it by voice

```powershell
python voice_app.py              # microphone -> ... -> speaker (step 11)
python voice_app.py --debug      # show every stage and its timing
```

With `--debug` (or `VOICE_DEBUG=true`) each turn shows what was heard, the
intent, the Dialog Manager's action, the database result, whether Ollama's
wording was approved or rejected (and why), what is spoken, and where the
time went.

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
| `/voice/call` (WebSocket) | telephony bridge | A spoken call: audio in, transcripts and audio out |
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
│   ├── admin.py            /admin/*               (administrator)
│   └── data.py             how the dashboards read Firestore: narrow queries,
│                           batched patients, counts, paging, parallel reads
├── appointment_backend/    the only code that creates or changes appointments
│   ├── appointment_service.py   facade
│   ├── booking_service.py, cancellation_service.py, reschedule_service.py
│   ├── availability_service.py, validation.py, result.py
│   ├── statistics.py       counts by status and period
│   └── api.py              public appointment routes
├── dialog_manager/         conversation state, slots, entities, fallback
├── ollama_judge/           LLM client, prompts, languages, templates, validator
├── speech/                 ElevenLabs STT and TTS, audio formats, one voice call,
│                           microphone.py and speaker.py (voice_app.py)
├── firebase/               Firestore repository and one service per collection,
│                           plus cache.py (clinic and doctor register) and
│                           metrics.py (read counting for PERF_DEBUG)
├── intent_detection/       mBERT intent model
│   ├── src/                preprocess, train, evaluate, inference, data builders
│   ├── data/               label maps (processed CSVs are regenerated)
│   ├── outputs/            test metrics, classification report, training plots
│   └── models/             trained weights (not in Git - see step 4)
├── Dashboard/              doctor and administrator web app (HTML/CSS/JS)
├── Data/                   raw intent datasets
├── docs/
│   ├── AI_Voice_Appointment_System_Final_Report.pdf
│   ├── PERFORMANCE.md      what was slow, what changed, measured before/after
│   └── report/final_report.html   source of the report
├── reports/                evaluation outputs (intent audit, latency, live runs)
├── scripts/                run_tests, build_report, demo, evaluate_intents,
│                           measure_performance, verify_firebase,
│                           firestore_indexes
├── tests/                  unittest suite
│   └── browser/            headless-Chrome dashboard checks and page timing
├── config.py               every setting, from environment variables
├── firestore.indexes.json  the composite indexes the queries need
├── firebase.json           points the Firebase CLI at that file
├── voice_pipeline.py       interactive console pipeline
├── voice_app.py            talk by microphone and speaker
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
| Appointment backend | 61 | 61 | 0 |
| Firebase | 40 | 31 | 9 |
| Ollama / wording | 54 | 51 | 3 |
| End-to-end | 25 | 25 | 0 |
| Dashboards (including performance) | 153 | 152 | 1 |
| Speech (ElevenLabs simulated; microphone and speaker simulated) | 94 | 94 | 0 |
| Security | 11 | 11 | 0 |
| **Total** | **595** | **582** | **13** |

No test fails. The skipped tests are the live Firestore and Ollama checks,
which need `--live`. The speech tests run against simulated ElevenLabs
services and a simulated sound card; the real service is measured by
`scripts/voice_benchmark.py` and the microphone app by live runs (results in
[docs/VOICE.md](docs/VOICE.md), sections 9 and 10).

Without the trained model (a fresh clone before step 4) the suite still
passes: the test classes that exercise the real model skip themselves,
giving 589 tests, 534 passed, 55 skipped, 0 failed (measured with
`INTENT_MODULE_DIR` pointing at a copy without `models/`; the speech tests
that run whole calls through mBERT skip themselves too). The dialogue tests use a
scripted stand-in classifier on purpose (`tests/helpers.py`), so they test
the dialogue logic on its own; the running system always uses mBERT.

**Browser checks** drive the real dashboard in headless Chrome - 59 and 51
checks, all passing. See [`tests/browser/README.md`](tests/browser/README.md).

**Page timing** against a running API started with `PERF_DEBUG=1`:
`node tests/browser/measure_pages.js <url> <doctor id> <password> <admin id>
<password>` (see [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)).

**Rebuilding the report** after editing its source:

```powershell
python scripts/build_report.py
```

---

## Performance

The dashboards read Firestore through [`api/data.py`](api/data.py): only the
records a screen shows, patients fetched by id in one batch, totals counted by
Firestore instead of downloaded, independent reads run in parallel, and lists
paged on the server. The browser shares identical requests and forgets
everything it reused the moment anything is changed.

Measured on the live Firestore project, warm server, median of three runs
against the previous code:

| | Before | After |
|---|---:|---:|
| Doctor: sign in to a usable dashboard | 7.3 s | 1.2 s |
| Doctor: 30-second refresh | 3.4 s, 4 requests | 0.57 s, 1 request |
| Administrator: sign in to the overview | 5.7 s | 1.7 s |
| Administrator: doctor register | 3.0 s | 0.72 s |
| Every page once: time / API requests | 40.8 s / 29 | 12.0 s / 20 |
| The same pages on a clinic of 20,000 appointments: Firestore reads | 157,429 | 9,240 |

What was slow, every change, the indexes, the measurements that did not
improve and why: [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md). Setting
`PERF_DEBUG=1` makes the API log and return (as a `Server-Timing` header) the
round trips and documents read by each request.

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
| Telephony | Asterisk/SIP not implemented; a call runs over the `/voice/call` WebSocket | An Asterisk bridge (ARI External Media or AudioSocket) - docs/VOICE.md, section 5 |
| Speech | Measured live with synthetic callers only: 5 of 6 test calls complete; caller stops → agent speaks 2.5 s median (3.0 s in the microphone app with Ollama wording every reply); some Urdu words misheard ("kal" → "Cole", "kal ke liye" → "Calcutta") | A real person through the microphone; real callers' recordings (`--recordings`); a phone line |
| Out-of-scope questions | mBERT has no out-of-scope class ("Aaj mausam kaisa hai?" gets clinic hours) | An out-of-scope class in the next training round |
| Patient notifications | Queued in Firestore and shown; not sent | Implement the `NotificationSender` interface for an SMS or voice provider |
| Intent model | Macro-F1 0.836; emergency recall 0.55 (backed by the safety net) | More natural examples per intent and a fresh held-out set |
| LLM wording | Every voice reply goes to `llama3.2`; in two 21-turn text checks the validator accepted 15 and 13 wordings and rejected 3 and 5 (a changed day, a dropped slot or question, an added "nahi"); the facts of accepted wordings are checked, their grammar is not ("Aap kis din convenient rahega?" passes) | A larger local model (e.g. `qwen2.5:7b`, fits the 6 GB GPU) |
| Authentication | Prototype sessions | Firebase Authentication and rate limiting |
| Speed (voice turns) | Several seconds per Firestore-backed turn; the booking checks read one after another | Fewer round trips in the booking checks - the dashboards' reads were reworked this way ([`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)) |
