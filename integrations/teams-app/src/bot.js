// SPDX-License-Identifier: Apache-2.0
// Plynf Teams bot — TeamsActivityHandler implementation.

import { TeamsActivityHandler, MessageFactory } from 'botbuilder';
import { shapeTool, chat, isKnownTool, listTools, PlynfError } from './plynf-client.js';

const HEURISTICS = [
  { keywords: ['order', 'bestellung', 'shipment'], tool: 'get_order', extract: (t) => {
      const m = t.match(/#?(\d{4,})/); return m ? { order_id: m[1] } : null;
    }
  },
  { keywords: ['lead'], tool: 'get_lead', extract: (t) => {
      const m = t.match(/(00Q[A-Z0-9]{12,})/i); return m ? { id: m[1] } : null;
    }
  },
  { keywords: ['account', 'kunde'], tool: 'get_account', extract: (t) => {
      const m = t.match(/(001[A-Z0-9]{12,})/i); return m ? { id: m[1] } : null;
    }
  },
];

export class PlynfBot extends TeamsActivityHandler {
  constructor(cfg) {
    super();
    this.cfg = cfg;

    this.onMessage(async (context, next) => {
      const text = (context.activity.text || '').replace(/<at>[^<]+<\/at>/g, '').trim();

      if (!text || /^help/i.test(text)) {
        await context.sendActivity(MessageFactory.text(this._helpText()));
        await next();
        return;
      }

      // Power-user: "fetch get_order {json}"
      const fetchMatch = text.match(/^fetch\s+(\w+)\s*(\{.*\})?$/i);
      if (fetchMatch) {
        const tool = fetchMatch[1];
        const argsRaw = fetchMatch[2] ?? '{}';
        if (!isKnownTool(tool)) {
          await context.sendActivity(`Unknown tool \`${tool}\`. Known: ${listTools().join(', ')}`);
          await next();
          return;
        }
        let args;
        try { args = JSON.parse(argsRaw); } catch (e) {
          await context.sendActivity(`Could not parse JSON args: ${e.message}`);
          await next();
          return;
        }
        await this._invoke(context, tool, args, text);
        await next();
        return;
      }

      // Natural-language heuristic.
      const picked = this._pickTool(text);
      if (!picked) {
        await context.sendActivity(this._helpText());
        await next();
        return;
      }
      await this._invoke(context, picked.tool, picked.args, text);
      await next();
    });
  }

  _pickTool(text) {
    const lower = text.toLowerCase();
    for (const h of HEURISTICS) {
      if (h.keywords.some((k) => lower.includes(k))) {
        const args = h.extract(text);
        if (args !== null) return { tool: h.tool, args };
      }
    }
    return null;
  }

  _helpText() {
    return [
      "Hi! I fetch business-system data through Plynf and shape it down to the fields your AI agents actually need.",
      "",
      "**Try:**",
      "- `What's the status of order #12345?`",
      "- `fetch get_lead {\"id\":\"00Q…\"}`",
      "- `help`",
      "",
      `Known tools: ${listTools().join(', ')}`,
    ].join('\n');
  }

  async _invoke(context, tool, args, userText) {
    try {
      const shaped = await shapeTool({
        baseUrl: this.cfg.plynfUrl,
        apiKey: this.cfg.plynfApiKey,
        tool,
        args,
        agentId: `teams:${context.activity.from?.id || 'unknown'}`,
        workflowId: `teams:${context.activity.conversation?.id || 'unknown'}`,
      });

      let reply;
      if (this.cfg.plynfModel) {
        reply = await chat({
          baseUrl: this.cfg.plynfUrl,
          apiKey: this.cfg.plynfApiKey,
          model: this.cfg.plynfModel,
          systemPrompt:
            'You are a Microsoft Teams assistant. Use ONLY the shaped tool response to ' +
            'answer the user. One short paragraph, friendly tone.',
          userPrompt: userText,
          toolJson: shaped.result,
        });
      } else {
        reply = this._renderCompact(tool, shaped);
      }

      await context.sendActivity(MessageFactory.text(reply));
    } catch (err) {
      if (err instanceof PlynfError && err.status === 402) {
        await context.sendActivity(`⚠️ Plynf tier limit reached. ${err.detail}`);
      } else {
        await context.sendActivity(`❌ Plynf request failed: ${err.message}`);
      }
    }
  }

  _renderCompact(tool, shaped) {
    const r = shaped.result ?? {};
    const savings = shaped.savings ?? {};
    const pct = typeof savings.savings_pct === 'number'
      ? (savings.savings_pct * 100).toFixed(0) + '%'
      : '?';
    const head = `**${tool}** · saved ${pct} tokens`;
    const lines = Object.entries(r)
      .slice(0, 10)
      .map(([k, v]) => `- **${k}**: ${this._formatValue(v)}`);
    return `${head}\n${lines.join('\n')}`;
  }

  _formatValue(v) {
    if (v == null) return '_null_';
    if (typeof v === 'string') return v.length > 200 ? v.slice(0, 200) + '…' : v;
    if (typeof v === 'object') return '`' + JSON.stringify(v).slice(0, 200) + '`';
    return String(v);
  }
}
