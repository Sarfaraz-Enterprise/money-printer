from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from filelock import FileLock

from app.services import profile_schedules as profiles, scheduler


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STORAGE", tmp_path)
    return tmp_path


def _profile(identifier="the_afterframe", times=None):
    return {
        "id": identifier, "name": identifier, "enabled": True, "category": "Sci-fi",
        "niche_prompt": "Twist ending", "visual_style": "dark laboratory",
        "enabled_platforms": ["youtube"],
        "schedule": {"enabled": True, "post_times": times or ["05:00", "20:00"]},
    }


def _now(slot="05:00"):
    now = datetime.now(profiles.IST)
    hour, minute = map(int, slot.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _claim(rows, slot="05:00"):
    profiles.set_master_enabled(True)
    with patch.object(profiles.accounts, "list_profiles", return_value=rows), patch.object(profiles, "_executor") as executor:
        profiles.tick(_now(slot))
    if executor.submit.called:
        profiles._draining.release()  # Executor is mocked; run the actual drain explicitly.


def test_independent_times_claim_all_same_minute_only_once():
    rows = [_profile(), _profile("mind_vault_paradox", ["05:00", "14:00", "21:00"])]
    _claim(rows)
    _claim(rows)
    assert len(profiles._ledger()) == 2
    assert all(row["status"] == "queued" for row in profiles._ledger().values())
    _claim(rows, "14:00")
    ledger = profiles._ledger()
    assert len(ledger) == 3
    assert sum(row["profile_id"] == "mind_vault_paradox" for row in ledger.values()) == 2


def test_paused_never_claims_or_generates():
    with patch.object(profiles.accounts, "list_profiles") as read, patch.object(profiles, "_executor") as executor:
        profiles.tick(_now())
    read.assert_not_called()
    executor.submit.assert_not_called()
    assert not profiles._ledger()


def test_two_due_accounts_are_drained_without_skipping_overlap():
    rows = [_profile(), _profile("mind_vault_paradox", ["05:00"])]
    _claim(rows)
    package = {"subject": "Fresh story", "script": "Narration"}

    def finish(package, settings, entry, record, **kwargs):
        assert kwargs["dispatch_allowed"]()
        entry.update(status="complete", stage="complete")
        record(entry)

    with (
        patch.object(profiles.accounts, "list_profiles", return_value=rows),
        patch.object(scheduler, "preflight"),
        patch.object(profiles.profile_content, "generate_profile_package", return_value=package),
        patch.object(scheduler, "render_and_publish", side_effect=finish) as render,
    ):
        profiles._draining.acquire()
        profiles._drain()
    assert render.call_count == 2
    assert [call.args[1].target_account_ids for call in render.call_args_list] == [["the_afterframe"], ["mind_vault_paradox"]]
    assert all(row["status"] == "complete" for row in profiles._ledger().values())


def test_busy_render_remains_queued_and_eventually_runs_without_duplicate():
    row = _profile()
    _claim([row])
    entry = next(iter(profiles._ledger().values()))
    with FileLock(str(scheduler._path("schedule-run.lock"))):
        assert not profiles.run_profile_pipeline(row["id"], "05:00", entry=entry)
    assert profiles._ledger()[entry["run_id"]]["status"] == "queued"
    with (
        patch.object(profiles.accounts, "list_profiles", return_value=[row]),
        patch.object(scheduler, "preflight"),
        patch.object(profiles.profile_content, "generate_profile_package", return_value={"subject": "Fresh", "script": "Narration"}),
        patch.object(scheduler, "render_and_publish", side_effect=OSError("network")) as render,
    ):
        profiles.run_profile_pipeline(row["id"], "05:00", entry=entry)
        profiles.run_profile_pipeline(row["id"], "05:00", entry=dict(entry))
    render.assert_called_once()
    assert profiles._ledger()[entry["run_id"]]["status"] == "failed"


def test_restart_marks_running_interrupted_but_keeps_queued():
    _claim([_profile(), _profile("second")])
    first, second = list(profiles._ledger().values())
    first["status"] = "running"
    profiles._record(first)
    profiles.recover_interrupted()
    assert profiles._ledger()[first["run_id"]]["status"] == "interrupted"
    assert profiles._ledger()[second["run_id"]]["status"] == "queued"
    _claim([_profile(), _profile("second")])
    assert len(profiles._ledger()) == 2


def test_previous_profile_history_is_imported_to_prevent_upgrade_replay():
    run_id = f"account-the_afterframe-{_now().date()}-05:00"
    scheduler._record({"run_id": run_id, "profile_id": "the_afterframe", "slot": "05:00", "status": "complete"})
    profiles.recover_interrupted()
    _claim([_profile()])
    assert len(profiles._ledger()) == 1
    assert profiles._ledger()[run_id]["status"] == "complete"


def test_disabled_or_deleted_profile_cancels_queued_run():
    _claim([_profile()])
    entry = next(iter(profiles._ledger().values()))
    with patch.object(profiles.accounts, "list_profiles", return_value=[]), patch.object(scheduler, "preflight") as preflight:
        assert profiles.run_profile_pipeline(entry["profile_id"], entry["slot"], entry=entry)
    preflight.assert_not_called()
    assert profiles._ledger()[entry["run_id"]]["status"] == "cancelled"


def test_overview_uses_each_accounts_next_ist_time():
    profiles.set_master_enabled(True)
    now = _now("06:00")
    rows = profiles.overview([_profile(), _profile("mind_vault_paradox", ["14:00"])], now)
    assert "20:00 IST" in rows[0]["Next Run (IST)"]
    assert "14:00 IST" in rows[1]["Next Run (IST)"]
    profiles.set_master_enabled(False)
    assert all(row["Next Run (IST)"] == "Standby" for row in profiles.overview([_profile()], now))


def test_stage_log_is_written_without_credentials(storage):
    profiles._event({"profile_id": "the_afterframe", "status": "failed", "stage": "publish", "secret": "never-log"})
    text = (storage / "logs" / "autoflow_agent.log").read_text()
    assert "the_afterframe" in text
    assert "never-log" not in text
