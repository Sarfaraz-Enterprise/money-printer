import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
import pytz
from apscheduler.triggers.cron import CronTrigger
from filelock import FileLock
from pydantic import ValidationError

from app.services import scheduler, profile_schedules


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "STORAGE", tmp_path)
    return tmp_path


def test_defaults_and_persistence():
    settings = scheduler.load_settings()
    assert not settings.enabled
    assert settings.slots == ["05:00", "20:00"]
    assert settings.timezone == "America/New_York"
    assert settings.topic_mode == "niche"
    scheduler.save_settings(settings)
    assert scheduler.load_settings() == settings
    assert "api_key" not in (scheduler.STORAGE / "schedule_config.json").read_text()


@pytest.mark.parametrize("changes", [
    {"timezone": "bad"}, {"slots": ["05:00", "05:00"]},
    {"slots": ["25:00", "20:00"]}, {"slots": ["5:00", "20:00"]},
    {"platforms": []}, {"platforms": ["youtube", "youtube"]},
    {"voice_name": "elevenlabs:../bad:voice"}, {"topic_mode": "invented"},
])
def test_settings_validation(changes):
    with pytest.raises(ValidationError):
        scheduler.ScheduleSettings(**changes)


@pytest.mark.parametrize("month,day,offset", [(1, 15, -5), (7, 15, -4), (3, 8, -4), (11, 1, -5)])
def test_cron_stays_on_eastern_wall_clock_across_dst(month, day, offset):
    zone = pytz.timezone("America/New_York")
    now = zone.localize(datetime(2026, month, day, 0, 0))
    for hour in (5, 20):
        run = CronTrigger(hour=hour, minute=0, timezone=zone).get_next_fire_time(None, now)
        assert run.hour == hour and run.minute == 0
        assert run.utcoffset().total_seconds() == offset * 3600


def test_single_owner_and_restart_restores_jobs():
    with patch.object(scheduler, "preflight"):
        scheduler.save_settings(scheduler.ScheduleSettings(enabled=True))
        profile_schedules.set_master_enabled(True)
        first = scheduler.SchedulerService()
        second = scheduler.SchedulerService()
        try:
            assert first.start()
            assert not second.start()
            assert not [job for job in first.scheduler.get_jobs() if job.id.startswith("daily-")]
            assert first.scheduler.get_job("autonomous-minute") is not None
            assert scheduler.scheduler_status()["running"]
        finally:
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(first.stop).result()
        assert second.start()
        second.stop()
    assert not scheduler.scheduler_status()["running"]


def test_disable_removes_jobs_and_stale_heartbeat_is_not_running():
    with patch.object(scheduler, "preflight"):
        scheduler.save_settings(scheduler.ScheduleSettings(enabled=True))
        service = scheduler.SchedulerService()
        try:
            service.start()
            scheduler.save_settings(scheduler.ScheduleSettings(enabled=False))
            service.refresh()
            assert not [job for job in service.scheduler.get_jobs() if job.id.startswith("daily-")]
        finally:
            service.stop()
    scheduler._write("schedule_status.json", {"heartbeat": "2000-01-01T00:00:00+00:00", "running": True})
    assert not scheduler.scheduler_status()["running"]
    assert not scheduler.scheduler_status()["worker_alive"]


def test_corrupt_settings_pause_jobs():
    with patch.object(scheduler, "preflight"):
        scheduler.save_settings(scheduler.ScheduleSettings(enabled=True))
        service = scheduler.SchedulerService()
        try:
            service.start()
            (scheduler.STORAGE / "autoflow_agent.json").write_text("broken")
            service.refresh()
            assert not scheduler.scheduler_status()["running"]
            assert scheduler.scheduler_status()["error_type"] == "JSONDecodeError"
        finally:
            service.stop()


def test_interrupted_run_is_not_replayed():
    scheduler._record({"run_id": "old", "status": "running"})
    service = scheduler.SchedulerService()
    try:
        service.start()
        assert scheduler.recent_history()[0]["status"] == "interrupted"
    finally:
        service.stop()


def test_trending_feed_and_failure_no_silent_evergreen_fallback():
    response = Mock()
    response.iter_content.return_value = [
        b"<rss><channel><item><title>USA topic</title></item></channel></rss>"
    ]
    with patch.object(scheduler.requests, "get", return_value=response) as get:
        topic = scheduler._trend_topic("tech")
    assert "USA topic" in topic
    assert "tech" in topic
    get.assert_called_once()
    assert get.call_args.args[0] == scheduler.TREND_URL
    response.iter_content.return_value = [b"<rss><channel /></rss>"]
    with patch.object(scheduler.requests, "get", return_value=response):
        with pytest.raises(ValueError, match="no topics"):
            scheduler._trend_topic("")


def test_content_retries_once_without_retrying_publish():
    story = {
        "subject": "The Reflection Echo", "hook": "A reflection moved.",
        "script": "Narration", "visual_keywords": "dark mirror, rain", "tags": ["TheAfterframe"],
    }
    with (
        patch.object(scheduler.agent, "generate_afterframe_story", side_effect=[
            scheduler.agent.ContentAgentError("network"), story,
        ]) as generate,
        patch.object(scheduler.time, "sleep"),
        patch.object(scheduler, "_trend_topic") as trends,
    ):
        package = scheduler._generate_package(scheduler.ScheduleSettings(
            topic_mode="trending", niche="business advice", sub_theme_focus="Time loops",
        ))
    assert generate.call_count == 2
    assert generate.call_args.args[0] == "Time loops"
    assert package["keywords"] == story["visual_keywords"]
    assert package["subject"] == story["subject"]
    assert package["description"].endswith("#TheAfterframe")
    trends.assert_not_called()


def test_pipeline_publishes_once_and_suppresses_legacy_uploader(tmp_path):
    video = tmp_path / "scheduled" / "US title.mp4"
    video.parent.mkdir()
    video.write_bytes(b"video")
    package = {
        "subject": "US title", "script": "Narration", "keywords": "ocean, forest",
        "language": "en-US", "description": "A caption. #Shorts",
    }
    publisher = Mock()
    publisher.snapshot_account.return_value = {"upload_post_api_key": "secret", "upload_post_username": "profile"}
    publisher.upload_video.return_value = {
        "success": True, "request_id": "req-1",
        "results": {
            "youtube": {"success": True, "video_id": "yt-1"},
            "instagram": {"success": True, "post_id": "ig-1"},
        },
        "api_key": "must-not-persist",
    }
    with (
        patch.object(scheduler, "preflight"),
        patch.object(scheduler, "_generate_package", return_value=package),
        patch.object(scheduler, "UploadPostService", return_value=publisher),
        patch.object(scheduler.task, "start", return_value={"videos": [str(video)]}) as render,
    ):
        settings = scheduler.ScheduleSettings(enabled=True)
        entry = {"run_id": "legacy-render-helper"}
        scheduler.render_and_publish(package, settings, entry, scheduler._record)
        scheduler._record(entry)
    render.assert_called_once()
    assert render.call_args.kwargs["auto_publish"] is False
    params = render.call_args.args[1]
    assert params.output_category == "scheduled"
    assert params.video_aspect.value == "9:16"
    assert params.subtitle_enabled
    assert params.voice_name.startswith("elevenlabs:")
    publisher.upload_video.assert_called_once()
    assert publisher.upload_video.call_args.args[0] == str(video)
    assert publisher.upload_video.call_args.kwargs["platforms"] == ["youtube", "instagram"]
    assert publisher.upload_video.call_args.args[1] == package["description"]
    entry = scheduler.recent_history()[0]
    assert entry["status"] == "complete"
    assert entry["request_id"] == "req-1"
    assert entry["results"][0]["post_id"] == "yt-1"
    assert "must-not-persist" not in json.dumps(entry)
    assert "secret" not in json.dumps(entry)


def test_uncertain_publish_is_recorded_without_resubmission(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    publisher = Mock()
    publisher.snapshot_account.return_value = {}
    publisher.upload_video.return_value = {"success": False, "request_id": "uncertain"}
    package = {"subject": "Title", "script": "Script", "keywords": "tree", "description": "Caption"}
    with (
        patch.object(scheduler, "preflight"),
        patch.object(scheduler, "_generate_package", return_value=package),
        patch.object(scheduler, "UploadPostService", return_value=publisher),
        patch.object(scheduler.task, "start", return_value={"videos": [str(video)]}),
    ):
        entry = {"run_id": "uncertain-helper"}
        with pytest.raises(ValueError, match="outcome is unconfirmed"):
            scheduler.render_and_publish(package, scheduler.ScheduleSettings(), entry, scheduler._record)
    publisher.upload_video.assert_called_once()
    assert entry["stage"] == "publish"
    assert entry["request_id"] == "uncertain"


def test_render_failure_never_uploads():
    with (
        patch.object(scheduler, "preflight"),
        patch.object(scheduler, "_generate_package", return_value={
            "subject": "Title", "script": "Script", "keywords": "tree", "description": "Caption",
        }),
        patch.object(scheduler, "UploadPostService") as publisher,
        patch.object(scheduler.task, "start", return_value=None),
    ):
        with pytest.raises(ValueError, match="non-empty MP4"):
            scheduler.render_and_publish(
                {"subject": "Title", "script": "Script", "keywords": "tree", "description": "Caption"},
                scheduler.ScheduleSettings(), {"run_id": "failed-render"}, scheduler._record,
            )
    publisher.return_value.upload_video.assert_not_called()
    assert scheduler.recent_history()[0]["stage"] == "render"


def test_overlap_is_skipped_without_generating():
    with patch.object(scheduler, "preflight"):
        scheduler.save_settings(scheduler.ScheduleSettings(enabled=True))
    with FileLock(str(scheduler._path("schedule-run.lock"))):
        with patch.object(scheduler, "_generate_package") as generate:
            scheduler.run_daily_autopilot_pipeline("05:00")
    generate.assert_not_called()
    assert not scheduler.recent_history()


def test_obsolete_trigger_does_not_publish_after_settings_change():
    with patch.object(scheduler, "preflight"):
        original = scheduler.ScheduleSettings(enabled=True)
        scheduler.save_settings(original.model_copy(update={"timezone": "America/Chicago"}))
    with patch.object(scheduler, "_generate_package") as generate:
        scheduler.run_daily_autopilot_pipeline("05:00", original.model_dump_json())
    generate.assert_not_called()
    assert not scheduler.recent_history()


def test_task_auto_publish_flag_prevents_double_posting():
    params = scheduler.VideoParams(video_subject="Title", bgm_type="none", bgm_volume=0)
    with (
        patch.object(scheduler.task, "generate_script", return_value="script"),
        patch.object(scheduler.task, "generate_terms", return_value=["tree"]),
        patch.object(scheduler.task, "save_script_data"),
        patch.object(scheduler.task, "generate_audio", return_value=("audio.mp3", 40, object())),
        patch.object(scheduler.task, "generate_subtitle", return_value="subtitle.srt"),
        patch.object(scheduler.task, "get_video_materials", return_value=["clip.mp4"]),
        patch.object(scheduler.task, "generate_final_videos", return_value=(["final.mp4"], [], [])),
        patch.object(scheduler.task.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(scheduler.task.sm.state, "update_task"),
        patch.object(scheduler.task.upload_post.upload_post_service, "is_configured", return_value=True),
        patch.object(scheduler.task.upload_post.upload_post_service.__class__, "auto_upload", True),
        patch.object(scheduler.task, "_schedule_cross_post") as publish,
    ):
        result = scheduler.task.start("autopilot-test", params, auto_publish=False)
    assert result["videos"] == ["final.mp4"]
    publish.assert_not_called()
