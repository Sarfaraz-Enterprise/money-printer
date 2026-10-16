from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services import accounts, profile_schedules, scheduler

MAIN = Path(__file__).resolve().parents[2] / "webui.py"
PROFILE = {
    "id": "the_afterframe", "name": "The Afterframe", "youtube": {}, "facebook": {}, "instagram": {},
    "enabled_platforms": ["youtube"],
    "schedule": {"enabled": True, "post_times": ["07:00", "14:00", "21:00"], "timezone": "Asia/Kolkata"},
}


def test_schedule_overview_has_no_shared_slot_controls_or_worker_side_effects(tmp_path):
    with (
        patch.object(scheduler, "STORAGE", tmp_path),
        patch.object(accounts, "list_profiles", return_value=[PROFILE]),
        patch.object(accounts, "save_profile") as save,
        patch.object(scheduler.SchedulerService, "start") as start,
    ):
        app = AppTest.from_file(str(MAIN), default_timeout=60).run()
    assert not app.exception
    assert any("Auto-Pilot Scheduler" in item.label for item in app.tabs)
    assert not app.time_input
    assert not any("Slot" in item.label for item in app.multiselect)
    save.assert_not_called()
    start.assert_not_called()


def test_profile_editor_saves_three_custom_daily_times_without_touching_other_profiles():
    with patch.object(accounts, "save_profile") as save:
        app = AppTest.from_string(
            "from webui.components.accounts_manager import _profile_form\n"
            f"_profile_form({PROFILE!r})\n", default_timeout=60,
        ).run()
        assert not app.exception
        assert [item.value.strftime("%H:%M") for item in app.time_input] == ["07:00", "14:00", "21:00"]
        next(item for item in app.button if item.label == "Save Profile").click().run()
        assert not app.exception
    payload = save.call_args.args[0]
    assert payload["id"] == "the_afterframe"
    assert payload["schedule"]["post_times_ist"] == ["07:00", "14:00", "21:00"]
    assert payload["schedule"]["enabled"]


def test_legacy_panel_time_display_remains_compatible(tmp_path):
    timestamp = "2026-07-15T05:00:00-04:00"
    status = {"worker_alive": True, "running": True, "timezone": "America/New_York", "next_runs": [timestamp]}
    with patch.object(scheduler, "STORAGE", tmp_path), patch.object(scheduler, "scheduler_status", return_value=status):
        app = AppTest.from_string(
            "from webui.components.autopilot import render_autopilot_panel\n"
            "render_autopilot_panel(lambda key: key)\n", default_timeout=60,
        ).run()
    assert not app.exception
    text = "\n".join(str(item.value) for item in app.markdown)
    assert "2026-07-15 05:00 EDT" in text
    assert datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M %Z") in text
