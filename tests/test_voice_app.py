"""
Microphone voice mode (voice_app.py) - offline tests.

The sound card is FakeSoundDevice (its callbacks run on their own threads at
real-time pace, as PortAudio's do), ElevenLabs is FakeScribe / FakeTTS and
Ollama is FakeOllama. mBERT, the Dialog Manager, the Appointment Backend, the
validator and the call orchestration are the real ones.

What these tests cannot show: that a real person's voice through a real
microphone is transcribed well, or that the reply sounds right. That needs
`python voice_app.py` and a person (docs/VOICE.md, section 10).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import unittest
from array import array
from types import SimpleNamespace

from config import Action, Collections, INTENT_MODEL_DIR
from ollama_judge.judge import OllamaJudge
from ollama_judge.response_validator import validate_rewording
from speech import microphone as M
from speech import results as R
from speech.audio import SpeechGate, audio_format, pack_frames, rms, to_pcm16, tone
from speech.microphone import (MicrophoneError, MicrophoneInput, SpeakerError, _Converter,
                               list_devices, resolve_device)
from speech.speaker import SpeakerOutput
from speech.stt import ElevenLabsSTT
from tests.speech_fakes import (API_KEY, FakeOllama, FakeScribe, FakeSoundDevice, FakeTTS,
                                MemoryOutput)
from tests.test_speech import spoken_day, stt_for, tts_for

PCM16 = audio_format("pcm_16000")
PCM22 = audio_format("pcm_22050")
PCM48 = audio_format("pcm_48000")
MODEL_PRESENT = (INTENT_MODEL_DIR / "config.json").exists()


def voice(ms: int = 600, pause_ms: int = 900) -> bytes:
    """Someone speaking into the microphone (a loud tone), then a pause."""
    return tone(PCM16, ms, amplitude=0.3) + tone(PCM16, pause_ms)


class Quiet(unittest.TestCase):
    """The validator logs every rejection as a warning; not needed here."""

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.WARNING)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
class DeviceTests(unittest.TestCase):

    def card(self, **options):
        return FakeSoundDevice(inputs=("Microphone (Realtek)", "Microphone (FANTECH)"),
                               outputs=("Speakers (FANTECH)",), **options)

    def test_empty_means_the_system_default_and_names_or_numbers_choose(self):
        card = self.card()
        self.assertIsNone(resolve_device("", "input", card))
        self.assertEqual(resolve_device("fantech", "input", card), 1)
        self.assertEqual(resolve_device("1", "input", card), 1)
        self.assertEqual(resolve_device("FANTECH", "output", card), 2)

    def test_an_unknown_device_is_named_as_not_found(self):
        card = self.card()
        for setting in ("Logitech", "7", "2"):         # 2 is a speaker, not a microphone
            with self.assertRaises(MicrophoneError) as caught:
                resolve_device(setting, "input", card)
            self.assertEqual(caught.exception.code, M.NOT_FOUND)
            self.assertIn("--list-devices", caught.exception.message)

    def test_no_microphone_at_all(self):
        card = FakeSoundDevice(inputs=(), outputs=("Speaker",), default=(-1, None))
        with self.assertRaises(MicrophoneError) as caught:
            resolve_device("", "input", card)
        self.assertEqual(caught.exception.code, M.NO_DEVICE)
        self.assertIn("No microphone found", caught.exception.message)

    def test_no_speaker_is_reported_as_a_speaker_problem(self):
        card = FakeSoundDevice(inputs=("Microphone",), outputs=(), default=(0, None))
        with self.assertRaises(SpeakerError) as caught:
            resolve_device("", "output", card)
        self.assertIn("No speaker", caught.exception.message)

    def test_the_listing_shows_both_kinds_and_the_defaults(self):
        rows = list_devices(self.card())
        self.assertEqual([r["name"] for r in rows["inputs"]],
                         ["Microphone (Realtek)", "Microphone (FANTECH)"])
        self.assertEqual([r["name"] for r in rows["outputs"]], ["Speakers (FANTECH)"])
        self.assertEqual((rows["default_input"], rows["default_output"]), (0, 2))


# --------------------------------------------------------------------------
# Microphone
# --------------------------------------------------------------------------
class MicrophoneTests(unittest.IsolatedAsyncioTestCase):

    async def test_records_16khz_mono_in_20ms_pieces_without_conversion(self):
        card = FakeSoundDevice()
        mic = MicrophoneInput(PCM16, None, sd=card)
        mic.start()
        try:
            chunks = [await mic.read() for _ in range(5)]
        finally:
            mic.close()
        self.assertEqual({len(c) for c in chunks}, {PCM16.bytes_for(20)})
        stream = card.streams[0]
        self.assertEqual((stream.samplerate, stream.channels), (16000, 1))
        self.assertIsNone(mic._convert)
        self.assertTrue(stream.closed)

    async def test_a_device_refusing_16khz_mono_is_recorded_natively_and_converted(self):
        card = FakeSoundDevice(native_rate=48000, refuse={(16000, 1), (48000, 1)})
        mono = tone(PCM48, 400, amplitude=0.3, frequency=440.0)
        samples = array("h")
        samples.frombytes(mono)
        stereo = array("h", (value for sample in samples for value in (sample, sample)))
        card.say(stereo.tobytes())
        mic = MicrophoneInput(PCM16, None, sd=card)
        mic.start()
        try:
            audio = b"".join([await mic.read() for _ in range(10)])      # 200 ms
        finally:
            mic.close()
        self.assertEqual((mic.device_rate, mic.device_channels), (48000, 2))
        self.assertAlmostEqual(len(audio) / 2, 16000 * 0.2, delta=4)
        self.assertAlmostEqual(rms(audio, PCM16), rms(mono, PCM48), delta=0.02)

    def test_conversion_joins_blocks_seamlessly(self):
        audio = tone(PCM48, 200, amplitude=0.3, frequency=300.0)
        whole = _Converter(48000, 16000, 1)(audio)
        converter = _Converter(48000, 16000, 1)
        pieces = b"".join(converter(block) for block in pack_frames(audio, PCM48.bytes_for(20)))
        self.assertAlmostEqual(len(pieces), len(whole), delta=4)
        a, b = array("h"), array("h")
        a.frombytes(whole[:len(pieces)])
        b.frombytes(pieces[:len(whole)])
        self.assertLess(max(abs(x - y) for x, y in zip(a, b)), 50)

    async def test_a_busy_device_is_reported_in_words(self):
        card = FakeSoundDevice(open_error="Device unavailable [PaErrorCode -9985]")
        with self.assertRaises(MicrophoneError) as caught:
            MicrophoneInput(PCM16, None, sd=card).start()
        self.assertEqual(caught.exception.code, M.BUSY)
        self.assertIn("in use by another program", caught.exception.message)

    async def test_an_unplugged_microphone_is_noticed(self):
        card = FakeSoundDevice()
        mic = MicrophoneInput(PCM16, None, sd=card, stall_secs=0.3)
        mic.start()
        await mic.read()
        card.frozen = True
        mic.drain()
        with self.assertRaises(MicrophoneError) as caught:
            for _ in range(100):
                await mic.read()
        mic.close()
        self.assertEqual(caught.exception.code, M.DISCONNECTED)

    async def test_a_muted_or_blocked_microphone_is_noticed(self):
        card = FakeSoundDevice()
        card.muted = True
        mic = MicrophoneInput(PCM16, None, sd=card, blocked_secs=0.3)
        mic.start()
        with self.assertRaises(MicrophoneError) as caught:
            for _ in range(100):
                await mic.read()
        mic.close()
        self.assertEqual(caught.exception.code, M.BLOCKED)
        self.assertIn("Privacy", caught.exception.message)

    def test_push_to_talk_sends_even_quiet_speech(self):
        gate = SpeechGate(PCM16)
        self.assertEqual(gate.push(tone(PCM16, 100, amplitude=0.005)), [])   # too quiet
        gate.start_utterance()
        self.assertTrue(gate.in_utterance)
        self.assertTrue(gate.push(tone(PCM16, 100, amplitude=0.005)))


# --------------------------------------------------------------------------
# Speaker
# --------------------------------------------------------------------------
class SpeakerTests(unittest.IsolatedAsyncioTestCase):

    async def test_the_reply_is_played_in_order_as_it_streams_in(self):
        card = FakeSoundDevice()
        speaker = SpeakerOutput(PCM22, None, sd=card)
        speaker.start()
        audio = tone(PCM22, 300, amplitude=0.3)
        speaker.new_reply()
        await speaker.play(audio[:1001])                # split inside a sample
        await speaker.play(audio[1001:])
        self.assertTrue(speaker.busy)
        await speaker.drain()
        self.assertFalse(speaker.busy)
        speaker.close()
        self.assertIn(audio[2:-2], bytes(card.played))
        self.assertGreaterEqual(speaker.reply_first_played_at, speaker.reply_first_chunk_at)
        self.assertEqual(card.streams[0].samplerate, 22050)

    async def test_stop_drops_what_has_not_been_played(self):
        card = FakeSoundDevice()
        speaker = SpeakerOutput(PCM22, None, sd=card)
        speaker.start()
        await speaker.play(tone(PCM22, 2000, amplitude=0.3))
        await asyncio.sleep(0.1)
        await speaker.stop()
        await asyncio.sleep(0.2)
        speaker.close()
        self.assertFalse(speaker.busy)
        self.assertLess(speaker.played_bytes, PCM22.bytes_for(600))

    async def test_phone_audio_is_converted_for_the_sound_card(self):
        ulaw = audio_format("ulaw_8000")
        card = FakeSoundDevice()
        speaker = SpeakerOutput(ulaw, None, sd=card)
        speaker.start()
        audio = tone(ulaw, 200, amplitude=0.3)
        await speaker.play(audio)
        await speaker.drain()
        speaker.close()
        self.assertEqual(card.streams[0].samplerate, 8000)
        self.assertIn(to_pcm16(audio, ulaw)[2:-2], bytes(card.played))

    async def test_a_speaker_that_stops_playing_does_not_mute_the_microphone_for_ever(self):
        card = FakeSoundDevice()
        speaker = SpeakerOutput(PCM22, None, sd=card)
        speaker.start()
        await asyncio.sleep(0.05)
        card.frozen = True                              # unplugged
        await speaker.play(tone(PCM22, 500, amplitude=0.3))
        self.assertTrue(speaker.busy)
        await asyncio.sleep(1.2)
        self.assertFalse(speaker.busy)
        self.assertTrue(speaker.stalled)
        speaker.close()

    def test_a_speaker_that_cannot_open_says_so(self):
        card = FakeSoundDevice(open_error="Device unavailable [PaErrorCode -9985]")
        with self.assertRaises(SpeakerError) as caught:
            SpeakerOutput(PCM22, None, sd=card).start()
        self.assertIn("speaker is in use", caught.exception.message)


# --------------------------------------------------------------------------
# Ollama on every turn: the validator for turns with no database result
# --------------------------------------------------------------------------
class RewordingValidatorTests(Quiet):
    REF = "Ji. Dr Ahmed Khan ke paas kal shaam 4 baje ka slot khali hai. Book kar doon?"

    def problems(self, text, reference=None, language="roman_urdu"):
        return validate_rewording(text, reference or self.REF, language).problems

    def test_the_same_facts_in_other_words_pass(self):
        self.assertEqual(self.problems(
            "Ji, Dr Ahmed Khan ke paas kal shaam 4 baje waqt khali hai. Kya main book kar doon?"), [])
        self.assertEqual(self.problems("جی، ڈاکٹر احمد کی فیس 2000 روپے ہے۔",
                                       "ڈاکٹر احمد کی فیس 2000 روپے ہے۔", "urdu"), [])

    def test_every_changed_fact_is_caught(self):
        cases = {
            "Ji, Dr Ahmed Khan ke paas kal shaam 5 baje slot khali hai. Book kar doon?": "time",
            "Ji, Dr Ahmed Khan ke paas parson shaam 4 baje slot khali hai. Book kar doon?": "day",
            "Ji, Dr Ali ke paas kal shaam 4 baje slot khali hai. Book kar doon?": "doctor",
            "Ji, Dr Ahmed Khan ke paas kal shaam 4 baje slot khali nahi hai. Book kar doon?":
                "negation",
            "Ji, Dr Ahmed Khan ke paas kal shaam chaar baje slot khali hai. Book kar doon?":
                "leaves out number",
            "Ji, Dr Ahmed Khan ke paas kal shaam 4 baje slot khali hai.": "question",
            "Ji, Dr Ahmed Khan ke saath kal shaam 4 baje appointment book ho gayi hai. Theek?":
                "claims",
            "Dr Ahmed Khan has a slot tomorrow at 4 PM. Shall I book it?": "English",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertTrue(any(expected in problem for problem in self.problems(text)),
                                self.problems(text))

    def test_invented_numbers_and_new_questions_are_caught(self):
        reference = "Dr Ahmed Khan ki consultation fee 2000 rupay hai."
        self.assertTrue(self.problems("Dr Ahmed Khan ki fee 2500 rupay hai.", reference))
        self.assertTrue(self.problems(
            "Dr Ahmed Khan ki fee 2000 rupay hai. Appointment book karni hai?", reference))

    def test_turning_the_sentence_onto_the_receptionist_is_caught(self):
        # measured with llama3.2: "take care of yourself" -> "I take care of myself"
        self.assertTrue(self.problems("Khuda hafiz, main apna khayal rakhti hoon.",
                                      "Khuda hafiz, apna khayal rakhiye ga."))

    def test_programme_internals_and_rambling_are_caught(self):
        self.assertTrue(self.problems("intent=book_appointment. Book kar doon?",
                                      "Book kar doon?"))
        self.assertTrue(self.problems("Ji " * 60 + "kis din?", "Kis din?"))


class PhraseJudgeTests(Quiet):

    def test_an_approved_wording_is_what_is_said(self):
        service = FakeOllama()
        verdict = OllamaJudge(service=service).phrase("Kis din aana chahenge?", "Dr Ahmed",
                                                      "roman_urdu")
        self.assertEqual((verdict.source, verdict.response),
                         ("ollama", "Ji bilkul. Kis din aana chahenge?"))

    def test_a_rejected_wording_is_replaced_by_the_reference_with_the_reason(self):
        service = FakeOllama(reword=lambda ref: "Dr Ali kal 5 baje free hain. Book kar doon?")
        verdict = OllamaJudge(service=service).phrase("Kis din aana chahenge?", "Dr Ahmed",
                                                      "roman_urdu")
        self.assertEqual((verdict.source, verdict.response),
                         ("fallback", "Kis din aana chahenge?"))
        self.assertTrue(verdict.validation_problems)
        self.assertEqual(verdict.llm_raw, "Dr Ali kal 5 baje free hain. Book kar doon?")

    def test_no_model_means_the_reference(self):
        verdict = OllamaJudge(service=FakeOllama(available=False)).phrase(
            "Kis din?", "Dr Ahmed", "roman_urdu")
        self.assertEqual((verdict.source, verdict.response), ("fallback", "Kis din?"))

    def test_the_prompt_asks_for_the_reply_language_not_the_callers(self):
        service = FakeOllama()
        OllamaJudge(service=service).phrase("Kis din?", "I want to see Dr Ahmed", "roman_urdu")
        self.assertIn("Roman Urdu", service.prompts[-1])
        self.assertNotIn("I want to see", service.prompts[-1])


class CommitStrategyTests(unittest.TestCase):

    def test_push_to_talk_commits_only_when_told(self):
        url = ElevenLabsSTT(api_key="k", commit_strategy="manual").realtime_url(PCM16)
        self.assertIn("commit_strategy=manual", url)
        self.assertNotIn("vad_silence_threshold_secs", url)

    def test_voice_activity_is_the_default(self):
        url = ElevenLabsSTT(api_key="k").realtime_url(PCM16)
        self.assertIn("commit_strategy=vad", url)
        self.assertIn("vad_silence_threshold_secs", url)

    def test_an_unknown_strategy_is_refused(self):
        with self.assertRaises(ValueError):
            ElevenLabsSTT(api_key="k", commit_strategy="sometimes")


# --------------------------------------------------------------------------
# The pipeline and the call, with the real mBERT model
# --------------------------------------------------------------------------
class BusyOutput(MemoryOutput):
    """An output device that says when it is still playing."""
    busy = False


@unittest.skipUnless(MODEL_PRESENT, "needs the trained mBERT model (README step 4)")
class MicrophoneConversationTests(unittest.IsolatedAsyncioTestCase):
    counter = 0

    @classmethod
    def setUpClass(cls):
        from firebase.appointment_service import AppointmentService
        from firebase.firebase_config import LocalRepository, reset_repository
        from firebase.patient_service import PatientService
        from firebase.seed_data import seed
        from voice_pipeline import VoicePipeline
        logging.disable(logging.WARNING)
        cls.repo = LocalRepository()
        reset_repository(cls.repo)
        seed(cls.repo, with_sample_appointment=False)
        cls.ollama = FakeOllama()
        cls.pipeline = VoicePipeline(repository=cls.repo, use_llm=True,
                                     judge=OllamaJudge(service=cls.ollama),
                                     response_language="roman_urdu", phrase_every_turn=True)
        cls.pipeline.dialog.router.route("warm up")
        cls.patients = PatientService(cls.repo)
        cls.appointments = AppointmentService(cls.repo)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def setUp(self):
        self.ollama.reword = FakeOllama().reword
        self.ollama.available, self.ollama.crash = True, False
        self.ollama.prompts.clear()

    def new_patient(self) -> str:
        MicrophoneConversationTests.counter += 1
        return self.patients.create_patient(f"Mic Caller {self.counter}",
                                            f"+92300888{self.counter:04d}").data["patient_id"]

    def session(self) -> str:
        MicrophoneConversationTests.counter += 1
        return f"MIC_T{self.counter:03d}"

    def make_call(self, scribe, fake_tts=None, patient=None, output=None, stt_options=None,
                  **options):
        from speech.call import VoiceCall
        fake_tts = fake_tts or FakeTTS()
        output = output or MemoryOutput()
        events: list[dict] = []
        stt = stt_for(scribe, audio_format_name="pcm_16000", **(stt_options or {}))
        call = VoiceCall(self.pipeline, stt, tts_for(fake_tts, output_format="pcm_22050"), output,
                         self.session(), patient, "pcm_16000", on_event=events.append, **options)
        return call, output, events, fake_tts

    async def speak(self, call, audio=None):
        for frame in pack_frames(audio or voice(), PCM16.bytes_for(20)):
            await call.feed(frame)
            await asyncio.sleep(0)

    async def until(self, condition, timeout=20.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("timed out")
            await asyncio.sleep(0.02)

    # ------------------------------------------------------- the pipeline
    def test_every_reply_is_worded_by_ollama_and_checked(self):
        result = self.pipeline.process(self.session(), "Mje docter ke pas jana h", self.new_patient())
        self.assertEqual((result["intent"], result["action"]),
                         ("book_appointment", Action.ASK_FOR_DOCTOR))
        self.assertEqual(result["judge"]["source"], "ollama")
        self.assertEqual(result["response"], "Ji bilkul. " + result["deterministic_response"])
        timings = result["timings"]
        self.assertGreater(timings["mbert_ms"], 0)
        self.assertIsNotNone(timings["llm_ms"])
        self.assertGreaterEqual(timings["database_ms"], 0)

    def test_a_wording_that_changes_a_fact_is_never_spoken(self):
        self.ollama.reword = lambda ref: "Dr Ali ke paas kal 5 baje waqt hai. Book kar doon?"
        result = self.pipeline.process(self.session(), "Mje docter ke pas jana h", self.new_patient())
        self.assertEqual(result["response"], result["deterministic_response"])
        self.assertEqual(result["judge"]["source"], "fallback")
        self.assertTrue(result["judge"]["validation_problems"])

    def test_ollama_down_or_crashing_still_gets_an_answer(self):
        for broken in ({"available": False}, {"crash": True}):
            with self.subTest(**broken):
                self.setUp()
                for name, value in broken.items():
                    setattr(self.ollama, name, value)
                result = self.pipeline.process(self.session(), "Dr Ahmed ki fee kitni hai?",
                                               self.new_patient())
                self.assertEqual(result["response"], result["deterministic_response"])
                self.assertIn("2000", result["response"])

    def test_with_the_switch_off_slot_questions_do_not_go_to_ollama(self):
        self.pipeline.phrase_every_turn = False
        try:
            result = self.pipeline.process(self.session(), "Mje docter ke pas jana h",
                                           self.new_patient())
        finally:
            self.pipeline.phrase_every_turn = True
        self.assertEqual(self.ollama.prompts, [])
        self.assertIsNone(result["timings"]["llm_ms"])

    # ----------------------------------------------------------- the call
    async def test_the_critical_sentence_is_spoken_as_ollama_worded_it(self):
        """Spec: "Mje docter ke pas jana h" -> STT -> mBERT book_appointment ->
        Dialog Manager -> Ollama -> validator -> TTS -> speaker."""
        async with FakeScribe(["Mje docter ke pas jana h"]) as scribe:
            call, output, _, tts = self.make_call(scribe, patient=self.new_patient())
            self.assertTrue(await call.start())
            await self.speak(call)
            await self.until(lambda: call.turns)
            await call.end()
        turn = call.turns[0]
        self.assertEqual((turn["intent"], turn["action"], turn["response_source"]),
                         ("book_appointment", Action.ASK_FOR_DOCTOR, "ollama"))
        self.assertTrue(turn["response"].startswith("Ji bilkul."))
        self.assertEqual(tts.texts(), [turn["response"]])     # exactly the approved text
        self.assertTrue(turn["spoken"])
        self.assertGreater(len(output.audio), 0)

    async def test_the_microphone_is_deaf_while_the_agent_can_be_heard(self):
        output = BusyOutput()
        output.busy = True
        async with FakeScribe(["Dr Ahmed ki fee kitni hai?"]) as scribe:
            call, _, _, _ = self.make_call(scribe, patient=self.new_patient(), output=output,
                                           listen_while_speaking=False, echo_tail_ms=200)
            self.assertTrue(await call.start())
            await self.speak(call)                   # the agent's own voice, heard back
            await asyncio.sleep(1.0)
            self.assertEqual((call.turns, scribe.commits_sent), ([], 0))
            self.assertFalse(call.gate.in_utterance)
            output.busy = False
            await self.speak(call, tone(PCM16, 300))  # the echo tail passes
            await self.speak(call)                   # now the caller
            await self.until(lambda: call.turns)
            await call.end()
        self.assertIn("2000", call.turns[0]["response"])

    async def test_push_to_talk_ends_the_sentence_on_the_key(self):
        async with FakeScribe(["Dr Ahmed ki fee kitni hai?"]) as scribe:
            call, _, _, _ = self.make_call(scribe, patient=self.new_patient(),
                                           stt_options={"commit_strategy": "manual"},
                                           max_silence_ms=600_000)
            self.assertTrue(await call.start())
            self.assertFalse(await call.end_utterance())       # nothing was said
            call.begin_utterance()
            await self.speak(call, tone(PCM16, 600, amplitude=0.3))   # no pause after it
            released = call.clock()
            self.assertTrue(await call.end_utterance(released))
            await self.until(lambda: call.turns)
            await call.end()
        self.assertEqual(scribe.commits_sent, 1)
        self.assertIn("commit_strategy=manual", scribe.paths[0])
        self.assertIn("2000", call.turns[0]["response"])
        self.assertIsNotNone(call.turns[0]["latency"]["speech_end_to_transcript_ms"])

    async def test_the_key_stops_the_agent_mid_sentence(self):
        async with FakeScribe([]) as scribe:
            call, output, events, _ = self.make_call(
                scribe, FakeTTS(chunks=40, delay=0.05), greeting=R.GREETING)
            self.assertTrue(await call.start())
            await self.until(lambda: output.chunks >= 2)
            await call.interrupt()
            await asyncio.sleep(0.2)
            await call.end()
        self.assertEqual(output.stops, 1)
        self.assertLess(output.chunks, 40)
        self.assertIn("barge_in", [e["type"] for e in events])

    async def test_a_booking_is_made_once_even_when_ollama_and_tts_fail(self):
        """Spec: separate the business operation from delivering the reply."""
        day, iso = spoken_day()
        patient = self.new_patient()
        self.ollama.crash = True
        failing = FakeTTS(responses=[503] * 20)
        async with FakeScribe([f"Mje Dr Ahmed se {day} appointment chahiye.", "4 baje.",
                               "Haan kar dein."]) as scribe:
            call, _, _, _ = self.make_call(scribe, failing, patient)
            self.assertTrue(await call.start())
            for number in (1, 2, 3):
                await self.speak(call)
                await self.until(lambda: len(call.turns) >= number)
            await call.end()
        booked = [a for a in self.repo._collection(Collections.APPOINTMENTS).values()
                  if a.get("patient_id") == patient]
        self.assertEqual(len(booked), 1)
        self.assertEqual(call.turns[-1]["action"], Action.CREATE_APPOINTMENT)
        self.assertFalse(any(turn["spoken"] for turn in call.turns))
        self.assertEqual(len(failing.requests), 6)      # 3 replies, each tried twice


# --------------------------------------------------------------------------
# The application: microphone -> ... -> speaker, as `python voice_app.py` runs
# --------------------------------------------------------------------------
@unittest.skipUnless(MODEL_PRESENT, "needs the trained mBERT model (README step 4)")
class VoiceAppTests(unittest.IsolatedAsyncioTestCase):

    async def until(self, condition, timeout=20.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("timed out")
            await asyncio.sleep(0.02)

    async def test_a_whole_conversation_through_microphone_and_speaker(self):
        import voice_app
        from firebase.firebase_config import LocalRepository, reset_repository
        from firebase.seed_data import seed
        from voice_pipeline import VoicePipeline
        repo = LocalRepository()
        reset_repository(repo)
        seed(repo, with_sample_appointment=False)
        pipeline = VoicePipeline(repository=repo, use_llm=True,
                                 judge=OllamaJudge(service=FakeOllama()),
                                 response_language="roman_urdu", phrase_every_turn=True)
        pipeline.dialog.router.route("warm up")
        card = FakeSoundDevice()
        fake_tts = FakeTTS()
        args = SimpleNamespace(ptt=False, mic=None, speaker=None, patient="P001", local=False,
                               no_llm=False, debug=True, report=None)
        printed = io.StringIO()
        logging.disable(logging.WARNING)
        try:
            async with FakeScribe(["Mje docter ke pas jana h", "Shukriya, Allah Hafiz"]) as scribe:
                stt = stt_for(scribe, audio_format_name="pcm_16000")
                tts = tts_for(fake_tts, output_format="pcm_22050")
                with contextlib.redirect_stdout(printed):
                    app = asyncio.create_task(voice_app.converse(
                        args, sd=card, stt=stt, tts=tts, pipeline=pipeline, read_keys=False))
                    for replies in (1, 2):                  # after the greeting, then the reply
                        await self.until(lambda: len(fake_tts.requests) >= replies)
                        await asyncio.sleep(0.8)
                        card.say(voice(700, 1200))
                    code = await asyncio.wait_for(app, 30)
        finally:
            logging.disable(logging.NOTSET)
        text = printed.getvalue()
        spoken = fake_tts.texts()
        self.assertEqual(code, 0)
        self.assertEqual(spoken[0], R.GREETING)
        self.assertTrue(spoken[1].startswith("Ji bilkul."))           # Ollama's, approved
        self.assertEqual(len(spoken), 3)                               # + the farewell
        self.assertIn("PATIENT : Mje docter ke pas jana h", text)
        self.assertIn("book_appointment", text)
        self.assertIn("DIALOG  : ask_for_doctor", text)
        self.assertIn("OLLAMA  : worded the reply, validator approved", text)
        self.assertIn(f"AGENT   : {spoken[1]}", text)
        self.assertIn("you stopped -> agent audio", text)
        self.assertIn("conversation ended (goodbye), 2 turn(s)", text)
        self.assertNotIn(API_KEY, text)
        self.assertTrue(bytes(card.played).strip(b"\x00"))            # the speaker played it
        self.assertTrue(all(stream.closed for stream in card.streams))
        self.assertEqual(scribe.connections, 1)


if __name__ == "__main__":
    unittest.main()
