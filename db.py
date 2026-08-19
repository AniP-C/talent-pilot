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

import posting
from config import (
    ACTIVE_STATUSES,
    DEFAULT_STATUS,
    STATUS_RANK,
    VALID_STATUSES,
    logger,
)

# Re-exported: the rule for "is this address a person?" belongs with the rest of
# the contact logic, and callers already reach for it here.
from contacts import is_replyable  # noqa: F401

# Bumped whenever the schema changes; see _migrate().
#   v1 -> v2  adds status_history
#   v2 -> v3  adds the recruiter contact on each application
#   v3 -> v4  adds the contact's phone number
#   v4 -> v5  adds the posting's location and salary
SCHEMA_VERSION = 5

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
    -- v5: read from the JSON-LD JobPosting block the extension already parses
    -- for company and role. Declared fields rather than prose, which is what
    -- makes them worth storing; the salary is kept as numbers rather than a
    -- display string so it can be filtered and compared later.
    location        TEXT,
    remote          INTEGER NOT NULL DEFAULT 0,
    salary_min      INTEGER,
    salary_max      INTEGER,
    salary_currency TEXT,
    salary_period   TEXT,
    -- Who to reply to. Inbox sync knows the sender of every email it
    -- classifies and used to discard it, so "who is handling this?" was a
    -- question the tracker could not answer about its own applications.
    contact_name    TEXT,
    contact_email   TEXT,
    -- Recruiters sign off with a direct line far more often than they are
    -- reachable on the address a relay sent from, so the number is worth as
    -- much as the address on an application that has gone quiet.
    contact_phone   TEXT,
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
    "location",
    "remote",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "contact_name",
    "contact_email",
    "contact_phone",
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

    if current < 4:
        # Same shape as the v3 step, and for the same reason: _SCHEMA declares
        # the column but CREATE TABLE IF NOT EXISTS does nothing to a jobs table
        # that already exists.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}

        if "contact_phone" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN contact_phone TEXT")
            logger.info("Added jobs.contact_phone to %s", db_path)

    if current < 5:
        # SQLite allows ADD COLUMN ... NOT NULL only with a non-NULL default,
        # which is why `remote` carries one. The rest are nullable: "no salary
        # recorded" and "a salary of zero" are different facts.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}

        for column, declaration in (
            ("location", "TEXT"),
            ("remote", "INTEGER NOT NULL DEFAULT 0"),
            ("salary_min", "INTEGER"),
            ("salary_max", "INTEGER"),
            ("salary_currency", "TEXT"),
            ("salary_period", "TEXT"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
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
    location: str = "",
    remote: bool = False,
    salary: Optional[dict] = None,
) -> int:
    """Insert a job application and return its id.

    Raises ``DuplicateJobError`` if this company+role is already tracked, and
    ``ValueError`` if required fields are blank or the status is unknown.

    ``salary`` is :func:`posting.normalise_salary`'s dict — ``min``, ``max``,
    ``currency``, ``period`` — passed whole rather than as four arguments,
    because the four are only ever meaningful together. ``None`` records no
    salary, which is different from a salary of zero.
    """
    company = (company or "").strip()
    role = (role or "").strip()

    if not company or not role:
        raise ValueError("Company and role are both required.")

    status = _validate_status(status)
    date_applied = date_applied or _today()
    now = _utcnow()
    salary = salary or posting.EMPTY_SALARY

    try:
        with connect(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    company, role, jd, status, date_applied, link, notes,
                    source, resume_used, location, remote,
                    salary_min, salary_max, salary_currency, salary_period,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    posting.normalise_location(location),
                    1 if remote else 0,
                    salary.get("min"),
                    salary.get("max"),
                    salary.get("currency") or None,
                    salary.get("period") or None,
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


def compose_email_note(
    category: str,
    subject: str,
    reasoning: str,
    *,
    sender: str = "",
    contact_name: str = "",
    contact_email: str = "",
    contact_phone: str = "",
    mentioned_emails: tuple[str, ...] = (),
    next_step: str = "",
    deadline: str = "",
) -> str:
    """The note one classified email leaves on an application.

    Notes are the only durable record of what actually arrived, so everything
    extracted goes here even when it is not promoted to a column. A shared
    ``careers@`` address or a second recruiter on the Cc line is context worth
    keeping and not somebody to phone, and the distinction is the point: columns
    are for "who do I reply to", the note is for "what did this say".
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"[{timestamp} | {category}]", f"Subject: {subject}"]

    if sender:
        lines.append(f"From: {sender}")

    contact_bits = [bit for bit in (contact_name, contact_email, contact_phone) if bit]
    if contact_bits:
        lines.append("Contact: " + " · ".join(contact_bits))

    if mentioned_emails:
        lines.append("Also mentioned: " + ", ".join(mentioned_emails[:5]))

    if next_step:
        lines.append(f"Next step: {next_step}")

    if deadline:
        lines.append(f"Deadline: {deadline}")

    lines.append(f"AI Note: {reasoning}")

    return "\n".join(lines)


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
    contact_phone: str = "",
    sender: str = "",
    mentioned_emails: tuple[str, ...] = (),
    next_step: str = "",
    deadline: str = "",
    email_date: str = "",
    link: str = "",
) -> str:
    """Apply an AI-classified email to the workspace.

    Returns ``"updated"`` when an existing application moved forward,
    ``"repeat"`` when it was already at that stage and this is a further
    message about it (a second interview round), ``"noted"`` when the email was
    recorded but the status was left alone (a backwards move), or ``"created"``
    when a new application was tracked.

    ``email_date`` (``YYYY-MM-DD``) dates a row this email *creates*. It is the
    earliest evidence the application exists, which beats the date the sync
    happened to run — an inbox scanned today can be full of confirmations from
    three weeks ago, and recording all of them as "applied today" makes the
    follow-up clock start from the wrong end.

    ``link`` is where to click through to. It comes from an email body, so it
    has already been through ``posting.is_job_link``; see the allowlist there
    for why nothing else is accepted.

    The contact is recorded when it looks like a person — see ``contacts.py``
    for how one is chosen. ``last_contact_at`` moves either way: an automated
    "we received your application" is still the employer making contact, and
    that is what the follow-up view measures silence against.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        raise ValueError("Company name is required.")

    category = _validate_status(category)
    role = (role or "").strip()

    contact_email = (contact_email or "").strip()
    contact_name = (contact_name or "").strip()
    contact_phone = (contact_phone or "").strip()

    # One rule, applied to the name and the number alike: details that arrive
    # attached to an identified person overwrite what is on the record, and
    # details from an unattributed message only fill a blank. NULL rather than
    # "" throughout, so the COALESCE chains below can express that.
    #
    # Without the second tier, the display name off a later no-reply relay
    # ("Acme Recruiting") would overwrite the human whose address is still
    # stored, leaving a name and an address that belong to different people.
    if is_replyable(contact_email):
        new_contact_email = contact_email
        named_contact = contact_name or contact_email
        named_phone = contact_phone or None
        loose_name = None
        loose_phone = None
    else:
        new_contact_email = None
        named_contact = None
        named_phone = None
        # Still worth keeping while nobody better is known: "Priya in Acme
        # Talent said X" beats an empty field on an application gone quiet, and
        # an ATS relay carries no replyable address at all while still signing
        # off with the recruiter's direct line.
        loose_name = contact_name or None
        loose_phone = contact_phone or None

    note = compose_email_note(
        category,
        subject,
        reasoning,
        sender=sender,
        contact_name=named_contact or loose_name or "",
        contact_email=new_contact_email or "",
        contact_phone=named_phone or loose_phone or "",
        mentioned_emails=tuple(mentioned_emails),
        next_step=next_step,
        deadline=deadline,
    )
    now = _utcnow()

    # Shared by every update path below, including the duplicate-row fallback,
    # so an email applies its contact the same way however it finds its
    # application. SQLite evaluates each SET expression against the pre-update
    # row, so `contact_email IS NULL` reads as "nobody was known before this
    # email" — which is precisely the condition the loose tier wants.
    contact_update = (
        "contact_name = COALESCE(?, "
        "CASE WHEN contact_email IS NULL THEN ? END, contact_name), "
        "contact_email = COALESCE(?, contact_email), "
        "contact_phone = COALESCE(?, "
        "CASE WHEN contact_email IS NULL THEN ? END, contact_phone), "
        # Fills a blank only. A row saved from the posting already has the real
        # URL; a link out of an email is the fallback for rows the tracker only
        # ever learned about by mail, and must never displace the better one.
        "link = CASE WHEN COALESCE(link, '') = '' THEN ? ELSE link END, "
        "last_contact_at = ?"
    )
    contact_values = (
        named_contact,
        loose_name,
        new_contact_email,
        named_phone,
        loose_phone,
        link if posting.is_job_link(link) else "",
        now,
    )

    with connect(db_path) as conn:
        row = _find_job_for_email(conn, company_name, role)

        if row:
            existing = row["notes"] or ""
            combined = f"{existing}\n\n{note}".strip()
            previous = row["status"]
            moves_forward = advances(previous, category)
            # A further message about a stage the application is already at:
            # round two of an interview, a rescheduled assessment, a second
            # "we need one more document". Nothing about jobs.status changes,
            # which is exactly why it used to disappear without trace.
            repeats_stage = previous == category

            if moves_forward and not repeats_stage:
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

            # Recorded whether or not the status moved, which is what the
            # status_history table has always claimed to hold. Gating this on a
            # *change* meant a second interview round left no record anywhere a
            # user looks: the stage was already INTERVIEW, so the timeline and
            # the recent-changes list both stayed exactly as they were and the
            # sync looked like it had missed the email.
            _record_transition(
                conn, row["id"], previous, category,
                applied=moves_forward,
                source="Email Sync",
                reason=subject,
            )

            if repeats_stage:
                logger.info(
                    "Email is a further %s update for %s / %s",
                    category, company_name, row["role"],
                )
                return "repeat"

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
                    company, role, status, date_applied, link, notes, source,
                    contact_name, contact_email, contact_phone, last_contact_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_name,
                    role or "Unknown Role",
                    category,
                    email_date or _today(),
                    link if posting.is_job_link(link) else "",
                    note,
                    "Email Sync",
                    # A brand-new row has nobody on it, so both tiers apply.
                    named_contact or loose_name,
                    new_contact_email,
                    named_phone or loose_phone,
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
                f"UPDATE jobs SET notes = ?, updated_at = ?, {contact_update} "
                "WHERE id = ?",
                (
                    f"{existing_row['notes'] or ''}\n\n{note}".strip(),
                    now,
                    *contact_values,
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
                   j.date_applied, j.contact_name, j.contact_email, j.contact_phone,
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


def company_for_role(role: str, *, db_path) -> str:
    """The employer of the one live application for ``role``, or "".

    An interview invitation routinely names no employer at all: "Your interview
    for Data Scientist has been scheduled", a time, and a meeting link. When HR
    writes from a personal address the sending domain names no employer either,
    so the email used to be dropped for having no company — the round was
    arranged and the tracker never heard about it.

    Only a single live application for that title is accepted. Two open
    applications for "Data Scientist" and there is no way to tell which was
    scheduled, and attaching it to the wrong one is worse than skipping it. An
    exact title wins outright; containment ("Senior Data Scientist" against a
    tracked "Data Scientist") is tried only if no title matches exactly, and
    only when it too is unambiguous.
    """
    role = (role or "").strip()

    if not role:
        return ""

    with connect(db_path) as conn:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        rows = conn.execute(
            f"SELECT company, role FROM jobs WHERE status IN ({placeholders})",
            tuple(sorted(ACTIVE_STATUSES)),
        ).fetchall()

    wanted = role.lower()
    exact = [row for row in rows if (row["role"] or "").lower() == wanted]

    if len(exact) == 1:
        return exact[0]["company"]
    if exact:
        return ""

    loose = [
        row
        for row in rows
        # A blank tracked role would be "contained" in every title there is.
        if (row["role"] or "").strip()
        and ((row["role"].lower() in wanted) or (wanted in row["role"].lower()))
    ]

    return loose[0]["company"] if len(loose) == 1 else ""


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
