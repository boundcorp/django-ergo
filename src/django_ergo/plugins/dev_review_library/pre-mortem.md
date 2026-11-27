---
name: pre-mortem
aliases: [premortem, pre-mortem review]
summary: Identify plausible failure chains before production.
targets: [plan, diff, app]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [concern, risk]}
findings: default
---
Assume the change failed in production. Trace plausible causal chains, detection gaps and mitigations; distinguish evidence from assumptions.
