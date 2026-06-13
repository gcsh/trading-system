################################################################
# Bootstrap: creates the S3 bucket + DynamoDB table that the
# main Terraform config (../paper) uses as its remote state
# backend. This module uses local state because it can't reference
# a backend it hasn't created yet.
#
# One-time apply. After this runs, never modify these resources
# from the main config — they're foundational.
################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = {
      Project   = "trading-bot"
      ManagedBy = "terraform"
      Component = "tfstate-backend"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  bucket     = "tradingbot-tfstate-${local.account_id}"
  table      = "tradingbot-tfstate-lock"
}

# State bucket — versioned + encrypted + public-access blocked.
resource "aws_s3_bucket" "tfstate" {
  bucket        = local.bucket
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lock table — on-demand billing, zero cost when idle.
resource "aws_dynamodb_table" "tfstate_lock" {
  name         = local.table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}
