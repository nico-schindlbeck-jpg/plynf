# make-plynf

Plynf — Agent Context Optimization Layer for [Make.com](https://make.com).

Drop the Plynf module into any Make scenario that touches CRM / ERP /
Slack data before an AI step. The module fetches the tool response,
runs it through your Plynf shaping policy, and gives the next step a
much smaller JSON to work with — typically 40–80% fewer tokens.

## Package layout

```
make-plynf/
├── app.json                       # app-level metadata
├── base.json                      # shared HTTP defaults
├── connections/
│   └── api-key/
│       └── connection.json        # custom auth: URL + API key
└── modules/
    └── shape-tool-response/
        ├── module.json            # module metadata
        ├── communication.json     # request → /v1/tools/<tool>/invoke
        ├── parameters.json        # UI inputs (tool dropdown + args)
        └── interface.json         # output bundle definition
```

## Publish to Make's app store

1. Open https://www.make.com/en/developers and create a new private app.
2. Upload each JSON file under its corresponding tab
   (App / Base / Connections / Modules).
3. Submit for "Verification" — Make's team reviews in 1–3 weeks.
4. After approval the app becomes searchable as "Plynf" in any user's
   scenario builder.

Until then, the app is usable by anyone you invite with the private
share link.

## Tier behaviour

Same as the Zapier app: the Plynf proxy enforces the tier gate, and
Make surfaces the proxy's HTTP 402 + upgrade hint as a scenario error.
Toggle the module's *Continue on error* to keep the scenario running
when free-tier limits are hit.
