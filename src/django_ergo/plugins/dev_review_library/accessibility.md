---
name: accessibility
aliases: [a11y, accessibility review]
summary: Find barriers to accessible use.
targets: [app, diff, plan]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [concern, risk]}
findings: default
---
Check keyboard access, screen readers, focus, contrast, reduced motion and labels. Describe affected users, flows and evidence.
