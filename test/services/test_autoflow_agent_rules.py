import json
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import env_secrets
from app.services import accounts, agent, autoflow_settings, profile_content, scheduler, voice


def test_global_credentials_are_file_only_and_reload_without_restart(private_test_dotenv, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-process-key")
    private_test_dotenv.write_text("OPENAI_API_KEY=file-key\nPEXELS_API_KEY=one,two\n")
    assert env_secrets.resolve("app", "openai_api_key", "wrong-config-key") == "file-key"
    assert env_secrets.resolve("app", "pexels_api_keys", ["wrong"]) == ["one", "two"]
    private_test_dotenv.write_text("OPENAI_API_KEY=rotated-key\n")
    assert env_secrets.resolve("app", "openai_api_key") == "rotated-key"
    private_test_dotenv.write_text("")
    assert not env_secrets.resolve("app", "openai_api_key", "wrong-config-key")


def test_global_file_uses_no_process_interpolation(private_test_dotenv, monkeypatch):
    monkeypatch.setenv("SECRET_SOURCE", "process-secret")
    private_test_dotenv.write_text("GEMINI_API_KEY='${SECRET_SOURCE}'\n")
    assert env_secrets.global_value("GEMINI_API_KEY") == "${SECRET_SOURCE}"


def test_elevenlabs_reads_file_not_old_config(private_test_dotenv, monkeypatch):
    monkeypatch.setitem(voice.config.elevenlabs, "api_key", "wrong-config")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "wrong-process")
    private_test_dotenv.write_text("ELEVENLABS_API_KEY=file-eleven\n")
    assert voice.get_elevenlabs_api_key() == "file-eleven"
    private_test_dotenv.write_text("")
    assert voice.get_elevenlabs_api_key() == ""


def test_requested_profile_schema_round_trips_independent_schedules(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "STORAGE_DIR", tmp_path)
    first = accounts.save_profile({
        "id": "the_afterframe", "name": "The Afterframe", "category": "Sci-Fi Thriller",
        "niche_prompt": "Hook in two seconds, twist ending", "visual_style": "dark neon",
        "schedule": {"enabled": True, "post_times": ["05:00", "20:00"]},
        "destinations": {"youtube": True, "instagram": False, "facebook": False},
    })
    second = accounts.save_profile({
        "id": "mind_vault_paradox", "name": "Mind Vault Paradox", "category": "Stoic Mindset",
        "niche_prompt": "Human behavior awareness",
        "schedule": {"enabled": True, "post_times": ["07:00", "14:00", "21:00"]},
        "destinations": {"youtube": True, "instagram": True, "facebook": True},
    })
    assert first["category"] == "Sci-Fi Thriller"
    assert first["niche_prompt"] == "Hook in two seconds, twist ending"
    assert first["schedule"]["post_times"] == ["05:00", "20:00"]
    assert len(second["schedule"]["post_times"]) == 3
    assert second["enabled_platforms"] == ["youtube", "facebook", "instagram"]
    accounts.save_profile({"id": second["id"], "schedule": {"post_times": ["14:00"]}})
    first_now, second_now = accounts.list_profiles()
    assert first_now["schedule"] == first["schedule"]
    assert second_now["schedule"]["post_times"] == ["14:00"]
    assert second_now["destinations"] == second["destinations"]


def test_corrupt_accounts_never_silently_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "STORAGE_DIR", tmp_path)
    target = tmp_path / "accounts.json"
    target.write_text("broken")
    with pytest.raises(ValueError):
        accounts.save_profile({"name": "New"})
    assert target.read_text() == "broken"


def test_existing_output_title_is_rejected_then_regenerated(tmp_path, monkeypatch):
    monkeypatch.setitem(profile_content.config.app, "final_video_export_dir", str(tmp_path))
    output = tmp_path / "the_afterframe"
    output.mkdir()
    (output / "2026-10-01_the_reflection_echo.mp4").write_bytes(b"existing")
    story = {"subject": "The Reflection Echo", "script": "story", "hook": "story",
             "visual_keywords": "moody hallway", "tags": ["TheAfterframe"]}
    fresh = {**story, "subject": "The Silent Moon"}
    with (
        patch.object(profile_content.content_quality, "evaluate", return_value={"word_count": 80}),
        patch.object(agent, "generate_afterframe_story", side_effect=[story, fresh]) as generate,
    ):
        result = profile_content.generate_profile_package({
            "id": "the_afterframe", "name": "The Afterframe", "category": "Sci-Fi",
            "niche_prompt": "Twist ending", "visual_style": "neon laboratory",
        })
    assert generate.call_count == 2
    assert result["subject"] == "The Silent Moon"
    assert "neon laboratory" in generate.call_args.kwargs["creative_brief"]
    assert "Twist ending" in generate.call_args.kwargs["creative_brief"]
    assert "the reflection echo" in str(generate.call_args.kwargs["recent_stories"])


def test_duplicate_retry_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setitem(profile_content.config.app, "final_video_export_dir", str(tmp_path))
    folder = tmp_path / "mind_vault_paradox"
    folder.mkdir()
    (folder / "2026-10-01_same_title.mp4").write_bytes(b"existing")
    with patch.object(agent, "generate_complete_video_package", return_value={"subject": "Same Title"}) as generate:
        with pytest.raises(agent.ContentAgentError, match="repeat"):
            profile_content.generate_profile_package({"id": folder.name, "name": "Mind Vault Paradox"})
    assert generate.call_count == 3


def test_pausing_after_render_prevents_dispatch(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    entry = {}
    with (
        patch.object(scheduler.task, "start", return_value={"videos": [str(video)]}),
        patch("app.services.account_dispatcher.dispatch_video") as dispatch,
    ):
        scheduler.render_and_publish(
            {"subject": "Title", "script": "Narration", "keywords": "dark hallway", "description": "Caption"},
            scheduler.ScheduleSettings(target_account_ids=["the_afterframe"]), entry, lambda _: None,
            dispatch_allowed=lambda: False,
        )
    dispatch.assert_not_called()
    assert entry["status"] == "cancelled"
    assert entry["video_path"] == str(video)


def test_vault_status_works_with_no_reentry(private_test_dotenv):
    private_test_dotenv.write_text("OPENAI_API_KEY=hidden-file-key\nELEVENLABS_API_KEY=hidden-eleven\n")
    app = AppTest.from_string("from webui.dashboard import _vault\n_vault()\n").run(timeout=60)
    assert not app.exception
    board = app.dataframe[0].value
    assert board.loc[board["Service"] == "OpenAI", "Status"].iloc[0] == "🟢 Connected via .env"
    assert not app.text_input
    assert not app.text_area
    assert "hidden-file-key" not in str(app)
