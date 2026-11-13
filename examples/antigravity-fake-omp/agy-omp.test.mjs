import { describe, expect, test } from 'bun:test';
import { createAssistantMessageEventStream } from '/usr/lib/node_modules/@oh-my-pi/pi-coding-agent/node_modules/@oh-my-pi/pi-ai/src/utils/event-stream.ts';
import { createOfflineAgyStreamSimple, encodeContext } from './agy-omp.mjs';

// Synthetic, modeled on the public headless documentation, not a captured session.
const model = { id: 'gemini-3.8-flash-medium', api: 'offline-agy', provider: 'offline' };
const context = { messages: [{ role: 'user', content: [{ type: 'text', text: 'hello' }] }] };
const usage = { input_tokens: 10, output_tokens: 2, thinking_tokens: 1, cache_read_tokens: 4, total_tokens: 12 };
const init = { event: 'init', conversation_id: 'synthetic', init: { cwd: '/sandbox', tools: [], permission_mode: 'request-review' } };
const step = { event: 'step_update', step_update: { conversation_id: 'synthetic', step_index: 1, state: 'DONE', step_type: 'agent_response', text_delta: 'hello' } };
const result = { event: 'result', result: { conversation_id: 'synthetic', status: 'SUCCESS', response: 'hello', usage } };
const bytes = rows => [new TextEncoder().encode(rows.map(row => JSON.stringify(row)).join('\n') + '\n')];
async function replay(rows, input = context, options = {}) {
  const stream = createOfflineAgyStreamSimple({ createEventStream: createAssistantMessageEventStream, fixtureChunks: bytes(rows) })(model, input, options);
  const events = [];
  for await (const event of stream) events.push(event);
  return { events, output: await stream.result() };
}

describe('synthetic agy OMP replay', () => {
  test('encodes a documented user input and rejects unrepresentable context', () => {
    expect(JSON.parse(encodeContext(context))).toEqual({ event: 'user', message: { content: [{ type: 'text', text: 'hello' }] } });
    for (const extra of [
      { messages: [...context.messages, { role: 'assistant', content: [] }] },
      { messages: [{ role: 'user', content: [{ type: 'image', data: 'x' }] }] },
      { ...context, systemPrompt: 'not an array' },
      { ...context, tools: [{ name: 'lookup' }] },
    ]) expect(() => encodeContext(extra)).toThrow();
  });
  test('serializes system, tool catalog, history and matching tool results', () => {
    const input = { systemPrompt: ['Follow instructions'], tools: [{ name: 'lookup', description: 'Look up', parameters: { type: 'object', properties: { query: { type: 'string' } } } }],
      messages: [...context.messages,
        { role: 'assistant', content: [{ type: 'text', text: 'Checking' }, { type: 'toolCall', id: 'one', name: 'lookup', arguments: { query: '</conversation>' } }] },
        { role: 'toolResult', toolCallId: 'one', toolName: 'lookup', content: [{ type: 'text', text: 'found' }], isError: true }] };
    const prompt = JSON.parse(encodeContext(input)).message.content;
    const record = JSON.parse(prompt.split('\n').at(-1));
    expect(record.system).toEqual(input.systemPrompt);
    expect(record.tools).toEqual(input.tools);
    expect(record.messages[1].content[1].arguments.query).toBe('</conversation>');
    expect(record.messages[2]).toEqual({ role: 'tool', toolCallId: 'one', name: 'lookup', content: 'found', isError: true });
    expect(() => encodeContext({ ...input, messages: input.messages.slice(0, -1) })).toThrow();
    expect(() => encodeContext({ ...input, messages: [...input.messages.slice(0, -1), { ...input.messages.at(-1), toolCallId: 'bad' }] })).toThrow();
    expect(() => encodeContext({ ...input, messages: [...input.messages, { role: 'user', content: [{ type: 'image' }] }] })).toThrow();
  });
  test('retains ordinary OMP bookkeeping but rejects unrepresented semantics', () => {
    const assistant = { role: 'assistant', api: 'offline', provider: 'offline', model: 'model', usage: {},
      timestamp: 1, stopReason: 'toolUse', content: [{ type: 'toolCall', id: 'c', name: 'lookup', arguments: { q: 1 } }] };
    const resultMessage = { role: 'toolResult', toolCallId: 'c', toolName: 'lookup', isError: false,
      content: [{ type: 'text', text: 'ok' }], timestamp: 2 };
    const input = { messages: [{ ...context.messages[0], timestamp: 0, steering: false }, assistant, resultMessage],
      tools: [{ name: 'lookup', description: 'lookup', parameters: { type: 'object', properties: {} } }] };
    expect(() => encodeContext(input)).not.toThrow();
    for (const bad of [
      { ...input, unknownContext: true },
      { ...input, tools: [{ ...input.tools[0], unknownTool: true }] },
      { ...input, tools: [{ ...input.tools[0], parameters: { type: 'object', value: undefined } }] },
      { ...input, messages: [{ ...input.messages[0], steering: true }, assistant, resultMessage] },
      { ...input, messages: [input.messages[0], { ...assistant, stopReason: 'error' }, resultMessage] },
      { ...input, messages: [input.messages[0], { ...assistant, stopReason: 'stop' }, resultMessage] },
      { ...input, messages: [input.messages[0], { ...assistant, providerPayload: {} }, resultMessage] },
      { ...input, messages: [input.messages[0], assistant, { ...resultMessage, details: { hidden: true } }] },
      { ...input, messages: [input.messages[0], assistant, { ...resultMessage, unknown: true }] },
      { ...input, messages: [input.messages[0], { ...assistant, content: [{ ...assistant.content[0], arguments: { q: NaN } }] }, resultMessage] },
    ]) expect(() => encodeContext(bad)).toThrow();
  });
  test('relays one synthetic inventory call to the parent without executing it', async () => {
    const input = { ...context, tools: [{ name: 'lookup', description: 'Look up', parameters: { type: 'object', properties: {} } }] };
    const tool = { event: 'step_update', step_update: { conversation_id: 'synthetic', step_index: 2, state: 'ACTIVE', step_type: 'tool', tool_name: 'call_mcp_tool', tool_info: { name: 'call_mcp_tool', parameters: { ServerName: 'hermes', ToolName: 'lookup', Arguments: '{"query":"x"}' } } } };
    const { events, output } = await replay([init, step, tool], input);
    expect(events.map(event => event.type)).toEqual(['start', 'text_start', 'text_delta', 'text_end', 'toolcall_start', 'toolcall_delta', 'toolcall_end', 'done']);
    expect(output.stopReason).toBe('toolUse');
    expect(output.content[1]).toMatchObject({ type: 'toolCall', name: 'lookup', arguments: { query: 'x' } });
    expect(output.content[1].id).toBeTruthy();
    expect((await replay([init, tool, result], input)).events.map(event => event.type)).toEqual(['error']);
    expect((await replay([init, tool], context)).events.map(event => event.type)).toEqual(['error']);
  });
  test('converts source-shaped text and usage to OMP without repeating result text', async () => {
    const { events, output } = await replay([init, step, result]);
    expect(events.map(event => event.type)).toEqual(['start', 'text_start', 'text_delta', 'text_end', 'done']);
    expect(output.content).toEqual([{ type: 'text', text: 'hello' }]);
    expect(output.usage).toMatchObject({ input: 10, output: 2, cacheRead: 4, totalTokens: 12, reasoningTokens: 1 });
  });
  test('rejects tool events instead of treating post-action observation as safe interception', async () => {
    const tool = { event: 'step_update', step_update: { conversation_id: 'synthetic', step_index: 2, state: 'DONE', step_type: 'tool', tool_name: 'run_command', tool_info: { name: 'run_command', parameters: { CommandLine: 'echo x' }, output: 'x' } } };
    const { events, output } = await replay([init, step, tool, result]);
    expect(events.map(event => event.type)).toEqual(['error']);
    expect(output.stopReason).toBe('error');
  });
  test('fails closed on upstream error and cancellation', async () => {
    const failure = { ...result, result: { ...result.result, status: 'ERROR', error: 'synthetic failure' } };
    expect((await replay([init, step, failure])).output.stopReason).toBe('error');
    const controller = new AbortController(); controller.abort();
    expect((await replay([init, step, result], context, { signal: controller.signal })).output.stopReason).toBe('aborted');
  });
});
