"""Per-user workspace layout and path safety.

Every user gets an isolated directory keyed by their numeric account id:

    data/workspaces/<user_id>/
        jobs.db            job applications
        profiles/*.json    parsed resume profiles
        answers/*.txt      saved application answers
        gmail_token.json   that user's Gmail OAuth token
        last_sync.txt      timestamp of the last inbox sync

Keying on the account id rather than the email address means a caller can
never influence where files land, and one user's data is structurally
unreachable from another user's session.
"""

import re
from pathlib import Path

from config import WORKSPACES_DIR

# Profile names are user-supplied, so they are restricted to a conservative
# character set before ever touching the filesystem.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class UnsafePathError(ValueError):
    """Raised when a caller-supplied filename escapes its workspace."""


def workspace_dir(user_id: int) -> Path:
    """Return (and create) the root directory for a user's workspace."""
    path = WORKSPACES_DIR / str(int(user_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def jobs_db_path(user_id: int) -> Path:
    return workspace_dir(user_id) / "jobs.db"


def profiles_dir(user_id: int) -> Path:
    path = workspace_dir(user_id) / "profiles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def answers_dir(user_id: int) -> Path:
    path = workspace_dir(user_id) / "answers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def gmail_token_path(user_id: int) -> Path:
    return workspace_dir(user_id) / "gmail_token.json"


def last_sync_path(user_id: int) -> Path:
    return workspace_dir(user_id) / "last_sync.txt"


def sanitize_filename(name: str, default: str = "profile") -> str:
    """Reduce arbitrary user input to a safe, flat filename stem.

    Strips directory separators, traversal sequences, and anything outside
    ``[A-Za-z0-9._-]``.
    """
    stem = Path(str(name or "")).name  # drops any directory component
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._-")
    return stem or default


def resolve_within(directory: Path, filename: str) -> Path:
    """Resolve ``filename`` inside ``directory``, refusing to escape it.

    This is the last line of defence for values that arrive over HTTP: even a
    caller sending ``../../etc/passwd`` lands inside the workspace or raises.
    """
    directory = directory.resolve()
    candidate = (directory / Path(str(filename)).name).resolve()

    if candidate != directory and directory not in candidate.parents:
        raise UnsafePathError(f"Path {filename!r} escapes {directory}")

    return candidate


def list_profiles(user_id: int) -> list[str]:
    """Return the user's saved profile filenames, newest first."""
    directory = profiles_dir(user_id)
    profiles = [p for p in directory.glob("*.json") if p.is_file()]
    profiles.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in profiles]


def profile_path(user_id: int, filename: str) -> Path:
    """Resolve a profile filename to a path inside the user's workspace."""
    return resolve_within(profiles_dir(user_id), filename)


def answer_path(user_id: int, filename: str) -> Path:
    """Resolve an answer-memory filename inside the user's workspace."""
    return resolve_within(answers_dir(user_id), filename)
