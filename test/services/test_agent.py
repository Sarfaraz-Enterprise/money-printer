import json
from unittest.mock import patch

import pytest

from app.services import agent


PACKAGE = {
    "subject": "Small Habits, Big Changes",
    "language": "en-US",
    "script": "Start with one small habit today.",
    "keywords": "morning sunrise, person walking, notebook",
    "description": "Build momentum, one step at a time. #Habits",
}


def test_complete_package_uses_configured_llm_and_strips_fences():
    snapshot = {"llm_provider": "anthropic"}
    with patch.object(
        agent.llm, "_generate_response",
        return_value="```json\n" + json.dumps(PACKAGE) + "\n```",
    ) as generate:
        result = agent.generate_complete_video_package("Habits", app_config=snapshot)
    assert result == PACKAGE
    assert generate.call_args.kwargs["app_config"] is snapshot
    assert '"topic": "Habits"' in generate.call_args.args[0]


def test_empty_topic_asks_for_evergreen_not_unverified_trends():
    with patch.object(agent.llm, "_generate_response", return_value=json.dumps(PACKAGE)) as generate:
        agent.generate_complete_video_package("")
    assert "do not claim" in generate.call_args.args[0]
    assert '"topic": ""' in generate.call_args.args[0]


@pytest.mark.parametrize("response", [
    "", "Error: invalid API key", "not JSON", "[]",
    json.dumps({**PACKAGE, "script": ""}),
    json.dumps({**PACKAGE, "language": "fr-FR"}),
    json.dumps({**PACKAGE, "keywords": ["forest"]}),
    json.dumps({**PACKAGE, "subject": "x" * 301}),
    json.dumps({key: value for key, value in PACKAGE.items() if key != "description"}),
])
def test_invalid_package_is_rejected(response):
    with patch.object(agent.llm, "_generate_response", return_value=response):
        with pytest.raises(agent.ContentAgentError):
            agent.generate_complete_video_package("Habits")
