import json
import os
import sys
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

# Initialize system configurations and environment logs
load_dotenv()

# Centralized logger hook from your root configurations
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import logger

client = genai.Client()

# =====================================================================
# 📋 PYDANTIC SCHEMAS FOR STRUCTURED OUTPUT MATRIX
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

# Phase 9: PDF Parsing Schemas
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
# 🧠 REUSABLE INTERNAL EXCEPTION MANAGER
# =====================================================================
def _handle_api_exception(exception_obj: Exception, context_tag: str) -> dict:
    error_message = str(exception_obj)
    logger.error(f"Gemini API failure during [{context_tag}]: {error_message}")
    
    if "429" in error_message or "RESOURCE_EXHAUSTED" in error_message:
        return {"error": "RATE_LIMIT", "message": "API Quota Exceeded. Please try again in a bit."}
    
    return {"error": "GENERAL_ERROR", "message": "Processing failed. Please check back shortly."}

# =====================================================================
# 🚀 SYSTEM FUNCTIONAL ENDPOINTS
# =====================================================================
def analyze_jd(jd_text: str, resume_data: str) -> dict:
    full_prompt = f"""
    You are an expert Tech Recruiter/ATS system. 
    Analyze the following Job Description against the provided Resume.
    Check for missing skill sets, matched skills, and summarize the gap.
    
    JOB DESCRIPTION:
    {jd_text}
    
    RESUME:
    {resume_data}
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=full_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=JDAnalysis,
            ),
        )
        return json.loads(response.text) # type: ignore
    except Exception as e:
        return _handle_api_exception(e, "JD_ANALYSIS")

def generate_smart_answer(question: str, company: str, role: str, jd_text: str, active_resume_str: str) -> dict:
    q_lower = question.lower()
    memory_file = "none"
    memory_context = ""
    
    if "about yourself" in q_lower or "background" in q_lower:
        memory_file = "about_me.txt"
    elif "why" in q_lower and ("company" in q_lower or company.lower() in q_lower):
        memory_file = "why_company.txt"
    elif "challenge" in q_lower or "difficult" in q_lower:
        memory_file = "challenging_project.txt"
    elif "weakness" in q_lower:
        memory_file = "weaknesses.txt"

    if memory_file != "none":
        file_path = os.path.join("data", "answers", memory_file)
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                memory_context = f.read()

    prompt = f"""
    You are an expert career coach helping a candidate write a response for a job application.
    Write a concise, professional, and highly relevant answer to the target question.
    
    Target Question: {question}
    Target Company: {company}
    Target Role: {role}
    
    CANDIDATE'S RESUME DATA:
    {active_resume_str}
    
    CANDIDATE'S PREVIOUS ANSWERS/MEMORY (Match their authentic facts if available):
    {memory_context if memory_context else "No prior context provided. Draft based strictly on resume skills and core metrics."}
    
    JOB DESCRIPTION FRAGMENT:
    {jd_text[:1500]}
    
    RULES:
    1. Keep it under 200 words.
    2. Sound like an authentic engineer; do not use generic filler words or empty metaphors.
    3. Respect any factual metrics provided in previous answers.
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AnswerResponse,
            ),
        )
        return json.loads(response.text) # type: ignore
    except Exception as e:
        return _handle_api_exception(e, "SMART_ANSWER_GENERATION")

def save_answer_to_memory(category: str, answer_text: str) -> bool:
    os.makedirs(os.path.join("data", "answers"), exist_ok=True)
    
    cat_lower = category.lower()
    filename = "general.txt"
    if "about" in cat_lower: filename = "about_me.txt"
    elif "why" in cat_lower: filename = "why_company.txt"
    elif "challenge" in cat_lower: filename = "challenging_project.txt"
    elif "weakness" in cat_lower: filename = "weaknesses.txt"
    
    file_path = os.path.join("data", "answers", filename)
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(f"\n\n--- Saved Answer ---\n{answer_text}")
    return True

def convert_pdf_to_json(pdf_raw_text: str) -> dict:
    prompt = f"""
    You are an expert ATS (Applicant Tracking System) parser.
    Take the following raw, messy text extracted from a PDF resume and convert it 
    into a perfectly structured JSON profile. 
    
    RULES:
    1. Extract all skills into a single flat list.
    2. Break down experience descriptions into concise bullet points.
    3. If a field is missing (like github), return "N/A".
    4. Fix any weird spacing or typos caused by the PDF extraction.
    
    RAW PDF TEXT:
    {pdf_raw_text}
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StructuredResume,
            ),
        )
        return json.loads(response.text) # type: ignore
    except Exception as e:
        return _handle_api_exception(e, "PDF_CONVERSION")

# =====================================================================
# 🔬 ROOT EXHAUSTION DISCOVERY VERIFICATION
# =====================================================================
if __name__ == "__main__":
    print("🔬 Core execution runner initialized. Awaiting pipeline calls from FastAPI / Streamlit.")