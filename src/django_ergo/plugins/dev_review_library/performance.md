---
name: performance
aliases: [performance review, perf review]
summary: Find plausible latency and scaling bottlenecks.
targets: [diff, plan, app]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [concern, risk]}
findings: default
---
Check queries, latency, memory, contention and scaling. Quantify likely impact when possible and avoid speculative micro-optimization.
