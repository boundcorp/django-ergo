// Synthetic data shaped from official headless documentation, not captured agy output.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createAgyStreamParser, encodeAgyUserMessage, recognizeHermesToolCall } from './agy-protocol.mjs';

const encoder = new TextEncoder();
const init = { event: 'init', conversation_id: 'synthetic-id', init: { cwd: '/synthetic', tools: [], permission_mode: 'request-review', agent: 'untrusted-claim' } };
const step = (step_type, state, extra = {}) => ({ event: 'step_update', step_update: { conversation_id: 'synthetic-id', step_index: 1, state, step_type, ...extra } });
const result = (response, extra = {}) => ({ event: 'result', result: { conversation_id: 'synthetic-id', status: 'SUCCESS', response, ...extra } });
const lines = events => events.map(event => JSON.stringify(event)).join('\n') + '\n';
const parse = value => {
  const parser = createAgyStreamParser();
  return [...parser.write(encoder.encode(value)), ...parser.end()];
};

test('encodes documented input without dropping unsupported blocks', () => {
  assert.equal(encodeAgyUserMessage('hi\nthere'), '{"event":"user","message":{"content":"hi\\nthere"}}\n');
  const blocks = [{ type: 'text', text: 'hello' }];
  assert.deepEqual(JSON.parse(encodeAgyUserMessage(blocks)).message.content, blocks);
  for (const value of [null, 1, [{ type: 'image', text: 'x' }], [{ type: 'text', text: 'x', extra: true }]]) {
    assert.throws(() => encodeAgyUserMessage(value));
  }
});

test('handles arbitrary byte splits, UTF-8, CRLF and unterminated final line', () => {
  const usage = { input_tokens: 3, output_tokens: 2, thinking_tokens: 0, cache_read_tokens: 0, total_tokens: 5 };
  const wire = lines([init, step('user_input', 'DONE'), step('agent_response', 'ACTIVE', { text_delta: 'hé' }), step('agent_response', 'DONE', { text_delta: 'llo' }), step('checkpoint', 'DONE'), result('héllo', { usage })]).replaceAll('\n', '\r\n').slice(0, -2);
  const parser = createAgyStreamParser();
  const events = [];
  for (const byte of encoder.encode(wire)) events.push(...parser.write(Uint8Array.of(byte)));
  events.push(...parser.end());
  assert.deepEqual(events, [{ type: 'init', conversationId: 'synthetic-id' }, { type: 'text', text: 'hé' }, { type: 'text', text: 'llo' }, { type: 'result', response: 'héllo', usage }]);
  assert.throws(() => parser.end());
});

test('rejects tool, subagent, unknown and malformed events', () => {
  const bad = [
    step('tool', 'DONE', { tool_name: 'run_command', tool_info: { parameters: {} } }),
    step('agent_response', 'DONE', { subagent_info: {} }),
    step('unknown', 'DONE'), step('agent_response', 'NEW'),
    step('user_input', 'DONE', { text_delta: 'not assistant text' }),
    { event: 'unknown' }, { event: 'step_update', step_update: null },
    step('agent_response', 'DONE', { conversation_id: 'other' }),
  ];
  for (const event of bad) assert.throws(() => parse(lines([init, event, result('')])));
  assert.throws(() => parse(lines([init]) + '{bad}\n'));
  assert.throws(() => parse(lines([init]) + '\n'));
});

test('requires ordered single-turn completion and matching response', () => {
  for (const events of [[], [result('')], [init], [init, init], [init, result('', { status: 'ERROR' })],
    [init, result('', { conversation_id: 'other' })], [init, result('missing delta')],
    [init, result(''), result('')]]) assert.throws(() => parse(lines(events)));
  assert.deepEqual(parse(lines([init, result('')])), [{ type: 'init', conversationId: 'synthetic-id' }, { type: 'result', response: '', usage: undefined }]);
});

test('fails permanently on invalid UTF-8, size limits, or invalid chunks', () => {
  for (const chunk of [Uint8Array.of(0xff), 'string']) {
    const parser = createAgyStreamParser();
    assert.throws(() => parser.write(chunk));
    assert.throws(() => parser.end());
  }
  const parser = createAgyStreamParser({ maxBytes: 2 });
  assert.throws(() => parser.write(encoder.encode('abc')));
  assert.throws(() => parser.write(encoder.encode('a')));
  assert.throws(() => createAgyStreamParser({ maxBytes: 0 }));
  const truncated = createAgyStreamParser();
  truncated.write(Uint8Array.of(0xc3));
  assert.throws(() => truncated.end());
});

test('recognizes only inventory dispatch events, without executing them', () => {
  const options = { conversationId: 'synthetic-id', inventory: new Set(['lookup']) };
  const call = step('tool', 'ACTIVE', { tool_name: 'call_mcp_tool', tool_info: { name: 'call_mcp_tool', parameters: { ServerName: 'hermes', ToolName: 'lookup', Arguments: { query: 'x' } } } });
  assert.deepEqual(recognizeHermesToolCall(call, options), { name: 'lookup', arguments: { query: 'x' } });
  const stringCall = structuredClone(call);
  stringCall.step_update.tool_info.parameters.Arguments = '{\"query\":\"x\"}';
  assert.deepEqual(recognizeHermesToolCall(stringCall, options), { name: 'lookup', arguments: { query: 'x' } });
  for (const [key, value] of [['ServerName', 'foreign'], ['ToolName', 'native'], ['Arguments', 'invalid']]) {
    const bad = structuredClone(call);
    bad.step_update.tool_info.parameters[key] = value;
    assert.throws(() => recognizeHermesToolCall(bad, options));
  }
  const native = structuredClone(call);
  native.step_update.tool_name = 'run_command';
  assert.throws(() => recognizeHermesToolCall(native, options));
  assert.throws(() => recognizeHermesToolCall(call, { ...options, conversationId: 'other' }));
  assert.throws(() => parse(lines([init, call, result('')])));
});

test('rejects invalid usage instead of inventing counters', () => {
  for (const usage of [{}, { input_tokens: 1 }, { input_tokens: -1, output_tokens: 0, thinking_tokens: 0, cache_read_tokens: 0, total_tokens: 0 }]) {
    assert.throws(() => parse(lines([init, result('', { usage })])));
  }
});
