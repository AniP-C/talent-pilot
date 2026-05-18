import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def authenticate_gmail():
    """Handles the OAuth 2.0 login and returns the Gmail service."""
    creds = None
    
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)


def is_high_probability_job_email(sender, subject, snippet):
    """
    THE BOUNCER: A fast, cheap rule engine to filter out junk before sending to AI.
    """
    sender_lower = sender.lower()
    content_lower = (subject + " " + snippet).lower()
    combined_text = sender_lower + " " + content_lower

    # 🛡️ PHASE 6.4: The Aggressive Blacklist (Checked FIRST)
    blacklist = ["newsletter", "job alerts", "job alert", "digest", "marketing", "weekly", "campaign"]
    if any(bad_word in combined_text for bad_word in blacklist):
        return False
        
    # 1. Check if the sender matches known ATS or Recruiter patterns
    ats_domains = [
        'greenhouse.io', 'lever.co', 'myworkdayjobs.com', 'smartrecruiters.com', 
        'icims.com', 'successfactors.com', 'taleo.net', 'bamboohr.com'
    ]
    human_indicators = ['talent@', 'careers@', 'recruiting@', 'recruiter@', 'hiring@', 'hr@', 'peopleops@']
    
    if any(domain in sender_lower for domain in ats_domains):
        return True
    if any(indicator in sender_lower for indicator in human_indicators):
        return True

    # 2. Check the text for high-signal job hunting words
    high_signal_words = [
        'application', 'interview', 'assessment', 'offer', 
        'candidate', 'applied', 'moving forward', 'next steps'
    ]
    if any(word in content_lower for word in high_signal_words):
        return True

    return False

def fetch_job_emails():
    """Fetches emails, passes them through the bouncer, and returns a list of valid emails."""
    service = authenticate_gmail()
    print("✅ Successfully connected to Gmail!\n")
    
    search_query = "subject:(application OR update OR status OR role OR position OR interview) newer_than:5d"
    
    try:
        results = service.users().messages().list(userId='me', q=search_query, maxResults=5).execute()
        messages = results.get('messages', [])

        if not messages:
            print("No job-related emails found in the specified timeframe.")
            return []

        print(f"Found {len(messages)} recent emails. Running them through the Bouncer...\n")
        
        valid_emails = []
        
        for msg in messages:
            msg_data = service.users().messages().get(
                userId='me', 
                id=msg['id'], 
                format='metadata', 
                metadataHeaders=['Subject', 'From']
            ).execute()
            
            headers = msg_data['payload']['headers']
            subject = next((header['value'] for header in headers if header['name'] == 'Subject'), "No Subject")
            sender = next((header['value'] for header in headers if header['name'] == 'From'), "Unknown Sender")
            snippet = msg_data.get('snippet', 'No snippet available')
            
            # THE BOUNCER
            if is_high_probability_job_email(sender, subject, snippet):
                valid_emails.append({
                    "sender": sender,
                    "subject": subject,
                    "snippet": snippet
                })
        
        print(f"Bouncer allowed {len(valid_emails)} emails through.")
        return valid_emails

    except Exception as error:
        print(f"An error occurred: {error}")
        return []

if __name__ == '__main__':
    fetch_job_emails()