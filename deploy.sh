#!/usr/bin/env bash
################################################################
# deploy.sh — one-command deploy of trading-bot from this Mac
# to the AWS EC2 instance behind https://pillar-watch.com.
#
# Usage:
#   ./deploy.sh                  # standard deploy
#   ./deploy.sh --skip-frontend  # backend-only deploy (faster)
#   ./deploy.sh --no-wait        # fire-and-forget (don't tail logs)
#   ./deploy.sh --help
#
# Steps:
#   1. Rebuild frontend if src is newer than dist (auto-detect).
#   2. Zip backend + frontend/dist + ml + requirements.txt.
#   3. Upload to S3 (versioned + latest.zip).
#   4. Trigger SSM remote deploy.
#   5. Wait for deploy + healthcheck.
#
# Exit code: 0 on success, 1 on any failure.
################################################################
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ---- config ---------------------------------------------------
REGION="us-east-1"
INSTANCE_ID="i-0426a45181d08adff"
S3_BUCKET="tradingbot-artifacts-157320905163"
PUBLIC_URL="https://pillar-watch.com"

# ---- flags ----------------------------------------------------
SKIP_FRONTEND=false
NO_WAIT=false
for arg in "$@"; do
  case "$arg" in
    --skip-frontend) SKIP_FRONTEND=true ;;
    --no-wait)       NO_WAIT=true ;;
    --help|-h)
      sed -n '2,/^####/p' "$0" | sed -e 's/^# \{0,1\}//' -e '$d'
      exit 0
      ;;
    *) echo "unknown arg: $arg (try --help)" >&2; exit 2 ;;
  esac
done

log() { printf '\033[36m==>\033[0m %s\n' "$*"; }
err() { printf '\033[31mERR\033[0m %s\n' "$*" >&2; }

# ---- 0. sanity ------------------------------------------------
command -v aws >/dev/null || { err "aws CLI not on PATH"; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { err "aws creds not configured (run 'aws configure')"; exit 1; }

# ---- 1. frontend rebuild --------------------------------------
if [ "$SKIP_FRONTEND" = true ]; then
  if [ ! -d frontend/dist ]; then
    err "--skip-frontend given but frontend/dist doesn't exist. Run once without --skip-frontend."
    exit 1
  fi
  log "Skipping frontend (using existing frontend/dist)."
else
  needs_build=false
  if [ ! -d frontend/dist ]; then
    needs_build=true
  elif find frontend/src frontend/index.html frontend/vite.config.js frontend/package.json -newer frontend/dist/index.html 2>/dev/null | grep -q .; then
    needs_build=true
  fi
  if [ "$needs_build" = true ]; then
    if [ ! -d frontend/node_modules ]; then
      log "Installing npm deps (first-run; takes ~1 min)..."
      (cd frontend && npm install --silent)
    fi
    log "Building frontend..."
    (cd frontend && npm run build)
  else
    log "Frontend up-to-date; skipping rebuild."
  fi
fi

# ---- 2. zip ---------------------------------------------------
log "Building deploy zip..."
ZIP=/tmp/trading-bot-deploy.zip
rm -f "$ZIP"
zip -qr "$ZIP" backend frontend/dist ml infra requirements.txt \
  -x '*/__pycache__/*' '*.pyc' '*.pyo' '*/node_modules/*' '*/.DS_Store'
log "Zip size: $(du -h "$ZIP" | cut -f1)"

# ---- 3. upload ------------------------------------------------
TS=$(date +%Y%m%d-%H%M%S)
log "Uploading to s3://$S3_BUCKET/code/{deploy-$TS.zip, latest.zip}..."
aws --region "$REGION" s3 cp "$ZIP" "s3://${S3_BUCKET}/code/deploy-${TS}.zip" >/dev/null
aws --region "$REGION" s3 cp "$ZIP" "s3://${S3_BUCKET}/code/latest.zip" >/dev/null

# ---- 4. trigger SSM deploy -----------------------------------
log "Triggering remote deploy via SSM..."
CMD_ID=$(aws --region "$REGION" ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --comment "deploy ${TS}" \
  --timeout-seconds 1800 \
  --parameters "commands=[\"aws --region ${REGION} s3 cp s3://${S3_BUCKET}/scripts/deploy-trading-bot.sh /tmp/deploy.sh\",\"bash /tmp/deploy.sh 2>&1 | tail -60\"]" \
  --query 'Command.CommandId' --output text)
log "SSM command: $CMD_ID"

if [ "$NO_WAIT" = true ]; then
  log "Fire-and-forget mode. Check status with:"
  echo "  aws --region $REGION ssm get-command-invocation --command-id $CMD_ID --instance-id $INSTANCE_ID"
  exit 0
fi

# ---- 5. wait + report -----------------------------------------
log "Waiting for deploy (typically 30-90s)..."
while true; do
  S=$(aws --region "$REGION" ssm get-command-invocation \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query 'Status' --output text 2>/dev/null || echo Pending)
  case "$S" in
    InProgress|Pending|Delayed) printf '.'; sleep 5 ;;
    *) echo; break ;;
  esac
done
log "SSM status: $S"

# Tail the deploy output.
echo "--- deploy tail (last 20 lines) ---"
aws --region "$REGION" ssm get-command-invocation \
  --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
  --query 'StandardOutputContent' --output text 2>/dev/null | tail -20
echo "-----------------------------------"

if [ "$S" != "Success" ]; then
  err "Deploy failed (status: $S)"
  aws --region "$REGION" ssm get-command-invocation \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query 'StandardErrorContent' --output text 2>/dev/null | tail -20
  exit 1
fi

# ---- 6. healthcheck ------------------------------------------
log "Healthcheck..."
HC=$(aws --region "$REGION" ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["systemctl is-active trading-bot && curl -sf -m 5 http://127.0.0.1:8000/bot/status"]' \
  --query 'Command.CommandId' --output text)
sleep 4
aws --region "$REGION" ssm get-command-invocation \
  --command-id "$HC" --instance-id "$INSTANCE_ID" \
  --query 'StandardOutputContent' --output text

log "Done. Live at $PUBLIC_URL"
