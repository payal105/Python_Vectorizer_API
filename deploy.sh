#!/usr/bin/env bash
# Deploys the latest Python_Vectorizer_API code on the VPS: pull -> install -> restart -> verify.
# Run it from inside the checkout on the VPS (/opt/vectorizer-api/Python_Vectorizer_API):
#   ./deploy.sh
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE_NAME="vectorizer-api"
LOCAL_HEALTH_URL="http://127.0.0.1:8000/api/v1/health"
PUBLIC_HEALTH_URL="https://mydesignbazaar.com/vector-api/api/v1/health"

echo "==> Checking for uncommitted local changes in $SCRIPT_DIR"
if [ -n "$(git status --porcelain)" ]; then
  echo "Local changes found — aborting so nothing gets overwritten by the pull."
  git status --short
  exit 1
fi

echo "==> Pulling latest code (fast-forward only)"
git pull --ff-only

echo "==> Installing dependencies"
.venv/bin/pip install -r requirements.txt

echo "==> Restarting $SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "==> Waiting for it to come up"
sleep 2

if ! sudo systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "Service failed to start. Recent logs:"
  sudo journalctl -u "$SERVICE_NAME" -n 40 --no-pager
  exit 1
fi

echo "==> Checking health (local)"
if ! curl -sf "$LOCAL_HEALTH_URL" > /dev/null; then
  echo "Local health check failed at $LOCAL_HEALTH_URL"
  sudo journalctl -u "$SERVICE_NAME" -n 40 --no-pager
  exit 1
fi

echo "==> Checking health (public, through Nginx)"
if ! curl -sf "$PUBLIC_HEALTH_URL" > /dev/null; then
  echo "Public health check failed at $PUBLIC_HEALTH_URL — check the Nginx /vector-api location."
  exit 1
fi

echo "==> Deployed and healthy."
