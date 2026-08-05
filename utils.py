"""Workspace-scoped file helpers shared by the UI and the API.

Resume profiles and sync timestamps are always read through the caller's own
workspace — there is deliberately no fallback to "whatever JSON file happens
to be lying around", which previously could hand one user another user's
resume.
"""

import json
from datetime import datetime
from typing import BinaryIO, Optional

import pypdf

import workspace
from config import logger

# Resumes are a couple of pages; anything larger is a mistake or an attack.
MAX_RESUME_BYTES = 5 * 1024 * 1024


class ResumeReadError(Exception):
    """Raised when an uploaded file cannot be read as a text-bearing PDF."""


def extract_pdf_text(source: BinaryIO) -> str:
    """Pull raw text out of a PDF.

    Shared by the dashboard uploader and the extension endpoint so both
    reject the same files for the same reasons.
    """
    try:
        reader = pypdf.PdfReader(source)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - pypdf raises a variety of types
        raise ResumeReadError(
            "That file could not be read as a PDF. It may be corrupt or password protected."
        ) from exc

    if not text.strip():
        raise ResumeReadError(
            "No text could be extracted. The PDF is likely a scanned image, "
            "which needs OCR before it can be parsed."
        )

    return text

# Returned when a user has not uploaded a profile yet, so the AI calls degrade
# to a harmless empty resume instead of crashing.
EMPTY_PROFILE = {
    "name": "",
    "email": "",
    "summary": "",
    "skills": [],
    "experience": [],
    "education": [],
}


def load_profile(user_id: int, filename: Optional[str] = None) -> dict:
    """Load a parsed resume profile from the user's own workspace.

    Falls back to the user's most recently modified profile when ``filename``
    is omitted, and to an empty profile when they have none.
    """
    if not filename:
        available = workspace.list_profiles(user_id)
        if not available:
            return dict(EMPTY_PROFILE)
        filename = available[0]

    try:
        path = workspace.profile_path(user_id, filename)
    except workspace.UnsafePathError:
        logger.warning("Rejected unsafe profile path %r for user %s", filename, user_id)
        return dict(EMPTY_PROFILE)

    if not path.exists():
        logger.warning("Profile %r not found for user %s", filename, user_id)
        return dict(EMPTY_PROFILE)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read profile %s: %s", path.name, exc)
        return dict(EMPTY_PROFILE)


def save_profile(user_id: int, filename: str, profile: dict) -> str:
    """Write a parsed profile into the user's workspace. Returns the filename."""
    stem = workspace.sanitize_filename(filename)
    if not stem.endswith(".json"):
        stem += ".json"

    path = workspace.profile_path(user_id, stem)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2, ensure_ascii=False)

    logger.info("Saved profile %s for user %s", stem, user_id)
    return stem


def delete_profile(user_id: int, filename: str) -> bool:
    """Remove a profile from the user's workspace."""
    try:
        path = workspace.profile_path(user_id, filename)
    except workspace.UnsafePathError:
        return False

    if not path.exists():
        return False

    path.unlink()
    logger.info("Deleted profile %s for user %s", path.name, user_id)
    return True


def update_last_sync(user_id: int) -> None:
    """Record that an inbox sync just finished."""
    path = workspace.last_sync_path(user_id)
    path.write_text(
        datetime.now().strftime("%b %d, %Y at %I:%M %p"), encoding="utf-8"
    )


def get_last_sync(user_id: int) -> str:
    """Human-readable timestamp of the last inbox sync, or 'Never'."""
    path = workspace.last_sync_path(user_id)
    try:
        return path.read_text(encoding="utf-8").strip() or "Never"
    except (FileNotFoundError, OSError):
        return "Never"


def profile_display_name(filename: str) -> str:
    """Turn 'senior_ai_engineer.json' into 'Senior Ai Engineer' for the UI."""
    return filename.removesuffix(".json").replace("_", " ").replace("-", " ").title()
