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
