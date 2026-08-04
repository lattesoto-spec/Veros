#!/usr/bin/env bash
# Set up the Carelog deployment on Vercel from nothing but a Vercel account.
#
#   bash scripts/setup_vercel.sh
#
# Idempotent: every step checks before it acts, so re-running after a failure
# picks up where it stopped. Nothing here deletes data.
#
# Steps that must happen in the Vercel dashboard (creating the database and the
# blob store) are prompted for rather than guessed at — the CLI surface for
# those changes between versions, and a wrong flag would fail confusingly.

set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
step()  { echo; echo "${BOLD}==> $*${OFF}"; }
info()  { echo "    $*"; }
ok()    { echo "    ${GREEN}✓${OFF} $*"; }
warn()  { echo "    ${YELLOW}!${OFF} $*"; }
die()   { echo "    ${RED}✗ $*${OFF}"; exit 1; }
ask()   { # ask VAR "prompt"
  local __var=$1 __prompt=$2 __val=""
  while [ -z "$__val" ]; do read -r -p "    $__prompt " __val; done
  printf -v "$__var" '%s' "$__val"
}

REGION="syd1"   # Australian data residency; must match vercel.json

# --------------------------------------------------------------- 0. tooling
step "0/7  Checking tooling"
command -v node >/dev/null 2>&1 || die "Node.js is required for the Vercel CLI. Install from https://nodejs.org"
if ! command -v vercel >/dev/null 2>&1; then
  info "Installing the Vercel CLI globally..."
  npm install -g vercel
fi
ok "vercel CLI $(vercel --version 2>/dev/null | head -1)"
command -v python3 >/dev/null 2>&1 || die "python3 is required."

if ! vercel whoami >/dev/null 2>&1; then
  info "Opening a browser to log in to Vercel..."
  vercel login
fi
ok "logged in as $(vercel whoami 2>/dev/null)"

# ---------------------------------------------------------------- 1. link
step "1/7  Linking this folder to a Vercel project"
if [ -f .vercel/project.json ]; then
  ok "already linked ($(python3 -c 'import json;print(json.load(open(".vercel/project.json"))["projectId"])' 2>/dev/null || echo linked))"
else
  info "Answer the prompts; choose 'yes' to set up and deploy, and accept the detected settings."
  vercel link
  ok "project linked"
fi

# ------------------------------------------------------------- 2. database
step "2/7  Postgres database"
echo
info "In the Vercel dashboard: ${BOLD}Storage → Create Database → Neon (Postgres)${OFF}"
info "  • Region: choose ${BOLD}Sydney / ap-southeast-2${OFF} (keeps aged-care data in Australia)"
info "  • Connect it to this project when prompted"
info "Then copy the ${BOLD}connection string${OFF} (it starts with postgres:// or postgresql://)."
echo
ask DATABASE_URL "Paste the Postgres connection string:"
case "$DATABASE_URL" in
  postgres://*|postgresql://*) ok "connection string looks right" ;;
  *) die "That does not look like a Postgres URL." ;;
esac

info "Testing the connection and creating the schema..."
python3 -m venv .venv-setup >/dev/null 2>&1 || true
./.venv-setup/bin/pip install -q -r requirements.txt
DATABASE_URL="$DATABASE_URL" ./.venv-setup/bin/python -m flask --app app init-db \
  || die "Could not create the schema — check the connection string and that the database is reachable."
ok "schema created"

# ------------------------------------------------------- 2b. first account
step "2b/7  Your organization and administrator account"
if DATABASE_URL="$DATABASE_URL" ./.venv-setup/bin/python -c "
import os,sys
from app import app
from models import User
with app.app_context():
    sys.exit(0 if User.query.first() else 1)
" 2>/dev/null; then
  ok "an administrator already exists — skipping"
else
  ask ORG_NAME "Your company name (e.g. Sunrise Aged Care):"
  ask ADMIN_EMAIL "Administrator email address:"
  ask ADMIN_PASS "Temporary password (12+ chars, you change it at first sign-in):"
  DATABASE_URL="$DATABASE_URL" ./.venv-setup/bin/python -m flask --app app bootstrap-org \
    --name "$ORG_NAME" --admin-email "$ADMIN_EMAIL" --password "$ADMIN_PASS" \
    --superuser --adopt-existing \
    || die "Could not create the organization."
  ok "organization and administrator created"
fi

# ----------------------------------------------------------------- 3. blob
step "3/7  Blob store (retained audit evidence)"
echo
info "In the dashboard: ${BOLD}Storage → Create → Blob${OFF}"
info "  • Region: ${BOLD}Sydney${OFF}, same reason as the database"
info "  • Access: ${BOLD}Private${OFF} — these are resident care records, not public files"
info "  • Connect it to this project"
info "Connecting the store injects the token automatically. If you set an"
info "environment-variable prefix, it arrives as <PREFIX>_READ_WRITE_TOKEN —"
info "the app finds it under any name, so just press Enter to skip if it is"
info "already connected. Otherwise paste it from the store's .env.local tab."
echo
read -r -p "    Paste the blob read-write token (or Enter to skip): " BLOB_TOKEN
ok "token captured"

# ------------------------------------------------------------------ 4. keys
step "4/7  Application secrets"
SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
WORKER_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
DEBUG_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
ok "generated SECRET_KEY (signs session cookies)"
ok "generated WORKER_SECRET (stops anyone else triggering import workers)"
ok "generated DEBUG_TOKEN (opens the diagnostics endpoints to you only)"
echo
info "Your Anthropic API key is what lets Carelog learn new file formats."
info "Get one at https://console.anthropic.com → API keys (starts with sk-ant-)."
ask ANTHROPIC_KEY "Paste your Anthropic API key:"

# --------------------------------------------------------------- 5. env vars
step "5/7  Pushing environment variables to Vercel"
put_env() { # put_env NAME VALUE
  local name=$1 value=$2
  for env in production preview development; do
    vercel env rm "$name" "$env" --yes >/dev/null 2>&1 || true
    printf '%s' "$value" | vercel env add "$name" "$env" >/dev/null 2>&1 \
      || warn "could not set $name for $env"
  done
  ok "$name"
}
put_env DATABASE_URL          "$DATABASE_URL"
[ -n "${BLOB_TOKEN:-}" ] && put_env BLOB_READ_WRITE_TOKEN "$BLOB_TOKEN" || ok "using the token the store integration injected"
put_env SECRET_KEY            "$SECRET_KEY"
put_env WORKER_SECRET         "$WORKER_SECRET"
put_env DEBUG_TOKEN           "$DEBUG_TOKEN"
put_env ANTHROPIC_API_KEY     "$ANTHROPIC_KEY"
put_env STORAGE_BACKEND       "vercel_blob"
put_env IMPORT_WORKER         "invoke"

# ---------------------------------------------------------------- 6. deploy
step "6/7  Deploying to production"
vercel deploy --prod
DEPLOY_URL=$(vercel inspect --json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("url",""))' 2>/dev/null || true)
[ -n "${DEPLOY_URL:-}" ] || ask DEPLOY_URL "Paste the production URL Vercel printed (without https://):"
BASE="https://${DEPLOY_URL#https://}"
ok "deployed to $BASE"

# ---------------------------------------------------------------- 7. verify
step "7/7  Verifying the live deployment"
info "Deployment health (works even when misconfigured):"
curl -fsS "$BASE/healthz" | python3 -m json.tool || warn "the deployment is not healthy — see the problems listed above"
echo
info "Database and storage self-test:"
curl -fsS -H "x-debug-token: $DEBUG_TOKEN" "$BASE/debug/storage" | python3 -m json.tool \
  || warn "self-test did not return cleanly"
echo
info "Anthropic connectivity self-test:"
curl -fsS -H "x-debug-token: $DEBUG_TOKEN" "$BASE/debug/network" | python3 -m json.tool \
  || warn "self-test did not return cleanly"

cat <<EOF

${BOLD}Done.${OFF} Open $BASE and run an import to confirm end to end.

${BOLD}If you have existing data on Fly${OFF}, bring it across:

  fly ssh sftp get /data/vero.db ./vero.db -a veros
  DATABASE_URL='$DATABASE_URL' \\
  BLOB_READ_WRITE_TOKEN='<token>' STORAGE_BACKEND=vercel_blob \\
    python scripts/migrate_to_postgres.py ./vero.db --uploads ./uploads

${DIM}Both secrets are stored in Vercel; you do not need to keep a copy locally.${OFF}
EOF
