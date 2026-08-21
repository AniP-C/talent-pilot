"""Server operations: services, settings, backups, logs, machine health.

The half of deploy/OPERATIONS.md that is not about accounts. Restarting a unit,
rotating the invite code, taking a backup and reading a log are the four things
most likely to be needed at the exact moment SSH is least convenient — from a
phone, or from a machine that has never had gcloud installed.

Two rules shape everything here, and both exist because this module runs
commands as root on behalf of a web request:

**Nothing is interpolated into a shell.** Every call is a list of arguments to
``subprocess.run`` with no ``shell=True`` anywhere, and the one variable part —
a service name, a setting key — is checked against a fixed allowlist first. A
free-text field that reaches ``sudo`` is a remote shell with extra steps.

**Privilege is granted narrowly and explicitly.** The dashboard runs as the
unprivileged ``talentpilot`` user and can do none of this until
deploy/talent-pilot-admin.sudoers is installed, which permits exactly three
commands and nothing else. Where that file is absent — a laptop, a fresh
checkout — every operation here reports itself unavailable and says what to
install, rather than failing with a permission error nobody can act on.

Note what is *not* editable: ADMIN_EMAILS. The panel can change the invite
code, the model, the log level and the sync limits, but it cannot appoint
another administrator, because the ability to do that through the app would
undo the reason the setting lives in a root-owned file at all.
"""

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import admin
from config import (
    BACKUP_COMMAND,
    BACKUP_DIR,
    DATA_DIR,
    ENV_FILE_PATH,
    ENVSET_COMMAND,
    ERROR_LOG_FILE,
    LOG_FILE,
    MANAGED_SERVICES,
    SYNC_LOG_FILE,
    logger,
)

# How long any one command is allowed to take. A restart is quick; a backup of
# a small SQLite deployment is seconds. Anything past this is stuck, and a
# stuck subprocess in a Streamlit callback is a page that never renders.
_TIMEOUT_SECONDS = 60

# Log files the panel may read, by the name shown in its selector. An
# allowlist, not a directory listing: "which file" arrives from the browser.
LOG_FILES = {
    "Inbox sync (sync.log)": SYNC_LOG_FILE,
    "Everything (app.log)": LOG_FILE,
    "Warnings and errors (errors.log)": ERROR_LOG_FILE,
}


class OpsError(RuntimeError):
    """Raised when a server operation cannot be performed or fails."""


# =====================================================================
# SETTINGS THE PANEL MAY CHANGE
# =====================================================================
# Each entry is one line of the environment file. ``kind`` drives both the
# widget and the validation; ``secret`` means the current value is never
# rendered back, only replaced.
#
# The list is short on purpose. Everything omitted — PUBLIC_URL,
# TRUST_PROXY_HEADERS, DATA_DIR, ADMIN_EMAILS — is either something that breaks
# the deployment when it is wrong in a way the panel cannot detect, or the very
# setting that decides who may use the panel.
SETTINGS = (
    {
        "key": "SIGNUP_CODE",
        "label": "Invite code",
        "kind": "text",
        "help": "Required to register. Rotate it after sharing it with anyone.",
    },
    {
        "key": "REGISTRATION_CLOSED",
        "label": "Registration closed",
        "kind": "bool",
        "help": "Refuses every new account, invite code or not.",
    },
    {
        "key": "GEMINI_API_KEY",
        "label": "Gemini API key",
        "kind": "secret",
        "help": "All AI features. Rotate at aistudio.google.com/apikey.",
    },
    {
        "key": "GEMINI_MODEL",
        "label": "Gemini model",
        "kind": "text",
        "help": "The model every analysis and draft is billed against.",
    },
    {
        "key": "GMAIL_LOOKBACK_DAYS",
        "label": "Sync lookback (days)",
        "kind": "int",
        "help": "How far back one inbox sync reads.",
    },
    {
        "key": "GMAIL_MAX_RESULTS",
        "label": "Sync ceiling (emails)",
        "kind": "int",
        "help": "Hard limit on emails classified per sync. Each one is a paid call.",
    },
    {
        "key": "GMAIL_THROTTLE_SECONDS",
        "label": "Sync throttle (seconds)",
        "kind": "number",
        "help": "Gap between AI calls. Keeps the free tier's rate limit happy.",
    },
    {
        "key": "MAX_LOGIN_ATTEMPTS",
        "label": "Failed sign-ins allowed",
        "kind": "int",
        "help": "Before an address or an IP is locked out temporarily.",
    },
    {
        "key": "LOGIN_LOCKOUT_MINUTES",
        "label": "Lockout length (minutes)",
        "kind": "int",
        "help": "How long that lockout lasts.",
    },
    {
        "key": "LOG_LEVEL",
        "label": "Log level",
        "kind": "choice",
        "options": ("DEBUG", "INFO", "WARNING", "ERROR"),
        "help": "DEBUG records per-request detail. Noisy; leave on INFO.",
    },
)

SETTING_KEYS = {entry["key"]: entry for entry in SETTINGS}

# What a value is allowed to contain. Anything with a newline could append a
# second line to the environment file and set a key that is not on the list.
_VALUE_RE = re.compile(r"^[^\r\n]{0,512}$")


# =====================================================================
# WHAT THIS INSTANCE CAN DO
# =====================================================================
def capabilities() -> dict:
    """Which server operations are actually available here, and why not.

    Called before anything is rendered, so the panel can show a plain
    explanation instead of a row of buttons that each fail differently.
    """
    sudo_ok, sudo_detail = _sudo_available()

    return {
        "env_file": ENV_FILE_PATH.is_file(),
        "env_path": str(ENV_FILE_PATH),
        "sudo": sudo_ok,
        "sudo_detail": sudo_detail,
        "envset": Path(ENVSET_COMMAND).is_file(),
        "backup": Path(BACKUP_COMMAND).is_file(),
        "systemd": shutil.which("systemctl") is not None,
        "backup_dir": BACKUP_DIR.is_dir(),
    }


def _sudo_available() -> tuple[bool, str]:
    """Whether this process can use sudo without being asked for a password.

    ``-n`` is what makes this safe to call from a web request: without it, a
    sudo that wants a password waits on a terminal that does not exist and the
    page hangs until the timeout.
    """
    if shutil.which("sudo") is None:
        return False, "sudo is not installed (this is not the server)."

    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"sudo could not be run: {exc}"

    if result.returncode == 0:
        return True, "Available."

    return False, "sudo needs a password — the admin sudoers rule is not installed."


def _run(args: list[str]) -> str:
    """Run one allowlisted command, returning its output or raising OpsError."""
    logger.info("Server operation: %s", " ".join(args))

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpsError(f"Timed out after {_TIMEOUT_SECONDS}s: {' '.join(args)}") from exc
    except OSError as exc:
        raise OpsError(f"Could not run {args[0]}: {exc}") from exc

    output = (result.stdout or "").strip() or (result.stderr or "").strip()

    if result.returncode != 0:
        raise OpsError(output or f"Command failed with status {result.returncode}.")

    return output


# =====================================================================
# SERVICES
# =====================================================================
def service_status() -> list[dict]:
    """Whether each managed unit is running.

    ``systemctl is-active`` exits non-zero for anything that is not active, so
    the status is read from its output rather than from its exit code — an
    inactive service is an answer, not an error.
    """
    if shutil.which("systemctl") is None:
        return []

    statuses = []

    for name in MANAGED_SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            state = (result.stdout or result.stderr or "unknown").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            state = f"unreadable ({exc})"

        statuses.append({"name": name, "state": state, "running": state == "active"})

    return statuses


def restart_service(actor: str, name: str) -> str:
    """Restart one managed unit.

    Restarting ``talent-pilot-dashboard`` restarts the process serving this
    very page — the browser loses its connection and reconnects a few seconds
    later. That is expected, and the panel says so before the click.
    """
    if name not in MANAGED_SERVICES:
        raise OpsError(f"{name!r} is not a managed service.")

    output = _run(["sudo", "-n", "systemctl", "restart", name])
    admin.record(actor, admin.ACTION_SERVICE_RESTARTED, target=name)

    return output or f"{name} restarted."


# =====================================================================
# ENVIRONMENT SETTINGS
# =====================================================================
def read_settings() -> dict[str, str]:
    """Current values of the allowlisted keys, as the file has them.

    Values are returned raw, including secrets — masking is a display decision
    and belongs to whatever renders them, not to the reader. Keys absent from
    the file come back as empty strings, which is what "unset, so the default
    applies" looks like.
    """
    values = {key: "" for key in SETTING_KEYS}

    if not ENV_FILE_PATH.is_file():
        return values

    try:
        text = ENV_FILE_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OpsError(f"Could not read {ENV_FILE_PATH}: {exc}") from exc

    for line in text.splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()

        if key in values:
            values[key] = value.strip().strip('"').strip("'")

    return values


def validate_setting(key: str, value: str) -> str:
    """Check one setting against its declared kind, returning what to write."""
    entry = SETTING_KEYS.get(key)

    if entry is None:
        raise OpsError(f"{key!r} is not a setting the panel may change.")

    value = str(value).strip()

    if not _VALUE_RE.match(value):
        raise OpsError("A value cannot contain a line break.")

    kind = entry["kind"]

    if kind == "bool":
        if value.lower() not in ("true", "false"):
            raise OpsError("Expected true or false.")
        return value.lower()

    if kind == "int":
        if not value.isdigit():
            raise OpsError("Expected a whole number.")
        return value

    if kind == "number":
        try:
            float(value)
        except ValueError as exc:
            raise OpsError("Expected a number.") from exc
        return value

    if kind == "choice":
        if value not in entry["options"]:
            raise OpsError(f"Expected one of: {', '.join(entry['options'])}.")
        return value

    return value


def write_setting(actor: str, key: str, value: str) -> str:
    """Change one setting in the environment file.

    Written through the ``talent-pilot-envset`` helper rather than by editing
    the file here, because the file is root-owned and must stay that way: it
    holds the Gemini key and the invite code, and making it writable by the
    service account would mean any flaw in the app is a path to both.

    The helper re-checks the key against its own allowlist. That duplication is
    deliberate — the shell script is the boundary that actually holds when the
    Python side is wrong.
    """
    value = validate_setting(key, value)

    if not Path(ENVSET_COMMAND).is_file():
        raise OpsError(
            f"{ENVSET_COMMAND} is not installed. See deploy/OPERATIONS.md — "
            "'Enabling server actions in the panel'."
        )

    _run(["sudo", "-n", ENVSET_COMMAND, key, value])

    # The new value itself is recorded only when it is not a secret: an audit
    # trail that quotes the API key is a copy of the API key.
    detail = "updated" if SETTING_KEYS[key]["kind"] == "secret" else f"= {value}"
    admin.record(actor, admin.ACTION_SETTING_CHANGED, target=key, detail=detail)

    return f"{key} saved. Restart both services for it to take effect."


# =====================================================================
# BACKUPS
# =====================================================================
def backups(limit: int = 10) -> list[dict]:
    """The archives on disk, newest first."""
    if not BACKUP_DIR.is_dir():
        return []

    try:
        files = [item for item in BACKUP_DIR.glob("*.tar.gz") if item.is_file()]
    except OSError as exc:
        raise OpsError(f"Could not list {BACKUP_DIR}: {exc}") from exc

    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    return [
        {
            "name": item.name,
            "bytes": item.stat().st_size,
            "taken_at": datetime.fromtimestamp(
                item.stat().st_mtime, timezone.utc
            ).isoformat(timespec="seconds"),
        }
        for item in files[:limit]
    ]


def run_backup(actor: str) -> str:
    """Take a backup now."""
    if not Path(BACKUP_COMMAND).is_file():
        raise OpsError(f"{BACKUP_COMMAND} is not installed on this machine.")

    output = _run(["sudo", "-n", BACKUP_COMMAND])
    admin.record(actor, admin.ACTION_BACKUP_TAKEN, detail=output[-200:] if output else "")

    return output or "Backup complete."


# =====================================================================
# LOGS
# =====================================================================
def tail(name: str, lines: int = 200, contains: str = "") -> list[str]:
    """Last ``lines`` of one allowlisted log file, optionally filtered.

    Read in Python rather than shelled out to ``tail``, so it works the same on
    a laptop as on the server and needs no privilege at all — the log directory
    is readable by the account the dashboard already runs as.
    """
    path = LOG_FILES.get(name)

    if path is None:
        raise OpsError(f"{name!r} is not a readable log.")

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            rows = [line.rstrip("\n") for line in handle.readlines()[-max(1, lines):]]
    except (FileNotFoundError, OSError):
        return []

    if contains:
        needle = contains.lower()
        rows = [row for row in rows if needle in row.lower()]

    return rows


# =====================================================================
# MACHINE HEALTH
# =====================================================================
def machine() -> dict:
    """Disk, memory, load and uptime, as far as this platform reports them.

    An e2-micro has 1 GB of RAM running three services, so memory is the
    tightest resource and the usual cause of a restart nobody ordered. Every
    field is optional: the same code runs on a developer's laptop, where
    ``/proc`` does not exist, and a missing number is reported as unknown
    rather than raising.
    """
    facts: dict = {"disk": None, "memory": None, "load": None, "uptime_seconds": None}

    try:
        total, used, free = shutil.disk_usage(DATA_DIR)
        facts["disk"] = {
            "total": total,
            "used": used,
            "free": free,
            "percent": round(used / total * 100, 1) if total else 0.0,
        }
    except OSError:
        pass

    facts["memory"] = _memory()

    try:
        facts["load"] = os.getloadavg()
    except (OSError, AttributeError):  # not available on Windows
        pass

    uptime = Path("/proc/uptime")
    if uptime.is_file():
        try:
            facts["uptime_seconds"] = float(
                uptime.read_text(encoding="utf-8").split()[0]
            )
        except (OSError, ValueError, IndexError):
            pass

    return facts


def _memory() -> Optional[dict]:
    """Total and available memory in bytes, from /proc/meminfo."""
    meminfo = Path("/proc/meminfo")

    if not meminfo.is_file():
        return None

    try:
        fields = {}
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                fields[key.strip()] = int(parts[0]) * 1024  # reported in kB
    except (OSError, ValueError):
        return None

    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")

    if not total:
        return None

    used = total - (available or 0)

    return {
        "total": total,
        "available": available or 0,
        "used": used,
        "percent": round(used / total * 100, 1),
    }
