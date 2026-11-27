---
name: correctness
aliases: [correctness review, bug review]
summary: Find broken invariants and failure cases.
targets: [diff, plan]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [concern, risk]}
findings: default
---
Check invariants, edge cases, concurrency, error handling and failure recovery. Trace realistic inputs and state transitions; give concrete evidence.
