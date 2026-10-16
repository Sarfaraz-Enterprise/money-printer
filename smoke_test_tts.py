"""Live ElevenLabs smoke test. Uses API credits; never prints credentials."""

import os
import tempfile
from pathlib import Path

from app.services import voice


def main() -> int:
    if not voice.get_elevenlabs_api_key():
        print("FAIL: ElevenLabs API key is not configured.")
        return 1

    env_default = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if not env_default:
        print("FAIL: ELEVENLABS_VOICE_ID is required for the environment fallback test.")
        return 1

    catalog_voice = voice.get_elevenlabs_catalog_voices()[0]
    cases = (
        ("Catalog voice (Adam)", catalog_voice, catalog_voice.split(":", 2)[1]),
        ("Empty voice ID (environment fallback)", "elevenlabs:", env_default),
    )
    failures = 0
    with tempfile.TemporaryDirectory(prefix="mpt-tts-smoke-") as directory:
        for index, (label, voice_name, expected_id) in enumerate(cases):
            if voice.resolve_elevenlabs_voice_id(voice_name) != expected_id:
                print(f"FAIL: {label}: resolved voice ID does not match.")
                failures += 1
                continue

            audio_file = Path(directory) / f"preview-{index}.mp3"
            timeline = voice.tts(
                text="This is a short voice preview test.",
                voice_name=voice_name,
                voice_rate=1.0,
                voice_file=str(audio_file),
            )
            audio_bytes = audio_file.read_bytes() if audio_file.is_file() else b""
            if timeline is None or not audio_bytes:
                print(f"FAIL: {label}: TTS did not return non-empty preview audio.")
                failures += 1
                continue

            duration = voice.get_audio_duration(str(audio_file))
            if duration <= 0:
                print(f"FAIL: {label}: audio has no valid playback duration.")
                failures += 1
                continue

            print(f"PASS: {label}: {len(audio_bytes)} audio bytes, {duration:.2f}s.")

    print(f"SUMMARY: {2 - failures}/2 synthesis checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
