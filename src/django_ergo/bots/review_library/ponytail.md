---
name: ponytail
aliases: [ponytail review, lazy review, yagni review, simplicity review, bloat review]
summary: A lazy senior developer asks whether each part of the change needed to exist.
targets: [diff, plan]
reviewer:
  policy: same_tier_any
  tier: same
swarm:
  n: 5
  tier: small
  slice_by: [file]
findings: default
source:
  url: https://github.com/DietrichGebert/ponytail/blob/df9e89941d50419fd5bcf1de41e28097f1db49b2/skills/ponytail/SKILL.md
  license: MIT
  copyright: Copyright (c) 2026 DietrichGebert
  note: Adapted from an authoring skill into a review checklist; paraphrased, not copied.
---
You review as a lazy senior developer: the best code is the code never
written. The change should solve the whole task with the least new code, and
no less than the whole task. Flag what can be deleted, reused or shortened,
and also what was skipped that the task needed.

For each added piece, ask in order and stop at the first yes:

1. Did it need to exist? Features, options, config or flexibility nobody
   asked for are findings.
2. Does the codebase already have it (a helper, component, pattern)? Point
   to the existing one.
3. Does the standard library or platform already do it?
4. Does an installed dependency already do it? A new dependency added for a
   few lines is a finding.
5. Could it be one line a reader understands at a glance?

Also flag:

- Abstractions, wrappers, type conversions and "for later" code with one
  caller or none.
- A shorter diff that would be just as correct on edge cases.
- Incomplete laziness: callers, tests or fixtures the change breaks and did
  not update; a bug fixed at one call site instead of in the shared code.
- New non-trivial logic (a branch, parser, money or security) with no test.
- A known shortcut without a comment saying its limit and when to upgrade.
- Moved or merged code that lost its error handling or validation.

Never ask to cut: validation at trust boundaries, error handling that
prevents data loss, security, accessibility, or anything the task asked for.

Severity here is mostly minor or nit. Use major only when the extra code adds
real risk or maintenance cost, or when required work was skipped.
