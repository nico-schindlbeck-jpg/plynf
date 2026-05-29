// SPDX-License-Identifier: Apache-2.0
/**
 * Invocable action so Salesforce Flow Builder, Agentforce, and Einstein
 * Copilot Topics can call Plynf without writing Apex.
 *
 * In Flow Builder this shows up as the action
 * "Plynf — Shape Tool Response".
 *
 * In Agentforce, register this as a Custom Action — the agent's planner
 * picks the right inputs from the conversation context.
 */
public with sharing class PlynfShapeToolAction {

    @InvocableMethod(
        label='Plynf — Shape Tool Response'
        description='Fetch a Plynf-managed tool response (Salesforce, Order DB, …) and return the shaped JSON, with token-savings metadata.'
        category='Plynf')
    public static List<Output> invoke(List<Input> inputs) {
        List<Output> results = new List<Output>();
        for (Input in : inputs) {
            Output out = new Output();
            try {
                Map<String, Object> args = parseArgsJson(in.argumentsJson);
                Map<String, Object> resp = PlynfClient.invokeTool(
                    in.tool,
                    args
                );
                out.resultJson = JSON.serialize(resp.get('result'));
                out.connector = (String) resp.get('connector');
                out.cacheHit = (Boolean) resp.get('cache_hit');
                Map<String, Object> savings = (Map<String, Object>) resp.get('savings');
                if (savings != null) {
                    out.rawResponseTokens = toInt(savings.get('raw_response_tokens'));
                    out.shapedResponseTokens = toInt(savings.get('shaped_response_tokens'));
                    out.savedTokens = toInt(savings.get('saved_tokens'));
                    out.savingsPct = (Decimal) savings.get('savings_pct');
                }
                out.success = true;
            } catch (Exception e) {
                out.success = false;
                out.errorMessage = e.getMessage();
            }
            results.add(out);
        }
        return results;
    }

    private static Map<String, Object> parseArgsJson(String raw) {
        if (String.isBlank(raw)) return new Map<String, Object>();
        try {
            Object parsed = JSON.deserializeUntyped(raw);
            if (!(parsed instanceof Map<String, Object>)) {
                throw new PlynfClient.PlynfHttpException(
                    'argumentsJson must deserialise to an object, got: ' + parsed
                );
            }
            return (Map<String, Object>) parsed;
        } catch (Exception e) {
            throw new PlynfClient.PlynfHttpException(
                'argumentsJson is not valid JSON: ' + e.getMessage()
            );
        }
    }

    private static Integer toInt(Object o) {
        if (o == null) return 0;
        if (o instanceof Integer) return (Integer) o;
        if (o instanceof Decimal) return ((Decimal) o).intValue();
        if (o instanceof Long) return ((Long) o).intValue();
        return Integer.valueOf(String.valueOf(o));
    }

    public class Input {
        @InvocableVariable(
            label='Tool'
            description='Which Plynf-managed tool to invoke (get_order, get_lead, get_account, …).'
            required=true)
        public String tool;

        @InvocableVariable(
            label='Arguments (JSON)'
            description='JSON-encoded arguments. Example: {"order_id":"12345"}'
            required=false)
        public String argumentsJson;
    }

    public class Output {
        @InvocableVariable(label='Success') public Boolean success;
        @InvocableVariable(label='Error Message') public String errorMessage;
        @InvocableVariable(label='Connector') public String connector;
        @InvocableVariable(label='Shaped Result (JSON string)') public String resultJson;
        @InvocableVariable(label='Cache Hit') public Boolean cacheHit;
        @InvocableVariable(label='Raw Response Tokens') public Integer rawResponseTokens;
        @InvocableVariable(label='Shaped Response Tokens') public Integer shapedResponseTokens;
        @InvocableVariable(label='Saved Tokens') public Integer savedTokens;
        @InvocableVariable(label='Savings %') public Decimal savingsPct;
    }
}
