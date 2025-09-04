# Monte Carlo's OpenTelemetry Collector Service - Terraform Configuration
# Copyright 2023 Monte Carlo Data, Inc.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Data sources
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Local values for conditions (equivalent to CloudFormation Conditions)
locals {
  use_current_account                  = var.external_access_principal == "N/A"
  use_aws_principal                    = var.external_access_principal_type == "AWS"
  has_additional_security_group        = var.existing_security_group_id != "N/A"
  use_custom_external_access_role_name = var.external_access_role_name != "N/A"

  # Extract S3 bucket name from ARN
  external_s3_bucket_name = split(":", var.telemetry_data_bucket_arn)[5]

  # Common tags
  common_tags = {
    Service  = "mcd-otel-collector"
    Provider = "monte-carlo"
  }

  # External access role name
  external_access_role_name = local.use_custom_external_access_role_name ? var.external_access_role_name : "${var.deployment_name}-EAR"
}



# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "log_group" {
  name              = "/aws/ecs/${var.deployment_name}-otel-collector"
  retention_in_days = 14

  tags = local.common_tags
}

# Security Group
resource "aws_security_group" "security_group" {
  name_prefix = "${var.deployment_name}-otel-collector-"
  description = "Security group for OpenTelemetry Collector containers"
  vpc_id      = var.existing_vpc_id

  tags = local.common_tags
}

# Security Group Ingress Rule
resource "aws_security_group_rule" "security_group_ingress" {
  type                     = "ingress"
  from_port                = 4317
  to_port                  = 4318
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.security_group.id
  security_group_id        = aws_security_group.security_group.id
  description              = "Allow TCP ingress on ports 4317 and 4318 from other resources associated with the security group within the VPC"
}

# Network Load Balancer
resource "aws_lb" "network_load_balancer" {
  name               = "${var.deployment_name}-otel-nlb"
  internal           = true
  load_balancer_type = "network"
  subnets            = var.existing_subnet_ids

  security_groups = local.has_additional_security_group ? [
    aws_security_group.security_group.id,
    var.existing_security_group_id
  ] : [aws_security_group.security_group.id]

  tags = local.common_tags
}

# Target Group for gRPC
resource "aws_lb_target_group" "target_group_grpc" {
  name        = "${var.deployment_name}-otel-grpc-tg"
  port        = var.grpc_port
  protocol    = "TCP"
  target_type = "ip"
  vpc_id      = var.existing_vpc_id

  health_check {
    enabled             = true
    healthy_threshold   = 2
    interval            = 30
    port                = "traffic-port"
    protocol            = "TCP"
    timeout             = 5
    unhealthy_threshold = 3
  }

  tags = local.common_tags
}

# Target Group for HTTP
resource "aws_lb_target_group" "target_group_http" {
  name        = "${var.deployment_name}-otel-http-tg"
  port        = var.http_port
  protocol    = "TCP"
  target_type = "ip"
  vpc_id      = var.existing_vpc_id

  health_check {
    enabled             = true
    healthy_threshold   = 2
    interval            = 30
    port                = "traffic-port"
    protocol            = "TCP"
    timeout             = 5
    unhealthy_threshold = 3
  }

  tags = local.common_tags
}

# Listener for gRPC
resource "aws_lb_listener" "listener_grpc" {
  load_balancer_arn = aws_lb.network_load_balancer.arn
  port              = var.grpc_port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.target_group_grpc.arn
  }
}

# Listener for HTTP
resource "aws_lb_listener" "listener_http" {
  load_balancer_arn = aws_lb.network_load_balancer.arn
  port              = var.http_port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.target_group_http.arn
  }
}

# ECS Cluster
resource "aws_ecs_cluster" "ecs_cluster" {
  name = "${var.deployment_name}-otel-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = local.common_tags
}

# IAM Role for Task Execution
resource "aws_iam_role" "task_execution_role" {
  name = "${var.deployment_name}-otel-task-execution-role"
  path = "/"

  assume_role_policy = jsonencode({
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })

  tags = local.common_tags
}

# Attach managed policy to task execution role
resource "aws_iam_role_policy_attachment" "task_execution_role_policy" {
  role       = aws_iam_role.task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# IAM Role for Task
resource "aws_iam_role" "task_role" {
  name = "${var.deployment_name}-otel-task-role"
  path = "/"

  assume_role_policy = jsonencode({
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })

  tags = local.common_tags
}

# IAM Policy for Task Role (S3 Export)
resource "aws_iam_role_policy" "task_role_s3_policy" {
  name = "${var.deployment_name}-S3Export"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Statement = [
      {
        Action = [
          "s3:PutObject",
          "s3:PutObjectAcl",
          "s3:GetBucketLocation"
        ]
        Effect = "Allow"
        Resource = [
          "${var.telemetry_data_bucket_arn}/mcd/otel-collector/*",
          var.telemetry_data_bucket_arn
        ]
      }
    ]
  })
}

# External Access Role
resource "aws_iam_role" "external_access_role" {
  name = local.external_access_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = local.use_current_account ? {
          AWS = [data.aws_caller_identity.current.account_id]
          } : (local.use_aws_principal ? {
            AWS = [
              var.external_access_principal,
              "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.external_access_role_name}"
            ]
            } : {
            Federated = [var.external_access_principal]
        })
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = var.external_id
          }
        }
      }
    ]
  })

  tags = local.common_tags
}

# External Access S3 Read Only Policy
resource "aws_iam_policy" "external_access_s3_read_only_policy" {
  name = "${var.deployment_name}-OpenTelemetryS3ExternalAccessReadOnly"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:GetObjectVersion"
        ]
        Resource = [
          var.telemetry_data_bucket_arn,
          "${var.telemetry_data_bucket_arn}/*"
        ]
      }
    ]
  })
}

# Attach policy to external access role
resource "aws_iam_role_policy_attachment" "external_access_policy_attachment" {
  role       = aws_iam_role.external_access_role.name
  policy_arn = aws_iam_policy.external_access_s3_read_only_policy.arn
}

# ECS Task Definition
resource "aws_ecs_task_definition" "task_definition" {
  family                   = "${var.deployment_name}-otel-collector"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.task_execution_role.arn
  task_role_arn            = aws_iam_role.task_role.arn

  container_definitions = jsonencode([
    {
      name      = "otel-collector"
      image     = var.container_image
      essential = true

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.log_group.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "otel-collector"
        }
      }

      portMappings = [
        {
          containerPort = var.grpc_port
          protocol      = "tcp"
        },
        {
          containerPort = var.http_port
          protocol      = "tcp"
        }
      ]

      command = [
        "--config",
        "env:OTEL_CONFIG_CONTENT"
      ]

      environment = [
        {
          name  = "AWS_REGION"
          value = data.aws_region.current.name
        },
        {
          name  = "OTEL_CONFIG_CONTENT"
          value = <<-EOT
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:${var.grpc_port}
      http:
        endpoint: 0.0.0.0:${var.http_port}

processors:
  batch:
    timeout: ${var.batch_timeout}
    send_batch_size: ${var.batch_size}
  memory_limiter:
    check_interval: 1s
    limit_mib: ${var.memory_limit_mib}
    spike_limit_mib: ${var.memory_spike_limit_mib}

exporters:
  debug:
    verbosity: detailed
  awss3/traces:
    s3uploader:
      region: ${data.aws_region.current.name}
      s3_bucket: ${local.external_s3_bucket_name}
      s3_prefix: mcd/otel-collector/traces
      file_prefix: traces
  awss3/metrics:
    s3uploader:
      region: ${data.aws_region.current.name}
      s3_bucket: ${local.external_s3_bucket_name}
      s3_prefix: mcd/otel-collector/metrics
      file_prefix: metrics
  awss3/logs:
    s3uploader:
      region: ${data.aws_region.current.name}
      s3_bucket: ${local.external_s3_bucket_name}
      s3_prefix: mcd/otel-collector/logs
      file_prefix: logs

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug, awss3/traces]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug, awss3/metrics]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug, awss3/logs]
EOT
        }
      ]
    }
  ])

  tags = local.common_tags
}

# ECS Service
resource "aws_ecs_service" "ecs_service" {
  name            = "${var.deployment_name}-otel-service"
  cluster         = aws_ecs_cluster.ecs_cluster.id
  task_definition = aws_ecs_task_definition.task_definition.arn
  desired_count   = var.task_desired_count
  launch_type     = "FARGATE"

  enable_ecs_managed_tags = true

  load_balancer {
    target_group_arn = aws_lb_target_group.target_group_grpc.arn
    container_name   = "otel-collector"
    container_port   = var.grpc_port
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.target_group_http.arn
    container_name   = "otel-collector"
    container_port   = var.http_port
  }

  network_configuration {
    assign_public_ip = false
    security_groups = local.has_additional_security_group ? [
      aws_security_group.security_group.id,
      var.existing_security_group_id
    ] : [aws_security_group.security_group.id]
    subnets = var.existing_subnet_ids
  }

  depends_on = [
    aws_lb_listener.listener_grpc,
    aws_lb_listener.listener_http
  ]

  tags = local.common_tags
}


