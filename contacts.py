"""Who to reply to, worked out from one recruiter email.

Inbox sync always knew more about a message than the tracker kept. The only
thing recorded was the ``From`` header, and only when it looked replyable — so
an ATS relay (``no-reply@greenhouse.io``) wiped the human out of the picture
even when the mail set a ``Reply-To`` to the recruiter's own address and signed
off with their name and direct line. "Who is handling this application?" then
had no answer without going back to Gmail, which is the thing the tracker
exists to avoid.

Everything here is pure text work: no I/O, no network, no model call. That
matters twice over. It is testable without either, and it is the layer that
gets to *distrust* the model — an email body is written by whoever sent it, so
an address the model reports is only accepted when it also appears verbatim in
the text it was supposedly read from. A body saying "reply to
security@paypal-support.example" cannot invent a contact the message does not
contain, and cannot invent one at all if the model was the only source.

Ordering principle throughout: **headers beat prose, and prose beats a guess.**
A ``Reply-To`` was set by the sending system; a signature block was typed by a
person; a model's reading of either is a convenience, not evidence.
"""

import re
from email.utils import getaddresses, parseaddr

# Long enough for "Talent Acquisition Partner, EMEA", short enough that a
# paragraph the model mistook for a name is rejected rather than stored.
MAX_CONTACT_NAME = 120

# Mailboxes that exist to send and not to receive. Storing one as the contact
# is worse than storing nothing: it reads as somebody to reply to.
_NOREPLY_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notification|notifications|"
    r"automated|auto|mailer|bounce|postmaster|noreply)",
    re.IGNORECASE,
)

# Addresses that belong to the plumbing rather than to a person: bounce
# handlers, click trackers, and the per-message return paths ATS vendors mint.
_MACHINE_DOMAIN_HINTS = (
    "bounce", "bounces", "sendgrid", "mailgun", "amazonses", "mandrill",
    "mailchimp", "sparkpost", "postmark", "customeriomail", "sendinblue",
)


def is_replyable(address: str) -> bool:
    """True when an address looks like a person rather than a send-only robot."""
    address = (address or "").strip()
    if "@" not in address:
        return False

    local, _, domain = address.partition("@")

    if _NOREPLY_LOCAL.match(local):
        return False

    # A return path such as bounces+12345@mail.acme.com is deliverable and
    # still nobody's inbox.
    return not any(hint in domain.lower() for hint in _MACHINE_DOMAIN_HINTS)


# =====================================================================
# HEADERS
# =====================================================================
def parse_sender(raw: str) -> tuple[str, str]:
    """Split a ``From``-style header into ``(display name, address)``.

    ``parseaddr`` handles both ``Jane Doe <jane@acme.com>`` and a bare address,
    plus the quoted display names that would otherwise need unpicking by hand.
    """
    name, address = parseaddr(raw or "")
    return _clean_name(name), address.strip()


def header_addresses(raw: str) -> list[tuple[str, str]]:
    """Every ``(name, address)`` pair in a header that may list several."""
    return [
        (_clean_name(name), address.strip())
        for name, address in getaddresses([raw or ""])
        if "@" in address
    ]


def _clean_name(value: str) -> str:
    """Tidy a display name, or return "" when it is not one.

    A great many senders put their own address in the display-name slot, and
    ATS relays put the role there. Neither is a person's name, and storing one
    means the dashboard shows an address twice over.
    """
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'")

    if not cleaned or len(cleaned) > MAX_CONTACT_NAME:
        return ""
    if "@" in cleaned or "://" in cleaned:
        return ""
    # "via Greenhouse", "on behalf of Acme" — the relay describing itself.
    if re.match(r"^(via|on behalf of)\b", cleaned, re.IGNORECASE):
        return ""

    return cleaned


# =====================================================================
# WHAT THE BODY CONTAINS
# =====================================================================
_EMAIL_RE = re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+")

# Image and asset filenames survive HTML-to-text stripping as things that look
# like addresses, and tracking pixels are worse: logo@2x.png reads as one.
_NOT_AN_ADDRESS = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$", re.IGNORECASE)


def find_addresses(text: str, exclude: tuple[str, ...] = ()) -> list[str]:
    """Every email address in some text, de-duplicated, first mention first.

    ``exclude`` drops addresses that are not the counterparty — above all the
    user's own, which appears in almost every recruiter email ("we received
    your application from you@gmail.com") and would otherwise be recorded as
    the person to reply to.
    """
    blocked = {address.strip().casefold() for address in exclude if address}
    found: list[str] = []
    seen: set[str] = set()

    for match in _EMAIL_RE.finditer(text or ""):
        address = match.group(0).strip(".")
        key = address.casefold()

        if key in seen or key in blocked or _NOT_AN_ADDRESS.search(address):
            continue

        seen.add(key)
        found.append(address)

    return found


# Phone numbers are recognised only when the text says so, or when the number
# carries an international prefix.
#
# A bare run of digits is not enough evidence. Recruiter mail is full of
# numbers that are not phone numbers — requisition ids, dates, salary bands,
# tracking references — and a wrong number in a tracker is worse than an empty
# field, because it will eventually be dialled. The cost of being strict is
# that an unlabelled local number is missed, which is the better failure.
_PHONE_LABEL = (
    r"(?:phone|tel|telephone|mobile|cell|whatsapp|direct(?:\s*line)?|"
    r"contact(?:\s*(?:me|number))?|call(?:\s*me)?(?:\s*(?:at|on))?)"
)
_PHONE_BODY = r"\+?\d[\d\s().\-]{7,18}\d"

_PHONE_PATTERNS = (
    # Labelled: "Mobile: 98765 43210", "call me on 020 7946 0018"
    re.compile(rf"{_PHONE_LABEL}\s*(?:number)?\s*[:\-–—]?\s*({_PHONE_BODY})", re.IGNORECASE),
    # Unlabelled but international, so unambiguous: "+44 20 7946 0018"
    re.compile(r"(?<![\w+])(\+\d[\d\s().\-]{7,18}\d)"),
)

# Shortest and longest a real number can be once the decoration is stripped.
# Ten digits is a national number; fifteen is the E.164 ceiling.
MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15


def find_phone_numbers(text: str) -> list[str]:
    """Phone numbers stated in some text, de-duplicated, first mention first."""
    found: list[str] = []
    seen: set[str] = set()

    for pattern in _PHONE_PATTERNS:
        for match in pattern.finditer(text or ""):
            number = re.sub(r"[\s.\-]+$", "", match.group(1).strip())
            digits = re.sub(r"\D", "", number)

            if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
                continue
            if digits in seen:
                continue

            seen.add(digits)
            found.append(re.sub(r"\s{2,}", " ", number))

    return found


def normalise_phone(value: str) -> str:
    """Just the digits, so two spellings of one number compare equal."""
    return re.sub(r"\D", "", str(value or ""))


# =====================================================================
# CHOOSING THE CONTACT
# =====================================================================
def choose_contact(
    *,
    sender: str = "",
    reply_to: str = "",
    body: str = "",
    suggested_name: str = "",
    suggested_email: str = "",
    suggested_phone: str = "",
    exclude: tuple[str, ...] = (),
) -> dict:
    """Work out who to reply to about one email.

    Returns ``{"name", "email", "phone", "source", "mentioned"}``. ``source``
    records which signal won, because "why is this the contact?" is a question
    the sync log has to be able to answer months later. ``mentioned`` is every
    other address the message contained — kept as a note rather than promoted,
    since a shared ``careers@`` inbox is context, not a person.

    Addresses are taken in strength order: ``Reply-To``, then ``From``, then
    the model's reading of the body, then the first replyable address the body
    actually contains. Anything the model reports must appear verbatim in the
    body — it is describing text it was shown, so a value that is not in that
    text is either a mistake or an injection, and neither belongs in a field
    the user will click.
    """
    blocked = tuple(address for address in exclude if address)
    body_addresses = find_addresses(body, exclude=blocked)
    body_keys = {address.casefold() for address in body_addresses}
    blocked_keys = {address.strip().casefold() for address in blocked if address}

    reply_name, reply_address = parse_sender(reply_to)
    from_name, from_address = parse_sender(sender)

    suggested_email = (suggested_email or "").strip()

    candidates = [
        ("reply-to", reply_name, reply_address),
        ("from", from_name, from_address),
        # Only if the body really says it. See the docstring.
        (
            "body (named)",
            _clean_name(suggested_name),
            suggested_email if suggested_email.casefold() in body_keys else "",
        ),
        ("body", "", body_addresses[0] if body_addresses else ""),
    ]

    chosen_name = ""
    chosen_email = ""
    source = ""

    for label, name, address in candidates:
        if not address or address.casefold() in blocked_keys:
            continue
        if not is_replyable(address):
            continue

        chosen_name = name
        chosen_email = address
        source = label
        break

    # A name is useful even when every address was a robot: "Priya from Acme
    # Talent said X" is still more than an empty field. Taken from the model
    # only when no header offered one, and never checked against the body —
    # a name is not an actionable value the way an address is.
    if not chosen_name:
        chosen_name = _clean_name(suggested_name) or reply_name or from_name

    phone = _choose_phone(body, suggested_phone)

    return {
        "name": chosen_name,
        "email": chosen_email,
        "phone": phone,
        "source": source,
        # Everything else the message named, minus whatever was promoted.
        "mentioned": [
            address
            for address in body_addresses
            if address.casefold() != chosen_email.casefold()
        ],
    }


def _choose_phone(body: str, suggested: str) -> str:
    """A phone number for the contact, or "".

    The model's answer is preferred when the body corroborates it, because the
    model can tell a recruiter's direct line from the support number in a
    footer. Failing that, the first number the body actually labels as one.
    """
    in_body = find_phone_numbers(body)
    wanted = normalise_phone(suggested)

    if wanted and MIN_PHONE_DIGITS <= len(wanted) <= MAX_PHONE_DIGITS:
        # Substring rather than equality: the body may write it with a country
        # code the model dropped, or vice versa.
        digits_only = normalise_phone(body)
        if wanted in digits_only:
            return suggested.strip()

    return in_body[0] if in_body else ""
