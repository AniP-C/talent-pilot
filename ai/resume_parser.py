"""Gemini-backed resume parsing, JD analysis, and answer drafting.

All answer-memory reads and writes are scoped to a single user's workspace, so
one person's saved answers never leak into another person's drafts.
"""

import os
import sys

from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scoring
import workspace
from ai.gemini import generate_structured
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
    (("why this company", "why do you want", "why are you interested"), "why_company.txt"),
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
        return path.read_text(encoding="utf-8").strip(), filename
    except OSError as exc:
        logger.error("Could not read answer memory %s: %s", filename, exc)
        return "", "none"


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
def analyze_jd(jd_text: str, resume_data: str) -> dict:
    """Compare a resume to a job description.

    The model is asked only to classify each requirement. The percentage is
    computed from those classifications in ``scoring.py``, so it is
    reproducible and can be shown with its working. Asking the model for the
    number produced one that ignored its own findings — an analysis listing a
    dozen absent requirements still came back 90%.
    """
    prompt = f"""
    You are a technical recruiter assessing one candidate against one job.

    List every distinct requirement the job description states, and for each
    one decide:

      importance  "required"  the job presents it as a must-have.
                  "preferred" the job calls it preferred, a bonus, a plus, or
                              nice to have.

      status      "demonstrated" the resume shows clear, specific evidence.
                  "partial"      the resume shows something adjacent or
                                 transferable but not the thing itself — a
                                 different cloud provider, or the underlying
                                 technique without the named tool.
                  "absent"       there is no evidence in the resume.

      evidence    where in the resume you saw it, or "" when absent.

    RULES:
    1. One entry per distinct requirement. Never list the same skill twice
       under different names: "LLM" and "Large Language Models" are one
       requirement, as are "GCP" and "Google Cloud".
    2. Only concrete skills, tools, platforms, and domain experience. Never
       list job-description prose such as "collaborate", "best practices",
       "solutions", "technical" or "development".
    3. Do not award "demonstrated" for something the resume merely implies.
       Adjacent evidence is "partial". Be strict: this assessment is only
       useful if it is honest about gaps.
    4. Do not produce a score or a percentage anywhere. The score is computed
       from your classifications.

    Then write a short summary of the fit, naming the strongest evidence and
    the most significant gaps.

    JOB DESCRIPTION:
    {jd_text[:8000]}

    RESUME:
    {resume_data[:8000]}
    """
    result = generate_structured(prompt, JDAnalysis, "JD_ANALYSIS")

    if "error" in result:
        return result

    requirements = scoring.normalise_requirements(result.get("requirements"))
    coverage = scoring.score_requirements(requirements)
    keywords = scoring.keyword_coverage(jd_text, resume_data)

    return {
        # Kept so the dashboard and the extension popup continue to work; both
        # read these three, and neither should have to know how the number is
        # arrived at.
        "match_percentage": coverage["score"],
        "matched_skills": [
            r["skill"] for r in requirements if r["status"] != "absent"
        ],
        "missing_skills": [r["skill"] for r in requirements if r["status"] == "absent"],
        "summary": result.get("summary", ""),
        # The working behind the number, plus the filter's-eye view.
        "requirements": requirements,
        "coverage": coverage,
        "keyword_coverage": keywords,
    }


def generate_smart_answer(
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
    """
    result = generate_structured(prompt, AnswerResponse, "SMART_ANSWER")

    if "error" not in result:
        result["memory_used"] = memory_file

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
