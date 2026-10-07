# Examples

- `*.py`: small scripts showing Ergo's knowledge base, RAG and feedback APIs.
- `hello/`: the smallest bot folder: `bot.yaml`, `agents.md` and one tool
  module. [Getting started](../docs/getting-started.md) runs it.
- `pantry/`: a bot with a table, a pinned `.jhtml` page and a `@page_action`.
  The page lists the `Pantry` table; its Restock button calls `restock` with
  `ergo.call` (the viewer asks to confirm first), the action updates rows and
  calls `touch()`, and the page re-renders itself when the table changes
  (`ergo.on("table:Pantry", ...)`). Add rows by asking the bot, then open the
  pinned Pantry page. See [Page actions](../docs/bots.md#page-actions).
- `proprietary/`: private example bots, such as the kitchen bot. This folder
  is git-ignored, so its bots never reach the public repo.

Run a bot folder with Ergonaut (see [Running Ergonaut](../docs/ergonaut.md)).
