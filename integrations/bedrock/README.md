# Plynf → AWS Bedrock Agents

One-click bridge: deploy a Lambda + IAM role that forwards Bedrock Agent
action-group calls to a Plynf proxy. Bedrock gets shaped tool responses;
your Plynf savings dashboard shows the tokens you saved.

## Deploy

```bash
# 1. Store your Plynf API key.
aws secretsmanager create-secret \
  --name plynf/api-key \
  --secret-string "pl-..."

SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id plynf/api-key --query ARN --output text)

# 2. Deploy the stack.
aws cloudformation deploy \
  --stack-name plynf-bedrock-bridge \
  --template-file cloudformation/plynf-bedrock-actiongroup.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    PlynfApiUrl="https://app.plynf.com" \
    PlynfApiKeySecretArn="$SECRET_ARN"

# 3. Note the bridge Lambda ARN.
aws cloudformation describe-stacks \
  --stack-name plynf-bedrock-bridge \
  --query "Stacks[0].Outputs[?OutputKey=='LambdaArn'].OutputValue" \
  --output text
```

## Attach to a Bedrock Agent

In the Bedrock console (or via `aws bedrock-agent`), create an Action Group:

- **Action group name:** `plynf-tools`
- **Action group invocation:** Lambda (paste the `LambdaArn` from above)
- **Action group schema:** upload `cloudformation/plynf-actiongroup-schema.yaml`

When your Bedrock Agent decides to call `getOrder`, it invokes the bridge
Lambda, which calls `POST /v1/tools/get_order/invoke` on Plynf. Plynf
shapes the response per your tenant policy and returns the slim JSON to
Bedrock, which feeds it back to the model.

## Self-hosted Plynf

Point `PlynfApiUrl` at your internal load balancer (VPC endpoint or
PrivateLink). The Lambda needs egress to that DNS — either deploy it in
the same VPC or use a NAT gateway.

## Costs

The bridge Lambda is one short-lived call per tool invocation; on
Lambda's free tier you can run thousands of agent calls per month for
cents. Plynf savings on the actual tool responses are typically 40–80%
of the Bedrock token bill — the bridge pays for itself within hours of
real traffic.

## Limitations

- The bridge is a thin proxy. It does NOT cache locally; caching happens
  inside Plynf.
- Bedrock's `actionGroupExecutor` envelope (HTTP method, statusCode,
  responseBody) is preserved; if your tool returns binary content, you'll
  need to base64-encode it before responding.
- Only the tools listed in `plynf-actiongroup-schema.yaml` are wired. Add
  new entries when Plynf gains new connectors.
