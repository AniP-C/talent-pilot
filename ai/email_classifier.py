import json
from google import genai
from pydantic import BaseModel
from enum import Enum
import os
from dotenv import load_dotenv

load_dotenv()
client = genai.Client()

# 1. The 6 Database States
class EmailCategory(str, Enum):
    RECEIVED = "RECEIVED"          # Application confirmed
    REJECTED = "REJECTED"          # Moving forward with others
    ASSESSMENT = "ASSESSMENT"      # HackerRank, OA, Tests
    INTERVIEW = "INTERVIEW"        # HR round, technical round
    OFFER = "OFFER"                # Selected, CTC, Onboarding
    ACTION_REQUIRED = "ACTION_REQUIRED" # Background check, missing docs
    UNKNOWN = "UNKNOWN"            # Newsletters, spam that slipped through the bouncer

# 2. Structured Output Schema
class EmailAnalysis(BaseModel):
    category: EmailCategory
    company_name: str
    reasoning: str

def classify_email(sender, subject, snippet):
    """
    Takes email details and uses Gemini to classify the status and extract the company.
    """
    full_prompt = f"""
    You are an expert ATS and Recruitment Assistant.
    Analyze the following email sent to a job applicant.
    
    1. Categorize the email strictly into one of the allowed categories.
    2. Extract the name of the company the email is regarding. (If unknown or if it is a job board like Indeed/Naukri sending a general alert, output "Unknown").
    3. Provide a 1-sentence reasoning for your classification.
    
    EMAIL SENDER: {sender}
    EMAIL SUBJECT: {subject}
    EMAIL SNIPPET: {snippet}
    """
    
    try:
        # THE CORRECTED API CALL
        response = client.models.generate_content(
            # model="gemini-2.0-flash",
            model = "gemini-2.5-flash-lite",
            contents=full_prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": EmailAnalysis
            }
        )
        
        # Return the parsed JSON
        return json.loads(response.text) #type: ignore
        
    except Exception as e:
        return {"error": str(e)}

