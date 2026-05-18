from integrations.gmail_client import fetch_job_emails
from ai.email_classifier import classify_email
from db import update_job_from_email
import time

def sync_inbox_to_db():
    print("🚀 Starting Inbox Sync...")
    
    emails_to_process = fetch_job_emails()
    
    if not emails_to_process:
        print("No new job emails to process.")
        return
        
    print(f"🤖 Sending {len(emails_to_process)} emails to Gemini for classification...")
    print("⏳ Adding a 4-second delay between emails to prevent API Rate Limiting...\n")
    
    success_count = 0
    
    for email in emails_to_process:
        print(f"Processing: {email['subject'][:60]}...")
        
        ai_result = classify_email(
            sender=email['sender'],
            subject=email['subject'],
            snippet=email['snippet']
        )
        
        # 1. Did the API crash or rate-limit us?
        if "error" in ai_result:
            print(f"   ❌ Skipped (API Error: {ai_result['error']})")
            
        # 2. Did Gemini successfully decide this was junk?
        elif ai_result.get("category") == "UNKNOWN":
            print(f"   ⏭️ Skipped (Classified as UNKNOWN)")
            
        # 3. Success! Save to DB.
        else:
            update_job_from_email(
                company_name=ai_result["company_name"],
                category=ai_result["category"],
                subject=email['subject'],
                reasoning=ai_result["reasoning"]
            )
            success_count += 1
            
        # 👈 THE THROTTLE: Sleep for 4 seconds to stay under 15 requests per minute
        time.sleep(5) 
            
    print(f"\n✅ Sync Complete! Successfully updated {success_count} jobs in the database.")

if __name__ == '__main__':
    sync_inbox_to_db()