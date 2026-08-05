"""Gemini-backed resume parsing, JD analysis, and answer drafting.

All answer-memory reads and writes are scoped to a single user's workspace, so
one person's saved answers never leak into another person's drafts.
"""

import os
import sys

from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workspace
from ai.gemini import generate_structured
from config import logger

# =====================================================================
# STRUCTURED OUTPUT SCHEMAS
# =====================================================================
class JDAnalysis(BaseModel):
    match_percentage: int
    matched_skills: list[str]
    missing_skills: list[str]
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
    """Score a resume against a job description."""
    prompt = f"""
    You are an expert Tech Recruiter/ATS system.
    Analyze the following Job Description against the provided Resume.
    Identify matched skills, missing skills, and summarize the gap honestly.

    JOB DESCRIPTION:
    {jd_text[:8000]}

    RESUME:
    {resume_data[:8000]}
    """
    return generate_structured(prompt, JDAnalysis, "JD_ANALYSIS")


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

    prompt = f"""
    You are an expert career coach helping a candidate write a response for a
    job application. Write a concise, professional, highly relevant answer.

    Target Question: {question}
    Target Company: {company}
    Target Role: {role}

    CANDIDATE'S RESUME DATA:
    {active_resume_str[:6000]}

    CANDIDATE'S PREVIOUS ANSWERS (match their authentic facts if available):
    {memory_context[:3000] if memory_context else "No prior context. Draft strictly from the resume."}

    JOB DESCRIPTION FRAGMENT:
    {jd_text[:1500]}

    RULES:
    1. Keep it under 200 words.
    2. Sound like an authentic engineer; no generic filler or empty metaphors.
    3. Never invent employers, dates, or metrics that are not in the resume.
    4. Respect any factual metrics provided in previous answers.
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
