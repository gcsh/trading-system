################################################################
# Remote state backend.
#
# The bucket + table below are created by ../bootstrap. After that
# one-time apply, this file works as-is.
#
# If you ever need to change account/region, update both this file
# AND ../bootstrap/main.tf.
################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "tradingbot-tfstate-157320905163"
    key            = "paper/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "tradingbot-tfstate-lock"
    encrypt        = true
  }
}
