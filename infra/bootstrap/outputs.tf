output "tfstate_bucket" {
  value       = aws_s3_bucket.tfstate.id
  description = "S3 bucket name for Terraform remote state. Reference this in paper/backend.tf."
}

output "tfstate_lock_table" {
  value       = aws_dynamodb_table.tfstate_lock.name
  description = "DynamoDB table name for Terraform state locking."
}

output "region" {
  value = "us-east-1"
}
