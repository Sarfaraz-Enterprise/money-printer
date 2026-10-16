import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import pyarrow as pa
from streamlit.testing.v1 import AppTest

from app.config import env_secrets
from app.services import (
    accounts, agent, autoflow_settings, autoflow_studio, content_quality,
    profile_content, profile_schedules, scheduler, story_memory,
)

SCRIPT = (
    "My reflection whispered tomorrow's date. At midnight, every mirror in the apartment "
    "turned toward the locked nursery. I lived alone, but someone inside hummed the "
    "song my mother forgot before she vanished. Beneath the door, a photograph slid "
    "across the floor, showing me asleep beside a child whose face was mine. "
    "The clock stopped. A tiny hand knocked from the other side, exactly five times. "
    "Then my reflection stepped away, and the nursery door opened into the room "
    "where I had been born."
)
PACKAGE = {"subject": "The Empty Nursery", "script": SCRIPT, "language": "en-US",
           "keywords": "dark hallway, rain at night, mirror, nursery, analog clock",
           "description": "A chilling mystery"}
QA_JSON = '{"high_curiosity_opening":true,"suspenseful_pacing":true}'
ROOT = Path(__file__).resolve().parents[2]


def _profile(**changes):
    return accounts.save_profile({
        "id": "the_afterframe", "name": "The Afterframe", "category": "Sci-Fi Mystery",
        "target_audience": "United States (US Tier-1)", "audience_tags": ["US", "Sci-Fi"],
        "timezone": "America/New_York", "system_prompt": "A two-second hook and logical twist",
        "visual_keywords_preset": "dark neon laboratory, silhouette in fog",
        "voice_direction": "Low, deliberate American English with natural pauses",
        "voice_name": "elevenlabs:custom_voice:Private narrator",
        "schedule": {"enabled": True, "post_times_ist": ["05:00", "19:30"]},
        "destinations": {"youtube": True, "instagram": False, "facebook": False},
        **changes,
    })


def test_full_profile_schema_roundtrip_preserves_new_and_old_aliases():
    profile = _profile()
    edited = accounts.save_profile({"id": profile["id"], "name": "The Afterframe Channel"})
    for key in ("target_audience", "audience_tags", "timezone", "system_prompt",
                "visual_keywords_preset", "voice_direction", "voice_name"):
        assert edited[key] == profile[key]
    assert edited["system_prompt"] == edited["niche_prompt"]
    assert edited["visual_keywords_preset"] == edited["visual_style"]
    assert edited["schedule"]["post_times_ist"] == ["05:00", "19:30"]
    assert edited["schedule"]["slots"] == edited["schedule"]["post_times"]
    settings = profile_schedules._settings(edited)
    assert settings.timezone == "Asia/Kolkata"
    assert settings.voice_name == "elevenlabs:custom_voice:Private narrator"
    accounts.save_profile({"id": edited["id"], "niche_prompt": "Updated old client brief",
                           "schedule": {"post_times": ["14:00"]}})
    updated = accounts.list_profiles()[0]
    assert updated["system_prompt"] == "Updated old client brief"
    assert updated["schedule"]["post_times_ist"] == ["14:00"]


@pytest.mark.parametrize("changes", [{"timezone": "bad/zone"}, {"voice_name": "edge-voice"},
                                    {"voice_name": "elevenlabs:../bad:voice"}, {"audience_tags": "not-a-list"}])
def test_profile_rejects_invalid_audience_or_voice(changes):
    with pytest.raises(ValueError):
        _profile(**changes)


def test_quality_checks_exact_word_count_and_first_five_words():
    with patch.object(content_quality.llm, "_generate_response", return_value=QA_JSON) as review:
        result = content_quality.evaluate(PACKAGE, app_config={})
    assert 75 <= result["word_count"] <= 95
    assert result["opening_beat"] == "My reflection whispered tomorrow's date"
    prompt = review.call_args.args[0]
    assert "opening_beat" in prompt
    assert "Judge only the opening beat" in prompt


@pytest.mark.parametrize("count", [0, 74, 96])
def test_word_count_rejected_before_paid_quality_call(count):
    with patch.object(content_quality.llm, "_generate_response") as review:
        with pytest.raises(agent.ContentAgentError, match="75-95"):
            content_quality.evaluate({**PACKAGE, "script": "word " * count}, app_config={})
    review.assert_not_called()


@pytest.mark.parametrize("count", [75, 95])
def test_word_count_endpoints_accepted(count):
    with patch.object(content_quality.llm, "_generate_response", return_value=QA_JSON):
        assert content_quality.evaluate({**PACKAGE, "script": "word " * count}, app_config={})["word_count"] == count


@pytest.mark.parametrize("answer", [
    "not json", '{"high_curiosity_opening":false,"suspenseful_pacing":true}',
    '{"high_curiosity_opening":1,"suspenseful_pacing":true}',
    '{"high_curiosity_opening":true}', '{"high_curiosity_opening":true,"suspenseful_pacing":false}',
])
def test_qa_fails_closed_for_bad_reviews(answer):
    with patch.object(content_quality.llm, "_generate_response", return_value=answer):
        with pytest.raises(agent.ContentAgentError):
            content_quality.evaluate(PACKAGE, app_config={})


def test_history_rejects_duplicate_title_or_script_and_survives_reload():
    assert story_memory.reserve("the_afterframe", PACKAGE, {"word_count": 87})
    assert not story_memory.reserve("the_afterframe", {**PACKAGE, "subject": "A different title"}, {})
    assert not story_memory.reserve("the_afterframe", {**PACKAGE, "script": "Different narration"}, {})
    assert not story_memory.reserve("the_afterframe", {**PACKAGE, "subject": "Different",
                                                     "script": SCRIPT.upper().replace(".", "!")}, {})
    rows = story_memory.previous("the_afterframe")
    assert rows[0]["script"] == SCRIPT
    assert len(rows[0]["script_hash"]) == 64
    assert story_memory.reserve("mind_vault_paradox", PACKAGE, {})  # Memory is profile-isolated.


def test_concurrent_reservations_accept_only_one_copy():
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: story_memory.reserve("the_afterframe", PACKAGE, {}), range(4)))
    assert outcomes.count(True) == 1
    assert len(story_memory.previous("the_afterframe")) == 1


def test_corrupt_story_memory_blocks_generation_without_overwrite():
    directory = Path(profile_content.video_outputs.profile_dir("the_afterframe"))
    target = directory / "history.json"
    target.write_text("broken")
    with patch.object(profile_content, "_generate") as generate:
        with pytest.raises(ValueError, match="corrupt"):
            profile_content.generate_profile_package({"id": "the_afterframe"})
    generate.assert_not_called()
    assert target.read_text() == "broken"


def test_generation_enforces_qa_and_stores_memory_before_returning():
    profile = _profile()
    story = {"subject": PACKAGE["subject"], "script": SCRIPT, "hook": "My reflection whispered tomorrow's date.",
             "visual_keywords": PACKAGE["keywords"], "tags": agent.AFTERFRAME_TAGS}
    with (
        patch.object(agent, "generate_afterframe_story", return_value=story) as write,
        patch.object(content_quality.llm, "_generate_response", return_value=QA_JSON),
    ):
        result = profile_content.generate_profile_package(profile)
    assert result["script"] == SCRIPT
    assert len(story_memory.previous(profile["id"])) == 1
    brief = write.call_args.kwargs["creative_brief"]
    for text in ("America/New_York", "US Tier-1", "dark neon laboratory", "Low, deliberate", "logical twist"):
        assert text in brief


def test_qa_failure_is_bounded_and_never_reserved():
    profile = _profile()
    with patch.object(profile_content, "_generate", return_value=PACKAGE) as generate, patch.object(content_quality.llm, "_generate_response", return_value='{"high_curiosity_opening":false,"suspenseful_pacing":true}'):
        with pytest.raises(agent.ContentAgentError, match="quality gate"):
            profile_content.generate_profile_package(profile)
    assert generate.call_count == 3
    assert story_memory.previous(profile["id"]) == []


def test_profile_editor_and_timeline_support_noncommon_valid_timezone():
    profile = _profile(timezone="Etc/GMT+5")
    app = AppTest.from_string(
        "from app.services import accounts\n"
        "from webui.components.accounts_manager import _profile_form\n"
        "_profile_form(accounts.list_profiles()[0])\n"
    ).run(timeout=60)
    assert not app.exception
    assert next(item for item in app.selectbox if item.label == "Audience Timezone").value == "Etc/GMT+5"
    assert profile_schedules.timeline([profile])[0]["Audience Time"]


def test_repeated_script_with_new_title_never_reaches_quality_gate():
    profile = _profile()
    story_memory.reserve(profile["id"], PACKAGE, {})
    with patch.object(profile_content, "_generate", return_value={**PACKAGE, "subject": "A New Title"}) as write, patch.object(content_quality, "evaluate") as qa:
        with pytest.raises(agent.ContentAgentError, match="repeat"):
            profile_content.generate_profile_package(profile)
    assert write.call_count == 3
    qa.assert_not_called()


def test_health_checks_configured_services_without_exposing_keys(private_test_dotenv):
    private_test_dotenv.write_text("\n".join(f"{name}=secret-{name}" for name in env_secrets.GLOBAL_FIELDS
                                           if name != "ELEVENLABS_VOICE_ID") + "\nELEVENLABS_VOICE_ID=validVoice\n")
    responses = []

    def get(*args, **kwargs):
        response = Mock(status_code=200)
        responses.append(response)
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        return response

    with patch.object(autoflow_settings.requests, "get", side_effect=get):
        result = autoflow_settings.diagnostics()
    assert len(result) == 8
    assert len(responses) == 7
    assert all(row["Latency (ms)"] is not None for row in result)
    assert "secret-" not in json.dumps(result)
    assert next(row for row in result if row["Service"] == "Google OAuth")["Status"] == "🟡 OAuth consent required"
    assert all(response.close.call_count == 1 for response in responses)


def test_health_network_error_contains_no_provider_exception(private_test_dotenv):
    private_test_dotenv.write_text("OPENAI_API_KEY=private-key\n")
    with patch.object(autoflow_settings.requests, "get", side_effect=autoflow_settings.requests.ConnectionError("private-key")):
        rows = autoflow_settings.diagnostics()
    openai = next(row for row in rows if row["Service"] == "OpenAI")
    assert openai["Status"] == "🔴 Error"
    assert openai["Latency (ms)"] is not None
    assert "private-key" not in str(rows)


def test_google_oauth_read_validation_uses_linked_profile(private_test_dotenv):
    private_test_dotenv.write_text("GOOGLE_CLIENT_ID=client\nGOOGLE_CLIENT_SECRET=secret\n")
    _profile(youtube={"refresh_token": "private-refresh"})
    with patch.object(accounts, "_refresh_youtube_token", return_value=("private-access", "ok")), patch.object(autoflow_settings.requests, "get", return_value=Mock(status_code=200)) as get:
        row = autoflow_settings._probe("Google OAuth", env_secrets.global_values())
    assert row["Status"] == "🟢 Connected via .env"
    assert "private-access" not in json.dumps(row)
    assert get.call_args.args[0].startswith("https://www.googleapis.com/youtube/")


def test_ist_timeline_does_not_shift_posting_times_for_us_dst():
    profile = _profile()
    profile_schedules.set_master_enabled(True)
    for month, expected_zone in [(1, "EST"), (7, "EDT")]:
        now = profile_schedules.IST.localize(datetime(2026, month, 15, 4, 0))
        timeline = profile_schedules.timeline([profile], now)
        assert [row["Time (IST)"] for row in timeline] == ["05:00", "19:30"]
        assert expected_zone in timeline[0]["Audience Time"]
        assert profile_schedules.next_trigger([profile], now).hour == 5


def test_timeline_chart_encodes_actual_profile_and_time_columns():
    _profile()
    app = AppTest.from_string("from webui.dashboard import _agent_overview\n_agent_overview()\n").run(timeout=60)
    assert not app.exception
    data = pa.ipc.open_stream(app.get("vega_lite_chart")[0].proto.data.data).read_all()
    assert data.column("Time (IST)").to_pylist() == ["05:00", "19:30"]
    assert data.column("Profile").to_pylist() == ["The Afterframe", "The Afterframe"]


def test_empty_profile_timeline_does_not_serialize_empty_arrow_table():
    with patch.object(accounts, "list_profiles", return_value=[]):
        app = AppTest.from_string("from webui.dashboard import _agent_overview\n_agent_overview()\n").run(timeout=60)
        assert not app.exception
        assert not app.dataframe


def test_generate_concept_is_one_click_private_test_flight():
    profile = _profile()
    with patch.object(autoflow_studio, "submit", return_value="test-flight") as submit, patch("webui.dashboard.state.state.get_task", return_value={"state": 4}):
        app = AppTest.from_file(str(ROOT / "webui.py"), default_timeout=60).run()
        next(item for item in app.toggle if item.key == "af-authorize").set_value(True)
        next(item for item in app.button if item.label == "Generate Concept").click().run()
    assert not app.exception
    args = submit.call_args.args
    assert args[:3] == (profile["id"], "", "")
    assert args[3] == profile["voice_name"]
    assert args[5:] == ([], False)


def test_read_only_health_board_has_no_key_entry_or_config_write(private_test_dotenv):
    private_test_dotenv.write_text("OPENAI_API_KEY=private-key\n")
    with patch.object(autoflow_settings, "save_vault") as save, patch.object(autoflow_settings, "diagnostics", return_value=[{"Service": "OpenAI", "Status": "OK", "Latency (ms)": 1.2}]):
        app = AppTest.from_string("from webui.dashboard import _vault\n_vault()\n").run(timeout=60)
        assert not app.text_input and not app.text_area and not app.selectbox
        next(item for item in app.button if item.label == "Run Health Check").click().run()
        assert not app.exception
        assert app.dataframe[0].value["Latency (ms)"].iloc[0] == 1.2
    save.assert_not_called()
    assert private_test_dotenv.read_text() == "OPENAI_API_KEY=private-key\n"
