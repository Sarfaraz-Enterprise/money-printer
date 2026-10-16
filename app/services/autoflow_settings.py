"""Allowlisted private environment updates and read-only connectivity checks."""

import os
import shutil
import tempfile
import time
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from dotenv import set_key
from filelock import FileLock

from app.config import config, env_secrets

VAULT_FIELDS = {
    "OPENAI_API_KEY": "OpenAI API Key",
    "ANTHROPIC_API_KEY": "Claude API Key",
    "GEMINI_API_KEY": "Gemini API Key",
    "ELEVENLABS_API_KEY": "ElevenLabs API Key",
    "ELEVENLABS_VOICE_ID": "ElevenLabs Default Voice ID",
    "PEXELS_API_KEY": "Pexels API Key",
    "PIXABAY_API_KEY": "Pixabay API Key",
    "GOOGLE_CLIENT_ID": "Google Client ID",
    "GOOGLE_CLIENT_SECRET": "Google Client Secret",
}


def save_vault(provider: str, values: dict[str, str]) -> None:
    if provider not in ("openai", "anthropic", "gemini") or set(values) - VAULT_FIELDS.keys():
        raise ValueError("Unsupported vault field or provider")
    updates = {key: value.strip() for key, value in values.items() if value.strip()}
    if any("\n" in value or "\r" in value or "\x00" in value for value in updates.values()):
        raise ValueError("Vault values must be single-line strings")
    target = env_secrets.DOTENV_PATH
    with config.runtime_config_lock(), FileLock(str(target) + ".lock", timeout=10):
        descriptor, name = tempfile.mkstemp(prefix=".vault-", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            if target.exists():
                shutil.copyfile(target, temporary)
            for key, value in updates.items():
                set_key(str(temporary), key, value)
            temporary.chmod(0o600)
            os.replace(temporary, target)
            config.app["llm_provider"] = provider
            config.save_config()
        finally:
            temporary.unlink(missing_ok=True)


SERVICES = {
    "OpenAI": ("OPENAI_API_KEY",),
    "Claude": ("ANTHROPIC_API_KEY",),
    "Gemini": ("GEMINI_API_KEY",),
    "ElevenLabs": ("ELEVENLABS_API_KEY",),
    "ElevenLabs Voice": ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"),
    "Pexels": ("PEXELS_API_KEY",),
    "Pixabay": ("PIXABAY_API_KEY",),
    "Google OAuth": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
}


def health_board() -> list[dict]:
    credentials = env_secrets.global_values()
    return [
        {"Service": label,
         "Status": "🟢 Connected via .env" if all(credentials.get(key) for key in keys) else "🔴 Missing in .env",
         "Latency (ms)": None,
         "Validation": "Configured; not yet tested" if all(credentials.get(key) for key in keys) else "Set missing values in the root .env"}
        for label, keys in SERVICES.items()
    ]


def credential_revision() -> tuple[int, int] | None:
    path = env_secrets.DOTENV_PATH
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _probe(label: str, credentials: dict[str, str]) -> dict:
    row = {"Service": label, "Status": "🔴 Missing in .env", "Latency (ms)": None,
           "Validation": "Set missing values in the root .env"}
    if not all(credentials.get(key) for key in SERVICES[label]):
        return row
    started = time.perf_counter()
    response = None
    try:
        if label == "Google OAuth":
            from app.services import accounts
            profiles = [profile for profile in accounts.list_profiles() if profile["youtube"].get("connected")]
            if not profiles:
                row.update(Status="🟡 OAuth consent required",
                           Validation="Client credentials present; connect a YouTube profile to validate OAuth")
                return row
            token, _ = accounts._refresh_youtube_token(profiles[0]["id"])
            if not token:
                row.update(Status="🔴 Error", Validation="Saved YouTube token could not be refreshed")
                return row
            url, headers, params = ("https://www.googleapis.com/youtube/v3/channels",
                                    {"Authorization": "Bearer " + token}, {"part": "id", "mine": "true"})
        else:
            checks = {
                "OpenAI": ("https://api.openai.com/v1/models", {"Authorization": "Bearer " + credentials.get("OPENAI_API_KEY", "")}),
                "Claude": ("https://api.anthropic.com/v1/models", {"x-api-key": credentials.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"}),
                "Gemini": ("https://generativelanguage.googleapis.com/v1beta/models", {"x-goog-api-key": credentials.get("GEMINI_API_KEY", "")}),
                "ElevenLabs": ("https://api.elevenlabs.io/v1/voices", {"xi-api-key": credentials.get("ELEVENLABS_API_KEY", "")}),
                "Pexels": ("https://api.pexels.com/videos/search", {"Authorization": credentials.get("PEXELS_API_KEY", "").split(",")[0].strip()}),
                "Pixabay": ("https://pixabay.com/api/videos/", {}),
            }
            if label == "ElevenLabs Voice":
                identifier = credentials["ELEVENLABS_VOICE_ID"]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
                    row.update(Status="🔴 Error", Validation="Invalid default voice ID")
                    return row
                url, headers = f"https://api.elevenlabs.io/v1/voices/{identifier}", {"xi-api-key": credentials["ELEVENLABS_API_KEY"]}
            else:
                url, headers = checks[label]
            params = ({"query": "night", "per_page": 1} if label == "Pexels"
                      else {"key": credentials["PIXABAY_API_KEY"].split(",")[0].strip(), "q": "night", "per_page": 3}
                      if label == "Pixabay" else {})
        response = requests.get(url, headers=headers, params=params, timeout=(5, 15), allow_redirects=False, stream=True)
        row.update(Status="🟢 Connected via .env" if response.status_code == 200 else "🔴 Error",
                   Validation=f"HTTP {response.status_code}; read access only, no generation or uploads tested")
    except requests.RequestException:
        row.update(Status="🔴 Error", Validation="Network request failed")
    except (OSError, ValueError):
        row.update(Status="🔴 Error", Validation="Local configuration could not be validated")
    finally:
        if response is not None:
            response.close()
        row["Latency (ms)"] = round((time.perf_counter() - started) * 1000, 1)
    return row


def diagnostics() -> list[dict]:
    credentials = env_secrets.global_values()
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="autoflow-health") as pool:
        return list(pool.map(lambda service: _probe(service, credentials), SERVICES))
