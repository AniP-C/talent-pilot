"""From a recruiter's email to a one-click link back to it.

The tracker could always say an assessment had been requested; it could not
say *which message* said so, which left the user searching their own inbox for
the mail the sync had already read. These pin the path that closes that gap:
the Gmail id travels from the fetched message, through the classifier, into
the job row, and out as a URL the dashboard can render.
"""

import db
import ui
import sync_controller as sc
import workspace
from integrations.gmail_client import Scan


# =====================================================================
# THE URL
# =====================================================================
def test_a_stored_id_becomes_a_gmail_url():
    url = ui.gmail_message_url("18f2c0abc")

    assert url == "https://mail.google.com/mail/u/0/#all/18f2c0abc"


def test_the_link_searches_all_mail_not_just_the_inbox():
    """An older application's mail is exactly what has been archived.

    An inbox-scoped link to an archived message opens an empty pane, which
    looks like the tracker lost the email rather than Gmail filing it.
    """
    assert "#all/" in ui.gmail_message_url("18f2c0abc")


def test_a_missing_id_yields_no_link():
    """Blank, never a dead link — most rows have no email behind them."""
    assert ui.gmail_message_url("") == ""
    assert ui.gmail_message_url(None) == ""
    assert ui.gmail_message_url("   ") == ""


def test_the_account_slot_is_configurable(monkeypatch):
    """Gmail numbers accounts by sign-in order and tells us nothing about it,
    so someone whose work account is second needs to say so."""
    monkeypatch.setattr(ui, "GMAIL_ACCOUNT_INDEX", 1)

    assert ui.gmail_message_url("abc").startswith(
        "https://mail.google.com/mail/u/1/"
    )


# =====================================================================
# THE PLUMBING
# =====================================================================
def _one_email(monkeypatch, message_id: str) -> None:
    """Drive the sync with a single assessment email from a known id."""
    monkeypatch.setattr(sc, "mailbox_address", lambda user_id: "me@example.com")
    monkeypatch.setattr(sc, "is_own_mail", lambda *a, **k: False)
    monkeypatch.setattr(
        sc,
        "fetch_job_emails",
        lambda user_id, own_address="": Scan(
            emails=[
                {
                    "id": message_id,
                    "sender": "Priya <priya@acme.com>",
                    "subject": "Your Acme assessment",
                    "snippet": "Please complete the take-home by Friday.",
                    "body": "Please complete the take-home by Friday.",
                    "reply_to": "",
                    "cc": "",
                    "to": "me@example.com",
                    "date": "1700000000000",
                }
            ],
            dropped=[],
        ),
    )
    monkeypatch.setattr(
        sc,
        "classify_email",
        lambda *a, **k: {
            "category": "ASSESSMENT",
            "company": "Acme",
            "role": "Engineer",
            "confidence": 0.99,
            "is_my_application": True,
            "is_suspicious": False,
            "reasoning": "Take-home assessment requested",
        },
    )


def test_the_gmail_id_survives_the_whole_sync(monkeypatch):
    """The end-to-end guard: an id that is dropped anywhere along the way
    leaves the user with the blank cell this feature exists to remove."""
    _one_email(monkeypatch, "18f2c0abc")

    sc.sync_inbox_to_db(707)

    jobs = db.get_all_jobs(db_path=workspace.jobs_db_path(707))
    assert jobs, "the sync recorded no application at all"
    assert jobs[0]["last_email_id"] == "18f2c0abc"
    assert ui.gmail_message_url(jobs[0]["last_email_id"]).endswith("18f2c0abc")
