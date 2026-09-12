"""Email filtering and category mapping — the pure logic, no network calls."""

from datetime import datetime

import pytest

import ai.email_classifier as email_classifier
from ai.email_classifier import classify_email, resolve_company, to_status
from config import VALID_STATUSES
from integrations.gmail_client import is_high_probability_job_email, screen_email
from sync_controller import _email_date, _primary_recipient


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
# WHAT THE BULK-MAIL LIST IS ALLOWED TO REJECT
#
# It used to be bare words checked against the sender, the subject and the
# first 1500 characters of the body at once, and it beats every other signal.
# That gave three ways to lose a real application, one per test below.
# =====================================================================
def test_a_recruiter_proposing_a_weekly_sync_is_not_a_newsletter():
    """"weekly" as a bare word took down any message that used the word."""
    assert screen_email(
        "recruiter@acme.com",
        "Your application - next steps",
        "We hold a weekly sync on Mondays and would like you to join Thursday's.",
    ).passed is True


@pytest.mark.parametrize(
    "subject",
    [
        "Application for Marketing Manager",
        "Your application - Campaign Strategist",
        "Interview - Marketing Campaign Analyst",
    ],
)
def test_marketing_and_campaign_are_job_titles_not_spam_words(subject):
    """Anyone applying for a marketing role had every confirmation rejected."""
    assert screen_email("careers@acme.com", subject, "Thanks for applying.").passed


def test_a_footer_does_not_outrank_the_subject_and_the_sender():
    """The body is not what a message is about.

    A blocked word beats every other signal, so one wrong word in an ATS
    template footer outranked "Your application" in the subject and a
    greenhouse.io sender — and the message was never classified.
    """
    body = (
        "Thanks for applying to Acme. We will be in touch.\\n\\n"
        "-- \\nAcme Talent. You are receiving this because you applied. "
        "Read our weekly newsletter digest or update your marketing preferences."
    )

    assert screen_email(
        "no-reply@greenhouse.io", "Your application to Acme", body
    ).passed is True


def test_a_real_blast_is_still_rejected_by_its_footer():
    """The narrow body markers stay: no genuine one-to-one mail says this."""
    verdict = screen_email(
        "alerts@somejobsite.com",
        "5 roles picked for you",
        "Here are today's roles.\\n\\nTo unsubscribe from job alerts, click here.",
    )

    assert verdict.passed is False
    assert "unsubscribe from job" in verdict.reason


def test_the_subject_still_gives_a_blast_away():
    for sender, subject in [
        ("jobs@indeed.com", "Your weekly job alert"),
        ("news@medium.com", "Your daily digest"),
        ("hello@site.com", "This week's newsletter"),
        ("noreply@board.com", "Jobs for you this week"),
    ]:
        assert screen_email(sender, subject, "").passed is False


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
# Two kinds, and only one is waste. A reply on a recruiter's thread matches
# every content rule there is — it is about an application and it quotes the
# thread — and says nothing the recruiter's own mail will not say again, so
# classifying it costs a model call to be told it is not an update. Seven such
# calls across three syncs in the log that prompted this.
#
# An application *sent* by mail is the opposite: it is frequently the only
# evidence the application exists, and dropping it meant the job was never
# tracked at all.
# =====================================================================
def test_your_own_reply_is_rejected_before_it_costs_anything():
    verdict = screen_email(
        "Ani Py <anipy2000@gmail.com>",
        "Re: Career Opportunity - Forward Deployed AI Engineer",
        "Thanks for reaching out, I am interested in the role.",
        own_address="anipy2000@gmail.com",
    )

    assert verdict.passed is False
    assert verdict.reason == "your own reply on an existing thread"


@pytest.mark.parametrize(
    "subject",
    [
        "Re: Career Opportunity - Forward Deployed AI Engineer",
        "RE: Your application",
        "Fwd: Application – Agentic AI Engineer",
        "FW: Interview",
        "re : Application",
    ],
)
def test_every_spelling_of_a_thread_reply_is_recognised(subject):
    assert (
        screen_email(
            "Ani Py <anipy2000@gmail.com>",
            subject,
            "Thanks, I am interested.",
            own_address="anipy2000@gmail.com",
        ).passed
        is False
    )


def test_an_application_you_sent_yourself_is_kept():
    """The message the tracker would otherwise never hear about.

    Applying by writing to a careers address produces no confirmation from a
    human inbox, so this outgoing mail is the only record the application
    exists. Straight from the log: it was dropped as "sent from your own
    address" and the job went untracked.
    """
    verdict = screen_email(
        "Ani Py <anipy2000@gmail.com>",
        "Application for AI/ML Engineer - Pune",
        "Please find my resume attached for the AI/ML Engineer role.",
        own_address="anipy2000@gmail.com",
    )

    assert verdict.passed is True


def test_your_own_address_is_matched_however_the_header_is_written():
    """Gmail writes From as a display name plus an angle-bracketed address."""
    for sender in (
        "anipy2000@gmail.com",
        "Ani Py <anipy2000@gmail.com>",
        "Ani Py <ANIPY2000@Gmail.com>",
    ):
        assert (
            screen_email(
                sender, "Re: Application", "", own_address="anipy2000@gmail.com"
            ).passed
            is False
        )


def test_a_reply_from_someone_else_is_not_treated_as_yours():
    """The thread prefix only silences mail the user themselves sent."""
    assert screen_email(
        "recruiter@acme.com",
        "Re: Your application",
        "We would like to speak with you.",
        own_address="anipy2000@gmail.com",
    ).passed is True


# =====================================================================
# WHO THE EMPLOYER IS ON MAIL YOU SENT
# =====================================================================
def test_the_employer_on_your_own_application_is_who_you_wrote_to():
    """From and Reply-To are both the applicant; only To names the company."""
    company = resolve_company(
        {"company_name": "Unknown", "role_title": "AI Engineer"},
        sender="Ani Py <anipy2000@gmail.com>",
        reply_to="",
        recipient="careers@onix.com",
    )

    assert company == "Onix"


def test_a_recipient_that_is_a_mail_provider_names_no_employer():
    """On incoming mail the recipient is the user's own inbox."""
    assert (
        resolve_company(
            {"company_name": "Unknown", "role_title": "AI Engineer"},
            sender="jobs@indeed.com",
            recipient="anipy2000@gmail.com",
        )
        == ""
    )


def _captured_prompt(monkeypatch) -> list:
    """Hold onto the prompt instead of calling Gemini."""
    seen = []

    def fake(prompt, schema, label):
        seen.append(prompt)
        return {}

    monkeypatch.setattr(email_classifier, "generate_structured", fake)
    return seen


def test_mail_you_sent_is_classified_as_your_own_application(monkeypatch):
    """Without this the model answers, correctly by rule 4, that mail from the
    applicant is not an update on an application — and the job is lost."""
    seen = _captured_prompt(monkeypatch)

    classify_email(
        sender="Ani Py <anipy2000@gmail.com>",
        subject="Application for AI/ML Engineer - Pune",
        snippet="Please find my resume attached.",
        recipient="careers@onix.com",
        from_me=True,
    )

    prompt = seen[0]
    assert "SENT BY THE APPLICANT" in prompt
    assert "TO: careers@onix.com" in prompt
    # The employer is read off the address it went to, not off gmail.com.
    assert '"Onix"' in prompt


def test_incoming_mail_carries_no_self_sent_rule(monkeypatch):
    seen = _captured_prompt(monkeypatch)

    classify_email(
        sender="careers@acme.com",
        subject="Your application",
        snippet="Thanks for applying.",
        recipient="anipy2000@gmail.com",
    )

    assert "SENT BY THE APPLICANT" not in seen[0]


def test_the_first_addressee_is_the_one_that_counts():
    """`parseaddr` returns nothing at all for a multi-address header."""
    assert (
        _primary_recipient("Careers <careers@acme.com>, hr@acme.com")
        == "Careers <careers@acme.com>"
    )
    assert _primary_recipient("careers@acme.com") == "careers@acme.com"
    assert _primary_recipient("") == ""


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
