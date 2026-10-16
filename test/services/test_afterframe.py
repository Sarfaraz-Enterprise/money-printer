import json
from unittest.mock import patch

import pytest

from app.services import agent, scheduler


STORY = {
    "subject": "The Reflection Echo",
    "hook": "My reflection blinked before I did.",
    "script": (
        "My reflection blinked before I did. I covered the bathroom mirror, but every "
        "window showed him waiting, one second ahead. At midnight, he raised a finger "
        "to his lips, then pointed behind me. The apartment was empty, except for wet "
        "footprints leading from the locked front door. I turned back, and he was crying. "
        "Then I noticed the footprints started beneath my feet. He wasn't predicting my "
        "movements. He was remembering them, begging me not to open the door again."
    ),
    "visual_keywords": "dark bathroom mirror, rain against window night, shadowy apartment, wet footprints, locked door, crying silhouette",
    "tags": agent.AFTERFRAME_TAGS,
}


def test_brand_prompt_and_exact_output_schema():
    snapshot = {"llm_provider": "anthropic"}
    with patch.object(agent.llm, "_generate_response", return_value=json.dumps(STORY)) as generate:
        result = agent.generate_afterframe_story("Doppelgängers", app_config=snapshot)
    assert result == STORY
    prompt = generate.call_args.args[0]
    assert prompt.startswith(agent.AFTERFRAME_SYSTEM_PROMPT)
    assert "75 to 95 words" in prompt
    assert "Doppelgängers" in prompt
    assert generate.call_args.kwargs["app_config"] is snapshot


@pytest.mark.parametrize("changes", [
    {"subject": "x" * 40}, {"script": "Too short."},
    {"script": "word " * 96}, {"hook": "Not the opening."},
    {"visual_keywords": "business, office"}, {"tags": ["Generic"]},
    {"extra": "unexpected"}, {"script": 123},
])
def test_invalid_stories_rejected(changes):
    with patch.object(agent.llm, "_generate_response", return_value=json.dumps({**STORY, **changes})):
        with pytest.raises(agent.ContentAgentError):
            agent.generate_afterframe_story()


def test_recent_story_rejected_and_history_passed_to_prompt():
    with patch.object(agent.llm, "_generate_response", return_value=json.dumps(STORY)) as generate:
        with pytest.raises(agent.ContentAgentError, match="repeated"):
            agent.generate_afterframe_story(recent_stories=[STORY])
    assert "recent_stories_to_avoid" in generate.call_args.args[0]


def test_scheduler_uses_brand_history_and_visual_keywords(tmp_path):
    with (
        patch.object(scheduler, "STORAGE", tmp_path),
        patch.object(scheduler, "recent_history", return_value=[STORY]),
        patch.object(agent, "generate_afterframe_story", return_value=STORY) as generate,
    ):
        result = scheduler._generate_package(scheduler.ScheduleSettings(sub_theme_focus="Time loops"))
    assert generate.call_args.kwargs["recent_stories"][0]["subject"] == STORY["subject"]
    assert result["keywords"] == STORY["visual_keywords"]
    assert result["subject"] == STORY["subject"]
    assert result["language"] == "en-US"
