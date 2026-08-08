"""Gmail payload parsing and message ordering. No network calls.

The pipeline used to fetch ``format="metadata"``, so the classifier saw only a
~200 character snippet. The company name usually sits in the body, which meant
genuine ATS confirmations were discarded as "Unknown" and — because they were
still marked processed — never looked at again.
"""

import base64

from integrations.gmail_client import (
    MAX_BODY_CHARS,
    _extract_body,
    _header,
    is_high_probability_job_email,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# =====================================================================
# HEADERS
# =====================================================================
def test_header_lookup_is_case_insensitive():
    """Gmail does not normalise header casing."""
    headers = [{"name": "subject", "value": "Interview"}, {"name": "FROM", "value": "a@b.com"}]

    assert _header(headers, "Subject", "none") == "Interview"
    assert _header(headers, "From", "none") == "a@b.com"


def test_missing_header_falls_back():
    assert _header([], "Subject", "No Subject") == "No Subject"


# =====================================================================
# BODY EXTRACTION
# =====================================================================
def test_plain_text_body_is_decoded():
    payload = {"mimeType": "text/plain", "body": {"data": _b64("Hello from Stripe")}}

    assert _extract_body(payload) == "Hello from Stripe"


def test_multipart_prefers_plain_text():
    payload = {
        "mimeType": "multipart/alternative",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain version")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>html version</p>")}},
        ],
    }

    assert _extract_body(payload) == "plain version"


def test_html_only_body_is_stripped_to_text():
    """Plenty of recruiter mail is HTML-only."""
    html = "<html><body><p>Thanks for applying to <b>Stripe</b>.</p></body></html>"
    payload = {"mimeType": "text/html", "body": {"data": _b64(html)}}

    result = _extract_body(payload)

    assert "Thanks for applying to" in result
    assert "Stripe" in result
    assert "<" not in result


def test_script_and_style_contents_are_dropped():
    """Otherwise their contents survive tag-stripping as noise."""
    html = "<style>.a{color:red}</style><script>var x=1;</script><p>Real content</p>"
    payload = {"mimeType": "text/html", "body": {"data": _b64(html)}}

    result = _extract_body(payload)

    assert "Real content" in result
    assert "color:red" not in result
    assert "var x" not in result


def test_html_entities_are_unescaped():
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64("<p>Ben &amp; Jerry&#39;s</p>")},
    }

    assert "Ben & Jerry's" in _extract_body(payload)


def test_nested_parts_are_walked():
    payload = {
        "mimeType": "multipart/mixed",
        "body": {},
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "body": {},
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("nested text")}}
                ],
            }
        ],
    }

    assert _extract_body(payload) == "nested text"


def test_quoted_reply_chain_is_trimmed():
    """Quoted history adds tokens without adding signal."""
    body = "Here are the next steps.\nOn Mon 1 Jan someone wrote:\n> old message"
    payload = {"mimeType": "text/plain", "body": {"data": _b64(body)}}

    result = _extract_body(payload)

    assert "next steps" in result
    assert "old message" not in result


def test_body_is_capped():
    """Prompt size drives token cost; the useful part is near the top."""
    payload = {"mimeType": "text/plain", "body": {"data": _b64("x" * 10_000)}}

    assert len(_extract_body(payload)) == MAX_BODY_CHARS


def test_malformed_base64_does_not_raise():
    payload = {"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}}

    assert isinstance(_extract_body(payload), str)


def test_empty_payload_is_empty_string():
    assert _extract_body({}) == ""


# =====================================================================
# THE RULE FILTER, NOW SEEING THE BODY
# =====================================================================
def test_a_bland_subject_passes_on_body_content():
    """The filter used to judge on subject and snippet alone, so a genuine
    update titled "Thank you for your interest" was thrown away."""
    assert is_high_probability_job_email(
        "someone@nexuslabs.com",
        "Thank you for your interest",
        "We reviewed your application and would like to schedule an interview.",
    ) is True


def test_marketing_is_still_filtered_out():
    assert is_high_probability_job_email(
        "deals@shop.com", "Weekly digest", "Limited time offer inside"
    ) is False
