################################################################
# Networking — we use the default VPC. Single EC2, single user.
# Custom VPC/subnets/NAT would be premature complexity for our
# scale; see todo.md item #9 "do NOT recommend k8s/microservices".
################################################################

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Find AZs that support our instance type (us-east-1e doesn't support
# t4g.small at the time of this writing).
data "aws_ec2_instance_type_offerings" "supported" {
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
  location_type = "availability-zone"
}

data "aws_subnet" "by_id" {
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}

locals {
  supported_azs = toset(data.aws_ec2_instance_type_offerings.supported.locations)
  supported_subnet_ids = [
    for s in data.aws_subnet.by_id : s.id if contains(local.supported_azs, s.availability_zone)
  ]
}

resource "aws_security_group" "app" {
  name        = "${local.name}-sg"
  description = "Trading bot app - SSM-only access by default."
  vpc_id      = data.aws_vpc.default.id

  # SSH only if explicit CIDRs given. Empty list = no inbound SSH;
  # use AWS SSM Session Manager instead (no port needed).
  dynamic "ingress" {
    for_each = length(var.allowed_ssh_cidrs) > 0 ? [1] : []
    content {
      description = "SSH from allowed CIDRs"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.allowed_ssh_cidrs
    }
  }

  # Egress: everything. The bot needs to reach yfinance, FRED,
  # EDGAR, Anthropic, broker APIs, etc.
  egress {
    description = "Outbound to anywhere"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
