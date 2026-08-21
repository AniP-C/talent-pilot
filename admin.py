"""Administration: who the accounts are, and everything done to them.

Every operation here already existed as a command in deploy/OPERATIONS.md —
listing accounts, revoking sessions, clearing a lockout, deleting somebody and
their workspace. Each one meant an SSH session, a remembered sqlite3
invocation, and a DELETE typed by hand against production with no undo and no
record that it happened. Two of those are where the accidents live: a mistyped
WHERE clause removes the wrong account, and afterwards nothing anywhere says
who did it or when.

So this module is not only a convenience layer over those commands. It is
where they become *recorded* — every state-changing call writes a row to
``admin_audit`` naming the administrator, the action, and the account it
touched. admin_ui.py renders all of this; nothing in here imports Streamlit, so
the operations stay testable and could be driven from a script if the
dashboard were ever the thing that was broken.

The one thing deliberately absent is any way to grant administrator rights.
That is decided by ADMIN_EMAILS in the environment file — see config.py for
why a database flag would be the wrong shape.
"""

import json
import shutil
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterator, Optional

import auth
import db
import usage
import workspace
from config import ADMIN_EMAILS, USERS_DB_PATH, WORKSPACES_DIR, logger

# The audit vocabulary. Fixed for the same reason usage.EVENTS is fixed: two
# callers spelling one action differently makes the trail unreadable, and an
# unreadable trail is the same as no trail.
ACTION_EMAIL_CHANGED = "account.email"
ACTION_PASSWORD_RESET = "account.password"
ACTION_RECOVERY_ISSUED = "account.recovery"
ACTION_SESSIONS_REVOKED = "account.sessions"
ACTION_LOCKOUT_CLEARED = "account.lockout"
ACTION_GMAIL_DISCONNECTED = "account.gmail"
ACTION_EXPORTED = "account.export"
ACTION_DELETED = "account.delete"
ACTION_SERVICE_RESTARTED = "server.restart"
ACTION_SETTING_CHANGED = "server.setting"
ACTION_BACKUP_TAKEN = "server.backup"

_SCHEMA = """
-- What an administrator did, to whom, and when.
--
-- ``target`` is text rather than a foreign key on purpose: the most important
-- row this table will ever hold is the deletion of an account, and a foreign
-- key would either block that delete or cascade the evidence away with it.
CREATE TABLE IF NOT EXISTS admin_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON admin_audit(occurred_at);
"""


@contextmanager
def _connect(db_path=None) -> Iterator[sqlite3.Connection]:
    """Open the accounts database with foreign keys and the audit table ready.

    ``PRAGMA foreign_keys`` is not decoration. Deleting a user is expected to
    take their tokens, recovery codes, handoff codes and usage events with it,
    and every one of those cascades is declared in the schema but honoured only
    by a connection that has asked for them.
    """
    path = Path(db_path or USERS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # Created here rather than in auth.init_db, following usage.py, so this
        # module owns its own schema and can be added to a running deployment
        # without a migration step.
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =====================================================================
# WHO IS AN ADMINISTRATOR
# =====================================================================
def is_admin(email: str) -> bool:
    """Whether this address may open the panel.

    Compared against the normalized address, so the answer does not depend on
    how the person happened to capitalise their email when registering.
    """
    return auth.normalize_email(email) in ADMIN_EMAILS


def configured() -> bool:
    """Whether any administrator has been named at all.

    An instance with an empty ADMIN_EMAILS has no panel, and that is the right
    default: the setting has to be added deliberately, on the server.
    """
    return bool(ADMIN_EMAILS)


# =====================================================================
# THE AUDIT TRAIL
# =====================================================================
def record(
    actor: str, action: str, *, target: str = "", detail: str = "", db_path=None
) -> None:
    """Write one administrative action to the trail. Never raises.

    Swallowing failures follows usage.record, for a sharper reason: an audit
    row that cannot be written must not turn a completed deletion into an
    exception the operator reads as "it did not work", because the deletion
    already happened and the obvious response is to try it again. The loss is
    logged loudly instead.
    """
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO admin_audit (occurred_at, actor, action, target, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_utcnow(), actor or "unknown", action, str(target), str(detail)),
            )
    except Exception as exc:  # noqa: BLE001 - the action itself already happened
        logger.error("Could not record admin action %s by %s: %s", action, actor, exc)

    logger.info("ADMIN %s by %s target=%s %s", action, actor, target, detail)


def audit_trail(limit: int = 100, db_path=None) -> list[dict]:
    """The most recent administrative actions, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT occurred_at, actor, action, target, detail "
            "FROM admin_audit ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()

    return [dict(row) for row in rows]


# =====================================================================
# READING ACCOUNTS
# =====================================================================
def accounts(days: Optional[int] = None, db_path=None) -> list[dict]:
    """Every account, with what it has used and what it holds on disk.

    Built on usage.per_user so the panel and deploy/usage_report.py cannot
    disagree — there is one definition of "paid units" and both read it.

    ``days`` narrows the usage window without hiding anybody: an account that
    did nothing in the window still appears, with zeros. That is the point —
    "who has stopped using it" is as much a pricing question as "who uses it
    most".
    """
    rows = usage.per_user(days=days, db_path=db_path)

    with _connect(db_path) as conn:
        sessions = _counts(
            conn,
            "SELECT user_id, COUNT(*) AS n FROM api_tokens "
            "WHERE expires_at > ? GROUP BY user_id",
            (_utcnow(),),
        )
        recovery = _counts(
            conn,
            "SELECT user_id, COUNT(*) AS n FROM recovery_codes "
            "WHERE used_at IS NULL GROUP BY user_id",
        )

    for row in rows:
        user_id = row["user_id"]
        row["sessions"] = sessions.get(user_id, 0)
        row["recovery_left"] = recovery.get(user_id, 0)
        row["workspace"] = workspace_facts(user_id)

    return rows


def _counts(conn, sql: str, params: tuple = ()) -> dict[int, int]:
    """Run a ``user_id, n`` aggregate into a dictionary."""
    return {row["user_id"]: row["n"] for row in conn.execute(sql, params).fetchall()}


def workspace_facts(user_id: int) -> dict:
    """What one account holds on disk, without opening anything it need not.

    Read straight from the filesystem rather than through workspace.py's
    helpers, because those create the directories they are asked about — which
    would quietly conjure a workspace for an account that has never used the
    app, and make "signed up and never came back" impossible to see.
    """
    root = WORKSPACES_DIR / str(int(user_id))

    facts = {
        "path": str(root),
        "exists": root.is_dir(),
        "bytes": 0,
        "jobs": 0,
        "stats": None,
        "profiles": 0,
        "answers": 0,
        "gmail_connected": False,
        "last_sync": "",
    }

    if not facts["exists"]:
        return facts

    facts["bytes"] = sum(
        item.stat().st_size for item in root.rglob("*") if item.is_file()
    )
    facts["profiles"] = len(list((root / "profiles").glob("*.json")))
    facts["answers"] = len(list((root / "answers").glob("*.txt")))
    facts["gmail_connected"] = (root / "gmail_token.json").is_file()

    last_sync = root / "last_sync.txt"
    if last_sync.is_file():
        facts["last_sync"] = last_sync.read_text(
            encoding="utf-8", errors="replace"
        ).strip()

    jobs_db = root / "jobs.db"
    if jobs_db.is_file():
        try:
            facts["stats"] = db.get_stats(db_path=jobs_db)
            facts["jobs"] = facts["stats"]["total"]
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
            logger.warning("Could not read jobs.db for account %s: %s", user_id, exc)

    return facts


def account(user_id: int, db_path=None) -> Optional[dict]:
    """One account in full, or None if there is no such id."""
    for row in accounts(db_path=db_path):
        if row["user_id"] == int(user_id):
            return row

    return None


def jobs_for(user_id: int) -> list[dict]:
    """What one account is tracking.

    The most invasive read in this module, and the only one that touches a
    user's own content rather than their metering — so the panel puts it
    behind an explicit click instead of folding it into the account row.
    """
    jobs_db = WORKSPACES_DIR / str(int(user_id)) / "jobs.db"

    if not jobs_db.is_file():
        return []

    return db.get_all_jobs(db_path=jobs_db)


def summary(db_path=None) -> dict:
    """The numbers along the top of the panel."""
    rows = accounts(db_path=db_path)

    return {
        "accounts": len(rows),
        "active_7d": usage.active_accounts(days=7, db_path=db_path),
        "active_30d": usage.active_accounts(days=30, db_path=db_path),
        "never_returned": sum(1 for row in rows if not row["last_login_at"]),
        "paid_units": sum(row["paid_units"] for row in rows),
        "paid_units_30d": sum(
            row["paid_units"] for row in usage.per_user(days=30, db_path=db_path)
        ),
        "jobs": sum(row["workspace"]["jobs"] for row in rows),
        "bytes": sum(row["workspace"]["bytes"] for row in rows),
    }


# =====================================================================
# CHANGING ACCOUNTS
# =====================================================================
def change_email(actor: str, user_id: int, new_email: str, db_path=None) -> str:
    """Rename an account, recording who did it and what it was called before."""
    existing = auth.get_user(user_id, db_path=db_path)

    if existing is None:
        raise auth.AuthError("No account with that id.")

    updated = auth.set_email(user_id, new_email, db_path=db_path)

    record(
        actor,
        ACTION_EMAIL_CHANGED,
        target=str(user_id),
        detail=f"{existing.email} -> {updated}",
        db_path=db_path,
    )
    return updated


def reset_password(actor: str, user_id: int, new_password: str, db_path=None) -> None:
    """Set a new password on someone's behalf and sign their devices out."""
    user = auth.get_user(user_id, db_path=db_path)

    if user is None:
        raise auth.AuthError("No account with that id.")

    auth.set_password(user_id, new_password, db_path=db_path)

    # The password itself never reaches the trail — only the fact that it
    # changed. An audit log that records credentials is a credential store.
    record(
        actor,
        ACTION_PASSWORD_RESET,
        target=str(user_id),
        detail=f"{user.email}; sessions revoked",
        db_path=db_path,
    )


def issue_recovery_codes(actor: str, user_id: int, db_path=None) -> list[str]:
    """Issue a fresh set of recovery codes, replacing any unused ones."""
    user = auth.get_user(user_id, db_path=db_path)

    if user is None:
        raise auth.AuthError("No account with that id.")

    codes = auth.issue_recovery_codes(user_id, db_path=db_path)

    record(
        actor,
        ACTION_RECOVERY_ISSUED,
        target=str(user_id),
        detail=f"{user.email}; {len(codes)} codes",
        db_path=db_path,
    )
    return codes


def revoke_sessions(actor: str, user_id: int, db_path=None) -> int:
    """Sign every device out of an account. Returns how many were signed out."""
    user = auth.get_user(user_id, db_path=db_path)

    if user is None:
        raise auth.AuthError("No account with that id.")

    with _connect(db_path) as conn:
        revoked = conn.execute(
            "DELETE FROM api_tokens WHERE user_id = ?", (int(user_id),)
        ).rowcount
        # Handoff codes are one-use session transfers. Left behind, a device
        # that was just signed out could walk straight back in on a code
        # issued a minute earlier.
        conn.execute("DELETE FROM handoff_codes WHERE user_id = ?", (int(user_id),))

    record(
        actor,
        ACTION_SESSIONS_REVOKED,
        target=str(user_id),
        detail=f"{user.email}; {revoked} token(s)",
        db_path=db_path,
    )
    return revoked


def clear_lockouts(actor: str, email: str = "", db_path=None) -> int:
    """Clear failed sign-in attempts, for one address or for everyone.

    Both halves matter. Someone locked out of their own account needs their own
    record cleared; an instance where the limit has run away — a misbehaving
    extension retrying in a loop — needs the table emptied.
    """
    with _connect(db_path) as conn:
        if email:
            identifier = f"email:{auth.normalize_email(email)}"
            cleared = conn.execute(
                "DELETE FROM login_attempts WHERE identifier = ?", (identifier,)
            ).rowcount
        else:
            cleared = conn.execute("DELETE FROM login_attempts").rowcount

    record(
        actor,
        ACTION_LOCKOUT_CLEARED,
        target=email or "all",
        detail=f"{cleared} attempt(s)",
        db_path=db_path,
    )
    return cleared


def disconnect_gmail(actor: str, user_id: int, db_path=None) -> bool:
    """Delete an account's stored Gmail token, ending inbox sync for them.

    The fix when a token has gone bad — a revoked grant, a changed scope — and
    the user is left with syncs that fail for a reason no message explains.
    Deleting it is safe: the next connection re-consents from scratch.
    """
    user = auth.get_user(user_id, db_path=db_path)

    if user is None:
        raise auth.AuthError("No account with that id.")

    token = WORKSPACES_DIR / str(int(user_id)) / "gmail_token.json"
    existed = token.is_file()

    if existed:
        token.unlink()

    record(
        actor,
        ACTION_GMAIL_DISCONNECTED,
        target=str(user_id),
        detail=user.email if existed else f"{user.email}; nothing to remove",
        db_path=db_path,
    )
    return existed


def export_account(actor: str, user_id: int, db_path=None) -> tuple[str, bytes]:
    """Everything one account holds, as a zip, for handing back or keeping.

    Two uses: answering "send me my data", and taking a copy of somebody
    immediately before deleting them — which is the only version of that
    operation worth offering.

    ``gmail_token.json`` is deliberately left out. It is a live OAuth refresh
    token for the user's mailbox, and a file in the Downloads folder is exactly
    the wrong place for it: the export is a record of their data, not a
    transferable key to their inbox.
    """
    user = auth.get_user(user_id, db_path=db_path)

    if user is None:
        raise auth.AuthError("No account with that id.")

    row = account(user_id, db_path=db_path) or {}
    root = WORKSPACES_DIR / str(int(user_id))

    buffer = BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "account.json",
            json.dumps(
                {
                    "id": user.id,
                    "email": user.email,
                    "created_at": user.created_at,
                    "last_login_at": row.get("last_login_at"),
                    "usage": row.get("events", {}),
                    "paid_units": row.get("paid_units", 0),
                    "exported_at": _utcnow(),
                },
                indent=2,
            ),
        )
        archive.writestr(
            "jobs.json", json.dumps(jobs_for(user_id), indent=2, default=str)
        )

        if root.is_dir():
            for item in sorted(root.rglob("*")):
                if item.is_file() and item.name != "gmail_token.json":
                    archive.write(item, arcname=str(item.relative_to(root)))

    record(
        actor, ACTION_EXPORTED, target=str(user_id), detail=user.email, db_path=db_path
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"talent-pilot-account-{user_id}-{stamp}.zip", buffer.getvalue()


def delete_account(actor: str, user_id: int, db_path=None) -> dict:
    """Remove an account and everything it owns. Not reversible.

    The database row goes first and the files second. If the process dies
    between the two, what is left is an orphaned directory: inert, invisible to
    the app, removable later. The other order would leave an account that can
    sign in to a workspace which no longer exists, and that is a bug report
    rather than a bit of litter.
    """
    user = auth.get_user(user_id, db_path=db_path)

    if user is None:
        raise auth.AuthError("No account with that id.")

    facts = workspace_facts(user_id)

    with _connect(db_path) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (int(user.id),))
        # login_attempts is keyed by address, not by user id, so no cascade
        # reaches it. Left behind, it would lock out the next person to
        # register with that address.
        conn.execute(
            "DELETE FROM login_attempts WHERE identifier = ?", (f"email:{user.email}",)
        )

    removed = _remove_workspace(user.id)

    record(
        actor,
        ACTION_DELETED,
        target=str(user.id),
        detail=f"{user.email}; {facts['jobs']} job(s); {facts['bytes']} bytes",
        db_path=db_path,
    )

    return {
        "email": user.email,
        "jobs": facts["jobs"],
        "bytes": facts["bytes"],
        "workspace_removed": removed,
    }


def _remove_workspace(user_id: int) -> bool:
    """Delete one workspace directory, refusing anything outside the root.

    The id has been through ``int()`` long before it arrives, so this is belt
    and braces — but the operation is ``rmtree`` against a path built from a
    value that entered the process over HTTP, and that is the one place where
    belt and braces is the correct amount of caution.
    """
    root = WORKSPACES_DIR.resolve()
    target = (root / str(int(user_id))).resolve()

    if target == root or root not in target.parents:
        raise workspace.UnsafePathError(f"Refusing to delete {target}")

    if not target.is_dir():
        return False

    shutil.rmtree(target)
    return True


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
