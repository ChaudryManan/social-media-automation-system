# PROGRESS — Live status and handoff

> **This file is the project's memory.** Any agent (Claude Code, Codex, Cursor, a human) must be able to read only this file + `AGENTS.md` and know exactly where to continue.
> Update it at the end of EVERY session (see `AGENTS.md` section 6). Keep it short and current; history goes in the Handoff log.

---

## 1. NEXT ACTION (one precise step)

> **Start task `B2-02` (migration for `workflow_runs` and `agent_steps`) in `TASKS.md`. `B2-01` is done.**
> Add Alembic migration `0002` (never edit `0001`), SQLAlchemy models in `backend/app/models/` and a small service (e.g. `services/runs.py`) that creates a run and records one `agent_steps` row per attempt, exactly as in `DATABASE.md` 4.10 and 4.11 (text+CHECK enums `run_status`/`step_verdict`, UUID PKs, UTC timestamps, indexes from section 5, `agent_steps.run_id` FK `ON DELETE CASCADE`). Note `workflow_runs.request_id` references `content_requests`, which only arrives in B3: create `request_id` as a plain indexed UUID column now and add the FK in the B3 migration (record this in Decisions), or ask the human. The step recorder should accept the numbers that `llm.capture_usage()` already provides (`tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `models`). Done when: inserting and querying a run with steps works (needs Postgres on 15432; start it with `docker compose up -d postgres redis`), migration upgrade/downgrade test passes. Do not start B2-03 in the same change.

Last updated: 2026-11-17 · Last agent: Claude (chat, via cloned repo) · Branch: `main` · Last commit: `B2-01: LLM wrapper with scripted fake provider`

## 2. How to resume (any agent)

1. Read `AGENTS.md` (rules), then this file, then the task in `TASKS.md`.
2. `git status`, `git log -5 --oneline`, `make test` (baseline).
3. Do the **NEXT ACTION** above. One task at a time.
4. Before stopping: lint, test, commit, tick `TASKS.md`, update sections 1, 3, 4, 5, 8 here and append to the Handoff log (section 9).

## 3. Current state

| Phase | Scope | Status | Done / Total |
|---|---|---|---|
| Docs | PRD, ARCHITECTURE, DATABASE, TASKS, AGENTS, PROGRESS, AGENT_SPECS | Done | 7 / 7 |
| B1 | Foundation: auth, roles, workspaces, limits | Done | 11 / 11 |
| B2 | Agent engine (post flow) | In progress | 1 / 13 |
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

- 2026-11-17 — B2-01 completed: LLM wrapper (`backend/app/agents/llm.py`). `get_chat_model(kind)` (provider and model names from settings; `fast` falls back to the text model), `generate_structured(schema, messages)` (Pydantic output, one repair retry, `LLMOutputError` after that), `capture_usage()` usage/cost/latency recorder, provider registry (`register_provider`) and a scripted `fake` provider (`FakeScript`: `queue`, `fail_next(n)`, `set_default`, `calls`). New shared fixture `fake_llm` in `tests/conftest.py`. +60 tests (335 passed, 101 skipped because Postgres/Redis are not reachable in the sandbox), `llm.py` 100% covered, 11 deliberate breakages of the code were each caught by the tests. No new dependency, no migration.
- 2026-11-15 — B1-11 completed: JSON access logs with request id (phase B1 done). New pure-ASGI `AccessLogMiddleware` writes one JSON line per request (logger `app.access`: `request_id`, `method`, `path`, `route`, `status_code`, `duration_ms`, `client_ip`; ERROR for 5xx/unhandled, WARNING if the client disconnects). Order is RequestID -> AccessLog -> CORS so preflights are logged. Uvicorn's access log is disabled (`--no-access-log` in compose and in `setup_logging`) and its startup lines now go through the JSON formatter. Fixed `alembic/env.py`, whose `fileConfig()` disabled every existing logger (the access log included) when migrations ran in-process. +30 tests (376 passed, 0 skipped), including a test that starts a real Uvicorn process and checks every output line is JSON.
- 2026-11-14 — B1-10 completed: B1 test sweep and OpenAPI check. `/docs` now lists each of the 15 operations once under `/api/v1` with tags, summaries, request/response examples and documented errors in the real `{"error": {...}}` envelope (`schemas/errors.py`, `api/docs.py::errors`); root aliases hidden from the schema. Coverage sweep added 138 tests (suite 208 -> 346 passed, 0 skipped with Postgres and Redis): OpenAPI contract tests, JWT edge cases, reset rate-limiter failing closed, HTTP end-to-end password reset, deleted/inactive user paths, rename race. Auth/workspaces/limits/audit modules are at 98-100% line coverage. Makefile made portable (`BOOTSTRAP_PYTHON`), verified from a clean clone: README steps, `make test`, `make lint`, `make migrate`, `make seed`, and a live server smoke test.
- 2026-11-14 — B1-09 completed: Limits and audit helper. `services/usage.py` (`get_effective_limits`: `user_limits` override, else `system_settings` default, else code fallback 3/5/100; `count_workspaces`; `ensure_can_create_workspace`), `services/audit.py::log` (adds an `audit_logs` row without committing; redacts sensitive `meta` keys). `WorkspaceService.create` locks the owner row and enforces `max_workspaces` (creating workspace number limit+1 gives 403 `workspace_limit_reached` with `details {limit, current}`); `WorkspaceService.soft_delete` now takes the actor and logs `workspace.delete` in the same transaction; `get_workspace_for_user` logs `workspace.admin_access` when an admin reaches someone else's workspace. 39 new tests (24 unit, 15 integration incl. a two-thread race test); full suite 208 passed.
- 2026-11-14 — B1-08 completed: Workspaces CRUD. `POST/GET /workspaces`, `GET/PATCH/DELETE /workspaces/{id}` (dual-mounted with `/api/v1`), schemas with validation (`schemas/workspaces.py`: trimmed name, IANA timezone, language tag, platform list, settings size cap, unknown fields rejected), `WorkspaceService` (`services/workspaces.py`: case-insensitive unique name per owner with DB-index race fallback, paginated own-list, partial update that can clear nullable fields, soft delete), `get_workspace_for_user` dependency in `core/deps.py` (owner or admin; anything else is 404). Delete requires `?confirm=true`. 79 new tests (35 unit, 44 integration); full suite 169 passed.
- 2026-11-14 — B1-07 completed: Password reset workflow. Implemented `/auth/forgot-password` and `/auth/reset-password` endpoints (dual-mounted with `/api/v1` prefix). Fixed-window rate limiting per-IP (5 / 10 min) and per-account (3 / 15 min) with 60s cooldown key via Redis (`core/ratelimit.py`), failing closed with HTTP 503 `ServiceUnavailableError`. Generic 200 response for forgot-password preventing account enumeration; single-use token semantics via `refresh_tokens` (`purpose='password_reset'`, `revoked_at`), row-level lock concurrency protection (`with_for_update()`), all active refresh tokens revoked upon reset. Added `BaseEmailSender` / `ConsoleEmailSender` (`providers/email.py`) dispatching reset URL without logging tokens. Full unit tests (21 tests in `test_password_reset_service.py`) and integration tests in `test_password_reset.py`.
- 2026-11-14 — B1-06 completed: RBAC and admin seed. Implemented `require_admin` dependency (`core/deps.py`), admin router with dummy authorization test route (`api/v1/admin.py`), idempotent seeding service (`services/seed.py`) with secure environment credential validation and automatic user promotion protection, 12 canonical system settings without overwriting customizations, and `make seed` Makefile target. Full unit and integration test coverage (`test_rbac.py`, `test_seed_service.py`, `test_admin_seed.py`).
- 2026-11-14 — B1-05 completed: Auth endpoints. Implemented Argon2id password hashing and PyJWT access token generation (`core/security.py`), bearer token auth dependency (`core/deps.py`), Pydantic schemas (`schemas/auth.py`), service layer (`services/auth.py`), and FastAPI routes (`api/v1/auth.py`) for `/auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`, and `/auth/me` (with dual `/api/v1/auth` mounting). Enforced inactive user blocks, case-insensitive email uniqueness, and token rotation. Full unit and integration test coverage (44 passing tests).
- 2026-11-14 — B1-04 completed: Database and first migration. Added SQLAlchemy 2.0 sync engine + Base + session dependency (`core/db.py`), 6 foundation models (`users`, `refresh_tokens`, `user_limits`, `workspaces`, `audit_logs`, `system_settings`), Alembic configuration and initial migration (`0001_initial_schema`), `make migrate`/`downgrade`/`migration` targets, model unit tests and migration upgrade/downgrade integration test (23 tests total passing). Resolved host IPv6 port resolution delay by standardizing on `127.0.0.1:15432`.
- 2026-11-14 — B1-03 completed: Core infrastructure with typed pydantic-settings, structured JSON logging with request-ID propagation, AppError hierarchy with global exception handlers (including 422 mapping), and CORS middleware.
- 2026-11-13 — B1-02 completed: Compose services and healthchecks, minimal FastAPI liveness/readiness endpoints, health tests, and Windows-friendly first-start timing.

## 6. Environment and how to run

*(Fill in as the project takes shape.)*
- Python: 3.11+ · DB: PostgreSQL 16 · Queue: Redis + Celery/Beat · Agents: LangGraph.
- Local run: `docker compose up` (after B1-02). Tests: `make test` (offline, fake LLM, mock publisher).
- Sandbox note (Claude chat sessions): Python is 3.12 and there is no Docker/Postgres/Redis, so use `make test BOOTSTRAP_PYTHON=python3`; the 101 integration tests then skip.
- Default dev env values: `LLM_PROVIDER=fake`, `PUBLISHER_MODE=mock`, fake image/video/TTS/search providers.
- Linux/macOS/CI: `make` creates the venv with `python3.13` (override with `make test BOOTSTRAP_PYTHON=python3`); Windows uses `py -3.13`. Integration tests need Postgres on 15432 and Redis (`docker compose up -d postgres redis`), otherwise they skip.
- Windows: GNU Make 4.4.1 installed via winget; `make` creates and uses the project-local `.venv` with Python 3.13. Docker Desktop with WSL2 is required from B1-02.
- Environment notes: Host port 5432 is blocked on this Windows machine (reserved/in-use by a local PostgreSQL service PID 4980), so Postgres is published on 127.0.0.1:15432 for host-side development (`postgresql+psycopg://sma:sma@127.0.0.1:15432/sma`). Never connect to port 5432 on the host machine. Always use IPv4 `127.0.0.1` rather than `localhost` to avoid Windows IPv6 resolution latency. Test database URL is configurable via `TEST_DATABASE_URL` (default `postgresql+psycopg://sma:sma@127.0.0.1:15432/sma_test`). Inside Docker Compose, the database host remains `postgres:5432`.

## 7. Decisions log (append-only; newest first)

| Date | ID | Decision | Why | Docs updated |
|---|---|---|---|---|
| 2026-11-17 | B2-01-1 | `llm.py` defines its own small `ChatModel` interface (`_generate()` + shared `invoke()`), not LangChain chat-model classes. No new runtime dependency, so the Dockerfile list is unchanged. | The fake provider and tests need no SDK; no Gemini/LangChain provider package is pinned in `pyproject.toml`; a LangChain-backed provider can still be adapted behind the interface. | ARCHITECTURE.md 6.6 |
| 2026-11-17 | B2-01-2 | Only `fake` is registered. `LLM_PROVIDER=gemini` (or anything else) raises `LLMConfigError` (`llm_not_configured`, details list the available providers). No real provider was written. | Open question 1 (which provider first) is unanswered, nothing live can be tested offline, and B2-01's Done-when only needs the fake. Adding one is `register_provider(name, factory)` plus tests with a mocked transport. | ARCHITECTURE.md 6.6 |
| 2026-11-17 | B2-01-3 | `.env.example` and ARCHITECTURE section 10 now default to `LLM_PROVIDER=fake` (was `gemini`). | A fresh `cp .env.example .env` selected a provider that does not exist yet and made every `get_chat_model()` call fail; AGENTS.md says development runs with the fake. Change it when a real provider lands. | ARCHITECTURE.md, .env.example |
| 2026-11-17 | B2-01-4 | `generate_structured` repairs only bad output (invalid JSON or schema mismatch, 1 retry). Provider failures are not retried there and surface as `LLMProviderError(retriable=...)`. | Retry/escalation and step logging belong to `make_checked_node` (B2-04); two retry loops would multiply calls and cost. | ARCHITECTURE.md 6.6, AGENT_SPECS.md 1.1 |
| 2026-11-17 | B2-01-5 | Prompts and model replies are never logged or put in error messages/details (`LLMOutputError.details` lists only schema name, attempt count and field-level problems without input values). Failed calls produce no usage record; repair calls count as separate calls. | AGENTS.md privacy rules; `agent_steps` must not hold full prompts. A test greps the logs for the prompt and reply text. | ARCHITECTURE.md 6.6 |
| 2026-11-17 | B2-01-6 | Tests that reach `get_chat_model()` use the `fake_llm` fixture (`tests/conftest.py`): sets `LLM_PROVIDER=fake`, blanks the model names, clears the `get_settings` cache and resets the shared script. | A developer's `.env` can select another provider; environment variables win over `.env`. | ARCHITECTURE.md 6.6 |
| 2026-11-15 | B1-11-1 | Access logging is a separate pure-ASGI `AccessLogMiddleware` (not an extension of the `BaseHTTPMiddleware`-based `RequestIDMiddleware`). Stack, outer to inner: RequestID -> AccessLog -> CORS. `duration_ms` runs until the app finishes sending the response. | Pure ASGI sees the real status, route template and failures without buffering responses; being inside RequestID it reads `request_id` from the logging context, and being outside CORS it also logs preflights (which now get an `X-Request-ID` header too). | ARCHITECTURE.md |
| 2026-11-15 | B1-11-2 | `JSONFormatter` copies only an allow-list of `extra=` fields (`STRUCTURED_FIELDS`), it does not dump every extra. Existing `extra=` values elsewhere stay unlogged as before. | Several call sites pass `email`/`to` (`services/auth.py`, `providers/email.py`, `services/seed.py`); dumping all extras would have started writing personal data to the logs. | ARCHITECTURE.md, AGENTS.md |
| 2026-11-15 | B1-11-3 | Every request is logged, health probes included (level INFO), and 4xx are INFO, not WARNING. Query string, headers and bodies are never logged; the path is capped at 2048 characters. | Matches the task ("one line per request"); 401/404 are routine client errors and would drown real warnings. | ARCHITECTURE.md, README.md |
| 2026-11-15 | B1-11-4 | `setup_logging` also disables `uvicorn.access` and routes `uvicorn`/`uvicorn.error` to the JSON root handler; compose runs the API with `--no-access-log` as well. | Output is JSON only and each request is logged once even if the flag is forgotten; both variants are tested against a real server process. | ARCHITECTURE.md, README.md |
| 2026-11-15 | B1-11-5 | `alembic/env.py` calls `fileConfig(..., disable_existing_loggers=False)` and only when the process has no root handlers yet (plain `alembic` CLI). | The default silently disabled all app loggers and replaced the JSON handler whenever migrations ran in-process (this broke the new tests in a full run). | - |
| 2026-11-14 | B1-10-1 | OpenAPI lists only the `/api/v1` routes. The root aliases (`/auth/...`, `/workspaces/...`, `/admin/...`) still work but use `include_in_schema=False`. `/health*` stay at the root, tag `health`. | ARCHITECTURE.md section 8 defines `/api/v1` as the API surface; registering each router twice showed every route twice in `/docs` and would double a generated client in B6-01. | ARCHITECTURE.md, README.md |
| 2026-11-14 | B1-10-2 | Errors are documented through `app/api/docs.py::errors(...)` and a shared `ErrorResponse` schema, replacing FastAPI's default 422 `HTTPValidationError` (`{detail: [...]}`), which did not match what the API really returns. | `/docs` must tell clients the truth; tests compare the documented codes with real responses. | ARCHITECTURE.md, AGENTS.md |
| 2026-11-14 | B1-10-3 | Examples live in `model_config["json_schema_extra"]` on request and response models, use placeholder values only, and `tests/unit/test_openapi.py` validates every example against its model. `null` cannot appear in an example (FastAPI strips it), so null-to-clear is described in the `WorkspaceUpdate` docstring. | Examples cannot rot or leak real credentials; the validation test caught a broken `{}` example. | ARCHITECTURE.md |
| 2026-11-14 | B1-10-4 | `Makefile` creates the venv with `$(BOOTSTRAP_PYTHON)`: `py -3.13` on Windows (unchanged), `python3.13` elsewhere, overridable. | A clean Linux clone failed at `py: No such file`; README promised `make test` works. | README.md |
| 2026-11-14 | B1-09-1 | Exceeding `max_workspaces` returns HTTP 403 with code `workspace_limit_reached` and `details {limit, current}` (via the existing `LimitExceededError`, which defaults to 400 but accepts a status). | A quota is a policy refusal, not a malformed request (400), a conflict (409, used for duplicate names) or a retry-later case (429). The stable `code` lets clients tell it apart from a permissions 403. | ARCHITECTURE.md, DATABASE.md |
| 2026-11-14 | B1-09-2 | Effective limit per field: user's non-null `user_limits` value, else `system_settings.default_*`, else code fallback (`FALLBACK_DEFAULTS` in `services/usage.py`, equal to the seeded values; a unit test keeps them in sync). `0` is valid; negative/non-int/bool values are ignored with a warning. | DATABASE.md 4.17 says defaults live in code; a missing or corrupted setting must not disable quotas. | DATABASE.md |
| 2026-11-14 | B1-09-3 | `audit.log(db, actor, action, entity_type, entity_id, meta, ip_address)` only adds the row (no commit) and redacts sensitive `meta` keys. The signature differs slightly from the sketch in TASKS (`db` explicit, `entity` split in two) to fit the sync-session style of the other services. It fills the existing `audit_logs.ip_address` column. | Entry and action commit atomically (tested); secrets can never reach the audit trail. | DATABASE.md |
| 2026-11-14 | B1-09-4 | Admin access to another user's workspace is audited inside `get_workspace_for_user` (`workspace.admin_access`, meta `owner_id`/`method`/`path`) for every method, including GET, and is committed there. `core/deps.py` therefore imports `services/audit`. | One choke point covers every present and future workspace-scoped route (ARCHITECTURE.md: "admin bypass explicit and audited"); a GET would otherwise lose the row on rollback. | ARCHITECTURE.md |
| 2026-11-14 | B1-09-5 | `WorkspaceService.create` runs `SELECT … FOR UPDATE` on the owner's `users` row before counting. `soft_delete(db, workspace, actor)` gained the `actor` argument. | Without the lock two simultaneous creates both pass the count check (the race test fails 3/3 without it). | DATABASE.md |
| 2026-11-14 | B1-08-1 | Workspace name uniqueness per owner is checked case-insensitively in the service (among non-deleted rows); the DB partial unique index stays case-sensitive as the race guard (IntegrityError maps to the same 409 `workspace_name_taken`). | "Brand" vs "brand" is confusing for users; no migration needed. | DATABASE.md |
| 2026-11-14 | B1-08-2 | A workspace that is missing, soft-deleted, or owned by someone else returns the same 404 `workspace_not_found` (admins may access any live workspace). `GET /workspaces` always lists only the caller's own workspaces, even for admins (an admin-wide list is `GET /admin/workspaces` in B5-10). | Does not reveal which ids exist; keeps the admin list task separate. | ARCHITECTURE.md |
| 2026-11-14 | B1-08-3 | Create/update schemas use `extra="forbid"`: `owner_id`, `is_suspended` and other unknown fields give 422 instead of being silently ignored. Delete without `?confirm=true` gives 400 `confirmation_required` (checked after authorization). | Prevents privilege/ownership tampering; makes API misuse visible. | DATABASE.md, ARCHITECTURE.md |
| 2026-11-14 | B1-08-4 | Added `tzdata==2025.2` as a runtime dependency (pyproject.toml and Dockerfile) for IANA timezone validation via `zoneinfo`. | Windows and slim images have no system timezone database, so every timezone check would fail without it. | pyproject.toml, Dockerfile |
| 2026-11-14 | B1-07-1 | Use Redis fixed-window rate limiter with per-IP (5 / 10m) and per-account (3 / 15m) counters plus a 60-second cooldown key (`SET NX EX 60`), failing closed with HTTP 503 `ServiceUnavailableError` when Redis is unreachable. | Prevents request flooding and account enumeration while strictly protecting the service against Redis outages. | PROGRESS.md, app/core/ratelimit.py |
| 2026-11-14 | B1-07-2 | Reuse `refresh_tokens` table for password-reset tokens (`purpose='password_reset'`, `revoked_at` for single-use, `expires_at` for TTL, `SELECT … FOR UPDATE` row-level lock concurrency protection) without adding a new database migration. | Existing migration `0001_initial_schema` already created `purpose` with CHECK constraint `('refresh', 'password_reset')` and `revoked_at` column. | DATABASE.md, app/services/auth.py |
| 2026-11-14 | B1-07-3 | On successful password reset, revoke all active refresh tokens for the account. Note: stateless JWT access tokens remain valid until expiration (`ACCESS_TOKEN_MINUTES=30`). | Standard security practice forcing re-login across devices; access tokens expire naturally due to short lifetime. | PROGRESS.md, app/services/auth.py |
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
- LLM layer (B2-01): model names come only from env settings (`LLM_MODEL_TEXT`/`LLM_MODEL_FAST`); the admin-editable `system_settings.model_text`/`model_fast` keys (DATABASE.md 4.17) are not read yet (the layer has no DB access on purpose). `cost_usd` is whatever the provider reports (fake: 0.0); there is no pricing table. `retry_model_upgrade` (AGENT_SPECS 1.4) is not implemented; it belongs to the checked node (B2-04) and needs the model choice per attempt.
- Health probes are logged like any request (docker healthcheck every 10 s). If that becomes noisy in production, add an exclusion list for `/health` and `/health/ready` to `AccessLogMiddleware` (setting in `core/config.py`).
- The access line has no `user_id` (authentication runs inside route dependencies, after the middleware starts). If needed later, have `get_current_user` store the id in `request.state` and read it when logging.
- `extra=` values on existing log calls (user ids, workspace ids, rate-limit counters) are still not written to the logs because of the allow-list (B1-11-2). Add individual safe fields to `STRUCTURED_FIELDS` deliberately if they are wanted.
- Test-suite hygiene found in B1-10 (not fixed, out of scope): (a) the `client` fixture in `tests/integration/test_password_reset.py` is unused and its rate-limit patch is a no-op (it patches `app.core.ratelimit`, but the router imports the function by name; patch `app.api.v1.auth.check_password_reset_rate_limits` as the new tests do); (b) every integration test file repeats ~70 lines of database setup, a shared `tests/conftest.py` would remove that; (c) `ruff format --check` still flags `services/seed.py` and `tests/integration/test_admin_seed.py` (pre-existing).
- Lines still uncovered in auth/workspaces code are intentional: the SQLite-only naive-datetime branch in `AuthService.refresh`, two defensive "user row missing" branches that the foreign-key cascade makes unreachable, and the Redis client constructor (always patched in tests).
- `docker compose` and `make dev` were not run in the sandbox (no Docker); the API was verified with uvicorn against local Postgres 16 and Redis instead.
- Every admin request to another user's workspace writes an `audit_logs` row (reads included, B1-09-4). Acceptable for v1 volumes; if admin browsing grows, consider sampling reads or a retention job.
- `max_accounts_per_workspace` and `monthly_generation_limit` are already resolved by `usage.get_effective_limits` but are enforced only later (B3-02 for accounts, B3-05 for requests). Changing limits by an admin (`PATCH /admin/users/{id}/limits`, B5-05) must call `audit.log`.
- The Dockerfile installs a pinned list of runtime packages by hand (including `tzdata` since B1-08). Any task that adds a runtime dependency must also update the Dockerfile, then rebuild the image with `docker compose build api`.
- Pending hook (B3-06): `WorkspaceService.soft_delete` must cancel the workspace's pending jobs and non-final requests once those tables exist (`DATABASE.md` section 6 rule 7). The reminder is in the B3-06 task text.
- Worker and Beat healthchecks are simple Redis pings until the Celery app exists in B3-06.

**Blocked (`[!]` tasks):** *(none)*

**Open questions for the human:**
1. Which LLM, image, video and TTS providers should be used first (all behind interfaces; fake by default)? For the LLM this now blocks only the real provider (`gemini` raises `llm_not_configured` until one is registered); B2 can be finished with the fake.
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

### 2026-11-17 — Claude (chat, cloned the public repo) — B2-01
- Did: LLM wrapper.
  - `backend/app/agents/llm.py`: `ChatModel` interface, provider registry, `get_chat_model(kind)`, `generate_structured(schema, messages, kind=, temperature=, model=)`, `capture_usage()`/`UsageRecorder`, error classes (`LLMError`, `LLMConfigError`, `LLMProviderError`, `LLMOutputError`, `FakeScriptExhaustedError`), scripted `FakeChatModel`/`FakeScript`.
  - `tests/conftest.py` (`fake_llm` fixture) and `tests/unit/test_llm.py` (60 tests: scripting, failures, model selection, JSON extraction, repair path, usage capture, no prompt/reply in logs or errors, thread safety).
  - Docs: `TASKS.md` (B2-01 ticked), `ARCHITECTURE.md` (6.6 new, section 10 default), `AGENT_SPECS.md` 1.1, `.env.example` (`LLM_PROVIDER=fake`), this file.
- State: `ruff check` clean; 335 passed, 101 skipped (no Postgres/Redis in the sandbox, same 101 as the baseline); `llm.py` 100% line coverage; 11 mutation checks all caught (one redundant code-fence branch the mutation showed to be dead was removed). Branch `main`; baseline before the change was 275 passed, 101 skipped. Docker not run.
- Left undone: nothing for B2-01. No real provider (see Decisions B2-01-2).
- Next: `B2-02` (migration for `workflow_runs` and `agent_steps`); see section 1.
- Watch out: `git log` HEAD before this session was `B-11-fix-slow-start-test` (contains the B1-11 work; the PROGRESS text was right, only the commit message differs). New tests that call `get_chat_model()`/`generate_structured()` must take the `fake_llm` fixture. Nodes should pass `temperature` per AGENT_SPECS 1.4 and wrap each attempt in `with capture_usage() as usage:` to fill `agent_steps`. Do not put prompts or replies into logs, `input_summary` or errors.

### 2026-11-15 — Claude (chat, cloned the public repo) — B1-11
- Did: JSON access logs with request id.
  - `core/middleware.py`: `AccessLogMiddleware` (one line per request, `app.access` logger). `core/logging.py`: `STRUCTURED_FIELDS` allow-list, `default=str`, and `setup_logging` now silences `uvicorn.access` and routes Uvicorn's other logs through the JSON handler. `main.py`: middleware order RequestID -> AccessLog -> CORS. `docker-compose.yml`: `--no-access-log` on the API command.
  - `backend/alembic/env.py`: `fileConfig` no longer disables existing loggers or replaces the app's handlers (root cause of 19 failures that only appeared in a full test run).
  - Tests (+30): `tests/unit/test_access_log.py` (fields, request id, levels, route template, duration, preflight, no secrets, path truncation and log-injection, ASGI edge cases, allow-list privacy guard, uvicorn logger setup, alembic regression) and `tests/unit/test_uvicorn_json_logs.py` (real Uvicorn process with and without `--no-access-log`; every output line must be JSON; compose flag check). Mutation-checked: 7 deliberate breakages of the implementation were each caught.
  - Docs: `TASKS.md` (B1-11 ticked), `ARCHITECTURE.md` section 11, `AGENTS.md` logging rule, `README.md` Logs section, this file.
- State: `ruff check` clean; 376 passed, 0 skipped (Postgres 16 on 15432 + Redis); `middleware.py` 100% covered. Branch `main`, last commit `B1-11: JSON access logs with request id`. Phase B1 is complete (11/11).
- Left undone: Docker was not available, so `docker compose up` (the container's actual log output) was not run; the same command line was run directly with `python -m uvicorn app.main:app --no-access-log` and every output line was valid JSON.
- Next: `B2-01` (LLM wrapper); read `AGENT_SPECS.md` sections 1-5 first.
- Watch out: do not add `extra=` fields to the allow-list if they can hold emails, tokens or prompts. Anything that calls `logging.config.fileConfig`/`dictConfig` in-process can disable the app loggers again; the alembic regression test guards that path.

### 2026-11-14 — Claude (chat, cloned the public repo) — B1-10
- Did: B1 test sweep and OpenAPI check.
  - OpenAPI: `schemas/errors.py` (`ErrorResponse`), `api/docs.py` (`errors()` helper, `ErrorDoc`), examples on all auth/workspace models, `responses=errors(...)` on every route, API description and tag metadata plus `health` tag in `main.py`, root aliases hidden from the schema.
  - Tests (+138): `tests/unit/test_openapi.py` (contract), `tests/unit/test_auth_edge_cases.py` (JWT edge cases, limiter fails closed, 429/503 envelope), additions to `test_auth_api.py` (deleted/inactive user, logout idempotency, HTTP end-to-end password reset, no-enumeration check), `test_workspaces_api.py` (rename race), `test_workspace_schemas.py`.
  - Makefile `BOOTSTRAP_PYTHON`; README steps updated; `AGENTS.md` rule 5 and `ARCHITECTURE.md` section 8 describe the OpenAPI conventions.
- State: `ruff check` clean; 346 passed, 0 skipped (Postgres 16 on 15432 + Redis). Clean clone (`git clone`, copy `.env.example`, `make test`, `make lint`, `make migrate`, `make seed` twice) all worked; live uvicorn smoke test confirmed `/docs`, the 403 limit error, the 422 envelope and the admin audit rows. Branch `main`, last commit `B1-10: OpenAPI docs, test sweep and portable Makefile`.
- Left undone: nothing for B1-10. Docker Compose was not run (no Docker in the sandbox).
- Next: `B1-11` (JSON access logs with request id), then B1 is complete.
- Watch out: a new route without tag/summary/examples/`errors(...)` now fails `tests/unit/test_openapi.py` on purpose. Examples must be valid instances of their model and must not contain `null` values.

### 2026-11-14 — Claude (chat, cloned the public repo) — B1-09
- Did: Implemented limits and audit helper (FR-2a.4):
  - `services/usage.py` (effective limits, workspace count, `ensure_can_create_workspace` raising `LimitExceededError` 403 `workspace_limit_reached`), `services/audit.py::log` (no commit, redacts secrets).
  - `WorkspaceService.create` locks the owner row and enforces `max_workspaces`; `soft_delete(db, workspace, actor)` writes `workspace.delete`; `get_workspace_for_user` writes `workspace.admin_access` for admins on other users' workspaces; `api/v1/workspaces.py` passes the actor and documents 403/409 in OpenAPI.
  - No migration: `user_limits`, `system_settings` and `audit_logs` (incl. `ip_address`) already exist from `0001_initial_schema`.
  - Docs updated: `TASKS.md` (B1-09 ticked, B5-05 hint), `DATABASE.md` (4.3, 4.19), `ARCHITECTURE.md` (Workspaces API row), this file.
- State: `ruff check` clean; full suite 208 passed (169 before + 24 unit + 15 integration) against PostgreSQL 16 on port 15432 and Redis. Race test verified to fail without the row lock. `ruff format --check` still flags the same two pre-existing files (`services/seed.py`, `tests/integration/test_admin_seed.py`), left untouched. Docker was not run. Branch `main`, last commit `B1-09: user limits and audit log helper`.
- Left undone: nothing for B1-09.
- Next: start `B1-10` (B1 test sweep and OpenAPI check).
- Watch out: a user can now hold at most 3 workspaces by default (`default_max_workspaces`), so any test that creates more per user must set a `user_limits` override (see `_set_limit` in `tests/integration/test_workspaces_api.py`; `test_list_pagination` needed it). Integration tests run in the sandbox only when Postgres is reachable; they skip otherwise.

### 2026-11-14 — Claude (chat, cloned the public repo) — B1-08
- Did: Implemented Workspaces CRUD (FR-1.4, FR-2a.1–3):
  - `schemas/workspaces.py`, `services/workspaces.py`, `api/v1/workspaces.py` (mounted at `/workspaces` and `/api/v1/workspaces` in `main.py`), `get_workspace_for_user` in `core/deps.py`.
  - Create/list(paginated, own only)/get/partial update/soft delete (`?confirm=true`); duplicate names per owner give 409 `workspace_name_taken` (case-insensitive); other users' or deleted workspaces give 404 `workspace_not_found`; admins may get/update/delete any live workspace.
  - No new migration: the `workspaces` table from `0001_initial_schema` already has every column.
  - Added `tzdata==2025.2` to `pyproject.toml` and `Dockerfile`.
  - Docs updated: `TASKS.md` (B1-08 ticked, B3-06 reminder), `DATABASE.md` (4.4 service rules), `ARCHITECTURE.md` (API row), this file.
- State: `ruff check` clean; full suite 169 passed against a real PostgreSQL 16 on port 15432 (35 new unit tests, 44 new integration tests). Integration tests skip cleanly if Postgres is not running. Two pre-existing files (`services/seed.py`, `tests/integration/test_admin_seed.py`) would be changed by `ruff format --check`; left untouched on purpose. Docker was not run in this session.
- Left undone: nothing for B1-08. Audit logging and the `max_workspaces` limit are B1-09.
- Next: start `B1-09` (limits and audit helper).
- Watch out: rebuild the API image (`docker compose build api`) because of the new `tzdata` dependency. `get_workspace_for_user` already hides other users' ids behind 404; reuse it for every workspace-scoped route (accounts, requests, analytics) in later phases.

### 2026-11-14 — Antigravity — B1-07
- Did: Implemented Password Reset workflow per PRD FR-1.2 and TASKS.md B1-07:
  - Added `RateLimitError` (HTTP 429) and `ServiceUnavailableError` (HTTP 503) in `core/errors.py`.
  - Added password reset settings and rate-limiting limits in `core/config.py` and `.env.example`.
  - Created `core/ratelimit.py` with Redis-backed fixed-window rate limiter enforcing per-IP limit (5/10 min), per-account limit (3/15 min with SHA-256 hashed email key), and per-account 60s cooldown key (`SET NX EX 60`), failing closed with HTTP 503 if Redis is unreachable.
  - Created `providers/email.py` with `BaseEmailSender` interface and `ConsoleEmailSender` development provider that prints reset URLs to console without logging tokens.
  - Added `ForgotPasswordRequest` and `ResetPasswordRequest` in `schemas/auth.py`.
  - Added `forgot_password()` and `reset_password()` in `services/auth.py`: generic 200 response on forgot-password to prevent account enumeration, revoking previous reset tokens, committing token before email dispatch, `SELECT … FOR UPDATE` row locking to ensure atomic single-use consumption, Argon2 hashing for new password, and revoking all active refresh tokens for the user upon reset. Handled timezone-aware comparison for SQLite and PostgreSQL compatibility.
  - Added `POST /auth/forgot-password` and `POST /auth/reset-password` endpoints in `api/v1/auth.py` (dual-mounted at root and `/api/v1`).
  - Added unit tests in `tests/unit/test_password_reset_service.py` (21 tests, all passing) and integration tests in `tests/integration/test_password_reset.py` covering rate limits, cooldown, single use, refresh token revocation, concurrent requests, and dual route mounting.
- State: tests pass (57 passed, 32 skipped offline when PostgreSQL is not running), lint clean (`make lint`), branch `main`.
- Left undone: nothing for B1-07. Ready for user commit approval.
- Next: start task B1-08 (Workspaces CRUD) in `TASKS.md`: implement workspace model CRUD endpoints with ownership dependency `get_workspace_for_user`.
- Watch out: Stateless JWT access tokens issued prior to password reset remain valid until natural expiration (`ACCESS_TOKEN_MINUTES=30`); refresh tokens are revoked immediately.

### 2026-11-14 — Antigravity — B1-06
- Did: Implemented RBAC and admin seeding per PRD FR-1.3 and TASKS.md B1-06:
  - Added `require_admin` dependency in `core/deps.py` enforcing `admin` role and active account status (raising HTTP 403 `admin_required`).
  - Created `api/v1/admin.py` with dummy test endpoint `/admin/test` (dual mounted at `/admin` and `/api/v1/admin`).
  - Configured `SEED_ADMIN_EMAIL` and `SEED_ADMIN_PASSWORD` in `core/config.py` without insecure defaults (requires environment values; safe placeholders in `.env.example`).
  - Implemented idempotent seeding service in `services/seed.py` (`seed_admin`, `seed_system_settings`, `seed_all`) that safely validates environment credentials, prevents automatic role promotion of existing regular users (raises `ValueError` and preserves existing password/role), creates new admin idempotently, and inserts 12 canonical system settings without overwriting customizations.
  - Updated `Makefile` `make seed` target.
  - Added unit tests in `test_rbac.py` and `test_seed_service.py` (36 unit tests passing total, covering missing credentials, new admin creation, role promotion prevention, idempotency, settings preservation) and integration tests in `test_admin_seed.py`.
- State: tests pass (36 passed, 23 skipped offline when PostgreSQL is not running), lint clean (`make lint`), branch `main`.
- Left undone: nothing for B1-06.
- Next: start task B1-07 (Password reset) in `TASKS.md`: `forgot-password` and `reset-password` endpoints with single-use expiring token; email sender interface with console implementation.
- Watch out: Running `make seed` is completely safe and idempotent; it will not duplicate accounts, will not overwrite modified system settings, and will refuse to promote non-admin accounts.

### 2026-11-14 — Antigravity — B1-05
- Did: Implemented authentication layer per PRD FR-1.1 and TASKS.md B1-05: `core/security.py` (Argon2id password hashing, JWT access token generation with HS256, secure refresh token generation and SHA-256 storage hash); `core/deps.py` (`get_current_user` Bearer dependency); `schemas/auth.py` (UserRegisterRequest with email validation, UserLoginRequest, TokenRefreshRequest, UserLogoutRequest, UserResponse, TokenResponse); `services/auth.py` (AuthService with register, login, refresh with rotation, logout with revocation); `api/v1/auth.py` (FastAPI router mounted at `/auth` and `/api/v1/auth` covering `/register`, `/login`, `/refresh`, `/logout`, `/me`); added unit tests in `test_security.py` and integration tests in `test_auth_api.py` covering happy paths, duplicate emails, inactive user blocking, invalid/expired tokens, and token rotation (44 total passing tests).
- State: tests pass (44 passed, 1 third-party deprecation warning), lint clean (`make lint`), branch `main`.
- Left undone: nothing for B1-05.
- Next: start task B1-06 (RBAC and admin seed) in `TASKS.md`: add `require_admin` dependency, `make seed` CLI command to idempotently create admin from settings, and seed default `system_settings`.
- Watch out: Argon2 password hashing takes ~100ms per operation by design for security; integration tests run in ~58 seconds against PostgreSQL test database.

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
