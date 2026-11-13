// Offline contract experiment. This module does not register an OMP provider.
import { spawn } from 'node:child_process';
import { StringDecoder } from 'node:string_decoder';
import { fileURLToPath } from 'node:url';

const FAKE_CLI = fileURLToPath(new URL('./fake-cli.cjs', import.meta.url));
const SCENARIOS = new Set(['text', 'tool', 'context', 'fallback', 'foreign-tool', 'native-tool', 'malformed', 'missing-done', 'nonzero', 'hang', 'slow', 'oversized', 'fake-error']);
const MAX_BYTES = 256 * 1024;

// The only injectable command value is the exact bundled fake; arbitrary executables
// and argument vectors are deliberately not supported.
export function createFakeStreamSimple({ createEventStream, scenario = 'text', command = FAKE_CLI, timeoutMs = 2000 } = {}) {
  if (command !== FAKE_CLI) throw new Error('Only the bundled fake CLI is permitted');
  if (!SCENARIOS.has(scenario)) throw new Error('Unknown fake scenario');
  if (typeof createEventStream !== 'function') throw new Error('An OMP event stream factory is required');
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new Error('Invalid timeout');

  return (model, context, options = {}) => {
    const stream = createEventStream();
    const output = {
      role: 'assistant', content: [], api: model.api, provider: model.provider, model: model.id,
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: 'stop', timestamp: Date.now(),
    };
    void (async () => {
      let child, timer, abort, problem;
      let closed = Promise.resolve();
      const fail = (message, aborted = false) => {
        if (!problem) problem = { message, aborted };
        if (child?.pid) {
          try { process.kill(-child.pid, 'SIGKILL'); } catch { /* Already exited. */ }
        }
      };
      try {
        if (options.signal?.aborted) throw Object.assign(new Error('Aborted'), { aborted: true });
        const request = JSON.stringify({ model: model.id, context });
        if (Buffer.byteLength(request) > MAX_BYTES) throw new Error('Request too large');
        const allowedTools = new Set((context.tools ?? []).map(tool => tool.name));
        child = spawn(FAKE_CLI, [scenario], { detached: true, env: {}, stdio: ['pipe', 'pipe', 'ignore'] });
        closed = new Promise(resolve => child.once('close', (code, signal) => resolve({ code, signal })));
        child.on('error', error => fail(`Fake process error: ${error.message}`));
        child.stdin.on('error', () => {});
        child.stdin.end(request);
        abort = () => fail('Aborted', true);
        options.signal?.addEventListener('abort', abort, { once: true });
        if (options.signal?.aborted) abort();
        timer = setTimeout(() => fail('Fake process timeout'), timeoutMs);
        stream.push({ type: 'start', partial: output });
        let pending = '', bytes = 0, ready = false, done = false, textIndex;
        const decoder = new StringDecoder('utf8');
        const calls = [], ids = new Set();
        const endText = () => {
          if (textIndex !== undefined) {
            stream.push({ type: 'text_end', contentIndex: textIndex, content: output.content[textIndex].text, partial: output });
            textIndex = undefined;
          }
        };
        const accept = line => {
          let event;
          try { event = JSON.parse(line); } catch { throw new Error('Invalid fake NDJSON'); }
          if (!event || typeof event !== 'object' || Array.isArray(event) || typeof event.type !== 'string') throw new Error('Invalid fake event');
          if (done) throw new Error('Event after fake completion');
          if (event.type === 'agent') {
            if (ready || event.name !== 'inventory-only') throw new Error('Fake agent fallback detected');
            ready = true;
          } else if (!ready) {
            throw new Error('Missing fake agent declaration');
          } else if (event.type === 'text' && typeof event.text === 'string') {
            if (textIndex === undefined) {
              textIndex = output.content.push({ type: 'text', text: '' }) - 1;
              stream.push({ type: 'text_start', contentIndex: textIndex, partial: output });
            }
            output.content[textIndex].text += event.text;
            stream.push({ type: 'text_delta', contentIndex: textIndex, delta: event.text, partial: output });
          } else if (event.type === 'tool') {
            if (event.source !== 'inventory' || !allowedTools.has(event.name)) throw new Error('Foreign or native tool event detected');
            if (typeof event.id !== 'string' || !event.id || ids.has(event.id) || !event.arguments || typeof event.arguments !== 'object' || Array.isArray(event.arguments)) throw new Error('Invalid fake tool event');
            ids.add(event.id);
            calls.push({ type: 'toolCall', id: event.id, name: event.name, arguments: event.arguments });
          } else if (event.type === 'done') {
            done = true;
          } else if (event.type === 'error' && typeof event.message === 'string') {
            throw new Error(`Fake CLI error: ${event.message}`);
          } else {
            throw new Error('Unknown or invalid fake event');
          }
        };
        try {
          for await (const chunk of child.stdout) {
            bytes += chunk.length;
            if (bytes > MAX_BYTES) throw new Error('Fake output too large');
            pending += decoder.write(chunk);
            let newline;
            while ((newline = pending.indexOf('\n')) !== -1) {
              const line = pending.slice(0, newline);
              pending = pending.slice(newline + 1);
              if (line) accept(line);
            }
          }
          pending += decoder.end();
          if (pending) accept(pending);
        } catch (error) { fail(error.message); }
        const exit = await closed;
        if (problem) throw Object.assign(new Error(problem.message), { aborted: problem.aborted });
        if (exit.code !== 0) throw new Error(`Fake process exited with code ${exit.code}`);
        if (!ready || !done) throw new Error('Incomplete fake stream');
        endText();
        for (const call of calls) {
          const contentIndex = output.content.push(call) - 1;
          stream.push({ type: 'toolcall_start', contentIndex, partial: output });
          stream.push({ type: 'toolcall_delta', contentIndex, delta: JSON.stringify(call.arguments), partial: output });
          stream.push({ type: 'toolcall_end', contentIndex, toolCall: call, partial: output });
        }
        output.stopReason = calls.length ? 'toolUse' : 'stop';
        stream.push({ type: 'done', reason: output.stopReason, message: output });
      } catch (error) {
        fail(error.message, !!error.aborted);
        await closed;
        output.stopReason = problem?.aborted ? 'aborted' : 'error';
        output.errorMessage = problem?.message ?? error.message;
        stream.push({ type: 'error', reason: output.stopReason, error: output });
      } finally {
        clearTimeout(timer);
        if (abort) options.signal?.removeEventListener('abort', abort);
        stream.end();
      }
    })();
    return stream;
  };
}
