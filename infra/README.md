# Trading-bot AWS infrastructure

Terraform that provisions the AWS substrate for the trading bot. See
[../todo.md](../todo.md) item #9 for the operator decision and overall
plan.

## Scope

This is **Phase 1 — prerequisite layer only.** It does NOT spin up the
EC2 instance yet. The compute module is written but gated behind a
variable (`create_compute = false`) so we save ~$12/mo while finishing
the Wave A code work.

When ready to actually deploy the app, flip the variable to `true` and
re-apply.

### What gets created in Phase 1 (this apply)

- 1 S3 bucket for Terraform state (encrypted + versioned)
- 1 DynamoDB table for Terraform state locking
- 1 S3 bucket for code archive + daily DB backups (versioned, lifecycle)
- 1 IAM role + instance profile for the future EC2
- 4 AWS Secrets Manager secrets (placeholders — you populate the values
  via the AWS Console after apply)
- 1 Security group (no inbound; SSM is the only access path)
- 1 CloudWatch log group
- Default VPC reference (we use the default VPC; no custom networking)

### What's gated to Phase 2 (later apply, when ready to deploy app)

- EC2 t4g.small + EBS gp3 20 GB
- Elastic IP (only allocated when EC2 is created)
- EC2 user-data bootstrap script

## Cost shape

- **Phase 1 idle (today through deploy day):** ~$1-2/month
  - S3 buckets: ~$0.50
  - DynamoDB on-demand: $0 when not in use
  - Secrets Manager: $0.40 × 4 = $1.60
  - CloudWatch log group (no logs flowing yet): $0
- **Phase 2 running (after EC2 enabled):** ~$15-18/month
  - EC2 t4g.small: $12.27
  - EBS gp3 20 GB: $1.60
  - Elastic IP attached: $0 (free when attached)
  - Total: ~$16-17/month

## Prerequisites

- Terraform ≥ 1.6 (we have 1.15.4 — fine)
- AWS CLI configured with an IAM user that has admin (this can be
  locked down later via Terraform itself)
- `aws sts get-caller-identity` must return your IAM user's ARN,
  not the root user's

## First-time setup (bootstrap)

The state bucket and lock table must exist before we can use them as
the Terraform backend — chicken-and-egg. The `bootstrap/` directory
solves this with local state, then `paper/` migrates to the remote
state created by bootstrap.

```bash
cd infra/bootstrap
terraform init
terraform plan       # review what will be created
terraform apply      # creates state bucket + lock table only

cd ../paper
terraform init       # uses the bucket created above
terraform plan       # review the rest
terraform apply      # creates everything else (still no EC2)
```

After bootstrap, you can `rm -rf infra/bootstrap/.terraform` — the
local state for bootstrap is tiny and committed-friendly.

## Day-to-day (after bootstrap)

```bash
cd infra/paper
terraform plan       # see what would change
terraform apply      # apply changes
terraform output     # see useful values (bucket names, role ARN, etc.)
```

## Phase 2 — when ready to deploy the app

Edit `infra/paper/terraform.tfvars`:

```hcl
create_compute = true
```

Then:

```bash
terraform plan       # see EC2 + EIP appear in the plan
terraform apply      # spins up the box
```

## Populating secrets

After Phase 1 apply, populate the placeholder secrets via the AWS
Console (Secrets Manager → each secret → "Retrieve secret value" →
"Edit"):

- `trading-bot/anthropic-api-key` — paste your rotated Anthropic key
- `trading-bot/fred-api-key` — your FRED key from `.env`
- `trading-bot/alpaca-api-key` — optional, only if using Alpaca broker
- `trading-bot/alpaca-secret-key` — optional, only if using Alpaca broker

The EC2 instance role has read access to these secrets but no write
access. Terraform never sees the values — they live only in Secrets
Manager.

## Files

| File | Purpose |
|---|---|
| `bootstrap/main.tf` | Creates state bucket + lock table (local state) |
| `paper/backend.tf` | Remote state config (uses bootstrap output) |
| `paper/providers.tf` | AWS provider |
| `paper/variables.tf` | All inputs |
| `paper/terraform.tfvars.example` | Template — copy to `terraform.tfvars` |
| `paper/locals.tf` | Common locals (tags, names) |
| `paper/network.tf` | Default VPC + security group |
| `paper/storage.tf` | S3 artifacts bucket |
| `paper/iam.tf` | EC2 instance role + policies |
| `paper/secrets.tf` | Secrets Manager placeholders |
| `paper/logs.tf` | CloudWatch log group |
| `paper/compute.tf` | EC2 (gated by var.create_compute) |
| `paper/outputs.tf` | Useful outputs after apply |
| `paper/.gitignore` | Excludes tfstate + tfvars |
