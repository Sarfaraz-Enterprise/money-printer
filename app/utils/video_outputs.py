"""Shared output naming and roots for renders and authenticated downloads."""

import os
import re
import unicodedata
from app.utils.profile_ids import validate_profile_id

from app.config import config
from app.utils import utils

OUTPUT_CATEGORIES = ("scheduled", "fully_automatic", "manual")


def _canonical_path(path: str) -> str:
    resolved = os.path.realpath(path)
    # Windows realpath can retain this prefix during concurrent directory creation.
    if os.name == "nt":
        if resolved.startswith("\\\\?\\UNC\\"):
            return "\\\\" + resolved[8:]
        if resolved.startswith("\\\\?\\"):
            return resolved[4:]
    return resolved


def valid_output_folder(folder: str) -> bool:
    if folder in OUTPUT_CATEGORIES:
        return True
    try:
        return validate_profile_id(folder) == folder
    except (ValueError, TypeError, AttributeError):
        return False


def profile_dir(profile_id: str) -> str:
    folder = validate_profile_id(profile_id)
    root = output_root()
    directory = _canonical_path(os.path.join(root, folder))
    if os.path.commonpath([root, directory]) != root:
        raise ValueError("Invalid profile output directory")
    os.makedirs(directory, exist_ok=True)
    return directory


def subject_slug(subject: str) -> str:
    text = unicodedata.normalize("NFKD", subject or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")[:100].rstrip("_") or "auto_video"


def output_root() -> str:
    configured = str(config.app.get("final_video_export_dir") or "").strip()
    root = os.path.expanduser(configured) if configured else utils.storage_dir("outputs")
    if not os.path.isabs(root):
        root = os.path.join(utils.root_dir(), root)
    return _canonical_path(root)


def clean_subject(subject: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", subject or "")
    text = text.strip().rstrip(".").rstrip()
    if not text:
        return "Auto_Video"
    # Windows reserves these names even when an extension is appended.
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", text.split(".", 1)[0].rstrip(), re.IGNORECASE):
        text = "_" + text
    return text


def category_dir(category: str) -> str:
    if category not in OUTPUT_CATEGORIES:
        raise ValueError("Invalid video output category")
    directory = os.path.join(output_root(), category)
    os.makedirs(directory, exist_ok=True)
    return directory
