"""Single-owner daily generation and publishing; the UI only edits settings."""

import json
import os
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from filelock import FileLock, Timeout
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import config, env_secrets
from app.services import agent, task, voice
from app.services.upload_post import UploadPostService
from app.models.schema import VideoParams


TIMEZONES = {
    "Asia/Kolkata": "India (IST)",
    "America/New_York": "US/Eastern (New York)",
    "America/Chicago": "US/Central (Chicago)",
    "America/Denver": "US/Mountain (Denver)",
    "America/Los_Angeles": "US/Pacific (Los Angeles)",
}
STORAGE = Path(config.root_dir) / "storage"
TREND_URL = "https://trends.google.com/trending/rss?geo=US"


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    timezone: str = "America/New_York"
    slots: list[str] = Field(default_factory=lambda: ["05:00", "20:00"], min_length=1, max_length=1440)
    platforms: list[Literal["youtube", "instagram"]] = Field(
        default_factory=lambda: ["youtube", "instagram"], min_length=1, max_length=2
    )
    topic_mode: Literal["niche", "trending"] = "niche"
    niche: str = Field(default="", max_length=2000)
    niche_style: Literal["afterframe"] = "afterframe"
    sub_theme_focus: str = Field(default="", max_length=2000)
    target_account_ids: list[str] = Field(default_factory=list, max_length=50)
    voice_name: str = voice.ELEVENLABS_SERVER_DEFAULT_VOICE
    video_source: Literal["pexels", "pixabay", "coverr"] = "pexels"
    youtube_made_for_kids: bool = False

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value):
        if value not in TIMEZONES:
            raise ValueError("Unsupported audience timezone")
        return value

    @field_validator("slots")
    @classmethod
    def check_slots(cls, values):
        for value in values:
            parsed = datetime.strptime(value, "%H:%M")
            if parsed.strftime("%H:%M") != value:
                raise ValueError("Schedule times must use HH:MM")
        if len(set(values)) != len(values):
            raise ValueError("Schedule slots must be different")
        return values

    @field_validator("voice_name")
    @classmethod
    def check_voice(cls, value):
        if not value.startswith("elevenlabs") or not voice.resolve_elevenlabs_voice_id(value):
            if value not in ("elevenlabs", voice.ELEVENLABS_SERVER_DEFAULT_VOICE):
                raise ValueError("Select a valid ElevenLabs voice or Server default")
        return value

    @model_validator(mode="after")
    def check_platforms(self):
        if len(set(self.platforms)) != len(self.platforms):
            raise ValueError("Duplicate target platforms")
        return self


def _path(name: str) -> Path:
    STORAGE.mkdir(parents=True, exist_ok=True)
    return STORAGE / name


def _read(name: str, default):
    path = _path(name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(name: str, data) -> None:
    path = _path(name)
    descriptor, temporary = tempfile.mkstemp(prefix=".schedule-", dir=STORAGE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_settings() -> ScheduleSettings:
    return ScheduleSettings.model_validate(_read("schedule_config.json", {}))


def preflight(settings: ScheduleSettings) -> None:
    if not voice.get_elevenlabs_api_key() or not voice.resolve_elevenlabs_voice_id(settings.voice_name):
        raise ValueError("Configure an ElevenLabs key and a usable voice before enabling Auto-Pilot.")
    media_key = f"{settings.video_source}_api_keys"
    if not env_secrets.resolve("app", media_key, config.app.get(media_key)):
        raise ValueError(f"Configure {settings.video_source} credentials before enabling Auto-Pilot.")
    if settings.target_account_ids:
        from app.services.accounts import list_profiles
        profiles = {row["id"]: row for row in list_profiles()}
        if any(account_id not in profiles for account_id in settings.target_account_ids):
            raise ValueError("A selected publishing profile no longer exists.")
        if any(
            not any(profiles[account_id][platform].get("enabled") for platform in ("youtube", "facebook", "instagram"))
            for account_id in settings.target_account_ids
        ):
            raise ValueError("Every target profile needs an enabled destination.")
    else:
        publisher = UploadPostService()
        if not publisher.api_key or not publisher.username:
            raise ValueError("Configure the Upload-Post API key and profile username before enabling Auto-Pilot.")
    if not (Path(config.root_dir) / "resource" / "fonts" / "BeVietnamPro-Bold.ttf").is_file():
        raise ValueError("Required subtitle font BeVietnamPro-Bold.ttf is missing.")


def save_settings(settings: ScheduleSettings) -> None:
    if settings.enabled:
        preflight(settings)
    with FileLock(str(_path("schedule-data.lock")), timeout=10):
        _write("schedule_config.json", settings.model_dump())


def recent_history() -> list[dict]:
    with FileLock(str(_path("schedule-data.lock")), timeout=10):
        history = _read("schedule_history.json", [])
    if not isinstance(history, list):
        raise ValueError("Schedule history must be a JSON list.")
    return history


def _record(entry: dict) -> None:
    with FileLock(str(_path("schedule-data.lock")), timeout=10):
        history = _read("schedule_history.json", [])
        index = next((i for i, old in enumerate(history) if old["run_id"] == entry["run_id"]), None)
        if index is None:
            history.append(entry)
        else:
            history[index] = entry
        _write("schedule_history.json", history[-1000:])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trend_topic(niche: str) -> str:
    response = requests.get(TREND_URL, timeout=(10, 30), stream=True)
    try:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(8192):
            content.extend(chunk)
            if len(content) > 1_000_000:
                raise ValueError("Trend feed exceeds the size limit.")
    finally:
        response.close()
    root = ET.fromstring(content)
    items = []
    for item in root.findall("./channel/item")[:10]:
        title = (item.findtext("title") or "").strip()
        headlines = [
            element.text.strip()[:500] for element in item.iter()
            if element.tag.endswith("news_item_title") and element.text
        ]
        if title:
            items.append({"topic": title[:300], "headlines": headlines[:3]})
    if not items:
        raise ValueError("Google Trends USA returned no topics; this run will not publish.")
    return (
        "USA audience. Choose one relevant topic from this live Google Trends USA "
        "feed, using the headlines as limited context. Avoid inventing news facts, "
        "quotes or statistics. Niche guidance: " + niche
        + "\nFeed topics: " + json.dumps(items, ensure_ascii=False)
    )[:2000]


def _generate_package(settings: ScheduleSettings):
    recent = [
        {key: row[key] for key in ("subject", "hook", "script") if key in row}
        for row in recent_history()[-20:] if row.get("subject")
    ]
    for attempt in range(2):
        try:
            with config.runtime_config_lock():
                snapshot = config.snapshot_config_with_pending(config.app)
            story = agent.generate_afterframe_story(
                settings.sub_theme_focus, recent_stories=recent, app_config=snapshot
            )
            return {
                **story, "language": "en-US", "keywords": story["visual_keywords"],
                "description": story["subject"] + "\n"
                + " ".join("#" + tag for tag in story["tags"]),
            }
        except (requests.RequestException, ET.ParseError, agent.ContentAgentError, ValueError):
            logger.warning(f"Auto-Pilot topic/content attempt {attempt + 1} failed")
            if attempt == 1:
                raise
            time.sleep(5)


def render_and_publish(
    package, settings: ScheduleSettings, entry: dict, record, *,
    output_category: Literal["manual", "scheduled", "fully_automatic"] = "scheduled",
    dispatch_allowed=None,
) -> None:
    """Render one portrait MP4 and publish it once; the caller must hold schedule-run.lock.

    Never call this again for the same entry after a failure in the publish stage:
    a lost response can still mean the video went public.
    """
    task_id = str(uuid.uuid4())
    entry.setdefault("run_id", task_id)
    entry.update(task_id=task_id, subject=package["subject"], stage="render")
    from app.services import job_store
    if job_store.get_job(entry["run_id"]) is None:
        job = dict(entry)
        job["job_id"] = entry["run_id"]
        job.setdefault("profile_id", settings.target_account_ids[0] if settings.target_account_ids else "")
        job.setdefault("status", "running")
        job_store.record_job(job)
    record(entry)
    params = VideoParams(
        video_subject=package["subject"], video_script=package["script"],
        output_category=output_category,
        profile_id=settings.target_account_ids[0] if len(settings.target_account_ids) == 1 else "",
        video_terms=package["keywords"], video_language="en-US",
        voice_name=settings.voice_name, video_source=settings.video_source,
        video_aspect="9:16", video_count=1, subtitle_enabled=True,
        font_name="BeVietnamPro-Bold.ttf", bgm_type="none", bgm_volume=0,
    )
    render_inputs = {
        "subject": package["subject"], "script": package["script"],
        "keywords": package["keywords"], "voice": settings.voice_name,
        "video_source": settings.video_source, "profile_ids": settings.target_account_ids,
        "params": params.model_dump(warnings=False),
    }
    cached_render = job_store.cached_output(entry["run_id"], "render", render_inputs)
    if cached_render is not None:
        task_id = str(cached_render.get("task_id") or task_id)
        entry["task_id"] = task_id
        videos = cached_render.get("videos", [])
    else:
        job_store.stage_begin(entry["run_id"], "render", render_inputs)
        try:
            with config.runtime_config_lock():
                account = None
                if not settings.target_account_ids:
                    account = UploadPostService().snapshot_account()
                    account.update(upload_post_enabled=True, upload_post_add_to_queue=False)
                result = task.start(
                    task_id, params, auto_publish=False,
                    ledger_parent_job_id=entry["run_id"],
                )
            videos = (result or {}).get("videos") or []
        except Exception as render_exc:
            job_store.stage_fail(entry["run_id"], "render", type(render_exc).__name__, "Video rendering failed.")
            raise
    if len(videos) != 1 or not Path(videos[0]).is_file() or Path(videos[0]).stat().st_size == 0:
        if cached_render is None:
            job_store.stage_fail(entry["run_id"], "render", "invalid_artifact", "Rendering produced no valid MP4.")
        raise ValueError("Video rendering did not produce one non-empty MP4.")
    if cached_render is None:
        job_store.stage_succeed(
            entry["run_id"], "render", {"videos": videos, "task_id": task_id},
            input_data=render_inputs,
            artifacts={f"final-{index}": video_path for index, video_path in enumerate(videos, 1)},
        )
    job_store.record_artifact(entry["run_id"], "final_video", videos[0])
    entry.update(video_path=videos[0], stage="publish")
    record(entry)
    if dispatch_allowed is not None and not dispatch_allowed():
        entry.update(status="cancelled", stage="dispatch_paused")
        record(entry)
        return
    if settings.target_account_ids:
        from app.services.account_dispatcher import dispatch_video
        entry["results"] = dispatch_video(
            videos[0], package["subject"], package["description"],
            settings.target_account_ids, tags=package.get("tags"),
            job_id=entry["run_id"],
            dispatch_allowed=dispatch_allowed,
        )
        entry.update(
            status="complete" if entry["results"] and all(row.get("success") for row in entry["results"]) else "partial_failure",
            stage="complete",
        )
        record(entry)
        return
    publisher = UploadPostService(account)

    def record_background_request(request_id):
        entry["request_id"] = request_id
        record(entry)

    response = publisher.upload_video(
        videos[0], package["description"], platforms=settings.platforms,
        youtube_extra={
            "youtube_title": package["subject"],
            "youtube_description": package["description"],
            "tags": package.get("tags") or [word.strip() for word in package["keywords"].split(",")],
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": settings.youtube_made_for_kids,
        },
        on_background_start=record_background_request,
    )
    # Store IDs and success flags only, never arbitrary remote bodies.
    if response.get("request_id"):
        entry["request_id"] = str(response["request_id"])[:200]
    platform_results = response.get("results", {})
    rows = platform_results.items() if isinstance(platform_results, dict) else (
        (row.get("platform", "unknown"), row)
        for row in platform_results if isinstance(row, dict)
    ) if isinstance(platform_results, list) else []
    entry["results"] = [
        {
            "platform": str(platform)[:64],
            "success": row.get("success") is True,
            "post_id": str(row.get("post_id") or row.get("video_id") or row.get("id") or "")[:200],
        }
        for platform, row in rows if isinstance(row, dict)
    ]
    reported = {row["platform"] for row in entry["results"] if row["success"]}
    if response.get("success") is not True or not set(settings.platforms).issubset(reported):
        raise ValueError("Upload failed or its outcome is unconfirmed; inspect the request ID before retrying.")
    entry.update(status="complete", stage="complete")


def run_daily_autopilot_pipeline(slot: str, expected_settings: str | None = None) -> None:
    """Compatibility shim: shared schedules no longer authorize generation."""
    logger.warning("Legacy daily trigger ignored; configure each profile's posting times")


class SchedulerService:
    def __init__(self):
        self.scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
        self.owner = FileLock(str(_path("schedule-owner.lock")), timeout=0, thread_local=False)
        self.settings_signature = None
        self.started = False

    def start(self) -> bool:
        try:
            self.owner.acquire()
        except Timeout:
            logger.info("Auto-Pilot already owned by another backend/worker process")
            return False
        try:
            for entry in recent_history():
                if entry.get("status") == "running":
                    entry.update(status="interrupted", finished_at=_utc_now())
                    _record(entry)
            from app.services import job_store, master_agent, profile_schedules
            job_store.initialize()
            recovery = job_store.recover_stuck()
            if any(recovery.values()):
                logger.warning("Durable ledger recovery: {}", recovery)
            master_agent.initialize()
            profile_schedules.recover_interrupted()
            self.scheduler.add_job(profile_schedules.tick, "interval", minutes=1,
                                   id="autonomous-minute", next_run_time=datetime.now(timezone.utc))
            self.scheduler.add_job(master_agent.supervise, "interval", minutes=1,
                                   id="master-supervisor", next_run_time=datetime.now(timezone.utc))
            self.refresh()
            self.scheduler.add_job(self.refresh, "interval", seconds=15, id="refresh-settings")
            self.scheduler.start()
            self.started = True
            self.refresh()
            return True
        except Exception:
            self.owner.release()
            raise

    def refresh(self) -> None:
        try:
            from app.services import accounts, profile_schedules
            enabled = profile_schedules.master_enabled()
            profiles = accounts.list_profiles()
            overview = profile_schedules.overview(profiles)
            _write("schedule_status.json", {
                "heartbeat": _utc_now(), "running": enabled, "profiles": overview,
                "timezone": "Asia/Kolkata", "next_runs": [], "pid": os.getpid(),
            })
        except Exception as exc:
            for job in self.scheduler.get_jobs():
                if job.id.startswith(("daily-", "account-")):
                    self.scheduler.remove_job(job.id)
            self.settings_signature = None
            _write("schedule_status.json", {
                "heartbeat": _utc_now(), "running": False, "error_type": type(exc).__name__,
                "next_runs": [], "pid": os.getpid(),
            })
            logger.error(f"Auto-Pilot settings refresh failed: {type(exc).__name__}")

    def stop(self):
        if self.started:
            try:
                self.scheduler.shutdown(wait=True)
                from app.services.profile_schedules import _draining
                with _draining:
                    pass
                _write("schedule_status.json", {"heartbeat": _utc_now(), "running": False, "next_runs": []})
            finally:
                self.started = False
                self.owner.release()


def scheduler_status() -> dict:
    status = _read("schedule_status.json", {})
    heartbeat = status.get("heartbeat")
    alive = bool(heartbeat) and (
        datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)
    ).total_seconds() < 60
    return {**status, "worker_alive": alive, "running": alive and status.get("running", False)}


if __name__ == "__main__":
    service = SchedulerService()
    if not service.start():
        raise SystemExit("Another backend/worker already owns Auto-Pilot.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
