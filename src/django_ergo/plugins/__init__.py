"""Ergo's official bot plugins, referenced by short name in bot.yaml.

One module per plugin:

- ``ergo_kb`` (kb.py): knowledge-base tools and RAG prefetch.
- ``bot_management`` (bot_management.py): a bot edits its own repo and proposes changes.
- ``telegram`` (telegram.py): a Telegram channel for a bot.
- ``orca`` (orca.py): manage Orca worktrees, terminals and workers with the Orca CLI.
- ``bash`` (bash.py): run shell commands on the host, with approval.

Bot-specific tools don't belong here; put them in the bot folder's ``tools/``.
"""
