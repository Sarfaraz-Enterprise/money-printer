import os
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services import comment_agent, profile_schedules, scheduler, voice


MAIN = Path(__file__).resolve().parents[2] / "webui.py"
TOGGLE = "Enable Autonomous Comment & Content Agent"
SAVE = "Save Comment Agent Settings"


def _app():
    app = AppTest.from_file(str(MAIN), default_timeout=60)
    app.session_state["ui_language"] = "en"
    app.run()
    assert not app.exception
    return app


def test_comment_panel_requires_consent_and_hides_tokens(tmp_path):
    secrets = {
        "ELEVENLABS_API_KEY": "", "YOUTUBE_CLIENT_ID": "id-value-secret",
        "YOUTUBE_CLIENT_SECRET": "client-secret-value", "YOUTUBE_REFRESH_TOKEN": "refresh-token-value",
        "META_PAGE_ACCESS_TOKEN": "", "META_PAGE_ID": "", "INSTAGRAM_BUSINESS_ACCOUNT_ID": "",
    }
    with (
        patch.object(comment_agent, "STORAGE", tmp_path),
        patch.object(scheduler, "STORAGE", tmp_path),
        patch.dict(os.environ, secrets),
        patch("app.config.config.try_save_config", return_value=True),
        patch.object(voice, "get_elevenlabs_voices", return_value=[]),
        patch.object(scheduler, "preflight"),
        patch.object(comment_agent.CommentAgentService, "start") as start,
    ):
        app = _app()
        assert not any("Social Comment Agent" in item.label for item in app.expander)
        assert any("Master Agent" in item.value for item in app.subheader)
        toggle = next(item for item in app.toggle if item.key == "af-master-agent")
        assert toggle.value is False
        assert not profile_schedules.master_enabled()
        toggle.set_value(True).run()
        assert not app.exception
        assert profile_schedules.master_enabled()
        assert not comment_agent.load_settings().enabled  # No second legacy worker/authority.
        start.assert_not_called()
        rendered = str(app._tree)
        for value in secrets.values():
            if value:
                assert value not in rendered
