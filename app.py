import streamlit as st
import pandas as pd
import json
import os
import pypdf

# Self made helper functions
import db
import utils
from ai import resume_parser
from sync_controller import sync_inbox_to_db

# Import our new Config and Logger
from config import VALID_STATUSES, logger

# 0. JD analyzer:
@st.cache_data
def get_ai_analysis(jd_text, resume_string):
    return resume_parser.analyze_jd(jd_text, resume_string)

# 1. Initialize the database and create the table
db.create_table()

# 2. Page configuration
st.set_page_config(page_title="Job Tracker", layout="wide")
st.title("Job Application Tracker")


# =============================================
# SIDEBAR: Automation Panel & Configuration
# =============================================
st.sidebar.title("Automation Panel")

last_sync = utils.get_last_sync()
st.sidebar.caption(f"🕒 **Last Synced:** {last_sync}")

# Feature 1: Trigger the Phase 4 pipeline directly from the UI
if st.sidebar.button("🔄 Sync Gmail Inbox", use_container_width=True):
    with st.spinner("Fetching emails..."):
        try:
            sync_inbox_to_db() 
            utils.update_last_sync()
            st.sidebar.success("Inbox sync complete!")
            logger.info("Manual Gmail sync triggered from UI and completed.")
        except Exception as e:
            st.sidebar.error("Sync failed. Check logs.")
            logger.error(f"Gmail sync failed: {str(e)}")
    st.rerun()

st.sidebar.divider()

# =============================================
# PHASE 9: Profile Onboarding (PDF to JSON)
# =============================================
st.sidebar.subheader("📄 Profile Onboarding")

# Ask the user what they want to name this specific resume track
new_profile_name = st.sidebar.text_input(
    "Name this profile (e.g., sre_profile, ai_engineer):", 
    value="resume_new"
)

# Strict PDF Uploader
uploaded_file = st.sidebar.file_uploader("Upload PDF Resume", type=["pdf"])

if uploaded_file is not None:
    if st.sidebar.button("✨ Convert & Save Profile"):
        
        # Sanitize filename to ensure it ends with .json
        safe_filename = new_profile_name.strip().replace(" ", "_")
        if not safe_filename.endswith(".json"):
            safe_filename += ".json"

        with st.sidebar.status("Extracting and Parsing PDF...", expanded=True) as status:
            try:
                # Read the PDF without saving it to disk
                status.write("Reading PDF text...")
                reader = pypdf.PdfReader(uploaded_file)
                raw_text = ""
                for page in reader.pages:
                    raw_text += page.extract_text() + "\n"
                
                # Hand it to Gemini
                status.write("Gemini is structuring the data...")
                structured_json = resume_parser.convert_pdf_to_json(raw_text)
                
                if "error" in structured_json:
                    status.update(label="Conversion Failed!", state="error")
                    st.sidebar.error(structured_json["message"])
                else:
                    # Save the golden JSON to the data folder
                    status.write(f"Saving as {safe_filename}...")
                    
                    if not os.path.exists("data"):
                        os.makedirs("data")
                        
                    file_path = os.path.join("data", safe_filename)
                    
                    with open(file_path, "w", encoding="utf-8") as f:
                        json.dump(structured_json, f, indent=4)
                        
                    status.update(label="Profile Created!", state="complete")
                    st.sidebar.success(f"Successfully created {safe_filename}!")
                    
            except Exception as e:
                status.update(label="System Error", state="error")
                st.sidebar.error(f"Failed to process PDF: {str(e)}")

st.sidebar.divider()

# =============================================
# Feature 3: File-Based Resume Selection Dashboard
# =============================================
st.sidebar.subheader("🎯 Active Target Profile")
resume_dir = "data"

if not os.path.exists(resume_dir):
    os.makedirs(resume_dir)

# Read available JSON resume variants dynamically
resume_files = [f for f in os.listdir(resume_dir) if f.endswith('.json')]

if resume_files:
    selected_resume_file = st.sidebar.selectbox(
        "Select your active track:", 
        resume_files,
        help="Select which parsed resume track Gemini will use for your match evaluation."
    )
else:
    st.sidebar.warning("⚠️ No profiles found. Upload a PDF above to generate one.")
    selected_resume_file = None


# =============================================
# MAIN LAYOUT: Create a layout with two columns
# =============================================
col1, col2 = st.columns([1,2]) 

# =============================================
# Column 1: Job Application Form    
# =============================================
with col1:
    st.header("Add a New Job Application")

    with st.form("job_form", clear_on_submit=True):
        company = st.text_input("Company Name")
        role = st.text_input("Role")
        jd = st.text_area("Job Description")
        
        # Phase 6.3: Use SSOT for Statuses
        status = st.selectbox("Application Status", VALID_STATUSES)
        
        # Phase 6.6: Add Source and Resume Selectors
        source = st.selectbox("Source", ["LinkedIn", "Company Site", "Wellfound", "Indeed", "Manual"])
        
        # Get resumes from the data folder for the dropdown
        resume_options = ["None"] + [f for f in os.listdir("data") if f.endswith('.json')]
        resume_used = st.selectbox("Resume Used", resume_options)
        
        date_applied = st.date_input("Date Applied")
        link = st.text_input("Job Posting Link")
        notes = st.text_area("Additional Notes")

        submitted = st.form_submit_button("Add Job Application")

        if submitted:
            if company.strip() and role.strip():
                # Call updated DB function
                success = db.add_job(
                    company, role, jd, status, 
                    date_applied.strftime("%Y-%m-%d"), 
                    link, notes, source, 
                    None if resume_used == "None" else resume_used
                )
                if success:
                    st.success(f"Added job application for {company} - {role}")
                else:
                    st.warning(f"Skipped: Application for {company} on this date already exists!")
                st.rerun()
            else:
                st.error("Please fill in all required fields.")


# =============================================
# Column 2: View and update job applications
# =============================================
with col2:
    st.header("Your Job Applications")

    # Fetch all job applications from the DB
    jobs = db.get_all_jobs()

    if jobs:
        columns = ["ID", "Company", "Role", "Job Description", "Status", "Date Applied", "Link", "Notes", "Source", "Resume Used"]
        df = pd.DataFrame(jobs, columns=columns)
        
        st.subheader("🔍 Quick Filter & Search")
        search_col1, search_col2 = st.columns(2)
        with search_col1:
            search_company = search_col1.text_input("Search Company Name", value="", placeholder="e.g. Amazon")
        with search_col2:
            search_role = search_col2.text_input("Search Job Title / Role", value="", placeholder="e.g. Engineer")

        status_filter = st.radio("Filter by Status", ["All", "Applied", "Interviewing", "Offered", "Rejected"], horizontal=True)

        filtered_df = df.copy()
        
        if search_company:
            filtered_df = filtered_df[filtered_df["Company"].str.contains(search_company, case=False, na=False)]
        if search_role:
            filtered_df = filtered_df[filtered_df["Role"].str.contains(search_role, case=False, na=False)]
        if status_filter != "All":
            filtered_df = filtered_df[filtered_df["Status"] == status_filter]

        display_df = filtered_df[["Company", "Role", "Status", "Date Applied"]]
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        csv_data = filtered_df.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="📥 Export Current Selection to CSV",
            data=csv_data,
            file_name="tracked_applications.csv",
            mime="text/csv",
            use_container_width=False
        )

        st.divider() 

        st.subheader("Update Application Status")
        job_options = {f"{row[1]} - {row[2]} (Current: {row[4]})": row[0] for row in jobs}

        selected_job_label = st.selectbox("Select a job application to update", list(job_options.keys()))
        new_status = st.selectbox("New Status", ["Applied", "Interviewing", "Offered", "Rejected"], key="status_update")

        if st.button("Update Status"):
            job_id = job_options[selected_job_label]
            db.update_status(job_id, new_status)
            st.success(f"Updated status for {selected_job_label} to {new_status}")
            st.rerun() 

        st.divider()
        st.subheader("Job Description Analyzer")
        analyze_job_label = st.selectbox("Select Job to Analyze", list(job_options.keys()), key="analyze_select") 

        if st.button("Analyze Match"):
            job_id = job_options[analyze_job_label]
            selected_job = next((job for job in jobs if job[0] == job_id), None)
            
            if not selected_resume_file: 
                st.error("Please create or upload a parsed resume profile in the sidebar first.")
            elif selected_job and selected_job[3]: 
                jd_text = selected_job[3]
                
                with open(os.path.join(resume_dir, selected_resume_file), 'r') as f:
                    resume_dict = utils.load_resumes(selected_resume_file)
                resume_str = json.dumps(resume_dict) 
                
                with st.spinner(f"🤖 Gemini is analyzing matching metrics against {selected_resume_file}..."):
                    result = get_ai_analysis(jd_text, resume_str)
                
                if "error" in result:
                    st.error(f"API Error: {result['error']}")
                else:
                    st.metric(label="AI Match Score", value=f"{result['match_percentage']}%")
                    st.info(f"**Recruiter Summary:** {result['summary']}")
                    
                    col_match, col_miss = st.columns(2)
                    with col_match:
                        st.success("✅ Matched Skills")
                        for skill in result['matched_skills']:
                            st.write(f"- {skill}")
                            
                    with col_miss:
                        st.error("❌ Missing Skills")
                        for skill in result['missing_skills']:
                            st.write(f"- {skill}")
            else:
                st.warning("No Job Description available for this role. Please update the job with a JD to analyze.")

    else:
        st.info("No job applications found. Please add some using the form on the left.")