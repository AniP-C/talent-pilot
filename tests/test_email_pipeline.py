"""Email filtering and category mapping — the pure logic, no network calls."""

import pytest

from ai.email_classifier import to_status
from config import VALID_STATUSES
from integrations.gmail_client import is_high_probability_job_email


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
