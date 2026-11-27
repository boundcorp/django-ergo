---
name: simplicity
aliases: [delete more, deletion review]
summary: Challenge unnecessary scope, abstractions and dependencies.
targets: [diff, plan]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [concern, risk]}
findings: default
---
Question whether the feature and each component must exist. Identify needless scope, abstractions and dependencies, while preserving all requested behavior, validation and security.
