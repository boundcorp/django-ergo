---
name: page-design
description: Design clear live Ergo pages or portable HTML attachments for plans, status, comparisons, flows and maps.
requires: [pages, attachments]
plugins: [pages, attachments]
install: [claude, codex]
---
# Page design

Use a visual presentation when structure matters more than a narrative: more than about five related items, dependencies, progress across many items, alternatives to compare, a process to follow, or something the reader will revisit. Keep prose for a short answer, a nuanced recommendation, or a decision whose evidence cannot be represented accurately in a compact view. A page should answer one question at a glance; pair it with a short text summary and the important caveat when needed.

This skill works for any subject. A campaign plan, house move, trip choice and software project are all collections of information with different useful shapes. Do not assume a project-development workflow.

## Choose the output

| Need | Use | Why |
| --- | --- | --- |
| Current bot-table data, a page the reader will reopen, or a chat tab | A live `.jhtml` page with `ergo_page_write` | It is rendered again whenever opened and can be pinned. |
| A self-contained record, email-like handoff, or data not owned by bot tables | A static `.html` or `.md` attachment with `ergo_attachments_create` | It preserves the exact snapshot and travels as a file. |
| A reviewed, durable page in the bot repository | `pages/<name>.jhtml` in the bot folder | It is versioned and can be pinned in `chats.<name>.pins`. |

A live page is not a general application surface: its Jinja sandbox is read-only. It can query `table("Name")`, use `blocks.*`, and include files in the **bot folder**. A library recipe cannot be included directly from this skill folder, so copy the recipe source into the chat page or the bot's `pages/` directory. This is deliberate: it avoids a second cross-directory loader and keeps page source reviewable where it runs.

## Build and check

1. Load this skill: `ergo_skill_load("page-design")`. It also loads `pages` and `attachments` and adds their plugins if the bot did not list them already.
2. Pick one pattern below. Copy its source, replace its sample list or dictionaries with the current facts, and give the page a descriptive title.
3. For a live page call `ergo_page_write(filename="…", title="…", source="…", pin=true)`. Prefer `blocks` only for standard metrics, tables and charts; use source for these layouts.
4. Read the result from `ergo_page_write` or call `ergo_page_preview` before presenting it. Correct rendering errors and check that text order, labels and states make sense without colour.
5. Pin a useful chat page with `ergo_page_pin`, or write a static snapshot with `ergo_attachments_create(filename="…html", content="…")`. Say whether the result is live or a snapshot.

The page plugin limits a chat page to its configured `max_bytes` (500,000 bytes by default); the attachments plugin has its own configured file limit (5,000,000 bytes by default). Keep visual pages far smaller: bounded lists, no huge embedded datasets, and no copied binaries. Summarize or link to a download instead.

## Readability and accessibility

- Start with an `h1`, then use ordered headings; name the view and its time range or freshness.
- Put the decision, key status, or next action near the top. Use short labels, familiar dates, and a visible legend when states occur.
- Never rely only on colour, position, hover, animation, or JavaScript. Put status in text; tables and relation lists are the fallback for a diagram.
- Use semantic `table`, `thead`, `th`, `caption`, `ul` and `li` where appropriate. Add an SVG `title` and a text relation list for maps and flows.
- Make the layout work at mobile width: wrapping grids, horizontal scrolling only around wide data tables, no fixed desktop-only canvas.
- Use CSS variables with both light and dark values, sufficient contrast, visible borders and modest type sizes. Do not fetch fonts, images, scripts or styles from external CDNs. The standard Ergo layout already supplies Chart.js for `blocks.chart`.
- Escape or render through Jinja normally; do not construct raw HTML from untrusted labels. The supplied recipes rely on Jinja autoescape.

## Pattern catalog

Each file is a copy-paste source recipe. It defines a macro that accepts generic data and then calls it with a small, domain-varied example. Keep the macro and replace the example call with your data.

| Pattern | Use when | Recipe |
| --- | --- | --- |
| Plan / roadmap | Work has phases, steps, owners and progress. | `patterns/roadmap.jhtml` |
| Status board | Many independent items need a state at a glance. | `patterns/status-board.jhtml` |
| Dependency table | Ordering, blockers or prerequisites need an auditable text view. | `patterns/dependency-table.jhtml` |
| Decision flow | A reader needs to choose a route based on answers or conditions. | `patterns/decision-flow.jhtml` |
| Timeline | Dates, milestones, bookings or events are the main story. | `patterns/timeline.jhtml` |
| Comparison matrix | Several options must be judged against shared criteria. | `patterns/comparison-matrix.jhtml` |
| Project map | Long-running work needs a current map of parts, progress, blockers and the next decision. This is also useful for a household, campaign or trip. | `patterns/project-map.jhtml` |

The project-map pattern is adapted from the [Voxyz_ai post](https://x.com/Voxyz_ai/status/2107455844992299272). A map must answer: what parts the work breaks into, how far each part is, what is stuck and what it is waiting on, and what to do next—including the default when nobody decides.

## Living project maps

Keep a living map for long-running or unattended work. Draw it before the work starts, update it at each milestone, highlight what changed since the last update, and answer “where are we?” from it. Use a live `.jhtml` page pinned in the chat, or rewrite the same attachment file.

Put decisions that need the user in the map with a deadline and a default, then continue with the default if no reply arrives. Choose panels for the subject rather than filling a fixed template: milestones, parts, next step, decisions, changes and relationships are independently useful.

If the bot has memory or a knowledge base, optionally ask once for dark or light and one accent colour, then reuse those preferences. Do not assume either preference or a memory tool exists.

## Pattern selection

- **Roadmap:** one row per phase or planned action; show owner and state. Good for a marketing launch or a move. Do not use it for an unordered inventory.
- **Status board:** one card per item, grouped by an explicit state. Good for household chores, applications or content production. Limit to the active set; split or filter an oversized board.
- **Dependency table:** show every item, its prerequisite names and state in text. Good when exact relationships matter more than a decorative graph.
- **Decision flow:** state the question and every available route. Good for travel booking rules or support triage. Keep branches few; use a matrix for weighted tradeoffs.
- **Timeline:** make date or sequence the leading dimension. Good for a trip itinerary, release plan or family schedule. Use real dates when known and label estimates.
- **Comparison matrix:** use common criteria and explicit scores or evidence. Good for providers, destinations or channels. Explain scores in text rather than implying false precision.
- **Project map:** use when a reader needs one current view of parts, milestones, blockers and the next action. Include state in text; add the optional relationship list only when dependencies between parts matter.

When a request combines views, start with the one that answers the decision and link or attach supporting detail rather than making an unreadable dashboard.
