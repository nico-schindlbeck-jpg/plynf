// SPDX-License-Identifier: Apache-2.0
/**
 * Thin HTTP client around the Plynf proxy's /v1/tools/{tool}/invoke
 * endpoint. Returns the shaped JSON as a Map; callers turn it into
 * whatever Apex-typed structure they need.
 *
 * Designed to be safe to call from synchronous trigger context — uses
 * the Apex Http class with a short timeout, no @future required.
 */
public with sharing class PlynfClient {
    private static final Integer DEFAULT_TIMEOUT_MS = 30000;

    public class PlynfHttpException extends Exception {}

    /**
     * Invoke a Plynf-managed tool. Throws PlynfHttpException on non-2xx.
     *
     * @param tool   The tool slug (e.g. "get_order", "get_lead")
     * @param args   Arbitrary Apex Map<String, Object> serialised to JSON
     * @return       The shaped tool result as a Map<String, Object>
     */
    public static Map<String, Object> invokeTool(String tool, Map<String, Object> args) {
        PlynfConfig cfg = PlynfConfig.load();
        return invokeTool(cfg, tool, args, null, null);
    }

    public static Map<String, Object> invokeTool(
        PlynfConfig cfg,
        String tool,
        Map<String, Object> args,
        String agentId,
        String workflowId
    ) {
        Map<String, Object> body = new Map<String, Object>{
            'arguments' => (args == null ? new Map<String, Object>() : args)
        };
        if (String.isNotBlank(agentId)) body.put('agent_id', agentId);
        if (String.isNotBlank(workflowId)) body.put('workflow_id', workflowId);

        HttpRequest req = new HttpRequest();
        req.setEndpoint(
            cfg.endpoint.removeEnd('/') + '/v1/tools/' + EncodingUtil.urlEncode(tool, 'UTF-8') + '/invoke'
        );
        req.setMethod('POST');
        req.setHeader('Content-Type', 'application/json');
        req.setHeader('Authorization', 'Bearer ' + cfg.apiKey);
        req.setTimeout(DEFAULT_TIMEOUT_MS);
        req.setBody(JSON.serialize(body));

        Http http = new Http();
        HttpResponse resp = http.send(req);
        Integer status = resp.getStatusCode();
        if (status < 200 || status >= 300) {
            throw new PlynfHttpException(
                'Plynf returned ' + status + ': ' + resp.getBody().abbreviate(500)
            );
        }
        return (Map<String, Object>) JSON.deserializeUntyped(resp.getBody());
    }
}
