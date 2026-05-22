# Plynf on AWS — example Terraform module

This module stands up an opinionated Plynf environment on AWS:

- VPC across 3 AZs with public + private subnets and NAT gateways
- EKS cluster (configurable Kubernetes version, default 1.30)
- Managed node group (3x t3.medium by default)
- RDS Postgres (db.t3.medium, 50 GB, encrypted at rest)
- S3 bucket for future blob storage (versioned, encrypted, public access blocked)
- IRSA role for the `plinth` ServiceAccount with read/write on the blob bucket
- A `helm_release` installing the Plynf chart from `../../helm/plinth`

> **This is a STARTING POINT, not a turnkey production module.** Before
> applying anywhere serious, read `main.tf` end-to-end and tighten every
> default flagged in the inline comments. Notable items:
>
> - RDS is single-AZ. Set `multi_az = true` and enable PIT recovery.
> - RDS `deletion_protection = false`. Flip to `true`.
> - EKS endpoint is public. Restrict via `endpoint_public_access_cidrs` or
>   move it private and use a bastion / SSO.
> - Secrets are generated via `random_password` and stored in plain Kubernetes
>   Secrets. Use SealedSecrets / External Secrets Operator + AWS Secrets
>   Manager in real life.
> - The chart Helm release runs against an EKS cluster that may not be ready
>   on first apply; if Helm fails, `terraform apply` again.

## Prerequisites

```bash
# AWS CLI configured + credentials available
aws sts get-caller-identity

# Terraform 1.6+
terraform version

# kubectl + helm for post-apply verification
kubectl version --client
helm version
```

## Usage

```bash
cd deploy/terraform/aws-example
terraform init
terraform plan -out plinth.tfplan
terraform apply plinth.tfplan

# Populate kubeconfig
$(terraform output -raw kubeconfig_command)

# Verify the cluster
kubectl -n plinth get pods,svc
```

## Inputs

See `variables.tf` for the full list. The most-tweaked values:

| Variable | Default | Purpose |
| --- | --- | --- |
| `aws_region` | `us-east-1` | AWS region. |
| `name_prefix` | `plinth` | Prefix on every resource name. |
| `vpc_cidr` | `10.30.0.0/16` | VPC CIDR. |
| `kubernetes_version` | `1.30` | EKS Kubernetes minor. |
| `node_desired_size` | `3` | Worker node count. |
| `node_instance_types` | `["t3.medium"]` | Node EC2 types. |
| `rds_instance_class` | `db.t3.medium` | RDS Postgres size. |
| `rds_allocated_storage_gb` | `50` | RDS storage. |
| `plinth_image_tag` | `1.0.0` | Plynf container image tag. |
| `plinth_values_files` | `[values.yaml, values.prod.yaml]` | Helm values to layer. |

## Outputs

| Output | Description |
| --- | --- |
| `cluster_name` | EKS cluster name. |
| `cluster_endpoint` | EKS API endpoint. |
| `kubeconfig_command` | One-liner to update `~/.kube/config`. |
| `rds_endpoint` | RDS Postgres `host:port`. |
| `blobs_bucket_name` | S3 bucket reserved for future blob storage. |
| `plinth_irsa_role_arn` | IAM role bound to the Plynf ServiceAccount. |
| `plinth_namespace` | Kubernetes namespace. |

## Destroy

```bash
terraform destroy
```

The S3 bucket has `force_destroy = false`. Empty it manually first if you've
written objects to it.

## See also

- `deploy/helm/plinth/` — the chart this module installs
- `docs/deployment.md` — operator handbook (sizing, secrets, backups)
