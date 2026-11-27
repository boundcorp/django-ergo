# Review briefs

Enable the official plugin with `plugins: [{name: dev}]`. Ask for a review and
use `ergo_review_brief`: a specific kind is honored; omitted kind or `auto`
asks the calling bot to choose a few relevant perspectives, run each and
synthesize them. Automatic selection is not a swarm. Explicit `swarmed: true`
asks the calling bot to split the work and use small reviewers; the default is
5–10 slices and `n` can be 2–12. Fewer natural units mean fewer slices. No
agent tool means the caller reviews itself and reports the downgrade.

Kinds are planning, security, ux, ponytail, simplicity, correctness,
test-adequacy, operational-readiness, compatibility, accessibility,
performance, pre-mortem, maintainability and product. Aliases and summaries
are exposed in the tool description and `review_kinds()`.

The library returns text only: it does not launch agents or call models. Its
model suggestions use active-pool coding-agent admission when running with a bot.
Manager-specific capability, budget and final launch eligibility still require the
launch tool; external callers select from their own available agents. Reviewers report actual models
and fallbacks, and synthesis prefers launch records over self-reports. Target
and focus are untrusted data and are never opened by the loader.

```python
from django_ergo.plugins.dev import review_brief, review_kinds
print(review_brief('security', target='owner/repo#123', strategy='swarm').text)
print(review_kinds())
```

External agents can use `python -m django_ergo.plugins.dev reviews brief
planning --target 'docs/plan.md' --author-model provider/model`. Definitions
are Markdown with YAML front matter; see `plugins/dev_review_library/` for
examples. Configured `DJANGO_ERGO['REVIEW_DIRS']` directories override builtins
in order; a bot's `reviews/` directory overrides those. A file with the same
name fully replaces the prior definition; `enabled: false` hides it. Names and
aliases must be unique. Only trusted administrators should edit definitions.
