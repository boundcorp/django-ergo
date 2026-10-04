# Skills

Everything a bot can do beyond talking is a **skill**: a name, a
description, and optionally instructions and tools. A chat starts knowing
only the list of skills and loads the ones it needs, so each model call
carries a short tool list and the prompt stays small.

## Where skills come from

| Source | Skill name | Description from |
| --- | --- | --- |
| `skills/<name>.md` or `skills/<name>/SKILL.md` | front matter `name`, else the file or folder name | front matter `description` |
| `skills/<name>/tools.py` next to a `SKILL.md` | the folder's skill | (tools join that skill) |
| a tool file in `tools:` (`tools/tandoor.py`) | `tandoor` | the module docstring's first line |
| a plugin with tools (`orca`, `bash`, `attachments`, `pages`, ...) | the plugin's name (`ergo_kb` is `kb`, `bot_management` is `config_repo`) | the plugin |
| a `toolkits:` factory (`myapp.toolkits:make_toolkit`) | the factory's name (`make_toolkit`) | the factory's docstring |
| built-ins | `history`, `orchestration`, `workers`, `introspection` (bots loaded from a folder), `tables` (when the bot has tables) | |
| Ergo's skill library (`skillbuilder`) | the folder name, when the bot names it (below) | front matter `description` |

## Writing a skill

A Markdown skill is a procedure the bot follows once it decides it's
relevant:

```markdown
---
name: meal-planning
description: Plan a week of dinners from the recipe library
requires: [tandoor]      # load these with it
always_load: false       # true: loaded in every chat from the start
plugins: [attachments]   # plugins it needs (below)
---
1. Look at the last 60 days of the meal plan with view_meal_plan.
2. Skip anything cooked in the last two weeks.
3. Propose five dinners as a question, with "Looks good" as a suggestion.
```

The description is what the model sees before loading, so say when to use
the skill, not just what it is. The body is what it reads after loading.

To ship tools with it, use a folder:

```
skills/
  triage/
    SKILL.md
    tools.py        # @bot_tool functions, offered once triage is loaded
```

`tools.py` is imported when the bot loads (see [Tools](tools.md)), but its
tools only reach the model while the skill is loaded.

A tool file listed under `tools:` is a skill too, with no instructions: its
module docstring's first line is the description, so write one.

Plugins without tools, like `telegram`, aren't skills.

### Skills that use other skills

`requires` is how one skill includes others: loading it loads them too,
along with their instructions and tools, transitively. Any kind of skill can
be required, so a Markdown skill can bundle a tool file, a plugin and
another Markdown skill.

A skill can also bring the plugins it needs, with the settings it depends
on:

```markdown
---
requires: [config_repo]
plugins:
  bot_management: {mode: propose_pr}
---
```

If bot.yaml doesn't list the plugin, the bot gets it with those settings.
If bot.yaml lists it, the skill's settings are added to it, and a setting
bot.yaml gives a different value fails the bot's load, so a skill that
depends on pull requests can't end up merging to main.

### Ergo's skill library

Ergo ships reusable skill folders in `django_ergo/bots/skill_library/`. A
bot gets one by naming it: in `skills: {include: [...]}`, in a chat's or
thread's `skills`, or in any skill's `requires`. A skill in the bot's own
folder with the same name replaces the library's.

| Skill | For |
| --- | --- |
| `skillbuilder` | Writing the bot's own skills, tool files, workers, tables, schedules and `.jhtml` dashboards, proposed as pull requests. Requires `config_repo` and `introspection`, and brings `bot_management` in `propose_pr` mode. |

```yaml
skills:
  include: [skillbuilder]
```

### Default skills

`DJANGO_ERGO["DEFAULT_SKILLS"]` lists library skills every bot loaded from a
folder gets without naming them (default: `["skillbuilder"]`). A bot opts
out in bot.yaml:

```yaml
skills:
  exclude: [skillbuilder]   # leave out some defaults
  defaults: false           # or all of them
```

A default skill whose plugin settings clash with bot.yaml (skillbuilder on a
bot whose `bot_management` is in `merge_main` mode) is left out with a
warning; a skill the bot names itself fails the load instead. Set
`DEFAULT_SKILLS: []` to turn defaults off for every bot.

## Loading and unloading

Every chat begins with an `ergo_skills_list` result already in its history:
each skill, whether it's loaded, how many tools it has, and short hints for
some unloaded plugins ("3 files in this chat").

- `ergo_skill_load(name)` returns the skill's instructions (and its plugin
  context, if any) and adds its tools from the next model call, in the same
  turn. Skills it `requires` load with it.
- A loaded skill whose tools go unused for `skills.unload_after_turns` turns
  (default 30, counting every turn in the chat) is dropped.
  `ergo_skill_unload(name)` drops one sooner.
- Loaded skills are remembered per chat.

## Choosing what's loaded where

```yaml
chats:
  main:
    skills: [orchestration, tandoor]    # always loaded in main (default: [orchestration])
  reports:
    skills: [analytics]
threads:
  skills: [triage]                      # always loaded in child threads
skills:
  folder: skills                        # default
  unload_after_turns: 30
  requires: {meal-planning: [tandoor]}  # same as front matter requires
  include: [skillbuilder]               # skills from Ergo's library
  exclude: [skillbuilder]               # leave out some default skills
```

`history` and `workers` are always loaded everywhere. Skills marked `always_load` are
loaded in every chat. Everything else waits for `ergo_skill_load`.

Load a skill from the start when the chat uses it on most turns; leave it
lazy when it's occasional. A main chat that mostly delegates needs little
beyond `orchestration`.

## Plugin skills

A plugin is one skill. Its tools, `context_sources` and `skill_instructions`
arrive when it loads; `always_context_sources` stay on regardless (the KB
plugin keeps `kb/index.md` and prefetched articles in context this way).
`skill_hint(ctx)` is the one-line note shown in the list while unloaded.
See [writing a plugin](plugins.md#writing-a-plugin).
