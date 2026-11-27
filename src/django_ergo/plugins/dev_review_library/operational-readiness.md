---
name: operational-readiness
aliases: [operations review, ops review]
summary: Assess observability, rollback and production failure handling.
targets: [diff, plan]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [concern, risk]}
findings: default
---
Check monitoring, alerts, rollout, rollback, migrations and partial failures. Identify practical recovery steps and operational gaps.
