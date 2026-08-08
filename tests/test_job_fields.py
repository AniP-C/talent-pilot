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
    is_placeholder,
    looks_like_role,
    normalize,
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
# HELPERS
# =====================================================================
def test_normalize_collapses_whitespace():
    assert normalize("  a   b  ") == "a b"


def test_is_placeholder():
    assert is_placeholder("Unknown Company") is True
    assert is_placeholder("Nexus Labs") is False
