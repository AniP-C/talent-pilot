"""Inbox -> tracker pipeline: fetch, classify, and record recruiter emails."""

import time
from typing import Callable, Optional

import db
import utils
import workspace
from ai.email_classifier import classify_email, to_status
from config import GMAIL_THROTTLE_SECONDS, logger
from integrations.gmail_client import fetch_job_emails


def sync_inbox_to_db(
    user_id: int,
    progress_callback: Optional[Callable[[str], None]] = None,
    throttle_seconds: float = GMAIL_THROTTLE_SECONDS,
) -> dict:
    """Classify a user's recent recruiter mail into their workspace.

    Returns a summary dict with counts. ``progress_callback`` receives a short
    status line per email so the UI can show live progress.
    """
    db_path = workspace.jobs_db_path(user_id)
    db.create_table(db_path)

    def report(message: str) -> None:
        logger.info(message)
        if progress_callback:
            progress_callback(message)

    report("Fetching recent emails…")
    emails = fetch_job_emails(user_id)

    summary = {"fetched": len(emails), "updated": 0, "created": 0, "skipped": 0, "failed": 0}

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
    summary["skipped"] += len(emails) - len(pending)

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
        )

        if "error" in result:
            logger.error("Classification failed for %s: %s", email["id"], result["error"])
            summary["failed"] += 1
            continue

        status = to_status(result.get("category", ""))
        company = (result.get("company_name") or "").strip()

        # UNKNOWN categories and unidentifiable senders are noise, but they are
        # still marked processed so the next sync does not pay to classify them
        # a second time.
        if status is None or not company or company.lower() == "unknown":
            db.mark_email_processed(email["id"], db_path=db_path)
            summary["skipped"] += 1
            continue

        try:
            outcome = db.update_job_from_email(
                company_name=company,
                category=status,
                subject=email["subject"],
                reasoning=result.get("reasoning", ""),
                db_path=db_path,
            )
            db.mark_email_processed(email["id"], db_path=db_path)
            summary[outcome] += 1
        except Exception as exc:  # noqa: BLE001 - one bad email must not stop the run
            logger.error("Could not record email %s: %s", email["id"], exc)
            summary["failed"] += 1

    utils.update_last_sync(user_id)
    report(
        f"Sync complete: {summary['updated']} updated, {summary['created']} created, "
        f"{summary['skipped']} skipped, {summary['failed']} failed."
    )
    return summary
