from pathlib import Path

from app.services import story_memory
from app.utils import video_outputs


def test_sqlite_topic_reservation_survives_json_mirror_loss():
    package = {"subject": "A Singular Clock", "script": "The clock stopped at noon.", "keywords": "clock"}
    assert story_memory.reserve("the_afterframe", package, {"status": "passed"})
    history = Path(video_outputs.profile_dir("the_afterframe")) / "history.json"
    history.unlink()

    assert any(row["subject"] == package["subject"] for row in story_memory.previous("the_afterframe"))
    assert not story_memory.reserve("the_afterframe", package, {"status": "passed"})
