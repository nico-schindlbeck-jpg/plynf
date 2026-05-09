# SPDX-License-Identifier: Apache-2.0
# Plinth — Helm release deploying the chart onto the EKS cluster created in main.tf.

data "aws_eks_cluster_auth" "this" {
  name = aws_eks_cluster.this.name
}

provider "kubernetes" {
  host                   = aws_eks_cluster.this.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.this.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.this.token
}

provider "helm" {
  kubernetes {
    host                   = aws_eks_cluster.this.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.this.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.this.token
  }
}

resource "kubernetes_namespace" "plinth" {
  metadata {
    name = var.plinth_namespace

    labels = {
      "app.kubernetes.io/part-of"          = "plinth"
      "pod-security.kubernetes.io/enforce" = "restricted"
      "pod-security.kubernetes.io/audit"   = "restricted"
      "pod-security.kubernetes.io/warn"    = "restricted"
    }
  }
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "oauth_encryption" {
  length  = 32
  special = false
}

# Plinth secret. In a real environment, source from AWS Secrets Manager via
# External Secrets Operator and set existingSecret on the chart instead.
resource "kubernetes_secret" "plinth" {
  metadata {
    name      = "plinth-secrets"
    namespace = kubernetes_namespace.plinth.metadata[0].name
  }

  data = {
    "jwt-secret"           = random_password.jwt.result
    "oauth-encryption-key" = random_password.oauth_encryption.result
    "postgres-url"         = "postgresql+psycopg://${var.rds_username}:${random_password.rds.result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${aws_db_instance.this.db_name}"
  }

  type = "Opaque"
}

resource "helm_release" "plinth" {
  name      = "plinth"
  namespace = kubernetes_namespace.plinth.metadata[0].name
  chart     = var.plinth_chart_path

  values = [for path in var.plinth_values_files : file(path)]

  set {
    name  = "existingSecret"
    value = kubernetes_secret.plinth.metadata[0].name
  }

  set {
    name  = "global.imageTag"
    value = var.plinth_image_tag
  }

  set {
    name  = "workspace.postgresUrl"
    value = "postgresql+psycopg://${var.rds_username}:${random_password.rds.result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${aws_db_instance.this.db_name}"
  }

  set {
    name  = "global.serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.plinth_irsa.arn
  }

  set {
    name  = "postgres.enabled"
    value = "false"
  }

  depends_on = [aws_eks_node_group.this, aws_db_instance.this]
}
