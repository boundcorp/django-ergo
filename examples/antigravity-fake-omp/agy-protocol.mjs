// Offline, single-turn stream-json subset; this does not start a process or ensure
// tool isolation. Fixtures are synthetic, not recordings of an agy invocation.
// Official, unversioned documentation: https://antigravity.google/docs/cli/headless.md
// Hermes transport at immutable revision ec07f86e6d918803ac5e328a4780d0ec0f48d0b8:
// https://github.com/Realtyxxx/hermes-plugin-antigravity-agy/blob/ec07f86e6d918803ac5e328a4780d0ec0f48d0b8/transport.py#L850-L918
// Hermes detects tool steps after they occur; this parser is not a safety boundary.

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const nonempty = value => typeof value === 'string' && value.length > 0;

const usageFields = ['input_tokens', 'output_tokens', 'thinking_tokens', 'cache_read_tokens', 'total_tokens'];
const validUsage = usage => object(usage) && usageFields.every(key => Number.isSafeInteger(usage[key]) && usage[key] >= 0);
// Recognizing an observed event cannot prevent an action that already occurred.
// Separate from the text-only parser; this never executes or authorizes a call.
export function recognizeHermesToolCall(event, { conversationId, inventory } = {}) {
  if (!nonempty(conversationId) || !(inventory instanceof Set) ||
      [...inventory].some(name => !nonempty(name))) throw new Error('Invalid inventory or conversation');
  const step = event?.step_update;
  const info = step?.tool_info;
  const params = info?.parameters;
  if (!object(event) || event.event !== 'step_update' || !object(step) ||
      step.conversation_id !== conversationId || step.step_type !== 'tool' || step.state !== 'ACTIVE' ||
      !Number.isSafeInteger(step.step_index) || step.step_index < 0 ||
      !object(info) || 'subagent_info' in step || (step.tool_name ?? info.name) !== 'call_mcp_tool' ||
      (info.name !== undefined && info.name !== 'call_mcp_tool') ||
      !object(params) || params.ServerName !== 'hermes' || !inventory.has(params.ToolName)) {
    throw new Error('Unsupported tool event');
  }
  let args = params.Arguments;
  if (typeof args === 'string') {
    try { args = JSON.parse(args); } catch { throw new Error('Invalid tool arguments'); }
  }
  if (!object(args)) throw new Error('Invalid tool arguments');
  return { name: params.ToolName, arguments: args };
}

// The documented input supports strings and text blocks only. Do not coerce or
// drop unsupported multimodal content.
export function encodeAgyUserMessage(content) {
  if (typeof content !== 'string') {
    if (!Array.isArray(content) || !content.every(block => object(block) &&
      Object.keys(block).every(key => key === 'type' || key === 'text') &&
      block.type === 'text' && typeof block.text === 'string')) {
      throw new Error('Only text content is supported');
    }
  }
  return JSON.stringify({ event: 'user', message: { content } }) + '\n';
}

export function createAgyStreamParser({ maxBytes = 256 * 1024, inventory } = {}) {
  if (inventory !== undefined && (!(inventory instanceof Set) || [...inventory].some(name => !nonempty(name)))) {
    throw new Error('Invalid inventory');
  }
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) throw new Error('Invalid byte limit');
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let pending = '', bytes = 0, phase = 'init', conversationId, text = '', failed = false, ended = false;
  const accept = line => {
    let event;
    try { event = JSON.parse(line); } catch { throw new Error('Invalid stream-json'); }
    if (!object(event) || typeof event.event !== 'string') throw new Error('Invalid event');
    if (phase === 'done' || phase === 'tool') throw new Error('Event after completion');
    if (event.event === 'init') {
      if (phase !== 'init' || !nonempty(event.conversation_id) || !object(event.init)) throw new Error('Invalid init');
      conversationId = event.conversation_id;
      phase = 'steps';
      return { type: 'init', conversationId };
    }
    if (phase !== 'steps') throw new Error('Missing init');
    if (event.event === 'step_update') {
      const step = event.step_update;
      if (step?.step_type === 'tool' && inventory !== undefined) {
        const call = recognizeHermesToolCall(event, { conversationId, inventory });
        phase = 'tool';
        return { type: 'tool', ...call };
      }
      if (!object(step) || step.conversation_id !== conversationId || !Number.isSafeInteger(step.step_index) || step.step_index < 0 ||
          !['ACTIVE', 'DONE'].includes(step.state) || !['user_input', 'agent_response', 'checkpoint'].includes(step.step_type) ||
          'tool_info' in step || 'subagent_info' in step || 'tool_name' in step) throw new Error('Unsupported step');
      if (step.text_delta !== undefined) {
        if (step.step_type !== 'agent_response' || typeof step.text_delta !== 'string') throw new Error('Invalid text delta');
        text += step.text_delta;
        return { type: 'text', text: step.text_delta };
      }
      return null;
    }
    if (event.event === 'result') {
      const result = event.result;
      if (!object(result) || result.conversation_id !== conversationId || result.status !== 'SUCCESS' ||
          typeof result.response !== 'string' || result.response !== text ||
          (result.usage !== undefined && !validUsage(result.usage))) throw new Error('Invalid or unsuccessful result');
      phase = 'done';
      return { type: 'result', response: result.response, usage: result.usage };
    }
    throw new Error('Unknown event');
  };
  const consume = final => {
    const events = [];
    let pos;
    while ((pos = pending.indexOf('\n')) !== -1) {
      const line = pending.slice(0, pos);
      pending = pending.slice(pos + 1);
      if (!line.trim()) throw new Error('Empty stream-json line');
      const event = accept(line);
      if (event) events.push(event);
    }
    if (final && pending) {
      const event = accept(pending);
      pending = '';
      if (event) events.push(event);
    }
    return events;
  };
  const guarded = fn => {
    if (failed || ended) throw new Error('Parser is closed');
    try { return fn(); } catch (error) { failed = true; throw error; }
  };
  return {
    write(chunk) {
      return guarded(() => {
        if (!(chunk instanceof Uint8Array)) throw new Error('Expected bytes');
        bytes += chunk.byteLength;
        if (bytes > maxBytes) throw new Error('Stream too large');
        pending += decoder.decode(chunk, { stream: true });
        return consume(false);
      });
    },
    end() {
      return guarded(() => {
        pending += decoder.decode();
        const events = consume(true);
        if (phase !== 'done' && phase !== 'tool') throw new Error('Incomplete stream');
        ended = true;
        return events;
      });
    },
  };
}
