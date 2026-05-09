# SPDX-License-Identifier: Apache-2.0
# Plinth — Terraform outputs.

output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "EKS API server endpoint."
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_certificate_authority" {
  description = "EKS cluster certificate authority (base64-encoded)."
  value       = aws_eks_cluster.this.certificate_authority[0].data
  sensitive   = true
}

output "kubeconfig_command" {
  description = "Run this to populate ~/.kube/config for the new cluster."
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${aws_eks_cluster.this.name}"
}

output "rds_endpoint" {
  description = "RDS Postgres endpoint (host:port)."
  value       = "${aws_db_instance.this.address}:${aws_db_instance.this.port}"
}

output "rds_database_name" {
  description = "RDS Postgres database name."
  value       = aws_db_instance.this.db_name
}

output "rds_username" {
  description = "RDS Postgres master username."
  value       = aws_db_instance.this.username
}

output "blobs_bucket_name" {
  description = "S3 bucket reserved for future Plinth blob storage."
  value       = aws_s3_bucket.blobs.id
}

output "plinth_irsa_role_arn" {
  description = "IAM role ARN for the Plinth ServiceAccount (IRSA)."
  value       = aws_iam_role.plinth_irsa.arn
}

output "plinth_namespace" {
  description = "Kubernetes namespace where Plinth is installed."
  value       = kubernetes_namespace.plinth.metadata[0].name
}
