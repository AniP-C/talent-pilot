"""Inbox -> tracker pipeline: fetch, classify, and record recruiter emails."""

import time
import uuid
from typing import Callable, Optional

import db
import utils
import workspace
from ai.email_classifier import MIN_CONFIDENCE, classify_email, resolve_company, to_status
from config import GMAIL_THROTTLE_SECONDS, sync_logger as logger
from integrations.gmail_client import fetch_job_emails


def sync_inbox_to_db(
    user_id: int,
    progress_callback: Optional[Callable[[str], None]] = None,
    throttle_seconds: float = GMAIL_THROTTLE_SECONDS,
) -> dict:
    """Classify a user's recent recruiter mail into their workspace.

    Emails arrive oldest-first from ``fetch_job_emails`` and are applied in
    that order, so an application ends on its most recent state.

    Returns a summary dict with counts. ``progress_callback`` receives a short
    status line per email so the UI can show live progress.
    """
    # One id for the whole run, stamped on every line it writes. Without it,
    # two overlapping syncs interleave in the log and neither can be followed.
    run_id = uuid.uuid4().hex[:8]

    db_path = workspace.jobs_db_path(user_id)
    db.create_table(db_path)

    def report(message: str) -> None:
        logger.info("[sync %s] %s", run_id, message)
        if progress_callback:
            progress_callback(message)

    def decision(message: str, *args) -> None:
        """Log why one email was or was not acted on.

        These lines are the only record of an automated decision. Without
        them, "why did this say Rejected?" has no answer after the fact.
        """
        logger.info("[sync %s] " + message, run_id, *args)

    logger.info("[sync %s] Starting inbox sync for user %s", run_id, user_id)
    report("Fetching recent emails…")

    emails = fetch_job_emails(user_id)

    summary = {
        "fetched": len(emails),
        "updated": 0,
        "created": 0,
        "noted": 0,
        "skipped": 0,
        "failed": 0,
        "needs_review": 0,
        "run_id": run_id,
    }

    if not emails:
        report("No new job emails to process.")
        utils.update_last_sync(user_id)
        return summary

    # Messages classified on a previous run are skipped before any AI call, so
    # a repeat sync costs nothing and cannot duplicate notes.
    pending = [
        email
        for email in emails
        if not db.is_email_processed(email["id"], db_path=db_path)
    ]
    already_done = len(emails) - len(pending)
    summary["skipped"] += already_done

    if already_done:
        decision("%s email(s) were already processed on an earlier run", already_done)

    if not pending:
        report(f"All {len(emails)} emails were already processed.")
        utils.update_last_sync(user_id)
        return summary

    report(f"Classifying {len(pending)} new emails…")

    for index, email in enumerate(pending):
        # Throttle *between* calls, not after the last one, so a single-email
        # sync does not sit idle at the end.
        if index > 0 and throttle_seconds > 0:
            time.sleep(throttle_seconds)

        report(f"[{index + 1}/{len(pending)}] {email['subject'][:60]}")

        result = classify_email(
            sender=email["sender"],
            subject=email["subject"],
            snippet=email["snippet"],
            body=email.get("body", ""),
        )

        if "error" in result:
            logger.error(
                "[sync %s] Classification failed for %s: %s",
                run_id, email["id"], result["error"],
            )
            summary["failed"] += 1
            # Deliberately NOT marked processed: a quota or network failure is
            # transient, and marking it would mean the email is never retried.
            continue

        status = to_status(result.get("category", ""))
        company = resolve_company(result, email["sender"])
        role = (result.get("role_title") or "").strip()
        confidence = float(result.get("confidence", 1.0) or 0.0)

        # Every skip reason is logged individually. "Skipped" as a bare number
        # is not actionable; knowing it was low confidence versus an unnamed
        # company tells you which part to improve.
        skip_reason = None

        if status is None:
            skip_reason = f"category {result.get('category')!r} is not a tracker status"
        elif not result.get("is_my_application", True):
            skip_reason = "not an update on an application this user submitted"
        elif result.get("is_suspicious", False):
            skip_reason = "flagged as phishing or a scam"
            summary["needs_review"] += 1
        elif not company:
            skip_reason = "no usable company name in the email or sender domain"
        elif confidence < MIN_CONFIDENCE:
            skip_reason = f"confidence {confidence:.2f} below {MIN_CONFIDENCE}"
            summary["needs_review"] += 1

        if skip_reason:
            decision(
                "SKIP  %s | from=%r subject=%r | %s",
                email["id"], email["sender"][:60], email["subject"][:60], skip_reason,
            )
            # Still marked processed so the next sync does not pay to
            # classify the same message again.
            db.mark_email_processed(email["id"], db_path=db_path)
            summary["skipped"] += 1
            continue

        try:
            outcome = db.update_job_from_email(
                company_name=company,
                category=status,
                subject=email["subject"],
                reasoning=result.get("reasoning", ""),
                role=role,
                db_path=db_path,
            )
            db.mark_email_processed(email["id"], db_path=db_path)
            summary[outcome] += 1

            decision(
                "%-7s %s | %s / %s -> %s (confidence %.2f)",
                outcome.upper(), email["id"], company, role or "Unknown Role",
                status, confidence,
            )
        except Exception as exc:  # noqa: BLE001 - one bad email must not stop the run
            logger.error(
                "[sync %s] Could not record email %s (%s / %s): %s",
                run_id, email["id"], company, role, exc,
            )
            summary["failed"] += 1

    utils.update_last_sync(user_id)
    report(
        f"Sync complete: {summary['updated']} updated, {summary['created']} created, "
        f"{summary['noted']} noted, {summary['skipped']} skipped, "
        f"{summary['failed']} failed."
    )
    logger.info(
        "[sync %s] Finished for user %s: %s", run_id, user_id, summary
    )
    return summary
