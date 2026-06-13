################################################################
# Inputs — override via terraform.tfvars (gitignored).
# Defaults are sane for the paper-trial single-user setup.
################################################################

variable "region" {
  description = "AWS region. Don't change after first apply without a migration plan."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Used as the prefix for all resource names + the Project tag."
  type        = string
  default     = "trading-bot"
}

variable "environment" {
  description = "Logical env. 'paper' today; 'live' added later when we trade real money."
  type        = string
  default     = "paper"
}

variable "instance_type" {
  description = "EC2 size. ARM Graviton is cheapest and our workload doesn't care about arch."
  type        = string
  default     = "t4g.small"
}

variable "ebs_size_gb" {
  description = "Root volume size. SQLite + logs + cached data — 20 GB is plenty."
  type        = number
  default     = 20
}

variable "create_compute" {
  description = "Gate for EC2 + Elastic IP. Leave false until ready to deploy the app — saves ~$12/mo."
  type        = bool
  default     = false
}

variable "allowed_ssh_cidrs" {
  description = "CIDRs allowed to SSH (port 22). Empty list = SSM-only access (recommended)."
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "How long CloudWatch keeps app logs. 30 is plenty for diagnosis."
  type        = number
  default     = 30
}
