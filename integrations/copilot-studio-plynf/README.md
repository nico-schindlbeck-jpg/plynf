# Plynf · Microsoft Copilot Studio connector

A Power Platform custom connector that lets Microsoft Copilot Studio
agents call Plynf-managed tools and receive shaped JSON responses
(40–80% fewer tokens than the raw CRM/ERP/Slack data).

## Files

- `plynf-connector-swagger.yaml` — OpenAPI 2.0 spec with Power Platform
  extensions (`x-ms-visibility`, `x-ms-summary`, securityDefinitions for
  API key). One operation per Plynf tool slot, one for tier introspection.
- `plynf-connector-properties.json` — Power Platform connector
  configuration: connection parameters, brand colour, the policy that
  injects `Authorization: Bearer <api_key>` on every request.

## Deploy

### Option A — Power Platform CLI (recommended)

```bash
# Install the CLI once.
dotnet tool install -g Microsoft.PowerApps.CLI.Tool

# Authenticate against your Power Platform environment.
pac auth create --environment <env-id>

# Push the connector.
pac connector create \
  --api-definition-file plynf-connector-swagger.yaml \
  --api-properties-file  plynf-connector-properties.json \
  --display-name "Plynf"
```

### Option B — Power Apps maker portal

1. https://make.powerapps.com → *Custom connectors* → *New custom connector* → *Import an OpenAPI file*
2. Upload `plynf-connector-swagger.yaml`.
3. On the *Security* tab pick **API Key** → header name `Authorization`,
   parameter label "Plynf API Key", location *Header*.
4. Test with your key — confirm `GetTier` returns 200.
5. Save.

## Use inside Copilot Studio

1. Open your copilot → *Tools* (left rail) → *Add a tool* → *Custom connector*.
2. Pick **Plynf** → **ShapeToolResponse**.
3. Map the tool input (e.g. `tool = "get_order"`, `arguments = { order_id: System.User.Properties.OrderId }`).
4. The copilot now receives the shaped JSON in the topic variables —
   typically a slim object with 8–10 fields instead of 200.

## Tier behaviour

Plynf's tier-gate runs server-side. Free-tier callers hitting the
100k-token monthly cap get HTTP 402 with an `upgrade_hint` body;
Copilot Studio surfaces this as a connector error in the test log. The
swagger marks the 402 response so users can route to a different topic
("Tier limit reached") if they want a graceful fallback.

## Going to AppSource (Microsoft commercial marketplace)

Once your tenant has run pilot scenarios, submit the connector for
Microsoft certification at
https://aka.ms/ConnectorCertification. The review takes 4–8 weeks and
unlocks the connector for every Power Platform / Copilot Studio tenant
globally — same path Salesforce, Stripe, and SAP take.
