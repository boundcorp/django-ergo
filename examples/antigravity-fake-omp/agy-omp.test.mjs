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
      { ...context, systemPrompt: ['instructions'] },
      { ...context, tools: [{ name: 'lookup' }] },
      { messages: [...context.messages, { role: 'assistant', content: [] }] },
      { messages: [{ role: 'user', content: [{ type: 'image', data: 'x' }] }] },
    ]) expect(() => encodeContext(extra)).toThrow();
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
