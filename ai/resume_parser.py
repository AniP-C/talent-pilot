"""Gemini-backed resume parsing, JD analysis, and answer drafting.

All answer-memory reads and writes are scoped to a single user's workspace, so
one person's saved answers never leak into another person's drafts.
"""

import os
import re
import sys
from typing import Optional

from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autofill
import posting
import scoring
import workspace
from ai.gemini import (
    CLASSIFICATION_TEMPERATURE,
    DRAFTING_TEMPERATURE,
    generate_structured,
)
from config import logger

# =====================================================================
# STRUCTURED OUTPUT SCHEMAS
# =====================================================================
class Requirement(BaseModel):
    """One thing the job asks for, and whether the resume shows it.

    Deliberately carries no score. The model classifies; ``scoring.py`` does
    the arithmetic — see that module for why.
    """

    skill: str
    importance: str   # required | preferred
    # skill | domain | employer_internal | meta. What sort of thing is being
    # asked for, which decides whether it can be scored at all — a requirement
    # naming the employer's own internal programme is unwinnable by anyone
    # applying from outside. See scoring.VALID_KINDS.
    kind: str
    status: str       # demonstrated | partial | absent
    evidence: str     # where in the resume, or "" when absent


class JDAnalysis(BaseModel):
    requirements: list[Requirement]
    summary: str


class AnswerResponse(BaseModel):
    suggested_answer: str
    confidence_score: int
    memory_used: str


class Experience(BaseModel):
    company: str
    role: str
    duration: str
    description: list[str]


class Education(BaseModel):
    institution: str
    degree: str
    graduation_year: str


class StructuredResume(BaseModel):
    name: str
    email: str
    phone: str
    location: str
    linkedin: str
    github: str
    summary: str
    skills: list[str]
    experience: list[Experience]
    education: list[Education]


# =====================================================================
# ANSWER MEMORY
# =====================================================================
# Question keywords -> memory filename. Both the reader and the writer use
# this one mapping, so a saved answer is always found again later.
ANSWER_CATEGORIES = [
    (("about yourself", "about you", "background", "introduce"), "about_me.txt"),
    # "What interests you about working for this company?" matched none of the
    # original three phrasings and fell into the general bucket, where it was
    # then fed back as an exemplar for unrelated questions. The employer asks
    # this a dozen ways; the bucket has to recognise more than three.
    (
        (
            "why this company",
            "why do you want",
            "why are you interested",
            "interests you about",
            "interest you about",
            "excites you about",
            "attracted you",
            "why us",
            "why join",
            "why work",
        ),
        "why_company.txt",
    ),
    (("challenge", "difficult", "hardest", "proud"), "challenging_project.txt"),
    (("weakness", "improve", "shortcoming"), "weaknesses.txt"),
    (("strength", "good at"), "strengths.txt"),
]

DEFAULT_ANSWER_FILE = "general.txt"


def categorize_question(question: str) -> str:
    """Map a question to the memory file that should hold its answer."""
    lowered = (question or "").lower()
    for keywords, filename in ANSWER_CATEGORIES:
        if any(keyword in lowered for keyword in keywords):
            return filename
    return DEFAULT_ANSWER_FILE


def load_answer_memory(user_id: int, question: str) -> tuple[str, str]:
    """Return ``(memory_text, filename)`` for a question, empty text if none."""
    filename = categorize_question(question)

    try:
        path = workspace.answer_path(user_id, filename)
    except workspace.UnsafePathError:
        return "", "none"

    if not path.exists():
        return "", "none"

    try:
        stored = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("Could not read answer memory %s: %s", filename, exc)
        return "", "none"

    if filename == DEFAULT_ANSWER_FILE:
        # general.txt is where every uncategorised question ends up, so it is a
        # grab-bag rather than a theme. Handing the whole file back as "answers
        # like yours" is how one generic paragraph became the house style: it
        # was offered as the model for questions it had nothing to do with,
        # and every accepted draft appended another copy of it. Only entries
        # that share real words with the question asked are relevant.
        stored = _entries_resembling(stored, question)

    return stored.strip(), filename if stored.strip() else "none"


# ``--- Q: <question> ---`` is the separator save_answer_to_memory writes.
_ENTRY_HEADER = re.compile(r"^--- Q: (.*?) ---$", re.MULTILINE)

# Words too common to mean two questions are about the same thing. Without
# these, "you" and "your" alone make everything resemble everything.
_COMMON_WORDS = frozenset(
    {
        "about", "would", "your", "yours", "you", "with", "what", "when", "this",
        "that", "there", "their", "them", "they", "have", "has", "here", "from",
        "into", "please", "tell", "describe", "explain", "mention", "give",
        "provide", "role", "job", "position", "company", "work", "working",
        "candidate", "applicant", "application", "answer", "question", "does",
        "did", "are", "were", "will", "shall", "should", "could", "must",
    }
)


def _keywords(text: str) -> set[str]:
    """The words in a question that carry its subject."""
    return {
        word for word in re.findall(r"[a-z]{4,}", (text or "").lower())
    } - _COMMON_WORDS


def _entries_resembling(stored: str, question: str) -> str:
    """Keep only the saved entries whose question shares subject words.

    Deliberately a word overlap rather than anything cleverer. The job is to
    exclude an answer about notice periods from a question about motivation,
    and one shared uncommon word does that; the cost of being slightly too
    strict is a draft with less context, which is the safer failure.
    """
    wanted = _keywords(question)

    if not wanted:
        return ""

    # split() on a capturing pattern gives [preamble, q1, a1, q2, a2, ...].
    parts = _ENTRY_HEADER.split(stored)
    kept = [
        f"--- Q: {recorded.strip()} ---\n{answer.strip()}"
        for recorded, answer in zip(parts[1::2], parts[2::2])
        if _keywords(recorded) & wanted
    ]

    return "\n\n".join(kept)


def save_answer_to_memory(user_id: int, question: str, answer_text: str) -> str:
    """Append an approved answer to the user's memory bank. Returns the filename."""
    filename = categorize_question(question)
    path = workspace.answer_path(user_id, filename)

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n--- Q: {question.strip()} ---\n{answer_text.strip()}")

    logger.info("Saved answer to %s for user %s", filename, user_id)
    return filename


# =====================================================================
# PUBLIC ENDPOINTS
# =====================================================================
def analyze_jd(jd_text: str, resume_data: str, resume_text: str = "") -> dict:
    """Compare a resume to a job description.

    The model is asked only to classify each requirement. The percentage is
    computed from those classifications in ``scoring.py``, so it is
    reproducible and can be shown with its working. Asking the model for the
    number produced one that ignored its own findings — an analysis listing a
    dozen absent requirements still came back 90%.

    ``resume_data`` is the parsed profile as JSON, which is what the model
    reads. ``resume_text`` is the resume as extracted from the PDF, and is what
    the keyword pass reads: that pass exists to approximate a literal filter
    over the document actually submitted, and running it over the model's
    re-encoding measured the wrong artefact — anything the parser normalised,
    merged or dropped was invisible to a scan whose only job is to be literal.

    It falls back to the parsed profile when no text is stored, which is the
    case for every profile uploaded before the text was kept.

    The job description is trimmed to the description itself first. What the
    extension captures is a page, and a corporate careers page carries a
    holiday allowance, a campus write-up and a word cloud of every technology
    the employer uses anywhere after the job ends — noise in the prompt, and
    paid for by the token.
    """
    jd_text = posting.trim_to_description(jd_text)
    prompt = f"""
    You are a technical recruiter assessing one candidate against one job.

    List every distinct requirement the job description states, and for each
    one decide:

      importance  "required"  the job presents it as a must-have.
                  "preferred" the job calls it preferred, a bonus, a plus, or
                              nice to have.

      kind        "skill"     a transferable tool, platform, language or
                              technique — Python, Kubernetes, RAG.
                  "domain"    industry or sector experience — payments,
                              healthcare, financial services.
                  "employer_internal"
                              THIS employer's own programme, platform, system
                              or process, named as though it were common
                              knowledge: "SOLD Simplification", "our SDLC",
                              "the Atlas migration", an internal team or tool.
                              A capitalised project name you do not recognise
                              as an industry-wide technology belongs here.
                  "meta"      behavioural or process expectations —
                              stakeholder management, mentoring, "risk and
                              controls".

      status      "demonstrated" the resume shows clear, specific evidence.
                  "partial"      the resume shows something adjacent or
                                 transferable but not the thing itself — a
                                 different cloud provider, or the underlying
                                 technique without the named tool.
                  "absent"       there is no evidence in the resume.

      evidence    where in the resume you saw it, or "" when absent.

    RULES:
    0. EVERY REQUIREMENT MUST COME FROM THE JOB DESCRIPTION. Read the resume
       only to decide the STATUS of a requirement the job itself states. Never
       list a skill because the resume mentions it — copying a resume's skills
       back as the job's requirements returns a perfect score for a posting
       that asked for nothing, which is the most misleading thing this tool can
       do. In the rare case that a description states no requirements at all —
       four sentences of culture and a "get in touch" — return an empty list.
    0b. BE COMPLETE AND BE GRANULAR. Everything the description does state
       belongs in the list. One sentence usually holds several requirements:
       "using microservices, REST APIs, event-driven architecture,
       authentication and enterprise integration patterns" is FIVE, not one,
       and collapsing them into a single entry averages real evidence against
       real gaps and hides both. Split a list into its items. Only rule 2
       below — an explicit choice between alternatives — is ever one entry.
    1. One entry per distinct requirement. Never list the same skill twice
       under different names: "LLM" and "Large Language Models" are one
       requirement, as are "GCP" and "Google Cloud".
    2. ALTERNATIVES ARE ONE REQUIREMENT. When a requirement offers a choice —
       "Python, Go, or Node.js", "LangChain or LlamaIndex", "AWS, GCP, or
       Azure", "Pinecone, Weaviate, or pgvector" — the job is asking for ANY
       ONE of them, not all of them.
         - List it ONCE, using the whole phrase as the skill.
         - Mark it "demonstrated" if the resume shows ANY ONE alternative.
         - NEVER list the alternatives the candidate lacks as their own
           requirements. A candidate with Python does not have a "Go" gap, and
           a candidate with LangChain does not have a "LlamaIndex" gap.
       This is the single most common way this assessment goes wrong: it
       invents gaps that the job description never asked for, and every
       invented gap makes a good candidate look unqualified.
    3. A job description often states the same requirement twice, once in a
       responsibilities or experience section and again in a skills list.
       That is still one requirement.
    4. Only concrete skills, tools, platforms, and domain experience. Never
       list job-description prose such as "collaborate", "best practices",
       "solutions", "technical" or "development".
    5. Do not award "demonstrated" for something the resume merely implies.
       Adjacent evidence is "partial" — the underlying technique without the
       named tool, or a different vendor in the same category where the job
       named specific ones and offered no choice. Be strict: this assessment
       is only useful if it is honest about gaps.
    6. Do not produce a score or a percentage anywhere. The score is computed
       from your classifications.
    7. Be decisive about "employer_internal". An applicant cannot have worked
       on this employer's internal initiative before joining it, so scoring
       them against it is scoring them against something no CV could ever
       satisfy — and then telling them it is a gap they should close. If a
       named programme is not a technology you would expect to find in another
       company's job advert, it is employer_internal. Still list it; it is
       reported as context rather than dropped.

    Then write a short summary naming the strongest evidence and the most
    significant gaps.

    The summary must NOT deliver a verdict. Do not write "strong fit",
    "excellent match", "strong candidate", "well suited", or any other overall
    judgement, and never a percentage. You are writing before the score exists:
    it is computed from your classifications above, and a summary that opens
    "the candidate is a strong match" above a headline reading 52% is the tool
    contradicting itself in the same breath. Describe what the resume shows and
    what it does not, and stop there.

    Do not mention requirements you classified as "employer_internal" as a
    shortcoming. Nobody applying from outside can have them.

    JOB DESCRIPTION:
    {jd_text[:8000]}

    RESUME:
    {resume_data[:8000]}
    """
    result = generate_structured(prompt, JDAnalysis, "JD_ANALYSIS")

    if "error" in result:
        return result

    requirements = scoring.normalise_requirements(result.get("requirements"))

    # The model is told that a choice is one requirement, and mostly obeys. On
    # one run in three it listed "Java" as its own absent must-have for a job
    # advertised as "either Python or Java" — enough to send somebody off to
    # learn a language the employer explicitly did not ask for.
    requirements = scoring.drop_alternatives_already_met(
        requirements, jd_text, resume_text or resume_data
    )

    coverage = scoring.score_requirements(requirements)

    # The posting's own word, over the model's. A description naming almost no
    # technology has not said enough to be scored against, however many
    # requirements came back for it — and what comes back for such a posting is
    # the resume's skills reflected at it.
    if scoring.terms_named(jd_text) < scoring.MIN_TERMS_IN_POSTING:
        coverage["thin"] = True
    keywords = scoring.keyword_coverage(jd_text, resume_text or resume_data)

    scoreable = [r for r in requirements if r["kind"] in scoring.SCOREABLE_KINDS]

    return {
        # Kept so the dashboard and the extension popup continue to work; both
        # read these three, and neither should have to know how the number is
        # arrived at.
        "match_percentage": coverage["score"],
        "matched_skills": [r["skill"] for r in scoreable if r["status"] != "absent"],
        # "Gaps a recruiter would probe" is what both clients label this list,
        # so an employer's own internal programme must not appear in it. It is
        # not something the candidate can go and fix, and listing it as a gap
        # was advice to acquire experience that only working there provides.
        "missing_skills": [r["skill"] for r in scoreable if r["status"] == "absent"],
        "summary": result.get("summary", ""),
        # The working behind the number, plus the filter's-eye view.
        "requirements": requirements,
        "coverage": coverage,
        "keyword_coverage": keywords,
    }


# =====================================================================
# WHAT SHAPE OF ANSWER A QUESTION WANTS
# =====================================================================
# Application forms mix two kinds of question and this code used to know only
# one. "Are you based out in Pune? Mention Y/N" wants a single character;
# "What interests you about working here?" wants a paragraph. Both were being
# handed to a prompt that says "write a concise, professional, highly relevant
# answer" over six thousand characters of resume — so both came back as the
# same pitch, which is exactly what that prompt asked for.
BRIEF = "brief"
OPEN = "open"

# Where the answer came from, so the caller can say so and can meter only
# what actually cost a model call.
FROM_PROFILE = "profile"
FROM_MODEL = "model"
NEEDS_PROFILE = "needs_profile"

# The form says outright what it wants.
_ASKS_YES_NO = re.compile(r"\by\s*/\s*n\b|\byes\s*/\s*no\b|\byes or no\b", re.IGNORECASE)

# Prose, and says so. Checked before the fact patterns below because "why are
# you interested" contains "are you" and is plainly not a yes/no question.
_ASKS_FOR_PROSE = re.compile(
    r"\bwhy\b|\bdescribe\b|\bexplain\b|tell\s+us|elaborate|walk\s+(us|me)"
    r"|what\s+(interests|excites|attracted|motivates|appeals|draws)"
    r"|in\s+your\s+own\s+words|cover\s+letter",
    re.IGNORECASE,
)

# A short factual answer, whatever verb the question opens with. "If N, then is
# your notice period less than 30 days?" opens with a conditional and is still
# a yes/no question.
_ASKS_FOR_A_FACT = re.compile(
    r"how\s+(many|much|soon|long)|notice\s*period|expected\s+(ctc|salary|compensation)"
    r"|current\s+(ctc|salary|compensation)|years?\s+of\s+experience|date\s+of\s+birth"
    r"|when\s+can\s+you|available\s+to\s+(start|join)"
    r"|\b(are|is|was|were|do|does|did|have|has|had|can|could|will|would|should)\s+(you|your)\b",
    re.IGNORECASE,
)

# An interrogative that asks for the value itself rather than a yes or no.
_WH_OPENER = re.compile(r"^\s*\W*(what|which|where|when|how|who)\b", re.IGNORECASE)

# A construction whose answer is yes or no, used to decide whether a stored
# fact can be handed over as-is. "Notice period?" is answered by "60 days";
# "is your notice period less than 30 days?" is answered by "N".
_CLOSED_CONSTRUCTION = re.compile(
    r"\b(are|is|was|were|do|does|did|have|has|had|can|could|will|would|should)\s+(you|your)\b",
    re.IGNORECASE,
)


def classify_question(question: str) -> str:
    """Whether this question wants a word or a paragraph."""
    text = (question or "").strip()

    if not text:
        return OPEN
    if _ASKS_YES_NO.search(text):
        return BRIEF
    if _ASKS_FOR_PROSE.search(text):
        return OPEN
    if _ASKS_FOR_A_FACT.search(text):
        return BRIEF

    return OPEN


def _saved_detail(user_id: int, question: str) -> Optional[dict]:
    """The user's own saved answer to this question, if they have one.

    autofill.py already holds their location, notice period, work
    authorisation and the rest, matched by the same patterns the extension
    uses in the page. Nothing here consulted it, so the app was paying a model
    to invent a paragraph for questions its own profile could answer exactly
    and for free.
    """
    try:
        return autofill.match(question, autofill.build_rules(user_id))
    except Exception as exc:  # noqa: BLE001 - a bad profile must not block drafting
        logger.warning("Could not read saved details for user %s: %s", user_id, exc)
        return None


def _needs_restating(question: str, fact: dict) -> bool:
    """Whether the stored value answers the question as it was asked.

    "Notice period?" takes the stored value verbatim. "Is your notice period
    less than 30 days?" does not — the honest answer is derived from it, and
    handing over "60 days" where the form wants Y/N is a wrong answer in a box
    that only accepts one character.
    """
    if fact.get("kind") == "yes_no":
        return False
    if _ASKS_YES_NO.search(question):
        return True

    # "What is your current location?" contains "is your" and is not a yes/no
    # question — it asks for the value itself, which is sitting right there.
    # Without this it went to the model to be told what it already knew.
    if _WH_OPENER.match(question or ""):
        return False

    return bool(_CLOSED_CONSTRUCTION.search(question))


def _restate_saved_detail(question: str, fact: dict) -> dict:
    """Derive the answer to a closed question from one stored fact.

    A deliberately tiny call: one fact and one question, no resume, no job
    description, no answer memory. It cannot invent where the candidate lives
    because the only thing it is given is where they live, and it is told to
    say UNKNOWN rather than guess when the fact does not settle the question.
    """
    prompt = f"""
    Answer one job-application question using a single saved fact about the
    candidate. You have no other information about them.

    SAVED FACT — {fact.get("question", "detail")}: {fact.get("answer", "")}
    QUESTION: {question}

    RULES:
    1. Answer in as few words as the question allows. If it asks for Y/N,
       answer exactly Y or N, with nothing after it.
    2. Use only the saved fact. Never guess or infer anything else about them.
    3. If the saved fact does not settle the question, reply with exactly:
       UNKNOWN
    """
    result = generate_structured(
        prompt, AnswerResponse, "BRIEF_ANSWER", temperature=CLASSIFICATION_TEMPERATURE
    )

    if "error" in result:
        return result

    answer = str(result.get("suggested_answer", "")).strip()

    if not answer or answer.upper().startswith("UNKNOWN"):
        return _no_saved_detail(question)

    result["suggested_answer"] = answer
    result["source"] = FROM_MODEL
    result["billable"] = True
    result["memory_used"] = f"saved detail: {fact.get('question', '')}"
    return result


def _no_saved_detail(question: str) -> dict:
    """A factual question with nothing saved to answer it.

    No model call. Where the candidate lives and how long their notice period
    runs are facts about them, and a model that is asked to produce one will
    produce a plausible one — which is worse than an empty box, because it is
    wrong in a way nobody checks before submitting.
    """
    return {
        "suggested_answer": "",
        "confidence_score": 0,
        "source": NEEDS_PROFILE,
        "billable": False,
        "memory_used": "none",
        "message": (
            "This asks for a fact about you rather than something that can be "
            "written from your resume. Answer it once under Application "
            "answers and it will be filled in automatically from then on."
        ),
    }


def generate_smart_answer(
    user_id: int,
    question: str,
    company: str,
    role: str,
    jd_text: str,
    active_resume_str: str,
) -> dict:
    """Answer one application question, as cheaply as it can be answered well.

    Three routes, in order of preference:

    1. The user's own saved detail, returned verbatim — free and exact.
    2. A closed question with nothing saved: say so, and do not call a model
       to guess at a fact about somebody.
    3. An open question: draft it, which is what this function used to do for
       everything including "Mention Y/N".
    """
    shape = classify_question(question)
    fact = _saved_detail(user_id, question)

    # A custom entry is a question the user answered in their own words, so it
    # is the answer whatever shape the question takes.
    if fact and (shape == BRIEF or str(fact.get("key", "")).startswith("custom:")):
        if not _needs_restating(question, fact):
            return {
                "suggested_answer": str(fact.get("answer", "")).strip(),
                "confidence_score": 100,
                "source": FROM_PROFILE,
                "billable": False,
                "memory_used": f"saved detail: {fact.get('question', '')}",
            }

        return _restate_saved_detail(question, fact)

    if shape == BRIEF:
        return _no_saved_detail(question)

    return _draft_open_answer(
        user_id, question, company, role, jd_text, active_resume_str
    )


def _draft_open_answer(
    user_id: int,
    question: str,
    company: str,
    role: str,
    jd_text: str,
    active_resume_str: str,
) -> dict:
    """Draft an application answer grounded in the user's resume and past answers."""
    memory_context, memory_file = load_answer_memory(user_id, question)

    # 1500 characters of a job description is the "About us" preamble and
    # almost never the requirements, which is what makes an answer specific.
    # The resume was already getting 6000; this is no longer the tight budget
    # it was when the model behind it had a far smaller context window.
    job_description = jd_text[:6000].strip()

    # Said explicitly rather than left blank. An empty section invites the
    # model to fill the gap by inventing what the role probably involves.
    if not job_description:
        job_description = (
            "Not available. Do not guess at what this role involves; "
            "answer from the resume and the question alone."
        )

    prompt = f"""
    You are an expert career coach helping a candidate write a response for a
    job application. Write a concise, professional, highly relevant answer.

    Target Question: {question}
    Target Company: {company or "Not named on the page."}
    Target Role: {role or "Not named on the page."}

    CANDIDATE'S RESUME DATA:
    {active_resume_str[:6000]}

    CANDIDATE'S PREVIOUS ANSWERS (match their authentic facts if available):
    {memory_context[:3000] if memory_context else "No prior context. Draft strictly from the resume."}

    JOB DESCRIPTION:
    {job_description}

    RULES:
    1. Keep it under 200 words.
    2. Sound like an authentic engineer; no generic filler or empty metaphors.
    3. Never invent employers, dates, or metrics that are not in the resume.
    4. Respect any factual metrics provided in previous answers.
    5. Answer the Target Question specifically. Where the job description names
       a requirement the resume can speak to, connect the two explicitly rather
       than describing the candidate in general terms.

    Before answering, re-read the question: {question}
    An answer that would fit equally well under a different question is the
    wrong answer, however well written it is.
    """
    # The one call that wants variety rather than repeatability: the same
    # question asked twice should not come back as the same sentence.
    result = generate_structured(
        prompt, AnswerResponse, "SMART_ANSWER", temperature=DRAFTING_TEMPERATURE
    )

    if "error" not in result:
        result["memory_used"] = memory_file
        result["source"] = FROM_MODEL
        result["billable"] = True

    return result


def convert_pdf_to_json(pdf_raw_text: str) -> dict:
    """Turn raw PDF resume text into a structured profile."""
    if not (pdf_raw_text or "").strip():
        return {
            "error": "EMPTY_PDF",
            "message": "No text could be extracted. The PDF may be a scanned image.",
        }

    prompt = f"""
    You are an expert ATS (Applicant Tracking System) parser.
    Convert the following raw, messy text extracted from a PDF resume into a
    perfectly structured JSON profile.

    RULES:
    1. Extract all skills into a single flat list.
    2. Break experience descriptions into concise bullet points.
    3. If a field is missing (like github), return "N/A".
    4. Fix spacing artifacts and typos caused by PDF extraction.
    5. Never invent information that is not present in the text.

    RAW PDF TEXT:
    {pdf_raw_text[:20000]}
    """
    return generate_structured(prompt, StructuredResume, "PDF_CONVERSION")
