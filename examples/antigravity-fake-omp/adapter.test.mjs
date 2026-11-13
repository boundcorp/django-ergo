import { describe, expect, test } from 'bun:test';
import { createAssistantMessageEventStream } from '/usr/lib/node_modules/@oh-my-pi/pi-coding-agent/node_modules/@oh-my-pi/pi-ai/src/utils/event-stream.ts';
import { createFakeStreamSimple } from './adapter.mjs';

const model = { id: 'fake-model-name', api: 'fake-api', provider: 'fake-provider' };
const context = { systemPrompt: ['offline'], messages: [{ role: 'user', content: [{ type: 'text', text: 'hello' }], timestamp: 1 }], tools: [{ name: 'lookup', description: 'lookup', parameters: { type: 'object' } }] };
const create = (scenario, extra = {}) => createFakeStreamSimple({ createEventStream: createAssistantMessageEventStream, scenario, ...extra });
async function collect(stream, onEvent = () => {}) {
  const events = [];
  for await (const event of stream) { events.push(event); onEvent(event); }
  return { events, result: await stream.result() };
}

describe('fake-only OMP stream contract', () => {
  test('rejects every command except the bundled fake and rejects unknown scenarios', () => {
    for (const command of ['agy', '/usr/bin/agy', process.execPath, './fake-cli.cjs']) {
      expect(() => create('text', { command })).toThrow('Only the bundled fake CLI');
    }
    expect(() => create('arbitrary')).toThrow('Unknown fake scenario');
  });

  test('streams text through the installed OMP event stream', async () => {
    const { events, result } = await collect(create('text')(model, context));
    expect(events.map(e => e.type)).toEqual(['start', 'text_start', 'text_delta', 'text_delta', 'text_end', 'done']);
    expect(events.filter(e => e.type === 'text_delta').map(e => e.delta).join('')).toBe('Hello world ☃');
    expect(result).toMatchObject({ role: 'assistant', api: model.api, provider: model.provider, model: model.id, stopReason: 'stop', content: [{ type: 'text', text: 'Hello world ☃' }] });
    expect(events.at(-1).message).toBe(result);
  });

  test('passes model and complete tool result history into the fake request', async () => {
    const history = { ...context, messages: [...context.messages,
      { role: 'assistant', content: [{ type: 'toolCall', id: 'prev', name: 'lookup', arguments: { q: 1 } }], api: model.api, provider: model.provider, model: model.id, usage: {}, stopReason: 'toolUse', timestamp: 2 },
      { role: 'toolResult', toolCallId: 'prev', toolName: 'lookup', content: [{ type: 'text', text: 'found' }], isError: false, timestamp: 3 },
    ] };
    const { result } = await collect(create('context')(model, history));
    expect(JSON.parse(result.content[0].text)).toEqual({ model: model.id, context: history });
  });

  test('emits native OMP tool calls for parent execution', async () => {
    const { events, result } = await collect(create('tool')(model, context));
    expect(events.map(e => e.type)).toEqual(['start', 'text_start', 'text_delta', 'text_end', 'toolcall_start', 'toolcall_delta', 'toolcall_end', 'done']);
    const call = { type: 'toolCall', id: 'call-1', name: 'lookup', arguments: { query: 'example' } };
    expect(events.find(e => e.type === 'toolcall_end').toolCall).toEqual(call);
    expect(events.find(e => e.type === 'toolcall_delta').delta).toBe(JSON.stringify(call.arguments));
    expect(result.content).toEqual([{ type: 'text', text: 'Looking up' }, call]);
    expect(result.stopReason).toBe('toolUse');
  });

  test.each([
    ['fallback', 'fallback detected'], ['foreign-tool', 'Foreign or native'], ['native-tool', 'Foreign or native'],
    ['malformed', 'Invalid fake NDJSON'], ['missing-done', 'Incomplete fake stream'],
    ['nonzero', 'exited with code 7'], ['fake-error', 'simulated failure'], ['oversized', 'output too large'],
  ])('%s fails closed without forwarding a staged tool call', async (scenario, message) => {
    const { events, result } = await collect(create(scenario)(model, context));
    expect(events.at(-1).type).toBe('error');
    expect(events.some(e => e.type === 'toolcall_end' || e.type === 'done')).toBe(false);
    expect(result.stopReason).toBe('error');
    expect(result.errorMessage).toContain(message);
  });

  test('times out and reaps the fake process', async () => {
    let pid;
    const { result } = await collect(create('hang', { timeoutMs: 300 })(model, context), event => {
      if (event.type === 'text_delta') pid = Number(event.delta);
    });
    expect(pid).toBeGreaterThan(0);
    expect(result.errorMessage).toContain('timeout');
    expect(() => process.kill(pid, 0)).toThrow();
  });

  test('cancellation kills and reaps the fake process', async () => {
    const controller = new AbortController();
    let pid;
    const { events, result } = await collect(create('hang')(model, context, { signal: controller.signal }), event => {
      if (event.type === 'text_delta') { pid = Number(event.delta); controller.abort(); }
    });
    expect(result.stopReason).toBe('aborted');
    expect(events.at(-1).reason).toBe('aborted');
    expect(() => process.kill(pid, 0)).toThrow();
  });

  test('already-aborted and oversized requests produce terminal errors', async () => {
    const controller = new AbortController(); controller.abort();
    expect((await collect(create('text')(model, context, { signal: controller.signal }))).result.stopReason).toBe('aborted');
    const large = { ...context, systemPrompt: ['x'.repeat(300000)] };
    expect((await collect(create('text')(model, large))).result.errorMessage).toContain('Request too large');
  });
});
