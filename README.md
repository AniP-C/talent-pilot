# Job Application Tracker and AI Copilot

A local job-search assistant with a Streamlit dashboard, FastAPI backend, Gemini-powered resume/job-description analysis, Gmail sync, and a Chrome extension for job pages.

## Features

- Track job applications in a local SQLite database.
- Analyze job descriptions against selected resume profiles.
- Convert uploaded PDF resumes into structured JSON profiles.
- Sync recent Gmail recruiting emails and update application status.
- Use a Chrome extension to detect job pages, analyze match score, save jobs, and draft application answers.

## Project Structure

```text
.
|-- app.py                    # Streamlit dashboard
|-- api/server.py             # FastAPI API used by the Chrome extension
|-- ai/
|   |-- resume_parser.py      # Gemini resume/JD and answer generation logic
|   `-- email_classifier.py   # Gemini email classification logic
|-- integrations/
|   `-- gmail_client.py       # Gmail OAuth and email fetch helpers
|-- extension/                # Chrome extension files
|-- db.py                     # SQLite helpers
|-- config.py                 # statuses and logging setup
|-- sync_controller.py        # Gmail sync orchestration
|-- utils.py                  # resume/profile and sync timestamp helpers
`-- requirements.txt
```

## Files Not Committed

The repository intentionally ignores local/private files:

- `.env` with API keys
- `credentials.json` and `token.json` for Gmail OAuth
- `jobs.db` and other local databases
- `data/` resume profiles, generated answer memories, and sync state
- `logs/`
- `extension/rules.js`, because it can contain personal autofill answers

Use `.env.example` and `extension/rules.example.js` as templates.

## Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create your environment file:

```bash
copy .env.example .env
```

Then edit `.env` and add your Gemini API key.

For Gmail sync, create OAuth credentials in Google Cloud and save the downloaded desktop OAuth client as `credentials.json` in the project root. The app will create `token.json` after the first successful Gmail login.

## Running

Start the Streamlit dashboard:

```bash
streamlit run app.py
```

Start the FastAPI backend for the Chrome extension:

```bash
uvicorn api.server:app --reload --port 8000
```

Run Gmail sync directly:

```bash
python sync_controller.py
```

## Chrome Extension

Before loading the extension, create your local autofill rules:

```bash
copy extension\rules.example.js extension\rules.js
```

Edit `extension/rules.js` with your own safe defaults.

To load the extension:

1. Open Chrome and go to `chrome://extensions`.
2. Enable Developer mode.
3. Click "Load unpacked".
4. Select the `extension/` folder.
5. Keep the FastAPI backend running at `http://localhost:8000`.

## Notes

- This project is designed for local personal use.
- Do not commit real resumes, OAuth tokens, Gmail credentials, databases, or personal autofill values.
- The dashboard uses `jobs.db`; it will be created automatically when the app starts.
