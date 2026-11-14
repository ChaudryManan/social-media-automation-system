# PROGRESS — Live status and handoff

> **This file is the project's memory.** Any agent (Claude Code, Codex, Cursor, a human) must be able to read only this file + `AGENTS.md` and know exactly where to continue.
> Update it at the end of EVERY session (see `AGENTS.md` section 6). Keep it short and current; history goes in the Handoff log.

---

## 1. NEXT ACTION (one precise step)

> **Start task `B1-05` (Auth endpoints) in `TASKS.md`.**
> Implement register, login, refresh, logout, GET /auth/me, password hashing, JWT access tokens, and hashed refresh tokens per `PRD.md` FR-1.1 and `DATABASE.md` 4.1-4.2.

Last updated: 2026-11-14 · Last agent: Antigravity · Branch: `main` · Last commit: `B1-04: database and first migration`

## 2. How to resume (any agent)

1. Read `AGENTS.md` (rules), then this file, then the task in `TASKS.md`.
2. `git status`, `git log -5 --oneline`, `make test` (baseline).
3. Do the **NEXT ACTION** above. One task at a time.
4. Before stopping: lint, test, commit, tick `TASKS.md`, update sections 1, 3, 4, 5, 8 here and append to the Handoff log (section 9).

## 3. Current state

| Phase | Scope | Status | Done / Total |
|---|---|---|---|
| Docs | PRD, ARCHITECTURE, DATABASE, TASKS, AGENTS, PROGRESS, AGENT_SPECS | Done | 7 / 7 |
| B1 | Foundation: auth, roles, workspaces, limits | In progress | 4 / 11 |
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

*(Nothing in progress.)*

## 5. Recently completed (newest first, keep last ~10)

- 2026-11-14 — B1-04 completed: Database and first migration. Added SQLAlchemy 2.0 sync engine + Base + session dependency (`core/db.py`), 6 foundation models (`users`, `refresh_tokens`, `user_limits`, `workspaces`, `audit_logs`, `system_settings`), Alembic configuration and initial migration (`0001_initial_schema`), `make migrate`/`downgrade`/`migration` targets, model unit tests and migration upgrade/downgrade integration test (23 tests total passing). Resolved host IPv6 port resolution delay by standardizing on `127.0.0.1:15432`.
- 2026-11-14 — B1-03 completed: Core infrastructure with typed pydantic-settings, structured JSON logging with request-ID propagation, AppError hierarchy with global exception handlers (including 422 mapping), and CORS middleware.
- 2026-11-13 — B1-02 completed: Compose services and healthchecks, minimal FastAPI liveness/readiness endpoints, health tests, and Windows-friendly first-start timing.
- 2026-11-13 — B1-01 completed: repository package scaffold, pinned Python tooling, project-local `.venv` Make targets, environment template, and placeholder test.
- 2026-11-13 — `AGENT_SPECS.md` added (per-agent schemas, checks, prompt rules, re-entry table, test fixtures); referenced from AGENTS, TASKS (B2, B4) and ARCHITECTURE.
- 2026-11-13 — Planning docs written (PRD, ARCHITECTURE, DATABASE, TASKS, AGENTS, PROGRESS, CLAUDE).

## 6. Environment and how to run

*(Fill in as the project takes shape.)*
- Python: 3.11+ · DB: PostgreSQL 16 · Queue: Redis + Celery/Beat · Agents: LangGraph.
- Local run: `docker compose up` (after B1-02). Tests: `make test` (offline, fake LLM, mock publisher).
- Default dev env values: `LLM_PROVIDER=fake`, `PUBLISHER_MODE=mock`, fake image/video/TTS/search providers.
- Windows: GNU Make 4.4.1 installed via winget; `make` creates and uses the project-local `.venv` with Python 3.13. Docker Desktop with WSL2 is required from B1-02.
- Environment notes: Host port 5432 is blocked on this Windows machine (reserved/in-use by a local PostgreSQL service PID 4980), so Postgres is published on 127.0.0.1:15432 for host-side development (`postgresql+psycopg://sma:sma@127.0.0.1:15432/sma`). Never connect to port 5432 on the host machine. Always use IPv4 `127.0.0.1` rather than `localhost` to avoid Windows IPv6 resolution latency. Test database URL is configurable via `TEST_DATABASE_URL` (default `postgresql+psycopg://sma:sma@127.0.0.1:15432/sma_test`). Inside Docker Compose, the database host remains `postgres:5432`.

## 7. Decisions log (append-only; newest first)

| Date | ID | Decision | Why | Docs updated |
|---|---|---|---|---|
| 2026-11-14 | B1-04-1 | Use text columns with CHECK constraints (`SQLAlchemy Enum(native_enum=False, create_constraint=True)`) instead of native Postgres enums for `user_role` and `token_purpose`. | Avoids PostgreSQL `ALTER TYPE` migration complexities and keeps enums portable and easy to maintain. | DATABASE.md, backend/app/models |
| 2026-11-14 | B1-04-2 | Store emails lowercased in application code and enforce uniqueness via a unique functional index on `lower(email)` instead of using `citext`. | Avoids requiring PostgreSQL `citext` extension while guaranteeing strict case-insensitive uniqueness. | DATABASE.md, backend/app/models |
| 2026-11-13 | B1-02 | Use generous first-start healthcheck windows: PostgreSQL gets a 120 s start period and 30 retries; Redis 30 s/20; API 60 s/20; worker and beat 30 s. | A fresh Windows Docker Desktop machine can initialize PostgreSQL and build/start the application slowly. | docker-compose |
| 2026-11-13 | B1-02 | Build one shared `social-media-automation:dev` application image for `api`, `worker`, and `beat`; install only their currently needed pinned runtime packages in the B1 image. | Avoids three concurrent identical builds and the unused future LangGraph stack during this minimal foundation task. | Dockerfile, docker-compose |
| 2026-11-13 | B1-01 | Keep `pyproject.toml` and `tests/` at the repository root, rather than under `backend/`. | This matches the established scaffold and lets project tooling discover them from the root. | ARCHITECTURE |
| 2026-11-13 | D1–D8 | See `ARCHITECTURE.md` section 14 (scheduler as service + job table, LangGraph interrupts, generate once per platform, mock publishers first, sync SQLAlchemy, `checked` factory, no tokens in state, status via `transition()`) | Backend-first plan with offline-testable pipeline | ARCHITECTURE |
| 2026-11-13 | — | Stack: FastAPI, LangGraph, PostgreSQL, Redis + Celery; frontend (React/Next.js) only after Backend Core Complete | Project owner's choice | PRD, ARCHITECTURE |
| 2026-11-13 | — | A workspace has exactly one owner (no team members in v1); one social account connected once per workspace | Keeps v1 scope small | PRD |

*Pending decisions to make when reached:* enum strategy (PG enum vs text+CHECK) in B1-04; first real LLM/image/video/TTS/search providers; email provider.

## 8. Known issues, blockers, open questions

**Known issues / tech debt:**
- The Dockerfile currently installs Celery, FastAPI, psycopg, pydantic, pydantic-settings, Redis, and Uvicorn. Any task that adds a runtime dependency must also update the Dockerfile.
- Worker and Beat healthchecks are simple Redis pings until the Celery app exists in B3-06.

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

### 2026-11-14 — Antigravity — B1-04
- Did: Implemented SQLAlchemy 2.0 Base, engine, and session management (`core/db.py`); created 6 foundation ORM models (`users`, `refresh_tokens`, `user_limits`, `workspaces`, `audit_logs`, `system_settings`) with text CHECK enums, email lowercasing validator, and partial unique indexes; configured Alembic (`alembic.ini`, `backend/alembic/env.py`) with initial migration `0001_initial_schema`; added `make migrate`/`downgrade`/`migration` targets; added unit tests (`test_models.py`) and migration upgrade/downgrade integration test (`test_migrations.py`); identified and resolved Windows IPv6 resolution latency by standardizing on `127.0.0.1:15432`; added FK index on `refresh_tokens.user_id` and aligned `user_limits` documentation.
- State: tests pass (23 passed, 1 third-party deprecation warning, ~13s execution), lint clean (`make lint`), branch `main`.
- Left undone: nothing for B1-04.
- Next: start task B1-05 (Auth endpoints) in `TASKS.md`: register, login, refresh, logout, GET /auth/me; password hashing; JWT access tokens; hashed refresh tokens.
- Watch out: Host port 5432 is in-use on this machine; Docker publishes Postgres on 127.0.0.1:15432. Always use 127.0.0.1 (not localhost) for host connections. When adding runtime dependencies, remember to update Dockerfile.

### 2026-11-14 — Antigravity — B1-03
- Did: Implemented `core/config.py` using `pydantic-settings` with environment overrides and CORS parsing; added structured JSON logging and `RequestIDMiddleware` (`X-Request-ID` extraction/generation and context propagation); added `AppError` hierarchy and global exception handlers for `AppError`, `RequestValidationError`, `HTTPException`, and unhandled errors; configured CORS middleware; synced runtime dependencies in `Dockerfile`; added unit tests for config, error handling, and middleware (16 passing tests total).
- State: tests pass (16 passed, 1 third-party deprecation warning), lint clean (`make lint`), branch `main`.
- Left undone: nothing for B1-03.
- Next: start task B1-04 (Database and first migration) in `TASKS.md`.
- Watch out: Remember to sync `Dockerfile` runtime dependencies whenever new packages are added.

### 2026-11-14 — Antigravity — B1-02 verification
- Did: Verified git status, lint, and test suite baseline; confirmed `tests/unit/test_health.py` is tracked by git in commit `0f34c16`; verified healthcheck timings in `docker-compose.yml` match specifications across all 5 services; confirmed `TASKS.md` has B1-02 marked complete.
- State: tests pass (4 passed, 1 third-party deprecation warning), lint clean, branch `main`, last commit `0f34c16 B1-02: docker compose and health endpoints`.
- Left undone: nothing for B1-02.
- Next: start B1-03 (Core infrastructure) in `TASKS.md`: add settings, structured logging with request IDs, application error hierarchy/handler, and configured CORS.
- Watch out: User manages Docker Compose locally; do not run docker/docker compose commands. No live network in default tests.

### 2026-11-13 — Codex — B1-02
- Did: Added the shared Docker Compose application image, five services and healthchecks, minimal FastAPI liveness/readiness endpoints, health tests, and `make dev` stack startup; increased first-start healthcheck windows.
- State: user verified `api`, `worker`, `beat`, `postgres`, and `redis` healthy, with `/health` and `/health/ready` returning OK; tests pass (4 passed, 1 third-party deprecation warning), lint clean, branch `main`.
- Left undone: nothing.
- Next: start B1-03 by adding settings, structured logging, application errors, and CORS.
- Watch out: The first Docker image build and PostgreSQL initialization are slow on this Windows machine; timing allowances are in Compose. Worker and Beat currently check Redis only until B3-06 supplies the Celery app.

### 2026-11-13 — Codex — B1-02 (partial)
- Did: Added the Compose stack, shared app image Dockerfile, `/health` and `/health/ready`, and health endpoint tests; changed `make dev` to start Compose.
- State: tests pass (4 passed, 1 third-party deprecation warning), lint clean, Compose config valid, branch `main`, last commit `3b23212 B1-01: align layout docs and add missing packages`.
- Left undone: build the shared image, start and verify all Compose services, then confirm `/health/ready` returns 200.
- Next: resume the B1-02 Docker build and stack verification; do not commit until it passes.
- Watch out: BuildKit successfully pulled the base image but did not finish the pinned Python dependency install after more than ten minutes; no containers or volumes were created.

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
