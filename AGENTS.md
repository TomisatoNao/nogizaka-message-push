# Repository Agent Instructions

## Frontend work

Before changing anything under `src/webui_static/`, read:

- [`docs/frontend-design-guidelines.md`](docs/frontend-design-guidelines.md) — mandatory design and implementation rules;
- [`docs/frontend-design-optimization-plan.md`](docs/frontend-design-optimization-plan.md) — current optimization priorities and scope boundaries.

Frontend agents must preserve existing HTML IDs, API paths, configuration semantics and event entry points unless the task explicitly authorizes a contract change. Prefer shared theme tokens and component classes over new hard-coded colors, layout inline styles or page-specific overrides. Validate both light/dark themes and desktop/mobile layouts, then run the relevant tests and static checks before reporting completion.

Do not treat a screenshot-only difference as permission to change behavior, data, backend APIs or configuration schema. If a proposed visual change needs to cross those boundaries, stop and document the required separate plan.
