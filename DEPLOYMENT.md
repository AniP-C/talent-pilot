# Deploying Talent Pilot

Running Talent Pilot on a server you control — no Docker required.

> **Verification status.** The application was exercised end-to-end in hosted
> configuration on the author's machine: invite-code enforcement, per-IP rate
> limiting behind a proxy header, CORS restriction, and both services under
> the exact commands the systemd units run. The **server bootstrap itself has
> not been run on a live VM** — the shell script and unit files are
> syntax-checked but untested against a real Oracle instance. Expect to fix
> one or two small things the first time.

---

## Contents

- [What you are deploying](#what-you-are-deploying)
- [Get a free domain first](#get-a-free-domain-first)
- [Oracle Cloud Always Free](#oracle-cloud-always-free)
- [The bootstrap script](#the-bootstrap-script)
- [Manual setup](#manual-setup)
- [Google OAuth for Gmail sync](#google-oauth-for-gmail-sync)
- [Pointing the extension at your server](#pointing-the-extension-at-your-server)
- [Operating it](#operating-it)
- [Backups](#backups)
- [Security checklist](#security-checklist)
- [Troubleshooting](#troubleshooting)
- [Appendix: Docker](#appendix-docker)

---

## What you are deploying

Two processes that share one filesystem:

| Process | Bound to | Serves |
| ------- | -------- | ------ |
| FastAPI backend | `127.0.0.1:8000` | The Chrome extension |
| Streamlit dashboard | `127.0.0.1:8501` | The web UI |
| Caddy | `:80` / `:443` | TLS, and routing between the two |

Both bind to localhost only. Caddy is the single public entrance, which means
one hostname serves both: API paths go to 8000, everything else falls through
to the dashboard.

They **must** share storage — the dashboard writes resume profiles the API
reads. Splitting them across machines without shared storage will not work.

All state lives under `DATA_DIR` (`/var/lib/talent-pilot`):

```text
/var/lib/talent-pilot/
├── users.db              accounts, tokens, failed sign-in records
└── workspaces/<user_id>/
    ├── jobs.db
    ├── profiles/*.json
    ├── answers/*.txt
    └── gmail_token.json
```

---

## Get a free domain first

You said you don't have one. **Get one anyway — it takes two minutes and
without it Gmail sync cannot work at all.** Google will not accept a bare IP
address as an OAuth redirect URI, and Let's Encrypt will not issue a
certificate for an IP, so there is no HTTPS either.

[DuckDNS](https://www.duckdns.org) is free, permanent, and needs no payment
details:

1. Sign in with GitHub or Google.
2. Pick a subdomain, e.g. `talentpilot` → `talentpilot.duckdns.org`.
3. Paste your server's public IP into the box and press **update ip**.

That is the whole process. Use `talentpilot.duckdns.org` wherever this guide
says `YOUR_DOMAIN`.

> Deploying without a domain still works for the dashboard and the extension —
> pass `--no-domain` to the setup script. You lose HTTPS and Gmail sync, and
> your password crosses the network in the clear. Fine for a private trial on
> a IP nobody knows; not fine for real use.

---

## Oracle Cloud Always Free

Free indefinitely, with real disk. Two firewall layers trip most people up.

### 1. Create the instance

**Compute → Instances → Create instance**

- **Shape:** `VM.Standard.A1.Flex` (Ampere ARM) — 1 OCPU / 6 GB is plenty
- **Image:** Canonical Ubuntu 22.04
- **SSH keys:** save the private key it offers; you cannot retrieve it later

ARM capacity is often exhausted in popular regions. If creation fails with
"Out of host capacity", either retry later or use the always-free AMD shape
(`VM.Standard.E2.1.Micro`, 1 GB RAM — tight but workable).

### 2. Open the ports — both layers

**Layer 1, the cloud firewall.** Networking → Virtual Cloud Networks → your
VCN → Security Lists → Default Security List → **Add Ingress Rules**:

| Source CIDR | Protocol | Destination port |
| ----------- | -------- | ---------------- |
| `0.0.0.0/0` | TCP | 80 |
| `0.0.0.0/0` | TCP | 443 |

**Layer 2, the instance firewall.** Oracle's Ubuntu images also ship
restrictive local iptables rules. The setup script handles this, but manually:

```bash
sudo iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Forgetting layer 2 is the usual reason a correctly-configured VM appears
completely unreachable.

### 3. Point your domain at it

Copy the instance's public IP into DuckDNS and press **update ip**. Confirm:

```bash
dig +short talentpilot.duckdns.org
```

---

## The bootstrap script

SSH in, then:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/AniP-C/talent-pilot.git
cd talent-pilot
sudo ./deploy/setup.sh talentpilot.duckdns.org
```

Or without a domain:

```bash
sudo ./deploy/setup.sh --no-domain
```

The script installs Python and Caddy, creates a `talentpilot` service account,
installs the app to `/opt/talent-pilot` in a virtualenv, writes
`/etc/talent-pilot/talent-pilot.env` with a **randomly generated invite code**,
registers both systemd services, configures Caddy, fixes the iptables rules,
and health-checks the API.

Re-running it updates the code and restarts the services. It will not
overwrite an existing env file.

### Then add your Gemini key

```bash
sudo nano /etc/talent-pilot/talent-pilot.env
```

Replace `PUT_YOUR_KEY_HERE`, then:

```bash
sudo systemctl restart talent-pilot-api talent-pilot-dashboard
```

Your invite code:

```bash
sudo grep SIGNUP_CODE /etc/talent-pilot/talent-pilot.env
```

Open `https://talentpilot.duckdns.org`, register with that code, and you are in.

---

## Manual setup

If you would rather not run a script, this is everything it does.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git rsync sqlite3
sudo useradd --system --home /opt/talent-pilot --shell /usr/sbin/nologin talentpilot
sudo mkdir -p /opt/talent-pilot /var/lib/talent-pilot /var/log/talent-pilot /etc/talent-pilot
```

```bash
sudo git clone https://github.com/AniP-C/talent-pilot.git /opt/talent-pilot
cd /opt/talent-pilot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
```

Create `/etc/talent-pilot/talent-pilot.env`:

```bash
GEMINI_API_KEY=your_real_key
PUBLIC_URL=https://talentpilot.duckdns.org
SIGNUP_CODE=something-long-and-random
TRUST_PROXY_HEADERS=true
DATA_DIR=/var/lib/talent-pilot
LOG_DIR=/var/log/talent-pilot
```

```bash
sudo chmod 640 /etc/talent-pilot/talent-pilot.env
sudo chown root:talentpilot /etc/talent-pilot/talent-pilot.env
sudo chown -R talentpilot:talentpilot /opt/talent-pilot /var/lib/talent-pilot /var/log/talent-pilot
```

```bash
sudo cp deploy/talent-pilot-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now talent-pilot-api talent-pilot-dashboard
```

Install Caddy, copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile` replacing
`YOUR_DOMAIN`, then `sudo systemctl restart caddy`.

### The two commands, if you skip systemd entirely

```bash
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000 --proxy-headers
```

```bash
python -m streamlit run app.py --server.port 8501 --server.address 127.0.0.1 --server.headless true
```

*These two are the commands verified in hosted configuration.* Everything else
here is wrapping around them.

---

## Google OAuth for Gmail sync

The desktop OAuth flow **cannot work on a server** — it opens a browser on the
machine running the code. Setting `PUBLIC_URL` switches the app to the
redirect flow automatically.

In the [Google Cloud console](https://console.cloud.google.com/):

1. **APIs & Services → Library** → enable **Gmail API**.
2. **OAuth consent screen** → External. Add your address under **Test users**.
   An unverified app allows 100 test users, which is ample.
3. **Credentials → Create credentials → OAuth client ID → Web application**.
4. Under **Authorised redirect URIs** add your `PUBLIC_URL` **exactly**,
   trailing slash included:

   ```text
   https://talentpilot.duckdns.org/
   ```

5. Download the JSON, then put it on the server:

   ```bash
   sudo cp credentials.json /opt/talent-pilot/credentials.json
   sudo chown talentpilot:talentpilot /opt/talent-pilot/credentials.json
   sudo chmod 600 /opt/talent-pilot/credentials.json
   sudo systemctl restart talent-pilot-api talent-pilot-dashboard
   ```

Users click **Connect Gmail** in the sidebar, approve at Google, and land back
on the dashboard connected. The callback is checked against a `state` value
held in the session, so one user's redirect cannot attach a mailbox to another
account. Scope is `gmail.readonly` — read access, nothing else.

---

## Pointing the extension at your server

The extension defaults to `http://localhost:8000`.

1. Open the extension popup.
2. Expand **⚙️ Settings**.
3. Enter `https://talentpilot.duckdns.org` and **Save**.
4. Chrome asks permission for that origin — accept.
5. Sign in with your account and invite code.

Changing the address clears the stored token, since a token from one server is
meaningless to another.

**CORS:** `ALLOWED_ORIGIN_REGEX` accepts `chrome-extension://` origins by
default — exactly what the extension needs, and it excludes ordinary web
pages. You do not need to change it. Never widen it to `*`; that would let any
site you visit call your API.

---

## Operating it

```bash
sudo systemctl status talent-pilot-api talent-pilot-dashboard
sudo systemctl restart talent-pilot-api
sudo journalctl -u talent-pilot-api -f
```

Application logs also go to files:

| File | Contents |
| ---- | -------- |
| `/var/log/talent-pilot/app.log` | Every request with a correlation id, auth events, sync progress |
| `/var/log/talent-pilot/errors.log` | Warnings and errors only |

Updating to a newer version:

```bash
cd ~/talent-pilot && git pull
sudo ./deploy/setup.sh talentpilot.duckdns.org
```

---

## Backups

Everything is under `/var/lib/talent-pilot`. SQLite needs a consistent copy,
so use its own backup command rather than `cp` on a live database:

```bash
sudo -u talentpilot sqlite3 /var/lib/talent-pilot/users.db \
    ".backup /tmp/users-$(date +%F).db"
```

Whole-directory backup, stopping the services for consistency:

```bash
sudo systemctl stop talent-pilot-api talent-pilot-dashboard
sudo tar czf ~/talent-pilot-$(date +%F).tar.gz -C /var/lib/talent-pilot .
sudo systemctl start talent-pilot-api talent-pilot-dashboard
```

A nightly cron job:

```bash
0 3 * * * systemctl stop talent-pilot-api talent-pilot-dashboard && \
  tar czf /root/backups/tp-$(date +\%F).tar.gz -C /var/lib/talent-pilot . && \
  systemctl start talent-pilot-api talent-pilot-dashboard && \
  find /root/backups -name 'tp-*.tar.gz' -mtime +14 -delete
```

Restore:

```bash
sudo systemctl stop talent-pilot-api talent-pilot-dashboard
sudo rm -rf /var/lib/talent-pilot/*
sudo tar xzf ~/talent-pilot-2026-08-05.tar.gz -C /var/lib/talent-pilot
sudo chown -R talentpilot:talentpilot /var/lib/talent-pilot
sudo systemctl start talent-pilot-api talent-pilot-dashboard
```

---

## Security checklist

Handled in code:

- Passwords hashed with PBKDF2-SHA256, 600k iterations, per-user salt
- Sign-in failures take constant time, so accounts cannot be enumerated
- API tokens stored as SHA-256 digests; a database copy grants no sessions
- Rate limiting per email and per IP, surviving restarts
- Invite code compared in constant time
- CORS restricted to extension origins
- Workspaces keyed by account id, so no caller string touches a path
- Uploads validated by type and size; profile names cannot escape the workspace
- Extension token confined to the service worker; scraped values never `innerHTML`

Handled by this deployment:

- Both app processes bound to localhost; Caddy is the only public listener
- Services run as an unprivileged account with `ProtectSystem=strict`
- Secrets in a root-owned env file, mode 640
- Caddy overwrites `X-Forwarded-For`, which is what makes
  `TRUST_PROXY_HEADERS=true` safe here
- HSTS and `nosniff` headers

Yours to do:

- **Set a Gemini billing cap.** An invite code limits who registers; each
  account still spends your quota.
- **Keep the invite code private**, and rotate it if it leaks.
- **Set up the backup cron.** Nothing is backed up automatically.
- **`sudo apt upgrade` periodically.**

Known gaps:

- **No password reset.** A forgotten password needs database access.
- **No email verification.** Addresses are unconfirmed.
- **No audit log** beyond the application log.
- SQLite is the scaling limit — fine for a handful of users, not hundreds.

---

## Troubleshooting

**The site is unreachable.**
Almost always the Oracle firewall's second layer. Check both the VCN security
list *and* `sudo iptables -L INPUT -n --line-numbers`.

**Caddy cannot get a certificate.**
DNS must resolve to the server before Caddy can validate. Check
`dig +short YOUR_DOMAIN`, then `sudo journalctl -u caddy -n 50`.

**A service will not start.**

```bash
sudo journalctl -u talent-pilot-api -n 50 --no-pager
```

A missing `GEMINI_API_KEY` is the usual cause — the app starts but every AI
call returns a config error.

**Google says `redirect_uri_mismatch`.**
The registered URI must match `PUBLIC_URL` character for character, trailing
slash included, `https` not `http`.

**"Use the Connect Gmail link to authorise this hosted instance."**
Working as intended — the desktop flow is blocked on servers. If the link does
not appear, `PUBLIC_URL` is unset.

**Extension says the address serves a different application.**
Confirm `curl https://YOUR_DOMAIN/health` returns
`"service":"talent-pilot-api"`. If it returns HTML, the Caddy `@api` matcher
is not routing that path.

**Everyone is rate limited at once.**
`TRUST_PROXY_HEADERS` is false, so every request looks like it comes from
Caddy. Set it true.

**Locked out with no way in.**

```bash
cd /opt/talent-pilot && sudo -u talentpilot .venv/bin/python -c \
  "import auth; auth.clear_attempts('email:you@example.com')"
```

**Dashboard loads but the UI never connects.**
Streamlit needs its websocket proxied. Confirm the `handle` block in
`/etc/caddy/Caddyfile` is present and Caddy was restarted.

---

## Appendix: Docker

A `Dockerfile`, `docker-compose.yml`, and `docker/supervisord.conf` are in the
repo if you prefer containers. **They are syntax-validated but were never
built** — Docker Desktop would not start in the development environment.

```bash
docker compose up -d --build
```

The systemd path above is the one this guide recommends, and its commands are
the ones actually exercised.
