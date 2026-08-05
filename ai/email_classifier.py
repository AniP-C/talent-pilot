"""Gemini-backed classification of recruiter emails into tracker statuses."""

import os
import sys
from enum import Enum

from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.gemini import generate_structured
from config import VALID_STATUSES


class EmailCategory(str, Enum):
    """Categories the model may return.

    These are the model's vocabulary, not the database's — `to_status()` maps
    them onto config.VALID_STATUSES.
    """

    RECEIVED = "RECEIVED"                # Application confirmation
    REJECTED = "REJECTED"                # Moving forward with other candidates
    ASSESSMENT = "ASSESSMENT"            # HackerRank, OA, take-home
    INTERVIEW = "INTERVIEW"              # HR or technical round
    OFFER = "OFFER"                      # Selected, CTC, onboarding
    ACTION_REQUIRED = "ACTION_REQUIRED"  # Background check, missing documents
    UNKNOWN = "UNKNOWN"                  # Newsletters and other noise


# A confirmation email means the application is simply logged, which the
# tracker already calls APPLIED. Everything else maps onto itself.
_CATEGORY_TO_STATUS = {
    EmailCategory.RECEIVED.value: "APPLIED",
}


def to_status(category: str) -> str | None:
    """Map a model category onto a tracker status, or None if it is not one.

    Returning None (rather than guessing) is what lets the sync loop skip
    UNKNOWN emails instead of writing junk rows.
    """
    normalized = (category or "").strip().upper()
    mapped = _CATEGORY_TO_STATUS.get(normalized, normalized)
    return mapped if mapped in VALID_STATUSES else None


class EmailAnalysis(BaseModel):
    category: EmailCategory
    company_name: str
    reasoning: str


def classify_email(sender: str, subject: str, snippet: str) -> dict:
    """Classify one email. Returns the parsed analysis or an ``error`` dict."""
    prompt = f"""
    You are an expert ATS and Recruitment Assistant.
    Analyze the following email sent to a job applicant.

    1. Categorize the email strictly into one of the allowed categories.
    2. Extract the company the email is about. If it is a job board blast
       (Indeed, Naukri, LinkedIn alerts) or the company is unclear, use "Unknown".
    3. Provide a one-sentence reason for the classification.

    EMAIL SENDER: {sender}
    EMAIL SUBJECT: {subject}
    EMAIL SNIPPET: {snippet}
    """
    return generate_structured(prompt, EmailAnalysis, "EMAIL_CLASSIFICATION")
