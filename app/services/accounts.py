"""Private, per-profile social account storage and YouTube OAuth helpers."""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from filelock import FileLock
from app.config import env_secrets
from app.utils.profile_ids import validate_profile_id


STORAGE_DIR = Path(__file__).resolve().parents[2] / "storage"
_PLATFORMS = ("youtube", "facebook", "instagram")
_TIME_SLOT = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_OAUTH_TIMEOUT_SECONDS = 300
_HTTP_TIMEOUT_SECONDS = 15

_oauth_sessions: dict[str, dict[str, Any]] = {}
_oauth_sessions_lock = threading.RLock()


def _storage_dir() -> Path:
    return Path(STORAGE_DIR)


def _accounts_path() -> Path:
    return _storage_dir() / "accounts.json"


def _lock_path() -> Path:
    return _storage_dir() / "accounts.lock"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_json_write(path: Path, value: Any) -> None:
    _ensure_private_dir(path.parent)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _load_profiles_unlocked() -> list[dict[str, Any]]:
    path = _accounts_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            content = json.load(handle)
        if isinstance(content, dict):
            content = content.get("profiles", [])
        if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
            raise ValueError("invalid_accounts_file")
        return content
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_accounts_file") from exc


def _read_profiles() -> list[dict[str, Any]]:
    _ensure_private_dir(_storage_dir())
    with FileLock(str(_lock_path())):
        return _load_profiles_unlocked()


def _validated_id(value: Any, *, generate: bool = False) -> str:
    if value in (None, "") and generate:
        return str(uuid.uuid4())
    return validate_profile_id(value)


def _tokens_dir() -> Path:
    storage = _storage_dir().resolve()
    tokens = _storage_dir() / "tokens"
    resolved = tokens.resolve()
    if resolved.parent != storage:
        raise ValueError("invalid_token_storage")
    _ensure_private_dir(resolved)
    return resolved


def _token_path(profile_id: str, provider: str) -> Path:
    safe_id = _validated_id(profile_id)
    if provider not in ("yt", "meta"):
        raise ValueError("invalid_token_provider")
    directory = _tokens_dir()
    result = directory / f"{provider}_{safe_id}.json"
    resolved = result.resolve()
    if resolved.parent != directory:
        raise ValueError("invalid_token_path")
    return result


def _read_secret_file(profile_id: str, provider: str) -> dict[str, Any]:
    path = _token_path(profile_id, provider)
    try:
        with path.open("r", encoding="utf-8") as handle:
            content = json.load(handle)
        return content if isinstance(content, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_secret_file(profile_id: str, provider: str, data: dict[str, Any]) -> None:
    path = _token_path(profile_id, provider)
    _atomic_json_write(path, data)


def _clean_text(value: Any, *, max_length: int = 200) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_length]


def _normalize_enabled_platforms(value: Any, profile: dict[str, Any]) -> list[str]:
    if isinstance(value, dict):
        return [name for name in _PLATFORMS if value.get(name) is True]
    if isinstance(value, (list, tuple, set)):
        enabled = {str(item).strip().lower() for item in value}
        return [name for name in _PLATFORMS if name in enabled]
    legacy_enabled = profile.get("platforms")
    if isinstance(legacy_enabled, dict):
        return [name for name in _PLATFORMS if legacy_enabled.get(name) is True]
    return []


def _normalize_schedule(schedule: Any, previous: Any = None) -> dict[str, Any]:
    defaults = {
        "enabled": False,
        "timezone": "Asia/Kolkata",
        "slots": ["05:00", "20:00"],
        "niche_style": "afterframe",
        "sub_theme_focus": "",
    }
    value = dict(previous) if isinstance(previous, dict) else {}
    if isinstance(schedule, dict):
        value.update(schedule)
    result = {
        "enabled": value.get("enabled", defaults["enabled"]) is True,
        "timezone": _clean_text(value.get("timezone", defaults["timezone"]), max_length=100),
        "slots": (next(schedule[key] for key in ("post_times_ist", "post_times", "slots") if key in schedule)
                  if isinstance(schedule, dict) and any(key in schedule for key in ("post_times_ist", "post_times", "slots"))
                  else value.get("post_times_ist", value.get("post_times", value.get("slots", defaults["slots"])))),
        "niche_style": value.get("niche_style", defaults["niche_style"]),
        "sub_theme_focus": _clean_text(value.get("sub_theme_focus", ""), max_length=500),
    }
    if not result["timezone"]:
        raise ValueError("invalid_schedule_timezone")
    try:
        ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid_schedule_timezone") from exc
    slots = result["slots"]
    if not isinstance(slots, (list, tuple)) or not 1 <= len(slots) <= 1440:
        raise ValueError("invalid_schedule_slots")
    normalized_slots = []
    for slot in slots:
        text = str(slot)
        if not _TIME_SLOT.fullmatch(text):
            raise ValueError("invalid_schedule_slots")
        if text not in normalized_slots:
            normalized_slots.append(text)
    result["slots"] = normalized_slots
    result["post_times"] = normalized_slots
    result["post_times_ist"] = normalized_slots
    if result["niche_style"] not in ("afterframe", "custom"):
        raise ValueError("invalid_schedule_niche_style")
    return result


def _profile_inputs(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    youtube = profile.get("youtube") if isinstance(profile.get("youtube"), dict) else {}
    facebook = profile.get("facebook") if isinstance(profile.get("facebook"), dict) else {}
    instagram = profile.get("instagram") if isinstance(profile.get("instagram"), dict) else {}
    return youtube, facebook, instagram


def _creative_fields(profile: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    value = {**(previous or {}), **profile}
    zone = _clean_text(value.get("timezone", "America/New_York"), max_length=100)
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid_audience_timezone") from exc
    prompt = profile.get("system_prompt", profile.get("niche_prompt", value.get("system_prompt", value.get("niche_prompt", ""))))
    visuals = profile.get("visual_keywords_preset", profile.get("visual_style", value.get("visual_keywords_preset", value.get("visual_style", ""))))
    voice_name = _clean_text(value.get("voice_name", "elevenlabs"), max_length=250)
    if not re.fullmatch(r"elevenlabs(?::[A-Za-z0-9_-]*(?::[^\r\n]{0,200})?)?", voice_name):
        raise ValueError("invalid_profile_voice")
    tags = value.get("audience_tags", [])
    if not isinstance(tags, list) or len(tags) > 30 or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("invalid_audience_tags")
    return {
        "target_audience": _clean_text(value.get("target_audience", "United States (US Tier-1)"), max_length=300),
        "audience_tags": list(dict.fromkeys(tag.strip()[:80] for tag in tags if tag.strip())),
        "timezone": zone,
        "system_prompt": _clean_text(prompt, max_length=4000),
        "niche_prompt": _clean_text(prompt, max_length=4000),
        "visual_keywords_preset": _clean_text(visuals, max_length=2000),
        "visual_style": _clean_text(visuals, max_length=2000),
        "voice_name": voice_name,
        "voice_direction": _clean_text(value.get("voice_direction", "Atmospheric American English, controlled suspense and natural pauses"), max_length=1000),
    }


def _token_input(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _profile_secret_data(profile_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    youtube = _read_secret_file(profile_id, "yt")
    meta = _read_secret_file(profile_id, "meta")
    return youtube, meta


def _public_profile(profile: dict[str, Any], *, include_secrets: bool = False) -> dict[str, Any]:
    profile_id = _validated_id(profile.get("id"))
    yt_file = _token_path(profile_id, "yt")
    meta_file = _token_path(profile_id, "meta")
    yt_secret, meta_secret = _profile_secret_data(profile_id)
    page_id = _clean_text(profile.get("page_id"), max_length=200)
    business_id = _clean_text(profile.get("business_account_id"), max_length=200)
    page_token = _token_input(meta_secret.get("page_access_token"))
    instagram_token = _token_input(meta_secret.get("instagram_access_token"), page_token)
    yt_connected = bool(_token_input(yt_secret.get("refresh_token"), yt_secret.get("access_token")))
    facebook_connected = bool(page_id and page_token)
    instagram_connected = bool(business_id and instagram_token)
    platforms = _normalize_enabled_platforms(
        profile.get("destinations", profile.get("enabled_platforms", profile.get("platforms"))), profile
    )
    result: dict[str, Any] = {
        "id": profile_id,
        "name": _clean_text(profile.get("name"), max_length=200),
        "niche": _clean_text(profile.get("category", profile.get("niche")), max_length=2000),
        "category": _clean_text(profile.get("category", profile.get("niche")), max_length=2000),
        "niche_prompt": _clean_text(profile.get("niche_prompt"), max_length=4000),
        "visual_style": _clean_text(profile.get("visual_style"), max_length=2000),
        "enabled": profile.get("enabled", True) is True,
        "enabled_platforms": platforms,
        "destinations": {name: name in platforms for name in _PLATFORMS},
        "youtube": {
            "enabled": "youtube" in platforms,
            "connected": yt_connected,
            "token_path": str(yt_file),
        },
        "facebook": {
            "enabled": "facebook" in platforms,
            "connected": facebook_connected,
            "page_id": page_id,
        },
        "instagram": {
            "enabled": "instagram" in platforms,
            "connected": instagram_connected,
            "business_account_id": business_id,
        },
        "schedule": _normalize_schedule(profile.get("schedule")),
        **_creative_fields(profile),
    }
    if include_secrets:
        result["youtube"].update(yt_secret)
        result["facebook"]["page_access_token"] = page_token or ""
        result["instagram"]["access_token"] = instagram_token or ""
    return result


def list_profiles(include_secrets: bool = False) -> list[dict[str, Any]]:
    """Return profiles, omitting credentials unless explicitly requested."""
    profiles = _read_profiles()
    summaries = []
    for item in profiles:
        summaries.append(_public_profile(item, include_secrets=include_secrets))
    return summaries


def save_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Create or update a profile and preserve any secrets submitted as blank."""
    if not isinstance(profile, dict):
        raise ValueError("invalid_profile")
    profile_id = _validated_id(profile.get("id"), generate=True)
    submitted_yt, submitted_fb, submitted_ig = _profile_inputs(profile)
    _ensure_private_dir(_storage_dir())
    with FileLock(str(_lock_path())):
        profiles = _load_profiles_unlocked()
        old = next((item for item in profiles if item.get("id") == profile_id), {})
        old_yt, old_meta = _profile_secret_data(profile_id)
        yt_secret = dict(old_yt)
        meta_secret = dict(old_meta)
        youtube_refresh = _token_input(
            submitted_yt.get("refresh_token"),
            profile.get("youtube_refresh_token"),
        )
        if youtube_refresh:
            yt_secret["refresh_token"] = youtube_refresh
        youtube_access = _token_input(submitted_yt.get("access_token"))
        if youtube_access:
            yt_secret["access_token"] = youtube_access
        for key in ("token_type", "scope", "expires_at"):
            value = submitted_yt.get(key)
            if value not in (None, ""):
                yt_secret[key] = value

        page_token = _token_input(
            submitted_fb.get("page_access_token"),
            submitted_fb.get("access_token"),
            profile.get("page_access_token"),
            profile.get("meta_page_access_token"),
        )
        instagram_token = _token_input(
            submitted_ig.get("access_token"),
            submitted_ig.get("page_access_token"),
            profile.get("instagram_access_token"),
        )
        if page_token:
            meta_secret["page_access_token"] = page_token
        if instagram_token:
            meta_secret["instagram_access_token"] = instagram_token

        submitted_schedule = profile.get("schedule")
        schedule = _normalize_schedule(submitted_schedule, old.get("schedule"))
        platforms = _normalize_enabled_platforms(
            profile.get("destinations", profile.get("enabled_platforms", profile.get("platforms", old.get("destinations", old.get("enabled_platforms"))))),
            profile,
        )
        if not platforms and not any(key in profile for key in ("destinations", "enabled_platforms", "platforms")):
            platforms = _normalize_enabled_platforms(old.get("destinations", old.get("enabled_platforms")), old)
        if not any(key in profile for key in ("destinations", "enabled_platforms", "platforms")):
            platforms = list(platforms)
            for name, submitted in zip(_PLATFORMS, (submitted_yt, submitted_fb, submitted_ig)):
                if "enabled" in submitted:
                    platforms = [item for item in platforms if item != name]
                    if submitted["enabled"] is True:
                        platforms.append(name)
        data = {
            "id": profile_id,
            "name": _clean_text(profile.get("name", old.get("name", "")), max_length=200),
            "niche": _clean_text(profile.get("category", profile.get("niche", old.get("category", old.get("niche", "")))), max_length=2000),
            "category": _clean_text(profile.get("category", profile.get("niche", old.get("category", old.get("niche", "")))), max_length=2000),
            "niche_prompt": _clean_text(profile.get("niche_prompt", old.get("niche_prompt", "")), max_length=4000),
            "visual_style": _clean_text(
                profile.get("visual_style", old.get("visual_style", "")), max_length=2000
            ),
            "enabled": profile.get("enabled", old.get("enabled", True)) is True,
            "enabled_platforms": platforms,
            "destinations": {name: name in platforms for name in _PLATFORMS},
            "page_id": _clean_text(
                profile.get("page_id", submitted_fb.get("page_id", old.get("page_id", ""))),
                max_length=200,
            ),
            "business_account_id": _clean_text(
                profile.get(
                    "business_account_id",
                    submitted_ig.get("business_account_id", old.get("business_account_id", "")),
                ),
                max_length=200,
            ),
            "schedule": schedule,
            **_creative_fields(profile, old),
        }
        # Persist credentials before the profile so a connected flag never points at a partial file.
        if yt_secret:
            _write_secret_file(profile_id, "yt", yt_secret)
        if meta_secret:
            _write_secret_file(profile_id, "meta", meta_secret)
        replaced = False
        for index, item in enumerate(profiles):
            if item.get("id") == profile_id:
                profiles[index] = data
                replaced = True
                break
        if not replaced:
            profiles.append(data)
        _atomic_json_write(_accounts_path(), {"profiles": profiles})
    return _public_profile(data)


def _secure_remove(path: Path) -> None:
    try:
        if path.is_symlink():
            path.unlink(missing_ok=True)
            return
        if path.is_file():
            try:
                size = path.stat().st_size
                with path.open("r+b") as handle:
                    remaining = size
                    zeros = b"\x00" * min(64 * 1024, max(1, size))
                    while remaining:
                        chunk = zeros[: min(remaining, len(zeros))]
                        handle.write(chunk)
                        remaining -= len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
            path.unlink(missing_ok=True)
    except OSError:
        pass


def delete_profile(profile_id: str) -> bool:
    """Delete only the selected profile and its two profile-scoped token files."""
    safe_id = _validated_id(profile_id)
    _ensure_private_dir(_storage_dir())
    with FileLock(str(_lock_path())):
        profiles = _load_profiles_unlocked()
        updated = [item for item in profiles if item.get("id") != safe_id]
        if len(updated) == len(profiles):
            return False
        _atomic_json_write(_accounts_path(), {"profiles": updated})
        for provider in ("yt", "meta"):
            _secure_remove(_token_path(safe_id, provider))
    with _oauth_sessions_lock:
        session = _oauth_sessions.pop(safe_id, None)
    if session:
        try:
            session["server"].shutdown()
            session["server"].server_close()
        except OSError:
            pass
    return True


def _safe_result(platform: str, *, ok: bool, code: str) -> dict[str, Any]:
    return {"ok": ok, "code": code}


def _refresh_youtube_token(profile_id: str, *, persist: bool = True) -> tuple[str | None, str]:
    client_id = env_secrets.global_value("GOOGLE_CLIENT_ID")
    client_secret = env_secrets.global_value("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None, "oauth_client_missing"
    token_data = _read_secret_file(profile_id, "yt")
    refresh_token = _token_input(token_data.get("refresh_token"))
    if not refresh_token:
        return None, "token_missing"
    try:
        response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return None, f"oauth_http_{response.status_code}"
        payload = response.json()
        access_token = _token_input(payload.get("access_token"))
        if not access_token:
            return None, "oauth_invalid_response"
        token_data["access_token"] = access_token
        token_data["token_type"] = payload.get("token_type", "Bearer")
        token_data["expires_at"] = int(time.time()) + int(payload.get("expires_in", 3600))
        if payload.get("refresh_token"):
            token_data["refresh_token"] = payload["refresh_token"]
        if persist:
            with FileLock(str(_lock_path())):
                _write_secret_file(profile_id, "yt", token_data)
        return access_token, "ok"
    except (requests.RequestException, ValueError, TypeError, OSError):
        return None, "oauth_unavailable"


def validate_profile(profile_id: str) -> dict[str, Any]:
    """Validate configured credentials using only read-only provider API calls."""
    safe_id = _validated_id(profile_id)
    profiles = _read_profiles()
    profile = next((item for item in profiles if item.get("id") == safe_id), None)
    if profile is None:
        raise ValueError("profile_not_found")
    summary = _public_profile(profile)
    results: dict[str, Any] = {"id": safe_id, "platforms": {}}
    if summary["youtube"]["connected"]:
        token, code = _refresh_youtube_token(safe_id)
        if token:
            try:
                response = requests.get(
                    "https://www.googleapis.com/youtube/v3/channels",
                    params={"part": "id", "mine": "true"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_HTTP_TIMEOUT_SECONDS,
                )
                ok = response.status_code == 200
                code = "ok" if ok else f"api_http_{response.status_code}"
            except requests.RequestException:
                ok, code = False, "api_unavailable"
        else:
            ok = False
        results["platforms"]["youtube"] = _safe_result("youtube", ok=ok, code=code)
    else:
        results["platforms"]["youtube"] = _safe_result(
            "youtube", ok=False, code="credentials_missing"
        )

    meta = _read_secret_file(safe_id, "meta")
    page_token = _token_input(meta.get("page_access_token"))
    page_id = summary["facebook"]["page_id"]
    if page_token and page_id:
        results["platforms"]["facebook"] = _validate_graph(
            f"{page_id}", page_token, fields="id,name"
        )
    else:
        results["platforms"]["facebook"] = _safe_result(
            "facebook", ok=False, code="credentials_missing"
        )

    ig_token = _token_input(meta.get("instagram_access_token"), page_token)
    business_id = summary["instagram"]["business_account_id"]
    if ig_token and business_id:
        results["platforms"]["instagram"] = _validate_graph(
            business_id, ig_token, fields="id,username"
        )
    else:
        results["platforms"]["instagram"] = _safe_result(
            "instagram", ok=False, code="credentials_missing"
        )
    return results


def _graph_version() -> str:
    version = os.environ.get("META_GRAPH_VERSION", "v21.0").strip()
    if not re.fullmatch(r"v\d+\.\d+", version):
        return "v21.0"
    return version


def _validate_graph(object_id: str, token: str, *, fields: str) -> dict[str, Any]:
    try:
        response = requests.get(
            f"https://graph.facebook.com/{_graph_version()}/{object_id}",
            params={"fields": fields, "access_token": token},
            timeout=_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        ok = response.status_code == 200
        code = "ok" if ok else f"api_http_{response.status_code}"
    except requests.RequestException:
        ok, code = False, "api_unavailable"
    return _safe_result("meta", ok=ok, code=code)


def _oauth_callback_page(message: str) -> bytes:
    text = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>YouTube authorization</title></head><body>"
        f"<p>{message}</p></body></html>"
    )
    return text.encode("utf-8")


def _oauth_exchange(profile_id: str, code: str, redirect_uri: str) -> tuple[bool, str]:
    client_id = env_secrets.global_value("GOOGLE_CLIENT_ID")
    client_secret = env_secrets.global_value("GOOGLE_CLIENT_SECRET")
    try:
        response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False, f"oauth_http_{response.status_code}"
        payload = response.json()
        refresh_token = _token_input(payload.get("refresh_token"))
        access_token = _token_input(payload.get("access_token"))
        if not refresh_token and not _read_secret_file(profile_id, "yt").get("refresh_token"):
            return False, "refresh_token_missing"
        token_data = _read_secret_file(profile_id, "yt")
        if refresh_token:
            token_data["refresh_token"] = refresh_token
        if access_token:
            token_data["access_token"] = access_token
        if payload.get("token_type"):
            token_data["token_type"] = payload["token_type"]
        if payload.get("scope"):
            token_data["scope"] = payload["scope"]
        if payload.get("expires_in"):
            token_data["expires_at"] = int(time.time()) + int(payload["expires_in"])
        with FileLock(str(_lock_path())):
            _write_secret_file(profile_id, "yt", token_data)
        return True, "ok"
    except (requests.RequestException, ValueError, TypeError, OSError):
        return False, "oauth_unavailable"


def start_youtube_oauth(profile_id: str) -> str:
    """Start a loopback callback listener and return a Google authorization URL."""
    safe_id = _validated_id(profile_id)
    if not any(item.get("id") == safe_id for item in _read_profiles()):
        raise ValueError("profile_not_found")
    client_id = env_secrets.global_value("GOOGLE_CLIENT_ID")
    client_secret = env_secrets.global_value("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("oauth_client_missing")
    with _oauth_sessions_lock:
        old = _oauth_sessions.pop(safe_id, None)
        if old:
            try:
                old["server"].shutdown()
                old["server"].server_close()
            except OSError:
                pass
        state = uuid.uuid4().hex + uuid.uuid4().hex
        session: dict[str, Any] = {
            "state": state,
            "status": "pending",
            "code": "awaiting_callback",
            "expires_at": time.time() + _OAUTH_TIMEOUT_SECONDS,
            "server": None,
        }

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                supplied_state = (query.get("state") or [""])[0]
                authorization_code = (query.get("code") or [""])[0]
                if parsed.path != "/oauth2callback" or not hmac.compare_digest(
                    supplied_state, state
                ):
                    message = "Authorization failed. You can close this window."
                    self.send_response(400)
                    session["status"], session["code"] = "error", "state_mismatch"
                elif not authorization_code:
                    message = "Authorization was not completed. You can close this window."
                    self.send_response(400)
                    session["status"], session["code"] = "error", "authorization_denied"
                else:
                    ok, code_result = _oauth_exchange(
                        safe_id, authorization_code, redirect_uri
                    )
                    message = (
                        "YouTube account connected. You can close this window."
                        if ok
                        else "Authorization failed. You can close this window."
                    )
                    self.send_response(200 if ok else 400)
                    session["status"] = "connected" if ok else "error"
                    session["code"] = code_result
                body = _oauth_callback_page(message)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                threading.Thread(
                    target=session["server"].shutdown, daemon=True
                ).start()

            def log_message(self, format: str, *args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        server.timeout = _HTTP_TIMEOUT_SECONDS
        redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
        session["server"] = server
        _oauth_sessions[safe_id] = session
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def expire_session() -> None:
            with _oauth_sessions_lock:
                current = _oauth_sessions.get(safe_id)
                if current is not session or current["status"] != "pending":
                    return
                current["status"], current["code"] = "timeout", "oauth_timeout"
            try:
                server.shutdown()
                server.server_close()
            except OSError:
                pass

        timer = threading.Timer(_OAUTH_TIMEOUT_SECONDS, expire_session)
        timer.daemon = True
        timer.start()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.force-ssl",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def oauth_status(profile_id: str) -> dict[str, Any]:
    """Return a token-free snapshot of the profile's in-memory OAuth operation."""
    safe_id = _validated_id(profile_id)
    with _oauth_sessions_lock:
        session = _oauth_sessions.get(safe_id)
        if session is None:
            connected = False
            try:
                connected = _public_profile(
                    next(item for item in _read_profiles() if item.get("id") == safe_id)
                )["youtube"]["connected"]
            except (StopIteration, ValueError):
                pass
            return {
                "id": safe_id,
                "status": "connected" if connected else "idle",
                "connected": connected,
                "code": "ok" if connected else "not_started",
            }
        return {
            "id": safe_id,
            "status": session["status"],
            "connected": session["status"] == "connected",
            "code": session["code"],
        }
