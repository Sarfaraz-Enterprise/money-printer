"""Profile-isolated comment replies; public attempts are durable and never replayed."""

from datetime import datetime, timedelta, timezone

from filelock import FileLock, Timeout
from loguru import logger

from app.config import config
from app.services import accounts, comment_agent, profile_schedules, scheduler

MAX_ANALYSED_PER_PROFILE_PER_DAY = 20


def load_records() -> dict[str, dict]:
    records = scheduler._read("master_comment_attempts.json", {})
    if not isinstance(records, dict):
        raise ValueError("Invalid master comment ledger")
    for key, row in records.items():
        if (not isinstance(key, str) or not isinstance(row, dict)
                or any(not isinstance(row.get(field), str) for field in ("profile_id", "platform", "status"))):
            raise ValueError("Invalid master comment record")
        for field in ("seen_at", "attempted_at"):
            if field in row:
                if not isinstance(row[field], str) or datetime.fromisoformat(row[field]).tzinfo is None:
                    raise ValueError("Invalid master comment timestamp")
    return records


def _enabled(profile_id: str, platform: str) -> bool:
    if not profile_schedules.master_enabled():
        return False
    return any(
        row["id"] == profile_id and row.get("enabled", True)
        and platform in row.get("enabled_platforms", [])
        for row in accounts.list_profiles()
    )


def run_cycle(daily_cap: int) -> list[dict]:
    try:
        lock = FileLock(str(scheduler._path("master-comments.lock")), timeout=0)
        lock.acquire()
    except Timeout:
        return [{"status": "busy"}]
    try:
        return _run(daily_cap)
    finally:
        lock.release()


def _run(daily_cap: int) -> list[dict]:
    results = []
    now = datetime.now(timezone.utc)
    records = load_records()
    profiles = accounts.list_profiles(include_secrets=True)
    for profile in profiles:
        if not profile_schedules.master_enabled():
            break
        if not profile.get("enabled", True):
            continue
        profile_id = profile["id"]
        attempted = [
            row for row in records.values() if row["profile_id"] == profile_id
            and row.get("attempted_at") and now - datetime.fromisoformat(row["attempted_at"]) < timedelta(days=1)
        ]
        # Conservative rolling-day cap includes failures, uncertain and in-flight posts.
        if len(attempted) >= daily_cap:
            continue
        analysed_today = sum(
            row["profile_id"] == profile_id and row.get("seen_at") is not None
            and now - datetime.fromisoformat(row["seen_at"]) < timedelta(days=1)
            for row in records.values()
        )
        remaining_analysis = MAX_ANALYSED_PER_PROFILE_PER_DAY - analysed_today
        if remaining_analysis <= 0:
            continue
        clients = {
            "youtube": comment_agent.YouTubeClient(profile_id),
            "instagram": comment_agent.InstagramClient(profile),
            "facebook": comment_agent.FacebookClient(profile),
        }
        for platform in profile.get("enabled_platforms", []):
            if not profile.get(platform, {}).get("connected") or not _enabled(profile_id, platform):
                continue
            # One reply per profile per cycle also spaces public replies by >=15 min.
            if attempted:
                latest = max(datetime.fromisoformat(row["attempted_at"]) for row in attempted)
                if now - latest < timedelta(minutes=15):
                    break
            row = {"profile_id": profile_id, "platform": platform, "status": "checked"}
            try:
                settings = comment_agent.CommentAgentSettings(
                    auto_video=False, max_videos_per_day=0,
                    brand_voice=(profile["name"] + ": " + profile.get("voice_direction", ""))[:500],
                )
                fresh = []
                for comment in clients[platform].fetch_comments(settings):
                    key = f"{profile_id}:{comment['key']}"
                    published = comment_agent._parse_time(comment.get("published_at"))
                    if key not in records and published and now - timedelta(hours=24) <= published <= now:
                        fresh.append(comment)
                # Review one candidate at a time: never pay to analyse replies
                # that this deliberately low-volume poll cannot send.
                fresh = fresh[:1]
                if not fresh:
                    results.append(row)
                    continue
                if not _enabled(profile_id, platform):
                    return results
                # Reserve analysis too: a provider timeout may still consume credits.
                for comment in fresh:
                    records[f"{profile_id}:{comment['key']}"] = {
                        "profile_id": profile_id, "platform": platform,
                        "status": "analysis_attempted", "seen_at": now.isoformat(),
                    }
                scheduler._write("master_comment_attempts.json", records)
                remaining_analysis -= len(fresh)
                with config.runtime_config_lock():
                    snapshot = config.snapshot_config_with_pending(config.app)
                analysis = comment_agent.analyze_comments(fresh, settings, app_config=snapshot)
                for comment in fresh:
                    key = f"{profile_id}:{comment['key']}"
                    text = analysis[comment["key"]]["reply_text"]
                    record = {"profile_id": profile_id, "platform": platform, "status": "skipped",
                              "seen_at": now.isoformat()}
                    if text:
                        if not _enabled(profile_id, platform):
                            return results
                        record.update(status="attempting", attempted_at=now.isoformat())
                    records[key] = record
                    scheduler._write("master_comment_attempts.json", records)
                    if not text:
                        continue
                    # A crash from this point leaves 'attempting': never retry a public call.
                    try:
                        reply_id = clients[platform].reply(comment, text)
                        record["status"] = "replied" if reply_id else "uncertain"
                    except comment_agent.PlatformError:
                        record["status"] = "uncertain"
                    except Exception as exc:
                        logger.error("Master comment reply uncertain: {}", type(exc).__name__)
                        record["status"] = "uncertain"
                    scheduler._write("master_comment_attempts.json", records)
                    row["status"] = record["status"]
                    profile_schedules._event({**row, "stage": "comment_reply"})
                    attempted.append(record)
                    break
            except Exception as exc:
                row.update(status="failed", error_type=type(exc).__name__)
                logger.error("Master comment check failed: {}", type(exc).__name__)
                profile_schedules._event({**row, "stage": "comment_check"})
            results.append(row)
            if remaining_analysis <= 0:
                break
    return results
