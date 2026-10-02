# Ergo documentation

## Building bots

| Guide | Covers |
| --- | --- |
| [Getting started](getting-started.md) | Running Ergonaut with the hello bot, your first tool |
| [Building bots](building-bots.md) | The bot folder, bot.yaml, chats and threads, sub-bots, knowledge, tables and pages, self-management |
| [Skills](skills.md) | Skill folders, tool files and plugins as skills; loading and unloading |
| [Tools and plugins](tools.md) | `@bot_tool`, the tool context, secrets, approvals, images, background tasks, workers, toolkits, writing plugins |
| [Schedules](schedules.md) | Cron, targets, `run` and `prompt` actions |
| [Bot reference](bots.md) | Every bot.yaml key and official plugin option |
| [Running Ergonaut](ergonaut.md) | Commands, settings, the web app, reloading, containers, production |

## Library

| Doc | Covers |
| --- | --- |
| [Structured calls](structured-calls.md) | `StructuredCall`, typed responses with tools, revisions |
| [Compaction](compaction.md) | Session compaction modes and summaries |
| [Context builder and window chats](context-builder.md) | Budgeted context blocks; chats with a fixed-size window |
| [Message history](message-history.md) | Reading sessions and Claude Code / Codex transcripts; the history toolkit |
| [Attachments](attachments.md) | Images, audio and documents on messages; session files |
| [Semantic fields and search](semantic-search.md) | `SemanticTextField`, vector search, embedding providers |
| [Knowledge foundation](knowledge-foundation.md) | Backend-neutral corpora, retrieval and usage tracking |
| [Knowledge paths](knowledge-paths.md) | Path-first KB APIs and reviewed moves |
| [Filesystem and vector integration](fs-vector-integration.md) | Repository snapshots, Markdown projection, wiki proposals |

## Project

- [Development](development.md): setup, tests, lint, CI, conventions
- [Git releases](git-release.md): installing from a reviewed commit
- [TODO.md](../TODO.md): the current backlog
- [archive/](archive/README.md): earlier plans, specs and status documents, kept for history
- [preservation/](preservation/2026-09-10/README.md): byte-identical historical evidence
