import os
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import agent, voice


WEBUI_MAIN = Path(__file__).resolve().parents[2] / "webui.py"
PACKAGE = {
    "subject": "New subject",
    "language": "en-US",
    "script": "New editable narration.",
    "keywords": "sunrise, walking, notebook",
    "description": "A fresh start. #Habits",
}


def widget(elements, key):
    exact = next((item for item in elements if str(item.key) == key), None)
    if exact is not None:
        return exact
    return next(item for item in elements if str(item.key).startswith(f"{key}_"))


def test_agent_applies_all_fields_and_preserves_manual_editing():
    with (
        patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}),
        patch.object(config, "ui", dict(config.ui, language="en", tts_server="azure-tts-v1")),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(agent, "generate_complete_video_package", return_value=PACKAGE) as generate,
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        widget(app.text_input, "content_agent_topic").set_value("Habits")
        widget(app.button, "content_agent_generate").click().run()
        assert not app.exception
        generate.assert_called_once()
        assert generate.call_args.args == ("Habits", "en-US")
        assert widget(app.text_area, "video_subject").value == PACKAGE["subject"]
        assert widget(app.text_area, "video_script").value == PACKAGE["script"]
        assert widget(app.text_area, "video_terms").value == PACKAGE["keywords"]
        assert widget(app.selectbox, "script_language_select").value == "en-US"
        assert widget(app.text_area, "content_agent_description").value == PACKAGE["description"]
        widget(app.text_area, "video_script").set_value("Manual edit.").run()
        assert widget(app.text_area, "video_script").value == "Manual edit."
        assert generate.call_count == 1


def test_agent_failure_leaves_content_unchanged():
    with (
        patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}),
        patch.object(config, "ui", dict(config.ui, language="en", tts_server="azure-tts-v1")),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(agent, "generate_complete_video_package", side_effect=agent.ContentAgentError("LLM unavailable")),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.session_state["video_subject"] = "Keep this"
        app.run()
        widget(app.button, "content_agent_generate").click().run()
        assert not app.exception
        assert widget(app.text_area, "video_subject").value == "Keep this"
        assert any("LLM unavailable" in str(item.value) for item in app.error)


def test_elevenlabs_env_default_catalog_and_manual_provider_switch():
    with (
        patch.dict(os.environ, {"ELEVENLABS_API_KEY": "env-test-key"}),
        patch.object(config, "ui", dict(config.ui, language="en", voice_mode="tts", tts_server="azure-tts-v1")),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(voice, "get_elevenlabs_voices", return_value=[]),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert not app.exception
        provider = widget(app.selectbox, "tts_server_select")
        assert provider.value == "elevenlabs"
        assert "ElevenLabs TTS" in provider.options
        voices = widget(app.selectbox, "speech_synthesis_select_elevenlabs")
        assert len(voices.options) == 7
        selected = voice.get_elevenlabs_catalog_voices()[0]
        voices.set_value(selected).run()
        assert voice.resolve_elevenlabs_voice_id(
            widget(app.selectbox, "speech_synthesis_select_elevenlabs").value
        ) == "pNInz6obpgDQGcFmaJgB"
        assert widget(app.selectbox, "elevenlabs_model_select").value
        widget(app.selectbox, "tts_server_select").set_value("azure-tts-v1").run()
        assert widget(app.selectbox, "tts_server_select").value == "azure-tts-v1"
        assert not app.exception
