from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.models.schema import VideoParams
from app.services import accounts, scheduler, task, voice
from app.services.state import MemoryState


ROOT = Path(__file__).resolve().parents[2]


def test_account_dispatch_uses_actual_video_and_continues_after_failed_target():
    state = MemoryState()
    state.update_task("profile-task", state=task.const.TASK_STATE_COMPLETE)
    rows = [
        {"profile_id": "a", "platform": "youtube", "success": False, "error": "upload_failed"},
        {"profile_id": "b", "platform": "facebook", "success": True, "url": "https://facebook.com/123"},
    ]
    with (
        patch.object(task.sm, "state", state),
        patch("app.services.account_dispatcher.dispatch_video", return_value=rows) as dispatch,
        patch.object(task.upload_post, "cross_post_video") as legacy,
    ):
        task._run_cross_post(
            "profile-task", (r"C:\outputs\manual\Exact Subject.mp4",),
            "Exact Subject", "Script", "en-US", ("accounts",), "private",
            target_account_ids=("a", "b"),
        )
    assert dispatch.call_args.args[0] == r"C:\outputs\manual\Exact Subject.mp4"
    assert dispatch.call_args.args[3] == ["a", "b"]
    assert state.get_task("profile-task")["cross_post_results"] == rows
    assert state.get_task("profile-task")["cross_post_state"] == task.const.CROSS_POST_STATE_FAILED
    legacy.assert_not_called()


def test_selected_accounts_publish_without_legacy_upload_post_configuration():
    params = VideoParams(video_subject="Exact Subject", target_account_ids=["profile-a"])
    with (
        patch.object(task, "generate_script", return_value="script"),
        patch.object(task, "generate_terms", return_value=["scene"]),
        patch.object(task, "save_script_data"),
        patch.object(task, "generate_audio", return_value=("audio.mp3", 1, object())),
        patch.object(task, "generate_subtitle", return_value=""),
        patch.object(task, "get_video_materials", return_value=["clip.mp4"]),
        patch.object(task, "generate_final_videos", return_value=(["Exact Subject.mp4"], [], [])),
        patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task.sm.state, "update_task"),
        patch.object(task.upload_post.upload_post_service, "is_configured", return_value=False),
        patch.object(task, "_schedule_cross_post", return_value=None) as publish,
    ):
        result = task.start("account-render", params)
    assert result["cross_post_state"] == task.const.CROSS_POST_STATE_PENDING
    assert publish.call_args.kwargs["params"].target_account_ids == ["profile-a"]
    assert publish.call_args.kwargs["platforms"] == ["accounts"]


def test_srz_accounts_tab_adds_profile_without_exposing_secret(tmp_path):
    with (
        patch.object(accounts, "list_profiles", return_value=[]),
        patch.object(scheduler, "STORAGE", tmp_path),
        patch.object(voice, "get_elevenlabs_voices", return_value=[]),
        patch("app.config.config.try_save_config", return_value=True),
    ):
        app = AppTest.from_file(str(ROOT / "webui.py"), default_timeout=60).run()
    assert not app.exception
    assert any(item.label == "👥 Channel Profiles" for item in app.tabs)
    assert any("Srz AutoFlow" in item.value for item in app.title)
    assert any(item.label == "Create your first profile" for item in app.button)
    app = AppTest.from_string(
        "from webui.components.accounts_manager import _profile_form\n_profile_form()\n",
        default_timeout=30,
    ).run()
    assert not app.exception
    token = next(item for item in app.text_input if item.label.startswith("Page Access Token"))
    assert token.value == ""
    assert token.proto.type == token.proto.PASSWORD


def test_profile_form_is_separate_from_schedule_mapping_and_has_visual_style():
    with patch.object(scheduler, "TIMEZONES", {"America/New_York": "US/Eastern"}):
        app = AppTest.from_string(
            "from webui.components.accounts_manager import _profile_form\n"
            "_profile_form()\n",
            default_timeout=30,
        ).run()
    assert not app.exception
    assert any(item.label == "Visual Style Keywords" for item in app.text_input)
    assert len(app.time_input) == 2
    assert any("Daily Posting Schedule (IST)" in item.value for item in app.subheader)
    assert not any("authorize" in item.label.lower() for item in app.checkbox)
