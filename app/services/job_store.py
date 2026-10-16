"""Portable transactional job, artifact, topic and publish ledgers."""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from app.config import config
from app.utils import utils


def database_path() -> Path:
    configured = str(config.app.get("autoflow_database_path") or "").strip()
    if configured:
        path = Path(os.path.expanduser(configured))
        if not path.is_absolute():
            path = Path(utils.root_dir()) / path
    else:
        from app.services import scheduler
        path = Path(scheduler.STORAGE) / "autoflow.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _open() -> sqlite3.Connection:
    path = database_path()
    connection = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            parent_job_id TEXT,
            slot_key TEXT UNIQUE,
            profile_id TEXT NOT NULL,
            scheduled_date TEXT,
            slot TEXT,
            status TEXT NOT NULL,
            stage TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            error_class TEXT,
            error_message TEXT,
            trigger_kind TEXT NOT NULL DEFAULT 'scheduled',
            finished_at TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS jobs_profile_date ON jobs(profile_id, scheduled_date);
        CREATE INDEX IF NOT EXISTS jobs_status_updated ON jobs(status, updated_at);
        CREATE TABLE IF NOT EXISTS artifacts (
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            stage TEXT NOT NULL,
            uri TEXT NOT NULL,
            checksum TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(job_id, stage)
        );
        CREATE TABLE IF NOT EXISTS posts (
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            profile_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            status TEXT NOT NULL,
            external_id TEXT,
            post_url TEXT,
            error_class TEXT,
            error_message TEXT,
            meta_json TEXT NOT NULL DEFAULT '{}',
            attempted_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(job_id, profile_id, platform)
        );
        CREATE TABLE IF NOT EXISTS job_stages (
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            input_hash TEXT,
            output_json TEXT,
            cacheable INTEGER NOT NULL DEFAULT 1,
            error_class TEXT,
            error_message TEXT,
            started_at TEXT,
            finished_at TEXT,
            PRIMARY KEY(job_id, stage)
        );
        CREATE INDEX IF NOT EXISTS job_stages_status ON job_stages(status, started_at);
        CREATE TABLE IF NOT EXISTS job_artifacts (
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            stage TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            uri TEXT NOT NULL,
            checksum TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(job_id, stage, kind, name)
        );
        CREATE TABLE IF NOT EXISTS topic_history (
            profile_id TEXT NOT NULL,
            subject_hash TEXT NOT NULL,
            script_hash TEXT NOT NULL,
            subject TEXT NOT NULL,
            script TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(profile_id, subject_hash),
            UNIQUE(profile_id, script_hash)
        );
        CREATE TABLE IF NOT EXISTS job_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        """)
        _ensure_columns(connection, "jobs", {
            "parent_job_id": "TEXT",
            "error_message": "TEXT",
            "trigger_kind": "TEXT NOT NULL DEFAULT 'scheduled'",
            "finished_at": "TEXT",
        })
        _ensure_columns(connection, "posts", {
            "error_message": "TEXT",
            "meta_json": "TEXT NOT NULL DEFAULT '{}'",
        })
        _ensure_columns(connection, "job_stages", {
            "cacheable": "INTEGER NOT NULL DEFAULT 1",
        })
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if schema_version < 2:
            _migrate_legacy_artifacts(connection)
        if schema_version < 3:
            connection.execute("PRAGMA user_version=3")
        if not path.exists():
            raise OSError("Job ledger was not created")
        if os.name != "nt":
            os.chmod(path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def _connect() -> Generator[sqlite3.Connection, None, None]:
    connection = _open()
    try:
        yield connection
    finally:
        connection.close()


def initialize() -> None:
    with _connect():
        pass


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _migrate_legacy_artifacts(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT job_id,stage,uri,checksum,created_at FROM artifacts"):
        path = Path(row["uri"])
        if not path.is_file():
            continue
        connection.execute("""
            INSERT OR IGNORE INTO job_artifacts
            (job_id,stage,kind,name,uri,checksum,size_bytes,created_at)
            VALUES(?,?,'output','',?,?,?,?)
        """, (
            row["job_id"], row["stage"], _portable_uri(path), row["checksum"],
            path.stat().st_size, row["created_at"],
        ))


def _now(entry: dict) -> str:
    return str(
        entry.get("updated_at") or entry.get("finished_at") or entry.get("started_at")
        or datetime.now(timezone.utc).isoformat()
    )


def _job_from_row(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload_json"])
    columns = {key: row[key] for key in row.keys() if key != "payload_json"}
    return {**payload, **columns}


def get_job(job_id: str) -> dict | None:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    return _job_from_row(row)


def get_slot(slot_key: str) -> dict | None:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE slot_key=?", (slot_key,)).fetchone()
    return None if row is None else _job_from_row(row)


def claim_job(entry: dict) -> tuple[bool, dict]:
    payload = json.dumps(entry, ensure_ascii=False)
    now = _now(entry)
    job_id = str(entry.get("run_id") or entry["job_id"])
    profile = str(entry.get("profile_id") or "")
    slot_key = (f"{profile}:{entry['date']}:{entry['slot']}"
                if entry.get("date") and entry.get("slot") else None)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO jobs
                (job_id,slot_key,profile_id,scheduled_date,slot,status,stage,attempt,error_class,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (job_id, slot_key, profile, entry.get("date"), entry.get("slot"),
                  entry.get("status", "queued"), entry.get("stage", "queued"),
                  int(entry.get("attempts", 0)), entry.get("error_type"), payload, now, now))
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=? OR slot_key=?", (job_id, slot_key)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    result = _job_from_row(row)
    return cursor.rowcount == 1, result


def record_job(entry: dict) -> None:
    job_id = str(entry.get("run_id") or entry["job_id"])
    now = _now(entry)
    payload = json.dumps(entry, ensure_ascii=False)
    slot_key = (f"{entry['profile_id']}:{entry['date']}:{entry['slot']}"
                if entry.get("profile_id") and entry.get("date") and entry.get("slot") else None)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("""
                INSERT INTO jobs
                (job_id,parent_job_id,slot_key,profile_id,scheduled_date,slot,status,stage,attempt,error_class,error_message,
                 trigger_kind,finished_at,payload_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status, stage=excluded.stage, attempt=excluded.attempt,
                    error_class=excluded.error_class, error_message=excluded.error_message,
                    parent_job_id=COALESCE(excluded.parent_job_id,jobs.parent_job_id),
                    trigger_kind=excluded.trigger_kind,
                    finished_at=COALESCE(excluded.finished_at,jobs.finished_at),
                    payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """, (job_id, entry.get("parent_job_id"), slot_key, str(entry.get("profile_id") or ""),
                  entry.get("date"), entry.get("slot"), entry.get("status", "pending"),
                  entry.get("stage", "queued"), int(entry.get("attempts", 0)),
                  entry.get("error_type"), entry.get("error_message"),
                  "manual" if entry.get("manual") else "schedule",
                  entry.get("finished_at"), payload, now, now))
            connection.execute(
                "INSERT INTO job_events(job_id,stage,status,created_at,details_json) VALUES(?,?,?,?,?)",
                (job_id, entry.get("stage", "queued"), entry.get("status", "pending"),
                 now, json.dumps({key: entry[key] for key in ("error_type", "retry_at", "subject", "task_id") if key in entry})),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def recoverable_jobs() -> list[dict]:
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at").fetchall()
    return [_job_from_row(row) for row in rows]


def record_artifact(
    job_id: str,
    stage: str,
    uri: str,
    checksum: str = "",
    *,
    kind: str = "output",
    name: str = "",
) -> None:
    path = Path(uri)
    if not path.is_file():
        raise FileNotFoundError(f"Cannot record missing artifact for stage {stage!r}")
    size_bytes = path.stat().st_size
    if not checksum:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum = digest.hexdigest()
    stored_uri = _portable_uri(path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("""
                INSERT INTO job_artifacts(job_id,stage,kind,name,uri,checksum,size_bytes,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,stage,kind,name) DO UPDATE SET
                    uri=excluded.uri,checksum=excluded.checksum,size_bytes=excluded.size_bytes,created_at=excluded.created_at
            """, (job_id, stage, kind, name, stored_uri, checksum, size_bytes, now))
            connection.execute("""
                INSERT INTO artifacts(job_id,stage,uri,checksum,created_at) VALUES(?,?,?,?,?)
                ON CONFLICT(job_id,stage) DO UPDATE SET uri=excluded.uri, checksum=excluded.checksum, created_at=excluded.created_at
            """, (job_id, stage, str(path.resolve()), checksum, now))
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _portable_uri(path: Path) -> str:
    resolved = path.resolve()
    root = Path(utils.root_dir()).resolve()
    try:
        return "app://" + resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_uri(uri: str) -> Path:
    if uri.startswith("app://"):
        relative = Path(uri.removeprefix("app://"))
        root = Path(utils.root_dir()).resolve()
        resolved = (root / relative).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("Artifact URI escapes the application root")
        return resolved
    return Path(uri)


def valid_artifact(job_id: str, stage: str, kind: str = "output", name: str = "") -> Path | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT uri,checksum,size_bytes FROM job_artifacts "
            "WHERE job_id=? AND stage=? AND kind=? AND name=?",
            (job_id, stage, kind, name),
        ).fetchone()
    if row is None:
        return None
    path = _resolve_uri(row["uri"])
    if not path.is_file() or path.stat().st_size != row["size_bytes"]:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return path if digest.hexdigest() == row["checksum"] else None


def _input_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def cached_output(job_id: str, stage: str, input_data: Any = None) -> Any:
    with _connect() as connection:
        row = connection.execute(
            "SELECT input_hash,output_json,cacheable FROM job_stages "
            "WHERE job_id=? AND stage=? AND status='succeeded'",
            (job_id, stage),
        ).fetchone()
        artifacts = connection.execute(
            "SELECT kind,name FROM job_artifacts WHERE job_id=? AND stage=?",
            (job_id, stage),
        ).fetchall()
    if (row is None or not row["cacheable"]
            or (input_data is not None and row["input_hash"] != _input_hash(input_data))):
        return None
    for artifact in artifacts:
        if valid_artifact(job_id, stage, artifact["kind"], artifact["name"]) is None:
            return None
    value = json.loads(row["output_json"])
    return value


def stage_begin(job_id: str, stage: str, input_data: Any = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    input_hash = _input_hash(input_data) if input_data is not None else None
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            exists = connection.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Cannot begin stage for unknown job {job_id!r}")
            connection.execute("""
                INSERT INTO job_stages(job_id,stage,status,attempt,input_hash,started_at)
                VALUES(?,?,'running',1,?,?)
                ON CONFLICT(job_id,stage) DO UPDATE SET
                    status='running',attempt=job_stages.attempt+1,input_hash=excluded.input_hash,
                    output_json=NULL,error_class=NULL,error_message=NULL,started_at=excluded.started_at,finished_at=NULL
            """, (job_id, stage, input_hash, now))
            connection.execute(
                "DELETE FROM job_artifacts WHERE job_id=? AND stage=?",
                (job_id, stage),
            )
            connection.execute(
                "UPDATE jobs SET status='running',stage=?,updated_at=? WHERE job_id=?",
                (stage, now, job_id),
            )
            attempt = connection.execute(
                "SELECT attempt FROM job_stages WHERE job_id=? AND stage=?", (job_id, stage)
            ).fetchone()["attempt"]
            connection.execute(
                "INSERT INTO job_events(job_id,stage,status,created_at,details_json) VALUES(?,?,?,?,?)",
                (job_id, stage, "running", now, json.dumps({"attempt": attempt, "input_hash": input_hash})),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def stage_succeed(
    job_id: str,
    stage: str,
    output: Any,
    *,
    input_data: Any = None,
    artifacts: dict[str, str] | None = None,
    cacheable: bool = True,
) -> None:
    artifacts = artifacts or {}
    artifact_rows = []
    missing_artifact = False
    for name, uri in artifacts.items():
        path = Path(uri)
        if not path.is_file():
            missing_artifact = True
            continue
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        artifact_rows.append((name, _portable_uri(path), digest.hexdigest(), path.stat().st_size))
    payload = json.dumps(output, ensure_ascii=False, default=str)
    now = datetime.now(timezone.utc).isoformat()
    input_hash = _input_hash(input_data) if input_data is not None else None
    cacheable = cacheable and not missing_artifact
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name, uri, checksum, size in artifact_rows:
                connection.execute("""
                    INSERT INTO job_artifacts(job_id,stage,kind,name,uri,checksum,size_bytes,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_id,stage,kind,name) DO UPDATE SET
                        uri=excluded.uri,checksum=excluded.checksum,size_bytes=excluded.size_bytes,created_at=excluded.created_at
                """, (job_id, stage, stage, name, uri, checksum, size, now))
            cursor = connection.execute("""
                UPDATE job_stages SET status='succeeded',output_json=?,input_hash=COALESCE(?,input_hash),
                    cacheable=?,
                    error_class=NULL,error_message=NULL,finished_at=?
                WHERE job_id=? AND stage=?
            """, (payload, input_hash, cacheable, now, job_id, stage))
            if cursor.rowcount != 1:
                raise ValueError(f"Cannot complete stage {stage!r} before it starts")
            connection.execute(
                "INSERT INTO job_events(job_id,stage,status,created_at,details_json) VALUES(?,?,?,?,?)",
                (job_id, stage, "succeeded", now, json.dumps({"cacheable": cacheable})),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def stage_fail(job_id: str, stage: str, error_type: str, message: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    safe_message = str(message)[:1000]
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            exists = connection.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Cannot fail stage for unknown job {job_id!r}")
            connection.execute("""
                INSERT INTO job_stages(job_id,stage,status,attempt,error_class,error_message,finished_at)
                VALUES(?,?,'failed',0,?,?,?)
                ON CONFLICT(job_id,stage) DO UPDATE SET
                    status='failed',error_class=excluded.error_class,error_message=excluded.error_message,
                    finished_at=excluded.finished_at
            """, (job_id, stage, error_type[:100], safe_message, now))
            connection.execute(
                "DELETE FROM job_artifacts WHERE job_id=? AND stage=?",
                (job_id, stage),
            )
            connection.execute(
                "UPDATE jobs SET status='failed',stage=?,error_class=?,error_message=?,finished_at=?,updated_at=? WHERE job_id=?",
                (stage, error_type[:100], safe_message, now, now, job_id),
            )
            connection.execute(
                "INSERT INTO job_events(job_id,stage,status,created_at,details_json) VALUES(?,?,?,?,?)",
                (job_id, stage, "failed", now, json.dumps({"error_type": error_type[:100]})),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def recent_stages(limit: int = 100) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT job_id,stage,status,attempt,error_class,started_at,finished_at "
            "FROM job_stages ORDER BY COALESCE(started_at,finished_at) DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [dict(row) for row in rows]


def set_job(job_id: str, status: str, error_type: str | None = None,
            error_message: str | None = None) -> None:
    allowed = {
        "queued", "running", "complete", "partial_failure", "failed",
        "uncertain", "interrupted", "cancelled", "budget_blocked",
    }
    if status not in allowed:
        raise ValueError(f"Invalid durable job state: {status}")
    now = datetime.now(timezone.utc).isoformat()
    terminal = status not in {"queued", "running"}
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status=?,error_class=?,error_message=?,updated_at=?,"
            "finished_at=CASE WHEN ? THEN ? ELSE finished_at END WHERE job_id=?",
            (status, error_type, str(error_message or "")[:1000] or None, now, terminal, now, job_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Cannot update unknown durable job {job_id!r}")


def videos_today(profile_id: str, date: str, exclude_job_id: str = "") -> int:
    with _connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE profile_id=? AND scheduled_date=? "
            "AND status NOT IN ('budget_blocked','cancelled') AND job_id!=?",
            (profile_id, date, exclude_job_id),
        ).fetchone()
    return int(row["count"])


def recover_stuck(stale_after_seconds: int = 900) -> dict[str, int]:
    if stale_after_seconds < 1:
        raise ValueError("stale_after_seconds must be positive")
    cutoff = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - stale_after_seconds, timezone.utc
    ).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            posts_count = connection.execute(
                "UPDATE posts SET status='uncertain',error_class='worker_lost',"
                "error_message='Worker stopped during upload; inspect provider before retry.',updated_at=? "
                "WHERE status='in_flight' AND updated_at<?", (now, cutoff)
            ).rowcount
            stages_count = connection.execute(
                "UPDATE job_stages SET status='failed',error_class='worker_lost',"
                "error_message='Worker stopped during stage.',finished_at=? "
                "WHERE status='running' AND started_at<?", (now, cutoff)
            ).rowcount
            jobs_count = connection.execute(
                "UPDATE jobs SET status=CASE WHEN job_id IN "
                "(SELECT job_id FROM posts WHERE status='uncertain') THEN 'uncertain' ELSE 'interrupted' END,"
                "error_class='worker_lost',error_message='Worker stopped before job completion.',"
                "finished_at=?,updated_at=? WHERE status='running' AND updated_at<?",
                (now, now, cutoff),
            ).rowcount
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"jobs_interrupted": jobs_count, "stages_interrupted": stages_count, "posts_uncertain": posts_count}


def artifacts(job_id: str) -> list[dict]:
    with _connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT stage,uri,checksum,created_at FROM artifacts WHERE job_id=? ORDER BY created_at", (job_id,)
        ).fetchall()]


def reserve_topic(profile_id: str, subject_hash: str, script_hash: str,
                  subject: str, script: str, created_at: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute("""
            INSERT OR IGNORE INTO topic_history(profile_id,subject_hash,script_hash,subject,script,created_at)
            VALUES(?,?,?,?,?,?)
        """, (profile_id, subject_hash, script_hash, subject, script, created_at))
    return cursor.rowcount == 1


def topic_history(profile_id: str) -> list[dict]:
    with _connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT subject,script,subject_hash,script_hash,created_at FROM topic_history WHERE profile_id=? ORDER BY created_at",
            (profile_id,),
        ).fetchall()]


def reserve_post(job_id: str, profile_id: str, platform: str) -> dict:
    from app.services.scheduler import _utc_now
    now = _utc_now()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO posts(job_id,profile_id,platform,status,attempted_at,updated_at)
                VALUES(?,?,?,'in_flight',?,?)
            """, (job_id, profile_id, platform, now, now))
            row = connection.execute(
                "SELECT * FROM posts WHERE job_id=? AND profile_id=? AND platform=?",
                (job_id, profile_id, platform),
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"created": cursor.rowcount == 1, **dict(row)}


def finish_post(job_id: str, profile_id: str, platform: str, *,
                status: str, external_id: str = "", post_url: str = "", error_class: str = "") -> None:
    if status not in {"success", "failed", "uncertain", "cancelled"}:
        raise ValueError("Invalid post state")
    from app.services.scheduler import _utc_now
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if external_id:
                duplicate = connection.execute(
                    "SELECT 1 FROM posts WHERE platform=? AND external_id=? "
                    "AND NOT (job_id=? AND profile_id=?) LIMIT 1",
                    (platform, external_id, job_id, profile_id),
                ).fetchone()
                if duplicate:
                    raise ValueError("Platform external ID is already recorded for another job")
            cursor = connection.execute("""
            UPDATE posts SET status=?,external_id=?,post_url=?,error_class=?,updated_at=?
            WHERE job_id=? AND profile_id=? AND platform=?
            """, (status, external_id[:200], post_url[:2000], error_class[:100], _utc_now(),
                  job_id, profile_id, platform))
            if cursor.rowcount != 1:
                raise ValueError("Cannot finish an unreserved platform post")
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def posts(job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as connection:
        if job_id:
            rows = connection.execute("SELECT * FROM posts WHERE job_id=? ORDER BY updated_at DESC", (job_id,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM posts ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
    return [dict(row) for row in rows]


def recent_jobs(limit: int = 100) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT job_id,parent_job_id,profile_id,scheduled_date,slot,status,stage,attempt,error_class,updated_at "
            "FROM jobs ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return [dict(row) for row in rows]


def recent_artifacts(limit: int = 100) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT a.job_id,j.profile_id,a.stage,a.kind,a.name,a.checksum,a.size_bytes,a.created_at "
            "FROM job_artifacts a JOIN jobs j ON j.job_id=a.job_id "
            "ORDER BY a.created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return [dict(row) for row in rows]


def recent_posts(limit: int = 100) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT job_id,profile_id,platform,status,external_id,error_class,attempted_at,updated_at "
            "FROM posts ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return [dict(row) for row in rows]


def summary() -> dict[str, int]:
    with _connect() as connection:
        jobs = connection.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        posts = connection.execute("SELECT status,COUNT(*) AS count FROM posts GROUP BY status").fetchall()
    return {"jobs_" + row["status"]: row["count"] for row in jobs} | {
        "posts_" + row["status"]: row["count"] for row in posts
    }
