from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from streamlit.testing.v1 import AppTest

from app.services import (
    account_dispatcher, accounts, comment_agent, master_agent, master_comments,
    profile_schedules as profiles, scheduler,
)


def _profile(identifier="the_afterframe"):
    return {
        "id": identifier, "name": identifier, "enabled": True,
        "enabled_platforms": ["youtube"],
        "schedule": {"enabled": True, "post_times_ist": ["05:00", "20:00"]},
        "youtube": {"enabled": True, "connected": True},
        "facebook": {"connected": False}, "instagram": {"connected": False},
    }


def _claim(row, slot):
    now = datetime.now(profiles.IST)
    hour, minute = map(int, slot.split(":"))
    with patch.object(accounts, "list_profiles", return_value=[row]), patch.object(profiles, "_executor") as executor:
        profiles.tick(now.replace(hour=hour, minute=minute, second=0, microsecond=0))
    if executor.submit.called:
        profiles._draining.release()
    return next(value for value in profiles._ledger().values()
                if value["slot"] == slot and value["profile_id"] == row["id"])


def test_initialize_persists_conservative_policy_without_activating():
    master_agent.initialize()
    assert scheduler._read("master_agent_policy.json", {})["videos_per_profile_per_day"] == 1
    assert not profiles.master_enabled()


def test_supervisor_paused_never_calls_remote_apis_or_replies():
    with patch.object(master_agent.autoflow_settings, "diagnostics") as diagnostics, patch.object(master_comments, "run_cycle") as comments:
        master_agent.supervise()
    diagnostics.assert_not_called()
    comments.assert_not_called()
    assert "Add a channel profile" in master_agent.status()["alerts"][0]


def test_active_supervision_runs_periodically_not_every_minute():
    profiles.set_master_enabled(True)
    with patch.object(master_agent.autoflow_settings, "diagnostics", return_value=[]) as health, patch.object(master_comments, "run_cycle", return_value=[]) as comments:
        master_agent.supervise()
        master_agent.supervise()
    health.assert_called_once()
    comments.assert_called_once_with(5)


def test_supervision_failure_is_visible_and_contains_no_exception_secret():
    profiles.set_master_enabled(True)
    with patch.object(master_agent.autoflow_settings, "diagnostics", side_effect=RuntimeError("private-token")):
        master_agent.supervise()
    report = master_agent.status()
    assert report["error_type"] == "RuntimeError"
    assert report["alerts"]
    assert "private-token" not in str(report)


def test_each_profile_has_one_durable_daily_reservation():
    profiles.set_master_enabled(True)
    first = _claim(_profile(), "05:00")
    second = _claim(_profile(), "20:00")
    assert first["status"] == "queued"
    assert second["status"] == "budget_blocked"
    other = _claim(_profile("other"), "20:00")
    assert other["status"] == "queued"


def test_next_trigger_skips_today_after_daily_reservation():
    profiles.set_master_enabled(True)
    profile = _profile()
    _claim(profile, "05:00")
    now = datetime.now(profiles.IST).replace(hour=6, minute=0, second=0, microsecond=0)
    assert profiles.next_trigger([profile], now).date() == (now + timedelta(days=1)).date()
    assert profiles.next_trigger([profile], now).hour == 5


def test_legacy_comment_jobs_cannot_bypass_master_or_daily_video_cap():
    master_agent.initialize()
    with patch.object(comment_agent, "load_settings") as settings:
        assert comment_agent.run_comment_cycle()["skipped"] == "managed_by_master"
        assert comment_agent.process_next_idea() is None
    settings.assert_not_called()


def test_preexisting_queue_is_capped_before_paid_work():
    profiles.set_master_enabled(True)
    profile = _profile()
    first = _claim(profile, "05:00")
    first.update(status="complete")
    profiles._record(first)
    second = _claim(profile, "20:00")
    second.update(status="queued")
    profiles._record(second)  # Simulate an old queue from before the daily limit.
    with patch.object(accounts, "list_profiles", return_value=[profile]), patch.object(scheduler, "preflight") as preflight:
        profiles.run_profile_pipeline(profile["id"], second["slot"], entry=second)
    preflight.assert_not_called()
    assert profiles._ledger()[second["run_id"]]["status"] == "budget_blocked"


def test_preflight_transient_failure_retries_once_without_paid_generation():
    profiles.set_master_enabled(True)
    profile = _profile()
    entry = _claim(profile, "05:00")
    with patch.object(accounts, "list_profiles", return_value=[profile]), patch.object(scheduler, "preflight", side_effect=OSError("unavailable")), patch.object(profiles.profile_content, "generate_profile_package") as generate:
        profiles.run_profile_pipeline(profile["id"], "05:00", entry=entry)
        assert entry["status"] == "queued"
        assert entry["retry_at"]
        profiles.run_profile_pipeline(profile["id"], "05:00", entry=entry)
    generate.assert_not_called()
    assert entry["status"] == "failed"
    assert entry["attempts"] == 2


def test_paid_content_failure_never_automatically_replays():
    profiles.set_master_enabled(True)
    profile = _profile()
    entry = _claim(profile, "05:00")
    with patch.object(accounts, "list_profiles", return_value=[profile]), patch.object(scheduler, "preflight"), patch.object(profiles.profile_content, "generate_profile_package", side_effect=OSError("provider timeout")) as generate:
        profiles.run_profile_pipeline(profile["id"], "05:00", entry=entry)
        profiles.run_profile_pipeline(profile["id"], "05:00", entry=entry)
    generate.assert_called_once()
    assert entry["status"] == "failed"
    assert "retry_at" not in entry


def _comment():
    return {"key": "youtube:comment1", "platform": "youtube", "comment_id": "comment1",
            "published_at": datetime.now(timezone.utc).isoformat(), "text": "Great story",
            "media_id": "video1", "author": "viewer"}


def _comment_cycle(profile, client, analysis=None):
    with (
        patch.object(accounts, "list_profiles", return_value=[profile]),
        patch.object(comment_agent, "YouTubeClient", return_value=client),
        patch.object(comment_agent, "analyze_comments", return_value=analysis or {"youtube:comment1": {"reply_text": "Thank you for watching."}}),
    ):
        return master_comments.run_cycle(5)


@pytest.mark.parametrize("failure", [None, comment_agent.ReplyUncertain("timeout"), RuntimeError("secret")])
def test_comment_reply_is_durable_at_most_once(failure):
    profiles.set_master_enabled(True)
    client = Mock()
    client.fetch_comments.return_value = [_comment()]
    client.reply.return_value = "reply1"
    client.reply.side_effect = failure
    profile = _profile()
    _comment_cycle(profile, client)
    _comment_cycle(profile, client)
    client.reply.assert_called_once()
    records = scheduler._read("master_comment_attempts.json", {})
    record = records["the_afterframe:youtube:comment1"]
    assert record["status"] == ("replied" if failure is None else "uncertain")
    assert "secret" not in str(records)


def test_comments_pause_immediately_before_public_reply():
    profiles.set_master_enabled(True)
    client = Mock()
    client.fetch_comments.return_value = [_comment()]
    with patch.object(master_comments, "_enabled", side_effect=[True, True, False]):
        _comment_cycle(_profile(), client)
    client.reply.assert_not_called()


def test_comments_rolling_day_cap_includes_uncertain_attempts():
    profiles.set_master_enabled(True)
    now = datetime.now(timezone.utc)
    scheduler._write("master_comment_attempts.json", {
        str(index): {"profile_id": "the_afterframe", "platform": "youtube",
                     "attempted_at": (now - timedelta(hours=2)).isoformat(), "status": "uncertain"}
        for index in range(5)
    })
    client = Mock()
    _comment_cycle(_profile(), client)
    client.fetch_comments.assert_not_called()
    client.reply.assert_not_called()


def test_comment_analysis_daily_cap_prevents_further_paid_calls():
    profiles.set_master_enabled(True)
    scheduler._write("master_comment_attempts.json", {
        str(index): {"profile_id": "the_afterframe", "platform": "youtube",
                     "seen_at": datetime.now(timezone.utc).isoformat(), "status": "skipped"}
        for index in range(20)
    })
    client = Mock()
    _comment_cycle(_profile(), client)
    client.fetch_comments.assert_not_called()


def test_corrupt_comment_memory_blocks_instead_of_discarding_attempts():
    scheduler._write("master_comment_attempts.json", {"bad": {"status": "uncertain"}})
    with pytest.raises(ValueError, match="Invalid master comment"):
        master_comments.run_cycle(5)
    assert scheduler._read("master_comment_attempts.json", {}) == {"bad": {"status": "uncertain"}}


def test_paid_comment_analysis_is_reserved_before_call_and_not_replayed():
    profiles.set_master_enabled(True)
    profile = _profile()
    client = Mock()
    client.fetch_comments.return_value = [_comment()]
    with patch.object(accounts, "list_profiles", return_value=[profile]), patch.object(comment_agent, "YouTubeClient", return_value=client), patch.object(comment_agent, "analyze_comments", side_effect=comment_agent.AnalysisError("failure")) as analyze:
        master_comments.run_cycle(5)
        master_comments.run_cycle(5)
    analyze.assert_called_once()
    client.reply.assert_not_called()


def test_comment_poll_only_analyses_one_sendable_candidate():
    profiles.set_master_enabled(True)
    client = Mock()
    first = _comment()
    client.fetch_comments.return_value = [first, {**first, "key": "youtube:comment2", "comment_id": "comment2"}]
    client.reply.return_value = "reply1"
    with patch.object(accounts, "list_profiles", return_value=[_profile()]), patch.object(comment_agent, "YouTubeClient", return_value=client), patch.object(comment_agent, "analyze_comments", return_value={first["key"]: {"reply_text": "Thank you."}}) as analyze:
        master_comments.run_cycle(5)
    assert len(analyze.call_args.args[0]) == 1
    client.reply.assert_called_once()


def test_youtube_uses_profile_oauth_not_global_token():
    with patch.object(accounts, "_refresh_youtube_token", return_value=("private-token", "ok")) as refresh:
        assert comment_agent.YouTubeClient("the_afterframe")._access_token() == "private-token"
    refresh.assert_called_once_with("the_afterframe")


def test_meta_clients_keep_credentials_isolated():
    profile = {**_profile(), "facebook": {"page_id": "page-a", "page_access_token": "fb-a"},
               "instagram": {"business_account_id": "ig-a", "access_token": "token-a"}}
    assert comment_agent.FacebookClient(profile)._credentials() == ("fb-a", "page-a")
    assert comment_agent.InstagramClient(profile)._credentials() == ("token-a", "ig-a")


def test_master_control_is_read_only_and_does_not_activate_engine():
    app = AppTest.from_string("from webui.dashboard import _master_control\n_master_control()\n").run(timeout=60)
    assert not app.exception
    assert not profiles.master_enabled()
    assert [item.value for item in app.metric] == ["1 / profile", "5 / profile", "Offline"]


def test_pause_blocks_remaining_platforms_without_replaying_success(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    profile = {**_profile(), "enabled_platforms": ["youtube", "facebook"]}
    with patch.object(accounts, "list_profiles", return_value=[profile]), patch.object(account_dispatcher, "_dispatch_one", return_value={"account_id": profile["id"], "profile_id": profile["id"], "platform": "youtube", "status": "success", "success": True, "url": None, "error_code": None, "error": None}) as dispatch, patch.object(account_dispatcher, "_append_history"):
        rows = account_dispatcher.dispatch_video(
            str(video), "Title", "Caption", [profile["id"]],
            dispatch_allowed=Mock(side_effect=[True, False]),
        )
    dispatch.assert_called_once()
    assert rows[0]["success"]
    assert rows[1]["status"] == "cancelled"
