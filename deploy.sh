#!/usr/bin/env bash
# Deploys the latest Python_Vectorizer_API code on the VPS: pull -> install -> restart -> verify.
# Run it from inside the checkout on the VPS (/opt/vectorizer-api/Python_Vectorizer_API):
#   ./deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE_NAME="vectorizer-api"
LOCAL_HEALTH_URL="http://127.0.0.1:8000/api/v1/health"
PUBLIC_HEALTH_URL="https://mydesignbazaar.com/vector-api/api/v1/health"

# --- visual progress helpers ---------------------------------------------------
GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
TOTAL_STEPS=6
STEP=0

step() {
  STEP=$((STEP + 1))
  printf '\n%s[%d/%d] %s%s\n' "$BOLD" "$STEP" "$TOTAL_STEPS" "$1" "$RESET"
}
ok()   { printf '%s  -> done%s\n' "$GREEN" "$RESET"; }
fail() { printf '%s  -> FAILED: %s%s\n' "$RED" "$1" "$RESET"; exit 1; }
# --------------------------------------------------------------------------------

step "Checking for uncommitted local changes"
if [ -n "$(git status --porcelain)" ]; then
  git status --short
  fail "local changes present — aborting so the pull can't overwrite them"
fi
ok

step "Pulling latest code"
if ! git pull --ff-only; then
  fail "git pull failed"
fi
ok

step "Installing dependencies"
if ! .venv/bin/pip install -r requirements.txt; then
  fail "pip install failed"
fi
ok

step "Restarting $SERVICE_NAME"
if ! sudo systemctl restart "$SERVICE_NAME"; then
  fail "failed to restart $SERVICE_NAME"
fi
sleep 2
if ! sudo systemctl is-active --quiet "$SERVICE_NAME"; then
  sudo journalctl -u "$SERVICE_NAME" -n 40 --no-pager
  fail "service did not come up — see logs above"
fi
ok

step "Checking health (local)"
if ! curl -sf "$LOCAL_HEALTH_URL" > /dev/null; then
  sudo journalctl -u "$SERVICE_NAME" -n 40 --no-pager
  fail "local health check failed at $LOCAL_HEALTH_URL"
fi
ok

step "Checking health (public, through Nginx)"
if ! curl -sf "$PUBLIC_HEALTH_URL" > /dev/null; then
  fail "public health check failed at $PUBLIC_HEALTH_URL — check the Nginx /vector-api location"
fi
ok

printf '\n%s%s✔ DEPLOYED AND HEALTHY%s\n\n' "$BOLD" "$GREEN" "$RESET"
