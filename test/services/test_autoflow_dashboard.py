import os
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

import pytest

from streamlit.testing.v1 import AppTest

from app.services import accounts, autoflow_settings, autoflow_studio, profile_schedules, scheduler
from app.config import env_secrets

ROOT = Path(__file__).resolve().parents[2]
PROFILE = {
    "id": "35b3f8b0-5631-46b2-8a52-f23c9eaab44a",
    "name": "The Afterframe", "niche": "Sci-Fi Thriller", "visual_style": "dark hallway, rain",
    "enabled": True, "enabled_platforms": ["youtube"],
    "youtube": {"connected": False, "enabled": True},
    "facebook": {"connected": False, "page_id": "", "enabled": False},
    "instagram": {"connected": False, "business_account_id": "", "enabled": False},
    "schedule": {"enabled": False, "slots": ["05:00", "20:00"], "timezone": "Asia/Kolkata", "niche_style": "afterframe"},
}


def _app(profiles=None):
    return AppTest.from_file(str(ROOT / "webui.py"), default_timeout=60)


def test_dashboard_has_exact_four_clean_tabs_and_blank_vault(tmp_path):
    with (
        patch.object(accounts, "list_profiles", return_value=[]),
        patch.object(scheduler, "STORAGE", tmp_path),
        patch("webui.dashboard.utils.check_ffmpeg_ready", return_value=True),
        patch("webui.dashboard.recent_dispatches", return_value=[]),
    ):
        app = _app().run()
    assert not app.exception
    assert [item.label for item in app.tabs] == [
        "🎬 Manual Studio", "⏰ Auto-Pilot Scheduler", "👥 Channel Profiles", "⚙️ Settings & System Health",
    ]
    assert len(app.toggle) == 2
    assert app.toggle[0].label == "Authorize automated multi-platform dispatch"
    assert all(item.value == "" for item in app.text_input)
    assert [item.label for item in app.metric[:4]] == [
        "Channel profiles", "Enabled schedules", "Linked accounts", "Autonomous engine",
    ]
    assert [item.value for item in app.metric[:4]] == ["0", "0", "0", "Offline"]
    assert any(item.label == "Create your first profile" for item in app.button)


def test_workspace_pulse_counts_real_profile_state_without_network_calls():
    profile = {
        **PROFILE, "youtube": {"connected": True},
        "schedule": {**PROFILE["schedule"], "enabled": True},
    }
    with (
        patch.object(accounts, "list_profiles", return_value=[profile]),
        patch.object(scheduler, "scheduler_status", return_value={"worker_alive": True}),
        patch.object(profile_schedules, "master_enabled", return_value=False),
    ):
        app = AppTest.from_string("from webui.dashboard import _workspace_pulse\n_workspace_pulse()").run(timeout=60)
    assert not app.exception
    assert [item.value for item in app.metric] == ["1", "1", "1", "Standby"]


def test_onboarding_opens_profile_editor_without_starting_generation():
    with patch("webui.dashboard._profile_modal") as modal, patch.object(autoflow_studio, "submit") as submit:
        app = AppTest.from_string("from webui.dashboard import _studio\n_studio([], False)").run(timeout=60)
        next(item for item in app.button if item.label == "Create your first profile").click().run()
    assert not app.exception
    modal.assert_called_once_with()
    submit.assert_not_called()


def test_studio_only_offers_primary_and_english_fallback_and_no_dispatch_without_consent(tmp_path):
    with (
        patch.object(accounts, "list_profiles", return_value=[PROFILE]),
        patch.object(scheduler, "STORAGE", tmp_path),
        patch("webui.dashboard.utils.check_ffmpeg_ready", return_value=True),
        patch("webui.dashboard.recent_dispatches", return_value=[]),
        patch.object(autoflow_studio, "submit", return_value="task") as submit,
        patch("webui.dashboard.state.state.get_task", return_value={"state": 4, "error": "test"}),
    ):
        app = _app().run()
        assert not app.exception
        engine = next(item for item in app.selectbox if item.label == "Voice Engine")
        assert engine.options == ["ElevenLabs", "Edge TTS (English)"]
        next(item for item in app.text_input if item.label == "Topic / Video Subject").set_value("The Reflection")
        next(item for item in app.text_area if item.label == "Narration Script").set_value("My reflection moved.")
        next(item for item in app.button if item.label == "Generate Video").click().run()
        assert not app.exception
    assert submit.call_args.args[5] == []
    assert submit.call_args.args[6] is False


def test_master_switch_is_persistent_and_does_not_rewrite_profile_times(tmp_path):
    with (
        patch.object(accounts, "list_profiles", return_value=[PROFILE]),
        patch.object(accounts, "save_profile") as save,
        patch.object(scheduler, "STORAGE", tmp_path),
        patch("webui.dashboard.utils.check_ffmpeg_ready", return_value=True),
        patch("webui.dashboard.recent_dispatches", return_value=[]),
    ):
        app = _app().run()
        next(item for item in app.toggle if item.key == "af-master-agent").set_value(True).run()
        assert not app.exception
        assert profile_schedules.master_enabled()
        save.assert_not_called()


def test_vault_writes_only_nonblank_values_preserving_prior_secrets(tmp_path):
    target = tmp_path / ".env"
    target.write_text("EXISTING_SECRET='keep'\nELEVENLABS_API_KEY='old'\n", encoding="utf-8")
    with (
        patch.object(autoflow_settings.config, "root_dir", str(tmp_path)),
        patch.object(env_secrets, "DOTENV_PATH", target),
        patch.object(autoflow_settings.config, "save_config"),
        patch.dict(os.environ, {}, clear=False),
        patch.dict(autoflow_settings.config.app, {}, clear=False),
    ):
        autoflow_settings.save_vault("anthropic", {"ELEVENLABS_API_KEY": "", "GOOGLE_CLIENT_ID": "desktop-client"})
        assert "EXISTING_SECRET='keep'" in target.read_text()
        assert "ELEVENLABS_API_KEY='old'" in target.read_text()
        assert env_secrets.global_value("GOOGLE_CLIENT_ID") == "desktop-client"


def test_settings_diagnostics_does_not_return_provider_response_or_secrets():
    class Response:
        status_code = 401
        text = "secret-should-not-leak"
        def close(self):
            pass
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sensitive-key"}), patch.object(autoflow_settings.requests, "get", return_value=Response()):
        rows = autoflow_settings.diagnostics()
    assert "sensitive-key" not in str(rows)
    assert "secret-should-not-leak" not in str(rows)


def test_studio_rejects_unauthorized_dispatch_before_enqueue():
    with patch.object(accounts, "list_profiles", return_value=[PROFILE]), patch.object(autoflow_studio, "_executor") as executor:
        with pytest.raises(ValueError, match="Authorize"):
            autoflow_studio.submit(PROFILE["id"], "title", "script", "elevenlabs", "pexels", [PROFILE["id"]], False)
        executor.submit.assert_not_called()


def test_studio_preserves_profile_subject_and_keywords_and_disables_legacy_publishing():
    with (
        patch("app.services.task.start", return_value={}) as start,
        patch("app.services.profile_content.generate_profile_package") as generate,
        patch("app.services.state.state.update_task"),
        patch.object(autoflow_studio, "_slot", MagicMock()) as gate,
    ):
        autoflow_studio._run("dry-test", PROFILE, "The Reflection Echo", "My reflection moved.",
                             "en-US-GuyNeural", "pexels", [], "dim laboratory, rain at night")
    generate.assert_not_called()
    params = start.call_args.args[1]
    assert params.profile_id == PROFILE["id"]
    assert params.video_subject == "The Reflection Echo"
    assert params.video_terms == "dim laboratory, rain at night"
    assert start.call_args.kwargs["auto_publish"] is False
    gate.release.assert_called_once()
