"""Bounded background studio jobs; Streamlit reruns never own paid operations."""

import threading
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoParams
from app.services import accounts, job_store, state, task

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autoflow-studio")
_slot = threading.BoundedSemaphore(1)


def submit(profile_id: str, topic: str, script: str, voice_name: str,
           video_source: str, targets: list[str], authorized: bool, keywords: str = "", description: str = "") -> str:
    profile = next((row for row in accounts.list_profiles() if row["id"] == profile_id), None)
    if profile is None:
        raise ValueError("Select an existing channel profile")
    if targets and not authorized:
        raise ValueError("Authorize dispatch before selecting publishing targets")
    if not _slot.acquire(blocking=False):
        raise ValueError("A studio render is already active. Wait for it to finish.")
    task_id = str(uuid.uuid4())
    try:
        state.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=0)
        _executor.submit(_run, task_id, profile, topic, script, voice_name, video_source, targets, keywords, description)
    except Exception:
        _slot.release()
        raise
    return task_id


def _run(task_id, profile, topic, script, voice_name, video_source, targets, keywords="", description=""):
    job = None
    try:
        from app.services.profile_content import generate_profile_package
        job = {
            "job_id": task_id, "profile_id": profile["id"], "status": "running",
            "stage": "preflight", "attempts": 1, "started_at": datetime.now(timezone.utc).isoformat(),
            "manual": True,
        }
        job_store.record_job(job)
        with config.runtime_config_lock():
            content_inputs = {
                "profile_id": profile["id"], "topic": topic.strip(),
                "manual_script": script.strip(), "keywords": keywords.strip(),
            }
            package = job_store.cached_output(task_id, "content", content_inputs)
            if package is None:
                job_store.stage_begin(task_id, "content", content_inputs)
                try:
                    if script.strip():
                        package = {
                            "subject": topic.strip() or profile["name"],
                            "script": script.strip(),
                            "keywords": keywords or profile.get("visual_keywords_preset") or profile.get("visual_style") or "cinematic night, dramatic shadows, silhouette",
                        }
                    else:
                        package = generate_profile_package(profile, topic)
                except Exception as content_exc:
                    job_store.stage_fail(
                        task_id, "content", type(content_exc).__name__,
                        "Manual Studio content preparation failed.",
                    )
                    raise
                job_store.stage_succeed(
                    task_id, "content", dict(package), input_data=content_inputs,
                )
            if keywords.strip():
                package["keywords"] = keywords.strip()
            params = VideoParams(
                video_subject=package["subject"], video_script=package["script"],
                video_terms=package["keywords"], profile_id=profile["id"],
                video_language="en-US", voice_name=voice_name,
                video_source=video_source, bgm_type="none", bgm_volume=0,
                font_name="BeVietnamPro-Bold.ttf", target_account_ids=targets,
                video_description=description.strip() or package.get("description", ""),
            )
            task.start(task_id, params, auto_publish=bool(targets))
        result = state.state.get_task(task_id) or {}
        for path in result.get("videos") or []:
            if Path(path).is_file():
                job_store.record_artifact(task_id, "final_video", path)
        if not targets or result.get("state") != const.TASK_STATE_COMPLETE:
            job.update(
                status="complete" if result.get("state") == const.TASK_STATE_COMPLETE else "failed",
                stage="complete" if result.get("state") == const.TASK_STATE_COMPLETE else "render",
                subject=package["subject"], task_id=task_id,
            )
            job_store.record_job(job)
    except Exception as exc:
        logger.error(f"Studio preparation failed: {type(exc).__name__}")
        if job is not None:
            job.update(status="failed", stage=job.get("stage", "preflight"), error_type=type(exc).__name__)
            try:
                job_store.record_job(job)
            except Exception as store_exc:
                logger.error("Failed to persist studio job failure: {}", type(store_exc).__name__)
        state.state.update_task(task_id, state=const.TASK_STATE_FAILED, progress=0,
                                error="Preparation failed. Check profile settings and API connectivity.")
    finally:
        _slot.release()
