# Who is using Talent Pilot, and how much

Every command here is **read-only**. Nothing on this page changes or deletes
anything; the destructive account operations live in
[OPERATIONS.md](OPERATIONS.md) and are deliberately kept apart from the ones
you will run most often.

Two databases hold the answers:

| Question | Where it lives |
| -------- | -------------- |
| Who has an account, and what they have used | `/var/lib/talent-pilot/users.db` (central) |
| What any one person is *tracking* | `/var/lib/talent-pilot/workspaces/<id>/jobs.db` (per user) |

Usage is recorded in the central database on purpose: "which accounts are worth
charging" is a question about all of them at once, and a per-workspace table
would mean opening every user's database to answer it.

---

## 1. How many people are using it

Signups and actives are different numbers, and only the second one matters for
pricing. `users` counts everyone who ever registered; the usage report counts
who came back.

```bash
gcloud compute ssh talent-pilot --zone us-west1-a --command "sudo sqlite3 /var/lib/talent-pilot/users.db 'SELECT COUNT(*) FROM users;'"
```

Signed in at least once in the last 30 days:

```bash
gcloud compute ssh talent-pilot --zone us-west1-a --command "sudo sqlite3 /var/lib/talent-pilot/users.db \"SELECT COUNT(DISTINCT user_id) FROM usage_events WHERE occurred_at >= datetime('now','-30 days');\""
```

---

## 2. Who they are

Account ids, email addresses, when they joined, and when they were last seen.
The workspace directory on disk is named after the account id in the first
column, so this is also how you find someone's data.

```bash
gcloud compute ssh talent-pilot --zone us-west1-a --command "sudo sqlite3 -header -column /var/lib/talent-pilot/users.db 'SELECT id, email, created_at, last_login_at FROM users ORDER BY id;'"
```

Accounts that registered and never came back — the ones to email, not to
charge:

```bash
gcloud compute ssh talent-pilot --zone us-west1-a --command "sudo sqlite3 -header -column /var/lib/talent-pilot/users.db 'SELECT id, email, created_at FROM users WHERE last_login_at IS NULL ORDER BY id;'"
```

---

## 3. What each account has actually used

This is the report to run before setting a price. One row per account, ordered
by what they cost.

```bash
gcloud compute ssh talent-pilot --zone us-west1-a --command "cd /opt/talent-pilot && sudo -u talentpilot DATA_DIR=/var/lib/talent-pilot LOG_DIR=/var/log/talent-pilot .venv/bin/python deploy/usage_report.py"
```

```text
#  Email                 Paid units  CV uploads  Analyses  Answers  Emails synced  Keyword scans  Jobs saved  Sign-ins  Last seen
-  --------------------  ----------  ----------  --------  -------  -------------  -------------  ----------  --------  ----------------
2  heavy@example.com     216         3           48        22       143            310            57          61        2026-08-19T09:12
1  light@example.com     4           1           3         0        0              12             4           6         2026-08-14T21:40
```

Useful variants:

```bash
# Just the last 30 days — what a monthly price would have to cover
... deploy/usage_report.py --days 30
```

```bash
# CSV, to open in a spreadsheet
... deploy/usage_report.py --csv > usage.csv
```

```bash
# The last 40 individual actions, to see what someone is doing right now
... deploy/usage_report.py --recent 40
```

To pull the CSV back to your own machine:

```bash
gcloud compute scp talent-pilot:/tmp/usage.csv . --zone us-west1-a
```

---

## 4. What the columns mean, and which ones cost money

Only four actions make a model call. Those are the ones a price has to cover;
the rest are worth counting as engagement, because they are what makes somebody
keep the extension installed.

| Column | Event | Cost | What it is |
| ------ | ----- | ---- | ---------- |
| CV uploads | `resume.upload` | **paid** — one large call | A PDF parsed into a profile. Whole document in the prompt, so it is the most expensive single action. |
| Analyses | `jd.analyze` | **paid** — one large call | Full requirement-by-requirement scoring of a job description. |
| Answers | `answer.draft` | **paid** — one call | One drafted application answer. |
| Emails synced | `email.sync` | **paid** — one call *per email* | Counted in emails, not in runs. A single sync can be forty calls, which is why it is usually the largest number on the row. |
| Keyword scans | `keyword.scan` | free | Deterministic string matching. Runs on every job page the extension opens. |
| Jobs saved | `job.save` | free | An application added from the extension. |
| Sign-ins | `auth.signin` | free | How "active" is measured. |
| Paid units | — | — | The four paid events added together. **This is the number a price has to beat.** |

### Turning that into a price

Look up the current per-1M-token rate for whichever `GEMINI_MODEL` is set (see
`config.py`; today it is `gemini-2.5-flash-lite`) and multiply it out. Rough
prompt sizes, from the code:

| Event | Prompt | Notes |
| ----- | ------ | ----- |
| `resume.upload` | up to 20k characters | `convert_pdf_to_json` |
| `jd.analyze` | up to 16k characters | 8k JD + 8k resume |
| `answer.draft` | up to 15k characters | resume + JD + saved answers |
| `email.sync` | up to ~4k characters per email | plus the body |

Two things the raw numbers will not tell you:

- **The distribution matters more than the total.** If one account is 80% of
  the spend, a flat monthly price is a bet on that account leaving. Check the
  top row against the sum before choosing between flat-rate and metered.
- **Keyword scans are the engagement number.** They cost nothing and they track
  how often someone opens a job posting with the extension running. An account
  with many scans and few analyses is getting value from the free path — that
  is the audience for a paid tier, not a lost cause.

---

## 5. Reading the same thing locally

Everything above works against a local development database by dropping the
SSH wrapper and the `DATA_DIR`:

```bash
python deploy/usage_report.py --recent 20
```

```bash
sqlite3 -header -column data/users.db 'SELECT id, email, created_at, last_login_at FROM users ORDER BY id;'
```

---

## 6. What is deliberately not recorded

A usage event is an account id, a verb, a count and a timestamp. No job
descriptions, no resume content, no company names, no email subjects. That
keeps this table something you can query freely for billing without it becoming
a second, cross-account copy of everybody's private data.

If you later need "which companies is this person applying to", that already
lives in their own workspace database and should stay there.
