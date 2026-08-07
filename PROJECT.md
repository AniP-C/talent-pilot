# Talent Pilot — What It Is and How It Works

A plain explanation of the project: the problem it solves, how it is put
together, the decisions that shaped it, and what broke along the way.

The other documents cover different ground: [README.md](README.md) is the
overview and setup guide, [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md) is
the internals reference, and [DEPLOYMENT.md](DEPLOYMENT.md) is for hosting it.
This one is the "why".

**Live at [katchjobs.online](https://katchjobs.online).**

---

## The problem

Applying for jobs generates a surprising amount of clerical work. Details
live in browser tabs, resume versions live in folders, application status
lives in your head, and updates arrive in an inbox alongside everything else.
The actual thinking — is this role a fit, what should I say — gets crowded out
by bookkeeping.

Talent Pilot collapses that into one loop:

**Detect → Analyse → Save → Track → Sync**

You are on a job page. The extension reads it, scores it against your resume,
and saves it with one click. Recruiter emails arriving later update the status
automatically. The dashboard is the single place that knows where everything
stands.

---

## What it actually does

**Reads job pages.** A browser extension recognises listings on LinkedIn,
Greenhouse, Lever, Wellfound, Ashby, and Workable, pulling out company, role,
and the job description. Any other site can be enabled per-origin on demand.

**Scores the fit.** Gemini compares the description against a structured
version of your resume and returns a match percentage, matched skills, missing
skills, and an honest summary of the gap.

**Parses resumes.** Upload a PDF and it becomes a structured profile. Keep
several — one per target role — and switch between them.

**Drafts answers.** For long-form application questions, it writes a draft
grounded in your resume and your own previously saved answers, so it sounds
like you rather than like a language model.

**Watches your inbox.** With Gmail connected, recruiter mail is classified
into application states and the matching record updates itself. A cheap rule
filter runs first so newsletters never reach a paid AI call.

**Tracks everything.** A dashboard with search, status filters, and summary
metrics.

---

## How it is built

Two processes sharing one filesystem:

```
        Browser extension                    You, in a browser
                │                                    │
        background.js (holds the token)              │
                │                                    │
                ▼                                    ▼
        FastAPI  :8000  ◄──── same disk ────►  Streamlit  :8501
                            │
                    data/workspaces/<user_id>/
                      jobs.db, profiles, answers, gmail token
```

They must share storage — the dashboard writes profiles the API reads. In
production, Caddy sits in front of both on one hostname, routing API paths to
8000 and everything else to the dashboard, with automatic HTTPS.

| Piece | Choice | Why |
| ----- | ------ | --- |
| Dashboard | Streamlit | Fast to build a data-heavy UI; no frontend build step |
| API | FastAPI | The extension needs a real HTTP API with typed validation |
| Storage | SQLite, one file per user | No server to run; isolation falls out of the design |
| AI | Gemini Flash Lite | Structured output via schemas; generous free tier |
| Hosting | GCE free tier + systemd + Caddy | Genuinely free, no containers to debug |

---

## Decisions worth explaining

### Workspaces are keyed by account id, not email

Every user's data lives in `data/workspaces/<user_id>/`. The earlier design
derived paths from the email address, which meant a caller-supplied string was
part of a filesystem path — the shape of problem that turns into a directory
traversal.

Using the numeric account id means **no input controls where files land**.
Cross-user access is not blocked by a check that could be forgotten; it is
unreachable by construction. Caller-supplied filenames, like profile names,
still pass through sanitisation and a containment check, but that is defence
in depth rather than the primary barrier.

### Identity comes from the token, never the request body

No endpoint accepts a `user_email` field. The API resolves who you are from
the bearer token and nothing else. An earlier version took the email from the
request, which meant anyone could read any workspace by typing a different
address.

### Duplicates are the database's job

"Already tracked" is a `UNIQUE` index on `(LOWER(company), LOWER(role))`, not a
SELECT-then-INSERT. The check-then-act version had a race, and worse, two
different definitions of "duplicate" in different code paths — one included
the application date, one did not, so the UI would say a job was already saved
and then save it again.

### The extension's token never touches page context

All network calls go through the background service worker. Two benefits: the
token lives somewhere a hostile page cannot reach, and requests originate from
the extension's own origin, which is the only origin the API's CORS policy
accepts. A job posting is untrusted input, so every scraped value is rendered
with `textContent` rather than `innerHTML`.

### Cheap filters before expensive calls

Inbox sync runs a keyword rule engine before any AI call, and records which
message ids it has already classified. A repeat sync costs nothing. The filter
matches phrases rather than bare words, because "offer" alone also matches
"limited time offer" — which is exactly the marketing mail it exists to
exclude.

---

## What broke, and what it taught

The interesting part of the project was rarely the feature; it was the failure
mode discovered on the way.

**A server cannot open a browser.** Gmail authorisation originally used
Google's desktop flow, which spins up a local web server and opens a browser —
on the machine running the code. Fine on a laptop, meaningless on a VM, where
it would simply hang. Hosting forced a rewrite to the redirect flow.

**A redirect is a new session.** Having rewritten it, consent still failed
every time with a state mismatch. The OAuth `state` was held in Streamlit's
session, but Google returns the user by redirecting the browser — a fresh page
load, therefore a new session. The value was *always* gone by the time the
callback ran. Not a race; a guaranteed failure. State now lives on disk.

**PKCE has two halves.** With state fixed, the callback got further and then
failed at the token exchange: `Missing code verifier`. Building the consent URL
generates a random verifier and sends only its hash to Google, which demands
the original back at exchange time — and the exchange was constructing a fresh
client object with no memory of it. Same lesson as the state, one layer deeper.

**Sandboxes block what you assume they allow.** Keeping the dashboard signed in
across refreshes needed a cookie, and Streamlit cannot set cookies server-side.
The obvious fix — redirect to an endpoint that can — is blocked: Streamlit
renders components in an iframe without `allow-top-navigation`. It *does* allow
same-origin access, so the component writes the cookie directly instead. The
trade-off is that a JavaScript-written cookie cannot be `HttpOnly`; it holds a
revocable token rather than credentials.

**`enable --now` is not `restart`.** A redeploy copied new code into place and
left both services running the old version, so a new endpoint returned 404 with
nothing in the logs to explain it. `systemctl enable --now` starts a unit but
does nothing when it is already running.

**An untested backup is not a backup.** The backup script is only trustworthy
because a restore was actually performed and the databases checked afterwards.

---

## Where it stands

| | |
| --- | --- |
| Tests | 159, no network calls |
| API endpoints | 20 |
| Hosting | GCE `e2-micro`, us-west1, free tier |
| TLS | Let's Encrypt via Caddy, auto-renewing |
| Backups | Nightly, 14-day retention, restore-verified |

Tests cover password hashing and token lifecycle, storage rules, path
traversal across several attack shapes, upload validation, the email filter,
and the API's authentication and isolation guarantees. The Gemini and Gmail
wrappers are deliberately untested — they are thin shims over external
services, and the logic worth testing sits in pure functions around them.

---

## Security posture

- Passwords: PBKDF2-HMAC-SHA256, 600k iterations, per-user salt
- Sign-in failures take constant time, so accounts cannot be enumerated
- API tokens stored as SHA-256 digests — a database copy grants no sessions
- Rate limiting per email and per IP, surviving restarts
- Registration gated behind an invite code
- CORS restricted to extension origins, so no web page can call the API
- Gmail access is `gmail.readonly`; the app can read mail and nothing else

Honest gaps: there is no password reset without database access, no email
verification, and no audit log beyond the application log.

---

## Limitations

**Extraction is heuristic.** Job boards change their markup and selectors
break. The failure is visible rather than silent — the extension reports no
job detected — but it needs occasional maintenance.

**SQLite is the ceiling.** Fine for a handful of users on one box. A real
multi-tenant deployment would want a server-side database.

**It depends on Gemini.** Quota exhaustion or an outage degrades to handled
error messages rather than retries.

**Latency.** The free tier only covers US regions, so from India there is
around 250 ms of round-trip on every page load.

---

## What I would do next

- Password reset by email
- A real job-board adapter layer, so a broken selector is a config change
- Analytics over time: response rates by source, time-to-first-response
- Postgres, if it ever needs to serve more than a few people
