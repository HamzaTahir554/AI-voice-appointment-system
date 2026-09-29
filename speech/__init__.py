"""
The voice of the assistant: ElevenLabs speech-to-text and text-to-speech
around the existing text pipeline (voice_pipeline.py).

    audio.py     formats (8 kHz mu-law, 16-bit PCM), the speech gate
    stt.py       Scribe realtime (calls) and batch (recordings)
    tts.py       streaming synthesis, the fixed-phrase cache
    keyterms.py  doctor names and specialisations from the live data
    call.py      one call: audio in, committed transcript, pipeline, audio out
    results.py   result types, error codes, what the caller hears on failure

Only stt.py and tts.py know ElevenLabs exists. See docs/VOICE.md.
"""
