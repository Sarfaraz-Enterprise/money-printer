"""Atomic per-profile subject/script reservations; failed renders never replay ideas."""

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from app.services import job_store
from app.utils import video_outputs


def fingerprint(text: str) -> str:
    normalized = " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _read(directory: Path) -> list[dict]:
    target = directory / "history.json"
    if not target.exists():
        return []
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Profile story memory is corrupt; generation is blocked.") from exc
    if not isinstance(value, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("subject"), str)
        or not isinstance(row.get("script"), str) for row in value
    ):
        raise ValueError("Profile story memory must contain subject/script records.")
    return value


def previous(profile_id: str) -> list[dict]:
    directory = Path(video_outputs.profile_dir(profile_id))
    with FileLock(str(directory / ".history.lock"), timeout=15):
        rows = _read(directory)
        db_rows = job_store.topic_history(profile_id)
        known = {(fingerprint(row["subject"]), fingerprint(row["script"])) for row in rows}
        rows.extend(row for row in db_rows if (fingerprint(row["subject"]), fingerprint(row["script"])) not in known)
        filenames = [
            {"subject": re.sub(r" \d+$", "", re.sub(r"^\d{4}-\d{2}-\d{2}_", "", path.stem)).replace("_", " ")}
            for path in directory.glob("*.mp4")
        ]
        return rows + filenames


def reserve(profile_id: str, package: dict, qa: dict) -> bool:
    directory = Path(video_outputs.profile_dir(profile_id))
    with FileLock(str(directory / ".history.lock"), timeout=15):
        rows = _read(directory)
        subject = video_outputs.subject_slug(package["subject"])
        subject_hash = fingerprint(subject)
        script_hash = fingerprint(package["script"])
        existing_titles = {
            video_outputs.subject_slug(re.sub(r" \d+$", "", re.sub(r"^\d{4}-\d{2}-\d{2}_", "", path.stem)))
            for path in directory.glob("*.mp4")
        }
        if subject in existing_titles or any(
            video_outputs.subject_slug(row["subject"]) == subject
            or fingerprint(row["script"]) == script_hash for row in rows
        ):
            return False
        created_at = datetime.now(timezone.utc).isoformat()
        if not job_store.reserve_topic(
            profile_id, subject_hash, script_hash, package["subject"], package["script"], created_at
        ):
            return False
        rows.append({
            "created_at": created_at,
            "subject": package["subject"], "script": package["script"],
            "subject_slug": subject, "script_hash": script_hash, "qa": qa,
        })
        descriptor, temporary = tempfile.mkstemp(prefix=".history-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(rows, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, directory / "history.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
    return True
