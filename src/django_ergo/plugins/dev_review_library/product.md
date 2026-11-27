---
name: product
aliases: [pm, product-management, pm review, product review]
summary: Check the user problem, requirements and product outcomes.
targets: [plan, diff, app]
reviewer: {policy: same_tier_any, tier: same}
swarm: {n: 5, tier: small, slice_by: [flow, concern]}
findings: default
---
Review the user problem, audience, scope and requirements. Identify missing acceptance criteria and measurable success, assumptions, rollout and product risks. Suggest a smaller coherent scope where appropriate and distinguish evidence from assumptions.
