"""Supervise autonomous jobs without replaying paid work or public actions."""

from datetime import datetime, timedelta, timezone

from filelock import FileLock, Timeout
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.services import accounts, autoflow_settings, job_store, profile_schedules, scheduler


class MasterPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    videos_per_profile_per_day: int = Field(default=1, ge=1, le=1)
    comments_enabled: bool = True
    replies_per_profile_per_day: int = Field(default=5, ge=1, le=20)
    health_interval_minutes: int = Field(default=15, ge=5, le=60)
    comment_interval_minutes: int = Field(default=15, ge=5, le=60)


def policy() -> MasterPolicy:
    return MasterPolicy.model_validate(scheduler._read("master_agent_policy.json", {}))


def initialize() -> None:
    settings = policy()
    with FileLock(str(scheduler._path("master-supervisor.lock")), timeout=10):
        if not scheduler._path("master_agent_policy.json").exists():
            scheduler._write("master_agent_policy.json", settings.model_dump())


def owns_legacy_automation() -> bool:
    return scheduler._path("master_agent_policy.json").exists()


def _due(value: str | None, minutes: int, now: datetime) -> bool:
    return not value or now - datetime.fromisoformat(value) >= timedelta(minutes=minutes)


def status() -> dict:
    from app.services.master_comments import load_records
    value = scheduler._read("master_agent_status.json", {})
    replies = load_records()
    activity = [
        {"Profile": row["profile_id"], "Platform": row["platform"],
         "Status": row["status"], "Time": row.get("attempted_at", row.get("seen_at", ""))}
        for row in list(replies.values())[-20:]
    ]
    return {**value, "policy": policy().model_dump(),
            "reply_activity": activity,
            "job_counts": job_store.summary(),
            "recent_jobs": job_store.recent_jobs(30),
            "recent_stages": job_store.recent_stages(50),
            "recent_artifacts": job_store.recent_artifacts(30),
            "recent_posts": job_store.recent_posts(30),
            "active": profile_schedules.master_enabled(),
            "worker_alive": scheduler.scheduler_status().get("worker_alive", False)}


def supervise() -> None:
    """Read-only health work plus profile-scoped replies, never video generation."""
    try:
        lock = FileLock(str(scheduler._path("master-supervisor.lock")), timeout=0)
        lock.acquire()
    except Timeout:
        return
    try:
        settings = policy()
        now = datetime.now(timezone.utc)
        previous = scheduler._read("master_agent_status.json", {})
        report = {**previous, "heartbeat": now.isoformat(), "alerts": []}
        active = profile_schedules.master_enabled()
        profiles = accounts.list_profiles()
        if not profiles:
            report["alerts"].append("Add a channel profile before activating scheduled generation.")
        if active and _due(previous.get("health_checked_at"), settings.health_interval_minutes, now):
            report["health"] = autoflow_settings.diagnostics()
            report["health_checked_at"] = now.isoformat()
            profile_schedules._event({"stage": "master_health", "status": "complete"})
        health = report.get("health", autoflow_settings.health_board())
        provider = str(autoflow_settings.config.app.get("llm_provider", "anthropic"))
        provider_label = {"anthropic": "Claude", "openai": "OpenAI", "gemini": "Gemini"}.get(provider)
        required = {provider_label, "ElevenLabs", "ElevenLabs Voice", "Pexels"}
        for row in health:
            if row["Service"] in required and not row["Status"].startswith("🟢"):
                report["alerts"].append(f"{row['Service']}: check Settings & System Health.")
        for profile in profiles:
            if not profile.get("enabled", True):
                continue
            for platform in profile.get("enabled_platforms", []):
                if not profile.get(platform, {}).get("connected"):
                    report["alerts"].append(f"{profile['name']}: connect {platform} before publishing.")
        # Persist before comment work; a crash must not continuously repoll paid analysis.
        poll_due = active and settings.comments_enabled and _due(
            previous.get("comments_checked_at"), settings.comment_interval_minutes, now
        )
        if poll_due:
            report["comments_checked_at"] = now.isoformat()
        scheduler._write("master_agent_status.json", report)
        if poll_due:
            from app.services import master_comments
            report["comments"] = master_comments.run_cycle(settings.replies_per_profile_per_day)
            scheduler._write("master_agent_status.json", report)
        profile_schedules._event({"stage": "master_heartbeat", "status": "active" if active else "standby"})
    except Exception as exc:
        logger.error("Master supervision failed: {}", type(exc).__name__)
        # Never include provider exception text or request details.
        scheduler._write("master_agent_status.json", {
            "heartbeat": datetime.now(timezone.utc).isoformat(),
            "alerts": ["Supervisor failed; inspect private storage and worker logs."],
            "error_type": type(exc).__name__,
        })
        profile_schedules._event({"stage": "master_supervisor", "status": "failed", "error_type": type(exc).__name__})
    finally:
        lock.release()
