# Deploying Talent Pilot

How to run Talent Pilot on a server you control, and what to change before it
faces the internet.

> **Verification status.** The application was exercised end-to-end in hosted
> configuration on the author's machine: invite-code enforcement, per-IP rate
> limiting behind a proxy header, CORS restriction, and both services running
> under the exact commands the container uses. The **Docker image itself was
> never built** — Docker Desktop would not start in that environment — so the
> `Dockerfile`, `docker-compose.yml`, and `supervisord.conf` are
> syntax-validated but untested. Build them once before trusting them.

---

## Contents

- [What you are deploying](#what-you-are-deploying)
- [Pre-flight checklist](#pre-flight-checklist)
- [Environment variables](#environment-variables)
- [Option A — Oracle Cloud Always Free](#option-a--oracle-cloud-always-free)
- [Option B — Hugging Face Spaces](#option-b--hugging-face-spaces)
- [Option C — Any Docker host](#option-c--any-docker-host)
- [Google OAuth for a hosted instance](#google-oauth-for-a-hosted-instance)
- [Pointing the extension at your server](#pointing-the-extension-at-your-server)
- [Backups](#backups)
- [Security checklist](#security-checklist)
- [Troubleshooting](#troubleshooting)

---

## What you are deploying

Two processes that share one filesystem:

| Process | Port | Serves |
| ------- | ---- | ------ |
| Streamlit dashboard | 8501 | The web UI you sign in to |
| FastAPI backend | 8000 | The Chrome extension |

They **must** share storage — the dashboard writes resume profiles that the API
reads, and both use the same per-user workspaces. Splitting them across
machines without shared storage will not work.

State lives entirely under `DATA_DIR`:

```text
$DATA_DIR/
├── users.db              accounts, tokens, failed sign-in records
└── workspaces/<user_id>/
    ├── jobs.db
    ├── profiles/*.json
    ├── answers/*.txt
    └── gmail_token.json
```

**This directory must be on persistent storage.** On a platform with an
ephemeral filesystem, every redeploy deletes every account.

### Free hosting, honestly

| Platform | Free? | Persistent disk | Verdict |
| -------- | ----- | --------------- | ------- |
| Oracle Cloud Always Free | Yes, indefinitely | Yes, 200 GB | Best fit; most setup |
| Hugging Face Spaces | Yes | No (paid add-on) | Easiest; data resets |
| Fly.io | No longer | Yes | Cheap, not free |
| Render | Free tier exists | **No** on free | Data resets; also sleeps |
| Railway | $5 credit, then paid | Yes | Good, not free |
| Streamlit Community Cloud | Yes | No | **Cannot run the API at all** |

Free tiers change often — confirm current terms before committing.

---

## Pre-flight checklist

Before exposing anything:

- [ ] `SIGNUP_CODE` set, or `REGISTRATION_CLOSED=true`
- [ ] `GEMINI_API_KEY` supplied as a secret, never committed
- [ ] `DATA_DIR` points at persistent storage
- [ ] `PUBLIC_URL` set to the real HTTPS URL
- [ ] `TRUST_PROXY_HEADERS=true` **only** if a reverse proxy sits in front
- [ ] TLS terminated (Caddy, nginx + certbot, or the platform's own)
- [ ] Google OAuth redirect URI registered, if using Gmail sync
- [ ] A backup of `DATA_DIR` scheduled

Set a billing cap on your Gemini key. An invite code limits who can register,
but each account still spends your quota.

---

## Environment variables

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `GEMINI_API_KEY` | — | **Required.** Google AI Studio key |
| `PUBLIC_URL` | empty | Public HTTPS base URL. Setting it switches Gmail to the redirect OAuth flow |
| `SIGNUP_CODE` | empty | When set, registration requires this code |
| `REGISTRATION_CLOSED` | `false` | `true` refuses all new accounts |
| `DATA_DIR` | `./data` | Where accounts and workspaces live |
| `LOG_DIR` | `./logs` | Where logs are written |
| `TRUST_PROXY_HEADERS` | `false` | Honour `X-Forwarded-For`. Only behind a trusted proxy |
| `MAX_LOGIN_ATTEMPTS` | `10` | Failures before lockout |
| `LOGIN_LOCKOUT_MINUTES` | `15` | Lockout duration |
| `TOKEN_TTL_DAYS` | `30` | Extension token lifetime |
| `ALLOWED_ORIGIN_REGEX` | chrome-extension only | Origins allowed to call the API |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Model for all AI calls |
| `LOG_LEVEL` | `INFO` | `DEBUG` for per-call detail |

### Why `TRUST_PROXY_HEADERS` defaults to off

Rate limiting keys on the client IP. `X-Forwarded-For` is a request header, so
a client can set it to anything. On a directly-exposed server, honouring it
would let an attacker send a fresh fake address on every attempt and never hit
the limit. Turn it on **only** when a proxy you control overwrites that header.

---

## Option A — Oracle Cloud Always Free

The most capable free option: a real VM with real disk, free indefinitely.

### 1. Create the instance

In the Oracle Cloud console: **Compute → Instances → Create**.

- Shape: **VM.Standard.A1.Flex** (Ampere ARM), 1–4 OCPU, 6–24 GB RAM
- Image: Ubuntu 22.04
- Save the SSH key it offers — you cannot retrieve it later

Then open the ports: **Networking → Virtual Cloud Networks → your VCN →
Security Lists → Add Ingress Rules** for TCP 80 and 443.

Oracle images also carry local firewall rules:

```bash
sudo iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

### 2. Install Docker

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER && newgrp docker
```

### 3. Deploy

```bash
git clone <your-repo-url> talent-pilot && cd talent-pilot
```

Create `.env` next to `docker-compose.yml`:

```bash
GEMINI_API_KEY=your_real_key
PUBLIC_URL=https://jobs.yourdomain.com
SIGNUP_CODE=pick-something-long-and-random
TRUST_PROXY_HEADERS=true
```

Upload `credentials.json` if you want Gmail sync, then:

```bash
docker compose up -d --build
```

### 4. TLS with Caddy

Caddy obtains and renews certificates automatically. Create `Caddyfile`:

```caddyfile
jobs.yourdomain.com {
    handle /health* {
        reverse_proxy localhost:8000
    }
    handle /auth/* {
        reverse_proxy localhost:8000
    }
    handle /jobs* {
        reverse_proxy localhost:8000
    }
    handle /profiles* {
        reverse_proxy localhost:8000
    }
    handle /analyze-job* {
        reverse_proxy localhost:8000
    }
    handle /generate-answer* {
        reverse_proxy localhost:8000
    }
    handle /save-answer* {
        reverse_proxy localhost:8000
    }
    handle /check-job* {
        reverse_proxy localhost:8000
    }
    handle /save-job* {
        reverse_proxy localhost:8000
    }
    handle {
        reverse_proxy localhost:8501
    }
}
```

```bash
sudo apt install -y caddy && sudo systemctl restart caddy
```

Everything not matched by an API route falls through to the dashboard, so
`https://jobs.yourdomain.com` is the UI and the same host serves the API.

> Prefer a cleaner split? Put the API on `api.yourdomain.com` and reverse-proxy
> the whole host to port 8000, leaving the apex for the dashboard.

---

## Option B — Hugging Face Spaces

Fastest path to a public URL. **Storage is ephemeral on the free tier** — data
is lost on restart or rebuild, so treat this as a demo, not your real tracker.

1. Create a Space, type **Docker**, visibility **Private**.
2. Push this repo to it.
3. In **Settings → Variables and secrets**, add `GEMINI_API_KEY`, `SIGNUP_CODE`,
   and `PUBLIC_URL` (`https://<user>-<space>.hf.space`).
4. Spaces route a single port. Add this so the dashboard is what visitors get:

```dockerfile
ENV STREAMLIT_SERVER_PORT=7860
EXPOSE 7860
```

and change the dashboard command in `docker/supervisord.conf` to
`--server.port 7860`.

Because only one port is exposed, the extension cannot reach the API on a
Space unless you front both with a path-routing proxy. Spaces suits the
dashboard alone.

---

## Option C — Any Docker host

```bash
docker compose up -d --build
docker compose logs -f
```

Or without compose:

```bash
docker build -t talent-pilot .
```

```bash
docker run -d --name talent-pilot \
  -p 8501:8501 -p 8000:8000 \
  -v talent-pilot-data:/data \
  -v "$PWD/credentials.json:/app/credentials.json:ro" \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  -e PUBLIC_URL="https://jobs.yourdomain.com" \
  -e SIGNUP_CODE="your-invite-code" \
  -e TRUST_PROXY_HEADERS=true \
  --restart unless-stopped \
  talent-pilot
```

### Running without Docker

The container is a convenience, not a requirement. Under systemd, run the same
two commands the container runs:

```bash
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
```

```bash
python -m streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true
```

Both need the same environment and the same `DATA_DIR`. *These two commands are
the ones verified in hosted configuration.*

---

## Google OAuth for a hosted instance

The desktop OAuth flow **cannot work on a server** — it opens a browser on the
machine running the code. Setting `PUBLIC_URL` switches the app to the
redirect flow automatically.

In the [Google Cloud console](https://console.cloud.google.com/):

1. **APIs & Services → Enable APIs** → enable **Gmail API**.
2. **OAuth consent screen** → External → add your address under **Test users**.
   An unverified app is capped at 100 test users, which is ample here.
3. **Credentials → Create credentials → OAuth client ID → Web application**.
4. Under **Authorised redirect URIs**, add your `PUBLIC_URL` **exactly**,
   including the trailing slash:

   ```text
   https://jobs.yourdomain.com/
   ```

5. Download the JSON as `credentials.json` and mount it into the container.

Users then click **Connect Gmail** in the sidebar, approve at Google, and land
back on the dashboard with the mailbox connected. The callback is checked
against a `state` value held in the session, so one user's redirect cannot
attach a mailbox to another account.

Scope requested is `gmail.readonly` — the app can read mail and nothing else.

---

## Pointing the extension at your server

The extension defaults to `http://localhost:8000`. To use a hosted API:

1. Open the extension popup.
2. Expand **⚙️ Settings**.
3. Enter your API address (`https://jobs.yourdomain.com`) and **Save**.
4. Chrome asks permission for that origin — accept it.
5. Sign in.

Changing the address clears the stored token, since a token from one server is
meaningless to another.

**CORS:** `ALLOWED_ORIGIN_REGEX` accepts `chrome-extension://` origins by
default, which is what the extension needs and excludes ordinary web pages. You
do not need to change it. If you ever must, never widen it to `*` — that would
let any site you visit call your API.

---

## Backups

Everything is in `DATA_DIR`. SQLite needs a consistent copy, so use its own
backup command rather than `cp` on a live database:

```bash
docker compose exec talent-pilot sh -c 'cd /data && \
  for db in users.db workspaces/*/jobs.db; do \
    sqlite3 "$db" ".backup /tmp/$(echo $db | tr / _)"; done'
```

Simplest reliable approach — stop, archive, start:

```bash
docker compose stop
docker run --rm -v talent-pilot-data:/data -v "$PWD:/backup" alpine \
  tar czf /backup/talent-pilot-$(date +%F).tar.gz -C /data .
docker compose start
```

Restore:

```bash
docker compose down
docker run --rm -v talent-pilot-data:/data -v "$PWD:/backup" alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/talent-pilot-2026-08-05.tar.gz -C /data'
docker compose up -d
```

---

## Security checklist

Already handled in code:

- Passwords hashed with PBKDF2-SHA256, 600k iterations, per-user salt
- Sign-in failures take constant time, so accounts cannot be enumerated
- API tokens stored as SHA-256 digests; a database copy grants no sessions
- Rate limiting per email and per IP, surviving restarts
- Invite code compared in constant time
- CORS restricted to extension origins
- Workspaces keyed by account id, so no caller string touches a path
- Uploads validated by type and size; profile names cannot escape the workspace
- Extension token confined to the service worker; scraped values never `innerHTML`

Yours to configure:

- **TLS.** The app speaks plain HTTP and assumes something terminates TLS.
- **`SIGNUP_CODE`.** Without it, anyone reaching the URL can register.
- **Gemini billing caps.** Set them on the Google side.
- **Backups.** Nothing is backed up automatically.

Known gaps:

- **No password reset.** A forgotten password needs direct database access.
- **No email verification.** Addresses are unconfirmed.
- **No audit log** beyond the application log.
- **Rate limiting is per-instance.** Running several replicas against one
  database still works, since counters live in SQLite — but SQLite itself is
  the scaling limit long before that matters.

---

## Troubleshooting

**Accounts disappear after redeploy.**
`DATA_DIR` is not on persistent storage. Mount a volume at `/data`.

**"Use the Connect Gmail link to authorise this hosted instance."**
Working as intended — the desktop flow is blocked on servers. Use the sidebar
link. If it does not appear, `PUBLIC_URL` is unset.

**Google says `redirect_uri_mismatch`.**
The registered URI must match `PUBLIC_URL` character for character, trailing
slash included, and `https` not `http`.

**Extension says the address serves a different application.**
It is reaching something other than this API — often the dashboard. Check
Settings, and confirm `curl https://your-url/health` returns
`"service":"talent-pilot-api"`.

**Rate limiting locks out everyone at once.**
`TRUST_PROXY_HEADERS` is off behind a proxy, so every request appears to come
from the proxy's IP. Set it to `true`.

**Locked out with no way in.**

```bash
docker compose exec talent-pilot python -c \
  "import auth; auth.clear_attempts('email:you@example.com')"
```

**Container is unhealthy.**

```bash
docker compose logs --tail=50
docker compose exec talent-pilot curl -s localhost:8000/health
```

Supervisor restarts either process if it dies; repeated restarts in the log
point at a missing environment variable.
