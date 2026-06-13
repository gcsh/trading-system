################################################################
# Compute — EC2 + Elastic IP.
# GATED by var.create_compute (default false).
#
# Leave gated until you're ready to deploy the app. Until then:
#   - Saves ~$12/month
#   - All other resources (S3, IAM, secrets, logs) are ready
#
# When ready: set `create_compute = true` in terraform.tfvars,
# run `terraform apply`. Box comes up in ~3 minutes.
#
# After this is enabled, see ../README.md for the manual app-deploy
# steps (or wait for a follow-up Terraform module that handles
# user-data / systemd / nginx).
################################################################

# Find the latest Amazon Linux 2023 ARM AMI. AL2023 ships with
# SSM agent preinstalled — no inbound SSH needed.
data "aws_ami" "al2023_arm" {
  count       = var.create_compute ? 1 : 0
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

resource "aws_instance" "app" {
  count = var.create_compute ? 1 : 0

  ami                    = data.aws_ami.al2023_arm[0].id
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.ec2.name
  vpc_security_group_ids = [aws_security_group.app.id]
  subnet_id              = local.supported_subnet_ids[0]

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.ebs_size_gb
    encrypted             = true
    delete_on_termination = true
  }

  # Minimal user-data — installs Python + git, installs CloudWatch
  # agent, creates app user. The actual app deployment (clone/zip
  # download, systemd unit, nginx) happens in a follow-up step
  # documented in ../README.md.
  user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail

    dnf update -y
    dnf install -y python3.11 python3.11-pip git amazon-cloudwatch-agent

    # App user with a writable home.
    useradd -m -s /bin/bash tradingbot || true

    # CloudWatch agent — basic config to stream /var/log/messages
    # + journald into our log group. Real config (with app log
    # paths) lands when we deploy the app.
    cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'JSON'
    {
      "logs": {
        "logs_collected": {
          "files": {
            "collect_list": [
              {
                "file_path": "/var/log/messages",
                "log_group_name": "${aws_cloudwatch_log_group.app.name}",
                "log_stream_name": "{instance_id}/messages"
              }
            ]
          }
        }
      }
    }
    JSON

    systemctl enable amazon-cloudwatch-agent
    systemctl start amazon-cloudwatch-agent
  EOF

  tags = {
    Name = "${local.name}-app"
  }

  # If we change user-data, replace the instance.
  user_data_replace_on_change = true

  lifecycle {
    # Don't destroy on AMI churn — AMIs update constantly; we
    # only replace when we explicitly want to.
    ignore_changes = [ami]
  }
}

resource "aws_eip" "app" {
  count    = var.create_compute ? 1 : 0
  instance = aws_instance.app[0].id
  domain   = "vpc"

  tags = {
    Name = "${local.name}-eip"
  }
}
