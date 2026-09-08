"""Email filtering and category mapping — the pure logic, no network calls."""

from datetime import datetime

import pytest

from ai.email_classifier import to_status
from config import VALID_STATUSES
from integrations.gmail_client import is_high_probability_job_email, screen_email
from sync_controller import _email_date


# =====================================================================
# THE DATE AN EMAIL ARRIVED
# =====================================================================
# What a row created from an email is dated by. Not the day the sync ran: an
# inbox scanned today is full of confirmations from weeks ago.
def test_gmails_epoch_milliseconds_become_a_calendar_date():
    # Built from a local datetime so the assertion holds in any timezone.
    stamp = int(datetime(2026, 8, 15, 12, 0).timestamp() * 1000)

    assert _email_date({"internal_date": stamp}) == "2026-08-15"


def test_a_missing_date_is_empty_not_nineteen_seventy():
    """`or 0` made this the epoch, and a row dated 1970 would sit at the top of
    the follow-up list forever."""
    assert _email_date({}) == ""
    assert _email_date({"internal_date": 0}) == ""


def test_a_nonsense_date_is_empty():
    assert _email_date({"internal_date": "not a number"}) == ""


# =====================================================================
# THE BOUNCER
# =====================================================================
@pytest.mark.parametrize(
    "sender,subject,snippet",
    [
        ("no-reply@greenhouse.io", "Your application", "Thanks for applying"),
        ("careers@acme.com", "Update", "We reviewed your profile"),
        ("someone@random.com", "Interview invitation", "Are you free Thursday?"),
        ("hr@startup.io", "Next steps", "Please complete the assessment"),
        ("talent@bigco.com", "Offer", "We are pleased to extend an offer"),
    ],
)
def test_genuine_recruiter_mail_passes(sender, subject, snippet):
    assert is_high_probability_job_email(sender, subject, snippet) is True


@pytest.mark.parametrize(
    "sender,subject,snippet",
    [
        ("jobs@indeed.com", "Your weekly job alert", "10 new jobs for you"),
        ("news@medium.com", "Your daily digest", "Top stories"),
        ("marketing@corp.com", "Campaign results", "Open rates are up"),
        ("random@spam.com", "Buy cheap watches", "Limited offer"),
    ],
)
def test_noise_is_filtered_out(sender, subject, snippet):
    assert is_high_probability_job_email(sender, subject, snippet) is False


def test_blacklist_beats_a_matching_ats_domain():
    """A marketing blast from an ATS domain is still a marketing blast."""
    assert (
        is_high_probability_job_email(
            "alerts@greenhouse.io", "Weekly job alert", "New roles this week"
        )
        is False
    )


def test_handles_empty_input():
    assert is_high_probability_job_email("", "", "") is False


# =====================================================================
# THE BOUNCER EXPLAINS ITSELF
#
# This filter runs before any model call, so a message it rejects never
# happens as far as the rest of the system is concerned. Until it named its
# reasons there was no way, even in principle, to find out which genuine
# recruiter mail it was throwing away.
# =====================================================================
def test_a_drop_names_the_rule_that_dropped_it():
    verdict = screen_email("jobs@indeed.com", "Your weekly job alert", "10 new jobs")

    assert verdict.passed is False
    assert "job alert" in verdict.reason


def test_a_pass_names_the_rule_that_passed_it():
    assert screen_email(
        "no-reply@greenhouse.io", "Your application", "Thanks for applying"
    ).reason == "ATS sender greenhouse.io"


def test_mail_with_no_signal_at_all_says_so():
    assert "no recruiting signal" in screen_email("a@b.com", "Hello", "Hi").reason


def test_the_boolean_wrapper_still_answers_the_old_question():
    """Callers that only want yes or no are unchanged."""
    assert is_high_probability_job_email("careers@acme.com", "Update", "") is True
    assert is_high_probability_job_email("a@b.com", "Hello", "") is False


# =====================================================================
# THE USER'S OWN SENT MAIL
#
# Their replies to recruiters match every content rule there is — they are
# about an application and they quote the thread — so they were fetched,
# classified at cost, and only then discarded downstream as "not an update on
# an application this user submitted". Seven such calls across three syncs in
# the log that prompted this.
# =====================================================================
def test_your_own_reply_is_rejected_before_it_costs_anything():
    verdict = screen_email(
        "Ani Py <anipy2000@gmail.com>",
        "Re: Career Opportunity - Forward Deployed AI Engineer",
        "Thanks for reaching out, I am interested in the role.",
        own_address="anipy2000@gmail.com",
    )

    assert verdict.passed is False
    assert verdict.reason == "sent from your own address"


def test_your_own_address_is_matched_however_the_header_is_written():
    """Gmail writes From as a display name plus an angle-bracketed address."""
    for sender in (
        "anipy2000@gmail.com",
        "Ani Py <anipy2000@gmail.com>",
        "Ani Py <ANIPY2000@Gmail.com>",
    ):
        assert (
            screen_email(sender, "Application", "", own_address="anipy2000@gmail.com")
            .passed
            is False
        )


def test_a_recruiter_at_a_different_address_is_not_mistaken_for_you():
    assert screen_email(
        "recruiter@acme.com",
        "Your application",
        "We would like to speak with you.",
        own_address="anipy2000@gmail.com",
    ).passed is True


def test_without_a_known_address_nothing_is_excluded_for_being_yours():
    """mailbox_address is best-effort; a failure must not filter the inbox."""
    assert screen_email(
        "Ani Py <anipy2000@gmail.com>", "Application", "About the role"
    ).passed is True


# =====================================================================
# CATEGORY MAPPING
# =====================================================================
def test_received_maps_onto_applied():
    """RECEIVED is model vocabulary; the tracker calls that state APPLIED."""
    assert to_status("RECEIVED") == "APPLIED"


@pytest.mark.parametrize(
    "category", ["INTERVIEW", "OFFER", "REJECTED", "ASSESSMENT", "ACTION_REQUIRED"]
)
def test_known_categories_map_to_themselves(category):
    assert to_status(category) == category


@pytest.mark.parametrize("category", ["UNKNOWN", "", "GIBBERISH", None])
def test_unusable_categories_map_to_none(category):
    assert to_status(category) is None


def test_every_mapped_status_is_a_valid_status():
    for category in ["RECEIVED", "INTERVIEW", "OFFER", "REJECTED", "ASSESSMENT"]:
        assert to_status(category) in VALID_STATUSES


def test_category_matching_is_case_insensitive():
    assert to_status("interview") == "INTERVIEW"


# =====================================================================
# OAUTH STATE (survives the redirect back from Google)
# =====================================================================
def test_oauth_state_round_trips_through_the_workspace(monkeypatch, tmp_path):
    """Google redirects back as a fresh page load, so the state cannot live
    in the web session — it has to be readable from storage afterwards."""
    from integrations import gmail_client

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(gmail_client, "_state_path", lambda user_id: state_file)

    import json, time
    state_file.write_text(
        json.dumps({
            "state": "abc123",
            "code_verifier": "verifier-xyz",
            "expires_at": time.time() + 600,
        })
    )

    # The PKCE verifier must survive alongside the state, or the token
    # exchange fails with "Missing code verifier".
    assert gmail_client._load_pending_state(1)["code_verifier"] == "verifier-xyz"

    assert gmail_client._load_pending_state(1)["state"] == "abc123"


def test_expired_oauth_state_is_discarded(monkeypatch, tmp_path):
    from integrations import gmail_client

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(gmail_client, "_state_path", lambda user_id: state_file)

    import json, time
    state_file.write_text(
        json.dumps({"state": "stale", "expires_at": time.time() - 1})
    )

    assert gmail_client._load_pending_state(1) is None
    assert not state_file.exists()


def test_missing_oauth_state_is_none(monkeypatch, tmp_path):
    from integrations import gmail_client

    monkeypatch.setattr(gmail_client, "_state_path", lambda user_id: tmp_path / "nope.json")

    assert gmail_client._load_pending_state(1) is None


def test_complete_auth_rejects_a_mismatched_state(monkeypatch, tmp_path):
    """The check that stops one user's callback attaching a mailbox to another."""
    import pytest as _pytest
    from integrations import gmail_client

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(gmail_client, "_state_path", lambda user_id: state_file)
    monkeypatch.setattr(gmail_client, "_require_credentials_file", lambda: None)

    import json, time
    state_file.write_text(
        json.dumps({
            "state": "expected",
            "code_verifier": "v",
            "expires_at": time.time() + 600,
        })
    )

    with _pytest.raises(gmail_client.GmailAuthError, match="did not match"):
        gmail_client.complete_auth(1, "some-code", "attacker-supplied")
