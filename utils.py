# All the data loading and matching will be done here, so that app.py can focus on the UI and user interactions. This separation of concerns makes the code cleaner and easier to maintain.

import os
import json
from datetime import datetime
SYNC_FILE = "data/last_sync.txt"

# 1. load the resumes
def load_resumes(filename="resume_ai.json"): # 🔄 Defaults to resume_ai.json if no name is given
    base_path = "data"
    full_path = os.path.join(base_path, filename)
    
    # If the specific file doesn't exist, fall back to whatever JSON file is available
    if not os.path.exists(full_path):
        available_files = [f for f in os.listdir(base_path) if f.endswith('.json')]
        if available_files:
            full_path = os.path.join(base_path, available_files[0])
        else:
            # Return an empty structured resume dictionary so the app doesn't crash
            return {"name": "Default Profile", "skills": [], "experience": []}
            
    with open(full_path, "r") as f:
        return json.load(f)

# 2. Extract keywords from JD

def extract_keywords(jd_text):
    if not jd_text:
        return []
    
    jd_text = jd_text.lower() # Convert to lowercase for case-insensitive matching

    # We can expand this list later. Added a mix of your JSON skills and others to test missing/matched.
    known_skills = [
        "python", "selenium", "sql", "docker", "pydantic", "fastapi",
        "kubernetes", "api", "testing", "aws", "langchain", "azure"
    ]

    found_skills = [skill for skill in known_skills if skill in jd_text] # Check if each known skill is present in the JD text
    return found_skills


# 3. Match JD keywords with resume skills
def match_skills(resume_data, jd_skills):
    # 1. Safely extract the list of skills from the dictionary
    actual_skills_list = resume_data.get("skills", [])
    
    # 2. Normalize resume skills to lowercase for accurate comparison
    resume_skills_lower = [s.lower() for s in actual_skills_list]

    # 3. Compare JD skills against the actual skills list
    matched = [skill for skill in jd_skills if skill in resume_skills_lower] 
    missing = [skill for skill in jd_skills if skill not in resume_skills_lower] 

    if not jd_skills: 
        return 0, [], [] 
    
    match_percentage = (len(matched) / len(jd_skills)) * 100 

    return match_percentage, matched, missing


# 4. Sync timestamp management and display
def update_last_sync():
    """Writes the current timestamp to a persistent file."""
    os.makedirs("data", exist_ok=True)
    with open("data/last_sync.txt", "w") as f:
        # Format it nicely: "May 18, 2026 at 03:45 PM"
        f.write(datetime.now().strftime("%b %d, %Y at %I:%M %p"))

def get_last_sync():
    """Reads the last sync timestamp."""
    try:
        with open("data/last_sync.txt", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Never"

