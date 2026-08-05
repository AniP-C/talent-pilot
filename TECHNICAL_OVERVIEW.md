# Talent Pilot Technical Overview

This document explains the code structure, runtime flows, and implementation
details behind Talent Pilot. The README is the portfolio/GitHub overview; this
file is for developers who want to understand how the system is wired
internally.

## System Summary

Talent Pilot is a local-first job application assistant built from six parts:

1. **Accounts layer** — registration, sign-in, password hashing, API tokens.
2. **Streamlit dashboard** — manual tracking, resume onboarding, JD analysis.
3. **FastAPI backend** — token-authenticated API used by the Chrome extension.
4. **Gemini AI layer** — JD analysis, answer drafting, email classification, PDF parsing.
5. **Gmail integration** — per-user recruiting email sync.
6. **Chrome extension** — job-page extraction, match analysis, save actions, form assistance.

Job records, resume profiles, and answer memory are stored per account in
isolated workspace directories under `data/`. Nothing leaves the machine except
Gemini and Gmail API calls.

## High-Level Architecture

```mermaid
flowchart LR
    subgraph Browser
        PAGE[Job/Application Page]
        CS[content.js]
        POP[popup.js]
        BG[background.js<br/>owns the token]
    end

    subgraph LocalBackend
        API[FastAPI: api/server.py]
        UI[Streamlit: app.py]
        AUTH[auth.py]
        WS[workspace.py]
        DB[(Per-user jobs.db)]
        USERS[(users.db)]
    end

    subgraph AI
        GEM[ai/gemini.py]
        RP[ai/resume_parser.py]
        EC[ai/email_classifier.py]
        GEMINI[Gemini API]
    end

    GMAIL[Gmail API]

    PAGE --> CS
    CS --> BG
    POP --> BG
    BG -->|Bearer token| API
    API --> AUTH
    AUTH --> USERS
    API --> WS
    WS --> DB
    UI --> AUTH
    UI --> WS
    API --> RP
    UI --> RP
    RP --> GEM
    EC --> GEM
    GEM --> GEMINI
    GMAIL --> EC
    EC --> DB
```

## Repository Structure

```text
.
|-- app.py                  Streamlit entrypoint
|-- ui.py                   Dashboard styling and render helpers
|-- auth.py                 Accounts, password hashing, API tokens
|-- workspace.py            Per-user paths and path-traversal defences
|-- db.py                   Job storage (per workspace)
|-- config.py               Paths, status SSOT, settings, logging
|-- utils.py                Profile loading, sync timestamps
|-- sync_controller.py      Gmail sync orchestration
|-- requirements.txt
|-- requirements-dev.txt
|-- pytest.ini
|-- api/
|   `-- server.py           Token-authenticated FastAPI app
|-- ai/
|   |-- gemini.py           Shared client + structured-output helper
|   |-- resume_parser.py    JD analysis, answers, PDF parsing
|   `-- email_classifier.py Recruiter email classification
|-- integrations/
|   `-- gmail_client.py     Gmail OAuth and fetch, per user
|-- extension/
|   |-- manifest.json
|   |-- background.js       Service worker: token + all API calls
|   |-- popup.html
|   |-- popup.js
|   |-- content.js
|   `-- rules.example.js
|-- tests/
|   |-- conftest.py
|   |-- test_auth.py
|   |-- test_db.py
|   |-- test_workspace.py
|   |-- test_email_pipeline.py
|   `-- test_api.py
|-- data/                   Accounts + per-user workspaces (gitignored)
`-- logs/                   Rotating logs (gitignored)
```

## Identity Model

### Accounts

`auth.py` owns a single central database, `data/users.db`:

```sql
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE api_tokens (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_used_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
```

Password digests are self-describing:

```text
pbkdf2_sha256$600000$<base64 salt>$<base64 hash>
```

Design points worth knowing:

- **Constant-time failure.** `authenticate()` verifies against a dummy hash
  when the email is unknown, so response time does not reveal whether an
  account exists.
- **Tokens are stored hashed.** Only the SHA-256 digest is persisted, so a
  copy of `users.db` does not yield usable sessions.
- **Password change revokes tokens.** `change_password()` deletes every row in
  `api_tokens` for that user.

### Workspaces

`workspace.py` maps an account id to an isolated directory:

```text
data/workspaces/<user_id>/
    jobs.db
    profiles/*.json
    answers/*.txt
    gmail_token.json
    last_sync.txt
```

Keying on the **numeric account id** rather than on the email address is the
central isolation decision: no caller-supplied string participates in path
construction, so there is no input that can steer a read or write into another
user's directory.

Filenames that *do* come from callers (profile names) pass through two gates:

```python
sanitize_filename(name)      # strips separators, traversal, unsafe chars
resolve_within(dir, name)    # resolves and refuses to escape `dir`
```

`api/server.py` is stricter still — it rejects any `profile` value that is not
already a plain filename, so the stored `resume_used` always equals the file
that will later be loaded.

## Module Responsibilities

### `config.py`

Single source of truth for paths, settings, and vocabulary. Paths resolve from
the module's own location, not the working directory, so behaviour is identical
under Streamlit, uvicorn, and pytest. Every setting reads from the environment
with a sensible default.

`VALID_STATUSES` is the status SSOT consumed by the UI, the API, storage, and
the email classifier:

```python
["APPLIED", "ASSESSMENT", "INTERVIEW", "OFFER", "REJECTED", "ACTION_REQUIRED"]
```

Logging uses a `RotatingFileHandler` (2 MB × 3 backups) plus console output,
with `propagate = False` so Streamlit reruns do not duplicate records.

### `db.py`

Per-workspace SQLite persistence. Every function takes the `db_path` it acts
on; there is no default path, because there is no such thing as a
workspace-less write.

```sql
CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company      TEXT NOT NULL,
    role         TEXT NOT NULL,
    jd           TEXT,
    status       TEXT NOT NULL DEFAULT 'APPLIED',
    date_applied TEXT,
    link         TEXT,
    notes        TEXT,
    source       TEXT NOT NULL DEFAULT 'Manual',
    resume_used  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_jobs_identity ON jobs (LOWER(company), LOWER(role));
CREATE INDEX idx_jobs_status ON jobs (status);

CREATE TABLE processed_emails (
    message_id   TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
```

Key properties:

- **Duplicate identity is `company + role`**, enforced by a unique index rather
  than a SELECT-then-INSERT, which removes the race and makes the rule the same
  everywhere. `add_job` raises `DuplicateJobError`.
- **Rows are dicts.** `get_all_jobs()` returns `list[dict]` keyed by column
  name, so callers cannot break when a migration appends a column.
- **Connections are context-managed.** `connect()` commits on success, rolls
  back on exception, and always closes — which on Windows is the difference
  between a clean exit and a locked file.
- **Migrations are version-stamped** via `PRAGMA user_version`, and opening a
  database written by a newer schema raises rather than corrupting it.
- **`processed_emails`** makes repeat inbox syncs no-ops.

### `auth.py`

Covered under [Identity Model](#identity-model). Public surface:

```python
register(email, password) -> User
authenticate(email, password) -> User          # raises AuthError
get_user(user_id) -> User | None
change_password(user_id, current, new) -> None
issue_token(user_id) -> str
verify_token(token) -> User | None
revoke_token(token) -> None
```

### `app.py` and `ui.py`

`app.py` is the Streamlit entrypoint. It renders an auth screen when
`st.session_state.user` is absent, and the dashboard otherwise. The dashboard
is split into five tabs — Dashboard, Add application, Analyzer, Profiles,
Settings — with the sidebar holding the account chip, active profile selector,
and Gmail sync controls.

`ui.py` holds the CSS and small render helpers (`render_metrics`,
`status_label`, `account_chip`). The styling is deliberately restrained:
spacing, metric cards, and tab treatment on top of the default theme, using
Streamlit's own CSS variables so it follows light and dark mode.

### `api/server.py`

FastAPI app used by the extension. The contract that matters:

**Identity comes from the bearer token, never from the request body.** No
endpoint accepts a `user_email` field. `current_user` is a dependency that
resolves the token or raises 401.

```text
GET    /health                      no auth
POST   /auth/register               no auth -> token
POST   /auth/login                  no auth -> token
POST   /auth/logout                 token
GET    /auth/me                     token
GET    /profiles                    token
GET    /jobs                        token
POST   /check-job                   token
POST   /save-job                    token (409 on duplicate)
PATCH  /jobs/{id}/status            token
POST   /analyze-job                 token
POST   /generate-answer             token
POST   /save-answer                 token
```

CORS accepts only origins matching `ALLOWED_ORIGIN_REGEX`, which defaults to
`^chrome-extension://[a-p]{32}$`. A regex avoids hardcoding an extension id
that changes between installs, while still excluding every ordinary web page —
the real CSRF risk against a service on localhost.

### `ai/gemini.py`

Shared client and error mapping. The client is built lazily on first use, so a
missing `GEMINI_API_KEY` surfaces as a handled error in the UI instead of
crashing startup. `generate_structured(prompt, schema, tag)` runs a
structured-output call and returns either parsed JSON or an error dict shaped
`{"error": CODE, "message": text}` with codes `RATE_LIMIT`, `AUTH_ERROR`,
`CONFIG_ERROR`, and `GENERAL_ERROR`.

### `ai/resume_parser.py`

```python
analyze_jd(jd_text, resume_data) -> dict
generate_smart_answer(user_id, question, company, role, jd_text, resume_str) -> dict
save_answer_to_memory(user_id, question, answer_text) -> str
convert_pdf_to_json(pdf_raw_text) -> dict
categorize_question(question) -> str
```

Answer memory is **workspace-scoped** — both reads and writes take a
`user_id`, so one person's saved answers never enter another person's prompt.
Reader and writer share one `ANSWER_CATEGORIES` mapping, so a saved answer is
always found again.

Prompts bound their inputs (`jd_text[:8000]`, `resume[:6000]`, and so on) to
keep token usage predictable, and instruct the model never to invent employers,
dates, or metrics absent from the resume.

### `ai/email_classifier.py`

Classifies an email into a model category, then `to_status()` maps that onto
the storage vocabulary:

```python
RECEIVED         -> APPLIED       # a confirmation just means "logged"
INTERVIEW        -> INTERVIEW
OFFER            -> OFFER
REJECTED         -> REJECTED
ASSESSMENT       -> ASSESSMENT
ACTION_REQUIRED  -> ACTION_REQUIRED
UNKNOWN / other  -> None          # caller skips it
```

Returning `None` rather than guessing is what lets the sync loop skip noise
instead of writing junk rows.

### `integrations/gmail_client.py`

Per-user OAuth. Tokens are written to `workspace.gmail_token_path(user_id)`,
never to a shared `token.json`.

`authenticate_gmail(user_id, allow_interactive)` — the API server passes
`allow_interactive=False` so a background request can never block waiting for
someone to click through a Google consent screen; only the Streamlit sidebar
opens the browser flow.

`is_high_probability_job_email()` is the cheap pre-filter that runs before any
paid AI call. Order matters: the blacklist is checked first, because marketing
blasts often contain the same words as genuine recruiter mail. Signals are
matched as **phrases, not bare words** — a lone `offer` also matches "limited
time offer", which is exactly what the filter exists to exclude.

### `sync_controller.py`

```python
sync_inbox_to_db(user_id, progress_callback=None, throttle_seconds=4.0) -> dict
```

Flow: resolve the user's workspace → fetch → drop already-processed message ids
→ classify → map category to status → write → mark processed. Returns
`{"fetched", "updated", "created", "skipped", "failed"}`.

Two details:

- The throttle sleeps **between** calls, not after the last one, so a
  single-email sync does not sit idle at the end.
- Unusable results are still marked processed, so the next run does not pay to
  classify the same noise again.

### Chrome extension

Three scripts with a strict separation:

| File | Runs in | Holds the token | Talks to the API |
| ---- | ------- | --------------- | ---------------- |
| `background.js` | Service worker | Yes | Yes |
| `popup.js` | Extension page | No | No — messages background |
| `content.js` | Page context | No | No — messages background |

`background.js` is the only network caller. This buys two things: the token
never enters a context a page could reach, and requests originate from the
extension's own `chrome-extension://` origin, which is the only origin the API
accepts. A 401 clears the stored session rather than leaving the popup
half-signed-in.

`content.js` renders every scraped value with `textContent` and builds nodes
directly. Job postings are untrusted input, and the popup is a privileged page.

Scanning uses a debounced `MutationObserver` rather than a polling interval, so
the script reacts to asynchronous job-board rendering without re-querying the
DOM every few seconds on every open tab.

Host permissions are limited to the supported boards plus
`http://localhost:8000/*`. Any other site is opt-in per origin through
**Enable Copilot on this site**, which requests `optional_host_permissions` and
injects the scripts on demand.

## Runtime Flows

### Registration and sign-in

```mermaid
sequenceDiagram
    participant U as User
    participant S as Streamlit
    participant A as auth.py
    participant W as workspace.py

    U->>S: email + password
    S->>A: register() / authenticate()
    A->>A: PBKDF2 hash or constant-time verify
    A-->>S: User(id, email)
    S->>W: jobs_db_path(user.id)
    W-->>S: data/workspaces/<id>/jobs.db
    S->>S: session_state.user = {...}
```

### Extension saving a job

```mermaid
sequenceDiagram
    participant P as Job page
    participant C as content.js
    participant B as background.js
    participant API as FastAPI
    participant DB as workspace db

    P->>C: DOM
    C->>C: extractJobData()
    C-->>B: job payload
    B->>API: POST /save-job + Bearer token
    API->>API: current_user() resolves token
    API->>DB: add_job(db_path=workspace of that user)
    DB-->>API: job id or DuplicateJobError
    API-->>B: 201 or 409
```

### Inbox sync

```mermaid
sequenceDiagram
    participant S as Streamlit
    participant SC as sync_controller
    participant G as gmail_client
    participant EC as email_classifier
    participant DB as workspace db

    S->>SC: sync_inbox_to_db(user_id)
    SC->>G: fetch_job_emails(user_id)
    G->>G: bouncer filter
    G-->>SC: [{id, sender, subject, snippet}]
    SC->>DB: is_email_processed(id)?
    SC->>EC: classify_email(...)
    EC-->>SC: {category, company, reasoning}
    SC->>SC: to_status(category)
    SC->>DB: update_job_from_email(...)
    SC->>DB: mark_email_processed(id)
```

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

118 tests, no network calls. `tests/conftest.py` points `DATA_DIR` at a
temporary directory *before* importing config, since config resolves paths at
import time.

| File | Covers |
| ---- | ------ |
| `test_auth.py` | Hashing, salting, constant-time failure, token issue/verify/revoke/expiry, password rotation |
| `test_db.py` | Duplicate rules, status validation, dict rows, email dedupe, stats, schema versioning |
| `test_workspace.py` | Path traversal across six attack shapes, cross-user isolation, no-fallback profile loading |
| `test_email_pipeline.py` | Bouncer true/false positives, category mapping |
| `test_api.py` | 401 on every protected endpoint, cross-account isolation, duplicate 409, traversal rejection |

The Gemini and Gmail layers are intentionally untested — they are thin wrappers
over external APIs, and the logic worth testing (filtering, mapping, storage)
sits in pure functions around them.

## Security Posture

| Concern | Handling |
| ------- | -------- |
| Password storage | PBKDF2-HMAC-SHA256, 600k iterations, per-user salt |
| Account enumeration | Constant-time verification against a dummy hash |
| Token storage | SHA-256 digests; plaintext returned once |
| Session invalidation | Password change revokes all tokens; 30-day expiry |
| Cross-user access | Paths keyed by account id; no caller string in path construction |
| Path traversal | `sanitize_filename` + `resolve_within`, plus strict API rejection |
| CSRF from web pages | CORS restricted to `chrome-extension://` origins |
| Token theft from pages | Token confined to the service worker |
| XSS in the popup | `textContent` and node construction, never `innerHTML` |
| Extension over-reach | Narrow host permissions, per-origin opt-in for others |

Do not commit: `.env`, `credentials.json`, `token.json`, `data/`, `logs/`,
`extension/rules.js`.

**Deployment note:** the API listens on localhost over plain HTTP and assumes
single-machine use. Exposing it to a network requires TLS, and the account
model would want rate limiting on `/auth/login` before facing the internet.

## Known Limitations

- Extension extraction relies on DOM heuristics and can break when job boards
  change their markup.
- SQLite suits local single-user-per-account use; a hosted deployment would
  want a server-side database and connection pooling.
- The AI layer depends on Gemini availability and quota; errors degrade to
  handled messages rather than retries.
- There is no rate limiting on sign-in attempts.
- Password reset requires direct database access — there is no email flow.
- Gemini and Gmail wrappers are not covered by automated tests.
