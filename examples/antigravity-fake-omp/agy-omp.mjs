// Offline replay only: no process, provider registration, credentials, or networking.
import { createAgyStreamParser, encodeAgyUserMessage } from './agy-protocol.mjs';

// Native agy stream input cannot represent system prompts, tool inventories,
// assistant messages or tool results. Never flatten or silently discard them.
export function encodeContext(context) {
  if (!context || (context.systemPrompt?.length ?? 0) || (context.tools?.length ?? 0) ||
      !Array.isArray(context.messages) || context.messages.length !== 1 || context.messages[0].role !== 'user') {
    throw new Error('Only a single text-only user message without system prompt or tools is supported');
  }
  const content = context.messages[0].content;
  if (typeof content !== 'string' && (!Array.isArray(content) || content.some(block => block.type !== 'text' || typeof block.text !== 'string'))) {
    throw new Error('Only text user content is supported');
  }
  return encodeAgyUserMessage(content);
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
        encodeContext(context);
        const parser = createAgyStreamParser();
        const events = [];
        for (const chunk of fixtureChunks) {
          if (options.signal?.aborted) throw Object.assign(new Error('Aborted'), { aborted: true });
          events.push(...parser.write(chunk));
        }
        events.push(...parser.end());
        if (options.signal?.aborted) throw Object.assign(new Error('Aborted'), { aborted: true });
        const result = events.find(event => event.type === 'result');
        if (!result) throw new Error('Missing terminal result');
        const usage = result.usage;
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
        stream.push({ type: 'done', reason: 'stop', message: output });
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
