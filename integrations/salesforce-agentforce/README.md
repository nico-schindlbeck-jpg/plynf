# Plynf · Salesforce Agentforce / Flow / Apex

SFDX package that exposes Plynf to Salesforce. After deploy you get:

- An **Invocable Action** `Plynf — Shape Tool Response` that's selectable
  from Flow Builder, Einstein Copilot Topics, and **Agentforce Custom
  Actions**.
- A reusable Apex client (`PlynfClient`) for direct callouts from
  triggers, batch jobs, or other Apex.
- A **Custom Metadata Type** (`Plynf_Config__mdt`) so the endpoint and
  API key live in Setup — not in code — and can be promoted across
  sandboxes without redeploy.
- A **Remote Site Setting** authorising callouts to `app.plynf.com`.
- Apex unit tests with `HttpCalloutMock` — runs offline, no real org or
  network needed for the `--test-level RunSpecifiedTests` deploy.

## Deploy

### Prerequisites

- Salesforce CLI (`sf` / `sfdx`) installed
- Authenticated against the target org:
  ```sh
  sf org login web -a my-org
  ```

### Deploy the package

```sh
cd integrations/salesforce-agentforce
sf project deploy start --target-org my-org
```

### Set your Plynf API key

The deploy includes a default `Plynf_Config__mdt.Default` record whose
`API_Key__c` reads `REPLACE_WITH_YOUR_PLYNF_API_KEY`. Two ways to set
the real value:

**Option A — Setup UI** (one-off, easiest):
1. Setup → **Custom Metadata Types** → **Plynf Config** → **Manage Records**
2. Edit **Default** → paste the API key into **API Key**
3. Save

**Option B — Metadata API** (CI-friendly):
1. Edit `force-app/main/default/customMetadata/Plynf_Config.Default.md-meta.xml`
2. Replace the placeholder
3. Redeploy

### Run the Apex tests

```sh
sf apex run test --target-org my-org \
  --test-level RunSpecifiedTests \
  --tests PlynfClientTest \
  --code-coverage --result-format human
```

You should see 3 passing tests with ≥75 % code coverage on `PlynfClient`
and `PlynfShapeToolAction`.

## Use it

### From Flow Builder

1. New Flow → drop an **Action** element
2. Search **"Plynf"** → pick **Plynf — Shape Tool Response**
3. Fill the inputs:
   - **Tool:** `get_order` (or any other registered tool)
   - **Arguments (JSON):** `{"order_id":"{!varOrderId}"}` (use Flow vars
     in the merge syntax)
4. Use the outputs in downstream elements:
   - `{!shapeOutput.resultJson}` — shaped tool response, parse with
     `parseJSON` or a follow-up Apex step
   - `{!shapeOutput.savingsPct}` — number 0–1, e.g. for branching logic
   - `{!shapeOutput.success}` / `{!shapeOutput.errorMessage}` — error
     handling

### From Agentforce

1. Setup → **Agentforce Studio** → your agent → **Topics → Custom Actions**
2. Add Action → **Apex** → pick `PlynfShapeToolAction`
3. Define the planner inputs (the agent's LLM fills them from the
   conversation context)
4. Plug the action into a topic, e.g. *"customer asks about an order"*
5. The agent now calls Plynf when it decides it needs data — and the
   response is already shaped to the relevant fields, saving downstream
   LLM cost

### From Apex (triggers, batch, schedulable)

```apex
Map<String, Object> resp = PlynfClient.invokeTool(
    'get_order',
    new Map<String, Object>{ 'order_id' => '12345' }
);
Map<String, Object> result = (Map<String, Object>) resp.get('result');
System.debug('Shipped via: ' + result.get('carrier'));
```

## Layout

```
salesforce-agentforce/
├── sfdx-project.json
└── force-app/main/default/
    ├── classes/
    │   ├── PlynfConfig.cls            # CMD loader
    │   ├── PlynfClient.cls            # HTTP client
    │   ├── PlynfShapeToolAction.cls   # @InvocableMethod for Flow / Agentforce
    │   └── PlynfClientTest.cls        # Apex tests (HttpCalloutMock)
    ├── objects/Plynf_Config__mdt/     # Custom metadata type definition
    │   ├── Plynf_Config__mdt.object-meta.xml
    │   └── fields/
    │       ├── Endpoint__c.field-meta.xml
    │       ├── API_Key__c.field-meta.xml
    │       └── Default_Model__c.field-meta.xml
    ├── customMetadata/
    │   └── Plynf_Config.Default.md-meta.xml   # the actual default config row
    └── remoteSiteSettings/
        └── Plynf_Endpoint.remoteSite-meta.xml # https://app.plynf.com
```

## Going to AppExchange

This package compiles to a 2GP managed package; the path to listing on
AppExchange goes through Salesforce's Security Review (4–8 weeks for a
first-time submission). Until then, deploy directly into customer orgs
via `sf project deploy start` — same code, just no marketplace listing.

## License

Apache-2.0
