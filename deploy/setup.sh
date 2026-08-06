#!/usr/bin/env bash
#
# Talent Pilot server bootstrap (Ubuntu/Debian, no Docker).
#
# Installs the app under /opt/talent-pilot, creates a service account,
# registers two systemd units, and configures Caddy for TLS.
#
# Usage:
#   sudo ./deploy/setup.sh yourdomain.duckdns.org
#   sudo ./deploy/setup.sh --no-domain          # plain HTTP on the server IP
#
# Re-running is safe: it updates the code and restarts the services.

set -euo pipefail

APP_USER="talentpilot"
APP_DIR="/opt/talent-pilot"
DATA_DIR="/var/lib/talent-pilot"
LOG_DIR="/var/log/talent-pilot"
ENV_DIR="/etc/talent-pilot"
ENV_FILE="${ENV_DIR}/talent-pilot.env"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; NC=$'\033[0m'
info()  { echo "${GREEN}==>${NC} $*"; }
warn()  { echo "${YELLOW}!!!${NC} $*"; }
fail()  { echo "${RED}ERROR:${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run with sudo."

DOMAIN="${1:-}"
[[ -n "$DOMAIN" ]] || fail "Pass a domain, or --no-domain. See the header."

# Resolve the repo root from this script's location.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "${REPO_ROOT}/app.py" ]] || fail "Run this from inside the cloned repo."

# ---------------------------------------------------------------------
info "Installing system packages"
# ---------------------------------------------------------------------
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl rsync gnupg \
    debian-keyring debian-archive-keyring apt-transport-https sqlite3

# ---------------------------------------------------------------------
info "Checking available memory"
# ---------------------------------------------------------------------
# The free tiers on GCP (e2-micro) and Oracle (E2.1.Micro) give 1 GB, which
# is not enough to build pandas and streamlit wheels — pip gets OOM-killed
# partway through. Swap has to exist before the install, not after.
TOTAL_MB=$(free -m | awk '/^Mem:/{print $2}')
info "Detected ${TOTAL_MB} MB RAM"

if (( TOTAL_MB < 1800 )); then
    if swapon --show 2>/dev/null | grep -q .; then
        info "Swap already active, leaving it alone"
    else
        warn "Low memory — adding 2 GB swap so the install does not get OOM-killed"
        fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
        swapon /swapfile
        grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
        info "Swap enabled"
    fi
fi

# ---------------------------------------------------------------------
info "Creating service account and directories"
# ---------------------------------------------------------------------
id -u "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

mkdir -p "$APP_DIR" "$DATA_DIR" "$LOG_DIR" "$ENV_DIR"

# ---------------------------------------------------------------------
info "Installing application to ${APP_DIR}"
# ---------------------------------------------------------------------
# Copy the working tree, excluding local state that must not follow it.
rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude 'data' --exclude 'logs' \
    --exclude '__pycache__' --exclude '.env' --exclude 'tests' \
    "${REPO_ROOT}/" "${APP_DIR}/"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

# ---------------------------------------------------------------------
info "Writing environment file"
# ---------------------------------------------------------------------
if [[ -f "$ENV_FILE" ]]; then
    warn "${ENV_FILE} exists — leaving it alone. Edit it by hand to change settings."
else
    # Generated rather than prompted, so a weak invite code is never the default.
    GENERATED_CODE="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"

    if [[ "$DOMAIN" == "--no-domain" ]]; then
        PUBLIC_URL=""
    else
        PUBLIC_URL="https://${DOMAIN}"
    fi

    cat > "$ENV_FILE" <<EOF
# Talent Pilot configuration. Restart services after editing:
#   sudo systemctl restart talent-pilot-api talent-pilot-dashboard

# REQUIRED — get one at https://aistudio.google.com/apikey
GEMINI_API_KEY=PUT_YOUR_KEY_HERE

# Public URL. Empty means "not hosted", which disables the Gmail redirect flow.
PUBLIC_URL=${PUBLIC_URL}

# Registration is gated by this code. Share it only with people you want in.
SIGNUP_CODE=${GENERATED_CODE}

# Set true only because Caddy sits in front and overwrites X-Forwarded-For.
TRUST_PROXY_HEADERS=true

DATA_DIR=${DATA_DIR}
LOG_DIR=${LOG_DIR}
LOG_LEVEL=INFO
EOF
    info "Generated invite code: ${GENERATED_CODE}"
fi

chmod 640 "$ENV_FILE"
chown root:"$APP_USER" "$ENV_FILE"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR" "$DATA_DIR" "$LOG_DIR"

# ---------------------------------------------------------------------
info "Registering systemd services"
# ---------------------------------------------------------------------
cp "${REPO_ROOT}/deploy/talent-pilot-api.service" /etc/systemd/system/
cp "${REPO_ROOT}/deploy/talent-pilot-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now talent-pilot-api talent-pilot-dashboard

# ---------------------------------------------------------------------
info "Configuring Caddy"
# ---------------------------------------------------------------------
if ! command -v caddy &>/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq && apt-get install -y -qq caddy
fi

mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy

API_PATHS='/health* /auth/* /jobs* /profiles* /check-job* /save-job* /analyze-job* /generate-answer* /save-answer* /docs* /openapi.json'

if [[ "$DOMAIN" == "--no-domain" ]]; then
    warn "No domain: serving plain HTTP. Gmail sync will not work."
    # Written out directly rather than un-commenting the template, which
    # would also strip the explanatory comments.
    cat > /etc/caddy/Caddyfile <<EOF
:80 {
	encode gzip

	@api path ${API_PATHS}
	handle @api {
		reverse_proxy 127.0.0.1:8000
	}

	handle {
		reverse_proxy 127.0.0.1:8501 {
			header_up Host {host}
			header_up X-Real-IP {remote_host}
		}
	}
}
EOF
else
    sed "s/YOUR_DOMAIN/${DOMAIN}/" "${REPO_ROOT}/deploy/Caddyfile" > /etc/caddy/Caddyfile
fi

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
    || fail "Caddyfile is invalid — not restarting Caddy."
systemctl restart caddy

# ---------------------------------------------------------------------
info "Opening the local firewall"
# ---------------------------------------------------------------------
# Oracle Cloud images ship restrictive local iptables rules on top of the
# cloud-level security list, and forgetting these is the usual reason a new
# Oracle VM appears unreachable even after opening the ports in the console.
#
# GCP images do not do this — the VPC firewall is the only layer there — so
# these rules are a harmless no-op on Google Compute Engine.
if command -v iptables &>/dev/null; then
    iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null \
        || iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
    iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null \
        || iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
    command -v netfilter-persistent &>/dev/null && netfilter-persistent save || true
fi

# ---------------------------------------------------------------------
info "Checking health"
# ---------------------------------------------------------------------
sleep 6
if curl -fsS http://127.0.0.1:8000/health | grep -q talent-pilot-api; then
    info "API is up."
else
    warn "API did not answer. Check: journalctl -u talent-pilot-api -n 40"
fi

echo
info "Done."
echo
echo "  1. Add your Gemini key:  sudo nano ${ENV_FILE}"
echo "  2. Restart:              sudo systemctl restart talent-pilot-api talent-pilot-dashboard"
if [[ "$DOMAIN" != "--no-domain" ]]; then
    echo "  3. Open:                 https://${DOMAIN}"
    echo "  4. Register the OAuth redirect URI in Google Cloud:"
    echo "                           https://${DOMAIN}/"
else
    echo "  3. Open:                 http://\$(curl -s ifconfig.me)"
fi
echo
echo "  Invite code:  grep SIGNUP_CODE ${ENV_FILE}"
echo "  Logs:         journalctl -u talent-pilot-api -f"
echo
