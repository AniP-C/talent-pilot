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


# Characters a PDF hands back that no reader ever typed.
#
# Typesetters — LaTeX especially, which is what most engineering CVs are built
# with — substitute a single ligature glyph for "fi", "fl" and "ffi". pypdf
# returns the glyph, so the extracted text of a real resume contains
# "workﬂows", "identiﬁcation", "eﬀort" and "conﬁdence". Left alone, a literal
# search for "workflow" misses a document that visibly says workflow, which is
# precisely the false negative the keyword pass exists to avoid.
#
# The soft hyphen and the non-breaking space are the same class of problem:
# invisible on the page, fatal to an exact match.
_TEXT_SUBSTITUTIONS = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
    " ": " ",   # non-breaking space
    "­": "",    # soft hyphen
    "‐": "-",   # hyphen
    "‑": "-",   # non-breaking hyphen
    "–": "-",   # en dash
    "—": "-",   # em dash
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
}


def normalise_pdf_text(text: str) -> str:
    """Replace typesetting glyphs with the characters a search would use."""
    for glyph, replacement in _TEXT_SUBSTITUTIONS.items():
        text = text.replace(glyph, replacement)
    return text


def extract_pdf_text(source: BinaryIO) -> str:
    """Pull raw text out of a PDF.

    Shared by the dashboard uploader and the extension endpoint so both
    reject the same files for the same reasons.

    Normalised on the way out rather than at each use, so the parser, the
    keyword pass and the stored copy all see the same characters.
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

    return normalise_pdf_text(text)

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


def save_profile(
    user_id: int, filename: str, profile: dict, raw_text: str = ""
) -> str:
    """Write a parsed profile into the user's workspace. Returns the filename.

    ``raw_text`` is the resume as extracted from the PDF, kept beside the
    parsed profile. The keyword pass reads it instead of the parsed profile:
    that pass approximates a literal filter, and a literal filter reads the
    document the employer receives, not a model's summary of it.

    Best-effort. A profile that saved but whose text did not is the behaviour
    every profile had before this existed, and is no reason to fail an upload
    the user has already paid a model call for.
    """
    stem = workspace.sanitize_filename(filename)
    if not stem.endswith(".json"):
        stem += ".json"

    path = workspace.profile_path(user_id, stem)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2, ensure_ascii=False)

    if raw_text:
        try:
            workspace.profile_text_path(user_id, stem).write_text(
                raw_text, encoding="utf-8"
            )
        except (OSError, workspace.UnsafePathError) as exc:
            logger.error("Could not store resume text for %s: %s", stem, exc)

    logger.info("Saved profile %s for user %s", stem, user_id)
    return stem


def load_profile_text(user_id: int, filename: Optional[str] = None) -> str:
    """The resume's own text, or "" when none was stored.

    Empty is a normal answer, not an error: every profile uploaded before the
    text was kept has none, and the caller falls back to the parsed profile.
    """
    if not filename:
        available = workspace.list_profiles(user_id)
        if not available:
            return ""
        filename = available[0]

    try:
        path = workspace.profile_text_path(user_id, filename)
    except workspace.UnsafePathError:
        logger.warning("Rejected unsafe profile path %r for user %s", filename, user_id)
        return ""

    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def delete_profile(user_id: int, filename: str) -> bool:
    """Remove a profile, and the resume text stored beside it."""
    try:
        path = workspace.profile_path(user_id, filename)
        text_path = workspace.profile_text_path(user_id, filename)
    except workspace.UnsafePathError:
        return False

    if not path.exists():
        return False

    path.unlink()
    # Deleting a resume has to delete the resume. Leaving the text behind would
    # keep the document itself in the workspace after the user asked for it to
    # be gone, and a later profile saved under the same name would silently
    # inherit somebody else's words.
    text_path.unlink(missing_ok=True)

    logger.info("Deleted profile %s for user %s", path.name, user_id)
    return True


# How many captures to keep per user. Enough to look back over a session's
# worth of analyses, few enough that a workspace does not fill with job adverts.
MAX_CAPTURES = 25


def save_jd_capture(
    user_id: int,
    jd_text: str,
    *,
    company: str = "",
    role: str = "",
    source: str = "dashboard",
    trimmed_text: str = "",
    unknown_terms: tuple = (),
) -> str:
    """Record the job description an analysis actually ran on. Returns the name.

    Both versions are kept: what arrived from the page, and what survived
    trimming. The difference is the whole diagnosis when a posting scores
    strangely — a page that captured half a job description and a page that
    captured the entire careers site look identical in a percentage and nothing
    alike here.

    Best-effort, like every other piece of bookkeeping: an analysis the user
    paid for must not fail because a diagnostic file could not be written.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = workspace.sanitize_filename(f"{company}-{role}".strip("-") or "posting")
    name = f"{stamp}-{label[:40]}.txt"

    trimmed_text = trimmed_text or jd_text
    header = [
        f"captured  : {datetime.now().isoformat(timespec='seconds')}",
        f"company   : {company or 'not stated'}",
        f"role      : {role or 'not stated'}",
        f"source    : {source}",
        f"characters: {len(jd_text)} captured, {len(trimmed_text)} after trimming"
        + ("" if len(trimmed_text) == len(jd_text) else "  (careers-page furniture removed)"),
        "",
    ]

    # The vocabulary cannot see a term it does not hold, and nothing used to
    # report the misses — LangGraph sat missing until somebody read a bad
    # result and went looking. These are candidates for review, not a verdict:
    # a page's proper nouns land here too.
    if unknown_terms:
        header += [
            "terms the keyword vocabulary does not hold (review, do not assume):",
            "  " + ", ".join(unknown_terms),
            "",
        ]

    header += [
        "=" * 70,
        "WHAT THE PAGE GAVE US",
        "=" * 70,
        jd_text,
    ]

    if len(trimmed_text) != len(jd_text):
        header += [
            "",
            "=" * 70,
            "WHAT WAS SCORED AND SENT TO THE MODEL",
            "=" * 70,
            trimmed_text,
        ]

    try:
        directory = workspace.captures_dir(user_id)
        (directory / name).write_text("\n".join(header), encoding="utf-8")

        # Oldest first, so the newest MAX_CAPTURES survive.
        existing = sorted(directory.glob("*.txt"), key=lambda p: p.name)
        for stale in existing[:-MAX_CAPTURES]:
            stale.unlink(missing_ok=True)
    except OSError as exc:
        logger.error("Could not store the analysed description: %s", exc)
        return ""

    return name


def list_jd_captures(user_id: int, limit: int = 10) -> list[dict]:
    """Recent captures, newest first: name, header lines, and the full text."""
    try:
        files = sorted(
            workspace.captures_dir(user_id).glob("*.txt"),
            key=lambda p: p.name,
            reverse=True,
        )[:limit]
    except OSError:
        return []

    captures = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        captures.append({"name": path.name, "text": text})

    return captures


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
