"""
Backend-only secrets: global generation/OAuth credentials come exclusively from
the application-root .env. Legacy integration settings retain env/config
precedence. Values are never copied into config.app or settings exports.
"""

import os
from pathlib import Path

from dotenv import dotenv_values

DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"
GLOBAL_FIELDS = frozenset({
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "PEXELS_API_KEY",
    "PIXABAY_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
})

APINEX_BASE_URL = "https://api.apinex.bond/v1"
APINEX_MODEL = "free/deepseek-v4-pro-0813"


def gateway_preview_settings() -> dict[str, str]:
    """Non-secret, file-only settings for an inactive gateway configuration."""
    values = (
        dotenv_values(DOTENV_PATH, encoding="utf-8-sig", interpolate=False)
        if DOTENV_PATH.exists() else {}
    )
    return {
        "base_url": (values.get("OPENAI_BASE_URL") or "").strip() or APINEX_BASE_URL,
        "model": (values.get("OPENAI_MODEL_NAME") or "").strip() or APINEX_MODEL,
    }


def global_values() -> dict[str, str]:
    """Read the private application-root file, never inherited process values."""
    if not DOTENV_PATH.exists():
        return {}
    values = dotenv_values(DOTENV_PATH, encoding="utf-8-sig", interpolate=False)
    return {key: (values.get(key) or "").strip() for key in GLOBAL_FIELDS}


def global_value(name: str) -> str:
    if name not in GLOBAL_FIELDS:
        raise ValueError("Unsupported global credential")
    return global_values().get(name, "")

# (config section, config key) -> environment variable
ENV_OVERRIDES = {
    ("app", "openai_api_key"): "OPENAI_API_KEY",
    ("app", "gemini_api_key"): "GEMINI_API_KEY",
    ("elevenlabs", "api_key"): "ELEVENLABS_API_KEY",
    ("elevenlabs", "voice_id"): "ELEVENLABS_VOICE_ID",
    ("app", "anthropic_api_key"): "ANTHROPIC_API_KEY",
    ("app", "anthropic_base_url"): "ANTHROPIC_BASE_URL",
    ("app", "anthropic_workspace_id"): "ANTHROPIC_WORKSPACE_ID",
    ("app", "anthropic_model_name"): "ANTHROPIC_MODEL",
    ("app", "pexels_api_keys"): "PEXELS_API_KEY",
    ("app", "pixabay_api_keys"): "PIXABAY_API_KEY",
    ("app", "coverr_api_keys"): "COVERR_API_KEY",
    ("app", "upload_post_api_key"): "UPLOAD_POST_API_KEY",
    # Social comment agent: environment-only, never stored in config.toml.
    ("social", "youtube_client_id"): "YOUTUBE_CLIENT_ID",
    ("social", "youtube_client_secret"): "YOUTUBE_CLIENT_SECRET",
    ("social", "youtube_refresh_token"): "YOUTUBE_REFRESH_TOKEN",
    ("social", "meta_page_access_token"): "META_PAGE_ACCESS_TOKEN",
    ("social", "meta_page_id"): "META_PAGE_ID",
    ("social", "instagram_business_account_id"): "INSTAGRAM_BUSINESS_ACCOUNT_ID",
    ("social", "meta_graph_version"): "META_GRAPH_VERSION",
}

# Stock media keys accept a comma-separated list for round-robin rotation.
_LIST_KEYS = {"pexels_api_keys", "pixabay_api_keys", "coverr_api_keys"}


def env_override(section: str, key: str):
    """Return the env value for a mapped config key, or None if unset/blank."""
    env_name = ENV_OVERRIDES.get((section, key))
    if not env_name:
        return None
    raw = global_value(env_name) if env_name in GLOBAL_FIELDS else os.getenv(env_name, "").strip()
    if not raw:
        return None
    if key in _LIST_KEYS:
        values = [item.strip() for item in raw.split(",") if item.strip()]
        return values or None
    return raw


def is_env_managed(section: str, key: str) -> bool:
    return env_override(section, key) is not None


def resolve(section: str, key: str, configured=None):
    """Global credentials are file-only; legacy integrations retain fallback."""
    value = env_override(section, key)
    if ENV_OVERRIDES.get((section, key)) in GLOBAL_FIELDS:
        return value
    return configured if value is None else value
