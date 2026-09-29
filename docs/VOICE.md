# Voice: ElevenLabs speech-to-text and text-to-speech

How the assistant hears and speaks, what was decided and why, and what has
and has not been tested. Code: [`speech/`](../speech), the call endpoint
[`api/voice_ws.py`](../api/voice_ws.py), tests
[`tests/test_speech.py`](../tests/test_speech.py), measurement
[`scripts/voice_benchmark.py`](../scripts/voice_benchmark.py).

**Status.** Speech-to-text and text-to-speech are implemented, tested offline
against simulated ElevenLabs services, and measured against the **real
ElevenLabs API** (29 September 2026, free plan): section 9. The callers in
those measurements are synthetic - ElevenLabs voices reading the test
sentences - not patients on a phone line. **Asterisk/SIP is not
implemented**: the call runs over a WebSocket that a telephony bridge would
connect to (section 5). Until it is, `python voice_app.py` puts the
computer's **microphone and speaker** in the phone line's place (section 10).

---

## 1. The path of one turn

```text
caller audio (8 kHz mu-law, as a phone line carries it)
   │  speech/call.py  VoiceCall.feed()
   ▼
SpeechGate ── silence before the caller speaks is held back (not paid for)
   │
   ▼
ElevenLabs Scribe v2 Realtime  (WebSocket, speech/stt.py)
   │  partial_transcript ... partial_transcript    → shown; used for barge-in
   │  committed_transcript                          → the ONLY thing that is a turn
   ▼
VoicePipeline.process(session_id, text, patient_id)          (unchanged)
   mBERT → Dialog Manager → Appointment Backend → Firestore → judge/validator
   │
   ▼  the approved reply text, as is
ElevenLabs streaming TTS  (speech/tts.py)  → first audio bytes played at once
   │
   ▼
AudioOutput.play()  → caller
```

The speech layer decides **when** a turn happens; it never decides **what** is
said. mBERT, the Dialog Manager, the backend and the judge are called exactly
as for typed text, in one dialogue session for the whole call, so state, slots,
corrections, confirmations and intent switching behave the same. The reply
spoken is the pipeline's final `response`, which the validator has already
checked against the backend result; TTS neither rewords it nor adds to it (a
test asserts the synthesised text equals the reply, turn by turn).

## 2. Configuration

Every model, voice and format comes from `.env` (config.py, "Speech"):
`ELEVENLABS_API_KEY`, `ELEVENLABS_STT_MODEL` (scribe_v2_realtime),
`ELEVENLABS_STT_LANGUAGE` (ur), `ELEVENLABS_STT_SECONDARY_LANGUAGES`,
`ELEVENLABS_STT_AUDIO_FORMAT` (ulaw_8000), `ELEVENLABS_STT_SILENCE_SECS` (0.8),
`ELEVENLABS_STT_KEYTERMS` (false), `ELEVENLABS_TTS_MODEL`
(eleven_flash_v2_5), `ELEVENLABS_VOICE_ID`, `ELEVENLABS_TTS_OUTPUT_FORMAT`
(ulaw_8000), `ELEVENLABS_TTS_LANGUAGE`, `ELEVENLABS_TIMEOUT` (15 s),
`ELEVENLABS_MAX_RETRIES` (1), `ELEVENLABS_ENABLE_LOGGING`, and
`VOICE_GATEWAY_TOKEN` for the call endpoint. The TTS model default,
`eleven_v3_conversational`, was chosen by measurement (section 9).

**Voices on a free plan.** The API refuses every Voice Library voice on a free
ElevenLabs plan (402 `paid_plan_required`), even one added to "My Voices";
only the built-in voices work. The measurements used the built-in voice
"Sarah" for the agent and "Chris" for the simulated caller.
`python scripts/voice_check.py --voices` marks which voices need a paid plan.

**SDK.** The official Python SDK (`elevenlabs`) cannot be installed on this
Windows machine: its file paths exceed the 260-character limit unless long
paths are enabled system-wide. The code therefore calls the official REST and
WebSocket endpoints directly with `httpx` and `websockets`, which the project
already depends on - the same API the SDK wraps. Only `speech/stt.py` and
`speech/tts.py` know ElevenLabs exists.

## 3. Hearing the caller

**Partial vs committed.** Scribe realtime sends partial transcripts while the
caller speaks and one committed transcript when its voice-activity detection
decides the turn is over (`commit_strategy=vad`). Partials never reach the
dialogue or the backend.

**Turn end.** `ELEVENLABS_STT_SILENCE_SECS` (0.8 s) is how long a pause must
last before the turn is committed: long enough that "Mujhe Dr Ahmed... se kal
appointment chahiye" stays one sentence, short enough not to delay the reply.
If ElevenLabs does not commit after 3 s of silence, the call commits the turn
itself.

**Languages.** Callers speak Urdu, English or both; "Roman Urdu" is a way of
writing Urdu, not a way of speaking it. With `language_code=ur` Scribe writes
what it hears in Urdu script, and without it may choose Hindi (Devanagari)
for the same words. mBERT handles all three - see section 7 - but which
setting transcribes this clinic's callers best is measured, not assumed.
ElevenLabs lists Urdu in its "moderate" accuracy band (25-50 % word error
rate); what matters here is whether the meaning survives into the intent.

**No translation.** Transcription only; nothing is translated to English.

**Keyterms.** `speech/keyterms.py` builds them from the live register - active
doctors' names, their specialisations, the clinic's name, then core
appointment words - so a doctor added by the administrator is recognised on
the next call. Realtime allows 50 terms of 20 characters and ElevenLabs
charges extra for keyterm prompting, so it is off unless
`ELEVENLABS_STT_KEYTERMS=true`.

**Noise and silence.** The speech gate learns the line's background level and
only opens for sustained speech (60 ms); a transcript that comes back empty
(noise, a cough) is not a turn. After two in a row the caller hears "Maazrat,
mujhe clear sunai nahi diya. Dobara bol dein." - not after every rustle.

## 4. Speaking

**Streaming.** `ElevenLabsTTS.stream()` yields audio as it arrives, and each
chunk is handed to the call immediately; the first chunk is the moment the
caller starts hearing the agent. The reply is complete before TTS starts
(the validator needs the whole sentence), so streaming the LLM's tokens into
TTS would bypass the hallucination check and is deliberately not done.

**Model.** ElevenLabs' own language lists (September 2026): Flash v2.5 - the
low-latency model - covers Hindi but **not Urdu**; Urdu is supported by
Eleven v3 and its low-latency variant `eleven_v3_conversational`;
Multilingual v2 does not list Urdu. Measured here (section 9), both give the
first audio in ~0.82 s, and in the samples transcribed back Flash turned a
time and a negation around where v3 conversational did not, so
**`eleven_v3_conversational` is the default**.

**Fixed phrases cached.** Only phrases the call marks as fixed - the
greeting, "please say that again", the technical-problem apology, the
goodbye - are kept as audio, keyed on the exact text and every voice setting.
Replies with a doctor, a date, a time or a patient in them are synthesised
every time.

## 5. Telephony (not implemented) and the interface for it

There is no Asterisk or SIP code in this project. The call runs over a
WebSocket, `/voice/call`: binary frames carry the caller's audio in and the
agent's audio out, text frames carry events. A telephony bridge implements
the other side:

| The bridge must | Via |
|---|---|
| send the caller's audio as it arrives | binary frames, `format=ulaw_8000` |
| play the agent's audio | binary frames (ELEVENLABS_TTS_OUTPUT_FORMAT) |
| flush its playback buffer when the caller talks over the agent | event `{"type": "stop_playback"}` |
| hang up | event `{"type": "hangup"}` |
| present the gateway secret | `?token=` or `Authorization: Bearer` |

**Audio formats - no conversion on the call path.** A phone line carries
8 kHz G.711 mu-law. ElevenLabs realtime STT accepts `ulaw_8000`, and TTS can
answer in `ulaw_8000`, so audio passes from Asterisk to ElevenLabs and back
without being decoded, resampled, re-encoded or written to disk. The only
decoding on the call path is reading sample values to measure loudness for
the gate. For an Asterisk bridge this means: ARI **External Media** with
`format=ulaw` passes mu-law RTP straight through; Asterisk's **AudioSocket**
carries 8 kHz 16-bit linear PCM, which maps to `pcm_8000` in both
directions, again without conversion.

**Barge-in.** When the caller makes sound again (the speech gate opens on new
audio) and a partial transcript of three or more characters arrives while the
agent is speaking, the call cancels the TTS stream and sends `stop_playback`.
Both are needed: in live calls ElevenLabs sometimes sent a partial about
audio it had already committed, and on text alone that cut the agent's reply
off. The same rule keeps an echo of the agent's own voice from stopping it. "Speaking" includes audio already sent but still playing:
ElevenLabs streams faster than real time, so a four-second reply can arrive in
one second. Whether the phone actually stops is up to the bridge.

**End of call.** A goodbye ("Allah Hafiz", "Thank you, bye") ends the dialogue
(`end_conversation`); the farewell is spoken, then the transcription session is
closed, the dialogue session ended, and `hangup` sent.

## 6. Failures, retries and fallback

| Failure | What happens |
|---|---|
| Timeout, rate limit, 5xx, dropped connection | retried `ELEVENLABS_MAX_RETRIES` (1) times |
| ElevenLabs closes a session after ~15 s without audio (measured: 15.3 s) | a keep-alive sends 100 ms of silence after 5 s of nothing sent (~2 % extra audio) |
| Voice Library voice on a free plan | `paid_plan_required`, never retried |
| Invalid key, exhausted quota, invalid voice or model | never retried |
| Transcription connection drops mid-call | reconnect once (at most twice per call); the sentence the caller was saying is re-sent |
| Transcription cannot be (re)opened | "Maazrat, is waqt system mein masla hai..." and the call ends cleanly |
| TTS fails before any audio | retried once; then the reply is sent as a `tts_failed` event with its text, the call carries on |
| TTS fails after audio started | not retried - the caller would hear words twice |
| Empty text, empty or <100 ms audio | refused locally; no request is made |

The caller never hears a technical reason. There is no second TTS provider:
the fallback is the text, which a bridge can play from its own prompts.

## 7. Language checks - three mBERT confusions found and corrected

Running the spec's sentences through mBERT before any audio was involved
found three confident mistakes that speech would expose:

| Caller says | mBERT | Effect |
|---|---|---|
| "Allah Hafiz" (capitalised, as STT writes it) | thank_you 0.87 | the call never ends |
| "ڈاکٹر احمد کل دستیاب ہیں؟" (Urdu script) | clinic_timing 0.97 | clinic hours instead of the doctor's slots |
| "Meri appointment Friday ko 5 baje kar dein" | book_appointment 0.66 | a new booking instead of moving the appointment |

Transcript formatting was measured first on mBERT's held-out test split (354
sentences): original 0.898 accuracy, STT-style (capital first letter, full
stop) 0.895, all-lowercase 0.881, Title Case 0.825 - so lowercasing
transcripts would make things worse, and the fix is not normalisation.

The corrections (dialog_manager/semantic_fallback.py, next to the existing
"Mje docter" fix) are narrow: a farewell only when the whole utterance is
farewell plus courtesy; availability only for a yes/no question about a
particular day with a doctor mentioned, never a "when" question. The third is
genuinely ambiguous - the training data labels "Mera appointment Saturday ko
rakh dein" a booking - so the Dialog Manager decides with the caller's diary:
a caller with an upcoming appointment is moving it, one without is booking.
Checked against all 3,461 labelled sentences (train, validation, test): **no
answer changes**. mBERT itself is not retrained and the confidence threshold is
unchanged.

The critical "Mje docter ke pas jana h" gives `book_appointment` (0.99-1.00)
in every spelling an STT engine might produce - Roman, Urdu script, and Hindi
script.

**Known limit.** A question outside the clinic's business ("Aaj mausam kaisa
hai?") is classified as clinic information and answered with opening hours:
the model has no out-of-scope class.

**Found by the live calls, and fixed in the Dialog Manager:**

* *Urdu punctuation.* Speech-to-text writes the Urdu comma and question mark
  (`،` `؟`); the yes/no detector did not strip them, so "ہاں، کر دیں" was not
  a "yes". Fixed in `validators.detect_yes_no`.
* *A "yes" overridden.* mBERT reads "ہاں، کر دیں" ("yes, do it") as
  `change_date` (0.87); with a booking waiting for confirmation that counted as
  a new task and the booking was asked about again. The confirmation step now
  treats a change-date/time/doctor label as part of the booking - the rule
  `_effective_intent` already applied everywhere else.
* *mBERT loaded on the first caller.* Loading takes 9-20 s here; the API now
  loads it in the background at start-up.

## 8. Cost, logging and security

* **Cost.** Silence before the caller speaks is not sent (the gate); nothing is
  transcribed twice; fixed phrases are cached; retries are bounded; each call
  logs what it used (audio seconds sent and held back, TTS characters and
  requests, cache hits).
* **Logging.** Session id, STT connected, transcript committed (its length,
  not its words - the words are at DEBUG), intent, action, latency. The key is
  never logged: the `websockets` library logs request headers at DEBUG,
  including `xi-api-key`, so its logger is held at INFO (a test caught this).
* **Security.** The key is read from `.env` on the server and sent only in the
  `xi-api-key` header to ElevenLabs; it is never in a URL, a response, an
  event, the dashboard, or Git. The call endpoint accepts only the gateway
  token (or, with none set, only this machine). A browser client would need
  ElevenLabs' single-use token, not the key - none exists in this project.

## 9. Testing and results

**Offline (no key needed)** - `python scripts/run_tests.py --category speech`:
realtime and batch STT against a local fake of the ElevenLabs realtime
protocol over a real WebSocket, streaming TTS against a mock, and whole calls
through the real mBERT, Dialog Manager and backend (in-memory database):
booking, cancellation, rescheduling, lookup, fee, availability, correction,
intent switching, goodbye, barge-in, noise, silence, disconnect and
reconnect, invalid key, TTS failure, twenty turns in one session with no leaked
connection or task, and the key never appearing in events or logs.

These prove the plumbing. They cannot prove how well ElevenLabs hears Urdu or
how it pronounces Roman Urdu.

**Live (needs the key)** - `python scripts/voice_benchmark.py all`:

* **TTS**: each model on Roman Urdu, Urdu script, English, mixed and a long
  reply; time to first audio and to completion; samples in
  `reports/voice/samples/` to **listen to** (pronunciation is judged by ear).
* **STT**: the spec's caller sentences through realtime STT at real-time
  pace, then mBERT; connect time, first partial, time from end of speech to
  the committed transcript, and whether the intent is right. Callers are
  ElevenLabs voices reading the sentences - an approximation of a patient on
  a phone line; real recordings can be added with `--recordings`.
* **Calls**: booking, cancellation, rescheduling, fee, availability and
  goodbye as whole calls, measuring **caller stops speaking → agent starts
  speaking** for every turn.

### Results (29 September 2026, real ElevenLabs API, free plan)

Measured from this machine in Pakistan; raw data in `reports/voice/`.

**Speech-to-text** (`stt.json`) - Scribe v2 Realtime, `language_code=ur`,
8 kHz mu-law streamed at real-time pace; caller "Chris" (a built-in American
English voice) reading the spec's sentences as written. **11 of 12 correct**:

| Said | Transcript | mBERT | |
|---|---|---|---|
| I want an appointment with Dr Ahmed. | I want an appointment with Dr. Ahmed. | book | ✓ |
| مجھے ڈاکٹر احمد سے اپائنٹمنٹ چاہیے | مجھے ڈاکٹر احمد سے appointment چاہیے۔ | book | ✓ |
| Mujhe Dr Ahmed se appointment chahiye. | مجھے Dr. Amit سے appointment چاہیے۔ | book | ✓ |
| Mje doctor ke pas jana h. | Miss Doctor کی پس جانا ہے۔ | book | ✓ |
| **Mje docter ke pas jana h** | جی، doctor کے پاس جانا ہے۔ | **book** | ✓ |
| Dr Ahmed kal available hain? | Dr. Ahmed kal available ہے؟ | availability | ✓ |
| Meri appointment cancel kar dein. | Many appointment cancel card. | cancel | ✓ |
| Dr Ahmed ki fee kitni hai? | Dr. Ahmed, کی فکت نہیں ہے۔ | availability | ✗ |
| Allah Hafiz | الحافز۔ | goodbye | ✓ |
| Thank you, bye | Thank you. Bye. | goodbye | ✓ |
| 2 s of silence / 2 s of line noise | (nothing) | - | ✓ ✓ |

The committed transcript arrived 1.5-1.8 s after the caller stopped (0.8 s
of that is the end-of-turn pause); a session connects in ~0.8 s. Nothing was
translated: Urdu came back in Urdu script with English words in Latin script.
The one failure is recognition ("fee kitni" heard as "فکت نہیں").

**Text-to-speech** (`tts.json`) - 11 sentences (Roman Urdu, Urdu script,
English, mixed, one long reply), two runs each, voice "Sarah", ulaw_8000:

| Model | First audio (median) | Complete (median) |
|---|---:|---:|
| eleven_flash_v2_5 | 818 ms | 984 ms |
| eleven_v3_conversational | 824 ms | 1,377 ms |

Every sample was transcribed back with Scribe. Both were intelligible in all
sentences (Scribe heard Hindustani and wrote it in Hindi script), but Flash's
"**4** baje ka slot booked hai" came back as "**5** baje" and "available
**nahi**" as "**unavailable** nahi" - a changed time and a reversed meaning -
where v3 conversational came back correct. Hence the default. Pronunciation
is for a person to judge: the samples are in `reports/voice/samples/` (00-04
Roman Urdu, 05-06 Urdu script, 07-08 English, 09 mixed, 10 long).

**Whole calls** (`calls_final_run1.json`, `calls_final_run2.json`,
`calls_final_llm.json`) - STT and TTS as above, the real pipeline on the
in-memory demo clinic; the caller's words synthesised from Urdu script (a
caller speaks Urdu - read as Roman Urdu, the English voice anglicised it and
"se kal" was heard as "Sehgal": `calls_roman_caller_llm_flash.json`).

| Call | Outcome (both runs) |
|---|---|
| Booking - "مجھے ڈاکٹر احمد سے کل اپائنٹمنٹ چاہیے" / "چار بجے" / "ہاں، کر دیں" | appointment created, kal 4 pm |
| Cancellation | cancelled |
| Rescheduling - "میری اپائنٹمنٹ جمعہ کو پانچ بجے کر دیں" / "ہاں" | moved to Friday 5 pm |
| Fee | "2000 rupay", from the database |
| Availability - "ڈاکٹر احمد کل available ہیں؟" | "kal" heard as "Cole": the agent asked which day |
| Goodbye - "شکریہ، اللہ حافظ" | farewell spoken, call ended, everything closed |

**Caller stops speaking → agent starts speaking**, 20 turns over two runs,
Ollama judge off: **median 2.52 s** (2.30-2.98 s) - committed transcript
+1.61 s, pipeline 0.05 s, first TTS audio 0.65 s. Every reply was spoken.
With the judge on (`calls_final_llm.json`) the ordinary turns are the same
(2.4-2.7 s) but the turns that book or cancel take **9.0-11.3 s**: the local
llama3.2 rewording the confirmation. (Measured before `OLLAMA_BASE_URL`
defaulted to `127.0.0.1`: every Ollama request then also waited ~2.1 s for
`localhost` to fail over IPv6 - section 10.) Whether that wording is worth ~7 s on a
phone call is a choice (`OLLAMA_ENABLED`); the deterministic replies are
already validated.

**What these numbers are not.** Synthetic callers, one machine, one network,
the free plan; not real patients, not a phone line, not Asterisk.

---

## 10. Microphone mode (`voice_app.py`)

The development stand-in for the phone line: the patient speaks into the
computer's microphone and hears the agent through its speaker or headphones.

```text
microphone ─ 16 kHz 16-bit mono (pcm_16000) ─ speech/microphone.py
   │  VoiceCall.feed()          (speech/call.py - the same object a phone bridge drives)
   ▼
SpeechGate → ElevenLabs realtime STT → COMMITTED transcript only
   ▼
VoicePipeline.process   mBERT → Dialog Manager → Appointment Backend → Firestore
                        → Ollama wording → validator            (voice_pipeline.py)
   ▼  the approved reply text
ElevenLabs streaming TTS (pcm_22050) → speech/speaker.py → speaker / headphones
```

Nothing of the conversation was rebuilt. The microphone is one more source of
audio for `VoiceCall`, and the speaker one more `AudioOutput` (`play`, `stop`,
`hang_up`) - so what works here is what a telephony bridge will drive.

| File | What it adds |
|---|---|
| `voice_app.py` | the program: devices, warm-up, the conversation loop, the terminal |
| `speech/microphone.py` | device listing and choice, capture, errors in words |
| `speech/speaker.py` | streamed playback, stop, "still playing?" |
| `speech/call.py` | half duplex (`listen_while_speaking`), push-to-talk (`begin_utterance` / `end_utterance`), `interrupt()` |
| `speech/stt.py` | `commit_strategy="manual"` for push-to-talk |
| `voice_pipeline.py` | `phrase_every_turn`, and each turn's timings |
| `ollama_judge/` | `judge.phrase()`, its prompt, and `validate_rewording()` |

### Audio

* **Microphone:** `pcm_16000` - a format ElevenLabs' realtime STT takes as is,
  and one every sound card records (on Windows the MME driver resamples).
  A device that refuses it is opened at its own rate and channel count and
  converted in `microphone.py` (linear interpolation, channel average - no
  FFmpeg). Both microphones here record 16 kHz directly.
* **Speaker:** `pcm_22050` from ElevenLabs, played as it streams in: the first
  chunk is heard while the rest is still arriving. Nothing is written to disk.
* **Driver latency:** both streams are opened with PortAudio's `latency="low"`.
  Measured on this machine: speaker 200 → 100 ms, microphone 40 → 20 ms. The
  streams are opened after mBERT and Ollama are loaded - importing torch
  starved an open speaker stream (3 underruns counted in live run 2, none
  after the change).

### End of speech

* **Voice activity (default):** the speech gate opens on the caller's voice
  (so silence is not paid for) and ElevenLabs commits the sentence after
  `ELEVENLABS_STT_SILENCE_SECS` (0.8 s) of silence. A pause shorter than that
  stays inside the sentence; a longer one ends it, and the rest arrives as the
  next turn - the Dialog Manager keeps the slots, so "Mujhe Dr Ahmed" ...
  "se kal appointment chahiye" still books. Not yet measured with a person.
* **Push-to-talk** (`--ptt` or `VOICE_INPUT_MODE=push_to_talk`): SPACE starts,
  SPACE again stops (a Windows console reports key presses, not releases, so
  hold-to-talk would need an extra library). Everything between the presses
  is sent however quiet, and the second press commits it
  (`commit_strategy=manual`).

### The agent must not hear itself

Through loudspeakers the microphone hears the agent. By default the
microphone is **muted while the agent is audible** - while its reply is being
generated, while the speaker is still playing, and for 300 ms of room echo
after - by replacing the audio with silence, so a sentence the caller was in
the middle of still ends normally. The cost: the agent cannot be
interrupted. With headphones, `VOICE_BARGE_IN=true` keeps listening and uses
the call's existing barge-in (it stops the reply when the caller starts
talking); in push-to-talk, pressing SPACE while the agent talks stops it.
Both interruptions are tested with simulated audio only.

### Ollama words every reply

The spec asks that what is spoken is the final **Ollama** response, approved
by the validator. The text pipeline sends only turns with a database result
to Ollama (`OLLAMA_TRANSLATE` is off because a 3B model's Roman Urdu cannot
be checked without one). The voice app turns on `phrase_every_turn`
(`VOICE_OLLAMA_EVERY_TURN=true`): every other reply - slot questions, the fee,
the farewell - is also worded by Ollama, and checked against the Dialog
Manager's own sentence for the turn (`validate_rewording`). The wording is
spoken only if it keeps **the same** numbers, times, day words and doctor
names (none added, none dropped), the same number of negations, keeps the
question the caller must answer and adds no other, claims nothing was done,
adds no "hoon" statement about the receptionist, is in the reply language and
is not much longer. Otherwise the Dialog Manager's sentence is spoken.

Measured on text (real mBERT and llama3.2, 21 turns in English, Roman Urdu
and Urdu, twice): **15 and 13 wordings accepted, 3 and 5 rejected**, 3
backend turns handled by the existing judge; median 0.79 and 0.66 s per
reply. Rejections caught real errors: "kal" turned into "today", an
invented "Friday", a slot dropped from "4, 4:20 ya 4:40", an added "Nahin",
"take care of yourself" turned into "I take care of myself". What is not
checked is grammar: "Bilkul. Aap kis din convenient rahega?" passes. Before
the prompt showed examples in the reply language only, 4 of 21 were accepted
- a 3B model shown English examples answers in English.

**`localhost` cost 2.1 s per Ollama request.** On this machine every request to
`http://localhost:11434` took 2,236-2,316 ms against 191 ms for
`http://127.0.0.1:11434`: Windows tries IPv6 first and Ollama listens on IPv4.
The default is now `127.0.0.1`; this also shortens the judge turns of
section 9.

### Errors

| Situation | What happens |
|---|---|
| No microphone / speaker, or MICROPHONE_DEVICE not found | a sentence saying so (and `--list-devices`), exit before loading anything |
| Device busy (another program) | "in use by another program", exit |
| Microphone delivers only digital silence (muted, Windows privacy setting) | a warning every 30 s; the conversation stays open |
| Microphone stops (unplugged) | "stopped sending audio"; it is reopened every 2 s for 30 s, then the conversation ends |
| Speaker stops playing | its queued audio is dropped after 1 s so the microphone is not muted for ever |
| STT, TTS, Ollama failures | as for a call (section 6): nothing empty reaches mBERT, the reply is shown as text if it cannot be spoken, Ollama's absence means the Dialog Manager's sentence |

The booking happens in the Dialog Manager before the reply is worded or
spoken, so a failing Ollama or TTS cannot repeat it: a test books with both
failing and finds one appointment and six TTS attempts (three replies, one
retry each), never a second booking.

### Tests (`tests/test_voice_app.py`, 39)

A simulated sound card (`FakeSoundDevice`: callbacks on their own threads at
real-time pace, as PortAudio's) stands in for the hardware, and the fake
ElevenLabs and Ollama services of section 9 for the providers; mBERT, the
Dialog Manager, the backend, the validator and the app loop are real.
Devices (default, by number, by name, missing, wrong kind), capture,
conversion when 16 kHz is refused, busy, unplugged, muted; playback order,
stop, telephone audio, a stalled speaker; every validator rule; the judge's
`phrase`; commit strategies; the critical sentence spoken exactly as Ollama
worded it; a changed fact never spoken; Ollama down or crashing; the switch
off; the microphone deaf while the agent is audible (the test fails without
the mute); push-to-talk; interrupting; one booking despite failures; and the
whole app - greeting, the critical sentence, goodbye - through microphone and
speaker, checking the terminal shows every stage and never the key.

### Live runs (29 September 2026)

`voice_app.converse` - the program itself - with the real ElevenLabs STT and
TTS, real mBERT, real llama3.2 wording every reply, and the real default
speaker (`VG248`, NVIDIA HDMI audio, through PortAudio). The microphone was
**virtual**: a synthetic caller (ElevenLabs "Chris", 16 kHz) delivered at
real-time pace. The clinic was the in-memory one. Reports:
`reports/voice/mic_app_live_run1.json` ... `run3.json`.

| Caller said | Heard | Result |
|---|---|---|
| Mje docter ke pas jana h | "مجھے doctor کے پاس جانا ہے۔" / "جی، doctor کے پاس جانا ہے۔" | book_appointment → which doctor? (Ollama's wording) - 3 of 3 |
| ڈاکٹر احمد | "Doctor Ahmed." / "Doctor احمد۔" | → which day? - 3 of 3 |
| کل کے لیے (run 1) | "Calcutta." | no date: asked again - the booking did not complete |
| Tomorrow. (runs 2, 3) | "Tomorrow." | → what time? |
| شام پانچ بجے | "شام 5 بجے۔" / "شام پانچ بجے۔" | → "Dr Ahmed Khan ke paas kal shaam 5 baje waqt khali hai. Kya main book kar doon?" |
| ہاں، کر دیں | "ہاں، کر دیں۔" | **booked** (APT48D0EE, APT0B6D22), kal 5 pm |
| شکریہ، اللہ حافظ | "شکریہ۔ اللہ حافظ۔" | farewell, conversation ended, devices closed |

Runs 2 and 3 completed the booking by voice; run 1 did not, because "kal ke
liye" said by an American-accented synthetic voice was heard as "Calcutta"
(the same kind of miss as "kal" → "Cole" in section 9). Every reply of all
three runs was spoken.

**Caller stops → agent audio**, 18 turns: **median 3.05 s** (2.92-7.86 s);
run 3, the final configuration: median 3.29 s (2.98-4.23 s). Where it goes
(run 3, medians): transcript committed 1.50 s after the caller stopped (0.8 s
of it is the end-of-sentence pause), mBERT 84 ms, Dialog Manager 1 ms,
database 0 ms (in memory), **Ollama 0.91 s**, TTS first byte 0.69 s; then the
speaker starts within one 20 ms block, plus the driver's reported 100 ms.
The slow turns are the booking confirmations, worded by the existing judge:
1.87 s in run 3, 5.72 s in run 2 - where it then returned no usable sentence
and the Dialog Manager's was spoken.

**What was not tested:** a person speaking into the real microphone - the
spec's acceptance test, which needs you; loudspeaker echo in a real room;
barge-in with real audio; the voice app against Firestore (the runs used the
in-memory clinic so as not to write into the live database); whether the
speaker output was audible - it played through PortAudio without error, but
nobody listened. Both microphones on this machine were opened and their
levels measured (`--mic-test`: background noise ~0.005 on each, speech
threshold 0.015) - that is all the real microphones were tested for.
