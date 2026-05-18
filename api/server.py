# api/server.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sys
import os

# Add parent directory to path so we can import our existing modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import utils
from ai.resume_parser import analyze_jd
from ai.resume_parser import generate_smart_answer, save_answer_to_memory

app = FastAPI(title="Job AI Assistant API")

# ⚠️ CRITICAL: Allow your Chrome Extension to talk to this local server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, restrict this. For local extension MVP, "*" is fine.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models for incoming data ---
class JobData(BaseModel):
    company: str
    role: str
    jd_text: str
    link: str
    profile: Optional[str] = None  # 🎯 NEW: Tells backend which persona to use

class CheckJobRequest(BaseModel):
    company: str
    role: str

class AnswerRequest(BaseModel):
    question: str
    company: str
    role: str
    jd_text: str
    profile: Optional[str] = None  # 🎯 NEW: Tells backend which persona to use

class SaveAnswerRequest(BaseModel):
    question: str
    answer: str

# --- API Endpoints ---

@app.get("/profiles")
def get_profiles():
    """Returns a list of all available JSON resume profiles in the data folder."""
    data_dir = "data"
    if not os.path.exists(data_dir):
        return {"profiles": []}
        
    profiles = [f for f in os.listdir(data_dir) if f.endswith('.json')]
    return {"profiles": profiles}

@app.post("/check-job")
def check_job(request: CheckJobRequest):
    """Checks if a job is already in the database (Phase 7.5)"""
    conn = db.create_connection()
    c = conn.cursor()
    c.execute('''
        SELECT status FROM jobs 
        WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?)
    ''', (request.company, request.role))
    result = c.fetchone()
    conn.close()
    
    if result:
        return {"exists": True, "status": result[0]}
    return {"exists": False}

@app.post("/analyze-job")
def analyze_job(job: JobData):
    """Trigger the Gemini JD analyzer"""
    try:
        # Load the specific resume requested, otherwise fallback to default
        if job.profile:
            resume_dict = utils.load_resumes(job.profile)
        else:
            resume_dict = utils.load_resumes() 
            
        import json
        resume_str = json.dumps(resume_dict)
        
        analysis = analyze_jd(job.jd_text, resume_str)
        return analysis
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/save-job")
def save_job(job: JobData):
    """Save the job directly to the SQLite database"""
    import datetime
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    success = db.add_job(
        company=job.company,
        role=job.role,
        jd=job.jd_text,
        status="APPLIED", # Default starting status
        date_applied=today,
        link=job.link,
        notes="Added via AI Browser Extension",
        source="Web Extension",
        resume_used=job.profile # Save which resume was used
    )
    
    if not success:
        raise HTTPException(status_code=400, detail="Job already exists on this date.")
    return {"message": "Job saved successfully!"}

@app.post("/generate-answer")
def generate_answer(req: AnswerRequest):
    """Triggers the RAG-lite Gemini pipeline"""
    try:
        # Load the specific resume requested, otherwise fallback to default
        if req.profile:
            resume_dict = utils.load_resumes(req.profile)
        else:
            resume_dict = utils.load_resumes()
            
        import json
        resume_str = json.dumps(resume_dict)
        
        result = generate_smart_answer(
            req.question, req.company, req.role, req.jd_text, resume_str
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/save-answer")
def save_answer(req: SaveAnswerRequest):
    """Saves a good answer back to the text file memory"""
    save_answer_to_memory(req.question, req.answer)
    return {"message": "Saved to memory bank!"}