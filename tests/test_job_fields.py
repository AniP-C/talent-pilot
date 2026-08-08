"""Company/role validation — the guard against storing a job title as a company.

The bug this exists to prevent: a Workable page titled "AI Engineer - Nexus
Labs" was split on " - " and index 0 taken, so the tracker recorded a company
called "AI Engineer". Every later email about that application then failed to
match and opened a duplicate row.
"""

import pytest

from job_fields import (
    InvalidJobField,
    company_from_email_domain,
    company_from_url,
    is_placeholder,
    looks_like_role,
    normalize,
    prettify_slug,
    validate_company,
    validate_role,
)


# =====================================================================
# ROLE DETECTION
# =====================================================================
@pytest.mark.parametrize(
    "value",
    [
        "AI Engineer",
        "Senior AI Engineer",
        "Machine Learning Engineer",
        "Data Scientist",
        "Backend Developer",
        "Full Stack Engineer",
        "Product Manager",
        "QA Analyst",
        "Software Engineer II",
        "Intern",
        "DevOps Engineer",
    ],
)
def test_job_titles_are_recognised_as_roles(value):
    assert looks_like_role(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "Nexus Labs",
        "Stripe",
        "Notion",
        "Acme Robotics",
        "Palantir Technologies",
        "Zoho",
        "Swiggy",
        # Contains a role word but is unambiguously a company.
        "Engineering Solutions Pvt Ltd",
        "Systems Ltd",
        "Head Digital Works",
        "Scientist.com",
    ],
)
def test_company_names_are_not_mistaken_for_roles(value):
    """Rejecting a real company is worse than accepting an odd one, so the
    check only fires when nothing in the string could name an employer."""
    assert looks_like_role(value) is False


def test_corporate_suffix_overrides_role_words():
    """'Engineering' is a role word; 'Ltd' settles that this is an org."""
    assert looks_like_role("Engineering Ltd") is False


# =====================================================================
# COMPANY VALIDATION
# =====================================================================
def test_a_bare_job_title_is_rejected_as_a_company():
    with pytest.raises(InvalidJobField, match="job title"):
        validate_company("AI Engineer", "AI Engineer")


def test_company_equal_to_role_is_rejected():
    """One value used for both fields means the extractor found only one."""
    with pytest.raises(InvalidJobField, match="both"):
        validate_company("Nexus Labs", "Nexus Labs")


@pytest.mark.parametrize(
    "value", ["Unknown Company", "unknown", "N/A", "none", "careers", "job"]
)
def test_placeholders_are_rejected(value):
    with pytest.raises(InvalidJobField, match="placeholder"):
        validate_company(value, "Backend Engineer")


def test_a_punctuation_only_company_reads_as_blank():
    """normalize() strips separators, so "-" is empty rather than a placeholder."""
    with pytest.raises(InvalidJobField, match="required"):
        validate_company("-", "Backend Engineer")


def test_blank_company_is_rejected():
    with pytest.raises(InvalidJobField, match="required"):
        validate_company("", "Backend Engineer")


def test_overlong_company_is_rejected():
    with pytest.raises(InvalidJobField, match="longer than"):
        validate_company("x" * 500, "Backend Engineer")


def test_a_real_company_survives():
    assert validate_company("  Nexus   Labs ", "AI Engineer") == "Nexus Labs"


def test_validation_strips_title_punctuation():
    """Values arriving from a title split carry separator debris."""
    assert validate_company("- Nexus Labs |", "AI Engineer") == "Nexus Labs"


# =====================================================================
# ROLE VALIDATION
# =====================================================================
def test_role_is_normalised():
    assert validate_role("  Senior   AI  Engineer ") == "Senior AI Engineer"


def test_blank_role_is_rejected():
    with pytest.raises(InvalidJobField, match="required"):
        validate_role("   ")


def test_a_role_may_look_like_a_role():
    """The role field is where a job title belongs."""
    assert validate_role("AI Engineer") == "AI Engineer"


# =====================================================================
# SENDER DOMAIN -> COMPANY
# =====================================================================
@pytest.mark.parametrize(
    "sender,expected",
    [
        ("careers@meesho.com", "Meesho"),
        ("hr@infosys.com", "Infosys"),
        ("recruiting@zomato.com", "Zomato"),
        ("talent@swiggy.in", "Swiggy"),
        ("careers@turing.com", "Turing"),
        ("Meesho Careers <careers@meesho.com>", "Meesho"),
        # Mail-infrastructure subdomains are stripped.
        ("talent@notifications.acme.com", "Acme"),
        ("no-reply@mail.nexuslabs.com", "Nexuslabs"),
        # Second-level domains.
        ("hr@acme.co.in", "Acme"),
        # Hyphens become spaces.
        ("careers@acme-robotics.com", "Acme Robotics"),
    ],
)
def test_company_is_recovered_from_the_sender_domain(sender, expected):
    """The sending domain is the steadiest company signal in recruiter mail —
    subject lines are dominated by the role."""
    assert company_from_email_domain(sender) == expected


@pytest.mark.parametrize(
    "sender",
    [
        "no-reply@greenhouse.io",
        "no-reply@hire.lever.co",
        "jobs@myworkdayjobs.com",
        "noreply@ashbyhq.com",
        "alerts@linkedin.com",
        "info@naukri.com",
        # Consumer mail says nothing about an employer.
        "priya@gmail.com",
        "someone@outlook.com",
    ],
)
def test_ats_and_consumer_domains_yield_no_company(sender):
    """These domains belong to the sender's tooling, not the hiring company."""
    assert company_from_email_domain(sender) == ""


def test_malformed_sender_is_handled():
    assert company_from_email_domain("not an address") == ""
    assert company_from_email_domain("") == ""
    assert company_from_email_domain(None) == ""


# =====================================================================
# POSTING URL -> COMPANY
# =====================================================================
# Used to repair rows whose company was mis-scraped: the saved link is
# independent of the markup that was misread. Each board hides the employer
# somewhere different, so there is no single rule.
@pytest.mark.parametrize(
    "url,expected",
    [
        # First path segment.
        ("https://boards.greenhouse.io/nexuslabs/jobs/1234", "Nexuslabs"),
        ("https://job-boards.greenhouse.io/acme-robotics/jobs/99", "Acme Robotics"),
        ("https://jobs.lever.co/nexuslabs/xyz-123", "Nexuslabs"),
        ("https://jobs.ashbyhq.com/nexus-labs/abc", "Nexus Labs"),
        ("https://apply.workable.com/nexus-labs/j/ABC/", "Nexus Labs"),
        ("https://jobs.smartrecruiters.com/NexusLabs/7439", "Nexuslabs"),
        # Subdomain.
        ("https://nexuslabs.workable.com/j/ABC123", "Nexuslabs"),
        ("https://nexuslabs.recruitee.com/o/ai-engineer", "Nexuslabs"),
        ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x", "Acme"),
        # Ordinary career pages.
        ("https://careers.nexuslabs.com/openings/42", "Nexuslabs"),
        ("https://nexuslabs.com/jobs/42", "Nexuslabs"),
        # Wellfound keeps it under /company/.
        ("https://wellfound.com/company/nexus-labs/jobs/123-ai", "Nexus Labs"),
    ],
)
def test_company_is_recovered_from_a_posting_url(url, expected):
    assert company_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Aggregators genuinely do not carry the employer in the URL. Guessing
        # would yield "Linkedin" as a company, which is worse than admitting it.
        "https://www.linkedin.com/jobs/view/4012345678",
        "https://in.indeed.com/viewjob?jk=abc",
        "https://www.naukri.com/job-listings-x-123",
        "",
        "not a url",
    ],
)
def test_opaque_urls_yield_no_company(url):
    assert company_from_url(url) == ""


# =====================================================================
# HELPERS
# =====================================================================
def test_prettify_slug():
    assert prettify_slug("acme-robotics") == "Acme Robotics"
    assert prettify_slug("nexus_labs") == "Nexus Labs"
    assert prettify_slug("") == ""


def test_normalize_collapses_whitespace():
    assert normalize("  a   b  ") == "a b"


def test_is_placeholder():
    assert is_placeholder("Unknown Company") is True
    assert is_placeholder("Nexus Labs") is False
