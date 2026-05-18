import sqlite3
from datetime import datetime
from config import logger # Import our new logger!

def create_connection():
    conn = sqlite3.connect('jobs.db', check_same_thread=False)
    return conn

# 1. Create the table
def create_table():
    conn = create_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company TEXT,
        role TEXT,
        jd TEXT,
        status TEXT,
        date_applied TEXT,
        link TEXT,
        notes TEXT
    )
    """)

    # 2. Schema Migration (Phase 6.6)
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN source TEXT DEFAULT 'Manual'")
        logger.info("Added 'source' column to DB.")
    except sqlite3.OperationalError:
        pass # Column already exists
        
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN resume_used TEXT")
        logger.info("Added 'resume_used' column to DB.")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


# 2. Insert a new job application
def add_job(company, role, jd, status, date_applied, link, notes, source="Manual", resume_used=None):
    conn = create_connection() # 🐛 FIX: Changed from get_connection() to create_connection()
    c = conn.cursor()
    
    # 3. Duplicate Protection (Phase 6.2)
    c.execute('''
        SELECT id FROM jobs 
        WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?) AND date_applied = ?
    ''', (company, role, date_applied))
    
    if c.fetchone():
        logger.warning(f"Duplicate prevented: {company} - {role} on {date_applied} already exists.")
        conn.close()
        return False # Tell the UI we skipped it
        
    c.execute('''
        INSERT INTO jobs (company, role, jd, status, date_applied, link, notes, source, resume_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (company, role, jd, status, date_applied, link, notes, source, resume_used))
    
    conn.commit()
    conn.close()
    logger.info(f"Successfully added job: {company} - {role}")
    return True


# 3. Fetch all job applications
def get_all_jobs():
    conn = create_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs")
    jobs = cursor.fetchall()
    conn.close()
    return jobs

# 4. Update job application status
def update_status(job_id, new_status):
    conn = create_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE jobs SET status = ? WHERE id = ?", (new_status, job_id)) 
    conn.commit()
    conn.close()


# 5. Handle AI Email Updates
def update_job_from_email(company_name, category, subject, reasoning):
    """
    Updates a job based on an AI-classified email.
    If the company exists, it updates the status and appends the email details to the notes.
    If it doesn't exist, it creates a new entry.
    """
    conn = create_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT id, notes FROM jobs WHERE LOWER(company) = LOWER(?)", (company_name,))
        result = cursor.fetchone()
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_note_entry = f"[{timestamp} | {category}]\nSubject: {subject}\nAI Note: {reasoning}"
        
        if result:
            job_id = result[0]
            existing_notes = result[1] or ""
            updated_notes = f"{existing_notes}\n\n{new_note_entry}".strip()
            
            cursor.execute("""
                UPDATE jobs 
                SET status = ?, notes = ? 
                WHERE id = ?
            """, (category, updated_notes, job_id))
            print(f"🔄 Updated existing record for {company_name} -> {category}")
            
        else:
            cursor.execute("""
                INSERT INTO jobs (company, status, date_applied, notes)
                VALUES (?, ?, ?, ?)
            """, (company_name, category, timestamp, new_note_entry))
            print(f"🆕 Added new company tracking for {company_name} -> {category}")
            
        conn.commit()
        
    except Exception as e:
        print(f"❌ Database Error: {e}")
        
    finally:
        conn.close()

if __name__ == "__main__":
    create_table()
    
    update_job_from_email(
        company_name="Amazon",
        category="ASSESSMENT",
        subject="Action Required for your Amazon Application – Complete Assessment",
        reasoning="The email requires the applicant to complete an online assessment as the next step in their application process with Amazon."
    )
    
    print("\nCurrent Database Records:")
    for job in get_all_jobs():
        print(job)