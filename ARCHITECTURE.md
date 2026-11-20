# ARCHITECTURE — Social Media Multi-Agent Automation Platform

> Source of truth for HOW the backend is built. Requirements: `PRD.md`. Data: `DATABASE.md`. Agent behaviour, schemas and checks: `AGENT_SPECS.md`.
> If code and this document disagree, fix one of them in the same PR and note it in `PROGRESS.md` (Decisions log).

---

## 1. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | Type hints everywhere. |
| API | FastAPI + Pydantic v2 | Auto OpenAPI = contract for the future frontend. |
| ORM / migrations | SQLAlchemy 2.0 + Alembic | Sync or async consistently (default: **sync** SQLAlchemy for simplicity with Celery). |
| Database | PostgreSQL 16 | JSONB for flexible agent output. |
| Queue / scheduler | Redis + Celery + Celery Beat | Beat polls the `scheduled_jobs` table. |
| Agents | LangGraph (+ LangChain model wrappers) | Postgres checkpointer for resumable runs. `langgraph`, `langgraph-checkpoint` and `langgraph-prebuilt` are pinned together in `pyproject.toml`: an unpinned `langgraph-checkpoint` resolves to a version that cannot import with the pinned `langchain-core`. Change them as a set. |
| LLM | Provider-agnostic wrapper, default Gemini | Config-driven model names; `fake` provider for tests. |
| Media | Provider interfaces (image, video-scene, TTS) + FFmpeg | Providers are config, not hard-coded. |
| Storage | Local filesystem (dev) / S3-compatible (prod) | Behind `StorageBackend` interface. |
| Auth | JWT (access + refresh), argon2/bcrypt | |
| Crypto | `cryptography` Fernet | Encrypts platform tokens. |
| Tests | pytest, httpx, factory helpers | LLM and platforms always faked in tests. |
| Quality | ruff (lint+format), mypy (optional), pre-commit | |
| Runtime | Docker Compose: `api`, `worker`, `beat`, `postgres`, `redis` | |
| Frontend (later) | React or Next.js consuming the OpenAPI spec | Phase B6. |

## 2. System overview

```mermaid
flowchart LR
  FE["Frontend (phase B6)"] -->|REST / JSON| API["FastAPI"]
  API --> DB[("PostgreSQL")]
  API --> REDIS[("Redis")]
  BEAT["Celery Beat"] -->|due jobs every 60s| REDIS
  REDIS --> WORKER["Celery worker"]
  WORKER --> GRAPH["LangGraph workflow"]
  GRAPH --> LLM["LLM and media providers"]
  GRAPH --> DB
  WORKER --> PUB["Publishers"]
  PUB --> PLAT["Platform APIs"]
  WORKER --> COL["Metrics collectors"]
  COL --> PLAT
  API --> STORE[("Media storage")]
  WORKER --> STORE
```

Principles:
1. **API is thin.** It validates, writes rows, enqueues jobs, reads status. It never calls an LLM or a platform.
2. **Workers do the slow work.** Generation, publishing, metrics.
3. **Everything external is behind an interface** (LLM, image/video/TTS, storage, publishers, collectors, email) with a fake/mock implementation.
4. **DB is the source of truth** for status, schedules and results. LangGraph checkpoints hold in-flight workflow state only.
5. **Idempotent jobs.** Re-running any job must not duplicate side effects.

## 3. Repository layout

```
repo-root/
├─ AGENTS.md  CLAUDE.md  PRD.md  ARCHITECTURE.md  DATABASE.md  TASKS.md  PROGRESS.md
├─ docker-compose.yml  Makefile  .env.example  pyproject.toml
└─ backend/
   ├─ alembic/                      # migrations
   ├─ app/
   │  ├─ main.py                    # FastAPI app factory
   │  ├─ core/                      # config.py, db.py, security.py, crypto.py, logging.py, errors.py, deps.py
   │  ├─ models/                    # SQLAlchemy models (one file per aggregate)
   │  ├─ schemas/                   # Pydantic request/response models
   │  ├─ api/v1/                    # routers: auth, users, workspaces, accounts, requests, approvals,
   │  │                             #          library, analytics, notifications, admin, oauth, health
   │  ├─ services/                  # business logic (no FastAPI imports): requests, scheduler, approvals,
   │  │                             #          publishing, analytics, notifications, moderation, audit, usage
   │  ├─ agents/
   │  │  ├─ llm.py                  # get_chat_model(), generate_structured(), usage tracking, fake provider
   │  │  ├─ state.py                # GraphState and sub-types
   │  │  ├─ graph.py                # builds main graph + checkpointer
   │  │  ├─ checked.py              # make_checked_node(): agent + check + retry/escalate pattern
   │  │  ├─ prompts/                # one file per agent prompt
   │  │  ├─ nodes/                  # validate_input, planner, researcher, strategist, router,
   │  │  │                          # reviewer, human_approval, schedule_gate, publisher, analytics_setup
   │  │  ├─ post/                   # writer, image, hashtag + subgraph.py
   │  │  ├─ video/                  # script, storyboard, scene, assembly, voice + subgraph.py
   │  │  └─ checks/                 # rule checks and LLM-judge checks
   │  ├─ providers/                 # image.py, video.py, tts.py, search.py (interfaces + fakes + real)
   │  ├─ publishers/                # base.py, mock.py, facebook.py, instagram.py, youtube.py, tiktok.py, registry.py
   │  ├─ collectors/                # base.py, mock.py, per-platform collectors
   │  ├─ storage/                   # base.py, local.py, s3.py
   │  └─ workers/                   # celery_app.py, tasks_workflow.py, tasks_publish.py, tasks_metrics.py, tasks_scheduler.py
└─ tests/                           # unit/, integration/, e2e/
```

Rule: dependencies point inward → `api` → `services` → `models`; `agents` and `workers` call `services`; `services` never import `api`.

## 4. Request lifecycle and status machine

`content_requests.status` values and allowed transitions:

```mermaid
stateDiagram-v2
  [*] --> queued: request accepted
  queued --> generating: start_generation job runs
  generating --> needs_attention: retries exhausted / escalation
  needs_attention --> generating: human resolves "retry"
  needs_attention --> failed: human resolves "abort"
  generating --> awaiting_approval: reviewer ok, interrupt at human_approval
  awaiting_approval --> generating: reject + regenerate
  awaiting_approval --> rejected: reject (stop)
  awaiting_approval --> scheduled: approve (publish_at in future)
  awaiting_approval --> held: publish_at passed without approval
  held --> scheduled: approve + reschedule
  held --> publishing: approve + publish now
  scheduled --> publishing: publish job due
  publishing --> published: all targets published
  publishing --> partially_published: some targets failed
  publishing --> failed: all targets failed
  queued --> cancelled: user/admin cancels
  scheduled --> cancelled
  awaiting_approval --> cancelled
```

Per-target status (`request_targets.status`): `pending → publishing → published | failed`.

All transitions go through `services/requests.py::transition(request, new_status, reason)` which validates the move, writes the row, and creates notifications/audit entries. **Never set `status` directly elsewhere.**

## 5. Scheduling design

The "Scheduler first" requirement is implemented as a **service + job table**, not an LLM node.

1. `POST /workspaces/{id}/requests` validates input, creates `content_requests` (`queued`), `request_targets`, and inserts a `scheduled_jobs` row:
   `job_type=start_generation`, `run_at = publish_at - lead_time(content_type)`; if that is already in the past, `run_at = now`.
2. **Celery Beat** runs `scheduler.poll_due_jobs` every 60 s. It selects due jobs with `FOR UPDATE SKIP LOCKED`, marks them `running`, and enqueues the matching Celery task.
3. `workflow.start_generation(request_id)` invokes the LangGraph graph (thread id = `request_id`) until it stops at an interrupt or ends.
4. After approval, `schedule_gate` either continues (if `publish_at <= now`) or inserts `job_type=publish, run_at=publish_at` and pauses the graph. The publish job resumes the graph.
5. After publishing, `analytics_setup` inserts `collect_metrics` jobs per the metrics schedule (config `metrics_schedule`, default +1h, +6h, +24h, then daily to day 14, then weekly to day 30).

Lead times (system settings, defaults): post 30 min, video 120 min.
Job statuses: `pending → running → done | failed | cancelled`, with `attempts`, `last_error`, exponential backoff for retriable failures.
Cancel/reschedule = update or cancel the pending job rows in the same transaction as the request change.

## 6. LangGraph workflow

### 6.1 Main graph

```mermaid
flowchart TD
  S(["start"]) --> V["validate_input"]
  V -->|invalid| END1(["end: failed"])
  V --> P["planner"] --> R["researcher"] --> ST["strategist"] --> CR{"content_router"}
  CR -->|post| PS["post_subgraph"] --> RV["reviewer"]
  CR -->|video| VS["video_subgraph"] --> RV
  RV --> RR{"route_after_review"}
  RR -->|"fix (max 2 cycles)"| CR
  RR -->|ok| HA["human_approval (interrupt)"]
  HA -->|"reject + regenerate"| CR
  HA -->|"reject (stop)"| END2(["end: rejected"])
  HA -->|approve| SG["schedule_gate (interrupt until publish_at)"]
  SG --> PU["publisher"] --> AN["analytics_setup"] --> END3(["end"])
```

Node reference:

| Node | Responsibility | Writes to state |
|---|---|---|
| `validate_input` | Re-validate topic, targets, moderation rules. | `errors` |
| `planner` | Create content plan (angle, audience, key messages, format). | `plan` |
| `researcher` | Gather and summarize facts/trends via `providers/search.py`. | `research` |
| `strategist` | Per-platform strategy (tone, length, posting notes) using workspace brand voice. | `strategy` |
| `content_router` | Conditional edge: `post` or `video` from `content_type`. On re-entry uses `state.rerun_from`. | — |
| `post_subgraph` | writer → image → hashtag, each with its own check (section 6.3). | `drafts` |
| `video_subgraph` | script → storyboard → scene(s) → assembly → voice/subtitles, each checked. | `drafts` |
| `reviewer` | Final LLM + rule review (quality, accuracy, policy, platform rules). Returns issues with `responsible_node`. | `review` |
| `route_after_review` | `ok` → approval; `fix` → re-enter the responsible node with feedback (max `max_review_cycles`, default 2; after that continue to approval with warnings). | `rerun_from` |
| `human_approval` | Writes content to DB, sets `awaiting_approval`, notifies, **interrupt**. Resume payload: `{action: approve|reject, regenerate: bool, feedback}`. | `approval` |
| `schedule_gate` | If `publish_at` is in the future: create publish job, **interrupt**. | — |
| `publisher` | For each target: publish via registry, store per-target result. Idempotent. | `publish_results` |
| `analytics_setup` | Create metric-collection jobs. | — |

### 6.2 State

```python
# app/agents/state.py
class TargetAccount(TypedDict):
    account_id: str
    platform: Literal["facebook", "instagram", "tiktok", "youtube"]

class PlatformDraft(TypedDict, total=False):
    caption: str
    hashtags: list[str]
    title: str                     # youtube / video
    media: list[dict]              # [{"type": "image|video|audio|subtitle", "storage_key": "...", "meta": {...}}]
    script: dict                   # video only
    storyboard: list[dict]         # video only
    scenes: list[dict]             # video only

class GraphState(TypedDict, total=False):
    request_id: str
    workspace_id: str
    user_id: str
    topic: str
    content_type: Literal["post", "video"]
    target_accounts: list[TargetAccount]
    publish_at: str                # ISO-8601 UTC
    brand_voice: str | None
    language: str
    options: dict                  # extra request options (tone override, ...); user-controlled
    plan: dict | None
    research: dict | None
    strategy: dict | None          # keyed by platform
    drafts: dict[str, PlatformDraft]   # keyed by platform
    retries: dict[str, int]        # keyed by step key, e.g. "writer", "scene:2"
    feedback: dict[str, str | None]    # last checker feedback per step key
    attempt_info: dict[str, dict]      # last attempt per step key; written only by checked.py
    review: dict | None
    review_cycles: int
    rerun_from: str | None
    approval: dict | None
    publish_results: list[dict]
    status: str
    errors: list[str]
```

**Never put tokens or secrets in state** (state is checkpointed to the DB). Publishers load credentials from `social_accounts` by `account_id`.

### 6.3 Checked-node pattern (retry loop)

Every generating agent is wrapped by one factory so behaviour is uniform:

```mermaid
flowchart LR
  A["agent node"] --> C{"check node"}
  C -->|pass| N["next node"]
  C -->|"fail and retries < max"| A
  C -->|"fail and retries >= max"| E["escalate node (interrupt: needs_attention)"]
  E -->|"resume: retry (counter reset)"| A
  E -->|"resume: abort"| X["end: failed"]
```

```python
# app/agents/checked.py  (B2-04)
def make_checked_node(key, agent_fn, check_fn, max_retries=3, *, judge_fn=None, judge_threshold=0.7) -> CheckedNode:
    """CheckedNode has .agent, .check, .escalate, .route, .route_resolution (+ node names).
    - agent_fn(state) -> state update (dict) or AgentOutput(update, prompt_version, summary, refused_reason);
      reads state.feedback[key]; may raise AppError.
    - check_fn(state) -> CheckResult (rule checks). judge_fn(state) -> CheckResult runs only if the rules pass
      and must reach judge_threshold.
    - route(state) -> "pass" | "retry" | "escalate"; route_resolution(state) -> "retry" | "abort"."""

def add_checked_node(graph, checked, *, on_pass, on_abort=END) -> str:  # wires the three nodes, returns the entry node
```

- `max_retries` is the number of **failed attempts** a step may have before it escalates, so the default 3 means three attempts in total (video scene: 2).
- **Bookkeeping** is in `state.retries[key]` (failed attempts since the last reset), `state.feedback[key]` (what the next attempt sees; `None` after a pass) and `state.attempt_info[key]` (attempt number, outcome, usage, issues of the last attempt). The attempt number keeps counting up across counter resets; the retry counter does not.
- Checks combine **rule checks** (length limits, hashtag count, image present/size, video duration/aspect ratio, audio-video sync, subtitle timing, banned words) with an **LLM judge** only where judgement is needed (tone, relevance, factual consistency). `CheckResult`/`Issue` are defined in `AGENT_SPECS.md` 1.5.
- Only `AppError`s (LLM provider/output errors, ...) count as a failed attempt (verdict `error`; the previous checker feedback is kept). Any other exception is a bug: it is recorded as an `error` step and re-raised. A non-retriable error (`retriable=False`, `LLMConfigError`) and a refusal (`AgentOutput.refused_reason`, AGENT_SPECS 1.7) escalate immediately.
- **Escalation** is a separate node that calls `interrupt()` with `{"type": "needs_attention", "step", "reason" (retries_exhausted | refused | non_retriable_error), "attempts", "max_retries", "feedback", "error_code", "refused", "issues"}`. The worker (B3) turns that interrupt into request status `needs_attention` and a notification. Resume value `{"action": "retry" | "abort"}` (via `POST /requests/{id}/resolve`, which must always send a non-empty object): `retry` resets that step's counter and keeps its feedback (`status` = `generating`); `abort` records a `failed` step, appends to `errors`, sets `status` = `failed` and goes to `on_abort`. Any other resume value is **not** an error: the node asks again (a new interrupt with `error: "invalid_resolution"`), because LangGraph would replay a raised bad value on every later resume.
- **Step logging:** one `agent_steps` row per attempt (`node_name` = key, e.g. `writer`, `scene:2`): verdict `passed`, `retried`, `escalated` or `error`, plus `failed` when a human aborts. Tokens, cost and model are the sum for the agent and the judge (`llm.capture_usage()`); `latency_ms` is the model time, `output_summary.duration_ms` the wall-clock time. Rows go through a recorder: `config["configurable"]["step_recorder"]` (callable, tests/dev runner) or `config["configurable"]["run_id"]` (database recorder, one transaction per row). With neither, nothing is stored. A recorder that fails is logged and does not stop the workflow.
- Log lines written inside an attempt carry `node`, `attempt` and `run_id` (context variables).
- Max retries come from `system_settings` (`max_retries_default=3`, `max_retries_scene=2`, `judge_pass_threshold=0.7`); the node builders read them and pass them in (the factory takes plain numbers).

### 6.4 Video subgraph details

`script_node` → `storyboard_node` → `scene_node` (loops over scenes; each scene has its own retry counter key `scene:<n>`; default max scenes 5) → `assembly_node` (FFmpeg: concat, transitions, resize to platform aspect ratio) → `voice_node` (TTS narration, SRT subtitles, mix/burn). Checks for assembly and voice are rule-based (duration, resolution, sync, subtitle timing). Regenerate only the failed scene, never the whole video.

### 6.5 Persistence and resume

- Checkpointer: LangGraph Postgres saver, `thread_id = request_id`. Created/migrated by the library's `setup()`, not by Alembic.
- Interrupt points: `human_approval`, `schedule_gate`, escalation.
- Resume with `graph.invoke(Command(resume=payload), config={"configurable": {"thread_id": request_id}})` from a Celery task (`workflow.resume`). Never run the graph inside an API request.
- Because DB is the source of truth for status, a lost checkpoint can be recovered by re-queuing from the last good status (documented in runbook section 12).

### 6.6 LLM layer (`app/agents/llm.py`, B2-01)

Agents never import a model SDK; they call this module only.

- `get_chat_model(kind)` with `kind` = `"text"` (strong, `LLM_MODEL_TEXT`) or `"fast"` (cheap, `LLM_MODEL_FAST`; falls back to the text model when unset). Provider from `LLM_PROVIDER`. Providers are plugged in with `register_provider(name, factory)`; a provider subclasses `ChatModel` and implements `_generate()` only. Today only `fake` is registered; any other value raises `LLMConfigError` (`llm_not_configured`).
- `generate_structured(schema, messages, kind=, temperature=)` returns a validated Pydantic object. If the reply is not valid JSON or fails validation it asks once more (`MAX_REPAIR_RETRIES = 1`) with the problem and the JSON schema; then raises `LLMOutputError` (`llm_invalid_output`). Provider failures raise `LLMProviderError` (`retriable` flag) and are **not** retried here: retry/escalation belongs to the checked-node pattern (6.3).
- `capture_usage()` is a context manager returning a `UsageRecorder` (calls, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `models`) for every successful call made inside it; recorders nest and are per-context. The checked node (B2-04) copies these into `agent_steps`.
- `fake` provider: `get_fake_script()` returns the shared `FakeScript` (`queue(...)`, `fail_next(n)`, `set_default(...)`, `calls`, `reset()`). An unscripted call raises `fake_llm_script_exhausted` so tests cannot pass by accident. Tests use the `fake_llm` fixture in `tests/conftest.py`, which forces `LLM_PROVIDER=fake` regardless of `.env`.
- Prompts and model replies are never logged and never appear in error messages or details; the log line carries provider, model, kind, token counts and latency only.

### 6.7 Run and step recording (`app/services/runs.py`, B2-02)

- `start_run(db, request_id)` creates a `workflow_runs` row (status `running`, `thread_id` = request id by default). Every (re)start of generation makes a new run.
- `record_step(db, run, node_name, attempt, verdict, ...)` adds one `agent_steps` row per node attempt. It takes plain numbers (`tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `model`) so services never import `app.agents`; the checked node copies them from `llm.capture_usage()`. Summaries are redacted and trimmed before storage; steps can only be added while the run is `running` or `paused`.
- `set_run_status(db, run, status)` is the only way a run changes status: `running` and `paused` alternate; `completed`, `failed`, `cancelled` are final, stamp `finished_at` and store `summarize_run()` in `summary`. Changing a finished run raises `ConflictError` (`run_finished`).
- Readers: `get_run` (`run_not_found`), `latest_run(request_id)`, `list_steps(run_id)`.
- Like `audit.log`, these functions flush but do not commit; the caller owns the transaction.

### 6.8 Prompts (`app/agents/prompts/`, B2-03)

- One Markdown file per prompt: `planner`, `researcher_queries`, `researcher_summary`, `strategist`, `writer`, `image`, `hashtag`, `reviewer` and the shared `judge` (video prompts come with B4). Format and rules: `AGENT_SPECS.md` 1.3.
- `load_prompt(name)` validates the file (front matter with `prompt_version`, sections in order, well-formed `{{variables}}`) and returns a `PromptTemplate`. `template.messages(feedback=None, **variables)` returns the system message (Role, Output format, Rules, Examples) and the user message (Task, Inputs, `# Previous attempt feedback` block on retries). `template.version` goes to `agent_steps.output_summary.prompt_version`.
- Rendering is strict (missing or unknown variables raise `PromptError`) and single-pass, so user text can never be expanded as a template. User-influenced text stays in the user message.
- `{{safety_rules}}` is built in (`prompts/fragments/safety_rules.md`, AGENT_SPECS 1.7).
- Prompt files are declared as package data in `pyproject.toml`; a test checks that every file is covered.

## 7. Interfaces (extension points)

```python
# app/publishers/base.py
class PublishResult(TypedDict):
    ok: bool
    platform_post_id: str | None
    post_url: str | None
    error: str | None
    retriable: bool

class Publisher(Protocol):
    platform: str
    def publish(self, account: SocialAccountCreds, draft: PlatformDraft) -> PublishResult: ...

# app/collectors/base.py
class MetricsCollector(Protocol):
    platform: str
    def fetch_metrics(self, account: SocialAccountCreds, platform_post_id: str) -> dict: ...
    def fetch_comments(self, account: SocialAccountCreds, platform_post_id: str, since: datetime | None) -> list[dict]: ...
```

- `publishers/registry.py::get_publisher(platform)` returns `MockPublisher` when `PUBLISHER_MODE=mock`, else the real class. Same for collectors.
- `MockPublisher` returns deterministic fake IDs, can be told to fail (`FAIL_PLATFORMS`, or a magic topic) to test partial failure and retries.
- Providers: `ImageProvider.generate(prompt, size) -> bytes`, `SceneProvider.generate(prompt, seconds) -> bytes`, `TTSProvider.speak(text, voice) -> bytes`, `SearchProvider.search(query) -> list[Result]`. Each has `Fake*` and at least one real implementation selected by config.
- `StorageBackend.put(key, bytes, mime) -> str`, `.get_url(key)`, `.delete(key)`.

## 8. API surface (v1, prefix `/api/v1`)

All workspace-scoped routes check ownership (`get_workspace_for_user` dependency). Admin routes require `role=admin`.

**OpenAPI conventions (B1-10).** `/docs`, `/redoc` and `/openapi.json` list every route once, under `/api/v1` (the same routers are also reachable at the root for backwards compatibility but are hidden from the schema; only the `/health*` probes are documented at the root). Each route has a tag (`auth`, `workspaces`, `admin`, `health`), a summary, and request/response examples (`json_schema_extra` on the Pydantic models). Errors are documented with the real envelope `{"error": {"code", "message", "details"}}` through `app/api/docs.py::errors(...)` (shared `ErrorResponse` schema; FastAPI's default `HTTPValidationError` is not used). `tests/unit/test_openapi.py` enforces all of this and checks that examples validate against their models.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/forgot-password`, `/auth/reset-password`; `GET /auth/me` |
| Users | `GET/PATCH /users/me` |
| Workspaces | `GET/POST /workspaces` (list is the caller's own, paginated, newest first); `GET/PATCH/DELETE /workspaces/{id}` (PATCH is partial; DELETE is a soft delete and needs `?confirm=true`; someone else's or a deleted workspace is 404; `POST` returns 403 `workspace_limit_reached` when the user is at their effective `max_workspaces`; an admin reaching another user's workspace is recorded in `audit_logs` as `workspace.admin_access`, and deletes as `workspace.delete`) |
| Accounts | `GET /workspaces/{id}/accounts`; `POST /workspaces/{id}/accounts/dev` (dev token, disabled in production); `POST /workspaces/{id}/accounts/connect/{platform}` (returns OAuth URL); `GET /oauth/{platform}/callback`; `DELETE /accounts/{id}` |
| Requests | `POST /workspaces/{id}/requests`; `GET /workspaces/{id}/requests`; `GET /requests/{id}` (status, steps summary); `PATCH /requests/{id}/schedule`; `POST /requests/{id}/cancel`; `POST /requests/{id}/resolve` (escalation) |
| Approval | `GET /requests/{id}/preview`; `PATCH /requests/{id}/content`; `POST /requests/{id}/approve` (`publish_now`, `reschedule_to` optional); `POST /requests/{id}/reject` (`feedback`, `regenerate`) |
| Library | `GET /workspaces/{id}/library`; `GET /content/{id}`; `DELETE /content/{id}`; `GET /media/{asset_id}` |
| Analytics | `GET /workspaces/{id}/analytics`; `GET /accounts/{id}/analytics`; `GET /requests/{id}/analytics`; `GET /requests/{id}/comments`; `POST /requests/{id}/analytics/refresh` |
| Notifications | `GET /notifications`; `POST /notifications/{id}/read`; `POST /notifications/read-all` |
| Admin | `GET/POST /admin/users`; `PATCH/DELETE /admin/users/{id}`; `PATCH /admin/users/{id}/limits`; `GET /admin/workspaces`; `PATCH/DELETE /admin/workspaces/{id}`; `GET/PUT /admin/settings`; `GET /admin/workflows`; `GET /admin/workflows/{run_id}`; `GET /admin/queue`; `POST /admin/requests/{id}/cancel`; `GET/POST/DELETE /admin/moderation-rules`; `GET /admin/usage`; `GET /admin/reports/export`; `GET /admin/audit-logs` |
| System | `GET /health` (liveness), `GET /health/ready` (db, redis) |

Conventions: JSON only; ISO-8601 UTC timestamps; UUID ids; pagination `?limit=&offset=` returning `{items, total}`; errors as `{error: {code, message, details}}`; validation errors 422; auth errors 401/403; not found (or not yours) 404. Request status values are exactly those in section 4.

## 9. Security

- Passwords: argon2 or bcrypt. JWT access token 15–30 min, refresh token 7–30 days stored hashed and revocable.
- Platform tokens: Fernet-encrypted columns; key from `FERNET_KEY`; never logged, never in API responses, never in graph state.
- OAuth: `state` parameter stored server-side to prevent CSRF; minimal scopes per platform.
- Authorization: ownership checks centralised in dependencies; admin bypass explicit and audited.
- Input: Pydantic validation; max lengths; moderation rules applied in `validate_input` and `reviewer`.
- Rate limiting on auth and expensive endpoints; CORS allowlist from env.
- Dev-only endpoints (`/accounts/dev`) hard-disabled when `ENVIRONMENT=production`.
- Audit log for admin actions and approvals.

## 10. Configuration (`.env.example`)

```
ENVIRONMENT=development            # development | test | production
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/sma
REDIS_URL=redis://localhost:6379/0
SECRET_KEY=change-me
ACCESS_TOKEN_MINUTES=30
REFRESH_TOKEN_DAYS=14
FERNET_KEY=generate-with-cryptography
CORS_ORIGINS=http://localhost:3000

LLM_PROVIDER=fake                  # fake (offline, default) | gemini (planned, not implemented yet)
GEMINI_API_KEY=
LLM_MODEL_TEXT=                    # set explicitly; do not hard-code in code
LLM_MODEL_FAST=
IMAGE_PROVIDER=fake                # fake | <real provider>
VIDEO_PROVIDER=fake
TTS_PROVIDER=fake
SEARCH_PROVIDER=fake

STORAGE_BACKEND=local              # local | s3
STORAGE_PATH=./storage
S3_ENDPOINT= S3_BUCKET= S3_ACCESS_KEY= S3_SECRET_KEY=

PUBLISHER_MODE=mock                # mock | real
FACEBOOK_APP_ID= FACEBOOK_APP_SECRET=
GOOGLE_CLIENT_ID= GOOGLE_CLIENT_SECRET=
TIKTOK_CLIENT_KEY= TIKTOK_CLIENT_SECRET=

SEED_ADMIN_EMAIL=admin@example.com
SEED_ADMIN_PASSWORD=change-me
```

Runtime-tunable values (retry limits, lead times, metrics schedule, quotas, platform rules) live in the `system_settings` table, editable by Admin, with defaults in code.

## 11. Observability and errors

- JSON logs with `request_id`, `run_id`, `node`, `attempt`.
- HTTP access log (B1-11): `AccessLogMiddleware` writes exactly one JSON line per request to the `app.access` logger with `request_id`, `method`, `path` (no query string), `route` (matched template), `status_code`, `duration_ms` and `client_ip`; level INFO, ERROR for 5xx/unhandled errors, WARNING if the client left before a response. Middleware order (outer to inner): `RequestIDMiddleware` -> `AccessLogMiddleware` -> CORS -> routes, so CORS preflights are logged too. Bodies, headers and query strings are never logged. Uvicorn's own access log is disabled (`--no-access-log` and in `setup_logging`) and its startup/shutdown lines are routed through the same JSON formatter, so API container output is JSON only.
- `JSONFormatter` copies only an allow-list of `extra=` fields (`STRUCTURED_FIELDS` in `core/logging.py`); any other `extra=` value is ignored on purpose because some call sites pass email addresses. Add a field to the allow-list only if it can never hold personal data or secrets.
- Every node attempt → `agent_steps` (verdict, feedback, tokens, cost, latency). Admin monitoring endpoints read from it.
- Domain errors derive from `AppError(code, message, http_status)`; one exception handler maps them to the error format.
- Celery tasks: bounded retries with exponential backoff for retriable errors; non-retriable errors set status `failed` and notify.

## 12. Runbook notes

- Start everything: `docker compose up`. Migrate: `make migrate`. Seed admin: `make seed`.
- Stuck request: look at `content_requests.status`, `scheduled_jobs`, and the latest `agent_steps`. If a job is `running` for too long, a janitor task (`scheduler.reclaim_stale_jobs`) resets it to `pending`.
- Lost checkpoint: set request back to `queued` and insert a `start_generation` job (content is regenerated).

## 13. Testing strategy

- **Unit:** each node with `LLM_PROVIDER=fake`; check functions with fixed inputs; services; status machine transitions.
- **Graph tests:** retry path (fake LLM fails twice then passes), escalation path, review `fix` loop, reject+regenerate, approve.
- **Integration:** API + DB (test DB via Docker or transaction rollback), Celery tasks run eagerly.
- **E2E ("Backend Core Complete")**: the scenario in `PRD.md` section 12, with `PUBLISHER_MODE=mock`.
- Real platform/LLM calls are never made in CI; real publishers have contract tests behind an opt-in marker (`-m live`).

## 14. Key decisions (ADR-lite)

| # | Decision | Why |
|---|---|---|
| D1 | Scheduler is a service + `scheduled_jobs` + Beat poller, not a graph node | Survives restarts, easy to cancel/reschedule, avoids long sleeps. |
| D2 | LangGraph `interrupt()` for approval, publish-time wait and escalation | Native pause/resume; state checkpointed in Postgres. |
| D3 | Generate once per platform format, publish to many accounts | Saves cost; accounts on the same platform get identical content. |
| D4 | Mock publishers/collectors first | Platform approvals take long; pipeline must be testable offline. |
| D5 | Sync SQLAlchemy | Simpler with Celery workers; API load is low. Revisit only if needed. |
| D6 | One `checked` factory for all agents | Uniform retry/escalation and uniform step logging. |
| D7 | Tokens never in graph state | State is persisted; secrets must stay encrypted in `social_accounts`. |
| D8 | Status transitions only via `transition()` | Single place for validation, notifications and audit. |

Add new decisions here (D9, D10, ...) and mirror them in `PROGRESS.md` Decisions log.

### 6.9 `validate_input` and `planner` (`app/agents/nodes/`, B2-05)

- `validate_input` (`nodes/validate_input.py`) uses no LLM and no database. `make_validate_input_node(banned_words=, platform_rules=, check_accounts=, check_quota=)` returns the node; each hook is a plain callable evaluated on every run. Defaults: `no_banned_words` (stub until `moderation_rules` exists, B5-08) and `platform_rules.default_platform_rules` (the code defaults of AGENT_SPECS section 2). `check_accounts(state)` (account active, right workspace, can post this type) and `check_quota(state)` need B3 tables and run only if the request is well formed. All problems are collected; with any, the node returns `{"status": "failed", "errors": [...]}` and `route_after_validation` returns `"failed"` (the graph ends), otherwise `{"status": "generating"}` and `"ok"`. Error messages are for the submitter and never name the banned word that matched.
- `agents/platform_rules.py` holds the default per-platform rules and `platform_supports(rules, platform, content_type)`: an explicit `allowed_formats` list wins, otherwise `image_aspect` means `post` and `video_aspect` means `video`.
- `planner` (`nodes/planner.py`): `make_planner_node(max_retries=, judge_threshold=, use_judge=, platform_rules=)` returns a `CheckedNode` (section 6.3) with step key `planner`. The agent renders prompt `planner` and stores `ContentPlan.model_dump()` in `state["plan"]`; rule checks: 2 to 5 non-empty key messages, angle and audience filled, plan language equal to the requested one (case-insensitive, `_` equals `-`); the judge (prompt `judge`, `kind="fast"`, rubric in code) sees the plan and the rules of the target platforms only. `ContentPlan` enforces only the shape (`goal` values), so rule failures get retry feedback instead of a silent schema repair.
