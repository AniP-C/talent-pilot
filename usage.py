"""Per-account usage metering: who is using what, and how much.

Written to the central accounts database rather than to a workspace, because
the question this exists to answer — "which accounts are worth charging, and
for what?" — is asked across all of them at once. A per-workspace table would
mean opening every user's database to answer it.

What it is for: the AI calls in this app cost real money per request, and they
are not evenly distributed. One person analysing forty job descriptions a week
costs more than fifty people who signed up and uploaded a CV once. Before there
was any way to see that, a price could only be guessed at.

What it deliberately does not record: no job descriptions, no resume content,
no company names, no email subjects. An event is an account id, a verb, a
count, and a timestamp. That is enough to bill against and not enough to be a
second copy of the user's data — which matters because this table, unlike a
workspace, spans every account.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from config import USERS_DB_PATH, logger

# The vocabulary. Recording arbitrary strings would make the report unreadable
# within a month — one caller writes "analyze", another "analyse", and neither
# total is right.
#
# The comment on each line is what it costs, because that is the reason to
# count it. "Free" means no model call: worth counting as engagement, not as
# spend.
ANALYZE_JD = "jd.analyze"          # one large model call (JD + resume)
RESUME_UPLOAD = "resume.upload"    # one large model call (whole PDF)
ANSWER_DRAFT = "answer.draft"      # one model call per drafted answer
EMAIL_SYNC = "email.sync"          # one model call per classified email
KEYWORD_SCAN = "keyword.scan"      # free: deterministic, no network
JOB_SAVE = "job.save"              # free
SIGN_IN = "auth.signin"            # free, but it is how "active" is measured

EVENTS = (
    ANALYZE_JD,
    RESUME_UPLOAD,
    ANSWER_DRAFT,
    EMAIL_SYNC,
    KEYWORD_SCAN,
    JOB_SAVE,
    SIGN_IN,
)

# Which events cost money, for the "billable" column of the report.
PAID_EVENTS = (ANALYZE_JD, RESUME_UPLOAD, ANSWER_DRAFT, EMAIL_SYNC)

# Where the action came from. Worth separating: if the extension drives most of
# the paid calls, pricing the dashboard alone would miss the cost entirely.
SOURCES = ("dashboard", "extension", "api", "sync")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    event       TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'dashboard',
    -- How many units the one action consumed. Nearly always 1; an inbox sync
    -- classifies a batch, and counting that as a single event would understate
    -- the most expensive thing the app does by an order of magnitude.
    quantity    INTEGER NOT NULL DEFAULT 1,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_event ON usage_events(event, occurred_at);
"""


@contextmanager
def _connect(db_path=None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or USERS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        # Created here rather than in auth.init_db so this module owns its own
        # schema and can be added to a running deployment without a migration
        # step. IF NOT EXISTS on an existing table is a no-op against the
        # schema cache, which is far cheaper than the model call whose cost it
        # is recording.
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record(
    user_id: int,
    event: str,
    *,
    source: str = "dashboard",
    quantity: int = 1,
    db_path=None,
) -> None:
    """Record one billable or notable action. Never raises.

    Metering is bookkeeping, not the job. A locked database or a full disk must
    not turn a successful analysis into an error the user sees, so every
    failure here is logged and swallowed — the report loses a row, the user
    loses nothing.
    """
    if event not in EVENTS:
        # A typo would otherwise create a silent second category that no
        # report ever shows.
        logger.warning("Refusing to record unknown usage event %r", event)
        return

    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO usage_events (user_id, event, source, quantity, occurred_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    event,
                    source if source in SOURCES else "api",
                    max(0, int(quantity)),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception as exc:  # noqa: BLE001 - metering must never break a request
        logger.error("Could not record usage %s for user %s: %s", event, user_id, exc)


# =====================================================================
# REPORTS
# =====================================================================
def _since_clause(days: Optional[int]) -> tuple[str, tuple]:
    """A WHERE fragment limiting to the last ``days``, or nothing at all."""
    if not days:
        return "", ()

    cutoff = datetime.now(timezone.utc).timestamp() - int(days) * 86400
    return " AND occurred_at >= ?", (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),)


def per_user(days: Optional[int] = None, db_path=None) -> list[dict]:
    """One row per account: who they are, and what they have used.

    Left-joined from ``users`` so an account that has never done anything still
    appears with zeros — the accounts that signed up and never came back are
    exactly as interesting for pricing as the heavy ones.
    """
    window, params = _since_clause(days)

    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT u.id, u.email, u.created_at, u.last_login_at,
                   e.event, COALESCE(SUM(e.quantity), 0) AS units
            FROM users u
            LEFT JOIN usage_events e
                   ON e.user_id = u.id{window}
            GROUP BY u.id, e.event
            ORDER BY u.id
            """,
            params,
        ).fetchall()

    accounts: dict[int, dict] = {}

    for row in rows:
        account = accounts.setdefault(
            row["id"],
            {
                "user_id": row["id"],
                "email": row["email"],
                "created_at": row["created_at"],
                "last_login_at": row["last_login_at"],
                "events": {name: 0 for name in EVENTS},
                "paid_units": 0,
            },
        )

        # NULL event means the LEFT JOIN found nothing — an account with no
        # activity, not an activity with no name.
        if row["event"] in account["events"]:
            account["events"][row["event"]] = row["units"]
            if row["event"] in PAID_EVENTS:
                account["paid_units"] += row["units"]

    return sorted(
        accounts.values(), key=lambda a: (-a["paid_units"], a["user_id"])
    )


def totals(days: Optional[int] = None, db_path=None) -> dict:
    """Units per event across every account."""
    window, params = _since_clause(days)

    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT event, SUM(quantity) AS units FROM usage_events "
            f"WHERE 1 = 1{window} GROUP BY event",
            params,
        ).fetchall()

    counted = {name: 0 for name in EVENTS}
    for row in rows:
        if row["event"] in counted:
            counted[row["event"]] = row["units"]

    return counted


def recent(limit: int = 50, db_path=None) -> list[dict]:
    """The newest events, newest first, named by account."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT e.occurred_at, u.email, e.event, e.source, e.quantity
            FROM usage_events e
            JOIN users u ON u.id = e.user_id
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    return [dict(row) for row in rows]


def active_accounts(days: int = 30, db_path=None) -> int:
    """How many accounts did anything at all in the window.

    "How many people use it" is not the number of rows in ``users`` — that
    counts everyone who ever signed up. This is the number that moves.
    """
    window, params = _since_clause(days)

    with _connect(db_path) as conn:
        return conn.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM usage_events WHERE 1 = 1{window}",
            params,
        ).fetchone()[0]
