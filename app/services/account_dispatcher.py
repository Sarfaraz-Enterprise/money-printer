"""Isolated uploads to configured YouTube, Instagram, and Facebook accounts."""

from __future__ import annotations

import json
import mimetypes
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests
from filelock import FileLock
from loguru import logger

from app.services import accounts, job_store


_HTTP_TIMEOUT_SECONDS = 60
_INSTAGRAM_PROCESSING_POLLS = 30
_INSTAGRAM_POLL_SECONDS = 2
_SAFE_ERROR_CODES = {
    "invalid_profile_id",
    "profile_not_found",
    "profile_disabled",
    "platform_disabled",
    "no_enabled_platforms",
    "video_unavailable",
    "credentials_missing",
    "token_missing",
    "oauth_client_missing",
    "oauth_unavailable",
    "oauth_invalid_response",
    "resumable_url_invalid",
    "upload_invalid_response",
    "publish_invalid_response",
    "container_processing_failed",
    "container_processing_timeout",
    "upload_uncertain",
    "publish_uncertain",
    "upload_failed",
    "dispatch_paused",
}


def _error(code: str, *, account_id: str, platform: str) -> dict[str, Any]:
    uncertain = code in ("upload_uncertain", "publish_uncertain")
    return {
        "account_id": account_id,
        "profile_id": account_id,
        "platform": platform,
        "status": "uncertain" if uncertain else "failed",
        "success": False,
        "url": None,
        "error_code": code,
        "error": code,
    }


def _success(account_id: str, platform: str, url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    if platform == "youtube":
        external_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        external_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return {
        "account_id": account_id,
        "profile_id": account_id,
        "platform": platform,
        "status": "success",
        "success": True,
        "url": url,
        "external_id": external_id or None,
        "error_code": None,
        "error": None,
    }


def _media_type(video_path: Path) -> str:
    return mimetypes.guess_type(video_path.name)[0] or "video/mp4"


def _safe_response_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _youtube_upload(
    profile: dict[str, Any],
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
) -> str:
    account_id = profile["id"]
    token, token_code = accounts._refresh_youtube_token(account_id)
    if not token:
        raise RuntimeError(token_code)
    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:500],
            "categoryId": "22",
        },
        "status": {"privacyStatus": "public"},
    }
    init_url = "https://www.googleapis.com/upload/youtube/v3/videos"
    try:
        response = requests.post(
            init_url,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(video_path.stat().st_size),
                "X-Upload-Content-Type": _media_type(video_path),
            },
            json=metadata,
            timeout=_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError("api_unavailable") from exc
    if response.status_code not in (200, 201):
        raise RuntimeError(f"api_http_{response.status_code}")
    upload_url = response.headers.get("Location", "")
    parsed = urlparse(upload_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.googleapis.com"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path != "/upload/youtube/v3/videos"
    ):
        raise RuntimeError("resumable_url_invalid")
    try:
        with video_path.open("rb") as video:
            upload = requests.put(
                upload_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": _media_type(video_path),
                    "Content-Length": str(video_path.stat().st_size),
                },
                data=video,
                timeout=_HTTP_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
    except (OSError, requests.RequestException) as exc:
        raise RuntimeError("upload_uncertain") from exc
    if upload.status_code not in (200, 201):
        raise RuntimeError(f"upload_http_{upload.status_code}")
    video_id = _safe_response_json(upload).get("id")
    if not isinstance(video_id, str) or not video_id or len(video_id) > 200:
        raise RuntimeError("upload_invalid_response")
    return f"https://www.youtube.com/watch?v={video_id}"


def _graph_base() -> str:
    return f"https://graph.facebook.com/{accounts._graph_version()}"


def _instagram_upload(
    profile: dict[str, Any],
    video_path: Path,
    caption: str,
) -> str:
    instagram = profile.get("instagram") or {}
    account_id = instagram.get("business_account_id", "")
    token = instagram.get("access_token", "")
    if not account_id or not token:
        raise RuntimeError("credentials_missing")

    base = _graph_base()
    try:
        created = requests.post(
            f"{base}/{account_id}/media",
            params={"media_type": "REELS", "caption": caption[:2200], "upload_type": "resumable"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError("api_unavailable") from exc
    if created.status_code not in (200, 201):
        raise RuntimeError(f"api_http_{created.status_code}")
    container_id = _safe_response_json(created).get("id")
    if not isinstance(container_id, str) or not container_id or len(container_id) > 200:
        raise RuntimeError("upload_invalid_response")

    upload_url = f"https://rupload.facebook.com/ig-api-upload/{account_id}/{container_id}"
    parsed_upload_url = urlparse(upload_url)
    if (
        parsed_upload_url.scheme != "https"
        or parsed_upload_url.hostname != "rupload.facebook.com"
        or parsed_upload_url.port not in (None, 443)
        or parsed_upload_url.username
        or parsed_upload_url.password
    ):
        raise RuntimeError("resumable_url_invalid")
    try:
        with video_path.open("rb") as video:
            uploaded = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(video_path.stat().st_size),
                    "Content-Type": "application/octet-stream",
                },
                data=video,
                timeout=_HTTP_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
    except (OSError, requests.RequestException) as exc:
        raise RuntimeError("upload_uncertain") from exc
    if uploaded.status_code not in (200, 201):
        raise RuntimeError(f"upload_http_{uploaded.status_code}")

    ready = False
    for attempt in range(_INSTAGRAM_PROCESSING_POLLS):
        try:
            status = requests.get(
                f"{base}/{container_id}",
                params={"fields": "status_code", "access_token": token},
                timeout=_HTTP_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError("api_unavailable") from exc
        if status.status_code != 200:
            raise RuntimeError(f"api_http_{status.status_code}")
        status_code = _safe_response_json(status).get("status_code")
        if status_code == "FINISHED":
            ready = True
            break
        if status_code == "ERROR":
            raise RuntimeError("container_processing_failed")
        if status_code not in ("IN_PROGRESS", "PUBLISHED", None):
            raise RuntimeError("container_processing_failed")
        if attempt + 1 < _INSTAGRAM_PROCESSING_POLLS:
            time.sleep(_INSTAGRAM_POLL_SECONDS)
    if not ready:
        raise RuntimeError("container_processing_timeout")

    try:
        published = requests.post(
            f"{base}/{account_id}/media_publish",
            params={"creation_id": container_id, "access_token": token},
            timeout=_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError("publish_uncertain") from exc
    if published.status_code not in (200, 201):
        raise RuntimeError(f"publish_http_{published.status_code}")
    media_id = _safe_response_json(published).get("id")
    if not isinstance(media_id, str) or not media_id or len(media_id) > 200:
        raise RuntimeError("publish_invalid_response")
    return f"https://www.instagram.com/reel/{media_id}/"


def _facebook_upload(
    profile: dict[str, Any],
    video_path: Path,
    title: str,
    description: str,
) -> str:
    facebook = profile.get("facebook") or {}
    page_id = facebook.get("page_id", "")
    token = facebook.get("page_access_token", "")
    if not page_id or not token:
        raise RuntimeError("credentials_missing")
    url = f"{_graph_base()}/{page_id}/videos"
    try:
        with video_path.open("rb") as video:
            response = requests.post(
                url,
                data={
                    "title": title[:100],
                    "description": description[:5000],
                    "access_token": token,
                },
                files={
                    "source": (
                        video_path.name,
                        video,
                        _media_type(video_path),
                    )
                },
                timeout=_HTTP_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
    except (OSError, requests.RequestException) as exc:
        raise RuntimeError("upload_uncertain") from exc
    if response.status_code not in (200, 201):
        raise RuntimeError(f"api_http_{response.status_code}")
    video_id = _safe_response_json(response).get("id")
    if not isinstance(video_id, str) or not video_id or len(video_id) > 200:
        raise RuntimeError("upload_invalid_response")
    return f"https://www.facebook.com/{video_id}"


def _dispatch_one(
    profile: dict[str, Any],
    platform: str,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
) -> dict[str, Any]:
    profile_id = profile["id"]
    if profile.get("enabled") is not True:
        return _error("profile_disabled", account_id=profile_id, platform=platform)
    if platform not in profile.get("enabled_platforms", []):
        return _error("platform_disabled", account_id=profile_id, platform=platform)
    try:
        if platform == "youtube":
            url = _youtube_upload(profile, video_path, title, description, tags)
        elif platform == "instagram":
            url = _instagram_upload(profile, video_path, description or title)
        elif platform == "facebook":
            url = _facebook_upload(profile, video_path, title, description)
        else:
            return _error("unsupported_platform", account_id=profile_id, platform=platform)
        return _success(profile_id, platform, url)
    except RuntimeError as exc:
        code = str(exc)
        if not (
            code in {
                "credentials_missing",
                "token_missing",
                "oauth_client_missing",
                "oauth_unavailable",
                "oauth_invalid_response",
                "resumable_url_invalid",
                "upload_invalid_response",
                "publish_invalid_response",
                "container_processing_failed",
                "container_processing_timeout",
                "upload_uncertain",
                "publish_uncertain",
            }
            or code.startswith(
                (
                    "oauth_http_",
                    "api_http_",
                    "upload_http_",
                    "publish_http_",
                )
            )
        ):
            code = "upload_failed"
        return _error(code, account_id=profile_id, platform=platform)
    except Exception:
        # Provider exceptions can contain access tokens and raw response bodies.
        return _error("upload_failed", account_id=profile_id, platform=platform)


def _append_history(results: list[dict[str, Any]]) -> None:
    storage = accounts._storage_dir()
    storage.mkdir(parents=True, exist_ok=True)
    history_path = storage / "dispatch_history.json"
    lock_path = storage / "dispatch_history.lock"
    with FileLock(str(lock_path)):
        try:
            with history_path.open("r", encoding="utf-8") as handle:
                history = json.load(handle)
            if not isinstance(history, list):
                history = []
        except (OSError, json.JSONDecodeError):
            history = []
        stamp = datetime.now(timezone.utc).isoformat()
        for result in results:
            history.append(
                {
                    "timestamp": stamp,
                    "account_id": result["account_id"],
                    "profile_id": result["profile_id"],
                    "name": result.get("name", ""),
                    "platform": result["platform"],
                    "status": result["status"],
                    "success": result["success"],
                    "url": result["url"],
                    "error_code": result["error_code"],
                    "error": result["error"],
                }
            )
        accounts._atomic_json_write(history_path, history[-1000:])


def dispatch_video(
    video_path: str,
    title: str,
    description: str,
    profile_ids: list[str],
    tags: list[str] | None = None,
    *,
    dispatch_allowed: Callable[[], bool] | None = None,
    job_id: str = "",
) -> list[dict[str, Any]]:
    """Publish a local video to each selected profile's enabled platforms."""
    safe_tags = [str(tag)[:100] for tag in (tags or []) if str(tag).strip()][:500]
    selected = list(profile_ids or [])
    profiles: dict[str, dict[str, Any]] = {}
    for profile in accounts.list_profiles(include_secrets=True):
        profiles[profile["id"]] = profile
    try:
        path = Path(video_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise OSError("not a file")
        path.stat()
        video_error = None
    except (OSError, TypeError, ValueError):
        path = None
        video_error = "video_unavailable"

    ledger_job = job_store.get_job(job_id) if job_id else None
    dispatch_inputs = {
        "profiles": selected,
        "title": str(title or ""),
        "description": str(description or ""),
        "tags": safe_tags,
    }
    if ledger_job is not None:
        job_store.stage_begin(job_id, "publish", dispatch_inputs)

    results: list[dict[str, Any]] = []
    for raw_id in selected:
        try:
            account_id = accounts._validated_id(raw_id)
        except ValueError:
            results.append(_error("invalid_profile_id", account_id="", platform="account"))
            continue
        profile = profiles.get(account_id)
        if profile is None:
            results.append(_error("profile_not_found", account_id=account_id, platform="account"))
            continue
        platforms = profile.get("enabled_platforms", [])
        if not platforms:
            results.append(
                _error("no_enabled_platforms", account_id=account_id, platform="account")
            )
            continue
        for platform in platforms:
            allowed = True
            if dispatch_allowed is not None:
                try:
                    allowed = dispatch_allowed() is True
                except Exception as exc:
                    logger.error("Dispatch authorization check failed: {}", type(exc).__name__)
                    allowed = False
            if not allowed:
                result = _error("dispatch_paused", account_id=account_id, platform=platform)
                result["status"] = "cancelled"
            elif video_error:
                result = _error(video_error, account_id=account_id, platform=platform)
            elif path is None:
                result = _error("video_unavailable", account_id=account_id, platform=platform)
            else:
                reservation = job_store.reserve_post(job_id, account_id, platform) if job_id else None
                if reservation and not reservation["created"]:
                    prior_status = reservation["status"]
                    result = {
                        "account_id": account_id, "profile_id": account_id, "platform": platform,
                        "status": "success" if prior_status == "success" else
                                  "uncertain" if prior_status in {"in_flight", "uncertain"} else prior_status,
                        "success": prior_status == "success",
                        "url": reservation.get("post_url"), "error_code": reservation.get("error_class"),
                        "error": reservation.get("error_class"),
                    }
                else:
                    result = _dispatch_one(
                        profile, platform, path, str(title or ""), str(description or ""), safe_tags,
                    )
                    if job_id:
                        status = ("success" if result.get("success") else
                                  "uncertain" if result.get("status") == "uncertain" else "failed")
                        job_store.finish_post(
                            job_id, account_id, platform, status=status,
                            external_id=result.get("external_id") or result.get("post_id") or "",
                            post_url=result.get("url") or "",
                            error_class=result.get("error_code") or "",
                        )
            results.append(result)
    names = {profile["id"]: profile.get("name", "") for profile in profiles.values()}
    for result in results:
        result["name"] = names.get(result["account_id"], "")
    try:
        _append_history(results)
    except Exception as exc:
        # History persistence failure must not hide publication outcomes.
        logger.error("Dispatch history could not be saved: {}", type(exc).__name__)
    if ledger_job is not None:
        job_store.stage_succeed(
            job_id, "publish",
            {"results": [
                {key: row.get(key) for key in ("profile_id", "platform", "status", "error_code", "url")}
                for row in results
            ]},
            input_data=dispatch_inputs,
            cacheable=False,
        )
    return results


def _clean_history_error(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value in _SAFE_ERROR_CODES:
        return value
    if re.fullmatch(r"(?:oauth|api|upload|publish)_http_[1-5]\d\d", value):
        return value
    return "upload_failed"


def recent_dispatches(limit: int = 50) -> list[dict[str, Any]]:
    """Return newest token-free dispatch rows suitable for a UI table."""
    try:
        count = max(0, min(int(limit), 1000))
    except (TypeError, ValueError):
        count = 50
    if count == 0:
        return []
    storage = accounts._storage_dir()
    history_path = storage / "dispatch_history.json"
    try:
        with FileLock(str(storage / "dispatch_history.lock")):
            with history_path.open("r", encoding="utf-8") as handle:
                history = json.load(handle)
        if not isinstance(history, list):
            return []
    except (OSError, json.JSONDecodeError):
        return []
    profile_names = {
        profile["id"]: profile["name"]
        for profile in accounts.list_profiles(include_secrets=False)
    }
    rows = []
    for item in reversed(history[-count:]):
        if not isinstance(item, dict):
            continue
        profile_id = item.get("profile_id", item.get("account_id", ""))
        platform = item.get("platform", "")
        if not isinstance(profile_id, str) or not isinstance(platform, str):
            continue
        status = item.get("status")
        success = item.get("success") is True or status == "success"
        if status not in ("success", "failed", "uncertain"):
            status = "success" if success else "failed"
        url = item.get("url")
        allowed_url_prefixes = (
            "https://www.youtube.com/",
            "https://www.facebook.com/",
            "https://www.instagram.com/",
        )
        if not isinstance(url, str) or not url.startswith(allowed_url_prefixes):
            url = None
        error = _clean_history_error(item.get("error_code", item.get("error")))
        rows.append(
            {
                "profile_id": profile_id,
                "account_id": profile_id,
                "name": profile_names.get(profile_id, str(item.get("name", ""))[:200]),
                "platform": platform,
                "status": status,
                "success": success,
                "url": url,
                "error": error,
                "error_code": error,
            }
        )
    return rows
