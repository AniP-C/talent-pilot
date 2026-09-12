"""Gmail inbox access, scoped per user.

Each user authorises their own mailbox; the resulting OAuth token is stored
inside that user's workspace rather than in a shared token.json at the repo
root.
"""

import base64
import binascii
import json
import os
import re
import secrets
import sys
import time
from html import unescape
from typing import NamedTuple, Optional

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workspace
from config import (
    GMAIL_CREDENTIALS_PATH,
    GMAIL_LOOKBACK_DAYS,
    GMAIL_MAX_RESULTS,
    IS_HOSTED,
    PUBLIC_URL,
    logger,
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Google redirects back here after consent. It must be registered verbatim as
# an authorised redirect URI in the Google Cloud console.
OAUTH_REDIRECT_PATH = "/"


class GmailAuthError(Exception):
    """Raised when a user's mailbox cannot be authorised."""


def redirect_uri() -> str:
    """The callback URL Google should return the user to."""
    return f"{PUBLIC_URL}{OAUTH_REDIRECT_PATH}"


def has_credentials_file() -> bool:
    """True when the Google OAuth client secret is available."""
    return GMAIL_CREDENTIALS_PATH.exists()


def is_connected(user_id: int) -> bool:
    """True when this user already has a stored Gmail token."""
    return workspace.gmail_token_path(user_id).exists()


def disconnect(user_id: int) -> None:
    """Forget a user's Gmail authorisation."""
    token_path = workspace.gmail_token_path(user_id)
    if token_path.exists():
        token_path.unlink()
        logger.info("Disconnected Gmail for user %s", user_id)


def authenticate_gmail(user_id: int, allow_interactive: bool = True):
    """Return an authorised Gmail service for one user.

    ``allow_interactive=False`` refuses to open a browser consent window,
    which is what the API server wants — a background request must never
    block waiting for someone to click through a Google login.

    When the app is hosted (``PUBLIC_URL`` set), the interactive desktop flow
    is impossible: ``run_local_server`` would open a browser on the *server*.
    Hosted deployments must use :func:`build_auth_url` and
    :func:`complete_auth` instead.
    """
    token_path = workspace.gmail_token_path(user_id)
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                token_path.unlink(missing_ok=True)
                raise GmailAuthError(
                    "Your Gmail authorisation expired. Please reconnect."
                ) from exc
        else:
            if not allow_interactive:
                raise GmailAuthError("Gmail is not connected for this account.")

            _require_credentials_file()

            if IS_HOSTED:
                raise GmailAuthError(
                    "Use the Connect Gmail link to authorise this hosted instance."
                )

            # Local desktop flow: opens a browser on this machine.
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        _save_credentials(user_id, creds)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# =====================================================================
# HOSTED (REDIRECT) OAUTH FLOW
# =====================================================================
# How long a pending consent request stays valid.
STATE_TTL_SECONDS = 900


def _state_path(user_id: int):
    return workspace.workspace_dir(user_id) / "gmail_oauth_state.json"


def _load_pending_state(user_id: int) -> Optional[dict]:
    """Return the pending consent request for a user, if still valid.

    Carries both the ``state`` and the PKCE ``code_verifier``: the verifier is
    generated when the consent URL is built and must be presented again at
    token exchange, or Google rejects it with "Missing code verifier".
    """
    path = _state_path(user_id)

    if not path.exists():
        return None

    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if time.time() > stored.get("expires_at", 0):
        path.unlink(missing_ok=True)
        return None

    return stored if stored.get("state") else None


def build_auth_url(user_id: int) -> str:
    """Return the Google consent URL for this user.

    The ``state`` is stored in the user's workspace rather than in the web
    session: Google redirects back as a fresh page load, which starts a new
    session, so anything held in memory would already be gone by the time
    the callback arrives.

    A still-valid pending state is reused so that re-rendering the page does
    not invalidate a consent link the user is about to click.
    """
    _require_credentials_file()

    if not IS_HOSTED:
        raise GmailAuthError("PUBLIC_URL is not configured for this instance.")

    existing = _load_pending_state(user_id)

    # Reuse a live pending request verbatim. Rebuilding it would mint a fresh
    # state and PKCE verifier, silently invalidating the link already on
    # screen that the user is about to click.
    if existing and existing.get("authorization_url"):
        return existing["authorization_url"]

    flow = Flow.from_client_secrets_file(
        str(GMAIL_CREDENTIALS_PATH), scopes=SCOPES, redirect_uri=redirect_uri()
    )

    authorization_url, state = flow.authorization_url(
        # offline + consent are what actually yield a refresh token; without
        # them the connection silently dies after an hour.
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    _state_path(user_id).write_text(
        json.dumps(
            {
                "state": state,
                # Set by authorization_url() when PKCE is in play; fetch_token
                # needs the identical value back.
                "code_verifier": flow.code_verifier,
                "authorization_url": authorization_url,
                "expires_at": time.time() + STATE_TTL_SECONDS,
            }
        ),
        encoding="utf-8",
    )

    return authorization_url


def complete_auth(user_id: int, code: str, state: str) -> None:
    """Exchange an authorisation code for credentials and store them.

    Verifies ``state`` against the value recorded when the consent URL was
    built, which is what stops one user's callback attaching a mailbox to
    somebody else's account.
    """
    _require_credentials_file()

    pending = _load_pending_state(user_id)
    _state_path(user_id).unlink(missing_ok=True)

    if not pending or not secrets.compare_digest(pending["state"], state or ""):
        logger.warning("Gmail callback state mismatch for user %s", user_id)
        raise GmailAuthError(
            "That authorisation did not match a pending request. Please try again."
        )

    flow = Flow.from_client_secrets_file(
        str(GMAIL_CREDENTIALS_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri(),
        state=state,
    )

    # Restored from the pending request; without it Google rejects the
    # exchange with "Missing code verifier".
    flow.code_verifier = pending.get("code_verifier")

    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - oauthlib raises many types
        logger.error("Gmail token exchange failed for user %s: %s", user_id, exc)
        raise GmailAuthError(
            "Google rejected that authorisation. Please try connecting again."
        ) from exc

    _save_credentials(user_id, flow.credentials)
    logger.info("Gmail connected for user %s via hosted flow", user_id)


# =====================================================================
# HELPERS
# =====================================================================
def _require_credentials_file() -> None:
    if not has_credentials_file():
        raise GmailAuthError(
            f"Missing {GMAIL_CREDENTIALS_PATH.name}. Download the OAuth client "
            "secret from the Google Cloud console and place it in the project root."
        )


def _save_credentials(user_id: int, creds) -> None:
    """Persist credentials into the user's own workspace."""
    workspace.gmail_token_path(user_id).write_text(creds.to_json(), encoding="utf-8")


def mailbox_address(user_id: int) -> str:
    """The address of the mailbox being read, or "" if it cannot be determined.

    Needed to keep the user out of their own contact fields. Their address is
    in almost every recruiter email — in ``To``, in ``Cc``, and quoted in the
    body as "we received your application from …" — so without knowing it, the
    first address found in a message is as likely to be the user as the
    recruiter.

    Best-effort by design: a failure here means one fewer exclusion, not a
    failed sync.
    """
    try:
        service = authenticate_gmail(user_id, allow_interactive=False)
        return service.users().getProfile(userId="me").execute().get("emailAddress", "")
    except Exception as exc:  # noqa: BLE001 - advisory only
        logger.info("Could not read the mailbox address for user %s: %s", user_id, exc)
        return ""


# Bulk mail declares itself in who sent it and what the subject says. Matched
# against those two only — never the body.
#
# This list used to be bare words checked against the sender, the subject and
# the first 1500 characters of the body together, which made three separate
# ways to lose a real application:
#
#   "weekly"    — a recruiter proposing a weekly sync, or any ATS footer
#                 mentioning a weekly anything, took the whole message down.
#   "marketing"
#   "campaign"  — both are job titles. Anyone applying for a marketing role had
#                 every confirmation about it rejected before it was read.
#   body text   — the blacklist wins over every other signal, so one wrong word
#                 in a template footer outranked "Your application" in the
#                 subject and an ATS domain in the sender.
#
# Phrases, therefore, and only ones that cannot also be someone's job.
MARKETING_PHRASES = [
    "newsletter",
    "marketing@",
    "promotions@",
    "job alert",
    "job alerts",
    "jobs for you",
    "job digest",
    "daily digest",
    "weekly digest",
    "weekly roundup",
    "weekly job",
]

# The exception, matched against the body as well: phrases that cannot occur in
# a genuine one-to-one recruiter email, only in the footer of a blast.
MARKETING_BODY_MARKERS = [
    "unsubscribe from job",
    "unsubscribe from these job",
    "manage your job alerts",
]

ATS_DOMAINS = [
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "icims.com",
    "successfactors.com",
    "taleo.net",
    "bamboohr.com",
    "ashbyhq.com",
    "workable.com",
]

RECRUITING_ADDRESSES = [
    "talent@",
    "careers@",
    "recruiting@",
    "recruiter@",
    "hiring@",
    "hr@",
    "peopleops@",
    "no-reply@",
]

# Phrases rather than bare words: a lone "offer" also appears in "limited
# time offer", which is exactly the marketing mail this filter exists to
# keep out.
HIGH_SIGNAL_PHRASES = [
    "application",
    "interview",
    "assessment",
    "job offer",
    "offer letter",
    "extend an offer",
    "pleased to offer",
    "candidate",
    "you applied",
    "moving forward",
    "next steps",
    "hiring team",
]


class Screening(NamedTuple):
    """One bouncer verdict, and the rule that produced it.

    The reason is the whole point. This filter runs before any model call and
    drops silently, so a genuine recruiter email it rejects simply never
    happens as far as the rest of the system is concerned — and until this
    existed there was no way, even in principle, to find out which ones.
    """

    passed: bool
    reason: str


def sender_address(sender: str) -> str:
    """The bare address out of a From header, lowercased."""
    match = re.search(r"<([^>]+)>", sender or "")
    return (match.group(1) if match else (sender or "")).strip().lower()


def is_own_mail(sender: str, own_address: str) -> bool:
    """Whether a message was sent by the account being synced."""
    own = (own_address or "").strip().lower()
    return bool(own) and sender_address(sender) == own


# "Re:", "Fwd:", "FW:" — including the localised prefixes Gmail writes when the
# thread started in another language, which are still followed by a colon.
_THREAD_PREFIX = re.compile(r"^\s*(re|fwd?|fw|aw|sv|res)\s*(\[\d+\])?\s*:", re.IGNORECASE)


def screen_email(
    sender: str, subject: str, snippet: str, own_address: str = ""
) -> Screening:
    """Cheap rule engine that filters out noise before paying for an AI call.

    Returns the verdict and the rule behind it, so a drop can be reported to
    the person whose mail it was rather than only counted.
    """
    sender_lower = (sender or "").lower()
    content_lower = f"{subject or ''} {snippet or ''}".lower()
    # The body reaches this function as part of ``snippet``, which is what lets
    # a bland subject still pass on its contents. The blacklist deliberately
    # does not see it: a footer is not what a message is about.
    header_lower = f"{sender_lower} {(subject or '').lower()}"

    # Mail the user sent, of which there are two kinds and only one is waste.
    #
    # Their replies on a recruiter's thread match every content rule below —
    # they are about an application, they quote the thread — and carry nothing
    # the recruiter's own mail will not say again, so classifying them costs a
    # model call to be told they are not an update. Those are dropped here.
    #
    # An application sent *by* mail is the opposite: where somebody applies by
    # writing to a careers address, that message is the only evidence the
    # application exists, and dropping it meant the tracker never heard of the
    # job at all. So it falls through to the rules below and is judged on its
    # contents like any other message — "Application for X" carries the same
    # high-signal phrase a confirmation does.
    if is_own_mail(sender, own_address) and _THREAD_PREFIX.match(subject or ""):
        return Screening(False, "your own reply on an existing thread")

    # Bulk mail wins over every other signal — a marketing blast from an ATS
    # domain is still a marketing blast.
    for phrase in MARKETING_PHRASES:
        if phrase in header_lower:
            return Screening(False, f"bulk mail: {phrase!r} in the sender or subject")

    for phrase in MARKETING_BODY_MARKERS:
        if phrase in content_lower:
            return Screening(False, f"bulk mail: {phrase!r}")

    for domain in ATS_DOMAINS:
        if domain in sender_lower:
            return Screening(True, f"ATS sender {domain}")

    for indicator in RECRUITING_ADDRESSES:
        if indicator in sender_lower:
            return Screening(True, f"recruiting address {indicator}")

    for phrase in HIGH_SIGNAL_PHRASES:
        if phrase in content_lower:
            return Screening(True, f"phrase {phrase!r}")

    return Screening(False, "no recruiting signal in the sender or the text")


def is_high_probability_job_email(sender: str, subject: str, snippet: str) -> bool:
    """Whether the bouncer lets this message through. See ``screen_email``."""
    return screen_email(sender, subject, snippet).passed


# The search that decides which mail is even considered.
#
# The previous version required one of six words in the *subject*, which threw
# away most real recruiter mail: "Thank you for your interest", "Next steps",
# "Congratulations!" and "Your offer letter" all match none of them, and a
# message never fetched can never be classified.
#
# Broadening the net does not increase AI cost. Two things bound it: the rule
# filter below runs before any model call and is free, and GMAIL_MAX_RESULTS
# caps how many messages a single sync will look at no matter how many match.
_SUBJECT_TERMS = (
    "application OR applying OR applied OR candidacy OR candidate OR "
    "interview OR assessment OR offer OR opportunity OR recruiter OR "
    "recruitment OR hiring OR role OR position OR opening OR vacancy OR "
    'status OR update OR "next steps" OR "thank you for" OR shortlisted OR '
    "congratulations OR onboarding"
)

# Senders that are nearly always about an application regardless of subject.
_SENDER_TERMS = (
    "greenhouse.io OR lever.co OR myworkdayjobs.com OR smartrecruiters.com OR "
    "icims.com OR successfactors.com OR taleo.net OR bamboohr.com OR "
    "ashbyhq.com OR workable.com OR jobvite.com OR hackerrank.com OR "
    "careers OR recruiting OR talent"
)

# How much message body to keep. The body is where the company name usually
# lives — the old metadata-only fetch meant a Lever or Greenhouse confirmation
# arrived with nothing but a role in it, and was discarded as "Unknown".
# Capped because prompt size drives token cost, and the useful content of a
# recruiter email is always near the top.
MAX_BODY_CHARS = 1500


def _decode_part(data: str) -> str:
    """Decode one base64url MIME part, tolerating Gmail's padding."""
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


def _extract_body(payload: dict) -> str:
    """Pull readable text out of a Gmail payload.

    Walks the MIME tree preferring text/plain; falls back to text/html with
    the tags stripped, because plenty of recruiter mail is HTML-only.
    """
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}

        if mime == "text/plain":
            plain.append(_decode_part(body.get("data", "")))
        elif mime == "text/html":
            html.append(_decode_part(body.get("data", "")))

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)

    text = "\n".join(filter(None, plain)).strip()

    if not text and html:
        raw = "\n".join(filter(None, html))
        # Drop script/style wholesale before stripping the remaining tags, or
        # their contents survive as noise.
        raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        text = unescape(raw)

    # Quoted replies and signature blocks add tokens without adding signal.
    text = re.split(r"\n-{2,}\s*\n|\nOn .{0,80} wrote:", text)[0]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text[:MAX_BODY_CHARS]


class Scan(NamedTuple):
    """What one mailbox scan saw, kept and threw away.

    ``dropped`` is returned rather than logged here so the caller decides how
    it is reported: the sync controller owns the run id every line is tagged
    with, and this module has no business knowing about it.
    """

    emails: list[dict]
    dropped: list[dict]

    @property
    def considered(self) -> int:
        return len(self.emails) + len(self.dropped)


def fetch_job_emails(
    user_id: int, max_results: int = None, own_address: str = ""
) -> Scan:
    """Fetch recent mail, filter it, and return the likely job-related messages.

    Returned **oldest first**. Gmail lists newest first, but an application
    moves forward through time, so replaying a batch in arrival order is what
    lets the tracker end on the latest state instead of the earliest.

    Each entry includes the Gmail ``id`` so the caller can skip messages it has
    already classified, and the ``Reply-To``, ``Cc`` and ``To`` headers, which
    is where the human behind an ATS relay is usually named.

    Everything the bouncer rejected comes back in ``dropped``, with the rule
    that rejected it. A filter that runs before any model call and leaves no
    trace is a filter nobody can correct.
    """
    service = authenticate_gmail(user_id, allow_interactive=False)
    max_results = max_results or GMAIL_MAX_RESULTS

    search_query = (
        f"(subject:({_SUBJECT_TERMS}) OR from:({_SENDER_TERMS})) "
        f"newer_than:{GMAIL_LOOKBACK_DAYS}d "
        # Categories Gmail has already judged to be bulk mail. Excluding them
        # here is free and removes most of what the rule filter would drop.
        "-category:promotions -category:social -in:chats"
    )

    try:
        results = (
            service.users()
            .messages()
            .list(userId="me", q=search_query, maxResults=max_results)
            .execute()
        )
        messages = results.get("messages", [])

        if not messages:
            logger.info("No candidate emails found for user %s", user_id)
            return Scan([], [])

        valid_emails = []
        dropped = []

        for msg in messages:
            msg_data = (
                service.users()
                .messages()
                .get(userId="me", id=msg["id"], format="full")
                .execute()
            )

            payload = msg_data.get("payload", {}) or {}
            headers = payload.get("headers", [])
            subject = _header(headers, "Subject", "No Subject")
            sender = _header(headers, "From", "Unknown Sender")
            snippet = msg_data.get("snippet", "")
            body = _extract_body(payload)

            # The filter sees the body too, so a genuine recruiter mail whose
            # subject is bland is no longer judged on the subject alone.
            verdict = screen_email(
                sender, subject, f"{snippet}\n{body}", own_address=own_address
            )

            if not verdict.passed:
                # Kept whole rather than counted. "18 fetched, 34 considered"
                # tells the user a filter exists; naming the message and the
                # rule tells them whether it was right.
                dropped.append(
                    {
                        "id": msg["id"],
                        "sender": sender,
                        "subject": subject,
                        "reason": verdict.reason,
                    }
                )
                continue

            valid_emails.append(
                {
                    "id": msg["id"],
                    "sender": sender,
                    # An ATS sends as no-reply@vendor and sets Reply-To to the
                    # recruiter who actually owns the requisition. It is the
                    # single best "who do I answer?" signal in the message,
                    # and was previously read and discarded.
                    "reply_to": _header(headers, "Reply-To", ""),
                    # Cc frequently carries the second recruiter on a thread
                    # or the hiring manager being looped in.
                    "cc": _header(headers, "Cc", ""),
                    "to": _header(headers, "To", ""),
                    "subject": subject,
                    "snippet": snippet,
                    "body": body,
                    # Epoch milliseconds, as a string, straight from Gmail.
                    "internal_date": int(msg_data.get("internalDate", 0)),
                }
            )

        # Oldest first: see the docstring. Without this the last email applied
        # is the earliest one, so an offer gets overwritten by the original
        # "we received your application".
        valid_emails.sort(key=lambda email: email["internal_date"])

        logger.info(
            "Gmail scan for user %s: %s fetched, %s passed the filter, %s dropped",
            user_id,
            len(messages),
            len(valid_emails),
            len(dropped),
        )
        return Scan(valid_emails, dropped)

    except GmailAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - network/API errors are reported upward
        logger.error("Gmail fetch failed for user %s: %s", user_id, exc)
        raise RuntimeError(f"Could not read the inbox: {exc}") from exc


def _header(headers: list[dict], name: str, default: str) -> str:
    """Case-insensitive header lookup — Gmail does not normalise casing."""
    target = name.lower()
    return next(
        (h["value"] for h in headers if h.get("name", "").lower() == target), default
    )
