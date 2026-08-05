"""Gmail inbox access, scoped per user.

Each user authorises their own mailbox; the resulting OAuth token is stored
inside that user's workspace rather than in a shared token.json at the repo
root.
"""

import os
import sys

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
def build_auth_url(user_id: int) -> tuple[str, str]:
    """Return ``(authorization_url, state)`` for the hosted consent flow.

    The caller stores ``state`` and must pass it back to
    :func:`complete_auth`, which is what stops one user's callback being
    replayed to attach a mailbox to somebody else's account.
    """
    _require_credentials_file()

    if not IS_HOSTED:
        raise GmailAuthError("PUBLIC_URL is not configured for this instance.")

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

    return authorization_url, state


def complete_auth(user_id: int, code: str, state: str) -> None:
    """Exchange an authorisation code for credentials and store them."""
    _require_credentials_file()

    flow = Flow.from_client_secrets_file(
        str(GMAIL_CREDENTIALS_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri(),
        state=state,
    )

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


def is_high_probability_job_email(sender: str, subject: str, snippet: str) -> bool:
    """Cheap rule engine that filters out noise before paying for an AI call."""
    sender_lower = (sender or "").lower()
    content_lower = f"{subject or ''} {snippet or ''}".lower()
    combined = f"{sender_lower} {content_lower}"

    # Blacklist wins over every other signal — marketing blasts often contain
    # the same words as genuine recruiter mail.
    blacklist = [
        "newsletter",
        "job alert",
        "job alerts",
        "digest",
        "marketing",
        "weekly",
        "campaign",
        "unsubscribe from job",
        "promotions",
    ]
    if any(word in combined for word in blacklist):
        return False

    ats_domains = [
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
    if any(domain in sender_lower for domain in ats_domains):
        return True

    human_indicators = [
        "talent@",
        "careers@",
        "recruiting@",
        "recruiter@",
        "hiring@",
        "hr@",
        "peopleops@",
        "no-reply@",
    ]
    if any(indicator in sender_lower for indicator in human_indicators):
        return True

    # Phrases rather than bare words: a lone "offer" also appears in "limited
    # time offer", which is exactly the marketing mail this filter exists to
    # keep out.
    high_signal_phrases = [
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
    return any(phrase in content_lower for phrase in high_signal_phrases)


def fetch_job_emails(user_id: int, max_results: int = None) -> list[dict]:
    """Fetch recent mail, filter it, and return the likely job-related messages.

    Each entry includes the Gmail ``id`` so the caller can skip messages it has
    already classified.
    """
    service = authenticate_gmail(user_id, allow_interactive=False)
    max_results = max_results or GMAIL_MAX_RESULTS

    search_query = (
        "subject:(application OR update OR status OR role OR position OR interview) "
        f"newer_than:{GMAIL_LOOKBACK_DAYS}d"
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
            return []

        valid_emails = []

        for msg in messages:
            msg_data = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From"],
                )
                .execute()
            )

            headers = msg_data["payload"]["headers"]
            subject = _header(headers, "Subject", "No Subject")
            sender = _header(headers, "From", "Unknown Sender")
            snippet = msg_data.get("snippet", "")

            if is_high_probability_job_email(sender, subject, snippet):
                valid_emails.append(
                    {
                        "id": msg["id"],
                        "sender": sender,
                        "subject": subject,
                        "snippet": snippet,
                    }
                )

        logger.info(
            "Gmail scan for user %s: %s fetched, %s passed the filter",
            user_id,
            len(messages),
            len(valid_emails),
        )
        return valid_emails

    except GmailAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - network/API errors are reported upward
        logger.error("Gmail fetch failed for user %s: %s", user_id, exc)
        raise RuntimeError(f"Could not read the inbox: {exc}") from exc


def _header(headers: list[dict], name: str, default: str) -> str:
    return next(
        (header["value"] for header in headers if header["name"] == name), default
    )
