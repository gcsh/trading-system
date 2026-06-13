output "artifacts_bucket" {
  value       = aws_s3_bucket.artifacts.id
  description = "S3 bucket for code archive + DB backups."
}

output "ec2_role_arn" {
  value       = aws_iam_role.ec2.arn
  description = "IAM role attached to the future EC2 instance."
}

output "ec2_instance_profile" {
  value       = aws_iam_instance_profile.ec2.name
  description = "Instance profile name for EC2."
}

output "security_group_id" {
  value       = aws_security_group.app.id
  description = "Security group for the app."
}

output "log_group_name" {
  value       = aws_cloudwatch_log_group.app.name
  description = "CloudWatch log group for the app."
}

output "secret_arns" {
  value = {
    for k, s in aws_secretsmanager_secret.app : k => s.arn
  }
  description = "ARNs of the Secrets Manager placeholders. Populate values via AWS Console."
}

output "instance_id" {
  value       = var.create_compute ? aws_instance.app[0].id : null
  description = "EC2 instance ID (null until create_compute = true)."
}

output "instance_public_ip" {
  value       = var.create_compute ? aws_eip.app[0].public_ip : null
  description = "Public IP for the box (null until create_compute = true)."
}

output "ssm_connect_command" {
  value = var.create_compute ? (
    "aws ssm start-session --target ${aws_instance.app[0].id} --region ${var.region}"
  ) : null
  description = "Command to SSH-equivalent into the box via SSM (null until create_compute = true)."
}
