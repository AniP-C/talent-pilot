# Operations runbook

Every command needed to look after the running deployment: reading logs,
rotating the invite code, inspecting the database, restarting services.

Adjacent documents: [PUSH_TO_GCLOUD.md](PUSH_TO_GCLOUD.md) is how new code gets
onto the server, [DEPLOYMENT.md](../DEPLOYMENT.md) is first-time setup, and
[README.md](../README.md) is the overview.

> **Almost everything here runs ON THE SERVER, not on your laptop.**
> There is no `/etc/talent-pilot/` on your machine. Connect first
> ([below](#connecting)), or wrap the command in
> `gcloud compute ssh … --command "…"`.

---

## Contents

- [Connecting](#connecting)
- [Where everything lives](#where-everything-lives)
- [Services](#services)
- [Logs](#logs)
- [The invite code and registration](#the-invite-code-and-registration)
- [Environment settings](#environment-settings)
- [Accounts](#accounts)
- [The database](#the-database)
- [Backups and restore](#backups-and-restore)
- [Caddy and routing](#caddy-and-routing)
- [TLS](#tls)
- [Machine health](#machine-health)
- [The extension package](#the-extension-package)
- [Data repair](#data-repair)
- [When something is broken](#when-something-is-broken)

---

## Connecting

```bash
gcloud compute ssh talent-pilot --zone us-west1-a
```

Run a single command without staying connected:

```bash
gcloud compute ssh talent-pilot --zone us-west1-a --command "systemctl is-active talent-pilot-api"
```

Confirm the instance is up and find its address:

```bash
gcloud compute instances list
```

---

## Where everything lives

| Path | Holds |
| --- | --- |
| `/opt/talent-pilot` | Application code and its virtualenv |
| `/opt/talent-pilot/.venv/bin/python` | The interpreter the services run |
| `/var/lib/talent-pilot` | **All user data.** Never overwrite |
| `/var/lib/talent-pilot/users.db` | Accounts, tokens, rate-limit state |
| `/var/lib/talent-pilot/workspaces/<id>/` | One user: jobs.db, resumes, answers, Gmail token |
| `/var/log/talent-pilot` | `app.log`, `errors.log`, `sync.log` |
| `/etc/talent-pilot/talent-pilot.env` | **Secrets.** Gemini key, invite code, PUBLIC_URL |
| `/etc/caddy/Caddyfile` | Reverse proxy and routing |
| `/var/backups/talent-pilot` | Nightly archives, 14-day retention |

The service account is `talentpilot`. Anything writing into those directories
should run as it (`sudo -u talentpilot …`), or ownership breaks.

---

## Services

Three units: the API on 8000, the dashboard on 8501, and Caddy in front.

```bash
systemctl is-active talent-pilot-api talent-pilot-dashboard caddy
```

```bash
sudo systemctl restart talent-pilot-api talent-pilot-dashboard
```

> `systemctl enable --now` is **not** a restart. It starts a stopped unit and
> does nothing at all to a running one, so using it after copying new code
> leaves both services serving the old version — with nothing in the logs to
> explain it. Use `restart`.

Both services run the same code. Restart **both**, always.

Why a unit will not start:

```bash
sudo journalctl -u talent-pilot-api -n 60 --no-pager
sudo journalctl -u talent-pilot-dashboard -n 60 --no-pager
```

---

## Logs

### Inbox sync decisions — start here for anything AI-related

Every automated status change, and every email that was skipped and why:

```bash
sudo tail -n 100 /var/log/talent-pilot/sync.log
```

Follow a sync as it happens:

```bash
sudo tail -f /var/log/talent-pilot/sync.log
```

Each line carries a run id (`[sync a3f9c1e2]`) so two overlapping syncs can be
told apart. Only the decisions:

```bash
sudo grep -E 'SKIP|UPDATED|CREATED|NOTED' /var/log/talent-pilot/sync.log | tail -40
```

### Everything else

```bash
sudo tail -n 100 /var/log/talent-pilot/app.log      # all activity
sudo tail -n 50  /var/log/talent-pilot/errors.log   # warnings and errors only
sudo journalctl -u caddy -n 50 --no-pager           # TLS, routing, HTTP errors
```

Trace one request end to end — every response carries an `X-Request-ID`:

```bash
sudo grep '<request-id>' /var/log/talent-pilot/app.log
```

### Without SSH

The dashboard's **📜 Activity** tab shows recent stage changes and the sync log,
with a decisions-only filter. That covers most questions.

---

## The invite code and registration

Registration is gated by `SIGNUP_CODE`. Without it, anyone who can reach the
server can sign up and spend your Gemini quota.

**Read it:**

```bash
sudo grep SIGNUP_CODE /etc/talent-pilot/talent-pilot.env
```

**Rotate it** — do this after sharing it with anyone, including a store
reviewer:

```bash
NEW=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
sudo sed -i "s|^SIGNUP_CODE=.*|SIGNUP_CODE=${NEW}|" /etc/talent-pilot/talent-pilot.env
sudo systemctl restart talent-pilot-api talent-pilot-dashboard
sudo grep SIGNUP_CODE /etc/talent-pilot/talent-pilot.env
```

**Close registration entirely** (single-user lockdown):

```bash
sudo sed -i 's|^REGISTRATION_CLOSED=.*|REGISTRATION_CLOSED=true|' /etc/talent-pilot/talent-pilot.env
sudo systemctl restart talent-pilot-api talent-pilot-dashboard
```

If the line is absent, append it instead:

```bash
echo 'REGISTRATION_CLOSED=true' | sudo tee -a /etc/talent-pilot/talent-pilot.env
```

**Check what the server currently reports** — no SSH needed:

```bash
curl -s https://katchjobs.online/health
```

`invite_required` and `open` in that response are the live truth.

---

## Environment settings

Everything configurable lives in one file:

```bash
sudo nano /etc/talent-pilot/talent-pilot.env
```

Nothing takes effect until the services restart:

```bash
sudo systemctl restart talent-pilot-api talent-pilot-dashboard
```

Worth knowing:

| Setting | Effect |
| --- | --- |
| `GEMINI_API_KEY` | All AI features. Rotate at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `SIGNUP_CODE` | Invite code for registration |
| `REGISTRATION_CLOSED` | `true` refuses all new accounts |
| `PUBLIC_URL` | Must match the real hostname or Gmail OAuth breaks |
| `GMAIL_LOOKBACK_DAYS` | How far back a sync reads (default 5) |
| `GMAIL_MAX_RESULTS` | Hard ceiling on emails classified per sync (default 25) |
| `GMAIL_THROTTLE_SECONDS` | Gap between AI calls; keeps you under the free-tier rate limit |
| `TRUST_PROXY_HEADERS` | `true` only because Caddy sits in front. Never on a directly-exposed server |
| `LOG_LEVEL` | `DEBUG` for per-request detail |

The file is `chmod 640`, owned `root:talentpilot`. Keep it that way.

---

## Accounts

List them:

```bash
sudo sqlite3 -header -column /var/lib/talent-pilot/users.db \
  'SELECT id, email, created_at FROM users ORDER BY id;'
```

Workspace ids on disk correspond to those account ids:

```bash
sudo ls /var/lib/talent-pilot/workspaces
```

**Delete an account and all its data.** Destructive and not reversible without
a backup — take one first:

```bash
sudo /usr/local/bin/talent-pilot-backup
sudo sqlite3 /var/lib/talent-pilot/users.db "DELETE FROM users WHERE email='someone@example.com';"
sudo rm -rf /var/lib/talent-pilot/workspaces/<id>
```

Sign every device out of an account by revoking its tokens:

```bash
sudo sqlite3 /var/lib/talent-pilot/users.db "DELETE FROM tokens WHERE user_id=<id>;"
```

Clear a lockout after too many failed sign-ins:

```bash
sudo sqlite3 /var/lib/talent-pilot/users.db "DELETE FROM login_attempts;"
```

---

## The database

One SQLite file per user. Install the client if it is missing:
`sudo apt-get install -y sqlite3`.

```bash
sudo sqlite3 -header -column /var/lib/talent-pilot/workspaces/1/jobs.db \
  'SELECT id, company, role, status, source FROM jobs ORDER BY id;'
```

Schema version — should be `2`:

```bash
sudo sqlite3 /var/lib/talent-pilot/workspaces/1/jobs.db 'PRAGMA user_version;'
```

How an application reached its current status:

```bash
sudo sqlite3 -header -column /var/lib/talent-pilot/workspaces/1/jobs.db \
  'SELECT occurred_at, from_status, to_status, applied, source, reason
   FROM status_history WHERE job_id=1 ORDER BY id;'
```

`applied = 0` means the observation was recorded but deliberately not applied,
because it would have moved the application backwards.

Migrations run automatically on first use. To force them for every workspace:

```bash
cd /opt/talent-pilot && sudo -u talentpilot \
  DATA_DIR=/var/lib/talent-pilot LOG_DIR=/var/log/talent-pilot \
  .venv/bin/python -c "
import db
from config import WORKSPACES_DIR
for ws in sorted(WORKSPACES_DIR.glob('*')):
    p = ws / 'jobs.db'
    if p.exists():
        db.create_table(p)
        print('ok', ws.name)
"
```

Let a sync reclassify mail it has already seen — useful after improving the
classifier:

```bash
sudo sqlite3 /var/lib/talent-pilot/workspaces/1/jobs.db 'DELETE FROM processed_emails;'
```

That makes the next sync pay for those emails again. Mind the Gemini quota.

---

## Backups and restore

Take one now:

```bash
sudo /usr/local/bin/talent-pilot-backup
sudo ls -lh /var/backups/talent-pilot | tail -5
```

Runs nightly by default, 14-day retention. Restore:

```bash
sudo systemctl stop talent-pilot-api talent-pilot-dashboard
sudo tar -xzf /var/backups/talent-pilot/talent-pilot-<STAMP>.tar.gz -C /
sudo chown -R talentpilot:talentpilot /var/lib/talent-pilot
sudo systemctl start talent-pilot-api talent-pilot-dashboard
```

An untested backup is not a backup. Restore one into a scratch directory and
open the databases occasionally.

---

## Caddy and routing

```bash
sudo nano /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

`validate` before `reload`, every time — a bad config takes the whole site down.

> **Adding an API endpoint means editing the `@api` matcher.** It is an
> explicit path allowlist. A path missing from it does not 404; it falls
> through to the dashboard, which answers **200 with an HTML page**. The
> extension then sees a success it cannot parse. This has already happened once
> with `/autofill`.

Check which service is answering a path:

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' https://katchjobs.online/health
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' https://katchjobs.online/autofill
```

`/health` should be JSON. An authenticated endpoint hit anonymously should be
`401` and JSON — `200 text/html` means it is reaching Streamlit instead.

---

## TLS

Caddy obtains and renews Let's Encrypt certificates automatically. Confirm:

```bash
curl -sI https://katchjobs.online | head -3
echo | openssl s_client -connect katchjobs.online:443 2>/dev/null | openssl x509 -noout -dates
```

Renewal problems appear in Caddy's log:

```bash
sudo journalctl -u caddy --since "24 hours ago" | grep -i "certificate\|acme\|error"
```

---

## Machine health

An `e2-micro` has 1 GB of RAM and runs three services. Memory is the tightest
resource and the usual cause of a mysterious restart.

```bash
free -h
df -h /
uptime
```

Was something killed for memory?

```bash
sudo dmesg -T | grep -i "killed process" | tail -5
```

What is using the memory:

```bash
ps aux --sort=-%mem | head -8
```

---

## The extension package

Run on your laptop, in the project directory:

```bash
python deploy/build_extension.py
```

Writes `dist/talent-pilot-extension-<version>.zip`, refusing to build if the
manifest references a missing file, icons are absent, or any file contains
personal data.

Bump `version` in `extension/manifest.json` before each store upload — the
Chrome Web Store rejects a re-upload of a version it already has.

---

## Data repair

Find and fix applications whose company holds a job title:

```bash
cd /opt/talent-pilot
sudo -u talentpilot DATA_DIR=/var/lib/talent-pilot LOG_DIR=/var/log/talent-pilot \
  .venv/bin/python deploy/fix_bad_companies.py
```

Dry run by default. To apply, having backed up first:

```bash
sudo /usr/local/bin/talent-pilot-backup
cd /opt/talent-pilot
sudo -u talentpilot DATA_DIR=/var/lib/talent-pilot LOG_DIR=/var/log/talent-pilot \
  .venv/bin/python deploy/fix_bad_companies.py --apply --set 3="Real Company Name"
```

Rows created from email have no posting URL, so the company cannot be derived
and must be supplied with `--set`.

---

## When something is broken

**Site unreachable.** Check the proxy before the app:

```bash
systemctl is-active caddy talent-pilot-api talent-pilot-dashboard
sudo journalctl -u caddy -n 30 --no-pager
```

**An endpoint returns HTML instead of JSON.** It is missing from the Caddy
`@api` matcher. See [Caddy and routing](#caddy-and-routing).

**"was written by a newer version of this app".** Old code is running against a
migrated database — the deploy did not land, or only one service restarted.

**AI features failing.** Almost always quota or the key:

```bash
sudo grep -i "RATE_LIMIT\|AUTH_ERROR\|quota" /var/log/talent-pilot/app.log | tail -10
```

**Gmail sync doing nothing.** The sync log says why, per email:

```bash
sudo tail -n 60 /var/log/talent-pilot/sync.log
```

`PUBLIC_URL` must exactly match the hostname registered in the Google Cloud
console, or consent fails with a redirect-URI mismatch.

**Permission denied writing data.** Ownership was lost during a copy:

```bash
sudo chown -R talentpilot:talentpilot /opt/talent-pilot /var/lib/talent-pilot /var/log/talent-pilot
```

**Everything looks fine but behaviour is old.** You ran `enable --now` instead
of `restart`. See [Services](#services).
