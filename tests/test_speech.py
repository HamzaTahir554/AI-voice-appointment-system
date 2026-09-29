"""
Speech layer tests (speech/, api/voice_ws.py) - offline.

Nothing here reaches ElevenLabs: transcription runs against FakeScribe, a
local WebSocket server that follows ElevenLabs' documented realtime protocol,
and synthesis against a mock of the streaming endpoint (tests/speech_fakes.py).
What these prove is the plumbing - partial vs committed transcripts, turn
order, retries, barge-in, clean-up, cost control, key handling - and that the
committed text reaches the real mBERT / Dialog Manager / Backend unchanged.

What they cannot prove is how well ElevenLabs hears or speaks Urdu: that is
measured against the real service by scripts/voice_benchmark.py and reported
in docs/VOICE.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import unittest
from datetime import datetime, timedelta

import httpx

from config import Action, Collections, INTENT_MODEL_DIR, TIMEZONE
from speech import results as R
from speech.audio import SpeechGate, audio_format, pack_frames, rms, tone, _ULAW_DECODE
from speech.keyterms import MAX_CHARS, MAX_TERMS, build_keyterms, keyterms_from_repository
from speech.stt import ElevenLabsSTT
from speech.tts import ElevenLabsTTS
from tests.speech_fakes import API_KEY, FakeScribe, FakeTTS, MemoryOutput, ScribeThread

ULAW = audio_format("ulaw_8000")
MODEL_PRESENT = (INTENT_MODEL_DIR / "config.json").exists()


def speech_audio(ms: int = 600, pause_ms: int = 900) -> bytes:
    """A caller saying something (a loud tone stands in for a voice), then
    a pause long enough for the silence detector."""
    return tone(ULAW, ms, amplitude=0.3) + tone(ULAW, pause_ms)


def stt_for(scribe: FakeScribe, **overrides) -> ElevenLabsSTT:
    options = dict(api_key=API_KEY, base_url=scribe.base_url, language="ur",
                   secondary_languages=[], silence_secs=0.6,
                   audio_format_name="ulaw_8000", timeout=3, max_retries=1)
    options.update(overrides)
    return ElevenLabsSTT(**options)


def tts_for(fake: FakeTTS, **overrides) -> ElevenLabsTTS:
    options = dict(api_key=API_KEY, base_url="https://tts.test", voice_id="voice-1",
                   model="eleven_flash_v2_5", output_format="ulaw_8000", language="",
                   timeout=3, max_retries=1, http_client=fake.client())
    options.update(overrides)
    return ElevenLabsTTS(**options)


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------
class AudioTests(unittest.TestCase):

    def test_mu_law_values_are_the_g711_ones(self):
        self.assertEqual(_ULAW_DECODE[0xFF], 0)
        self.assertEqual(_ULAW_DECODE[0x00], -32124)
        self.assertEqual(_ULAW_DECODE[0x80], 32124)

    def test_formats(self):
        self.assertEqual(audio_format("ulaw_8000").bytes_for(100), 800)
        self.assertEqual(audio_format("pcm_16000").bytes_for(100), 3200)
        with self.assertRaises(ValueError):
            audio_format("mp3_44100_128")

    def test_silence_is_never_sent(self):
        gate = SpeechGate(ULAW, clock=lambda: 0.0)
        self.assertEqual(gate.push(tone(ULAW, 2000)), [])
        self.assertEqual(gate.held_back_bytes, ULAW.bytes_for(2000))

    def test_steady_background_noise_does_not_open_the_gate(self):
        gate = SpeechGate(ULAW, clock=lambda: 0.0)
        hum = tone(ULAW, 3000, amplitude=0.01, frequency=50, noise=0.004)
        self.assertEqual(gate.push(hum), [])
        self.assertFalse(gate.in_utterance)

    def test_speech_opens_it_with_the_first_syllable_kept(self):
        gate = SpeechGate(ULAW, clock=lambda: 0.0)
        gate.push(tone(ULAW, 1000))
        sent = gate.push(tone(ULAW, 600, amplitude=0.3))
        self.assertTrue(gate.in_utterance)
        self.assertGreater(sum(map(len, sent)), ULAW.bytes_for(600))   # + pre-roll
        # pauses inside the sentence are sent: the silence detector needs them
        self.assertEqual(sum(map(len, gate.push(tone(ULAW, 400)))), ULAW.bytes_for(400))
        gate.committed()
        self.assertEqual(gate.push(tone(ULAW, 400)), [])

    def test_a_click_is_not_speech(self):
        gate = SpeechGate(ULAW, clock=lambda: 0.0)
        self.assertEqual(gate.push(tone(ULAW, 20, amplitude=0.5) + tone(ULAW, 500)), [])


# --------------------------------------------------------------------------
# Keyterms
# --------------------------------------------------------------------------
class KeytermTests(unittest.TestCase):

    def test_from_the_live_register_within_elevenlabs_limits(self):
        from firebase.firebase_config import LocalRepository
        from firebase.seed_data import seed
        repo = LocalRepository()
        seed(repo, with_sample_appointment=False)
        terms = keyterms_from_repository(repo)
        self.assertIn("Ahmed Khan", terms)
        self.assertIn("Cardiologist", terms)
        # a doctor the administrator adds is recognised on the next call
        repo.set(Collections.DOCTORS, "D099", {"doctor_id": "D099", "name": "Dr Zainab Qureshi",
                                               "specialization": "Dentist", "active": True})
        self.assertIn("Zainab Qureshi", keyterms_from_repository(repo))
        self.assertIn("Dentist", keyterms_from_repository(repo))

    def test_limits_and_switched_off_doctors(self):
        doctors = [{"name": f"Dr Name{i} Surname{i}", "specialization": f"Spec{i}"}
                   for i in range(40)]
        doctors.append({"name": "Dr Gone Away", "active": False})
        terms = build_keyterms(doctors, {"name": "A clinic name that is far too long"})
        self.assertLessEqual(len(terms), MAX_TERMS)
        self.assertTrue(all(len(t) <= MAX_CHARS for t in terms))
        self.assertNotIn("Gone Away", terms)


# --------------------------------------------------------------------------
# Realtime transcription client, against the fake server
# --------------------------------------------------------------------------
class RealtimeSTTTests(unittest.IsolatedAsyncioTestCase):

    async def test_url_header_partial_and_committed(self):
        async with FakeScribe(["Meri appointment cancel kar dein"]) as scribe:
            stt = stt_for(scribe)
            session = await stt.open_realtime("ulaw_8000", ["Ahmed Khan", "appointment"])
            path = scribe.paths[0]
            for expected in ("model_id=scribe_v2_realtime", "audio_format=ulaw_8000",
                             "language_code=ur", "commit_strategy=vad",
                             "vad_silence_threshold_secs=0.6", "keyterms=Ahmed+Khan",
                             "keyterms=appointment"):
                self.assertIn(expected, path)
            self.assertNotIn(API_KEY, path)                   # the key is a header
            self.assertEqual(scribe.header_keys, [API_KEY])
            for chunk in pack_frames(speech_audio(), 800):
                await session.send(chunk)
            kinds, final = [], None
            while final is None:
                event = await session.next_event(timeout=5)
                kinds.append(event.kind)
                if event.kind == "final":
                    final = event
            self.assertIn("partial", kinds)                   # partials came first
            self.assertEqual(final.text, "Meri appointment cancel kar dein")
            self.assertEqual(final.result().to_dict()["is_final"], True)
            self.assertIsNone(final.language)                 # not invented
            await session.close()
            await session.close()                             # idempotent

    async def test_an_invalid_key_is_never_retried(self):
        async with FakeScribe() as scribe:
            stt = stt_for(scribe, api_key="wrong-key", max_retries=3)
            with self.assertRaises(R.SpeechError) as raised:
                await stt.open_realtime()
            self.assertEqual(raised.exception.code, R.AUTH_FAILED)
            self.assertEqual(len(scribe.header_keys), 1)

    async def test_a_rate_limit_is_retried_once(self):
        async with FakeScribe(script={1: "rate_limited"}) as scribe:
            session = await stt_for(scribe).open_realtime()
            self.assertEqual(scribe.connections, 2)
            await session.close()

    async def test_retries_stop_when_the_service_stays_down(self):
        async with FakeScribe(script={1: "rate_limited", 2: "rate_limited",
                                      3: "rate_limited"}) as scribe:
            with self.assertRaises(R.SpeechError) as raised:
                await stt_for(scribe, max_retries=1).open_realtime()
            self.assertEqual(raised.exception.code, R.RATE_LIMITED)
            self.assertEqual(scribe.connections, 2)

    async def test_unreachable_service(self):
        stt = ElevenLabsSTT(api_key=API_KEY, base_url="http://127.0.0.1:9",
                            timeout=2, max_retries=1)
        with self.assertRaises(R.SpeechError) as raised:
            await stt.open_realtime("ulaw_8000")
        self.assertIn(raised.exception.code, (R.UNAVAILABLE, R.TIMEOUT))

    async def test_a_dropped_connection_is_reported(self):
        async with FakeScribe(script={1: "drop_after_audio"}) as scribe:
            session = await stt_for(scribe).open_realtime()
            await session.send(tone(ULAW, 100, amplitude=0.3))
            event = await session.next_event(timeout=5)
            self.assertEqual((event.kind, event.error), ("error", R.DISCONNECTED))
            await session.close()

    async def test_not_configured(self):
        with self.assertRaises(R.SpeechError) as raised:
            await ElevenLabsSTT(api_key="").open_realtime()
        self.assertEqual(raised.exception.code, R.NOT_CONFIGURED)


class BatchSTTTests(unittest.IsolatedAsyncioTestCase):

    def stt(self, handler):
        self.requests = []

        def record(request):
            self.requests.append(request)
            return handler(request)
        client = httpx.AsyncClient(transport=httpx.MockTransport(record))
        return ElevenLabsSTT(api_key=API_KEY, base_url="https://stt.test", timeout=2,
                             max_retries=1, http_client=client)

    async def test_nothing_is_sent_for_empty_or_tiny_audio(self):
        stt = self.stt(lambda r: httpx.Response(200, json={"text": "x"}))
        self.assertEqual((await stt.transcribe(b"", "ulaw_8000")).error, R.EMPTY_AUDIO)
        self.assertEqual((await stt.transcribe(tone(ULAW, 50), "ulaw_8000")).error,
                         R.TOO_SHORT)
        self.assertEqual(self.requests, [])

    async def test_text_and_language_come_from_the_response_only(self):
        stt = self.stt(lambda r: httpx.Response(200, json={
            "text": "مجھے ڈاکٹر کے پاس جانا ہے", "language_code": "urd"}))
        result = await stt.transcribe(speech_audio(), "ulaw_8000")
        self.assertEqual(result.to_dict(), {"success": True, "text": "مجھے ڈاکٹر کے پاس جانا ہے",
                                            "language": "urd", "is_final": True})
        self.assertNotIn("confidence", result.to_dict())
        self.assertEqual(self.requests[0].headers["xi-api-key"], API_KEY)

    async def test_silence_comes_back_as_no_speech(self):
        stt = self.stt(lambda r: httpx.Response(200, json={"text": ""}))
        self.assertEqual((await stt.transcribe(tone(ULAW, 500), "ulaw_8000")).error,
                         R.NO_SPEECH)

    async def test_retry_policy(self):
        answers = iter([httpx.Response(503), httpx.Response(200, json={"text": "ok"})])
        stt = self.stt(lambda r: next(answers))
        self.assertTrue((await stt.transcribe(speech_audio(), "ulaw_8000")).success)
        self.assertEqual(len(self.requests), 2)

        stt = self.stt(lambda r: httpx.Response(401))
        self.assertEqual((await stt.transcribe(speech_audio(), "ulaw_8000")).error,
                         R.AUTH_FAILED)
        self.assertEqual(len(self.requests), 1)

        stt = self.stt(lambda r: httpx.Response(503))
        self.assertEqual((await stt.transcribe(speech_audio(), "ulaw_8000")).error,
                         R.UNAVAILABLE)
        self.assertEqual(len(self.requests), 2)               # once, then give up


# --------------------------------------------------------------------------
# Streaming synthesis client
# --------------------------------------------------------------------------
class TTSTests(unittest.IsolatedAsyncioTestCase):

    async def test_streams_the_text_as_given(self):
        fake = FakeTTS(chunks=4)
        tts = tts_for(fake)
        text = "Ji bilkul. Aapki appointment kal 4 baje confirm ho gayi hai."
        chunks = [c async for c in tts.stream(text)]
        self.assertEqual(len(chunks), 4)                      # arrived as a stream
        request = fake.requests[0]
        self.assertEqual(request.url.path, "/v1/text-to-speech/voice-1/stream")
        self.assertEqual(request.url.params["output_format"], "ulaw_8000")
        self.assertEqual(fake.texts(), [text])                # nothing reworded or added
        self.assertEqual(request.headers["xi-api-key"], API_KEY)
        self.assertNotIn(API_KEY, str(request.url))
        self.assertIsNotNone(tts.last_first_byte_ms)

    async def test_empty_text_and_missing_configuration(self):
        fake = FakeTTS()
        self.assertEqual((await tts_for(fake).synthesize("  ")).error, R.EMPTY_TEXT)
        self.assertEqual((await tts_for(fake, api_key="").synthesize("hi")).error,
                         R.NOT_CONFIGURED)
        self.assertEqual((await tts_for(fake, voice_id="").synthesize("hi")).error,
                         R.INVALID_VOICE)
        self.assertEqual(fake.requests, [])

    async def test_an_invalid_voice_is_not_retried(self):
        fake = FakeTTS(responses=[404, "ok"])
        result = await tts_for(fake).synthesize("Kal 3 aur 5 baje ke slots available hain.")
        self.assertEqual((result.success, result.error), (False, R.INVALID_VOICE))
        self.assertEqual(len(fake.requests), 1)

    async def test_rate_limit_retried_once_then_ok(self):
        fake = FakeTTS(responses=[429, "ok"])
        result = await tts_for(fake).synthesize("Dobara bol dein.")
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    async def test_timeouts_give_up_after_the_retry(self):
        fake = FakeTTS(responses=["timeout", "timeout", "ok"])
        result = await tts_for(fake).synthesize("Dobara bol dein.")
        self.assertEqual((result.success, result.error), (False, R.TIMEOUT))
        self.assertEqual(len(fake.requests), 2)

    async def test_a_sentence_is_never_started_twice(self):
        fake = FakeTTS(chunks=4, responses=["break", "ok"])
        result = await tts_for(fake).synthesize("Aapki appointment confirm ho gayi hai.")
        self.assertEqual(result.error, R.DISCONNECTED)
        self.assertEqual(len(fake.requests), 1)               # no retry after audio played

    async def test_only_fixed_phrases_are_cached(self):
        fake = FakeTTS()
        tts = tts_for(fake)
        for _ in range(3):
            result = await tts.synthesize("Shukriya. Allah Hafiz.", cacheable=True)
        self.assertTrue(result.from_cache)
        self.assertEqual(len(fake.requests), 1)
        for _ in range(2):
            await tts.synthesize("Aapki appointment kal 4 baje hai.")
        self.assertEqual(len(fake.requests), 3)               # dynamic: asked each time
        await tts_for(fake, voice_id="voice-2").synthesize("Shukriya. Allah Hafiz.",
                                                           cacheable=True)
        self.assertEqual(len(fake.requests), 4)               # other voice, other key


# --------------------------------------------------------------------------
# Whole calls: audio -> STT -> the real pipeline -> TTS
# --------------------------------------------------------------------------
def spoken_day():
    """'kal', unless tomorrow is Sunday (the seeded clinic is shut)."""
    tomorrow = datetime.now(TIMEZONE).date() + timedelta(days=1)
    if tomorrow.weekday() != 6:
        return "kal", tomorrow.isoformat()
    return "parson", (tomorrow + timedelta(days=1)).isoformat()


@unittest.skipUnless(MODEL_PRESENT, "needs the trained mBERT model (README step 4)")
class VoiceCallTests(unittest.IsolatedAsyncioTestCase):
    """The committed transcript goes through mBERT, the Dialog Manager, the
    Appointment Backend and the database exactly as typed text does, and the
    reply that comes back is what is spoken - nothing added or reworded."""

    counter = 0

    @classmethod
    def setUpClass(cls):
        from firebase.appointment_service import AppointmentService
        from firebase.firebase_config import LocalRepository, reset_repository
        from firebase.patient_service import PatientService
        from firebase.seed_data import seed
        from voice_pipeline import VoicePipeline
        cls.repo = LocalRepository()
        reset_repository(cls.repo)
        seed(cls.repo, with_sample_appointment=True)
        cls.pipeline = VoicePipeline(repository=cls.repo, use_llm=False)
        # Loaded now, as the API does at start-up: loading lazily inside the
        # first turn would shift every timing the tests rely on.
        cls.pipeline.dialog.router.route("warm up")
        cls.patients = PatientService(cls.repo)
        cls.appointments = AppointmentService(cls.repo)

    def new_patient(self) -> str:
        VoiceCallTests.counter += 1
        return self.patients.create_patient(f"Voice Caller {self.counter}",
                                            f"+92300777{self.counter:04d}").data["patient_id"]

    def book_for(self, patient_id: str) -> str:
        """An appointment for this caller in the last free slot of the day,
        so the tests never compete for the same one."""
        _, iso = spoken_day()
        free = self.appointments.get_available_slots("D001", iso).data
        booked = self.appointments.book_appointment(patient_id, "D001", iso, free[-1])
        self.assertTrue(booked.ok, booked.message)
        return booked.data["appointment_id"]

    def make_call(self, scribe, fake_tts=None, patient_id=None, **options):
        from speech.call import VoiceCall
        VoiceCallTests.counter += 1
        fake_tts = fake_tts or FakeTTS()
        output, events = MemoryOutput(), []
        call = VoiceCall(self.pipeline, stt_for(scribe), tts_for(fake_tts), output,
                         f"CALL_T{self.counter:03d}", patient_id, "ulaw_8000",
                         on_event=events.append, **options)
        return call, output, events, fake_tts

    async def speak(self, call, ms=600, pause_ms=900):
        for frame in pack_frames(speech_audio(ms, pause_ms), ULAW.bytes_for(20)):
            await call.feed(frame)
            await asyncio.sleep(0)

    async def until(self, condition, timeout=20.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("timed out waiting for the call")
            await asyncio.sleep(0.02)

    async def converse(self, transcripts, patient_id=None, fake_tts=None, **options):
        async with FakeScribe(transcripts) as scribe:
            call, output, events, fake_tts = self.make_call(scribe, fake_tts, patient_id,
                                                            **options)
            self.assertTrue(await call.start())
            for number in range(1, len(transcripts) + 1):
                await self.speak(call)
                await self.until(lambda: len(call.turns) >= number or call.ended)
            await call.end()
            await self.until(lambda: scribe.open_connections == 0)
            return call, output, events, fake_tts, scribe

    def spoken_is_the_reply(self, call, fake_tts):
        """TTS said exactly the pipeline's replies, once each, in order."""
        self.assertEqual(fake_tts.texts(), [turn["response"] for turn in call.turns])

    # ---------------------------------------------------------- the flows
    async def test_booking_by_voice(self):
        day, iso = spoken_day()
        patient = self.new_patient()
        call, output, events, tts, scribe = await self.converse(
            [f"Mje Dr Ahmed se {day} appointment chahiye.", "4 baje.", "Haan kar dein."],
            patient)
        self.assertEqual([t["action"] for t in call.turns],
                         [Action.ASK_FOR_TIME, Action.ASK_CONFIRMATION,
                          Action.CREATE_APPOINTMENT])
        self.assertEqual(call.turns[0]["intent"], "book_appointment")
        booked = [a for a in self.repo._collection(Collections.APPOINTMENTS).values()
                  if a.get("patient_id") == patient]
        self.assertEqual([(a["doctor_id"], a["date"], a["time"], a["status"]) for a in booked],
                         [("D001", iso, "16:00", "confirmed")])
        self.assertIn(booked[0]["appointment_id"], call.turns[-1]["response"])
        self.spoken_is_the_reply(call, tts)
        self.assertEqual(scribe.connections, 1)               # one session for the call
        self.assertGreater(len(output.audio), 0)
        for turn in call.turns:
            self.assertIsNotNone(turn["latency"]["speech_end_to_agent_audio_ms"])

    async def test_booking_confirmed_in_urdu_script(self):
        """What speech-to-text wrote in a live call: the "yes" must book."""
        day, iso = spoken_day()
        # the latest free afternoon hour, said as Urdu words - away from the
        # 4 and 5 o'clock that other tests ask for
        hours = {16: "چار", 17: "پانچ", 18: "چھ", 19: "سات"}
        free = self.appointments.get_available_slots("D001", iso).data
        hour = max(int(t[:2]) for t in free if t.endswith(":00") and int(t[:2]) in hours)
        patient = self.new_patient()
        call, _, _, tts, _ = await self.converse(
            ["مجھے Dr. Ahmed سے کل appointment چاہیے۔" if day == "kal"
             else "مجھے Dr. Ahmed سے پرسوں appointment چاہیے۔",
             f"{hours[hour]} بجے۔", "ہاں، کر دیں۔"], patient)
        self.assertEqual(call.turns[-1]["action"], Action.CREATE_APPOINTMENT)
        booked = [a for a in self.repo._collection(Collections.APPOINTMENTS).values()
                  if a.get("patient_id") == patient]
        self.assertEqual([(a["time"], a["status"]) for a in booked],
                         [(f"{hour}:00", "confirmed")])
        self.spoken_is_the_reply(call, tts)

    async def test_cancellation_by_voice(self):
        patient = self.new_patient()
        appointment = self.book_for(patient)
        call, _, _, tts, _ = await self.converse(
            ["Meri appointment cancel kar dein.", "Haan ji."], patient)
        self.assertEqual([t["action"] for t in call.turns],
                         [Action.ASK_CONFIRMATION, Action.CANCEL_APPOINTMENT])
        self.assertEqual(self.repo.get(Collections.APPOINTMENTS, appointment)["status"],
                         "cancelled")
        self.spoken_is_the_reply(call, tts)

    async def test_rescheduling_by_voice(self):
        patient = self.new_patient()
        appointment = self.book_for(patient)
        call, _, _, tts, _ = await self.converse(
            ["Meri appointment Friday ko 5 baje kar dein.", "Haan."], patient)
        self.assertEqual(call.turns[0]["intent"], "reschedule_appointment")
        self.assertEqual(call.turns[1]["action"], Action.RESCHEDULE_APPOINTMENT)
        moved = self.repo.get(Collections.APPOINTMENTS, appointment)
        self.assertEqual((moved["time"], moved["status"]), ("17:00", "rescheduled"))
        self.assertEqual(datetime.fromisoformat(moved["date"]).weekday(), 4)   # Friday
        self.spoken_is_the_reply(call, tts)

    async def test_the_same_words_book_for_a_caller_with_no_appointment(self):
        call, _, _, _, _ = await self.converse(
            ["Meri appointment Friday ko 5 baje kar dein."], self.new_patient())
        self.assertEqual(call.turns[0]["intent"], "book_appointment")

    async def test_fee_comes_from_the_database(self):
        call, _, _, tts, _ = await self.converse(["Dr Ahmed ki fee kitni hai?"],
                                                 self.new_patient())
        fee = self.repo.get(Collections.DOCTORS, "D001")["fee"]
        self.assertEqual(call.turns[0]["action"], Action.PROVIDE_DOCTOR_FEE)
        self.assertIn(str(fee), tts.texts()[0])

    async def test_availability_comes_from_the_backend(self):
        day, iso = spoken_day()
        call, _, _, tts, _ = await self.converse([f"Dr Ahmed {day} available hain?"],
                                                 self.new_patient())
        self.assertEqual(call.turns[0]["action"], Action.PROVIDE_DOCTOR_AVAILABILITY)
        from ollama_judge.language import spoken_time
        free = self.appointments.get_available_slots("D001", iso).data
        for slot in free[:3]:
            self.assertIn(spoken_time(slot, "roman_urdu"), tts.texts()[0])

    async def test_critical_regression_mje_docter(self):
        for words in ("Mje docter ke pas jana h", "mujhe doctor ke paas jana hai",
                      "مجھے ڈاکٹر کے پاس جانا ہے"):
            call, _, _, _, _ = await self.converse([words], self.new_patient())
            self.assertEqual(call.turns[0]["intent"], "book_appointment", words)

    async def test_appointment_lookup(self):
        patient = self.new_patient()
        self.book_for(patient)
        call, _, _, _, _ = await self.converse(["Meri appointment kab hai?"], patient)
        self.assertEqual(call.turns[0]["action"], Action.CHECK_APPOINTMENT_STATUS)

    async def test_correction_and_intent_switch_keep_one_dialogue(self):
        day, _ = spoken_day()
        call, _, _, _, _ = await self.converse(
            [f"Mujhe Dr Ahmed se {day} appointment chahiye.",
             "Acha pehle ye batayein Dr Ahmed ki fee kitni hai?", "5 baje."],
            self.new_patient())
        self.assertEqual(call.turns[1]["action"], Action.PROVIDE_DOCTOR_FEE)
        # the booking survived the side question: the time goes into it
        self.assertEqual(call.turns[2]["action"], Action.ASK_CONFIRMATION)

    async def test_goodbye_ends_and_cleans_up(self):
        async with FakeScribe(["Shukriya, Allah Hafiz"]) as scribe:
            call, output, events, tts = self.make_call(scribe, patient_id=self.new_patient())
            await call.start()
            await self.speak(call)
            await self.until(lambda: call.ended)
            await self.until(lambda: scribe.open_connections == 0)
        self.assertEqual(call.end_reason, "goodbye")
        self.assertEqual(call.turns[0]["action"], Action.END_CONVERSATION)
        self.assertTrue(call.turns[0]["spoken"])              # farewell said, then hung up
        self.assertTrue(output.hung_up)
        self.assertIsNone(self.pipeline.dialog.sessions.get(call.session_id))
        self.assertEqual(events[-1]["type"], "call_ended")

    # ------------------------------------------------------ call mechanics
    async def test_barge_in_stops_the_agent(self):
        slow = FakeTTS(chunks=30, delay=0.03)                 # a long reply
        day, _ = spoken_day()
        async with FakeScribe([f"Dr Ahmed {day} available hain?", "5 baje kar dein"]) as scribe:
            call, output, events, tts = self.make_call(scribe, slow, self.new_patient())
            await call.start()
            await self.speak(call)
            await self.until(lambda: any(e["type"] == "tts_start" for e in events))
            await self.until(lambda: output.chunks >= 2)
            await self.speak(call)                            # the caller talks over it
            await self.until(lambda: len(call.turns) >= 2)
            await call.end()
        self.assertGreaterEqual(output.stops, 1)
        self.assertTrue(any(e["type"] == "barge_in" for e in events))
        self.assertFalse(call.turns[0]["spoken"])             # the first reply was cut
        self.assertLess(output.chunks, 60)

    async def test_a_late_partial_does_not_cut_the_agent_off(self):
        slow = FakeTTS(chunks=20, delay=0.03)
        # arrives while the reply is already playing
        async with FakeScribe(["Dr Ahmed ki fee kitni hai?"], late_partial=0.4) as scribe:
            call, output, events, tts = self.make_call(scribe, slow, self.new_patient())
            await call.start()
            await self.speak(call)
            await self.until(lambda: len(call.turns) >= 1)
            await call.end()
        self.assertEqual(output.stops, 0)
        self.assertFalse(any(e["type"] == "barge_in" for e in events))
        self.assertTrue(call.turns[0]["spoken"])

    async def test_noise_is_not_a_turn(self):
        async with FakeScribe([]) as scribe:                  # nothing recognisable
            call, output, events, tts = self.make_call(scribe, patient_id=self.new_patient())
            await call.start()
            await self.speak(call)
            await self.until(lambda: sum(e["type"] == "no_speech" for e in events) >= 1)
            self.assertEqual(tts.requests, [])                # not after one rustle
            await self.speak(call)
            await self.until(lambda: len(tts.requests) >= 1)
            await call.end()
        self.assertEqual(call.turns, [])                      # the dialogue never saw it
        self.assertEqual(tts.texts(), [R.say_again(None)])

    async def test_a_dropped_connection_reconnects_once_and_keeps_the_sentence(self):
        async with FakeScribe(["Dr Ahmed ki fee kitni hai?"],
                              script={1: "drop_after_audio"}) as scribe:
            call, _, events, _ = self.make_call(scribe, patient_id=self.new_patient())
            await call.start()
            await self.speak(call)
            await self.until(lambda: len(call.turns) >= 1)
            await call.end()
        self.assertEqual(scribe.connections, 2)
        self.assertEqual(call.turns[0]["action"], Action.PROVIDE_DOCTOR_FEE)
        self.assertEqual(sum(e["type"] == "stt_reconnected" for e in events), 1)

    async def pause_between_turns(self, keepalive_secs):
        """ElevenLabs closes a session after ~15 s without audio; the fake
        does it after 0.6 s. Two turns with a 1.5 s silence between them."""
        async with FakeScribe(["Dr Ahmed ki fee kitni hai?", "Clinic kahan hai?"],
                              idle_timeout=0.6) as scribe:
            call, _, events, _ = self.make_call(scribe, patient_id=self.new_patient(),
                                                keepalive_secs=keepalive_secs)
            await call.start()
            await self.speak(call)
            await self.until(lambda: len(call.turns) >= 1)
            await asyncio.sleep(1.5)                      # the caller thinks; nothing sent
            await self.speak(call)
            await self.until(lambda: len(call.turns) >= 2)
            await call.end()
        return call, scribe

    async def test_keep_alive_holds_the_session_through_a_pause(self):
        call, scribe = await self.pause_between_turns(keepalive_secs=0.2)
        self.assertEqual((scribe.connections, scribe.idle_closes), (1, 0))
        self.assertEqual([t["action"] for t in call.turns],
                         [Action.PROVIDE_DOCTOR_FEE, Action.PROVIDE_CLINIC_INFORMATION])

    async def test_a_session_closed_while_idle_loses_no_sentence(self):
        call, scribe = await self.pause_between_turns(keepalive_secs=60)
        self.assertGreaterEqual(scribe.idle_closes, 1)
        self.assertGreaterEqual(scribe.connections, 2)    # reconnected
        self.assertEqual(call.turns[1]["transcript"], "Clinic kahan hai?")   # nothing lost
        self.assertEqual(call.turns[1]["action"], Action.PROVIDE_CLINIC_INFORMATION)

    async def test_an_invalid_key_apologises_and_hangs_up(self):
        async with FakeScribe(api_key="the-real-one") as scribe:
            call, output, events, tts = self.make_call(scribe, patient_id=self.new_patient())
            self.assertFalse(await call.start())
        self.assertEqual(len(scribe.header_keys), 1)          # tried once, not in a loop
        self.assertEqual(tts.texts(), [R.technical_problem(None)])
        self.assertTrue(output.hung_up)
        self.assertEqual(call.end_reason, "stt_unavailable")

    async def test_speech_failure_leaves_the_reply_as_text(self):
        failing = FakeTTS(responses=[503, 503])
        call, output, events, _, _ = await self.converse(["Dr Ahmed ki fee kitni hai?"],
                                                         self.new_patient(), failing)
        self.assertFalse(call.turns[0]["spoken"])
        failed = [e for e in events if e["type"] == "tts_failed"]
        self.assertEqual(failed[0]["text"], call.turns[0]["response"])
        self.assertEqual(len(failing.requests), 2)            # retried once, then text

    async def test_the_key_never_appears_in_events_or_logs(self):
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Keep(level=logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            call, _, events, _, scribe = await self.converse(
                ["Dr Ahmed ki fee kitni hai?"], self.new_patient())
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        text = repr(events) + "\n".join(records) + "\n".join(scribe.paths)
        self.assertNotIn(API_KEY, text)
        self.assertTrue(records)

    async def test_silence_between_turns_is_not_paid_for(self):
        async with FakeScribe(["Dr Ahmed ki fee kitni hai?"]) as scribe:
            call, _, _, _ = self.make_call(scribe, patient_id=self.new_patient())
            await call.start()
            for frame in pack_frames(tone(ULAW, 5000), ULAW.bytes_for(20)):   # 5 s quiet
                await call.feed(frame)
            await self.speak(call)
            await self.until(lambda: len(call.turns) >= 1)
            await call.end()
        usage = call.usage()
        self.assertLess(usage["stt_audio_ms_sent"], 2000)
        self.assertGreaterEqual(usage["stt_audio_ms_held_back"], 4500)

    async def test_twenty_turns_one_session_no_leaks(self):
        before = {t for t in asyncio.all_tasks()}
        words = ["Dr Ahmed ki fee kitni hai?", "Clinic kahan hai?"] * 10
        call, output, events, tts, scribe = await self.converse(words, self.new_patient())
        self.assertEqual(len(call.turns), 20)
        self.assertEqual(scribe.connections, 1)
        self.assertEqual(len(tts.requests), 20)               # one synthesis per reply
        self.assertEqual(len(self.pipeline.dialog.sessions.get(call.session_id)
                             .history if self.pipeline.dialog.sessions.get(call.session_id)
                             else []), 0)
        await asyncio.sleep(0.05)
        leftover = [t for t in asyncio.all_tasks() - before
                    if t is not asyncio.current_task() and not t.done()
                    and (t.get_name().startswith(("stt-", "turns-", "tts-")))]
        self.assertEqual(leftover, [])
        self.assertEqual(scribe.open_connections, 0)


# --------------------------------------------------------------------------
# The WebSocket a telephony bridge connects to
# --------------------------------------------------------------------------
@unittest.skipUnless(MODEL_PRESENT, "needs the trained mBERT model (README step 4)")
class VoiceSocketTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from api import main
        from firebase.firebase_config import LocalRepository, reset_repository
        from firebase.seed_data import seed
        cls.repo = LocalRepository()
        reset_repository(cls.repo)
        seed(cls.repo, with_sample_appointment=True)
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        from api import voice_ws
        self.voice_ws = voice_ws
        self.saved = dict(voice_ws._services)

    def tearDown(self):
        self.voice_ws._services.clear()
        self.voice_ws._services.update(self.saved)

    def read_until(self, ws, kind, limit=400):
        seen, audio = [], 0
        for _ in range(limit):
            message = ws.receive()
            if message.get("bytes"):
                audio += len(message["bytes"])
                continue
            if message.get("text"):
                import json
                event = json.loads(message["text"])
                seen.append(event)
                if event.get("type") == kind:
                    return seen, audio
        self.fail(f"no {kind!r} event; saw {[e.get('type') for e in seen]}")

    def test_a_call_over_the_socket(self):
        fake_tts = FakeTTS()
        with ScribeThread(["Dr Ahmed ki fee kitni hai?"]) as scribe:
            self.voice_ws._services["stt"] = stt_for(scribe)
            self.voice_ws._services["tts"] = tts_for(fake_tts)
            with self.client.websocket_connect(
                    "/voice/call?session_id=WS_T1&patient_id=P001&format=ulaw_8000") as ws:
                first = ws.receive_json()
                self.assertEqual((first["type"], first["session_id"]), ("session", "WS_T1"))
                _, greeting_audio = self.read_until(ws, "tts_done")
                self.assertGreater(greeting_audio, 0)        # the greeting was spoken
                for frame in pack_frames(speech_audio(), ULAW.bytes_for(20)):
                    ws.send_bytes(frame)
                events, reply_audio = self.read_until(ws, "latency")
                turn = next(e for e in events if e["type"] == "turn")
                self.assertEqual(turn["action"], Action.PROVIDE_DOCTOR_FEE)
                self.assertGreater(reply_audio, 0)
                ws.send_text('{"type": "end"}')
                self.read_until(ws, "call_ended")
        self.assertEqual(fake_tts.texts()[-1], turn["response"])

    def test_the_token_is_required_when_configured(self):
        from unittest import mock
        from starlette.websockets import WebSocketDisconnect
        with mock.patch("config.VOICE_GATEWAY_TOKEN", "bridge-secret"):
            with self.assertRaises(WebSocketDisconnect) as closed:
                with self.client.websocket_connect("/voice/call?token=wrong") as ws:
                    ws.receive_json()
            self.assertEqual(closed.exception.code, 4401)

    def test_not_configured_says_so_without_details(self):
        self.voice_ws._services["stt"] = ElevenLabsSTT(api_key="")
        self.voice_ws._services["tts"] = ElevenLabsTTS(api_key="", voice_id="")
        with self.client.websocket_connect("/voice/call") as ws:
            event = ws.receive_json()
        self.assertEqual(event["code"], R.NOT_CONFIGURED)
        self.assertIn("ELEVENLABS_API_KEY", event["message"])   # the variable's name only


# --------------------------------------------------------------------------
# What callers actually say (docs/VOICE.md, "Language checks")
# --------------------------------------------------------------------------
@unittest.skipUnless(MODEL_PRESENT, "needs the trained mBERT model (README step 4)")
class KnownConfusionTests(unittest.TestCase):
    """Three confident mBERT mistakes found with the voice test sentences,
    corrected narrowly - and nothing else changed."""

    @classmethod
    def setUpClass(cls):
        from dialog_manager.intent_router import IntentRouter
        cls.router = IntentRouter()

    def intent(self, text):
        return self.router.route(text).intent

    def test_farewells_end_the_call_whatever_the_capitals(self):
        for text in ("Allah Hafiz", "ALLAH HAFIZ", "allah hafiz.", "Shukriya, Allah Hafiz",
                     "Khuda Hafiz", "Thank you, bye", "اللہ حافظ"):
            self.assertEqual(self.intent(text), "goodbye", text)
        self.assertEqual(self.intent("Shukriya"), "thanks")    # thanks alone is not goodbye

    def test_availability_asked_in_urdu_script(self):
        for text in ("ڈاکٹر احمد کل دستیاب ہیں؟", "ڈاکٹر احمد کل اویلیبل ہیں؟",
                     "Dr Ahmed kal available hain?"):
            self.assertEqual(self.intent(text), "doctor_availability", text)
        # "when is the doctor available" is about hours, as the training data says
        self.assertEqual(self.intent("ڈاکٹر کب دستیاب ہے؟"), "clinic_information")

    def test_yes_and_no_with_the_punctuation_speech_to_text_writes(self):
        from dialog_manager.validators import detect_yes_no
        for text in ("ہاں، کر دیں۔", "جی، کر دیں۔", "ہاں جی۔", "Haan, kar dein.", "Yeah."):
            self.assertEqual(detect_yes_no(text), "yes", text)
        for text in ("نہیں، رہنے دیں۔", "جی نہیں؟", "Nahi, rehne do."):
            self.assertEqual(detect_yes_no(text), "no", text)

    def test_no_labelled_example_changes(self):
        import csv
        from unittest import mock
        from dialog_manager import intent_router
        rows = [r for r in csv.DictReader(open(
            INTENT_MODEL_DIR.parents[1] / "data" / "splits.csv", encoding="utf-8"))
            if r["split"] == "test"]
        with_rules = [self.router.route(r["text"]).intent for r in rows]
        with mock.patch.object(intent_router, "correct_known_confusion",
                               lambda text, result: None):
            without = [self.router.route(r["text"]).intent for r in rows]
        changed = [r["text"] for r, a, b in zip(rows, with_rules, without) if a != b]
        self.assertEqual(changed, [])
