# AGENTS.md — Instructions for coding agents

> Applies to any AI coding agent (Claude Code, Codex, Cursor, etc.) and to humans.
> Project: Social Media Multi-Agent Automation Platform (backend first: FastAPI, LangGraph, PostgreSQL, Redis/Celery).

---

## 0. Start here (every session, in this order)

1. Read **`PROGRESS.md`** — it says what is done, what is in progress, and the exact **Next action**.
2. Read the **task** in **`TASKS.md`** that PROGRESS points to (and its `Dep`/`Refs`).
3. Read only the parts of `PRD.md`, `ARCHITECTURE.md`, `DATABASE.md` that the task touches. For any task that creates or changes an agent/node (B2-xx, B4-xx), also read the matching section of **`AGENT_SPECS.md`** — it defines the output schemas, checks, prompt rules and test fixtures. Do not guess; the docs are the contract.
4. Run `git status` and `git log -5 --oneline`. If the tree is dirty or the last commit does not match PROGRESS, say so in your first message and reconcile before coding.
5. Run `make test` to see the baseline. If it is already red, fix or record why before adding features.

## 1. Document map

| File | Purpose | You may edit it? |
|---|---|---|
| `PRD.md` | What and why (requirements) | Only to fix mistakes/add clarified requirements; note in PROGRESS Decisions |
| `ARCHITECTURE.md` | How (components, graph, API, config) | Yes, when you change design — in the same PR |
| `DATABASE.md` | Schema contract | Yes, with the matching Alembic migration |
| `AGENT_SPECS.md` | Per-agent schemas, checks, prompt rules, test fixtures | Yes, when you change an agent contract — with code and tests |
| `TASKS.md` | Ordered work list | Tick boxes; append new tasks at end of a phase |
| `PROGRESS.md` | Live status and handoff log | **Always, at the end of every session** |
| `AGENTS.md` | These rules | Rarely; propose changes to the human |

## 2. How to work

- **One task at a time.** Take the first unchecked task whose dependencies are done, unless the human says otherwise. Do not start the next task in the same change.
- **Smallest correct change.** Do not refactor unrelated code. Do not rename or move files without need.
- **Follow the architecture.** Layering: `api` → `services` → `models`. Agents/workers call services. Services never import `api`. No business logic in routers.
- **Do not invent** endpoints, tables, columns, statuses or env vars that are not in the docs. If you need one, update the doc and migration first and record it in PROGRESS Decisions.
- **Status changes** go only through `services/requests.py::transition()`.
- **External calls behind interfaces.** LLM only via `app/agents/llm.py`. Image/video/TTS/search via `app/providers/*`. Platforms via `app/publishers/*` and `app/collectors/*`. Storage via `app/storage/*`. Never call these libraries/SDKs directly from nodes, routers or services.
- **Mock first.** Development and tests run with `LLM_PROVIDER=fake`, `PUBLISHER_MODE=mock`, fake providers. Real integrations are added behind the same interface and tested with the opt-in marker `-m live`. Never make live calls in default tests.
- **Ask when blocked or ambiguous.** If a requirement is unclear, platform API access is missing, or a decision changes the architecture, stop and write the question in PROGRESS "Open questions" and tell the human. Do not silently pick a risky option.
- **If you must deviate from a doc**, say so explicitly, update the doc, and log the decision.

## 3. Definition of done (a task is done only when all are true)

1. The task's **Done when** criteria pass.
2. New/changed behaviour has tests (unit, graph, integration as appropriate) and `make test` is green.
3. `make lint` (ruff) is clean; types are annotated on public functions.
4. DB changes have an Alembic migration (up and down work) and `DATABASE.md` is updated.
5. API changes appear in OpenAPI with request/response schemas and error cases; `ARCHITECTURE.md` API table updated. New routes get a tag, a summary, model examples and `responses=errors(...)` from `app/api/docs.py`; `tests/unit/test_openapi.py` fails if one is missing.
6. No secrets, tokens or personal data in code, logs, fixtures or commits.
7. `TASKS.md` box ticked; `PROGRESS.md` updated (section 6).

## 4. Coding standards

- Python 3.11+, type hints, Pydantic v2 schemas for all API I/O and all LLM structured outputs.
- Format/lint with ruff (`make format`, `make lint`). Keep functions small and named for what they do.
- Config only through `core/config.py` (pydantic-settings). No `os.environ` reads elsewhere. No hard-coded model names, URLs or limits — use settings/`system_settings`.
- Errors: raise `AppError` subclasses with a stable `code`; never leak stack traces to clients.
- Logging: structured; include `request_id`/`run_id`/`node`. Never log tokens, passwords, full prompts with user secrets.
- Time: UTC everywhere; use timezone-aware datetimes; convert only at the edges.
- IDs: UUIDs. Pagination: `limit`/`offset` returning `{items, total}`.
- Idempotency: jobs and publishing must be safe to run twice. Check state before side effects.
- Concurrency: job pickup with `FOR UPDATE SKIP LOCKED`; never run LangGraph inside an API request; use Celery tasks.
- Prompts live in `app/agents/prompts/`, not inline in node code.
- Agent nodes: always built with `make_checked_node`; never write ad-hoc retry loops.
- Tests: pytest; descriptive names; no network; use fixtures/factories; freeze time for scheduling tests.

## 5. Database and migrations

- Alembic only. **Never edit an applied migration**; add a new one.
- Keep `DATABASE.md` and the models in sync.
- Tokens are Fernet-encrypted columns. Never store or return plaintext tokens.
- LangGraph checkpoint tables are created by the library setup call, not by Alembic.

## 6. Session end checklist (mandatory)

Before you stop — even if the task is unfinished:

1. Run `make lint` and `make test`; note results.
2. Commit work in small commits with clear messages (`B3-05: validate request input and create targets`). Do not leave half-working changes uncommitted; if needed, commit as WIP on a branch and say so.
3. Update **`TASKS.md`** checkboxes (`[~]` for partial work).
4. Update **`PROGRESS.md`**:
   - "Next action" (one precise sentence another agent can execute without asking).
   - "Current state" table and "In progress" (what is done/left inside the task, files touched).
   - "Recently completed", "Decisions log", "Known issues", "Open questions" as needed.
   - Append an entry to the **Handoff log** (template inside PROGRESS.md), newest first.
5. State in your final message: what was done, test results, what is next, any blocker.

## 7. Handoff between agents (Claude Code ↔ Codex ↔ others)

- The repo is the memory. Do not rely on chat history; anything the next agent needs must be in `PROGRESS.md`, the docs, or code comments/tests.
- Claude Code reads `CLAUDE.md` (which imports this file); Codex reads `AGENTS.md`. Keep rules **only here**; `CLAUDE.md` stays a one-line import.
- If you start and PROGRESS looks stale versus git history, trust git + tests, fix PROGRESS, and note it in the Handoff log.
- Never rewrite the Handoff log history; append only.

## 8. Commands

| Command | Purpose |
|---|---|
| `make dev` | Start the stack with Docker Compose |
| `make test` | Run the test suite (offline) |
| `make test-live` | Run opt-in live contract tests (`-m live`; needs real credentials) |
| `make lint` / `make format` | Ruff check / format |
| `make migrate` | Apply Alembic migrations |
| `make migration m="message"` | Create a new migration |
| `make seed` | Create admin user and default settings |
| `python -m app.agents.run --topic "..." --type post --platforms facebook` | Run the agent graph standalone (fake LLM by default) |

(These targets are created in task B1-01. If one is missing, create it as part of the task you are on and record it.)

## 9. Safety rules

- Do not run destructive commands against any non-local database or any real social account.
- Never post to a real platform unless the human explicitly enabled `PUBLISHER_MODE=real` with test accounts.
- Do not commit `.env`, tokens, keys, or media outputs. Keep `.env.example` current.
- Do not add dependencies casually: justify in the commit message and update `pyproject.toml`.
- Platform API rules, quotas and review requirements change. Before implementing a real publisher, read the current official documentation and record the relevant constraints in PROGRESS.
- Respect content policy: moderation rules and reviewer checks must stay on; do not bypass them to make tests pass.

## 10. When things go wrong

- Failing test you did not cause: record it in PROGRESS "Known issues" with the command and error; do not delete or skip the test to go green.
- Environment problem (Docker, ports, keys): write the exact symptom and what you tried in PROGRESS "Environment notes".
- Scope creep: if you find missing work, add a task at the end of the right phase in `TASKS.md` instead of doing it now.
