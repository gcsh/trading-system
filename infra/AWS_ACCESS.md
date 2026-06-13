# Day-to-day AWS access for trading-bot

The bot now runs on AWS EC2. The Mac is the operator console, not the host.

## Open the UI

In one terminal (this stays open while you use the UI):

```bash
aws --region us-east-1 ssm start-session \
  --target i-0426a45181d08adff \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
```

Then open http://localhost:8000 in your browser. The session forwards
your Mac's port 8000 to the EC2 instance over SSM (no public ports, no
SSH keys).

Close the terminal session (Ctrl-C) when done.

## SSH-equivalent shell on the box

```bash
aws --region us-east-1 ssm start-session --target i-0426a45181d08adff
```

Lands you as `ssm-user`. To act as the app user:

```bash
sudo -i -u tradingbot
cd /opt/trading-bot
```

## Common commands on the box

```bash
# Service control
sudo systemctl status trading-bot
sudo systemctl restart trading-bot
sudo systemctl stop trading-bot

# Live logs
sudo journalctl -fu trading-bot

# Recent errors only
sudo journalctl -u trading-bot --since "1 hour ago" -p err

# Check disk + memory
df -h
free -h
top
```

## Re-deploy after code changes

Just run `./deploy.sh` from the repo root. It does everything:

```bash
./deploy.sh                  # standard: rebuild frontend if needed, push, wait
./deploy.sh --skip-frontend  # backend-only: skips npm build (faster)
./deploy.sh --no-wait        # fire-and-forget (don't tail logs)
./deploy.sh --help           # show all options
```

The script:
1. Auto-detects if frontend needs rebuild (checks `src/` mtime vs `dist/`).
2. Zips backend + frontend/dist + ml + requirements.txt.
3. Uploads to S3 (versioned `deploy-YYYYMMDD-HHMMSS.zip` + `latest.zip`).
4. Triggers SSM remote deploy on EC2.
5. Waits for systemd to confirm trading-bot is active.
6. Hits `/bot/status` to confirm API responds.

If anything fails, exit code is 1 and the error tail is printed.

### To update the *remote* deploy script (rare)

`scripts/deploy-trading-bot.sh` in S3 is what does the install on EC2.
You only need to update it if EC2 setup steps change (new system pkgs,
new systemd unit, etc.):

```bash
aws --region us-east-1 s3 cp /path/to/updated-deploy-trading-bot.sh \
  s3://tradingbot-artifacts-157320905163/scripts/deploy-trading-bot.sh
```

Then next `./deploy.sh` will use the new version.

## Fresh-start the trial (resets to $5,000)

```bash
aws --region us-east-1 ssm send-command \
  --instance-ids i-0426a45181d08adff \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["cd /opt/trading-bot && sudo -u tradingbot .venv/bin/python -m backend.bot.system_reset 5000"]'
```

## Rotate / update a secret

```bash
# Anthropic key (example)
aws --region us-east-1 secretsmanager put-secret-value \
  --secret-id trading-bot/anthropic-api-key \
  --secret-string "sk-ant-..."

# Then re-deploy (step 4 above) so the running app picks up the new value.
```

## Backup the SQLite DB to S3

```bash
aws --region us-east-1 ssm send-command \
  --instance-ids i-0426a45181d08adff \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["TS=$(date +%Y%m%d-%H%M%S); cp /opt/trading-bot/trading_bot.db /tmp/db-${TS}.db && aws --region us-east-1 s3 cp /tmp/db-${TS}.db s3://tradingbot-artifacts-157320905163/db-backups/db-${TS}.db && rm /tmp/db-${TS}.db"]'
```

(TODO: cron this on the box — Wave A.5 item.)

## Tear down everything (CAREFUL)

```bash
cd infra/paper
terraform destroy   # destroys EC2, EIP, S3 artifacts, IAM, secrets, SG

# To also destroy the state backend itself (last):
cd ../bootstrap
terraform destroy
```

Note: `force_destroy = false` on the S3 buckets means destroy will
fail if buckets have objects. Empty them first via console or
`aws s3 rm s3://... --recursive`.

## Costs (steady state)

- EC2 t4g.small: $12.27/mo
- EBS gp3 30 GB: $2.40/mo
- Elastic IP (attached): $0
- S3 (state + artifacts): ~$1
- DynamoDB lock (on-demand idle): $0
- Secrets Manager (4 secrets, 2 populated): $1.60
- **Total: ~$17/month**

## What's on the box right now

- AMI: Amazon Linux 2023 ARM
- Python: 3.11 (in venv at `/opt/trading-bot/.venv`)
- App: `/opt/trading-bot/`
- App user: `tradingbot`
- Service: `trading-bot.service` (uvicorn on 127.0.0.1:8000)
- Logs: systemd journal + CloudWatch log group `/trading-bot/paper/app`
- SSM agent: 3.3.4108.0 (preinstalled)
