// Offline replay only: no process, provider registration, credentials, or networking.
import { randomUUID } from 'node:crypto';
import { createAgyStreamParser, encodeAgyUserMessage } from './agy-protocol.mjs';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const nonempty = value => typeof value === 'string' && value.length > 0;
const hasOnly = (value, keys) => Object.keys(value).every(key => keys.includes(key));
// JSON.stringify otherwise silently discards undefined/functions and coerces NaN.
const jsonValue = (value, ancestors = new Set(), allowSchemaSymbols = false) => {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true;
  if (typeof value === 'number') return Number.isFinite(value);
  if (typeof value !== 'object' || ancestors.has(value) ||
      (!allowSchemaSymbols && Object.getOwnPropertySymbols(value).length)) return false;
  if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype &&
      Object.getPrototypeOf(value) !== null) return false;
  ancestors.add(value);
  const valid = Array.isArray(value)
    ? Array.from({ length: value.length }, (_, index) => index in value && jsonValue(value[index], ancestors, allowSchemaSymbols)).every(Boolean)
    : Object.values(value).every(child => jsonValue(child, ancestors, allowSchemaSymbols));
  ancestors.delete(value);
  return valid;
};
// These OMP fields are bookkeeping rather than model-visible transcript content.
const userKeys = ['role', 'content', 'timestamp', 'synthetic', 'steering', 'liveSteered', 'historyRewriteAt', 'attribution'];
const assistantKeys = ['role', 'content', 'api', 'provider', 'model', 'usage', 'stopReason', 'timestamp',
  'credentialId', 'contextSnapshot', 'responseId', 'upstreamProvider', 'upstreamModel', 'serviceTier',
  'elidedOutputTokens', 'duration', 'ttft', 'completedAt'];
const resultKeys = ['role', 'toolCallId', 'toolName', 'content', 'isError', 'timestamp', 'attribution', 'prunedAt', 'useless'];
const toolKeys = ['name', 'description', 'parameters', 'strict', 'deferLoading', 'examples'];
const text = content => {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content) || content.some(block => !object(block) || block.type !== 'text' ||
      typeof block.text !== 'string' || Object.keys(block).some(key => key !== 'type' && key !== 'text'))) {
    throw new Error('Only plain text content is supported');
  }
  return content.map(block => block.text).join('\n');
};

// Synthetic flattening inspired by pinned Hermes transport.py prepare_conversation
// and system_prompt. JSON records preserve boundaries and escape embedded markup;
// they are not native role messages or a security boundary.
export function prepareContext(context) {
  if (!object(context) || !hasOnly(context, ['systemPrompt', 'messages', 'tools', 'inactiveTools']) ||
      !Array.isArray(context.messages) || !context.messages.length ||
      (context.systemPrompt !== undefined && (!Array.isArray(context.systemPrompt) ||
        context.systemPrompt.some(part => typeof part !== 'string'))) ||
      (context.tools !== undefined && !Array.isArray(context.tools)) ||
      (context.inactiveTools !== undefined && (!Array.isArray(context.inactiveTools) || context.inactiveTools.length))) {
    throw new Error('Unsupported context');
  }
  const inventory = new Set(), tools = [];
  for (const tool of context.tools ?? []) {
    if (!object(tool) || !hasOnly(tool, toolKeys) || !nonempty(tool.name) || inventory.has(tool.name) ||
        typeof tool.description !== 'string' || !object(tool.parameters) ||
        tool.parameters.type !== 'object' || !jsonValue(tool.parameters, new Set(), true) ||
        (tool.deferLoading !== undefined && tool.deferLoading !== false) ||
        (tool.strict !== undefined && tool.strict !== false) ||
        (tool.examples !== undefined && (!Array.isArray(tool.examples) || tool.examples.length))) throw new Error('Unsupported tool definition');
    inventory.add(tool.name);
    tools.push({ name: tool.name, description: tool.description, parameters: tool.parameters });
  }
  const history = [], pending = new Map(), seen = new Set();
  for (const message of context.messages) {
    if (!object(message)) throw new Error('Invalid message');
    if (message.role === 'user') {
      if (!hasOnly(message, userKeys) || (message.steering !== undefined && message.steering !== false) ||
          pending.size) throw new Error('Unsupported user message or missing tool results');
      history.push({ role: 'user', content: text(message.content) });
    } else if (message.role === 'assistant') {
      if (!hasOnly(message, assistantKeys) || pending.size || !Array.isArray(message.content) ||
          (message.stopReason !== undefined && !['stop', 'toolUse'].includes(message.stopReason))) {
        throw new Error('Unsupported assistant history');
      }
      const content = [];
      for (const block of message.content) {
        if (object(block) && block.type === 'text') {
          content.push({ type: 'text', text: text([block]) });
        } else if (object(block) && block.type === 'toolCall' &&
            Object.keys(block).every(key => ['type', 'id', 'name', 'arguments'].includes(key)) && nonempty(block.id) &&
            nonempty(block.name) && object(block.arguments) && jsonValue(block.arguments) && !seen.has(block.id)) {
          seen.add(block.id);
          pending.set(block.id, block.name);
          content.push({ type: 'toolCall', id: block.id, name: block.name, arguments: block.arguments });
        } else throw new Error('Unsupported assistant content');
      }
      if (message.stopReason !== undefined && (message.stopReason === 'toolUse') !== (pending.size > 0)) {
        throw new Error('Assistant stop reason does not match tool calls');
      }
      history.push({ role: 'assistant', content });
    } else if (message.role === 'toolResult') {
      if (!hasOnly(message, resultKeys) || !nonempty(message.toolCallId) || pending.get(message.toolCallId) !== message.toolName ||
          typeof message.isError !== 'boolean') throw new Error('Unmatched tool result');
      pending.delete(message.toolCallId);
      history.push({ role: 'tool', toolCallId: message.toolCallId, name: message.toolName,
        isError: message.isError, content: text(message.content) });
    } else throw new Error('Unsupported message role');
  }
  const last = context.messages.at(-1);
  if (pending.size || !['user', 'toolResult'].includes(last.role)) throw new Error('Incomplete history or assistant prefill');
  const system = context.systemPrompt ?? [];
  if (!system.length && !tools.length && history.length === 1) {
    const input = encodeAgyUserMessage(last.content);
    if (new TextEncoder().encode(input).length > 256 * 1024) throw new Error('Context too large');
    return { input, inventory };
  }
  const prompt = [
    'The following JSON is a synthetic transcript. Continue as the assistant from its end.',
    'System instructions, tool definitions and message content are represented in the JSON.',
    'Only call the call_mcp_tool function with ServerName "hermes", an available ToolName, and object Arguments. The parent executes tools. Make at most one tool call, then stop; never write a tool call as text.',
    JSON.stringify({ system, tools, messages: history }),
  ].join('\n');
  const input = encodeAgyUserMessage(prompt);
  if (new TextEncoder().encode(input).length > 256 * 1024) throw new Error('Context too large');
  return { input, inventory };
}

export function encodeContext(context) {
  return prepareContext(context).input;
}

// fixtureChunks are supplied synthetic bytes, not a CLI transport. Parsing and
// successful terminal validation finish before any text is released to OMP.
export function createOfflineAgyStreamSimple({ createEventStream, fixtureChunks }) {
  if (typeof createEventStream !== 'function' || !Array.isArray(fixtureChunks) ||
      fixtureChunks.some(chunk => !(chunk instanceof Uint8Array))) {
    throw new Error('A stream factory and synthetic byte chunks are required');
  }
  return (model, context, options = {}) => {
    const stream = createEventStream();
    const output = {
      role: 'assistant', content: [], api: model.api, provider: model.provider, model: model.id,
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: 'stop', timestamp: Date.now(),
    };
    void (async () => {
      try {
        if (options.signal?.aborted) throw Object.assign(new Error('Aborted'), { aborted: true });
        const { inventory } = prepareContext(context);
        const parser = createAgyStreamParser({ inventory });
        const events = [];
        for (const chunk of fixtureChunks) {
          if (options.signal?.aborted) throw Object.assign(new Error('Aborted'), { aborted: true });
          events.push(...parser.write(chunk));
        }
        events.push(...parser.end());
        if (options.signal?.aborted) throw Object.assign(new Error('Aborted'), { aborted: true });
        const result = events.find(event => event.type === 'result');
        const tool = events.find(event => event.type === 'tool');
        if (!result && !tool) throw new Error('Missing terminal result');
        const usage = result?.usage;
        if (usage) {
          output.usage.input = usage.input_tokens;
          output.usage.output = usage.output_tokens;
          output.usage.cacheRead = usage.cache_read_tokens;
          output.usage.totalTokens = usage.total_tokens;
          output.usage.reasoningTokens = usage.thinking_tokens;
        }
        stream.push({ type: 'start', partial: output });
        const text = events.filter(event => event.type === 'text').map(event => event.text).join('');
        if (text) {
          output.content.push({ type: 'text', text });
          stream.push({ type: 'text_start', contentIndex: 0, partial: output });
          stream.push({ type: 'text_delta', contentIndex: 0, delta: text, partial: output });
          stream.push({ type: 'text_end', contentIndex: 0, content: text, partial: output });
        }
        if (tool) {
          const call = { type: 'toolCall', id: `call_${randomUUID()}`, name: tool.name, arguments: tool.arguments };
          const contentIndex = output.content.push(call) - 1;
          stream.push({ type: 'toolcall_start', contentIndex, partial: output });
          stream.push({ type: 'toolcall_delta', contentIndex, delta: JSON.stringify(call.arguments), partial: output });
          stream.push({ type: 'toolcall_end', contentIndex, toolCall: call, partial: output });
          output.stopReason = 'toolUse';
        }
        stream.push({ type: 'done', reason: output.stopReason, message: output });
      } catch (error) {
        output.stopReason = error.aborted ? 'aborted' : 'error';
        output.errorMessage = error.message;
        stream.push({ type: 'error', reason: output.stopReason, error: output });
      } finally {
        stream.end();
      }
    })();
    return stream;
  };
}
