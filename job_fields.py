"""Validation and normalisation for the company/role pair.

Company and role arrive from two untrustworthy places: a scraper reading a job
board's markup, and a language model reading an email. Both fail the same way —
they hand back the *role* where the *company* belongs, because on a job page the
role is the most prominent string on screen.

Storing a role as a company is quietly destructive. The tracker's identity is
``(company, role)``, so a bad company means later emails about that application
cannot find their row and create a second one instead.

Everything here is pure: no I/O, no network. Both the API and the inbox sync
import it, so a value rejected in one path is rejected in the other.
"""

import re

# Words that make a string a job title rather than an organisation. Matched as
# whole words so "Engineering Ltd" or "Palantir Technologies" stay valid.
_ROLE_WORDS = {
    "administrator", "analyst", "apprentice", "architect", "assistant",
    "associate", "consultant", "contractor", "coordinator", "designer",
    "developer", "devops", "director", "engineer", "engineering",
    "executive", "freelance", "fresher", "graduate", "head", "intern",
    "internship", "lead", "manager", "officer", "operator", "president",
    "programmer", "recruiter", "researcher", "scientist", "specialist",
    "sre", "strategist", "technician", "trainee", "vp", "writer",
}

# Seniority and discipline words that only ever qualify a role. A string made
# up entirely of these plus role words is a job title, never a company.
_ROLE_QUALIFIERS = {
    "ai", "analytics", "android", "backend", "back", "big", "chief",
    "cloud", "data", "deep", "end", "front", "frontend", "full", "fullstack",
    "generative", "genai", "i", "ii", "iii", "intermediate", "ios", "junior",
    "machine", "ml", "mid", "mlops", "mobile", "of", "platform", "principal",
    "product", "project", "python", "qa", "quality", "senior", "sr", "staff",
    "stack", "system", "systems", "test", "and", "&", "-", "the", "a", "an",
    "learning", "security", "software", "solution", "solutions", "support",
    "web", "java", "javascript", "react", "node", "golang", "go", "rust",
    # Specialisations that trail a job title after a dash — "AI/ML Engineer –
    # Agentic AI" is one role, not a role at a company called Agentic AI.
    "agentic", "llm", "llms", "nlp", "vision", "robotics", "gen",
    "infrastructure", "reliability", "site", "embedded", "firmware",
    "blockchain", "quantitative", "quant", "research", "applied",
}

# Placeholders the scraper and the model emit when they find nothing.
_PLACEHOLDERS = {
    "", "-", "n/a", "na", "none", "null", "undefined", "unknown",
    "unknown company", "unknown role", "not specified", "not found",
    "company", "role", "job", "position", "career", "careers", "jobs",
    "apply", "application", "job application", "we are hiring", "hiring",
}

# Corporate suffixes that positively identify an organisation, overriding the
# role-word check: "Engineering Solutions Pvt Ltd" is a company.
#
# Deliberately excludes "software", "systems", "solutions", "services" and
# "consulting". Those read as corporate suffixes in "Acme Software" but as
# role qualifiers in "Software Engineer II", and treating them as decisive
# made every such job title validate as a company.
_COMPANY_MARKERS = {
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "plc", "gmbh", "corp",
    "corp.", "corporation", "co", "co.", "company", "pvt", "private",
    "technologies", "labs", "group", "holdings", "ventures", "partners",
    "industries", "sa", "ag", "bv", "nv", "ab", "oy", "srl", "spa", "pty",
}

# Applicant-tracking and mail-relay domains. Mail from these carries the ATS
# vendor's domain, not the hiring company's, so the domain says nothing about
# who the application is with.
ATS_DOMAINS = {
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "icims.com", "successfactors.com", "taleo.net",
    "bamboohr.com", "ashbyhq.com", "workable.com", "jobvite.com",
    "breezy.hr", "recruitee.com", "teamtailor.com", "personio.de",
    "hackerrank.com", "hackerearth.com", "codility.com", "karat.io",
    "linkedin.com", "indeed.com", "naukri.com", "glassdoor.com",
    "monster.com", "ziprecruiter.com", "dice.com", "wellfound.com",
    "angel.co", "instahyre.com", "cutshort.io", "hirist.com",
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "protonmail.com",
    "googlemail.com", "icloud.com", "live.com", "aol.com",
}

# Mail subdomains that sit in front of a real company domain.
_MAIL_SUBDOMAINS = {
    "mail", "email", "e", "em", "smtp", "mailer", "notifications", "notify",
    "no-reply", "noreply", "reply", "info", "news", "hire", "hiring", "jobs",
    "careers", "recruiting", "recruit", "talent", "apply", "auto", "bounce",
    "mktg", "marketing", "send", "sender", "track", "links", "click",
}

MAX_FIELD_LENGTH = 200


class InvalidJobField(ValueError):
    """Raised when a company or role value is unusable."""


def _words(value: str) -> list[str]:
    return [w for w in re.split(r"[\s/,|·–—-]+", value.lower().strip()) if w]


def is_placeholder(value: str) -> bool:
    """True when a value carries no information."""
    return normalize(value).lower() in _PLACEHOLDERS


def normalize(value: str) -> str:
    """Collapse whitespace and strip decoration, without changing the words."""
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    # Leading/trailing punctuation left behind by title splitting.
    cleaned = cleaned.strip(" \t-–—|·,:;. ")
    return cleaned


def looks_like_role(value: str) -> bool:
    """True when a string reads as a job title rather than an organisation.

    The test is deliberately conservative: a real company name is allowed
    through even when it contains a role word, because rejecting a genuine
    company is worse than accepting an occasional bad one. A string only fails
    when it contains a role word *and* every other word is a qualifier — that
    is, when there is nothing left that could be a company name.
    """
    cleaned = normalize(value)
    if not cleaned:
        return False

    words = _words(cleaned)
    if not words:
        return False

    # An explicit corporate suffix settles it: this is an organisation.
    if any(word.strip(".") in _COMPANY_MARKERS for word in words):
        return False

    if not any(word in _ROLE_WORDS for word in words):
        # No role word, but a multi-word string made entirely of seniority and
        # technology qualifiers is a specialisation rather than an employer:
        # "Indeed Application: AI/ML Engineer – Agentic AI" yields "Agentic AI",
        # which is the tail of the job title.
        #
        # The trade-off is deliberate. A real company called "Data Systems"
        # would be rejected — but such a name almost always appears with a
        # suffix ("Pvt Ltd", "Inc") in a recruiter email, which the marker
        # check above already accepts. Skipping an email is recoverable and
        # logged; a wrong company silently splits an application in two.
        return len(words) >= 2 and all(word in _ROLE_QUALIFIERS for word in words)

    # Every word is either a role word or a qualifier -> nothing identifies a
    # company, so the string is a bare job title.
    return all(word in _ROLE_WORDS or word in _ROLE_QUALIFIERS for word in words)


def validate_company(company: str, role: str = "") -> str:
    """Return a clean company name, or raise ``InvalidJobField``.

    ``role`` is optional context: when company and role are the same string,
    the extractor found one value and used it for both, which is always wrong.
    """
    cleaned = normalize(company)

    if not cleaned:
        raise InvalidJobField("Company name is required.")

    if len(cleaned) > MAX_FIELD_LENGTH:
        raise InvalidJobField(f"Company name is longer than {MAX_FIELD_LENGTH} characters.")

    if is_placeholder(cleaned):
        raise InvalidJobField(
            f"{cleaned!r} is a placeholder, not a company name."
        )

    # Checked before the equality test below: when the value is a job title,
    # saying so is more useful than reporting that two fields happen to match.
    if looks_like_role(cleaned):
        raise InvalidJobField(
            f"{cleaned!r} is a job title, not a company. "
            "The job page or email did not name the employer."
        )

    if role and cleaned.casefold() == normalize(role).casefold():
        raise InvalidJobField(
            f"Company and role are both {cleaned!r} — the job page was misread."
        )

    return cleaned


def validate_role(role: str) -> str:
    """Return a clean role, or raise ``InvalidJobField``.

    Roles are held to a lower bar than companies: an unnamed role is recorded
    as ``Unknown Role`` elsewhere rather than rejected, because an application
    with a known employer is still worth tracking.
    """
    cleaned = normalize(role)

    if not cleaned:
        raise InvalidJobField("Role is required.")

    if len(cleaned) > MAX_FIELD_LENGTH:
        raise InvalidJobField(f"Role is longer than {MAX_FIELD_LENGTH} characters.")

    return cleaned


def company_from_email_domain(sender: str) -> str:
    """Guess a company name from a sender address, or return "".

    The sending domain is the single most reliable company signal in a
    recruiter email — far steadier than anything in the subject line, which is
    usually dominated by the role. It is used as a hint to the classifier and
    as a fallback when the model cannot name the employer.

    Returns "" for ATS vendors and consumer mail providers, where the domain
    belongs to the sender's tooling rather than to the hiring company.
    """
    match = re.search(r"[\w.+-]+@([\w.-]+)", str(sender or ""))
    if not match:
        return ""

    host = match.group(1).lower().strip(".")
    labels = host.split(".")

    # Drop mail-infrastructure subdomains: notifications.acme.com -> acme.com
    while len(labels) > 2 and labels[0] in _MAIL_SUBDOMAINS:
        labels = labels[1:]

    host = ".".join(labels)

    # Compare against the registrable domain so hire.lever.co is recognised
    # as Lever even after the subdomain strip leaves lever.co.
    for known in ATS_DOMAINS:
        if host == known or host.endswith("." + known):
            return ""

    if len(labels) < 2:
        return ""

    # Second-level domains like co.uk / co.in leave the name one label further
    # left: careers@acme.co.in -> acme
    name = labels[0] if labels[0] not in {"www"} else labels[1]
    if len(labels) >= 3 and labels[-2] in {"co", "com", "net", "org", "gov", "ac"}:
        name = labels[-3]
    elif len(labels) >= 2:
        name = labels[-2]

    if not name or len(name) < 2 or name in _MAIL_SUBDOMAINS:
        return ""

    return prettify_slug(name)


def prettify_slug(slug: str) -> str:
    """Turn a URL or domain slug into a display name: acme-robotics -> Acme Robotics."""
    return " ".join(
        part.capitalize() for part in re.split(r"[-_]+", str(slug or "")) if part
    )


# ATS boards that put the employer in the FIRST PATH SEGMENT:
#   boards.greenhouse.io/nexuslabs/jobs/1  ->  nexuslabs
_ATS_PATH_HOSTS = {
    "boards.greenhouse.io", "job-boards.greenhouse.io", "greenhouse.io",
    "jobs.lever.co", "lever.co",
    "jobs.ashbyhq.com", "ashbyhq.com",
    "apply.workable.com",
    "jobs.smartrecruiters.com", "careers.smartrecruiters.com",
    "jobs.jobvite.com",
}

# ATS boards that put the employer in the SUBDOMAIN:
#   nexuslabs.workable.com/j/ABC  ->  nexuslabs
_ATS_SUBDOMAIN_HOSTS = (
    "workable.com", "recruitee.com", "bamboohr.com", "teamtailor.com",
    "breezy.hr", "myworkdayjobs.com", "applytojob.com", "freshteam.com",
)

# Boards where the employer simply is not in the URL. Guessing from these
# would produce "Linkedin" as a company, which is worse than admitting defeat.
_OPAQUE_HOSTS = (
    "linkedin.com", "indeed.com", "naukri.com", "glassdoor.com",
    "monster.com", "ziprecruiter.com", "dice.com", "instahyre.com",
    "cutshort.io", "hirist.com", "google.com", "bing.com",
)


def company_from_url(url: str) -> str:
    """Guess the employer from a job posting URL, or return "".

    Used to repair rows whose company was mis-scraped: the saved link is
    independent evidence of who the application was with, and it survives the
    page markup changing.

    Each board hides the employer somewhere different — a path segment, a
    subdomain, or not at all — so there is no single rule to apply.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""

    match = re.match(r"^(?:https?://)?([^/?#]+)([^?#]*)", raw, re.IGNORECASE)
    if not match:
        return ""

    host = match.group(1).lower().split(":")[0].strip(".")
    segments = [seg for seg in match.group(2).split("/") if seg]

    if any(host == h or host.endswith("." + h) for h in _OPAQUE_HOSTS):
        return ""

    # wellfound.com/company/<slug>/jobs/...
    if "wellfound.com" in host or "angel.co" in host:
        if len(segments) >= 2 and segments[0] == "company":
            return prettify_slug(segments[1])
        return ""

    if host in _ATS_PATH_HOSTS or any(
        host.endswith("." + h) for h in _ATS_PATH_HOSTS
    ):
        # Skip routing prefixes that sit before the employer slug.
        for segment in segments:
            if segment.lower() in {"embed", "job", "jobs", "o", "j", "careers"}:
                continue
            return prettify_slug(segment)
        return ""

    for suffix in _ATS_SUBDOMAIN_HOSTS:
        if host.endswith("." + suffix):
            sub = host[: -(len(suffix) + 1)].split(".")[0]
            # Workday hosts look like acme.wd1.myworkdayjobs.com; the leading
            # label is still the tenant.
            if sub and sub not in {"www", "apply", "jobs", "careers"}:
                return prettify_slug(sub)
            return ""

    # An ordinary company career page: careers.nexuslabs.com -> Nexus Labs.
    # Reuses the email path by presenting the host as an address.
    return company_from_email_domain(f"x@{host}")
