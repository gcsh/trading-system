locals {
  account_id = data.aws_caller_identity.current.account_id
  name       = "${var.project}-${var.environment}"

  artifacts_bucket = "tradingbot-artifacts-${local.account_id}"

  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
