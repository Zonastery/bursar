# Skills in this repository

This repository ships one Agent Skill:

| Skill                       | Purpose                                                                                                                                                                                                                                                                                                       |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`bursar`](bursar/SKILL.md) | Integrate Bursar, the credit billing engine for AI SaaS, into a Python or TypeScript application: usage metering, prepaid credits, the credit ledger, plans and allowances, subscriptions, quotas, spend caps, leases, idempotent billing, pricing configs and rate cards, auto-recharge, and billing events. |

The `bursar` skill follows the [Agent Skills](https://agentskills.io/specification)
format: YAML frontmatter (`name`, `description`, `license`, `compatibility`,
`metadata`) plus markdown instructions — the mental model, hard money
invariants, the standard install → tenant → config → facade → metering
workflow, side-by-side Python and TypeScript recipes, and one-level-deep
`references/*.md` pointers into the full docs at
https://zonastery.github.io/bursar/docs/.

## Installing the skill

Install all skills from this repository:

```bash
npx skills add zonastery/bursar
```

Install only the `bursar` skill:

```bash
npx skills add zonastery/bursar --skill bursar
```

Skills are installed at **project scope** by default. Pass `--global` to
install them for all projects:

```bash
npx skills add zonastery/bursar --global
```
