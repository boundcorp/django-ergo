---
name: compatibility
aliases: [compatibility review, upgrade review]
summary: Check contracts and upgrade compatibility.
targets: [diff, plan]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [concern, risk]}
findings: default
---
Check API and data contracts, old clients, migrations and upgrades. Identify concrete breakage and safe transition paths.
