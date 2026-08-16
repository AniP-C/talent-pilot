"""Facts about the posting itself: where it is, what it pays, where to find it.

Both come out of the JSON-LD ``JobPosting`` block the extension already parses
for company and role — and that provenance is the whole reason they are worth
storing. These are *declared* fields, so reading them is not the guesswork that
scraping a salary out of prose would be. The block was being parsed and these
two thrown away.

Everything here is pure: no I/O, no network, no model call. It exists because
the numbers arrive from a client the server does not control, so they have to be
checked rather than believed — the same reason ``job_fields.py`` exists for the
company and role pair.

The standing rule when a value cannot be trusted is the one used everywhere else
in this codebase: **report nothing rather than something wrong.** An empty
salary field is corrected by the next posting. A wrong one is a number somebody
makes a decision on.
"""

import re

# What a salary can be quoted per. Anything else is dropped rather than guessed.
SALARY_PERIODS = ("HOUR", "DAY", "WEEK", "MONTH", "YEAR")

# schema.org's `unitText` is meant to be one of these, and in practice is any of
# a dozen spellings of them.
_PERIOD_ALIASES = {
    "HOUR": "HOUR", "HOURLY": "HOUR", "HR": "HOUR", "PERHOUR": "HOUR",
    "DAY": "DAY", "DAILY": "DAY", "PERDAY": "DAY",
    "WEEK": "WEEK", "WEEKLY": "WEEK", "PERWEEK": "WEEK",
    "MONTH": "MONTH", "MONTHLY": "MONTH", "PERMONTH": "MONTH", "MO": "MONTH",
    "YEAR": "YEAR", "YEARLY": "YEAR", "ANNUAL": "YEAR", "ANNUALLY": "YEAR",
    "ANNUM": "YEAR", "PERANNUM": "YEAR", "PERYEAR": "YEAR", "PA": "YEAR",
    "YR": "YEAR", "PERYR": "YEAR",
}

# The ceiling exists to catch a value that is not a salary at all: an epoch
# timestamp, a requisition id, a phone number with the punctuation stripped.
# Deliberately currency-agnostic and generous — an annual salary in IDR or VND
# runs to hundreds of millions, and rejecting a real one to tidy up the range
# would be the worse error.
MAX_SALARY = 10**10

# A salary quoted per hour in the hundreds of thousands is a yearly figure whose
# period was misread. Only the obviously-impossible pairings are rejected.
_PERIOD_CEILING = {
    "HOUR": 100_000,
    "DAY": 1_000_000,
    "WEEK": 5_000_000,
}

MAX_LOCATION_LENGTH = 120

# Currency symbols that identify exactly one currency. "$" is deliberately
# absent: it is USD, CAD, AUD, SGD, HKD, MXN and more, and a tracker showing the
# wrong currency is worse than one showing no currency at all.
CURRENCY_SYMBOLS = {
    "₹": "INR",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₩": "KRW",
    "₽": "RUB",
    "₪": "ILS",
    "₺": "TRY",
    "R$": "BRL",
    "CHF": "CHF",
}

# What the scraper emits when a page names no location.
_LOCATION_PLACEHOLDERS = {
    "", "-", "n/a", "na", "none", "null", "undefined", "unknown",
    "not specified", "anywhere", "various", "multiple locations",
    "location", "tbd",
}

# Words a posting uses in the location slot when it means "remote". Read as a
# remote flag rather than stored as a place, so "Remote" never ends up being
# both the location and the flag.
_REMOTE_WORDS = re.compile(
    r"\b(remote|telecommute|work\s*from\s*home|wfh|anywhere|distributed)\b",
    re.IGNORECASE,
)

EMPTY_SALARY = {"min": None, "max": None, "currency": "", "period": ""}


# =====================================================================
# SALARY
# =====================================================================
def normalise_period(value: str) -> str:
    """Map any spelling of a pay period onto one of SALARY_PERIODS, or ""."""
    key = re.sub(r"[^A-Z]", "", str(value or "").upper())
    return _PERIOD_ALIASES.get(key, "")


def normalise_currency(value: str) -> str:
    """Return a three-letter currency code, or "".

    Accepts a code as written and the unambiguous symbols. An ambiguous symbol
    yields "" — the amount is still worth storing without a currency, and a
    wrong currency is not.
    """
    raw = str(value or "").strip()

    if not raw:
        return ""

    if raw in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[raw]

    letters = re.sub(r"[^A-Za-z]", "", raw).upper()
    return letters if len(letters) == 3 else ""


def _as_amount(value) -> int | None:
    """One salary figure as a positive integer, or None if it is not one."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, str):
        # "120,000" and "1 20 000" both appear; a decimal point is meaningful
        # and a comma never is.
        cleaned = re.sub(r"[,\s]", "", value)
        match = re.search(r"\d+(?:\.\d+)?", cleaned)
        if not match:
            return None
        value = match.group(0)

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    # NaN fails every comparison, so it has to be excluded by the first test.
    if not number == number or number <= 0 or number > MAX_SALARY:
        return None

    return int(round(number))


def normalise_salary(minimum=None, maximum=None, currency="", period="") -> dict:
    """Return ``{"min", "max", "currency", "period"}``, with anything unusable
    dropped to ``None`` / ``""``.

    A posting quoting a single figure sets both bounds to it, so the stored
    range always reads the same way and a later query does not have to handle
    three shapes.
    """
    low = _as_amount(minimum)
    high = _as_amount(maximum)
    period = normalise_period(period)

    # A pair the wrong way round is a scrape that read the fields in page order
    # rather than by name. Swapping is obviously what was meant.
    if low is not None and high is not None and low > high:
        low, high = high, low

    # One bound is a valid posting ("from ₹18,00,000"), but storing it in only
    # one column would mean every reader handling the half-open case. A single
    # figure is a range of width zero.
    if low is None:
        low = high
    if high is None:
        high = low

    # A period the amount cannot possibly be quoted in was misread. The number
    # survives; the wrong period does not.
    ceiling = _PERIOD_CEILING.get(period)
    if ceiling is not None and high is not None and high > ceiling:
        period = ""

    if low is None:
        return dict(EMPTY_SALARY)

    return {
        "min": low,
        "max": high,
        "currency": normalise_currency(currency),
        "period": period,
    }


_PERIOD_SUFFIX = {
    "HOUR": "/hr",
    "DAY": "/day",
    "WEEK": "/wk",
    "MONTH": "/mo",
    "YEAR": "/yr",
}


def format_salary(minimum=None, maximum=None, currency="", period="") -> str:
    """A salary as one short string for display, or "" when none is known.

    Takes stored column values rather than raw input, so it assumes they already
    went through :func:`normalise_salary`.
    """
    low = _as_amount(minimum)
    high = _as_amount(maximum)

    if low is None and high is None:
        return ""

    if low is None:
        low = high
    if high is None:
        high = low

    code = normalise_currency(currency)
    prefix = f"{code} " if code else ""
    suffix = _PERIOD_SUFFIX.get(normalise_period(period), "")

    if low == high:
        return f"{prefix}{low:,}{suffix}"

    return f"{prefix}{low:,}–{high:,}{suffix}"


# =====================================================================
# LOCATION
# =====================================================================
def looks_remote(value: str) -> bool:
    """True when a location string is really saying "remote"."""
    return bool(_REMOTE_WORDS.search(str(value or "")))


def normalise_location(value: str) -> str:
    """Clean a location for storage, or return "".

    A value that only says "remote" returns "" — that belongs in the remote
    flag, and keeping it in both places means every reader deciding which one to
    believe.
    """
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" ,;·-\t")

    if not cleaned or cleaned.lower() in _LOCATION_PLACEHOLDERS:
        return ""

    # Only when the whole string is the remote word. "Remote — London" keeps its
    # place; see format_location for how the two are shown together.
    if looks_remote(cleaned) and not _REMOTE_WORDS.sub("", cleaned).strip(" ,;·—-"):
        return ""

    return cleaned[:MAX_LOCATION_LENGTH].strip(" ,;·-")


# =====================================================================
# WHERE TO FIND THE POSTING
# =====================================================================
# Hosts a job link may point at, and nothing else is accepted.
#
# This allowlist is a security boundary, not a convenience. A row created by
# inbox sync gets its link from an email body, and an email body is written by
# whoever sent it — so without this, anyone could mail the user and have their
# URL rendered as a trustworthy "Posting ↗" button inside the user's own
# tracker, days later, stripped of every cue that it arrived in a cold email.
# That is a better phishing delivery vehicle than the email was.
#
# Restricting to boards and ATS vendors costs almost nothing: those are the
# links that are actually useful, and a link that is merely missing is a link
# the user can paste in themselves.
JOB_LINK_HOSTS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "icims.com", "successfactors.com", "taleo.net",
    "bamboohr.com", "ashbyhq.com", "workable.com", "jobvite.com",
    "breezy.hr", "recruitee.com", "teamtailor.com", "applytojob.com",
    "zohorecruit.com", "pinpointhq.com", "freshteam.com",
    "linkedin.com", "indeed.com", "naukri.com", "glassdoor.com",
    "wellfound.com", "workatastartup.com", "instahyre.com", "hirist.com",
)

MAX_LINK_LENGTH = 2000

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# Unsubscribe footers and tracking pixels sit on the same hosts as real links
# on a few boards, so the path is checked as well as the host.
_LINK_NOISE = re.compile(
    r"(unsubscribe|optout|opt-out|/track|/click|/pixel|/beacon|"
    r"\.(png|jpe?g|gif|css|js)(\?|$))",
    re.IGNORECASE,
)


def _host_of(url: str) -> str:
    match = re.match(r"https?://([^/?#:]+)", url, re.IGNORECASE)
    return match.group(1).lower().strip(".") if match else ""


def is_job_link(url: str) -> bool:
    """True when a URL points at a job board or ATS and is not a tracker."""
    url = str(url or "").strip()

    if not url or len(url) > MAX_LINK_LENGTH or _LINK_NOISE.search(url):
        return False

    host = _host_of(url)
    return any(host == known or host.endswith("." + known) for known in JOB_LINK_HOSTS)


def find_job_link(text: str) -> str:
    """The first link in some text that points at a job posting, or "".

    Used on recruiter mail, so that an application the tracker learned about
    from an email still has somewhere to click through to. Everything the
    allowlist above does not recognise is discarded — see why there.
    """
    for match in _URL_RE.finditer(str(text or "")):
        url = match.group(0).rstrip(".,;:)]}>\"'")
        if is_job_link(url):
            return url

    return ""


def format_location(location: str = "", remote=False) -> str:
    """A place and a working arrangement as one string, or "".

    "Remote · Bengaluru" is a real and common combination — a remote role with a
    timezone or right-to-work anchor — so the two are shown together rather than
    one replacing the other.
    """
    place = normalise_location(location)
    parts = []

    if remote:
        parts.append("Remote")
    if place:
        parts.append(place)

    return " · ".join(parts)
