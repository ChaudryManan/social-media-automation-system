# PRD — Social Media Multi-Agent Automation Platform

> Status: v1.0 (backend-first). Source of truth for WHAT we build.
> Related: `ARCHITECTURE.md` (how), `DATABASE.md` (data), `TASKS.md` (work plan), `AGENTS.md` (agent rules), `PROGRESS.md` (live status).

---

## 1. Overview

A multi-agent system that turns a **topic** into a published social media **post or video**.
The user only provides a topic (plus workspace, target accounts, content type, upload time).
A team of AI agents researches, plans, writes/generates, reviews and checks the content.
A human approves it, the system publishes it to **Facebook, Instagram, TikTok and YouTube** at the scheduled time,
and then collects **reactions and comments** back from each platform.

This is a final-year project. It is built **backend first** (all agent work from user input to publish and feedback),
then integrated with a frontend.

## 2. Goals

| ID | Goal |
|---|---|
| G1 | Minimal user effort: topic in, published content out. |
| G2 | Quality through collaboration: every agent output is checked and retried before moving on. |
| G3 | Human stays in control: nothing is published without approval. |
| G4 | Multi-brand support: a user can run several workspaces, each with several accounts per platform. |
| G5 | Close the loop: pull comments and metrics back so users can see results. |
| G6 | Safe to operate: Admin can monitor, moderate, limit and audit everything. |

## 3. Non-goals (v1)

- Frontend UI (comes after the backend core is complete; see phase B6).
- Team workspaces with multiple members/roles (a workspace has exactly one owner).
- Replying to comments from inside the system (read-only feedback in v1).
- Paid ads, influencer tools, cross-posting of third-party content.
- Mobile apps.

## 4. Actors

| Actor | Description |
|---|---|
| **User** | Creates workspaces, connects accounts, submits requests, approves content, views analytics. |
| **Admin** | Full access. Everything a User can do, plus user management, system config, monitoring, moderation, usage reports. |
| **Platform APIs** (secondary) | Facebook, Instagram, TikTok, YouTube. Receive posts; return post IDs, comments, metrics. |
| **AI agents** (internal) | Not human actors. Run the LangGraph workflow. |

## 5. Glossary

- **Workspace**: a container (brand/client/project) owned by one user. Has its own settings and connected accounts.
- **Social account**: one connected account/page/channel on a platform inside a workspace. Several per platform allowed.
- **Content request**: one submission (topic + targets + type + publish time).
- **Content item**: the generated output for one platform format (caption/image/hashtags or video/subtitles), versioned.
- **Target**: one (request, social account) pair; publish status and metrics are tracked per target.
- **Agent step**: one attempt of one agent node, logged for monitoring.
- **Escalation**: workflow pauses for a human after retries are exhausted.

## 6. Core user journey (happy path)

1. User logs in, selects a workspace, picks target accounts, enters topic, content type (post/video) and upload time.
2. System validates input and stores the schedule.
3. At the lead time before upload, the agent workflow runs: plan → research → strategy → generate → check/retry → final review.
4. User gets a notification, previews, optionally edits, then approves (or rejects with feedback → regenerate).
5. At the scheduled time the system publishes to each selected account.
6. System periodically fetches comments/metrics; user views them per post, account and workspace.

## 7. Functional requirements

Priority: **M** = Must, **S** = Should, **C** = Could. Module = see section 11.

### 7.1 Authentication & access (M1)
| ID | Requirement | Pri |
|---|---|---|
| FR-1.1 | Register, log in, log out, refresh session, get current user. | M |
| FR-1.2 | Password reset flow (token-based). | S |
| FR-1.3 | Role-based access: `user` and `admin`. Admin routes forbidden to users. | M |
| FR-1.4 | Every workspace-scoped resource is accessible only by its owner (or an admin). | M |

### 7.2 Workspaces (M2)
| ID | Requirement | Pri |
|---|---|---|
| FR-2a.1 | Create, list, rename, update and delete (soft) workspaces. | M |
| FR-2a.2 | Workspace settings: brand voice/tone, default language, timezone, default platforms. | M |
| FR-2a.3 | Deleting a workspace asks for confirmation (API flag) and cancels its scheduled items. | S |
| FR-2a.4 | Per-user limits (max workspaces) enforced. | S |

### 7.3 Social accounts (M3)
| ID | Requirement | Pri |
|---|---|---|
| FR-2.1 | Connect an account to a workspace (OAuth). Several accounts per platform allowed. | M |
| FR-2.2 | Each account has a display label; the same platform account is connected once per workspace. | M |
| FR-2.3 | Tokens are encrypted at rest, refreshed before expiry; failures mark the account `expired` and notify the user. | M |
| FR-2.4 | Disconnect an account (its history is kept; scheduled items targeting it are flagged). | M |
| FR-2.5 | Dev mode: accounts can be attached with manually supplied tokens (no OAuth redirect needed). | M |

### 7.4 Content request & scheduling (M4, M5)
| ID | Requirement | Pri |
|---|---|---|
| FR-3.1 | Submit request: workspace, topic, content type, target accounts (>=1), publish time. | M |
| FR-3.2 | Validate input (non-empty topic, accounts belong to the workspace and are active, time in the future, moderation rules). Invalid input returns clear errors and does not start a workflow. | M |
| FR-4.1 | Save publish time on submission; start generation at a lead time before it (video earlier than post; configurable). If the lead time already passed, start immediately. | M |
| FR-4.2 | List, reschedule and cancel scheduled requests. | M |
| FR-4.3 | If content is not approved by the publish time, hold it (`held`) and notify; the user may approve later and choose "publish now" or reschedule. | M |

### 7.5 Content generation (M6, M7, M8)
| ID | Requirement | Pri |
|---|---|---|
| FR-5.1 | Planner, researcher and strategist agents produce a plan, research notes and a per-platform strategy using the workspace brand voice. | M |
| FR-5.2 | Content router sends the request to the post flow or the video flow. | M |
| FR-5.3 | Post flow: caption/body, image, hashtags. | M |
| FR-5.4 | Video flow: script, storyboard, scenes, assembled video, voice and subtitles. | M |
| FR-5.5 | Content is generated once per distinct platform format and reused for all accounts of that platform. | M |

### 7.6 Quality control (M6)
| ID | Requirement | Pri |
|---|---|---|
| FR-6.1 | Every agent output passes a check (rule-based and/or LLM judge). | M |
| FR-6.2 | On failure, the same agent runs again with the checker's feedback. | M |
| FR-6.3 | Retry limit per step (default 3; video scenes 2). When exhausted, the workflow pauses for a human (`needs_attention`). | M |
| FR-6.4 | A final reviewer checks quality, accuracy, content policy and platform rules; if it finds problems it routes back to the responsible agent (max 2 review cycles). | M |

### 7.7 Human approval (M9)
| ID | Requirement | Pri |
|---|---|---|
| FR-7.1 | Preview of the final content per platform. | M |
| FR-7.2 | Edit text (caption, hashtags, title) before approving. | M |
| FR-7.3 | Approve; or reject with feedback and choose regenerate or stop. | M |
| FR-7.4 | Nothing is ever published without an approval record. | M |

### 7.8 Publishing (M10)
| ID | Requirement | Pri |
|---|---|---|
| FR-8.1 | Publish approved content to every target account at the scheduled time, formatted per platform. | M |
| FR-8.2 | Track status per target (`pending`, `publishing`, `published`, `failed`). One failing target does not block the others. | M |
| FR-8.3 | Retry temporary API failures with backoff; never publish the same target twice (idempotent). | M |
| FR-8.4 | Publisher interface per platform plus a `MockPublisher` for development and tests. | M |

### 7.9 Analytics & feedback (M11)
| ID | Requirement | Pri |
|---|---|---|
| FR-9.1 | Collect likes, comments count, shares, views, reach per target on a schedule (e.g. +1h, +6h, +24h, then daily up to 30 days). | M |
| FR-9.2 | Store and expose individual comments (author, text, time). | M |
| FR-9.3 | Analytics endpoints per request, per account and per workspace; manual refresh. | M |

### 7.10 Content library (M13)
| ID | Requirement | Pri |
|---|---|---|
| FR-10.1 | List/search/filter history by workspace, status, platform, date. | S |
| FR-10.2 | Download media; delete drafts/old content. | S |

### 7.11 Notifications (M12)
| ID | Requirement | Pri |
|---|---|---|
| FR-11.1 | In-app notifications for: ready for approval, needs attention, held, published, failed, account expired. | M |
| FR-11.2 | Optional email delivery (interface now, provider later). | C |

### 7.12 Admin (M14)
| ID | Requirement | Pri |
|---|---|---|
| FR-12.1 | Admin manages users: create, edit, suspend/activate, delete, change role. | M |
| FR-13.1 | System settings: retry limits, lead times, models/providers, platform rules, quotas. | M |
| FR-14.1 | Monitoring: workflow runs, per-agent steps, failures, scheduler queue, health. | M |
| FR-15.1 | Moderation: banned-word rules; view all content; remove/block; cancel or override scheduled posts. | S |
| FR-16.1 | Usage and cost reports per user; platform-wide analytics; export (CSV). | S |
| FR-16.2 | Admin views/suspends/deletes any workspace and sets per-user limits. | S |

## 8. Use case index

| ID | Use case | Actor |
|---|---|---|
| UC-01 | Register / log in | User, Admin |
| UC-02 | Connect social account (in a workspace) | User, Admin |
| UC-03 | Submit content request | User, Admin |
| UC-04 | Manage schedule | User, Admin |
| UC-05 | Preview generated content | User, Admin |
| UC-06 | Edit and approve content | User, Admin |
| UC-07 | Reject / regenerate content | User, Admin |
| UC-08 | Publish content (system) | System, Platform APIs |
| UC-09 | View analytics and comments | User, Admin |
| UC-10 | View content history | User, Admin |
| UC-11 | Manage users | Admin |
| UC-12 | Configure system settings | Admin |
| UC-13 | Monitor workflows and logs | Admin |
| UC-14 | Moderate content | Admin |
| UC-15 | View usage and cost reports | Admin |
| UC-16 | Create workspace | User, Admin |
| UC-17 | Switch workspace | User, Admin |
| UC-18 | Edit or delete workspace | User, Admin |
| UC-19 | Disconnect social account | User, Admin |
| UC-20 | Manage workspaces and limits | Admin |

The Admin inherits all User use cases.

### UC-03 Submit content request (detail)
- **Precondition:** logged in; workspace has >=1 active account.
- **Main flow:** enter topic, type, accounts, time → validate and save schedule → (at lead time) agents plan/research/strategy → router → generate with per-step checks/retries → final review → notify user "ready for approval".
- **Alternative:** invalid input → error, nothing saved.
- **Exception:** retries exhausted → `needs_attention`, user notified.
- **Postcondition:** content awaiting approval, schedule saved.

### UC-06 Edit and approve content (detail)
- **Precondition:** request status `awaiting_approval`.
- **Main flow:** open preview → optional edits → approve → system schedules publish → publishes at time → collects analytics.
- **Alternative:** reject → regenerate with feedback (UC-07).
- **Exception:** publish time passes without approval → `held` + notification.

## 9. Non-functional requirements

| Area | Requirement |
|---|---|
| Security | Passwords hashed (argon2/bcrypt); JWT access + refresh; platform tokens encrypted (Fernet); secrets only from env; ownership checks on every workspace route; rate limiting on auth; CORS allowlist. |
| Reliability | Workflow state is checkpointed (resumable after crash/restart). Publishing and job execution are idempotent. Jobs use DB locking (`FOR UPDATE SKIP LOCKED`). |
| Performance | API p95 < 500 ms for non-generation endpoints. Generation is asynchronous (workers); the API never blocks on LLM calls. |
| Cost control | Retry caps, per-user monthly generation limit, per-step token/cost logging. |
| Observability | Structured JSON logs with `request_id`, `run_id`, `node`; every agent attempt stored in `agent_steps`; health endpoint. |
| Extensibility | New platform = new publisher + collector class, no workflow change. New model/provider = config change. |
| Testability | All LLM and platform calls behind interfaces with fake/mock implementations; full pipeline testable offline. |
| Portability | `docker compose up` runs the whole backend. |
| Privacy | Do not log tokens or full prompts containing user secrets; soft-delete workspaces; ability to hard-delete a user's data on request. |

## 10. Assumptions, constraints, risks

- **Platform API access:** Meta (Facebook/Instagram), TikTok and YouTube each require developer apps and, for production use, may require app review/audit, business accounts or quota approval. TikTok approval is typically the slowest. Requirements change; verify against current official docs before implementing each publisher. Therefore development uses `MockPublisher` and `PUBLISHER_MODE=mock` first.
- **Generative media cost/latency:** video generation is slow and costly; hence retry caps, lead times and quotas.
- **LLM variability:** outputs are non-deterministic; checks and the retry loop exist for that reason; tests use fake LLMs.
- **Time zones:** all timestamps stored in UTC; workspace timezone used only for display and input interpretation.
- **Content policy:** platform policies and copyright apply; reviewer and moderation rules reduce but do not eliminate risk.

## 11. Modules and release phases

| Module | Name | Phase |
|---|---|---|
| M0 | Foundation | B1 |
| M1 | Auth and roles | B1 |
| M2 | Workspaces | B1 |
| M6 | Agent core (planner, researcher, strategist, router, check/retry) | B2 |
| M7 | Post flow | B2 |
| M3 | Social accounts | B3 |
| M4 | Content request | B3 |
| M5 | Scheduler and queue | B3 |
| M9 | Review and approval | B3 |
| M10 | Publishing | B3 (first platform), B4/B5 (others) |
| M11 | Analytics and comments | B3 |
| M8 | Video flow | B4 |
| M12 | Notifications | B3 (core), B5 (extras) |
| M13 | Content library | B5 |
| M14 | Admin panel APIs | B5 |
| M15 | Testing and deployment | B6 |

Phases: **B1** Foundation → **B2** Agent engine (post) → **B3** Core pipeline end-to-end → **B4** Video → **B5** Remaining backend → **B6** Frontend integration and delivery.
Detailed work items: `TASKS.md`.

## 12. Acceptance: "Backend Core Complete" (end of B3)

All steps work via Swagger/pytest with `PUBLISHER_MODE=mock`:

1. User registers and logs in.
2. User creates a workspace.
3. User attaches two accounts on the same platform (dev token).
4. User submits a post request with a publish time.
5. Workflow runs; status becomes `awaiting_approval`; a notification exists.
6. User edits the caption and approves.
7. At publish time each target is published (mock); status `published`.
8. Collector stores metrics and comments; analytics endpoints return them.
9. A forced failure in one agent triggers retries, then `needs_attention`, and resolving it continues the flow.
10. A request not approved in time becomes `held`.

## 13. Open questions

- Which LLM and image/video providers are used first? (default: Gemini for text; others behind interfaces)
- Which platform gets the first real publisher? (default: Facebook Pages)
- Email provider for password reset/notifications? (default: console logger)
