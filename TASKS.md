# TASKS — Development Plan (backend first)

> The ordered work list. Pick the **first unchecked task whose dependencies are done** (also check `PROGRESS.md` for the "Next action").
> Mark a task `[x]` only when its **Done when** criteria pass and tests are green. Update `PROGRESS.md` at the end of every session.
> Task IDs are stable: `B<phase>-<nn>`. Never renumber; add new tasks at the end of a phase with the next number.

Legend: `Refs` = requirement IDs in `PRD.md`. `Dep` = tasks that must be done first. Status markers: `[ ]` todo, `[~]` in progress, `[x]` done, `[!]` blocked (explain in `PROGRESS.md`).

Phases: **B1** Foundation → **B2** Agent engine (post) → **B3** Core pipeline end-to-end → **B4** Video → **B5** Remaining backend → **B6** Frontend integration and delivery.

---

## Phase B1 — Foundation (M0, M1, M2)

Goal: runnable backend with auth, roles, workspaces and limits.

- [x] **B1-01 Repo scaffold and tooling** — Create folder layout from `ARCHITECTURE.md` section 3, `pyproject.toml` (deps pinned), ruff config, pytest config, `Makefile` targets (`dev`, `test`, `lint`, `format`, `migrate`, `seed`), `.env.example`, `.gitignore`.
  **Done when:** `make lint` and `make test` run (even with a placeholder test) and pass.
- [x] **B1-02 Docker Compose** — Services `api`, `worker`, `beat`, `postgres`, `redis` with healthchecks; `GET /health` and `GET /health/ready` (db + redis check).
  **Done when:** `docker compose up` starts all; `/health/ready` returns 200. **Dep:** B1-01.
- [x] **B1-03 Core infrastructure** — `core/config.py` (pydantic-settings), structured JSON logging with request id middleware, `AppError` hierarchy + global handler producing the standard error format, CORS from env.
  **Done when:** a test hits an endpoint that raises `AppError` and gets `{error:{code,message,details}}`. **Dep:** B1-01.
- [x] **B1-04 Database and first migration** — SQLAlchemy base/session (`core/db.py`), Alembic setup; migration for `users`, `refresh_tokens`, `user_limits`, `workspaces`, `audit_logs`, `system_settings` per `DATABASE.md`. Decide enum strategy (PG enum vs text+CHECK) and record it in `PROGRESS.md` Decisions.
  **Done when:** `make migrate` on an empty DB creates the tables; downgrade works. **Dep:** B1-02, B1-03.
- [x] **B1-05 Auth endpoints** — register, login, refresh, logout, `GET /auth/me`; password hashing; JWT access + hashed refresh tokens; inactive users blocked. Refs: FR-1.1.
  **Done when:** tests cover register→login→me→refresh→logout and wrong-password/inactive cases. **Dep:** B1-04.
- [x] **B1-06 RBAC and admin seed** — dependencies `get_current_user`, `require_admin`; `make seed` creates the admin idempotently; default `system_settings` inserted if missing. Refs: FR-1.3.
  **Done when:** a user token gets 403 on a dummy admin route; admin gets 200. **Dep:** B1-05.
- [x] **B1-07 Password reset** — `forgot-password` / `reset-password` with single-use expiring token; email sender interface with console implementation. Refs: FR-1.2.
  **Done when:** test completes the reset flow using the console sender's captured token. **Dep:** B1-05.
- [x] **B1-08 Workspaces CRUD** — create/list/get/update/soft-delete; fields per `DATABASE.md` 4.4; `get_workspace_for_user` ownership dependency (admin bypass); unique name per owner; delete requires `?confirm=true`. Refs: FR-1.4, FR-2a.1–3.
  **Done when:** user A cannot read/modify user B's workspace (404); soft-deleted workspaces disappear from lists. **Dep:** B1-06.
- [x] **B1-09 Limits and audit helper** — `services/usage.py` (effective limits = `user_limits` override else `system_settings` default); enforce `max_workspaces`; `services/audit.py::log(actor, action, entity, meta)`; log admin overrides and workspace deletes. Refs: FR-2a.4.
  **Done when:** creating workspace number limit+1 returns a clear 4xx error. **Dep:** B1-08.
- [x] **B1-10 B1 test sweep and OpenAPI check** — coverage of auth/workspaces; `/docs` shows all routes with tags and examples.
  **Done when:** `make test` green; CI-style run from a clean clone works using README steps. **Dep:** B1-01…B1-09.
- [x] **B1-11 Uvicorn access logs in JSON with request id** — Configure Uvicorn / middleware so that API access logs are emitted as JSON lines that include request_id (e.g. disable Uvicorn's default access log and emit one structured line per request from middleware).
  **Done when:** the API container's access logs are JSON lines that include request_id. **Dep:** B1-03.

## Phase B2 — Agent engine, post flow (M6, M7)

Goal: run the full post-generation graph from a CLI with a fake LLM, including retry and escalation.
**Read `AGENT_SPECS.md` first** (sections 1–5): schemas, checks, feedback format and test fixtures for every B2 node are defined there.

- [x] **B2-01 LLM wrapper** — `agents/llm.py`: `get_chat_model(kind)`, `generate_structured(schema, messages)` (Pydantic output with one repair retry), usage/cost capture hook, `fake` provider (scripted responses, can be told to fail N times). Models come from settings, never hard-coded.
  **Done when:** tests prove fake provider scripting and structured-output parsing. **Dep:** B1-03.
- [ ] **B2-02 Migration for runs and steps** — `workflow_runs`, `agent_steps` + models + service to record steps.
  **Done when:** inserting and querying a run with steps works. **Dep:** B1-04.
- [ ] **B2-03 State and prompts** — `agents/state.py` per `ARCHITECTURE.md` 6.2; `agents/prompts/` with one prompt file per agent (placeholders, no secrets).
  **Done when:** state types import and a prompt loader test passes. **Dep:** B2-01.
- [ ] **B2-04 Checked-node factory** — `agents/checked.py::make_checked_node` implementing agent→check→(pass|retry|escalate), per-step counters, feedback passing, step logging (B2-02), escalate via interrupt.
  **Done when:** tests: pass first time; fail twice then pass; exhausts retries and escalates; resume with `retry` resets the counter. **Dep:** B2-02, B2-03. Refs: FR-6.1–6.3.
- [ ] **B2-05 validate_input and planner nodes** — moderation hook (stub reading rules, real table arrives in B5), plan schema.
  **Done when:** node tests with fake LLM; invalid input ends the graph with `errors`. **Dep:** B2-03.
- [ ] **B2-06 researcher node** — `providers/search.py` interface + `FakeSearch` + one real implementation selectable by config; summarize results into `research`.
  **Done when:** works with fake search; real provider behind `SEARCH_PROVIDER` documented in `.env.example`. **Dep:** B2-03.
- [ ] **B2-07 strategist node** — per-platform strategy from plan + research + workspace brand voice.
  **Done when:** strategy keyed by each target platform; brand voice appears in the prompt input (test). **Dep:** B2-05, B2-06. Refs: FR-5.1.
- [ ] **B2-08 Router and main graph skeleton** — `content_router` conditional edge, `graph.py` with Postgres checkpointer, placeholder subgraphs.
  **Done when:** invoking with `content_type=post` reaches the post placeholder and `video` the video placeholder. **Dep:** B2-07. Refs: FR-5.2.
- [ ] **B2-09 Storage and image provider** — `StorageBackend` (local), `providers/image.py` (`FakeImage` returns a valid small PNG, one real provider by config).
  **Done when:** put/get/delete test; fake image generates bytes with correct mime. **Dep:** B1-03.
- [ ] **B2-10 Post subgraph: writer, image, hashtag** — three checked nodes; rule checks (length limits from `platform_rules`, hashtag count, image present/size); LLM-judge for tone/relevance.
  **Done when:** subgraph produces `drafts[platform]` with caption, hashtags and one image per platform; retry feedback visibly changes the second attempt (test). **Dep:** B2-04, B2-08, B2-09. Refs: FR-5.3, FR-5.5.
- [ ] **B2-11 Reviewer and route_after_review** — final review returning issues with `responsible_node`; `fix` routes back to that node with feedback; `max_review_cycles` respected.
  **Done when:** tests for ok path, fix path, and cycle cap (continues to approval with warnings). **Dep:** B2-10. Refs: FR-6.4.
- [ ] **B2-12 Dev runner** — `python -m app.agents.run --topic ... --type post --platforms facebook,instagram` runs the graph with fake or real LLM until the first interrupt and prints the draft.
  **Done when:** works offline with `LLM_PROVIDER=fake`. **Dep:** B2-11.
- [ ] **B2-13 Graph test suite** — retry path, escalation path, review fix loop, router both directions, multi-platform drafts.
  **Done when:** all green, no network. **Dep:** B2-12.

## Phase B3 — Core pipeline end-to-end (M3 basic, M4, M5, M9, M10, M11, M12 core)

Goal: "Backend Core Complete" (`PRD.md` section 12) with `PUBLISHER_MODE=mock`.

- [ ] **B3-01 Migration for pipeline tables** — `social_accounts`, `content_requests`, `request_targets`, `content_items`, `media_assets`, `approvals`, `scheduled_jobs`, `post_metrics`, `comments`, `notifications` + models.
  **Done when:** migrate up/down clean; constraints from `DATABASE.md` present. **Dep:** B1-04.
- [ ] **B3-02 Token crypto and accounts (dev mode)** — `core/crypto.py` (Fernet), accounts service, `GET /workspaces/{id}/accounts`, `POST .../accounts/dev` (disabled in production), `DELETE /accounts/{id}`; per-workspace account limit.
  **Done when:** two accounts on the same platform can be attached; tokens are ciphertext in DB and never in responses/logs. **Dep:** B3-01, B1-09. Refs: FR-2.1–2.5.
- [ ] **B3-03 Status service** — `services/requests.py::transition()` implementing the machine in `ARCHITECTURE.md` section 4 (validation, reason, audit, notification hook).
  **Done when:** table-driven test covers every allowed and a sample of forbidden transitions. **Dep:** B3-01.
- [ ] **B3-04 Notifications service (core)** — create notifications for events; `GET /notifications`, `POST /notifications/{id}/read`, `read-all`. Refs: FR-11.1.
  **Done when:** transitions to `awaiting_approval`, `needs_attention`, `held`, `published`, `publish_failed` create notifications. **Dep:** B3-03.
- [ ] **B3-05 Submit request endpoint** — `POST /workspaces/{id}/requests` with full validation (accounts belong and active, publish_at future, moderation, quotas); creates request, targets and the `start_generation` job at `publish_at - lead_time`; list/get endpoints with filters and pagination. Refs: FR-3.1, 3.2, 4.1.
  **Done when:** invalid cases return clear errors and create no rows; valid case creates exactly one request, N targets, one job. **Dep:** B3-02, B3-03.
- [ ] **B3-06 Scheduler service and Beat poller** — `services/scheduler.py`; Celery app, Beat entry `poll_due_jobs` every 60 s using `FOR UPDATE SKIP LOCKED`; stale-job reclaimer; backoff; cancel/reschedule helpers; `PATCH /requests/{id}/schedule`, `POST /requests/{id}/cancel`. Also wire `WorkspaceService.soft_delete` (B1-08) to cancel the workspace's pending jobs and non-final requests (`DATABASE.md` section 6 rule 7, `PRD.md` FR-2a.3). Refs: FR-4.1, 4.2, 2a.3.
  **Done when:** test with frozen time: due job enqueued once, not twice; cancel removes pending jobs; deleting a workspace cancels its pending jobs. **Dep:** B3-05.
- [ ] **B3-07 Generation task** — `workflow.start_generation(request_id)`: create run row, invoke graph, persist content items/media, transition to `generating` → `awaiting_approval` or `needs_attention`; `human_approval` node writes `content_items` and notifies. Refs: FR-5.x, 6.3.
  **Done when:** with fake LLM, a queued request ends `awaiting_approval` with a content item per platform. **Dep:** B3-06, B2-13.
- [ ] **B3-08 Escalation resolve endpoint** — `POST /requests/{id}/resolve {action: retry|abort}` resuming the graph.
  **Done when:** forced failure → `needs_attention` → `retry` continues; `abort` → `failed`. **Dep:** B3-07.
- [ ] **B3-09 Approval endpoints** — `GET /requests/{id}/preview`, `PATCH /requests/{id}/content` (edit caption/hashtags/title; marks `edited_by_user`), `POST /requests/{id}/approve` (optional `publish_now`, `reschedule_to`), `POST /requests/{id}/reject` (`feedback`, `regenerate`) resuming the graph with `Command(resume=...)`; new content versions on regenerate. Refs: FR-7.1–7.4.
  **Done when:** approve → `scheduled`; reject+regenerate → `generating` → new version `awaiting_approval`; reject stop → `rejected`. **Dep:** B3-07.
- [ ] **B3-10 Held detection** — periodic check: `awaiting_approval` requests past `publish_at` → `held` + notification; approve of a held request supports `publish_now` or `reschedule_to`. Refs: FR-4.3.
  **Done when:** frozen-time test moves the request to `held`; approve with reschedule returns it to `scheduled`. **Dep:** B3-09.
- [ ] **B3-11 schedule_gate and publish job** — graph pauses until `publish_at`, publish job resumes it (or continues immediately if already due).
  **Done when:** approved request becomes `publishing` only when the publish job runs. **Dep:** B3-09, B3-06.
- [ ] **B3-12 Publisher interface and MockPublisher** — `publishers/base.py`, `mock.py`, `registry.py` honoring `PUBLISHER_MODE`; mock can fail on demand (config or magic topic).
  **Done when:** registry returns mock in mock mode; failure injection works. **Dep:** B3-01. Refs: FR-8.4.
- [ ] **B3-13 Publisher node** — per-target publish with credentials loaded from DB, per-target status, backoff retry on `retriable` errors, idempotency, final request status (`published` / `partially_published` / `failed`), notifications. Refs: FR-8.1–8.3.
  **Done when:** tests: all succeed; one of three fails (partial); retriable failure then success; re-running the job never double-posts. **Dep:** B3-11, B3-12.
- [ ] **B3-14 Collectors and analytics storage** — `collectors/base.py` + mock; `analytics_setup` node creates `collect_metrics` jobs from `metrics_schedule`; `tasks_metrics.collect` stores `post_metrics` and upserts `comments`, schedules the next job. Refs: FR-9.1, 9.2.
  **Done when:** after mock publish, jobs run and rows appear; comments are not duplicated on re-fetch. **Dep:** B3-13.
- [ ] **B3-15 Analytics endpoints** — per request, per account, per workspace (totals, latest snapshot, time series), comments list with pagination, manual refresh. Refs: FR-9.3.
  **Done when:** values match seeded snapshots in tests; ownership enforced. **Dep:** B3-14.
- [ ] **B3-16 First real publisher (Facebook Pages)** — implement text+image post via the official API, token handling and error mapping (`retriable` vs not); matching collector for post metrics/comments. Verify requirements in current official docs first and note them in `PROGRESS.md`.
  **Done when:** contract test marked `-m live` passes against a test page; default CI stays mock-only. **Dep:** B3-13. *(May be postponed if app review blocks; mark `[!]` and continue.)*
- [ ] **B3-17 OAuth connect flow (Facebook)** — `POST /workspaces/{id}/accounts/connect/{platform}` returns auth URL with server-side `state`; `GET /oauth/{platform}/callback` exchanges code, lists pages, stores encrypted tokens; token refresh job. Refs: FR-2.1, 2.3.
  **Done when:** callback handler tested with a mocked HTTP layer; invalid `state` rejected. **Dep:** B3-02. *(Should; can follow the frontend start.)*
- [ ] **B3-18 Backend Core Complete test** — one e2e test implementing all 10 steps of `PRD.md` section 12.
  **Done when:** green offline. **Dep:** B3-01…B3-15.

## Phase B4 — Video flow (M8)

Goal: topic → video with voice and subtitles, per-scene retry, publish to a video platform.
**Read `AGENT_SPECS.md` section 6** (script, storyboard, scene, assembly, voice) before each task.

- [ ] **B4-01 Media toolchain** — FFmpeg wrapper (concat, scale/pad to aspect ratio, mix audio, burn/attach subtitles), `SceneProvider`, `TTSProvider` interfaces with fakes (fake scene = short generated color clip; fake TTS = silent/beep audio of right length).
  **Done when:** unit tests produce a playable mp4 offline (ffprobe verifies). **Dep:** B2-09.
- [ ] **B4-02 script_node + check** — structured script (hook, scenes with narration and duration); rule check for total duration vs platform limit. **Dep:** B2-04, B4-01.
- [ ] **B4-03 storyboard_node + check** — per-scene visual description and shot prompts; check coverage of all script scenes. **Dep:** B4-02.
- [ ] **B4-04 scene_node (per-scene loop) + check** — generate each scene; retry only the failed scene (`scene:<n>`, max 2); check duration/resolution/non-empty. **Dep:** B4-03.
- [ ] **B4-05 assembly_node + check** — FFmpeg assembly to platform aspect ratio; rule checks (duration, resolution, file playable). **Dep:** B4-04.
- [ ] **B4-06 voice_node + check** — narration audio, SRT subtitles, sync and subtitle timing checks. **Dep:** B4-05.
- [ ] **B4-07 Video subgraph wiring** — connect nodes, update router, reviewer handles video, preview endpoint returns video URL.
  **Done when:** `content_type=video` e2e test (fake providers) reaches `awaiting_approval`. **Dep:** B4-06.
- [ ] **B4-08 YouTube publisher and collector** — resumable upload, title/description/tags, metrics and comments. Verify current API quota/verification rules first.
  **Done when:** mock path e2e for video passes; live contract test opt-in. **Dep:** B4-07, B3-13.
- [ ] **B4-09 Platform rules config** — durations/aspect ratios/caption limits per platform in `system_settings.platform_rules`; checks read from it.
  **Done when:** changing a rule changes check outcomes in a test. **Dep:** B4-02.
- [ ] **B4-10 Video test suite** — scene retry isolation, escalation, assembly failure path.
  **Done when:** green offline. **Dep:** B4-07.

## Phase B5 — Remaining backend (M12, M13, M14, more platforms)

- [ ] **B5-01 Content library API** — list/search/filter, get, delete, media streaming/URLs. Refs: FR-10.x.
- [ ] **B5-02 Notification extras** — account-expired notifications, optional email sender interface wired (console default). Refs: FR-11.x.
- [ ] **B5-03 Instagram publisher/collector** — (needs linked Facebook Page / business account per current rules). **Dep:** B3-13.
- [ ] **B5-04 TikTok publisher/collector** — video posting per current API and audit status; if unavailable keep mock and document. **Dep:** B4-07.
- [ ] **B5-05 Admin users API** — CRUD, suspend/activate, role change, limits (`PATCH /admin/users/{id}/limits`). Refs: FR-12.1. Audit logged (use `services/audit.log`; read/validate limits via `services/usage.py`).
- [ ] **B5-06 Admin settings API** — `GET/PUT /admin/settings` with validation per key. Refs: FR-13.1.
- [ ] **B5-07 Admin monitoring API** — workflows list/detail with agent steps, queue view, failure filters, health. Refs: FR-14.1.
- [ ] **B5-08 Moderation** — `moderation_rules` migration + CRUD; wired into `validate_input` and `reviewer`; admin remove/block content and cancel/override scheduled requests. Refs: FR-15.1.
- [ ] **B5-09 Usage and cost reports** — aggregates from `agent_steps` per user/period, platform-wide analytics, CSV export. Refs: FR-16.1.
- [ ] **B5-10 Admin workspace management** — list/suspend/delete any workspace; suspended workspaces cannot submit/publish. Refs: FR-16.2.
- [ ] **B5-11 OAuth for remaining platforms** — Google (YouTube), TikTok, Instagram via Facebook; token refresh sweep.
- [ ] **B5-12 Hardening** — rate limiting, security headers, dependency audit, secret scan, input size limits, production config checks (dev endpoints disabled).
- [ ] **B5-13 Retention janitors** — optional cleanup tasks from `DATABASE.md` section 7.

Each B5 task: **Done when** endpoints are covered by tests, appear in OpenAPI with schemas, and enforce role/ownership.

## Phase B6 — Frontend integration and delivery (M15)

- [ ] **B6-01 Freeze API contract** — review OpenAPI, fix naming inconsistencies, export `openapi.json`, generate a typed client for the chosen frontend.
- [ ] **B6-02 Frontend project setup** — React or Next.js, auth flow (login, refresh), API client, env config, CORS verified.
- [ ] **B6-03 Screens, in order:** auth → workspaces → connect accounts (OAuth redirect) → new request → request status/list → preview/edit/approve/reject → analytics and comments → library → notifications → admin (users, settings, monitoring, moderation, reports).
- [ ] **B6-04 Status updates** — polling (default) or SSE for request status and notifications.
- [ ] **B6-05 E2E tests** — browser-level happy path against the mock publisher.
- [ ] **B6-06 Deployment** — production compose/hosting, secrets, HTTPS, backups, monitoring.
- [ ] **B6-07 Demo and final report** — seeded demo data, demo script, use case/sequence diagrams synced with the final system, user guide.

---

## Task writing rules (for adding new tasks)

1. One task = one reviewable unit of work (ideally under a day).
2. Must have **Done when** criteria that are testable.
3. List `Dep` and `Refs`.
4. If a task needs a decision, add it to `PROGRESS.md` Decisions log first.
