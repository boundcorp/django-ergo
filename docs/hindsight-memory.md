# Hindsight working memory

Hindsight is an opt-in, automatic memory layer alongside the git/Markdown KB. It does not change `ergo_kb` or the KB files. Add it to a bot's `bot.yaml`:

```yaml
plugins:
  - name: hindsight_memory
    bank: ergonaut
    retain: after_turn       # after_turn | off
    prefetch: every_turn     # new_session | every_turn | off
    top_k: 5
    budget: low              # low | mid | high
    timeout_seconds: 3
```

Set `HINDSIGHT_URL` in Ergonaut's environment, for Octo `http://hindsight.llm.svc.cluster.local:8888`. Set `HINDSIGHT_API_KEY` only if the API is configured to require a bearer key. With no URL, the plugin disables itself and logs one warning per bot load. No bot enables it by default.

After a completed human turn, Ergonaut queues one document containing the user message and final assistant reply on its Celery `bot_tasks` queue. Hindsight processes it asynchronously. The document ID uses the turn ID so retries replace the same document. Empty, failed, approval-pending, automated, and forwarded turns are skipped. Enqueue or retain failures are logged without affecting the turn.

Prefetch recalls relevant facts for the incoming message into the turn's context block. It does not write them to conversation history and stops waiting after `timeout_seconds`. The skill offers `memory_recall(query)`, `memory_reflect(question)` (slower), and `memory_retain(text)` for explicit memory requests. Bank selection comes only from bot configuration. The shared bank receives `bot`, `chat`, `user`, and, for child sessions, `thread` tags and metadata on each item. Recall and reflect require the current user's tag so memories cannot cross users. A future bank template can be added at the plugin's `_bank` method without changing model-visible tools.

The API uses the [Hindsight retain](https://hindsight.vectorize.io/developer/api/retain), [recall](https://hindsight.vectorize.io/developer/api/recall), and [reflect](https://hindsight.vectorize.io/developer/api/reflect) endpoints.
