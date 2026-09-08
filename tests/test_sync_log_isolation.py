"""The inbox sync log must never leak one account's mail into another's.

Sync lines quote real company names and email subjects, so each user's Activity
tab reads only their own per-user log. This guards the fix for the leak where a
single shared ``sync.log`` was shown to every account.
"""

import sync_controller as sc
import workspace
from config import sync_logger


def test_runs_are_isolated_even_when_interleaved():
    """Two users syncing at once each capture only their own run's lines."""
    handler_a = sc._attach_user_sync_log(101, "aaaa1111")
    handler_b = sc._attach_user_sync_log(202, "bbbb2222")
    try:
        # Emitted exactly as the sync does: every line tagged with its run id,
        # and interleaved to mimic two concurrent runs on the shared logger.
        sync_logger.info("[sync %s] CREATED Acme / Engineer -> APPLIED", "aaaa1111")
        sync_logger.info("[sync %s] SKIP recruiter spam from naukri", "bbbb2222")
        sync_logger.info("[sync %s] REPEAT Acme / Engineer -> INTERVIEW", "aaaa1111")
    finally:
        sc._detach_user_sync_log(handler_a)
        sc._detach_user_sync_log(handler_b)

    a = workspace.sync_log_path(101).read_text(encoding="utf-8")
    b = workspace.sync_log_path(202).read_text(encoding="utf-8")

    # Each file has its own lines...
    assert "Acme" in a and "REPEAT" in a
    assert "naukri" in b
    # ...and none of the other user's.
    assert "naukri" not in a
    assert "Acme" not in b


def test_untagged_lines_reach_no_user_file():
    """A line without a run tag belongs to the shared ops log only."""
    handler = sc._attach_user_sync_log(303, "cccc3333")
    try:
        sync_logger.info("a stray line with no run tag")
        sync_logger.info("[sync %s] CREATED Beta / Dev -> APPLIED", "cccc3333")
    finally:
        sc._detach_user_sync_log(handler)

    text = workspace.sync_log_path(303).read_text(encoding="utf-8")
    assert "Beta" in text
    assert "stray line" not in text


def test_detach_leaves_the_shared_logger_clean():
    """No per-user handler lingers on the module logger after a run ends."""
    before = list(sync_logger.handlers)
    handler = sc._attach_user_sync_log(404, "dddd4444")
    assert handler in sync_logger.handlers
    sc._detach_user_sync_log(handler)
    assert list(sync_logger.handlers) == before


# =====================================================================
# WHAT THE BOUNCER THREW AWAY
#
# The rule filter runs before any model call and used to drop in silence. Its
# one log line went to the shared ops log untagged, so `_RunFilter` could not
# pass it into a user's file even in principle — the counts existed and were
# unreachable. These pin the path from a dropped message to the Activity tab.
# =====================================================================
def test_a_dropped_message_reaches_the_users_own_log(monkeypatch, tmp_path):
    from integrations.gmail_client import Scan

    monkeypatch.setattr(sc, "mailbox_address", lambda user_id: "me@example.com")
    monkeypatch.setattr(
        sc,
        "fetch_job_emails",
        lambda user_id, own_address="": Scan(
            emails=[],
            dropped=[
                {
                    "id": "abc123",
                    "sender": "Aarya from foundit <jobmessenger@monsterindia.com>",
                    "subject": "2 jobs that you haven't applied yet",
                    "reason": "blocked word 'job alert'",
                }
            ],
        ),
    )

    summary = sc.sync_inbox_to_db(505)
    text = workspace.sync_log_path(505).read_text(encoding="utf-8")

    assert "DROPPED abc123" in text
    assert "monsterindia.com" in text
    assert "blocked word 'job alert'" in text
    # And the counts the summary reports, so the sidebar can say it too.
    assert summary["dropped"] == 1
    assert summary["considered"] == 1
    assert summary["fetched"] == 0


def test_a_clean_scan_says_nothing_about_the_bouncer(monkeypatch):
    """No drops, no noise. The line only appears when it has something to say."""
    from integrations.gmail_client import Scan

    monkeypatch.setattr(sc, "mailbox_address", lambda user_id: "")
    monkeypatch.setattr(
        sc, "fetch_job_emails", lambda user_id, own_address="": Scan([], [])
    )

    summary = sc.sync_inbox_to_db(606)
    text = workspace.sync_log_path(606).read_text(encoding="utf-8")

    assert "DROPPED" not in text
    assert "Bouncer:" not in text
    assert summary["dropped"] == 0
