---
name: test-adequacy
aliases: [test review, test coverage]
summary: Find missing coverage and tests that do not prove behavior.
targets: [diff, plan]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [concern, risk]}
findings: default
---
Check meaningful coverage of changed behavior, failure paths, boundaries and regressions. Flag tests that pass without proving the claimed behavior.
