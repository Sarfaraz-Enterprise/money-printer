import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from app.services import job_store, scheduler


@pytest.fixture
def isolated_store():
    with TemporaryDirectory() as directory, patch.object(scheduler, "STORAGE", Path(directory)):
        job_store.initialize()
        yield Path(directory)


def _slot_job():
    return {
        "run_id": "account-profile-2026-10-17-05:00",
        "profile_id": "profile",
        "date": "2026-10-17",
        "slot": "05:00",
        "status": "queued",
        "stage": "queued",
    }


def test_initialize_closes_connection_and_timestamps_are_filled(isolated_store):
    job_store.initialize()
    db = isolated_store / "autoflow.sqlite3"
    job_store.record_job({"job_id": "manual", "profile_id": "profile", "status": "running", "stage": "render"})
    connection = sqlite3.connect(db)
    try:
        created_at = connection.execute(
            "SELECT created_at FROM jobs WHERE job_id='manual'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert created_at
    db.rename(isolated_store / "renamed.sqlite3")


def test_child_task_keeps_parent_workflow_link(isolated_store):
    job_store.record_job({
        "job_id": "workflow", "profile_id": "profile",
        "status": "running", "stage": "render",
    })
    job_store.record_job({
        "job_id": "render-task", "parent_job_id": "workflow",
        "profile_id": "profile", "status": "running", "stage": "preflight",
    })
    job_store.record_job({
        "job_id": "render-task", "profile_id": "profile",
        "status": "complete", "stage": "complete",
    })

    assert job_store.get_job("render-task")["parent_job_id"] == "workflow"
    assert next(row for row in job_store.recent_jobs() if row["job_id"] == "render-task")[
        "parent_job_id"
    ] == "workflow"


def test_slot_claim_is_unique_and_survives_reopening(isolated_store):
    first_claimed, first = job_store.claim_job(_slot_job())
    duplicate_claimed, duplicate = job_store.claim_job(_slot_job())

    assert first_claimed
    assert not duplicate_claimed
    assert first["job_id"] == duplicate["job_id"] == _slot_job()["run_id"]
    assert job_store.get_slot("profile:2026-10-17:05:00")["status"] == "queued"


def test_concurrent_slot_claims_have_one_winner(isolated_store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: job_store.claim_job(_slot_job()), range(8)))

    assert sum(created for created, _ in results) == 1
    assert len({row["job_id"] for _, row in results}) == 1


def test_artifact_checksum_is_persisted(isolated_store):
    job_store.record_job({"job_id": "artifact-job", "profile_id": "profile", "status": "running", "stage": "render"})
    artifact = isolated_store / "final.mp4"
    artifact.write_bytes(b"private render bytes")

    job_store.record_artifact("artifact-job", "final_video", str(artifact))

    assert job_store.artifacts("artifact-job")[0]["checksum"] == hashlib.sha256(
        b"private render bytes"
    ).hexdigest()


def test_topic_reservation_rejects_duplicate_subject_or_script(isolated_store):
    args = ("profile", "subject-hash", "script-hash", "A subject", "A script", "2026-10-17T00:00:00Z")
    assert job_store.reserve_topic(*args)
    assert not job_store.reserve_topic(*args)
    assert not job_store.reserve_topic(
        "profile", "another-subject", "script-hash", "Another subject", "A script", "2026-10-17T00:00:00Z"
    )


def test_post_outbox_prevents_duplicate_publication_and_keeps_success(isolated_store):
    job_store.record_job({"job_id": "post-job", "profile_id": "profile", "status": "running", "stage": "publish"})
    first = job_store.reserve_post("post-job", "profile", "youtube")
    job_store.finish_post("post-job", "profile", "youtube", status="success", external_id="video-1")
    retry = job_store.reserve_post("post-job", "profile", "youtube")

    assert first["created"]
    assert not retry["created"]
    assert retry["status"] == "success"
    assert retry["external_id"] == "video-1"


def test_corrupt_database_errors_are_not_silenced(isolated_store):
    path = isolated_store / "autoflow.sqlite3"
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        job_store.summary()


def test_stage_checkpoints_cache_only_matching_inputs(isolated_store):
    job_store.record_job({"job_id": "stage-job", "profile_id": "profile", "status": "running", "stage": "script"})
    inputs = {"topic": "Clock"}
    job_store.stage_begin("stage-job", "script", inputs)
    job_store.stage_succeed("stage-job", "script", {"script": "The clock stopped."}, input_data=inputs)

    assert job_store.cached_output("stage-job", "script", inputs) == {"script": "The clock stopped."}
    assert job_store.cached_output("stage-job", "script", {"topic": "Different"}) is None
    assert job_store.recent_stages()[0]["status"] == "succeeded"


def test_cached_stage_requires_matching_artifact_hash(isolated_store):
    job_store.record_job({"job_id": "artifact-stage-job", "profile_id": "profile", "status": "running", "stage": "render"})
    video = isolated_store / "render.mp4"
    video.write_bytes(b"original")
    job_store.stage_begin("artifact-stage-job", "render", {"subject": "One"})
    job_store.stage_succeed(
        "artifact-stage-job", "render", {"video": str(video)},
        input_data={"subject": "One"}, artifacts={"final": str(video)},
    )
    assert job_store.cached_output("artifact-stage-job", "render", {"subject": "One"})

    video.write_bytes(b"tampered")
    assert job_store.valid_artifact("artifact-stage-job", "render", "render", "final") is None
    assert job_store.cached_output("artifact-stage-job", "render", {"subject": "One"}) is None


def test_recovery_marks_old_upload_uncertain_without_touching_recent_jobs(isolated_store):
    old = "2020-01-01T00:00:00+00:00"
    job_store.record_job({
        "job_id": "stuck-job", "profile_id": "profile", "status": "running",
        "stage": "publish", "started_at": old, "updated_at": old,
    })
    job_store.reserve_post("stuck-job", "profile", "youtube")
    with job_store._connect() as connection:
        connection.execute(
            "UPDATE posts SET attempted_at=?,updated_at=? WHERE job_id='stuck-job'",
            (old, old),
        )
    job_store.record_job({
        "job_id": "active-job", "profile_id": "profile", "status": "running",
        "stage": "render",
    })

    recovered = job_store.recover_stuck(stale_after_seconds=60)

    assert recovered == {"jobs_interrupted": 1, "stages_interrupted": 0, "posts_uncertain": 1}
    assert job_store.posts("stuck-job")[0]["status"] == "uncertain"
    assert job_store.get_job("stuck-job")["status"] == "uncertain"
    assert job_store.get_job("active-job")["status"] == "running"


def test_daily_video_count_excludes_cancelled_and_budget_blocked(isolated_store):
    for job_id, status in (("one", "complete"), ("two", "cancelled"), ("three", "budget_blocked")):
        job_store.record_job({
            "job_id": job_id, "profile_id": "profile", "date": "2026-10-17",
            "status": status, "stage": "complete",
        })
    assert job_store.videos_today("profile", "2026-10-17") == 1


def test_external_platform_id_cannot_be_attached_to_two_jobs(isolated_store):
    for job_id, profile_id in (("post-one", "profile-one"), ("post-two", "profile-two")):
        job_store.record_job({"job_id": job_id, "profile_id": profile_id, "status": "running", "stage": "publish"})
        job_store.reserve_post(job_id, profile_id, "youtube")
    job_store.finish_post(
        "post-one", "profile-one", "youtube", status="success",
        external_id="youtube-video-id",
    )

    with pytest.raises(ValueError, match="already recorded"):
        job_store.finish_post(
            "post-two", "profile-two", "youtube", status="success",
            external_id="youtube-video-id",
        )


def test_additive_migration_preserves_existing_ledger_tables(isolated_store, monkeypatch):
    legacy_root = isolated_store / "legacy"
    legacy_root.mkdir()
    db_path = legacy_root / "autoflow.sqlite3"
    old_artifact = legacy_root / "old-final.mp4"
    old_artifact.write_bytes(b"legacy video")
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY, slot_key TEXT UNIQUE, profile_id TEXT NOT NULL,
                scheduled_date TEXT, slot TEXT, status TEXT NOT NULL, stage TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0, error_class TEXT, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE artifacts (
                job_id TEXT NOT NULL REFERENCES jobs(job_id), stage TEXT NOT NULL,
                uri TEXT NOT NULL, checksum TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(job_id,stage)
            );
            CREATE TABLE posts (
                job_id TEXT NOT NULL REFERENCES jobs(job_id), profile_id TEXT NOT NULL,
                platform TEXT NOT NULL, status TEXT NOT NULL, external_id TEXT, post_url TEXT,
                error_class TEXT, attempted_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(job_id,profile_id,platform)
            );
            CREATE TABLE topic_history (
                profile_id TEXT NOT NULL, subject_hash TEXT NOT NULL, script_hash TEXT NOT NULL,
                subject TEXT NOT NULL, script TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(profile_id,subject_hash), UNIQUE(profile_id,script_hash)
            );
            CREATE TABLE job_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(job_id),
                stage TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, details_json TEXT NOT NULL
            );
            INSERT INTO jobs VALUES(
                'old-job',NULL,'profile',NULL,NULL,'complete','render',1,NULL,'{}',
                '2026-10-16T00:00:00+00:00','2026-10-16T00:00:00+00:00'
            );
        """)
        connection.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?)",
            ("old-job", "final_video", str(old_artifact), hashlib.sha256(b"legacy video").hexdigest(), "2026-10-16T00:00:00+00:00"),
        )
    connection.close()
    with patch.object(scheduler, "STORAGE", legacy_root):
        job_store.initialize()
        assert job_store.get_job("old-job")["status"] == "complete"
        assert job_store.valid_artifact("old-job", "final_video") == old_artifact
        with job_store._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(posts)")}
            job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            stage_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_stages'"
            ).fetchone()
    assert {"meta_json", "error_message"} <= columns
    assert {"parent_job_id", "finished_at", "trigger_kind"} <= job_columns
    assert stage_table is not None
