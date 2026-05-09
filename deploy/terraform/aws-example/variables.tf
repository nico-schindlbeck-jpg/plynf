# SPDX-License-Identifier: Apache-2.0
# Plinth — Terraform input variables.

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix applied to every resource name (must be DNS-safe, <=24 chars)."
  type        = string
  default     = "plinth"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,23}$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter, contain only lowercase letters / digits / '-', and be no longer than 24 chars."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.30.0.0/16"
}

variable "azs" {
  description = "AWS availability zones to spread subnets across."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

variable "kubernetes_version" {
  description = "EKS Kubernetes minor version (must be supported by AWS at apply time)."
  type        = string
  default     = "1.30"
}

variable "node_instance_types" {
  description = "EC2 instance types for the EKS managed node group."
  type        = list(string)
  default     = ["t3.medium"]
}

variable "node_desired_size" {
  description = "Desired number of worker nodes."
  type        = number
  default     = 3
}

variable "node_min_size" {
  description = "Minimum number of worker nodes."
  type        = number
  default     = 3
}

variable "node_max_size" {
  description = "Maximum number of worker nodes."
  type        = number
  default     = 10
}

variable "rds_instance_class" {
  description = "RDS Postgres instance class."
  type        = string
  default     = "db.t3.medium"
}

variable "rds_allocated_storage_gb" {
  description = "RDS Postgres allocated storage (GB)."
  type        = number
  default     = 50
}

variable "rds_engine_version" {
  description = "RDS Postgres engine version."
  type        = string
  default     = "16.3"
}

variable "rds_username" {
  description = "RDS master username."
  type        = string
  default     = "plinth"
}

variable "tags" {
  description = "Common tags applied to every taggable resource."
  type        = map(string)
  default = {
    "Project"   = "plinth"
    "ManagedBy" = "terraform"
  }
}

# Plinth-specific config.

variable "plinth_namespace" {
  description = "Kubernetes namespace for Plinth."
  type        = string
  default     = "plinth"
}

variable "plinth_chart_path" {
  description = "Local filesystem path to the Plinth Helm chart."
  type        = string
  default     = "../../helm/plinth"
}

variable "plinth_image_tag" {
  description = "Plinth container image tag (matches helm appVersion)."
  type        = string
  default     = "1.0.0"
}

variable "plinth_values_files" {
  description = "Extra values files to layer on top of the chart defaults."
  type        = list(string)
  default     = ["../../helm/plinth/values.yaml", "../../helm/plinth/values.prod.yaml"]
}
