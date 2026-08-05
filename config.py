"""Central configuration: paths, status vocabulary, and logging.

Every module imports its settings from here so there is exactly one place to
change a path, a model name, or a log destination.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# PATHS
# =====================================================================
# Resolved from this file rather than the working directory, so the app
# behaves the same whether it is started by Streamlit, uvicorn, or pytest.
BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data")).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR") or (BASE_DIR / "logs")).resolve()

# Central account store. Per-user job data lives under WORKSPACES_DIR.
USERS_DB_PATH = DATA_DIR / "users.db"
WORKSPACES_DIR = DATA_DIR / "workspaces"

# OAuth client secret downloaded from Google Cloud Console.
GMAIL_CREDENTIALS_PATH = Path(
    os.getenv("GMAIL_CREDENTIALS_PATH") or (BASE_DIR / "credentials.json")
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

# =====================================================================
# STATUS SSOT (Single Source of Truth)
# =====================================================================
# The AI email classifier, the API, and the UI all validate against this
# list. Adding a status here makes it available everywhere.
VALID_STATUSES = [
    "APPLIED",
    "ASSESSMENT",
    "INTERVIEW",
    "OFFER",
    "REJECTED",
    "ACTION_REQUIRED",
]

DEFAULT_STATUS = "APPLIED"

# Statuses that mean the application is still alive.
ACTIVE_STATUSES = {"APPLIED", "ASSESSMENT", "INTERVIEW", "ACTION_REQUIRED"}

# Display metadata for the UI. Keys must stay in sync with VALID_STATUSES.
STATUS_LABELS = {
    "APPLIED": "🔵 Applied",
    "ASSESSMENT": "🟠 Assessment",
    "INTERVIEW": "🟣 Interview",
    "OFFER": "🟢 Offer",
    "REJECTED": "🔴 Rejected",
    "ACTION_REQUIRED": "🟡 Action Required",
}

JOB_SOURCES = ["Manual", "LinkedIn", "Company Site", "Wellfound", "Indeed", "Web Extension"]

# =====================================================================
# AI / GEMINI
# =====================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

# =====================================================================
# API SERVER
# =====================================================================
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8501")

# Browser extensions get a chrome-extension:// origin. Matching by regex keeps
# ordinary web pages (the actual CSRF risk against a localhost server) out,
# without needing to hardcode an extension id that changes between installs.
ALLOWED_ORIGIN_REGEX = os.getenv(
    "ALLOWED_ORIGIN_REGEX", r"^chrome-extension://[a-p]{32}$"
)

# How long an issued API token stays valid.
TOKEN_TTL_DAYS = int(os.getenv("TOKEN_TTL_DAYS", "30"))

# Public base URL when hosted (e.g. https://tracker.example.com). Leaving this
# empty means "running locally", which switches Gmail to the desktop OAuth
# flow and relaxes the HTTPS expectations below.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
IS_HOSTED = bool(PUBLIC_URL)

# =====================================================================
# REGISTRATION AND ABUSE CONTROLS
# =====================================================================
# When set, registration requires this code. Essential once the app is
# reachable from the internet: without it, strangers can sign up and spend
# your Gemini quota.
SIGNUP_CODE = os.getenv("SIGNUP_CODE", "")

# Set to "true" to refuse all new registrations (single-user lockdown).
REGISTRATION_CLOSED = os.getenv("REGISTRATION_CLOSED", "false").lower() == "true"

# Failed sign-ins allowed per identifier before a temporary lockout.
MAX_LOGIN_ATTEMPTS = int(os.getenv("MAX_LOGIN_ATTEMPTS", "10"))
LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))

# Set to "true" ONLY when the app sits behind a reverse proxy you control
# (nginx, Caddy, a platform router). X-Forwarded-For is trivially forged by
# the client, so trusting it on a directly-exposed server would let an
# attacker sidestep the per-IP rate limit by inventing a new address each try.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"

# =====================================================================
# GMAIL SYNC
# =====================================================================
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "25"))
GMAIL_LOOKBACK_DAYS = int(os.getenv("GMAIL_LOOKBACK_DAYS", "5"))
# Free-tier Gemini allows ~15 requests/minute; 4s between calls stays under it.
GMAIL_THROTTLE_SECONDS = float(os.getenv("GMAIL_THROTTLE_SECONDS", "4"))

# =====================================================================
# LOGGING
# =====================================================================
# Set LOG_LEVEL=DEBUG in .env to see request bodies and per-call detail.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LOG_FILE = LOG_DIR / "app.log"
ERROR_LOG_FILE = LOG_DIR / "errors.log"

logger = logging.getLogger("JobTracker")

if not logger.handlers:
    logger.setLevel(LOG_LEVEL)

    _file_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(module)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # The console is read live, so it stays terse; the file carries the detail.
    _console_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Everything, rotated so a long-running sync cannot fill the disk.
    _file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(_file_formatter)
    logger.addHandler(_file_handler)

    # Warnings and errors again on their own, so a problem is not buried in
    # thousands of routine request lines.
    _error_handler = RotatingFileHandler(
        ERROR_LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    _error_handler.setLevel(logging.WARNING)
    _error_handler.setFormatter(_file_formatter)
    logger.addHandler(_error_handler)

    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_console_formatter)
    logger.addHandler(_console_handler)

    # Streamlit re-imports modules on every rerun; without this the root
    # logger picks up duplicate records.
    logger.propagate = False
