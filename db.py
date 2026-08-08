"""Per-workspace job application storage.

Each user's applications live in their own SQLite file (see ``workspace.py``),
so every function here takes the ``db_path`` it should operate on.

Rows are returned as plain dicts keyed by column name — callers must never
depend on column order, because migrations append columns.
"""

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from config import (
    ACTIVE_STATUSES,
    DEFAULT_STATUS,
    STATUS_RANK,
    VALID_STATUSES,
    logger,
)

# Bumped whenever the schema changes; see _migrate().
#   v1 -> v2  adds status_history
#   v2 -> v3  adds the recruiter contact on each application
SCHEMA_VERSION = 3

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
    -- Who to reply to. Inbox sync knows the sender of every email it
    -- classifies and used to discard it, so "who is handling this?" was a
    -- question the tracker could not answer about its own applications.
    contact_name    TEXT,
    contact_email   TEXT,
    last_contact_at TEXT,
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

-- Every status observation, whether or not it changed the job's status.
--
-- The jobs table holds only where an application stands *now*, which cannot
-- answer "how long did they sit on my assessment?" or "when did this go
-- quiet?". This table is the append-only record that can.
--
-- `applied = 0` marks an observation that was deliberately not written to
-- jobs.status — a backwards move rejected by the rank check. Keeping it
-- visible means a wrong-looking timeline can be explained rather than guessed
-- at.
CREATE TABLE IF NOT EXISTS status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    applied     INTEGER NOT NULL DEFAULT 1,
    source      TEXT NOT NULL DEFAULT 'Manual',
    reason      TEXT,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_job ON status_history (job_id, id);
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
    "contact_name",
    "contact_email",
    "last_contact_at",
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

    if current < 2:
        # status_history itself is created by _SCHEMA above, which has already
        # run. What this step adds is a seed row for every job that predates
        # the table, so an existing tracker shows a timeline rather than an
        # empty panel.
        #
        # `created_at` is used as the timestamp because it is the only date
        # available: the real transition times were never recorded. `source`
        # says 'Migration' so a backfilled entry is never mistaken for an
        # observation the app actually made.
        seeded = conn.execute(
            """
            INSERT INTO status_history
                (job_id, from_status, to_status, applied, source, reason, occurred_at)
            SELECT id, NULL, status, 1, 'Migration',
                   'Recorded when stage history was introduced', created_at
            FROM jobs
            WHERE id NOT IN (SELECT job_id FROM status_history)
            """
        ).rowcount

        if seeded:
            logger.info("Backfilled stage history for %s job(s) in %s", seeded, db_path)

    if current < 3:
        # _SCHEMA declares these, but CREATE TABLE IF NOT EXISTS is a no-op on
        # a database that already has a jobs table, so an existing workspace
        # needs them added. Checked against the live column list rather than
        # catching the duplicate-column error, so a fresh database — where
        # _SCHEMA did create them and user_version is still 0 — passes through
        # here without either failing or silently swallowing a real problem.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}

        for column in ("contact_name", "contact_email", "last_contact_at"):
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
                logger.info("Added jobs.%s to %s", column, db_path)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    logger.info(
        "Schema at v%s for %s (was v%s)", SCHEMA_VERSION, db_path, current
    )


# =====================================================================
# STAGE HISTORY
# =====================================================================
def _record_transition(
    conn: sqlite3.Connection,
    job_id: int,
    from_status: Optional[str],
    to_status: str,
    *,
    applied: bool,
    source: str,
    reason: str = "",
) -> None:
    """Append one status observation. Takes an open connection deliberately,
    so the history row and the jobs row commit or roll back together."""
    conn.execute(
        """
        INSERT INTO status_history
            (job_id, from_status, to_status, applied, source, reason, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (int(job_id), from_status, to_status, 1 if applied else 0, source, reason, _utcnow()),
    )


def advances(current: Optional[str], proposed: str) -> bool:
    """True when ``proposed`` is at least as far along as ``current``.

    Guards automated updates only. A person editing the dashboard is
    authoritative and bypasses this — the point is to stop a batch of email
    arriving out of order from rewinding an application, not to stop the user
    correcting a mistake.
    """
    if not current:
        return True
    return STATUS_RANK.get(proposed, 0) >= STATUS_RANK.get(current, 0)


def get_recent_status_changes(*, db_path, limit: int = 40) -> list[dict]:
    """Recent status observations across every application, newest first.

    Joined onto jobs so the dashboard can name the application without a
    second query per row.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT h.id, h.job_id, h.from_status, h.to_status, h.applied,
                   h.source, h.reason, h.occurred_at,
                   j.company, j.role
            FROM status_history h
            JOIN jobs j ON j.id = h.job_id
            ORDER BY h.occurred_at DESC, h.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    return [dict(row) for row in rows]


def get_status_history(job_id: int, *, db_path) -> list[dict]:
    """Return every status observation for a job, oldest first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, job_id, from_status, to_status, applied, source, reason, occurred_at
            FROM status_history
            WHERE job_id = ?
            ORDER BY occurred_at, id
            """,
            (int(job_id),),
        ).fetchall()

    return [dict(row) for row in rows]


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
            _record_transition(
                conn, job_id, None, status,
                applied=True, source=source, reason="Application added",
            )
    except sqlite3.IntegrityError as exc:
        logger.warning("Duplicate rejected: %s - %s", company, role)
        raise DuplicateJobError(f"{company} - {role} is already tracked.") from exc

    logger.info("Added job #%s: %s - %s", job_id, company, role)
    return job_id


def update_status(
    job_id: int, new_status: str, *, db_path, source: str = "Manual", reason: str = ""
) -> bool:
    """Update a job's status. Returns False if the id does not exist.

    Always applies the change — this is the path a person drives from the
    dashboard, and the user is the authority on their own application. The
    rank guard in :func:`advances` exists for automated callers.
    """
    new_status = _validate_status(new_status)

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (int(job_id),)
        ).fetchone()

        if row is None:
            logger.warning("Tried to update missing job #%s", job_id)
            return False

        previous = row["status"]

        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, _utcnow(), int(job_id)),
        )

        # A no-op re-save is not a transition and would only clutter the
        # timeline, so it is not recorded.
        if previous != new_status:
            _record_transition(
                conn, job_id, previous, new_status,
                applied=True, source=source, reason=reason,
            )
            logger.info(
                "Job #%s status %s -> %s (%s)", job_id, previous, new_status, source
            )

    return True


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


def _find_job_for_email(
    conn: sqlite3.Connection, company_name: str, role: str
) -> Optional[sqlite3.Row]:
    """Locate the application an email is about.

    Matching on company alone was wrong: two applications at one company meant
    a rejection for the second role silently flipped the first one. When the
    email names a role, that role picks the row; only when it does not does
    the search fall back to the company, and then only when the company has a
    single tracked application.
    """
    role = (role or "").strip()

    if role:
        # Exact role first, then a containment match so "Senior Platform
        # Engineer" in an email still finds a "Platform Engineer" row.
        row = conn.execute(
            """
            SELECT id, notes, status, role FROM jobs
            WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?)
            ORDER BY id LIMIT 1
            """,
            (company_name, role),
        ).fetchone()

        if row:
            return row

        row = conn.execute(
            """
            SELECT id, notes, status, role FROM jobs
            WHERE LOWER(company) = LOWER(?)
              AND (INSTR(LOWER(?), LOWER(role)) > 0 OR INSTR(LOWER(role), LOWER(?)) > 0)
            ORDER BY id LIMIT 1
            """,
            (company_name, role, role),
        ).fetchone()

        if row:
            return row

    candidates = conn.execute(
        "SELECT id, notes, status, role FROM jobs WHERE LOWER(company) = LOWER(?) ORDER BY id",
        (company_name,),
    ).fetchall()

    # Exactly one application at this company — unambiguous, so use it. More
    # than one and there is no way to tell which the email is about; returning
    # None makes the caller create a new row rather than corrupt a good one.
    if len(candidates) == 1:
        return candidates[0]

    return None


# Mailboxes that exist to send and not to receive. Storing one as the contact
# is worse than storing nothing: it reads as somebody to reply to.
_NOREPLY_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notification|notifications|"
    r"automated|auto|mailer|bounce|postmaster|noreply)",
    re.IGNORECASE,
)


def is_replyable(address: str) -> bool:
    """True when an address looks like a person rather than a send-only robot."""
    address = (address or "").strip()
    if "@" not in address:
        return False
    return not _NOREPLY_LOCAL.match(address.split("@", 1)[0])


def update_job_from_email(
    company_name: str,
    category: str,
    subject: str,
    reasoning: str,
    *,
    db_path,
    role: str = "",
    contact_name: str = "",
    contact_email: str = "",
) -> str:
    """Apply an AI-classified email to the workspace.

    Returns ``"updated"`` when an existing application moved forward,
    ``"noted"`` when the email was recorded but the status was left alone
    (a backwards move), or ``"created"`` when a new application was tracked.

    The sender is recorded as the application's contact when it looks like a
    person. ``last_contact_at`` moves either way — an automated "we received
    your application" is still the employer making contact, and that is what
    the follow-up view measures silence against.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        raise ValueError("Company name is required.")

    category = _validate_status(category)
    role = (role or "").strip()

    # NULL rather than "" so COALESCE below keeps a human contact already on
    # the record when a later no-reply message arrives about the same job.
    contact_email = (contact_email or "").strip()
    if is_replyable(contact_email):
        new_contact_email = contact_email
        new_contact_name = (contact_name or "").strip() or contact_email
    else:
        new_contact_email = None
        new_contact_name = None

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    note = f"[{timestamp} | {category}]\nSubject: {subject}\nAI Note: {reasoning}"
    now = _utcnow()

    with connect(db_path) as conn:
        row = _find_job_for_email(conn, company_name, role)

        if row:
            existing = row["notes"] or ""
            combined = f"{existing}\n\n{note}".strip()
            previous = row["status"]
            moves_forward = advances(previous, category)

            contact_update = (
                "contact_name = COALESCE(?, contact_name), "
                "contact_email = COALESCE(?, contact_email), "
                "last_contact_at = ?"
            )
            contact_values = (new_contact_name, new_contact_email, now)

            if moves_forward and previous != category:
                conn.execute(
                    f"UPDATE jobs SET status = ?, notes = ?, updated_at = ?, "
                    f"{contact_update} WHERE id = ?",
                    (category, combined, now, *contact_values, row["id"]),
                )
            else:
                # The note is still worth keeping even when the status is not
                # changed — it is evidence of what arrived and when.
                conn.execute(
                    f"UPDATE jobs SET notes = ?, updated_at = ?, "
                    f"{contact_update} WHERE id = ?",
                    (combined, now, *contact_values, row["id"]),
                )

            if previous != category:
                _record_transition(
                    conn, row["id"], previous, category,
                    applied=moves_forward,
                    source="Email Sync",
                    reason=subject,
                )

            if moves_forward:
                logger.info(
                    "Email moved %s / %s: %s -> %s",
                    company_name, row["role"], previous, category,
                )
                return "updated"

            logger.info(
                "Email for %s / %s noted but not applied: %s would move back from %s",
                company_name, row["role"], category, previous,
            )
            return "noted"

        try:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    company, role, status, date_applied, notes, source,
                    contact_name, contact_email, last_contact_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_name,
                    role or "Unknown Role",
                    category,
                    _today(),
                    note,
                    "Email Sync",
                    new_contact_name,
                    new_contact_email,
                    now,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            # The company has several tracked roles, so _find_job_for_email
            # declined to guess — and one of them already occupies this exact
            # company+role. Attach the note there rather than lose the email.
            existing_row = conn.execute(
                """
                SELECT id, notes FROM jobs
                WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?)
                """,
                (company_name, role or "Unknown Role"),
            ).fetchone()

            conn.execute(
                "UPDATE jobs SET notes = ?, updated_at = ?, "
                "contact_name = COALESCE(?, contact_name), "
                "contact_email = COALESCE(?, contact_email), "
                "last_contact_at = ? WHERE id = ?",
                (
                    f"{existing_row['notes'] or ''}\n\n{note}".strip(),
                    now,
                    new_contact_name,
                    new_contact_email,
                    now,
                    existing_row["id"],
                ),
            )
            logger.info(
                "Email for %s / %s attached to existing row #%s",
                company_name, role or "Unknown Role", existing_row["id"],
            )
            return "noted"

        _record_transition(
            conn, cursor.lastrowid, None, category,
            applied=True, source="Email Sync", reason=subject,
        )
        logger.info(
            "Email created tracking for %s / %s -> %s",
            company_name, role or "Unknown Role", category,
        )
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


def get_followups(*, db_path, quiet_after_days: int = 10) -> list[dict]:
    """Live applications that have gone quiet, longest silence first.

    The tracker could say where every application stood but not which ones
    were drifting, which is the question that actually changes what you do
    next. Silence is measured from the most recent real signal — the last
    email from the employer, the last stage change, or failing both the date
    applied — so an application that moved yesterday is not reported as stale
    merely because it was submitted months ago.

    Terminal statuses are excluded: an offer or a rejection is not waiting on
    anybody.
    """
    with connect(db_path) as conn:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        rows = conn.execute(
            f"""
            SELECT j.id, j.company, j.role, j.status, j.link,
                   j.date_applied, j.contact_name, j.contact_email,
                   MAX(
                       COALESCE(j.last_contact_at, ''),
                       COALESCE((SELECT MAX(h.occurred_at) FROM status_history h
                                 WHERE h.job_id = j.id), ''),
                       COALESCE(j.date_applied, ''),
                       COALESCE(j.created_at, '')
                   ) AS last_activity
            FROM jobs j
            WHERE j.status IN ({placeholders})
            ORDER BY last_activity ASC
            """,
            tuple(sorted(ACTIVE_STATUSES)),
        ).fetchall()

    today = datetime.now(timezone.utc)
    followups = []

    for row in rows:
        job = dict(row)
        # Timestamps are written in two shapes — a full UTC stamp from
        # _utcnow() and a bare date from _today() — so only the date part is
        # comparable across both.
        stamp = (job.pop("last_activity", "") or "")[:10]

        try:
            last = datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            # Undatable rows are surfaced rather than hidden: a job with no
            # usable timestamp is exactly the kind that gets forgotten.
            job["days_quiet"] = None
            job["last_activity"] = None
            followups.append(job)
            continue

        job["days_quiet"] = (today - last).days
        job["last_activity"] = stamp
        followups.append(job)

    # None sorts first: unknown means "look at this", not "ignore it".
    followups.sort(key=lambda j: (j["days_quiet"] is not None, -(j["days_quiet"] or 0)))

    return [
        job for job in followups
        if job["days_quiet"] is None or job["days_quiet"] >= quiet_after_days
    ]


def get_job_by_identity(company: str, role: str, *, db_path) -> Optional[dict]:
    """The tracked application for a company+role pair, or None.

    Used when drafting an answer. The application form is usually a different
    page from the posting — often an iframe with none of the description in it
    — so what was captured when the job was saved is better context than
    whatever the form page can see.
    """
    if not Path(db_path).exists():
        return None

    try:
        with connect(db_path) as conn:
            row = conn.execute(
                f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs "
                "WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?)",
                ((company or "").strip(), (role or "").strip()),
            ).fetchone()
    except sqlite3.OperationalError:
        # The file exists but the schema has not been created yet.
        return None

    return dict(row) if row else None


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
