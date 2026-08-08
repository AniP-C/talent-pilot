"""Per-user answer bank for job application forms.

Application forms ask the same two dozen questions forever — work
authorisation, notice period, whether you have a criminal record — usually
buried in a paragraph of legal text. Answering them by hand every time is most
of the clerical work the tracker exists to remove.

This replaces ``extension/rules.js``, a static file that shipped one person's
name, phone number and email inside the extension. Answers now live per user in
their own workspace: the extension asks the API for them, so two people using
the same build get their own details, and nobody's personal data is baked into
the code.

Answers come from three places, strongest first:

1. **The questionnaire** — what the user explicitly told us.
2. **The resume** — name, email, phone and so on, parsed at upload.
3. **Saved AI drafts** — a question answered once with AI is kept, so the
   same question is instant and free the next time it appears.

Matching is deterministic. A form asking "Are you legally authorized to work?"
resolves against a regex, not a model call — it is instant, free, and gives the
same answer every time. AI is reserved for genuinely new questions.
"""

import json
import re
from typing import Optional

import workspace
from config import logger

# Answers are short by nature; a paragraph belongs in the AI drafting path.
MAX_ANSWER_LENGTH = 2000
MAX_CUSTOM_ENTRIES = 200


class Field:
    """One question in the catalogue.

    ``patterns`` are matched against the form's label text. ``resume_path`` is
    a dotted path into the parsed resume JSON used to pre-fill the answer, so
    the questionnaire arrives mostly complete instead of empty.
    """

    __slots__ = ("key", "question", "kind", "options", "patterns", "group", "resume_path", "help")

    def __init__(
        self,
        key: str,
        question: str,
        kind: str = "text",
        *,
        group: str,
        patterns: list[str],
        options: Optional[list[str]] = None,
        resume_path: str = "",
        help: str = "",
    ):
        self.key = key
        self.question = question
        self.kind = kind          # text | yes_no | choice | number | date
        self.options = options or []
        self.patterns = patterns
        self.group = group
        self.resume_path = resume_path
        self.help = help

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "question": self.question,
            "kind": self.kind,
            "options": self.options,
            "group": self.group,
            "help": self.help,
        }


# The catalogue.
#
# ORDER IS SIGNIFICANT: matching walks this list and takes the first hit, so
# specific questions must precede general ones. "First name" has to be tested
# before "name", or every name field on every form fills with the full name —
# which is exactly what the old rules.js did.
FIELDS: list[Field] = [
    # ---------------- identity ----------------
    Field("first_name", "First name", group="Personal", resume_path="name",
          patterns=[r"first\s*name", r"given\s*name", r"\bforename\b"]),
    Field("last_name", "Last name", group="Personal", resume_path="",
          patterns=[r"last\s*name", r"\bsurname\b", r"family\s*name"]),
    Field("full_name", "Full name", group="Personal", resume_path="name",
          patterns=[r"full\s*name", r"legal\s*name", r"your\s*name", r"^name$", r"\bname\b"]),
    Field("email", "Email address", group="Personal", resume_path="email",
          patterns=[r"e-?mail"]),
    Field("phone", "Phone number", group="Personal", resume_path="phone",
          patterns=[r"phone", r"mobile", r"\bcell\b", r"contact\s*number", r"telephone"]),
    Field("location", "Current location", group="Personal", resume_path="location",
          patterns=[r"current\s*(location|city|residence)", r"where\s*are\s*you\s*(based|located)",
                    r"\bcity\b", r"\blocation\b", r"\baddress\b"]),
    Field("linkedin", "LinkedIn URL", group="Personal", resume_path="linkedin",
          patterns=[r"linked\s*-?in"]),
    Field("github", "GitHub URL", group="Personal", resume_path="github",
          patterns=[r"\bgithub\b"]),
    Field("portfolio", "Portfolio / website", group="Personal", resume_path="portfolio",
          patterns=[r"portfolio", r"personal\s*(website|site)", r"\bwebsite\b"]),

    # ---------------- work authorisation ----------------
    # The single most common blocker, and the one people get wrong by
    # autofilling "Yes" into the sponsorship question.
    Field("work_authorized", "Are you legally authorized to work in the country of this role?",
          "yes_no", group="Work authorisation",
          patterns=[r"legally\s*authoriz", r"authoriz(ed|ation)\s*to\s*work",
                    r"eligib(le|ility)\s*to\s*work", r"right\s*to\s*work",
                    r"work\s*authoriz"]),
    Field("needs_sponsorship",
          "Will you now or in the future require visa sponsorship?", "yes_no",
          group="Work authorisation",
          patterns=[r"require\s*(visa\s*)?sponsor", r"need\s*sponsor",
                    r"future\s*sponsor", r"employment\s*sponsor",
                    r"visa\s*sponsor", r"\bsponsorship\b"],
          help="Answered separately from the question above — they are opposites, "
               "and filling both with the same value is the classic mistake."),
    Field("citizenship", "Citizenship / nationality", group="Work authorisation",
          patterns=[r"citizenship", r"nationality", r"\bcitizen\b"]),
    Field("visa_status", "Current visa or work permit status", group="Work authorisation",
          patterns=[r"visa\s*status", r"work\s*permit", r"immigration\s*status"]),

    # ---------------- employment ----------------
    Field("current_company", "Current employer", group="Employment",
          resume_path="experience.0.company",
          patterns=[r"current\s*(company|employer)", r"present\s*(company|employer)",
                    r"most\s*recent\s*employer"]),
    Field("current_title", "Current job title", group="Employment",
          resume_path="experience.0.title",
          patterns=[r"current\s*(designation|role|title|position)",
                    r"present\s*(designation|role|title)", r"job\s*title"]),
    Field("total_experience", "Total years of experience", "number", group="Employment",
          patterns=[r"years?\s*of\s*(work\s*)?experience", r"total\s*experience",
                    r"work\s*experience\s*\(years", r"experience\s*in\s*years"]),
    Field("notice_period", "Notice period", group="Employment",
          patterns=[r"notice\s*period", r"joining\s*period",
                    r"how\s*soon\s*can\s*you\s*(join|start)", r"when\s*can\s*you\s*(join|start)"]),
    Field("current_ctc", "Current salary / CTC", group="Employment",
          patterns=[r"current\s*(ctc|salary|compensation)", r"present\s*(ctc|salary)"]),
    Field("expected_ctc", "Expected salary / CTC", group="Employment",
          patterns=[r"expected\s*(ctc|salary|compensation)", r"salary\s*expectation",
                    r"desired\s*(salary|compensation)", r"compensation\s*expectation"]),
    Field("reason_for_leaving", "Reason for leaving your current role", group="Employment",
          patterns=[r"reason\s*for\s*leaving", r"why\s*are\s*you\s*leaving",
                    r"why\s*do\s*you\s*want\s*to\s*leave"]),
    Field("available_start", "Earliest available start date", group="Employment",
          patterns=[r"available\s*to\s*(join|start)", r"start\s*date",
                    r"date\s*of\s*joining", r"availability\s*to\s*start"]),

    # ---------------- education ----------------
    Field("highest_qualification", "Highest qualification", group="Education",
          resume_path="education.0.degree",
          patterns=[r"highest\s*(qualification|degree|education)",
                    r"level\s*of\s*education", r"\bdegree\b", r"\beducation\b"]),
    Field("university", "University / college", group="Education",
          resume_path="education.0.institution",
          patterns=[r"university", r"college", r"institution", r"school\s*name"]),
    Field("graduation_year", "Graduation year", "number", group="Education",
          resume_path="education.0.year",
          patterns=[r"graduation\s*year", r"passing\s*year", r"year\s*of\s*graduation",
                    r"completion\s*year"]),
    Field("field_of_study", "Field of study / major", group="Education",
          patterns=[r"field\s*of\s*study", r"\bmajor\b", r"discipline", r"specializ"]),

    # ---------------- logistics ----------------
    Field("willing_to_relocate", "Are you willing to relocate?", "yes_no",
          group="Logistics",
          patterns=[r"willing\s*to\s*relocate", r"open\s*to\s*relocat", r"relocation"]),
    Field("remote_ok", "Are you comfortable working remotely?", "yes_no",
          group="Logistics",
          patterns=[r"remote\s*work", r"work\s*from\s*home", r"comfortable\s*working\s*remote"]),
    Field("onsite_ok", "Are you able to work from the office / hybrid?", "yes_no",
          group="Logistics",
          patterns=[r"work\s*from\s*(the\s*)?office", r"\bhybrid\b", r"\bon-?site\b"]),
    Field("willing_to_travel", "Are you willing to travel?", "yes_no", group="Logistics",
          patterns=[r"willing\s*to\s*travel", r"able\s*to\s*travel", r"travel\s*requirement"]),

    # ---------------- compliance ----------------
    # The paragraph-of-legal-text questions. Almost always the same answer,
    # almost never worth reading twice.
    Field("criminal_record", "Have you ever been convicted of a crime?", "yes_no",
          group="Compliance",
          patterns=[r"convicted", r"criminal\s*(record|history|conviction)",
                    r"\bfelony\b", r"pleaded\s*guilty", r"criminal\s*background"]),
    Field("background_check_consent", "Do you consent to a background check?", "yes_no",
          group="Compliance",
          patterns=[r"background\s*(check|verification|screening)",
                    r"consent\s*to\s*a?\s*background"]),
    Field("drug_test_consent", "Do you consent to a drug test?", "yes_no",
          group="Compliance",
          patterns=[r"drug\s*(test|screen)", r"substance\s*(test|screen)"]),
    Field("veteran_status", "Veteran status", "choice", group="Compliance",
          options=["I am not a protected veteran", "I am a protected veteran",
                   "I prefer not to answer"],
          patterns=[r"veteran", r"military\s*service"]),
    Field("disability_status", "Disability status", "choice", group="Compliance",
          options=["No, I do not have a disability", "Yes, I have a disability",
                   "I prefer not to answer"],
          patterns=[r"disabilit"]),
    Field("gender", "Gender", "choice", group="Compliance",
          options=["Male", "Female", "Non-binary", "I prefer not to say"],
          patterns=[r"\bgender\b", r"\bsex\b"]),
    Field("ethnicity", "Race / ethnicity", "choice", group="Compliance",
          options=["Asian", "Black or African American", "Hispanic or Latino",
                   "White", "Two or more races", "I prefer not to say"],
          patterns=[r"ethnicit", r"\brace\b", r"racial"]),

    # ---------------- company-specific ----------------
    Field("previously_worked_here", "Have you previously worked for this company?", "yes_no",
          group="This employer",
          patterns=[r"previously\s*(worked|employed)", r"worked\s*(here|for\s*us)\s*before",
                    r"former\s*employee", r"ever\s*been\s*employed\s*by"]),
    Field("relative_employed", "Do you have a relative employed here?", "yes_no",
          group="This employer",
          patterns=[r"relative\s*(employed|working)", r"family\s*member\s*(employed|work)",
                    r"related\s*to\s*(any|an)\s*employee"]),
    Field("referred_by", "Were you referred? By whom?", group="This employer",
          patterns=[r"referred\s*by", r"employee\s*referral", r"referral\s*(name|source)"]),
    Field("how_did_you_hear", "How did you hear about this role?", group="This employer",
          patterns=[r"how\s*did\s*you\s*hear", r"where\s*did\s*you\s*(hear|find)",
                    r"source\s*of\s*application"]),
]

FIELDS_BY_KEY = {field.key: field for field in FIELDS}

GROUPS = list(dict.fromkeys(field.group for field in FIELDS))


# =====================================================================
# STORAGE
# =====================================================================
def _empty() -> dict:
    return {"answers": {}, "custom": []}


def load(user_id: int) -> dict:
    """Read a user's answer bank. Never raises — a broken file reads as empty."""
    path = workspace.autofill_path(user_id)

    if not path.exists():
        return _empty()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read autofill answers for user %s: %s", user_id, exc)
        return _empty()

    if not isinstance(data, dict):
        return _empty()

    return {
        "answers": data.get("answers") or {},
        "custom": data.get("custom") or [],
    }


def save(user_id: int, bank: dict) -> None:
    """Write the answer bank back to the user's workspace."""
    workspace.autofill_path(user_id).write_text(
        json.dumps(bank, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(
        "Saved %s standard and %s custom answers for user %s",
        len(bank.get("answers", {})), len(bank.get("custom", [])), user_id,
    )


def set_answers(user_id: int, answers: dict[str, str]) -> dict:
    """Merge answers from the questionnaire into the bank.

    An empty value clears the answer rather than storing a blank, so a field
    the user deliberately emptied stops being suggested.
    """
    bank = load(user_id)

    for key, value in (answers or {}).items():
        if key not in FIELDS_BY_KEY:
            continue

        cleaned = str(value or "").strip()[:MAX_ANSWER_LENGTH]

        if cleaned:
            bank["answers"][key] = cleaned
        else:
            bank["answers"].pop(key, None)

    save(user_id, bank)
    return bank


def add_custom(user_id: int, question: str, answer: str) -> dict:
    """Record an answer to a question outside the catalogue.

    Used both by the dashboard's "add your own" form and by the extension when
    an AI-drafted answer is saved, so a question answered once is free and
    instant the next time it appears.
    """
    question = str(question or "").strip()[:MAX_ANSWER_LENGTH]
    answer = str(answer or "").strip()[:MAX_ANSWER_LENGTH]

    if not question or not answer:
        raise ValueError("Both a question and an answer are required.")

    bank = load(user_id)

    # Re-answering replaces rather than appends, so the bank does not fill
    # with stale variants of the same question.
    for entry in bank["custom"]:
        if entry.get("question", "").casefold() == question.casefold():
            entry["answer"] = answer
            save(user_id, bank)
            return bank

    if len(bank["custom"]) >= MAX_CUSTOM_ENTRIES:
        raise ValueError(
            f"You already have {MAX_CUSTOM_ENTRIES} saved answers. "
            "Remove one before adding another."
        )

    bank["custom"].append({"question": question, "answer": answer})
    save(user_id, bank)
    return bank


def remove_custom(user_id: int, question: str) -> dict:
    bank = load(user_id)
    target = str(question or "").strip().casefold()
    bank["custom"] = [
        entry for entry in bank["custom"]
        if entry.get("question", "").casefold() != target
    ]
    save(user_id, bank)
    return bank


# =====================================================================
# SEEDING FROM A RESUME
# =====================================================================
def _dig(data: dict, path: str):
    """Follow a dotted path like ``experience.0.company`` through parsed JSON."""
    current = data

    for part in path.split("."):
        if current is None:
            return None
        if part.isdigit():
            if not isinstance(current, list) or len(current) <= int(part):
                return None
            current = current[int(part)]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(part)

    return current


def seed_from_resume(user_id: int, profile: dict) -> dict:
    """Pre-fill answers from a parsed resume, without overwriting the user.

    Called after a resume upload so the questionnaire arrives mostly complete.
    Existing answers always win: the user correcting a badly-parsed phone
    number must not be undone by re-uploading the same PDF.
    """
    bank = load(user_id)
    filled = 0

    for field in FIELDS:
        if not field.resume_path or bank["answers"].get(field.key):
            continue

        value = _dig(profile or {}, field.resume_path)

        if isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str) or not value.strip():
            continue

        value = value.strip()

        # "Aniruddh Parashar" -> first "Aniruddh", last "Parashar".
        if field.key == "first_name":
            value = value.split()[0]
        elif field.key == "full_name" and len(value.split()) < 2:
            continue

        bank["answers"][field.key] = value[:MAX_ANSWER_LENGTH]
        filled += 1

    # last_name has no resume_path of its own; it is derived from the name.
    if not bank["answers"].get("last_name"):
        name = (_dig(profile or {}, "name") or "").strip()
        if len(name.split()) >= 2:
            bank["answers"]["last_name"] = name.split()[-1]
            filled += 1

    if filled:
        save(user_id, bank)
        logger.info("Seeded %s answers from a resume for user %s", filled, user_id)

    return bank


# =====================================================================
# WHAT THE EXTENSION CONSUMES
# =====================================================================
def build_rules(user_id: int) -> list[dict]:
    """The matching rules for one user, strongest match first.

    Catalogue order is preserved because it encodes specificity — "first name"
    must be tested before "name". Custom entries come last and match on their
    literal text rather than a regex, so a user typing "(" into a question does
    not produce a broken pattern.
    """
    bank = load(user_id)
    rules = []

    for field in FIELDS:
        answer = bank["answers"].get(field.key)
        if not answer:
            continue

        rules.append(
            {
                "key": field.key,
                "patterns": field.patterns,
                "literal": False,
                "answer": answer,
                "question": field.question,
                "kind": field.kind,
            }
        )

    for entry in bank["custom"]:
        question = entry.get("question", "")
        answer = entry.get("answer", "")
        if not question or not answer:
            continue

        rules.append(
            {
                "key": f"custom:{question[:60]}",
                "patterns": [question],
                "literal": True,
                "answer": answer,
                "question": question,
                "kind": "text",
            }
        )

    return rules


def match(label: str, rules: list[dict]) -> Optional[dict]:
    """Find the answer for a form label, or None.

    Mirrors the matching the extension does in the page, so the same label
    resolves the same way on both sides.
    """
    text = str(label or "").strip()
    if not text:
        return None

    for rule in rules:
        for pattern in rule["patterns"]:
            if rule.get("literal"):
                if pattern.casefold() in text.casefold():
                    return rule
            elif re.search(pattern, text, re.IGNORECASE):
                return rule

    return None


def completeness(user_id: int) -> dict:
    """How much of the catalogue is answered, for the setup nudge."""
    bank = load(user_id)
    answered = sum(1 for field in FIELDS if bank["answers"].get(field.key))

    return {
        "answered": answered,
        "total": len(FIELDS),
        "missing": len(FIELDS) - answered,
        "custom": len(bank["custom"]),
    }
