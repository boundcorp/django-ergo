---
name: maintainability
aliases: [tech-debt, tech debt, dry, cleanup, tech debt review, cleanup review]
summary: Find debt and duplication worth simplifying safely.
targets: [diff, plan]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [file, concern]}
findings: default
---
Review duplication, dead code, needless abstractions and maintenance debt. Explain the cost and payoff of each proposed simplification, and check behavior, tests and migration risks. Avoid DRY at all costs: small, clear duplication may be better than a coupling or abstraction. Preserve necessary validation and error handling.
