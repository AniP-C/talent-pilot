# Pushing an update to Google Cloud

How to get code from your laptop onto the running GCE instance and confirm it
took effect.

[DEPLOYMENT.md](../DEPLOYMENT.md) covers **first-time** setup — creating the
instance, DNS, TLS, OAuth. This document covers every deploy after that one.

> **This release changes the database schema (v1 → v2).** Do not skip
> [step 2](#2-back-up-first-not-optional-this-time). The migration runs
> automatically and is not reversible without a backup.

---

## Contents

- [What you are deploying](#what-you-are-deploying)
- [0. Preflight](#0-preflight)
- [1. Connect](#1-connect)
- [2. Back up first](#2-back-up-first-not-optional-this-time)
- [3. Copy the code up](#3-copy-the-code-up)
- [4. Update dependencies](#4-update-dependencies)
- [5. Restart both services](#5-restart-both-services)
- [6. Verify](#6-verify)
- [7. Repair rows saved by the old scraper](#7-repair-rows-saved-by-the-old-scraper)
- [8. Reload the browser extension](#8-reload-the-browser-extension)
- [Rollback](#rollback)
- [Troubleshooting](#troubleshooting)
- [The whole thing, as one script](#the-whole-thing-as-one-script)

---

## What you are deploying

| Path on the VM | Holds | Touched by a deploy? |
| --- | --- | --- |
| `/opt/talent-pilot` | Application code and the virtualenv | **Yes — replaced** |
| `/var/lib/talent-pilot` | Accounts, per-user job databases, resumes, Gmail tokens | No — never overwrite |
| `/var/log/talent-pilot` | `app.log`, `errors.log`, `sync.log` | No |
| `/etc/talent-pilot/talent-pilot.env` | Secrets: Gemini key, signup code, `PUBLIC_URL` | No — edit by hand only |

Two systemd units serve it: `talent-pilot-api` (port 8000) and
`talent-pilot-dashboard` (port 8501), with Caddy in front on one hostname.
**Both run the same code**, so both must be restarted.

---

## 0. Preflight

Run the tests locally. A deploy is not the place to discover a broken import.

```bash
python -m pytest -q
```

Confirm you know the instance and zone:

```bash
gcloud compute instances list
```

---

## 1. Connect

```bash
gcloud compute ssh talent-pilot --zone us-west1-b
```

Replace the name and zone with whatever the previous command printed. Every
step below runs on the VM unless it says otherwise.

---

## 2. Back up first (not optional this time)

The schema migration adds a `status_history` table and backfills a row per
existing application. It is safe and idempotent, but it is still a one-way
change: a v2 database is refused by older code, on purpose.

```bash
sudo /usr/local/bin/talent-pilot-backup
```

Confirm the archive exists and is not empty:

```bash
ls -lh /var/backups/talent-pilot | tail -3
```

---

## 3. Copy the code up

Two options. Use whichever matches how you got the code onto the VM originally.

### Option A — from git (preferred)

```bash
cd /opt/talent-pilot && sudo -u talentpilot git pull --ff-only
```

### Option B — from your laptop, when the VM has no git remote

Run this **on your laptop**, not on the VM. It stages the code in your home
directory first, because `/opt/talent-pilot` is not writable by your SSH user:

```bash
gcloud compute scp --recurse --zone us-west1-b ./ talent-pilot:~/talent-pilot-staging --compress
```

Then, back on the VM, sync it into place. The excludes are what protect your
data, logs, and virtualenv from being clobbered:

```bash
sudo rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'data' --exclude 'logs' --exclude '__pycache__' --exclude '.env' ~/talent-pilot-staging/ /opt/talent-pilot/
```

Restore ownership — rsync copied the files as your user:

```bash
sudo chown -R talentpilot:talentpilot /opt/talent-pilot
```

---

## 4. Update dependencies

Only needed when `requirements.txt` changed. It is cheap and harmless to run
either way:

```bash
sudo -u talentpilot /opt/talent-pilot/.venv/bin/pip install -q -r /opt/talent-pilot/requirements.txt
```

---

## 5. Restart both services

```bash
sudo systemctl restart talent-pilot-api talent-pilot-dashboard
```

> **`enable --now` is not `restart`.** `systemctl enable --now` starts a unit
> that is stopped and does *nothing at all* to one that is already running.
> Using it after a code copy leaves both services happily serving the old
> version, which shows up as a new endpoint returning 404 with nothing in the
> logs to explain it. Use `restart`.

Confirm both came back:

```bash
systemctl is-active talent-pilot-api talent-pilot-dashboard
```

Two lines reading `active`. Anything else, go to
[Troubleshooting](#troubleshooting).

---

## 6. Verify

### The API is serving the new code

```bash
curl -s https://katchjobs.online/health
```

Expect `"service":"talent-pilot-api"` and the current `version`.

### The schema migrated

```bash
sudo -u talentpilot sqlite3 /var/lib/talent-pilot/workspaces/1/jobs.db 'PRAGMA user_version;'
```

Expect `2`. If `sqlite3` is not installed, `sudo apt-get install -y sqlite3`.

Confirm the backfill produced history rows:

```bash
sudo -u talentpilot sqlite3 /var/lib/talent-pilot/workspaces/1/jobs.db 'SELECT COUNT(*) FROM status_history;'
```

Should be at least one row per tracked application.

### Nothing is erroring

```bash
sudo tail -n 40 /var/log/talent-pilot/errors.log
```

### The dashboard renders

Open `https://katchjobs.online`, sign in, and check that the **📜 Activity**
tab appears alongside Dashboard, Add application, Analyzer, Profiles, and
Settings. That tab is new in this release, so its presence is itself proof the
dashboard is running the new code.

---

## 7. Repair rows saved by the old scraper

The new code stops bad rows being created; it does not rewrite the ones already
stored. Any application saved with a company of "AI Engineer" stays broken, and
keeps opening duplicates every time a recruiter emails about it.

See what needs fixing — this writes nothing:

```bash
cd /opt/talent-pilot && sudo -u talentpilot .venv/bin/python deploy/fix_bad_companies.py
```

It reports each broken row, the employer it derived from the saved posting URL,
and any row it cannot resolve. Rows from LinkedIn and Indeed usually need you to
supply the company, because those URLs genuinely do not contain it.

You took a backup in [step 2](#2-back-up-first-not-optional-this-time), so apply:

```bash
sudo -u talentpilot .venv/bin/python deploy/fix_bad_companies.py --apply
```

Add any companies it could not work out, and re-run:

```bash
sudo -u talentpilot .venv/bin/python deploy/fix_bad_companies.py --apply --set 12="Nexus Labs" --set 15="Zerodha"
```

Where repairing a company collides with a duplicate the bug had already created,
the two rows are merged: the further-along status wins, and notes and stage
history from both are kept. Every repair is recorded in the timeline with source
`Cleanup`, so nothing changes silently. Running it twice is a no-op.

---

## 8. Reload the browser extension

`extension/content.js` changed in this release, and Chrome will not pick that
up on its own. On your laptop:

1. Open `chrome://extensions`
2. Find **AI Job Copilot**
3. Click the reload icon
4. Open a job posting and confirm the popup shows the **company**, not the job
   title

If a page genuinely does not name the employer, the popup now asks you to type
it rather than guessing — that is the intended behaviour, not a failure.

---

## Rollback

### Code only

```bash
cd /opt/talent-pilot && sudo -u talentpilot git reset --hard <previous-commit> && sudo systemctl restart talent-pilot-api talent-pilot-dashboard
```

### Code and data

A v2 database is deliberately refused by v1 code, so rolling the code back
means rolling the data back with it:

```bash
sudo systemctl stop talent-pilot-api talent-pilot-dashboard
sudo tar -xzf /var/backups/talent-pilot/talent-pilot-<STAMP>.tar.gz -C /
sudo chown -R talentpilot:talentpilot /var/lib/talent-pilot
sudo systemctl start talent-pilot-api talent-pilot-dashboard
```

---

## Troubleshooting

**A service will not start.** The unit's own log says why:

```bash
sudo journalctl -u talent-pilot-api -n 60 --no-pager
```

**"was written by a newer version of this app".** Old code is running against
a v2 database — the code copy did not land, or only one service restarted.
Redo [step 3](#3-copy-the-code-up) and [step 5](#5-restart-both-services).

**Permission denied writing to the data directory.** Ownership was lost during
the copy:

```bash
sudo chown -R talentpilot:talentpilot /opt/talent-pilot /var/lib/talent-pilot /var/log/talent-pilot
```

**The dashboard is up but the extension gets CORS errors.** `ALLOWED_ORIGIN_REGEX`
in `/etc/talent-pilot/talent-pilot.env` must match your installed extension id.
Editing that file requires a restart to take effect.

**Inbox sync behaves oddly.** Every decision it made is in its own log:

```bash
sudo tail -n 100 /var/log/talent-pilot/sync.log
```

Each line carries a run id, so one sync can be followed even if two overlapped.
The same content is visible in the dashboard's **Activity** tab, without SSH.

---

## The whole thing, as one script

Run **on the VM** after the code is in place, when you already know the deploy
is routine:

```bash
set -e
sudo /usr/local/bin/talent-pilot-backup
cd /opt/talent-pilot && sudo -u talentpilot git pull --ff-only
sudo -u talentpilot /opt/talent-pilot/.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart talent-pilot-api talent-pilot-dashboard
sleep 3
systemctl is-active talent-pilot-api talent-pilot-dashboard
curl -s https://katchjobs.online/health
```
