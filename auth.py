"""Account registration, sign-in, and API tokens.

Accounts live in a single central database (``data/users.db``). Job data never
does — that is per-workspace, keyed by the account id issued here.

Passwords are stored as PBKDF2-HMAC-SHA256 digests with a per-user random
salt. API tokens are stored as SHA-256 digests, so a copy of the database
does not hand out live sessions.
"""

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from config import (
    LOGIN_LOCKOUT_MINUTES,
    MAX_LOGIN_ATTEMPTS,
    REGISTRATION_CLOSED,
    SIGNUP_CODE,
    TOKEN_TTL_DAYS,
    USERS_DB_PATH,
    logger,
)

# PBKDF2 parameters. Iterations are deliberately high; verification happens
# once per sign-in, not per request (requests carry a token instead).
_HASH_ALGORITHM = "sha256"
_ITERATIONS = 600_000
_SALT_BYTES = 16

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD_LENGTH = 8


class AuthError(Exception):
    """Raised for any user-facing authentication or registration failure."""


class RateLimitError(AuthError):
    """Raised when an identifier has exceeded its failed sign-in allowance."""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    created_at: str


# =====================================================================
# CONNECTION HANDLING
# =====================================================================
@contextmanager
def _connect(db_path=None) -> Iterator[sqlite3.Connection]:
    """Open the users database, committing on success and rolling back on error."""
    path = db_path or USERS_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None) -> None:
    """Create the account tables if they do not exist yet."""
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS api_tokens (
                token_hash   TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                created_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                last_used_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tokens_user ON api_tokens(user_id);

            -- Failed sign-ins, used for temporary lockout. Kept in the
            -- database rather than in memory so the limit survives a restart
            -- and holds across multiple worker processes.
            CREATE TABLE IF NOT EXISTS login_attempts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier   TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_attempts
                ON login_attempts(identifier, attempted_at);

            -- Short-lived, single-use codes that hand an authenticated
            -- session from the extension to the dashboard without making
            -- the user sign in twice.
            CREATE TABLE IF NOT EXISTS handoff_codes (
                code_hash  TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )


# =====================================================================
# PASSWORD HASHING
# =====================================================================
def hash_password(password: str) -> str:
    """Return a self-describing ``algo$iterations$salt$hash`` digest."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        _HASH_ALGORITHM, password.encode("utf-8"), salt, _ITERATIONS
    )
    return "$".join(
        [
            f"pbkdf2_{_HASH_ALGORITHM}",
            str(_ITERATIONS),
            _b64(salt),
            _b64(digest),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored digest in constant time."""
    try:
        algorithm, iterations, salt_b64, hash_b64 = encoded.split("$")
        if not algorithm.startswith("pbkdf2_"):
            return False

        digest = hashlib.pbkdf2_hmac(
            algorithm.removeprefix("pbkdf2_"),
            password.encode("utf-8"),
            _unb64(salt_b64),
            int(iterations),
        )
        return hmac.compare_digest(digest, _unb64(hash_b64))
    except (ValueError, TypeError):
        return False


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(encoded: str) -> bytes:
    return base64.b64decode(encoded.encode("ascii"))


# Verified against when an email is unknown, so a failed sign-in costs the same
# time whether or not the account exists. The values are placeholders; only the
# PBKDF2 work it forces matters.
_DUMMY_HASH = "$".join(
    [
        f"pbkdf2_{_HASH_ALGORITHM}",
        str(_ITERATIONS),
        _b64(b"\x00" * _SALT_BYTES),
        _b64(b"\x00" * 32),
    ]
)


# =====================================================================
# VALIDATION
# =====================================================================
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_credentials(email: str, password: str) -> str:
    """Validate a new account's details, returning the normalized email."""
    email = normalize_email(email)

    if not _EMAIL_RE.match(email):
        raise AuthError("Please enter a valid email address.")
    if len(email) > 254:
        raise AuthError("That email address is too long.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if len(password) > 1024:
        raise AuthError("That password is too long.")

    return email


# =====================================================================
# ACCOUNTS
# =====================================================================
def register(
    email: str, password: str, signup_code: str = "", db_path=None
) -> User:
    """Create a new account.

    Raises ``AuthError`` if registration is closed, the invite code is wrong,
    or the email is already taken.
    """
    if REGISTRATION_CLOSED:
        raise AuthError("Registration is closed on this instance.")

    # Compared in constant time so the code cannot be guessed character by
    # character from response timing.
    if SIGNUP_CODE and not hmac.compare_digest(
        (signup_code or "").strip(), SIGNUP_CODE
    ):
        logger.warning("Registration rejected for %r: bad invite code", email)
        raise AuthError("That invite code is not valid.")

    email = validate_credentials(email, password)
    now = _utcnow()

    try:
        with _connect(db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, hash_password(password), now),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise AuthError("An account with that email already exists.") from exc

    logger.info("Registered new account id=%s", user_id)
    return User(id=user_id, email=email, created_at=now)


def authenticate(
    email: str, password: str, client_ip: str = "", db_path=None
) -> User:
    """Verify credentials and return the user.

    Raises ``RateLimitError`` when the email or client IP has too many recent
    failures, and ``AuthError`` when the credentials are simply wrong.
    """
    email = normalize_email(email)

    # Both the account and the source address are limited: the first stops
    # someone hammering one account, the second stops them spraying many.
    for identifier in filter(None, [f"email:{email}", f"ip:{client_ip}" if client_ip else ""]):
        if is_locked_out(identifier, db_path=db_path):
            logger.warning("Sign-in blocked by rate limit: %s", identifier)
            raise RateLimitError(
                f"Too many failed attempts. Please wait {LOGIN_LOCKOUT_MINUTES} "
                "minutes and try again."
            )

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, created_at FROM users WHERE email = ?",
            (email,),
        ).fetchone()

        # Always run a verification so response time does not reveal whether
        # the account exists.
        stored_hash = row["password_hash"] if row else _DUMMY_HASH
        password_ok = verify_password(password or "", stored_hash)

    if not row or not password_ok:
        logger.warning("Failed sign-in attempt for %r from %r", email, client_ip or "local")
        record_failed_attempt(f"email:{email}", db_path=db_path)
        if client_ip:
            record_failed_attempt(f"ip:{client_ip}", db_path=db_path)
        raise AuthError("Incorrect email or password.")

    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?", (_utcnow(), row["id"])
        )
        # A success clears the slate so a user who mistyped twice is not
        # punished later.
        conn.execute(
            "DELETE FROM login_attempts WHERE identifier = ?", (f"email:{email}",)
        )

    logger.info("Successful sign-in for account id=%s", row["id"])
    return User(id=row["id"], email=row["email"], created_at=row["created_at"])


# =====================================================================
# RATE LIMITING
# =====================================================================
def record_failed_attempt(identifier: str, db_path=None) -> None:
    """Record one failed sign-in against an email or IP."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO login_attempts (identifier, attempted_at) VALUES (?, ?)",
            (identifier, _utcnow()),
        )


def is_locked_out(identifier: str, db_path=None) -> bool:
    """True when this identifier has exceeded its recent failure allowance."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
    ).isoformat()

    with _connect(db_path) as conn:
        # Drop expired rows on the way past so the table stays small without
        # needing a scheduled cleanup.
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
        count = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE identifier = ? AND attempted_at >= ?",
            (identifier, cutoff),
        ).fetchone()[0]

    return count >= MAX_LOGIN_ATTEMPTS


def clear_attempts(identifier: str, db_path=None) -> None:
    """Manually lift a lockout (used by tests and admin recovery)."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM login_attempts WHERE identifier = ?", (identifier,))


def get_user(user_id: int, db_path=None) -> Optional[User]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, email, created_at FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()

    return User(**dict(row)) if row else None


def change_password(
    user_id: int, current_password: str, new_password: str, db_path=None
) -> None:
    """Rotate a password and revoke every existing token for that account."""
    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()

        if not row or not verify_password(current_password or "", row["password_hash"]):
            raise AuthError("Current password is incorrect.")

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), int(user_id)),
        )
        # A password change should invalidate sessions elsewhere.
        conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (int(user_id),))

    logger.info("Password changed for account id=%s; tokens revoked", user_id)


# =====================================================================
# API TOKENS
# =====================================================================
def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(user_id: int, db_path=None) -> str:
    """Mint an API token for the extension. The plaintext is returned once."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=TOKEN_TTL_DAYS)

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO api_tokens (token_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (_token_digest(token), int(user_id), now.isoformat(), expires.isoformat()),
        )
        # Housekeeping: drop anything already past its expiry.
        conn.execute(
            "DELETE FROM api_tokens WHERE expires_at < ?", (now.isoformat(),)
        )

    return token


def verify_token(token: str, db_path=None) -> Optional[User]:
    """Resolve a bearer token to a user, or ``None`` if invalid or expired."""
    if not token:
        return None

    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT u.id, u.email, u.created_at, t.expires_at
            FROM api_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ?
            """,
            (_token_digest(token),),
        ).fetchone()

        if not row:
            return None

        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            conn.execute(
                "DELETE FROM api_tokens WHERE token_hash = ?", (_token_digest(token),)
            )
            return None

        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?",
            (_utcnow(), _token_digest(token)),
        )

    return User(id=row["id"], email=row["email"], created_at=row["created_at"])


def revoke_token(token: str, db_path=None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM api_tokens WHERE token_hash = ?", (_token_digest(token),)
        )


# =====================================================================
# HANDOFF CODES (extension -> dashboard single sign-on)
# =====================================================================
# Deliberately short: the code only has to survive opening a new tab.
HANDOFF_TTL_SECONDS = 60


def issue_handoff_code(user_id: int, db_path=None) -> str:
    """Mint a single-use code that signs this user into the dashboard.

    The code travels in a URL, so it is stored hashed, expires in a minute,
    and is destroyed the first time it is redeemed.
    """
    code = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO handoff_codes (code_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                _token_digest(code),
                int(user_id),
                now.isoformat(),
                (now + timedelta(seconds=HANDOFF_TTL_SECONDS)).isoformat(),
            ),
        )
        conn.execute(
            "DELETE FROM handoff_codes WHERE expires_at < ?", (now.isoformat(),)
        )

    return code


def consume_handoff_code(code: str, db_path=None) -> Optional[User]:
    """Redeem a handoff code exactly once, returning the user or None."""
    if not code:
        return None

    digest = _token_digest(code)

    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT u.id, u.email, u.created_at, h.expires_at
            FROM handoff_codes h
            JOIN users u ON u.id = h.user_id
            WHERE h.code_hash = ?
            """,
            (digest,),
        ).fetchone()

        if not row:
            return None

        # Deleted whether or not it had expired, so a code is never reusable.
        conn.execute("DELETE FROM handoff_codes WHERE code_hash = ?", (digest,))

        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            logger.warning("Rejected expired handoff code for account %s", row["id"])
            return None

    logger.info("Handoff code redeemed for account %s", row["id"])
    return User(id=row["id"], email=row["email"], created_at=row["created_at"])


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Make sure the tables exist as soon as anything imports this module.
init_db()
