"""Inbox -> tracker pipeline: fetch, classify, and record recruiter emails."""

import time
import uuid
from datetime import datetime
from typing import Callable, Optional

import contacts
import db
import posting
import utils
import workspace
from ai.email_classifier import MIN_CONFIDENCE, classify_email, resolve_company, to_status
from config import GMAIL_THROTTLE_SECONDS, sync_logger as logger
from integrations.gmail_client import fetch_job_emails, mailbox_address


def _email_date(email: dict) -> str:
    """The day an email arrived, as ``YYYY-MM-DD``, or "" if Gmail said nothing.

    Gmail's ``internalDate`` is epoch milliseconds. Converted in local time
    because every other date the tracker stores is a local calendar date, and a
    UTC one would show as the previous day for anyone east of Greenwich for
    several hours a night.
    """
    stamp = email.get("internal_date") or 0

    # Zero is "Gmail told us nothing", not 1 January 1970 — and a row dated 1970
    # would sit at the top of the follow-up list forever.
    if not stamp:
        return ""

    try:
        return datetime.fromtimestamp(int(stamp) / 1000).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


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
        # How many applications came away with somebody to reply to. Worth
        # counting separately: a sync that changed no statuses can still have
        # been the run that found the recruiter.
        "contacts": 0,
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

    # Read once per run, not once per email. Needed so the user's own address
    # never becomes the recruiter contact — it is in To, in Cc, and quoted in
    # the body of nearly every application confirmation.
    own_address = mailbox_address(user_id)

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
            reply_to=email.get("reply_to", ""),
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
        company = resolve_company(result, email["sender"], email.get("reply_to", ""))
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

        # Who to reply to. The From header used to be the only source, which
        # meant an ATS relay ("no-reply@greenhouse.io") left the application
        # with no contact at all even when the mail set a Reply-To and signed
        # off with a direct line. See contacts.py for the ordering.
        body = f"{email.get('snippet', '')}\n{email.get('body', '')}"

        contact = contacts.choose_contact(
            sender=email["sender"],
            reply_to=email.get("reply_to", ""),
            body=body,
            suggested_name=result.get("recruiter_name", ""),
            suggested_email=result.get("recruiter_email", ""),
            suggested_phone=result.get("recruiter_phone", ""),
            # Never record the user as their own recruiter.
            exclude=(own_address,),
        )

        try:
            outcome = db.update_job_from_email(
                company_name=company,
                category=status,
                subject=email["subject"],
                reasoning=result.get("reasoning", ""),
                role=role,
                contact_name=contact["name"],
                contact_email=contact["email"],
                contact_phone=contact["phone"],
                sender=email["sender"],
                mentioned_emails=tuple(contact["mentioned"]),
                next_step=result.get("next_step", ""),
                deadline=result.get("deadline", ""),
                # The date the email arrived, not the date the sync ran. An
                # inbox scanned today is full of confirmations from weeks ago,
                # and dating them all "today" starts the follow-up clock at the
                # wrong end for every one of them.
                email_date=_email_date(email),
                # Somewhere to click through to, for an application the tracker
                # only ever learned about by mail. Allowlisted to job boards and
                # ATS vendors — see posting.JOB_LINK_HOSTS for why.
                link=posting.find_job_link(body),
                db_path=db_path,
            )
            db.mark_email_processed(email["id"], db_path=db_path)
            summary[outcome] += 1

            if contact["email"] or contact["phone"]:
                summary["contacts"] += 1

            decision(
                "%-7s %s | %s / %s -> %s (confidence %.2f)",
                outcome.upper(), email["id"], company, role or "Unknown Role",
                status, confidence,
            )

            # Logged separately, and only when something was found. The choice
            # between a Reply-To, a From and a signature block is the part of
            # this that is worth being able to audit later.
            if contact["email"] or contact["phone"]:
                decision(
                    "CONTACT %s | %s <%s> %s | via %s",
                    email["id"],
                    contact["name"] or "unnamed",
                    contact["email"] or "no address",
                    contact["phone"] or "",
                    contact["source"] or "signature",
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
        f"{summary['failed']} failed, {summary['contacts']} with a contact."
    )
    logger.info(
        "[sync %s] Finished for user %s: %s", run_id, user_id, summary
    )
    return summary
