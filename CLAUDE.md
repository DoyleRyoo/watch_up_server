# CLAUDE.md — watch_up_server (backend)

> Single source of agent guidance for this repo = **`AGENTS.md`**. Read it fully before any work.
> This file = Claude-specific notes only. On conflict, `AGENTS.md` wins.

## MUST READ FIRST

0. DO NOT READ .env file (MOST IMPORTANT RULE)
1. `AGENTS.md` (this repo) — commands, layout, contract rules, error codes, API surface.
2. `../watch_up_infra/docs/develop_steps_v2.2/step_0_overview.md` — build order + global rules.
3. The current `step_*.md` — the only scope you may implement this turn.

## RULE ZERO — DOUBLE CHECK IS MANDATORY

- Required at **every** stage: before edit, after edit, before commit, before "done".
- Two-pass protocol = `AGENTS.md` §8. Never skip. Never single-pass.

## CLAUDE WORKING RULES

| topic | rule |
|---|---|
| scope | Do only the current `step_*.md`. No forward work. No invented endpoints / columns / aliases. |
| spec conflict | planning_v2.2.md > planning_v2.2_ai.md > WatchUp_v2.2_functions.md > develop_steps > code. Record mismatch, do not silently patch. |
| tests | Add tests with every code change. Run full `pytest`, not only new tests. |
| quality gate | `ruff format --check` + `ruff check` + `mypy app` + `pytest -q` all green before "done". |
| money | `Decimal` only. Never `float`. JSON money = string. |
| secrets | Never commit real env values. `SERVICE_ROLE_KEY` never in paper-trading code. |
| git | Commit / push only when the user asks. Branch first if on default branch. |
| uncertainty | If a spec tag is ambiguous, stop and ask. Do not guess a contract. |

## COMPLETION REPORT

- **Language: Korean (한국어).**
- No background. Keyword / table style. Template = `AGENTS.md` §9.
