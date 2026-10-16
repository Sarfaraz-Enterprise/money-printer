"""Durable, at-most-once profile slots, drained serially without losing overlaps."""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytz
from filelock import FileLock, Timeout
from loguru import logger

from app.services import accounts, job_store, profile_content, scheduler

IST = pytz.timezone("Asia/Kolkata")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autoflow-agent")
_draining = threading.Lock()


def master_enabled() -> bool:
    return scheduler._read("autoflow_agent.json", {"enabled": False}).get("enabled") is True


def set_master_enabled(enabled: bool) -> None:
    with FileLock(str(scheduler._path("schedule-data.lock")), timeout=10):
        scheduler._write("autoflow_agent.json", {"enabled": bool(enabled)})
    _event({"status": "active" if enabled else "paused", "stage": "master"})


def _event(entry: dict) -> None:
    path = scheduler._path("logs") / "autoflow_agent.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {key: entry[key] for key in (
        "run_id", "profile_id", "slot", "status", "stage", "error_type", "video_path",
        "subject", "task_id", "word_count", "hook_qa", "finished_at", "attempts", "retry_at",
    ) if key in entry}
    if "results" in entry:
        safe["results"] = [
            {key: row[key] for key in ("profile_id", "platform", "success", "status", "url") if key in row}
            for row in entry["results"] if isinstance(row, dict)
        ]
    with FileLock(str(path) + ".lock", timeout=10), path.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"timestamp": scheduler._utc_now(), **safe}) + "\n")


def _ledger() -> dict:
    value = scheduler._read("autoflow_agent_runs.json", {})
    if not isinstance(value, dict):
        raise ValueError("Invalid autonomous run ledger")
    return value


def _record(entry: dict) -> None:
    job_store.record_job(entry)
    with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
        rows = _ledger()
        rows[entry["run_id"]] = dict(entry)
        scheduler._write("autoflow_agent_runs.json", rows)
    scheduler._record(entry)
    _event(entry)


def recover_interrupted() -> None:
    previous = scheduler.recent_history()
    for entry in previous:
        if entry.get("profile_id") and entry.get("run_id"):
            job_store.record_job(entry)
    with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
        rows = _ledger()
        for entry in previous:
            if entry.get("profile_id") and entry.get("run_id") and entry.get("slot"):
                rows.setdefault(entry["run_id"], dict(entry))
        changed = []
        for entry in rows.values():
            if entry.get("status") == "running":
                entry.update(status="interrupted", finished_at=scheduler._utc_now())
                changed.append(dict(entry))
        for entry in job_store.recoverable_jobs():
            rows.setdefault(entry["job_id"], entry)
        for entry in rows.values():
            if entry.get("profile_id") and entry.get("run_id"):
                job_store.record_job(entry)
        scheduler._write("autoflow_agent_runs.json", rows)
    for entry in changed:
        job_store.record_job(entry)
        scheduler._record(entry)
        _event(entry)


def _settings(profile: dict) -> scheduler.ScheduleSettings:
    schedule = profile.get("schedule") or {}
    return scheduler.ScheduleSettings(
        enabled=bool(schedule.get("enabled")),
        timezone="Asia/Kolkata",
        slots=schedule.get("post_times_ist", schedule.get("post_times", schedule.get("slots", ["05:00", "20:00"]))),
        voice_name=profile.get("voice_name") or scheduler.voice.ELEVENLABS_SERVER_DEFAULT_VOICE,
        target_account_ids=[profile["id"]],
        sub_theme_focus=schedule.get("sub_theme_focus", ""),
    )


def _daily_limit_reached(profile_id: str, date: str, rows: list[dict]) -> bool:
    from app.services.master_agent import policy
    used = sum(
        row.get("profile_id") == profile_id and (
            row.get("date") == date
            or (not row.get("date") and row.get("run_id", "").startswith(f"account-{profile_id}-{date}-"))
        )
        and row.get("status") not in {"budget_blocked", "cancelled"}
        for row in rows
    )
    used = max(used, job_store.videos_today(profile_id, date))
    return used >= policy().videos_per_profile_per_day


def overview(profiles: list[dict], now: datetime | None = None) -> list[dict]:
    now = (now or datetime.now(IST)).astimezone(IST)
    active = master_enabled()
    with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
        history = list(_ledger().values())
    rows = []
    for profile in profiles:
        times = _settings(profile).slots
        enabled = profile.get("enabled", True) and profile.get("schedule", {}).get("enabled", False)
        candidates = []
        for value in times:
            hour, minute = map(int, value.split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now or _daily_limit_reached(profile["id"], str(now.date()), history):
                due += timedelta(days=1)
            candidates.append(due)
        rows.append({
            "Profile": profile["name"],
            "Status": "🟢 Active" if enabled and active else "🟡 Standby" if enabled else "Disabled",
            "Daily Times (IST)": ", ".join(times),
            "Next Run (IST)": min(candidates).strftime("%d %b %Y %H:%M IST") if enabled and active else "Standby",
            "Audience": profile.get("target_audience", "United States (US Tier-1)"),
            "Platforms": ", ".join(profile.get("enabled_platforms", [])) or "None",
            "Latest Run": next((row.get("status", "") for row in reversed(history)
                                if row.get("profile_id") == profile["id"]), "No runs yet"),
        })
    return rows


def timeline(profiles: list[dict], now: datetime | None = None) -> list[dict]:
    now = (now or datetime.now(IST)).astimezone(IST)
    with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
        history = _ledger()
    active = master_enabled()
    rows = []
    for profile in profiles:
        settings = _settings(profile)
        for slot in settings.slots:
            hour, minute = map(int, slot.split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            record = history.get(f"account-{profile['id']}-{now.date()}-{slot}", {})
            status = record.get("status") or (
                "Disabled" if not settings.enabled or not profile.get("enabled", True)
                else "Standby" if not active else "Daily limit reached" if _daily_limit_reached(profile["id"], str(now.date()), list(history.values()))
                else "Missed" if due < now.replace(second=0, microsecond=0) else "Scheduled"
            )
            rows.append({
                "Profile": profile["name"], "Time": due.isoformat(), "Time (IST)": slot,
                "Audience Time": due.astimezone(ZoneInfo(profile.get("timezone", "America/New_York"))).strftime("%d %b %H:%M %Z"),
                "Audience": profile.get("target_audience", "United States (US Tier-1)"),
                "Status": status, "Platforms": ", ".join(profile.get("enabled_platforms", [])),
            })
    return sorted(rows, key=lambda row: row["Time"])


def next_trigger(profiles: list[dict], now: datetime | None = None) -> datetime | None:
    if not master_enabled():
        return None
    now = (now or datetime.now(IST)).astimezone(IST)
    with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
        history = list(_ledger().values())
    candidates = []
    for profile in profiles:
        settings = _settings(profile)
        if not profile.get("enabled", True) or not settings.enabled:
            continue
        for slot in settings.slots:
            hour, minute = map(int, slot.split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            candidates.append(due if due > now and not _daily_limit_reached(profile["id"], str(now.date()), history)
                              else due + timedelta(days=1))
    return min(candidates) if candidates else None


def tick(now: datetime | None = None) -> None:
    """Claim all matching profiles before starting any slow generation."""
    try:
        if not master_enabled():
            return
        now = (now or datetime.now(IST)).astimezone(IST)
        slot = now.strftime("%H:%M")
        profiles = accounts.list_profiles()
        claimed = []
        with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
            rows = _ledger()
            for profile in profiles:
                settings = _settings(profile)
                if not profile.get("enabled", True) or not settings.enabled or slot not in settings.slots:
                    continue
                run_id = f"account-{profile['id']}-{now.date()}-{slot}"
                if run_id in rows:
                    continue
                blocked = _daily_limit_reached(profile["id"], str(now.date()), list(rows.values()))
                entry = {
                    "run_id": run_id, "profile_id": profile["id"], "slot": slot,
                    "date": str(now.date()), "timezone": "Asia/Kolkata",
                    "started_at": scheduler._utc_now(),
                    "status": "budget_blocked" if blocked else "queued",
                    "stage": "daily_limit" if blocked else "queued",
                }
                created, durable = job_store.claim_job(entry)
                if created or durable.get("status") == "queued":
                    rows[run_id] = durable
                    claimed.append(durable)
                else:
                    rows[run_id] = durable
            scheduler._write("autoflow_agent_runs.json", rows)
        for entry in claimed:
            scheduler._record(entry)
            _event(entry)
        if _draining.acquire(blocking=False):
            try:
                _executor.submit(_drain)
            except Exception:
                _draining.release()
                raise
    except Exception as exc:
        logger.error(f"Autonomous agent tick failed: {type(exc).__name__}")
        _event({"status": "failed", "stage": "tick", "error_type": type(exc).__name__})


def _drain() -> None:
    try:
        while master_enabled():
            with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
                entry = next((dict(row) for row in _ledger().values()
                              if row.get("status") == "queued" and (
                                  not row.get("retry_at") or row["retry_at"] <= scheduler._utc_now())), None)
            if entry is None:
                return
            if not run_profile_pipeline(entry["profile_id"], entry["slot"], entry=entry):
                return  # Another renderer owns the lock; keep queued for next tick.
    except Exception as exc:
        logger.error(f"Autonomous queue failed: {type(exc).__name__}")
        _event({"status": "failed", "stage": "queue", "error_type": type(exc).__name__})
    finally:
        _draining.release()


def run_profile_pipeline(profile_id: str, slot: str, *, entry: dict) -> bool:
    try:
        lock = FileLock(str(scheduler._path("schedule-run.lock")), timeout=0)
        lock.acquire()
    except Timeout:
        return False
    try:
        with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
            current = _ledger().get(entry["run_id"], {})
            if current.get("status") != "queued":
                return True
        profile = next((row for row in accounts.list_profiles() if row["id"] == profile_id), None)
        if (not master_enabled() or profile is None or not profile.get("enabled", True)
                or not profile.get("schedule", {}).get("enabled")
                or slot not in _settings(profile).slots
                or entry["date"] != str(datetime.now(IST).date())):
            entry.update(status="cancelled", stage="cancelled", finished_at=scheduler._utc_now())
            _record(entry)
            return True
        with FileLock(str(scheduler._path("agent-runs.lock")), timeout=10):
            from app.services.master_agent import policy
            rows = _ledger()
            used = sum(
                row.get("run_id") != entry["run_id"] and row.get("profile_id") == profile_id
                and row.get("date") == entry["date"]
                and row.get("status") not in {"queued", "budget_blocked", "cancelled"}
                for row in rows.values()
            )
        if used >= policy().videos_per_profile_per_day:
            entry.update(status="budget_blocked", stage="daily_limit")
            _record(entry)
            return True
        entry.update(status="running", stage="preflight", attempts=entry.get("attempts", 0) + 1)
        _record(entry)  # Durable claim precedes paid calls / any possible upload.
        settings = _settings(profile)
        scheduler.preflight(settings)
        entry["stage"] = "content"
        _record(entry)
        content_inputs = {
            "profile": {
                key: profile.get(key) for key in (
                    "id", "name", "category", "system_prompt", "niche_prompt",
                    "visual_keywords_preset", "visual_style", "target_audience",
                    "audience_tags", "timezone", "voice_direction",
                )
            },
            "topic_focus": settings.sub_theme_focus,
        }
        package = job_store.cached_output(entry["run_id"], "content", content_inputs)
        if package is None:
            job_store.stage_begin(entry["run_id"], "content", content_inputs)
            try:
                package = profile_content.generate_profile_package(profile, settings.sub_theme_focus)
            except Exception as content_exc:
                job_store.stage_fail(
                    entry["run_id"], "content", type(content_exc).__name__,
                    "Profile content generation failed.",
                )
                raise
            job_store.stage_succeed(
                entry["run_id"], "content", dict(package), input_data=content_inputs,
            )
        entry.update(subject=package["subject"], stage="quality_passed",
                     word_count=len(re.findall(r"\b\w+(?:['\u2019]\w+)*\b", package["script"])),
                     hook_qa="passed")
        _record(entry)
        scheduler.render_and_publish(
            package, settings, entry, _record,
            dispatch_allowed=lambda: _dispatch_allowed(profile_id, slot),
        )
    except Exception as exc:
        # Retry only transient local preflight failures before any paid/public work.
        # Content/TTS/render failures may have consumed credits; never replay them.
        if entry.get("stage") == "preflight" and isinstance(exc, OSError) and entry.get("attempts", 1) < 2:
            entry.update(status="queued", error_type=type(exc).__name__,
                         retry_at=(datetime.now(scheduler.timezone.utc) + timedelta(minutes=5)).isoformat())
            _record(entry)
        else:
            entry.update(status="failed", error_type=type(exc).__name__)
        logger.error(f"Account schedule failed: {type(exc).__name__}")
    finally:
        try:
            if entry.get("status") != "queued":
                entry["finished_at"] = scheduler._utc_now()
                _record(entry)
        finally:
            lock.release()
    return True


def _dispatch_allowed(profile_id: str, slot: str) -> bool:
    if not master_enabled():
        return False
    profile = next((row for row in accounts.list_profiles() if row["id"] == profile_id), None)
    return bool(profile and profile.get("enabled", True) and profile.get("schedule", {}).get("enabled")
                and slot in _settings(profile).slots)
