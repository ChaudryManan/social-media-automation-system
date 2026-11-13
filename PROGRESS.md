# PROGRESS — Live status and handoff

> **This file is the project's memory.** Any agent (Claude Code, Codex, Cursor, a human) must be able to read only this file + `AGENTS.md` and know exactly where to continue.
> Update it at the end of EVERY session (see `AGENTS.md` section 6). Keep it short and current; history goes in the Handoff log.

---

## 1. NEXT ACTION (one precise step)

> **Start task `B1-02` (Docker Compose) in `TASKS.md`.**
> Add the `api`, `worker`, `beat`, `postgres`, and `redis` Compose services with healthchecks, then implement `GET /health` and `GET /health/ready`.

Last updated: 2026-11-13 · Last agent: Codex · Branch: `main` · Last commit: `B1-01: scaffold repo and tooling`

## 2. How to resume (any agent)

1. Read `AGENTS.md` (rules), then this file, then the task in `TASKS.md`.
2. `git status`, `git log -5 --oneline`, `make test` (baseline).
3. Do the **NEXT ACTION** above. One task at a time.
4. Before stopping: lint, test, commit, tick `TASKS.md`, update sections 1, 3, 4, 5, 8 here and append to the Handoff log (section 9).

## 3. Current state

| Phase | Scope | Status | Done / Total |
|---|---|---|---|
| Docs | PRD, ARCHITECTURE, DATABASE, TASKS, AGENTS, PROGRESS, AGENT_SPECS | Done | 7 / 7 |
| B1 | Foundation: auth, roles, workspaces, limits | In progress | 1 / 10 |
| B2 | Agent engine (post flow) | Not started | 0 / 13 |
| B3 | Core pipeline end-to-end (Backend Core Complete) | Not started | 0 / 18 |
| B4 | Video flow | Not started | 0 / 10 |
| B5 | Remaining backend (library, admin, more platforms) | Not started | 0 / 13 |
| B6 | Frontend integration and delivery | Not started | 0 / 7 |

Milestones:
- [ ] **Backend Core Complete** (end of B3): the 10-step scenario in `PRD.md` section 12 passes offline.
- [ ] Video flow complete (end of B4)
- [ ] Admin and platforms complete (end of B5)
- [ ] Frontend integrated, demo ready (end of B6)

## 4. In progress

*(Nothing yet. When a task is partly done, write here: task ID, what is finished, what remains, files touched, any uncommitted/WIP branch.)*

## 5. Recently completed (newest first, keep last ~10)

- 2026-11-13 — B1-01 completed: repository package scaffold, pinned Python tooling, project-local `.venv` Make targets, environment template, and placeholder test.
- 2026-11-13 — `AGENT_SPECS.md` added (per-agent schemas, checks, prompt rules, re-entry table, test fixtures); referenced from AGENTS, TASKS (B2, B4) and ARCHITECTURE.
- 2026-11-13 — Planning docs written (PRD, ARCHITECTURE, DATABASE, TASKS, AGENTS, PROGRESS, CLAUDE).

## 6. Environment and how to run

*(Fill in as the project takes shape.)*
- Python: 3.11+ · DB: PostgreSQL 16 · Queue: Redis + Celery/Beat · Agents: LangGraph.
- Local run: `docker compose up` (after B1-02). Tests: `make test` (offline, fake LLM, mock publisher).
- Default dev env values: `LLM_PROVIDER=fake`, `PUBLISHER_MODE=mock`, fake image/video/TTS/search providers.
- Windows: GNU Make 4.4.1 installed via winget; `make` creates and uses the project-local `.venv` with Python 3.13. Docker Desktop with WSL2 is required from B1-02.

## 7. Decisions log (append-only; newest first)

| Date | ID | Decision | Why | Docs updated |
|---|---|---|---|---|
| 2026-11-13 | B1-01 | Keep `pyproject.toml` and `tests/` at the repository root, rather than under `backend/`. | This matches the established scaffold and lets project tooling discover them from the root. | ARCHITECTURE |
| 2026-11-13 | D1–D8 | See `ARCHITECTURE.md` section 14 (scheduler as service + job table, LangGraph interrupts, generate once per platform, mock publishers first, sync SQLAlchemy, `checked` factory, no tokens in state, status via `transition()`) | Backend-first plan with offline-testable pipeline | ARCHITECTURE |
| 2026-11-13 | — | Stack: FastAPI, LangGraph, PostgreSQL, Redis + Celery; frontend (React/Next.js) only after Backend Core Complete | Project owner's choice | PRD, ARCHITECTURE |
| 2026-11-13 | — | A workspace has exactly one owner (no team members in v1); one social account connected once per workspace | Keeps v1 scope small | PRD |

*Pending decisions to make when reached:* enum strategy (PG enum vs text+CHECK) in B1-04; first real LLM/image/video/TTS/search providers; email provider.

## 8. Known issues, blockers, open questions

**Known issues / tech debt:** *(none)*

**Blocked (`[!]` tasks):** *(none)*

**Open questions for the human:**
1. Which LLM, image, video and TTS providers should be used first (all behind interfaces; fake by default)?
2. Which platform gets the first real publisher? (default plan: Facebook Pages, task B3-16.)
3. Do the developer apps/accounts for Meta, Google and TikTok exist yet? (Needed only for live tests; not for B1–B3 offline work.)

## 9. Handoff log (append-only, newest first)

Template — copy for each session:

```
### YYYY-MM-DD HH:MM — <agent/tool> — <task IDs touched>
- Did: <what changed, 1–4 bullets>
- State: tests <pass/fail + counts>, lint <ok/fail>, branch <name>, last commit <hash + message>
- Left undone: <exact remaining work inside the task, or "nothing">
- Next: <the precise next action — also copied to section 1>
- Watch out: <gotchas, flaky tests, decisions the next agent must know>
```

### 2026-11-13 — Codex — B1-01
- Did: Created the documented package skeleton, pinned `pyproject.toml` dependencies, ruff/pytest settings, a Windows-compatible `.venv` Makefile, `.env.example`, ignore and line-ending rules, README, and an offline placeholder test.
- State: tests pass (1 passed), lint clean, branch `main`, last commit `B1-01: scaffold repo and tooling`.
- Left undone: nothing.
- Next: start B1-02 by adding the Docker Compose stack and health endpoints.
- Watch out: GNU Make was installed for the current user via winget. The pre-existing `PROGRESS.md` last-commit field was stale; the confirmed repository began at `e190975` (`adding project files`).

### 2026-11-13 — Claude (chat) — planning
- Did: Wrote `PRD.md`, `ARCHITECTURE.md`, `DATABASE.md`, `TASKS.md`, `AGENTS.md`, `PROGRESS.md`, `CLAUDE.md`. Project split into modules M0–M15 and phases B1–B6 (backend first).
- State: no code or repo yet.
- Left undone: everything in `TASKS.md`.
- Next: start `B1-01`.
- Watch out: create the repo with these docs at the **repo root** (not inside `backend/`); `AGENTS.md` and `CLAUDE.md` must stay at the root so Codex and Claude Code find them.
