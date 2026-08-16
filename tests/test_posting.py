"""Salary and location normalisation. No network, no model.

These values arrive from a client the server does not control, read out of a
page the server never sees. So the tests are mostly about what gets *rejected*:
the standing rule is that an empty field is corrected by the next posting while
a wrong one is a number somebody makes a decision on.
"""

import pytest

import posting


# =====================================================================
# PERIODS AND CURRENCIES
# =====================================================================
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("YEAR", "YEAR"),
        ("year", "YEAR"),
        ("YEARLY", "YEAR"),
        ("ANNUAL", "YEAR"),
        ("per annum", "YEAR"),
        ("P.A.", "YEAR"),
        ("MONTH", "MONTH"),
        ("monthly", "MONTH"),
        ("HOUR", "HOUR"),
        ("hourly", "HOUR"),
        ("per hour", "HOUR"),
        ("WEEK", "WEEK"),
        ("", ""),
        ("fortnightly", ""),
        (None, ""),
    ],
)
def test_pay_periods_are_normalised_or_dropped(raw, expected):
    assert posting.normalise_period(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("USD", "USD"),
        ("inr", "INR"),
        ("₹", "INR"),
        ("€", "EUR"),
        ("£", "GBP"),
        # "$" is USD, CAD, AUD, SGD, HKD and more. A tracker showing the wrong
        # currency is worse than one showing none.
        ("$", ""),
        ("", ""),
        ("dollars", ""),
        ("US", ""),
    ],
)
def test_currencies_are_codes_or_nothing(raw, expected):
    assert posting.normalise_currency(raw) == expected


# =====================================================================
# SALARY
# =====================================================================
def test_a_declared_range_is_stored_as_given():
    result = posting.normalise_salary(120000, 160000, "USD", "YEAR")

    assert result == {"min": 120000, "max": 160000, "currency": "USD", "period": "YEAR"}


def test_a_single_figure_becomes_a_range_of_width_zero():
    """So every reader handles one shape rather than three."""
    result = posting.normalise_salary(1_800_000, None, "INR", "YEAR")

    assert result["min"] == result["max"] == 1_800_000


def test_only_an_upper_bound_still_stores_both():
    result = posting.normalise_salary(None, 90000, "GBP", "YEAR")

    assert result["min"] == result["max"] == 90000


def test_a_reversed_range_is_swapped():
    """A scrape that read the fields in page order rather than by name."""
    result = posting.normalise_salary(160000, 120000, "USD", "YEAR")

    assert (result["min"], result["max"]) == (120000, 160000)


def test_strings_with_separators_are_read():
    result = posting.normalise_salary("18,00,000", "24,00,000", "INR", "YEAR")

    assert (result["min"], result["max"]) == (1800000, 2400000)


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        -50000,
        "competitive",
        "",
        # A timestamp or a requisition id that landed in the salary slot.
        1755302400000,
        float("nan"),
    ],
)
def test_values_that_are_not_salaries_are_dropped(value):
    assert posting.normalise_salary(value, None, "USD", "YEAR") == posting.EMPTY_SALARY


def test_a_period_the_amount_cannot_be_quoted_in_is_dropped():
    """£120,000 per hour is a yearly figure whose unitText was misread. The
    number survives — it is probably right — and the period does not."""
    result = posting.normalise_salary(120000, None, "GBP", "HOUR")

    assert result["min"] == 120000
    assert result["period"] == ""


def test_a_plausible_hourly_rate_keeps_its_period():
    result = posting.normalise_salary(85, None, "USD", "HOUR")

    assert result["period"] == "HOUR"


def test_an_amount_survives_an_unknown_currency():
    """"120,000–160,000/yr" without a currency is still worth showing."""
    result = posting.normalise_salary(120000, 160000, "$", "YEAR")

    assert result["min"] == 120000
    assert result["currency"] == ""
    assert result["period"] == "YEAR"


# =====================================================================
# FORMATTING
# =====================================================================
@pytest.mark.parametrize(
    "args,expected",
    [
        ((120000, 160000, "USD", "YEAR"), "USD 120,000–160,000/yr"),
        ((1800000, 1800000, "INR", "YEAR"), "INR 1,800,000/yr"),
        ((85, 110, "USD", "HOUR"), "USD 85–110/hr"),
        ((60000, 70000, "", "MONTH"), "60,000–70,000/mo"),
        ((90000, 90000, "GBP", ""), "GBP 90,000"),
        ((None, None, "USD", "YEAR"), ""),
    ],
)
def test_salaries_format_readably(args, expected):
    assert posting.format_salary(*args) == expected


# =====================================================================
# LOCATION
# =====================================================================
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Bengaluru, Karnataka", "Bengaluru, Karnataka"),
        ("  London,  England ", "London, England"),
        ("Berlin,", "Berlin"),
        ("", ""),
        ("N/A", ""),
        ("Unknown", ""),
        ("Multiple locations", ""),
    ],
)
def test_locations_are_cleaned_or_dropped(raw, expected):
    assert posting.normalise_location(raw) == expected


@pytest.mark.parametrize("raw", ["Remote", "remote", "Telecommute", "Work from home", "WFH"])
def test_a_location_that_only_says_remote_becomes_the_flag(raw):
    """Keeping "Remote" in both the place and the flag means every reader
    deciding which one to believe."""
    assert posting.normalise_location(raw) == ""
    assert posting.looks_remote(raw) is True


def test_a_remote_role_with_an_anchor_keeps_its_place():
    """A real and common combination: remote, but tied to a timezone or a
    right-to-work region."""
    assert posting.normalise_location("Remote — London") == "Remote — London"
    assert posting.looks_remote("Remote — London") is True


def test_an_over_long_location_is_capped():
    assert len(posting.normalise_location("x" * 500)) == posting.MAX_LOCATION_LENGTH


# =====================================================================
# WHERE TO FIND THE POSTING
# =====================================================================
# This allowlist is a security boundary. A row created by inbox sync takes its
# link from an email body, so without it anyone could mail the user and have
# their URL rendered as a trustworthy "Posting ↗" button inside the user's own
# tracker days later, stripped of every cue that it arrived in a cold email.
@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/nexuslabs/jobs/1",
        "https://jobs.lever.co/nexuslabs/abc",
        "https://nexuslabs.workable.com/j/ABC",
        "https://www.linkedin.com/jobs/view/123",
        "https://acme.wd1.myworkdayjobs.com/careers/job/123",
    ],
)
def test_board_and_ats_links_are_accepted(url):
    assert posting.is_job_link(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # The whole point: an arbitrary sender's domain never becomes a button.
        "https://acme-verify.example/offer/release",
        "http://192.168.1.10/payroll",
        "https://bit.ly/3xYzAbC",
        # Real boards, but the footer rather than the posting.
        "https://boards.greenhouse.io/unsubscribe?token=abc",
        "https://www.linkedin.com/track/open?id=99",
        "https://jobs.lever.co/pixel.png",
        "",
        "not a url",
    ],
)
def test_everything_else_is_refused(url):
    assert posting.is_job_link(url) is False


def test_the_posting_link_is_found_in_an_email_body():
    body = (
        "Thanks for applying! Track your application here: "
        "https://boards.greenhouse.io/nexuslabs/jobs/1 \n"
        "Unsubscribe: https://mailer.example/unsubscribe?x=1"
    )

    assert posting.find_job_link(body) == "https://boards.greenhouse.io/nexuslabs/jobs/1"


def test_an_email_with_no_board_link_yields_nothing():
    body = "Reply to jane@acme.com or see https://acme-verify.example/pay-now"

    assert posting.find_job_link(body) == ""


def test_trailing_punctuation_is_not_part_of_the_link():
    body = "Apply at https://jobs.lever.co/nexuslabs/abc."

    assert posting.find_job_link(body) == "https://jobs.lever.co/nexuslabs/abc"


@pytest.mark.parametrize(
    "location,remote,expected",
    [
        ("Bengaluru", False, "Bengaluru"),
        ("Bengaluru", True, "Remote · Bengaluru"),
        ("", True, "Remote"),
        ("", False, ""),
        ("Remote", True, "Remote"),
    ],
)
def test_location_and_arrangement_are_shown_together(location, remote, expected):
    assert posting.format_location(location, remote) == expected
