"""Per-workspace job application storage.

Each user's applications live in their own SQLite file (see ``workspace.py``),
so every function here takes the ``db_path`` it should operate on.

Rows are returned as plain dicts keyed by column name — callers must never
depend on column order, because migrations append columns.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from config import ACTIVE_STATUSES, DEFAULT_STATUS, VALID_STATUSES, logger

# Bumped whenever the schema changes; see _migrate().
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company      TEXT NOT NULL,
    role         TEXT NOT NULL,
    jd           TEXT,
    status       TEXT NOT NULL DEFAULT 'APPLIED',
    date_applied TEXT,
    link         TEXT,
    notes        TEXT,
    source       TEXT NOT NULL DEFAULT 'Manual',
    resume_used  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- One row per company+role. This is the single definition of "duplicate",
-- enforced by the database rather than by a racy SELECT-then-INSERT.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_identity
    ON jobs (LOWER(company), LOWER(role));

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);

-- Gmail message ids already classified, so repeat syncs are no-ops instead of
-- appending the same note again.
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id   TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
"""

JOB_COLUMNS = [
    "id",
    "company",
    "role",
    "jd",
    "status",
    "date_applied",
    "link",
    "notes",
    "source",
    "resume_used",
    "created_at",
    "updated_at",
]


class DuplicateJobError(Exception):
    """Raised when a company+role pair is already tracked."""


# =====================================================================
# CONNECTION HANDLING
# =====================================================================
@contextmanager
def connect(db_path) -> Iterator[sqlite3.Connection]:
    """Open a workspace database, committing on success, rolling back on error.

    Always use this rather than raw ``sqlite3.connect`` — it guarantees the
    handle is closed even when a query raises, which on Windows is the
    difference between a clean exit and a locked file.
    """
    path = Path(db_path)
    if path.parent != Path("."):
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


def create_table(db_path) -> None:
    """Create the schema and apply any pending migrations."""
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn, db_path)


def _migrate(conn: sqlite3.Connection, db_path) -> None:
    """Apply schema migrations based on ``PRAGMA user_version``.

    Version-stamped migrations replace the old pattern of firing ALTER TABLE
    and swallowing the error, which could not distinguish "already applied"
    from a genuine failure.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]

    if current == SCHEMA_VERSION:
        return

    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"{db_path} was written by a newer version of this app "
            f"(schema v{current} > v{SCHEMA_VERSION}). Please update."
        )

    # Future migrations go here as `if current < N: ...` blocks.

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    logger.info("Schema initialised at v%s for %s", SCHEMA_VERSION, db_path)


# =====================================================================
# WRITES
# =====================================================================
def add_job(
    company: str,
    role: str,
    jd: str = "",
    status: str = DEFAULT_STATUS,
    date_applied: Optional[str] = None,
    link: str = "",
    notes: str = "",
    source: str = "Manual",
    resume_used: Optional[str] = None,
    *,
    db_path,
) -> int:
    """Insert a job application and return its id.

    Raises ``DuplicateJobError`` if this company+role is already tracked, and
    ``ValueError`` if required fields are blank or the status is unknown.
    """
    company = (company or "").strip()
    role = (role or "").strip()

    if not company or not role:
        raise ValueError("Company and role are both required.")

    status = _validate_status(status)
    date_applied = date_applied or _today()
    now = _utcnow()

    try:
        with connect(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    company, role, jd, status, date_applied, link, notes,
                    source, resume_used, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company,
                    role,
                    jd,
                    status,
                    date_applied,
                    link,
                    notes,
                    source,
                    resume_used,
                    now,
                    now,
                ),
            )
            job_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        logger.warning("Duplicate rejected: %s - %s", company, role)
        raise DuplicateJobError(f"{company} - {role} is already tracked.") from exc

    logger.info("Added job #%s: %s - %s", job_id, company, role)
    return job_id


def update_status(job_id: int, new_status: str, *, db_path) -> bool:
    """Update a job's status. Returns False if the id does not exist."""
    new_status = _validate_status(new_status)

    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, _utcnow(), int(job_id)),
        )
        updated = cursor.rowcount > 0

    if updated:
        logger.info("Job #%s status -> %s", job_id, new_status)
    else:
        logger.warning("Tried to update missing job #%s", job_id)

    return updated


def delete_job(job_id: int, *, db_path) -> bool:
    """Delete a job application. Returns False if the id does not exist."""
    with connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM jobs WHERE id = ?", (int(job_id),))
        deleted = cursor.rowcount > 0

    if deleted:
        logger.info("Deleted job #%s", job_id)

    return deleted


def append_note(job_id: int, note: str, *, db_path) -> bool:
    """Append a timestamped note to a job."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT notes FROM jobs WHERE id = ?", (int(job_id),)
        ).fetchone()

        if not row:
            return False

        existing = row["notes"] or ""
        combined = f"{existing}\n\n{note}".strip()
        conn.execute(
            "UPDATE jobs SET notes = ?, updated_at = ? WHERE id = ?",
            (combined, _utcnow(), int(job_id)),
        )

    return True


def update_job_from_email(
    company_name: str,
    category: str,
    subject: str,
    reasoning: str,
    *,
    db_path,
) -> str:
    """Apply an AI-classified email to the workspace.

    Updates the matching company's status and appends the email as a note; if
    no such company is tracked, creates a new entry. Returns ``"updated"`` or
    ``"created"``.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        raise ValueError("Company name is required.")

    category = _validate_status(category)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    note = f"[{timestamp} | {category}]\nSubject: {subject}\nAI Note: {reasoning}"
    now = _utcnow()

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, notes FROM jobs WHERE LOWER(company) = LOWER(?) ORDER BY id LIMIT 1",
            (company_name,),
        ).fetchone()

        if row:
            existing = row["notes"] or ""
            conn.execute(
                "UPDATE jobs SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
                (category, f"{existing}\n\n{note}".strip(), now, row["id"]),
            )
            logger.info("Email updated %s -> %s", company_name, category)
            return "updated"

        conn.execute(
            """
            INSERT INTO jobs (
                company, role, status, date_applied, notes, source,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_name,
                "Unknown Role",
                category,
                _today(),
                note,
                "Email Sync",
                now,
                now,
            ),
        )
        logger.info("Email created tracking for %s -> %s", company_name, category)
        return "created"


# =====================================================================
# READS
# =====================================================================
def get_all_jobs(*, db_path) -> list[dict]:
    """Return every job as a dict, newest first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs ORDER BY id DESC"
        ).fetchall()

    return [dict(row) for row in rows]


def get_job(job_id: int, *, db_path) -> Optional[dict]:
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs WHERE id = ?", (int(job_id),)
        ).fetchone()

    return dict(row) if row else None


def check_if_applied(company: str, role: str, *, db_path) -> tuple[bool, Optional[str]]:
    """Return ``(exists, status)`` for a company+role pair.

    Safe to call before the workspace has been initialised — a missing file
    reads as "not applied" rather than raising.
    """
    if not Path(db_path).exists():
        return False, None

    try:
        with connect(db_path) as conn:
            row = conn.execute(
                "SELECT status FROM jobs WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?)",
                ((company or "").strip(), (role or "").strip()),
            ).fetchone()
    except sqlite3.OperationalError:
        # Database file exists but the schema has not been created yet.
        return False, None

    return (True, row["status"]) if row else (False, None)


def get_stats(*, db_path) -> dict:
    """Aggregate counts for the dashboard header."""
    jobs = get_all_jobs(db_path=db_path)
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job["status"]] = counts.get(job["status"], 0) + 1

    total = len(jobs)
    # "Responded" means anything beyond the initial application.
    responded = total - counts.get("APPLIED", 0)

    return {
        "total": total,
        "active": sum(counts.get(s, 0) for s in ACTIVE_STATUSES),
        "interviews": counts.get("INTERVIEW", 0),
        "offers": counts.get("OFFER", 0),
        "rejected": counts.get("REJECTED", 0),
        "response_rate": round(responded / total * 100) if total else 0,
        "by_status": counts,
    }


# =====================================================================
# EMAIL DEDUPE
# =====================================================================
def is_email_processed(message_id: str, *, db_path) -> bool:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)
        ).fetchone()

    return row is not None


def mark_email_processed(message_id: str, *, db_path) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_emails (message_id, processed_at) VALUES (?, ?)",
            (message_id, _utcnow()),
        )


# =====================================================================
# HELPERS
# =====================================================================
def _validate_status(status: str) -> str:
    normalized = (status or "").strip().upper()
    if normalized not in VALID_STATUSES:
        raise ValueError(
            f"Unknown status {status!r}. Expected one of: {', '.join(VALID_STATUSES)}"
        )
    return normalized


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")
