# AGENT_SPECS — Behaviour of every agent node

> Read this before implementing or changing ANY node under `backend/app/agents/` (tasks B2-xx and B4-xx).
> Structure and wiring: `ARCHITECTURE.md` section 6. State fields: `ARCHITECTURE.md` 6.2. Step logging: `DATABASE.md` 4.11.
> If you change a schema or rule here, update the code, tests and this file together, and log it in `PROGRESS.md` Decisions.

---

## 1. Global conventions

### 1.1 Structured output
- Every LLM call that feeds the workflow returns a **Pydantic model** through `generate_structured(schema, messages)` (one automatic repair retry on invalid JSON). Free-form text is only allowed inside string fields.
- Provider failures (`LLMProviderError`) are not repaired or retried by `generate_structured`; the checked node decides (ARCHITECTURE 6.3, 6.6). After the repair attempt fails it raises `LLMOutputError`.
- All schemas below are the contract. Field names are fixed; add optional fields only with a doc update.
- Every output model includes `language: str` (BCP-47 or plain name, e.g. `en`, `ur`, `roman-ur`) when it contains user-facing text.

### 1.2 Language and brand voice
- Output language = `request.options.language` if set, else `workspace.default_language`.
- `workspace.brand_voice` (free text) is passed to planner, strategist, writer, script and reviewer. Writer/script must follow it; reviewer must check it.
- Never mix languages unless the brand voice asks for it.

### 1.3 Prompts
- One file per agent in `app/agents/prompts/<agent>.md` with sections: `# Role`, `# Task`, `# Inputs`, `# Output format`, `# Rules`, `# Examples` (optional).
- Variables use `{{name}}`. Prompts contain no secrets and no hard-coded model names.
- Each prompt file has a `prompt_version` (e.g. `writer.v1`) stored in `agent_steps.output_summary.prompt_version`.
- Retry prompts append a `# Previous attempt feedback` block (see 1.5).

### 1.4 Generation settings (defaults, override in settings)
| Agent | Temperature | Notes |
|---|---|---|
| planner, strategist | 0.5 | structured, stable |
| researcher (summarizer) | 0.2 | factual |
| writer, script | 0.8 | creative |
| hashtag | 0.5 | |
| image prompt, storyboard, scene prompts | 0.7 | |
| judges / reviewer | 0.0–0.2 | deterministic as possible |

Optional setting `retry_model_upgrade=true`: use the stronger `LLM_MODEL_TEXT` on the last allowed attempt, the cheaper `LLM_MODEL_FAST` before that.

### 1.5 Check result and feedback format
Every check (rule or judge) returns:

```python
class Issue(BaseModel):
    code: str            # stable machine code, e.g. "caption_too_long"
    message: str         # actionable, specific, <= 200 chars
    severity: Literal["error", "warning"]

class CheckResult(BaseModel):
    ok: bool             # False if any error-severity issue
    score: float | None  # 0..1 from judges, None for pure rule checks
    issues: list[Issue]
    feedback: str        # concatenated error messages (max 500 chars) + "Keep: <what was good>" if known
```

- **Order:** run cheap **rule checks first**. Run the LLM judge only if rules pass.
- Judge pass threshold: `score >= 0.7` and no error issue (setting `judge_pass_threshold`).
- `feedback` is what the next attempt sees. It must say **what to change**, not just "bad".
- Warnings never block; they are shown to the human on the preview (`content_items.warnings`).

### 1.6 Judge pattern (LLM-as-judge)
Judge prompt receives: the agent's output, the plan/strategy it must satisfy, platform rules, and the rubric for that agent (below). It returns `CheckResult` with `score` and issues. Judges must not rewrite content; they only evaluate.

### 1.7 Safety rules for all generating agents
- No fabricated facts, statistics, quotes, or sources. Claims must come from `research` or be general, non-specific statements.
- No medical, legal or financial guarantees; no misleading or sensational claims presented as fact.
- No hate, harassment, sexual content, dangerous instructions, impersonation of real people, or copyrighted lyrics/passages.
- No competitor or brand attacks unless the brief explicitly asks and it is factual.
- Respect `moderation_rules` (banned words) — they are rule checks in `validate_input` and `reviewer`.
- If the topic is unsafe or cannot be done safely, the agent returns `refused=true` with `reason`; the graph escalates (`needs_attention`) instead of producing content.

### 1.8 Step logging
Each attempt writes one `agent_steps` row: `node_name`, `attempt`, `verdict`, `feedback`, trimmed `input_summary`/`output_summary`, tokens, cost, latency, model. Never store full prompts that include user secrets.

## 2. Platform rules (config-driven defaults)

Stored in `system_settings.platform_rules`. **These are starting values only — verify against each platform's current official documentation before relying on them, and update the setting, not the code.**

```json
{
  "facebook":  {"caption_max": 63000, "hashtags_min": 0, "hashtags_max": 5,  "image_aspect": ["1:1", "4:5", "16:9"], "video_aspect": ["16:9", "9:16", "1:1"]},
  "instagram": {"caption_max": 2200,  "hashtags_min": 3, "hashtags_max": 15, "image_aspect": ["1:1", "4:5"],          "video_aspect": ["9:16"], "video_max_seconds": 90},
  "tiktok":    {"caption_max": 2200,  "hashtags_min": 2, "hashtags_max": 8,  "video_aspect": ["9:16"], "video_max_seconds": 180},
  "youtube":   {"title_max": 100, "description_max": 5000, "tags_max_chars": 500, "hashtags_min": 0, "hashtags_max": 15, "video_aspect": ["16:9", "9:16"], "video_max_seconds": 180}
}
```

Rules in checks read from this setting. `*_max_seconds` for YouTube Shorts-style vs long-form is decided by `strategy.video.format` (`short` or `long`); keep both entries configurable.

## 3. Shared (pre-generation) agents

### 3.1 `validate_input`
- **Purpose:** last validation before spending LLM money.
- **Input:** `topic`, `content_type`, `target_accounts`, `publish_at`, `options`, workspace + moderation rules.
- **Output:** no LLM required. Writes `errors: list[str]` and ends the graph with status `failed` if any.
- **Rule checks:** topic length 3–500 chars, not only symbols; accounts active; content type supported by every target platform (e.g. no `video` target on an account that cannot post video — error); banned words; per-user generation quota not exceeded.
- **Optional LLM safety classifier:** off by default (`safety_classifier_enabled`); if on, returns `{safe: bool, reason}`.

### 3.2 `planner`
- **Input:** topic, content_type, target platforms, brand_voice, language, options.
- **Output:**
```python
class ContentPlan(BaseModel):
    angle: str                     # the single main idea/hook
    audience: str
    goal: Literal["awareness", "engagement", "education", "promotion", "other"]
    key_messages: list[str]        # 2..5
    tone: str
    cta: str | None
    format_notes: str              # post vs video, length direction
    must_avoid: list[str]          # sensitive topics, claims
    risks: list[str]               # policy/accuracy risks to watch
    language: str
```
- **Prompt rules:** one clear angle; messages must serve the goal; respect brand voice; no invented data.
- **Rule checks:** 2–5 key messages, non-empty angle/audience, language matches requested.
- **Judge rubric (0–1):** relevance to topic, clarity of angle, audience fit, brand-voice fit.

### 3.3 `researcher`
- **Input:** plan, topic, language.
- **Process:** LLM proposes up to **5 search queries** → `SearchProvider.search()` → LLM summarizes results into notes. If no results or provider fails: continue with `grounded=false`.
- **Output:**
```python
class Fact(BaseModel):
    claim: str
    source_url: str | None
    source_title: str | None
    confidence: Literal["high", "medium", "low"]

class ResearchNotes(BaseModel):
    facts: list[Fact]              # each fact must have source_url or be marked low confidence
    trends: list[str]
    angles: list[str]              # extra angle ideas
    cautions: list[str]            # outdated/controversial/uncertain points
    grounded: bool                 # False if no usable sources
```
- **Rules:** never output a specific number/date/quote without a source; when `grounded=false` set `cautions` to say "avoid specific statistics".
- **Rule checks:** every `high/medium` fact has a `source_url`; at most 15 facts; no duplicate claims.
- **Judge rubric:** relevance, source quality, no hallucinated specifics. (Skip judge when `grounded=false`.)

### 3.4 `strategist`
- **Input:** plan, research, brand_voice, per-platform rules, `content_type`, targets (distinct platforms only).
- **Output:** `dict[platform, PlatformStrategy]`:
```python
class PlatformStrategy(BaseModel):
    platform: str
    tone: str
    structure: str                 # e.g. "hook line, 3 short paragraphs, CTA"
    length_target: int             # characters for posts, seconds for video
    cta: str | None
    hashtag_count: int             # within platform min/max
    media_notes: str               # image/video direction
    posting_notes: str
    video: VideoStrategy | None    # only if content_type == "video"

class VideoStrategy(BaseModel):
    format: Literal["short", "long"]
    aspect_ratio: str              # must be allowed for the platform
    target_duration_s: int         # within platform max
    scene_count: int               # 3..max_scenes (default max 5)
    style: str                     # visual style guide for consistency
```
- **Rule checks:** one strategy per target platform; values within platform rules; scene_count within settings.
- **Judge rubric:** platform fit, consistency with plan, brand-voice fit.

## 4. Post flow

Dependencies inside the post flow: `writer → hashtag` (hashtags read the final caption). `image` depends on `writer` output (for relevance) but is regenerated only if the reviewer or judge targets it.

### 4.1 `writer`
- **Input (per platform):** plan, research, that platform's strategy, brand_voice, language, `feedback["writer"]`.
- **Output:**
```python
class WriterOutput(BaseModel):
    caption: str                   # body text WITHOUT hashtags
    title: str | None              # required for youtube, else None
    hook: str                      # first line (also appears in caption)
    cta: str | None
    language: str
    refused: bool = False
    reason: str | None = None
```
- **Prompt rules:** strong first line; follow strategy structure and length; use only grounded facts; follow brand voice; allowed emoji only if brand voice allows; platform-native style (Facebook: conversational; Instagram: visual-first, short lines; YouTube: SEO-friendly title and description; TikTok: casual, hook-driven).
- **Rule checks:** within platform length limit and within ±30% of `length_target`; non-empty hook; no banned words; no hashtags inside caption body (hashtag agent owns them); no raw URLs unless strategy allows; no unfilled placeholders like `[brand]` or `{{`; language matches.
- **Judge rubric:** hook strength, clarity, tone/brand-voice fit, factual safety (claims supported by research), CTA fit, absence of filler.

### 4.2 `image`
- **Step A (LLM): image prompt.**
```python
class ImagePrompt(BaseModel):
    prompt: str                    # concrete visual description
    negative_prompt: str | None
    aspect_ratio: str              # allowed for platform
    alt_text: str                  # accessibility text, <= 200 chars
    style_notes: str
```
  Rules: describe subject, setting, style, lighting, composition; **no text rendered in the image** unless explicitly requested; no real people or brand logos; consistent with caption and brand voice.
- **Step B:** `ImageProvider.generate(prompt, size)` → bytes → `StorageBackend.put` → `media_assets` row (`type=image`).
- **Rule checks:** file is a valid image; format png/jpg/webp; size under limit; aspect ratio allowed; min resolution (default 1024 on the short side, setting).
- **Judge (optional, only if a vision-capable model is configured, `image_judge_enabled`):** relevance to caption, no visible gibberish text, no unsafe content, brand-fit. If disabled, the judge evaluates prompt vs caption only.

### 4.3 `hashtag`
- **Input:** final caption, plan, strategy (`hashtag_count`), platform rules, language.
- **Output:** `HashtagOutput(hashtags: list[str])`.
- **Rules:** each starts with `#`, no spaces or punctuation inside, unique (case-insensitive), within platform min/max, mix of broad (1–2), niche (rest), at least one topic-specific; no banned/misleading/trending-hijack tags; no brand names that are not in the brief.
- **Rule checks:** all the above are mechanical; length per tag ≤ 40 chars.
- **Judge:** none needed (rule-only) unless `hashtag_judge_enabled`.

## 5. Final review

### 5.1 `reviewer`
- **Input:** full drafts for all platforms, plan, strategy, research, platform rules, brand voice, moderation rules.
- **Output:**
```python
class ReviewIssue(BaseModel):
    severity: Literal["error", "warning"]
    platform: str | None
    responsible_node: str          # "planner" | "strategist" | "writer" | "image" | "hashtag" | "script" | "storyboard" | "scene:<n>" | "assembly" | "voice"
    message: str
    fix_hint: str

class ReviewResult(BaseModel):
    ok: bool                       # False if any error issue
    scores: dict[str, float]       # quality, accuracy, policy, platform_fit, brand_voice (0..1)
    issues: list[ReviewIssue]
    warnings: list[str]            # shown to human on preview
```
- **Checks performed:** quality (clarity, hook, polish), accuracy (claims vs research), policy (safety rules 1.7, moderation), platform fit (limits, aspect ratio, hashtag range), brand voice, consistency between caption/image/hashtags (post) or script/scenes/voice (video).
- **Routing (`route_after_review`):**
  - `ok=true` → `human_approval` (warnings are attached to the content item).
  - Errors present and `review_cycles < max_review_cycles` (default 2): set `rerun_from` to the **earliest** responsible node among errors; increment `review_cycles`; store the issues' `fix_hint`s into `feedback[node]`; re-enter that node and rerun everything downstream of it (table 5.2).
  - Cycle cap reached: continue to `human_approval` with the remaining issues as warnings (the human decides).
- A `policy` error that cannot be fixed (e.g. unsafe topic) escalates to `needs_attention`.

### 5.2 Re-entry dependency table
| `rerun_from` | Nodes rerun |
|---|---|
| `planner` | planner, researcher (if plan changed topic), strategist, then the content flow |
| `strategist` | strategist, then the content flow |
| `writer` | writer, hashtag (image only if the reviewer also flagged it) |
| `image` | image only |
| `hashtag` | hashtag only |
| `script` | script, storyboard, scenes, assembly, voice |
| `storyboard` | storyboard, scenes, assembly, voice |
| `scene:<n>` | that scene, assembly, voice (subtitle timing recomputed) |
| `assembly` | assembly, voice |
| `voice` | voice only |

Retry counters for rerun nodes are reset when the reviewer sends them back (the reviewer cycle has its own cap).

## 6. Video flow

Order: `script → storyboard → scene (per scene) → assembly → voice`.
Default limits (settings): `max_scenes=5`, scene retries 2, other retries 3.

### 6.1 `script`
- **Input:** plan, research, strategy.video, brand_voice, language, `feedback["script"]`.
- **Output:**
```python
class ScriptScene(BaseModel):
    n: int                         # 1-based
    narration: str                 # spoken text
    on_screen_text: str | None     # short overlay, <= 8 words
    duration_s: float

class VideoScript(BaseModel):
    title: str
    hook: str                      # first 3 seconds line
    scenes: list[ScriptScene]
    cta: str | None
    total_duration_s: float
    language: str
    refused: bool = False
    reason: str | None = None
```
- **Prompt rules:** hook in scene 1; one idea per scene; plain spoken language; match brand voice; grounded facts only.
- **Rule checks:** scene count = `strategy.video.scene_count` (±1 allowed); `total_duration_s` within ±15% of target and ≤ platform max; narration word count ≈ duration × speaking rate (default 2.5 words/sec, ±25%); no empty narration; no banned words.
- **Judge rubric:** hook strength, pacing, clarity, brand-voice fit, factual safety.

### 6.2 `storyboard`
- **Input:** script, strategy.video.style, brand_voice.
- **Output:**
```python
class StoryboardScene(BaseModel):
    n: int
    visual_description: str
    shot_type: str                 # wide, medium, close-up, overhead, ...
    camera_motion: str | None
    setting: str
    transition: str | None
    scene_prompt: str              # prompt sent to the SceneProvider
    negative_prompt: str | None

class Storyboard(BaseModel):
    style_guide: str               # global look: palette, lighting, character/subject consistency tokens
    scenes: list[StoryboardScene]
```
- **Rules:** every scene prompt repeats the `style_guide` essentials so scenes look consistent; no real people, no brand logos, no text in frame.
- **Rule checks:** same scene numbers as the script; non-empty prompts; `style_guide` present.
- **Judge rubric:** faithfulness to script, visual consistency, feasibility for the provider.

### 6.3 `scene` (loops per scene; retry key `scene:<n>`)
- **Process:** `SceneProvider.generate(scene_prompt, seconds=duration_s)` → bytes → storage → `media_assets` (`type=video`, `position=n`).
- **Rule checks:** valid playable video (ffprobe); duration within ±20% of `duration_s` (or trimmed/padded by assembly if provider length is fixed — record in meta); resolution ≥ target; not blank/black (mean luminance and frame-variance threshold); size under limit.
- **Judge (optional vision, `scene_judge_enabled`):** matches `visual_description`, no gibberish text, no unsafe content.
- Only the failed scene is regenerated.

### 6.4 `assembly` (no LLM)
- FFmpeg: normalize all scenes to target resolution/fps, scale+pad or crop to platform aspect ratio, concat with the storyboard transitions (simple cut or crossfade), output H.264/AAC mp4.
- **Rule checks (ffprobe):** playable; duration within ±10% of script total; aspect ratio allowed; resolution ≥ target; file size under platform limit (setting); video stream and (after voice) audio stream present.
- Failure → retry assembly (deterministic failures such as missing scene files escalate immediately).

### 6.5 `voice` (TTS + subtitles + mix)
- **Process:** `TTSProvider.speak(narration, voice)` per scene (voice chosen by language/brand setting) → measure real durations → build **SRT** from real timings → mix narration (optional background music off by default) → mux/burn into the assembled video; also store the `.srt` as a `subtitle` asset.
- **Subtitle rules:** max 42 characters per line, max 2 lines, each cue 1–7 s, no overlaps, reading speed ≤ 20 chars/s, language matches.
- **Rule checks:** audio duration vs video duration within ±5% (otherwise time-stretch or re-time scenes; if impossible → fail with feedback "narration too long for scene n"); no clipping; subtitle rules above; final file playable with audio stream.
- If narration is much longer than a scene's duration, feedback goes to `script` (shorten narration) via the reviewer path, not to `voice` retries.

## 7. Non-LLM nodes (for completeness)

| Node | Behaviour |
|---|---|
| `content_router` | Pure function on `content_type` and `rerun_from`; no LLM. |
| `human_approval` | Persist drafts to DB, notify, interrupt; see ARCHITECTURE 6.1. |
| `schedule_gate` | Interrupt until `publish_at`; see ARCHITECTURE 5. |
| `publisher` | Per-target publish via `Publisher` interface; no LLM. |
| `analytics_setup` | Creates `collect_metrics` jobs; no LLM. |

## 8. Per-agent test fixtures (use the fake LLM)

For each agent keep two scripted responses in `tests/fixtures/agents/<agent>/`:
- `good.json` — passes all rule checks.
- `bad_<reason>.json` — fails exactly one rule (e.g. `writer/bad_too_long.json`, `hashtag/bad_duplicates.json`, `script/bad_duration.json`).

Mandatory tests per checked agent: pass first try; `bad` then `good` (retry changes the output and the second prompt contains the feedback); `bad` ×(max+1) escalates; refusal path (`refused=true`) escalates; judge below threshold fails; rules short-circuit the judge (judge not called when rules fail).

Example good writer output (fixture sketch):
```json
{"caption": "Struggling to stay consistent online? Here are 3 habits that actually work...", "title": null, "hook": "Struggling to stay consistent online?", "cta": "Save this for later.", "language": "en", "refused": false}
```

## 9. Open items to decide while implementing (log answers in PROGRESS)

1. First real providers for text, image, scene video, TTS and search.
2. Whether a vision-capable judge is available (controls `image_judge_enabled`, `scene_judge_enabled`).
3. Voice selection per language (including Urdu/Roman Urdu) and whether background music is allowed.
4. Whether to ground every post in research or allow ungrounded general posts (`grounded=false` behaviour is defined above; confirm product preference).
5. Final platform limits after verifying official docs (update `platform_rules` setting).
