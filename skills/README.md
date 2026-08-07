# Agent skills

This repository publishes one portable [Agent Skill](https://agentskills.io/specification): [`bursar`](bursar/SKILL.md).

The skill guides coding agents through Bursar integration and review work. It contains only agent-specific procedure, source lookup order, financial invariants, and verification gates. Tutorials and API details remain in the [Bursar documentation](https://zonastery.github.io/bursar/docs/) so maintainers update each fact in one place.

## Install

Install the Bursar skill at project scope:

```bash
npx skills add zonastery/bursar@bursar
```

Install it globally only when every local project should use the skill:

```bash
npx skills add zonastery/bursar@bursar --global
```

Update project-scoped skills after a release:

```bash
npx skills update --project
```

Review the [Skills CLI](https://github.com/vercel-labs/skills) documentation for supported agents and non-interactive options.
