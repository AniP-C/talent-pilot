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
