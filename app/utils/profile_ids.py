"""Filesystem-safe profile identifiers shared by storage, renders and routes."""

import re


def validate_profile_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise ValueError("invalid_profile_id")
    if value in {"scheduled", "fully_automatic", "manual", "con", "prn", "aux", "nul"} or re.fullmatch(r"(com|lpt)[1-9]", value):
        raise ValueError("invalid_profile_id")
    return value
