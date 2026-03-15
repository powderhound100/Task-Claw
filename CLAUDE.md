# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Agent

```bash
python task-claw.py
# Windows: Start-TaskClaw.bat
```

Only one runtime dependency: `pip install requests`

## Architecture

The entire agent lives in a single file: `task-claw.py`. There are no modules, packages, or build steps.

**Multi-agent pipeline (`run_pipeline()`):**
```
User Prompt
    ↓
[Rewrite] — PM rewrites prompt for clarity
    ↓
[Plan]    — PM directs → team plans → PM oversees (quality gate)
    ↓                                  ↺ REVISE if needed
[Code]    — PM directs → team implements in parallel
    ↓       → cross-review (each agent reviews the other's code)
    ↓       → PM deep-merge (compares, finds gaps, merges best parts)
    ↓       → quality gate (APPROVE / REVISE)
    ↓                                  ↺ REVISE if needed
[Simplify]— Review changed code for reuse, quality, efficiency; fix issues
    ↓
[Test]    — PM directs → team tests → PM oversees (quality gate)
    ↓                                  ↺ REVISE if needed
[Review]  — Structured security audit → PM oversees → verdict
    ↓
[Publish] — git commit + push (blocked if HIGH severity)
```

The **Program Manager** oversees the entire pipeline as a quality gatekeeper:
- `pm_direct_team()` — writes task briefs before each stage
- `pm_oversee_stage()` — verifies output quality, checks for requirement gaps/drift, returns APPROVE or REVISE
- `cross_review_code()` — when 2+ agents produce code, each reviews the other's implementation
- `pm_merge_with_reviews()` — deep merge using both implementations + cross-reviews

If the PM returns REVISE, the stage is re-run with feedback (up to `PIPELINE_MAX_REVISE` attempts, default 1). CLI team members are stateless tools. Pipeline is configured in `pipeline.json`.

**Safety gates:**
- Publish is gated on `test_passed` — if tests fail, `_git_commit_and_push` is skipped
- `_test_found_failures()` uses word-boundary matching for short keywords to reduce false positives
- PM direct-mode failover requires 2 consecutive failures (not 1) before switching
- `_parse_overseer_response()` defaults to REVISE (not approve) when PM response is malformed
- Garbage detection uses tiered scoring: strong signals (2pts) vs weak signals (1pt, only if short/questioning)

**Prompt system (`prompts.json`):**
- All PM and CLI prompt templates are externalized in `prompts.json` — editable without code changes
- PM prompts (API calls) can be detailed; CLI prompts MUST stay short (<500 chars of instructions)
- `_warn_cli_prompt_size()` logs a warning if CLI instruction chars exceed the safe threshold
- `_load_prompts()` caches the file; inline fallbacks keep the agent running if the file is missing
- Prompt patterns drawn from Kiro, Cursor, Codex CLI, Devin, Claude Code, and Antigravity

**Context quality:**
- `_clean_stage_output()` strips garbage lines before appending to pipeline context
- `_cap_context()` splits on `=== ... ===` delimiters and preserves plan sections
- Garbage retry preserves plan context via `_extract_plan_context()` instead of passing empty context
- Code-fix prompt is structured with `_extract_test_failures()` for targeted failure context

**Agent comparison (multi-agent code stage):**
- Cross-review uses structured format (Agreement Points, Divergences, Winner Per Component, Merge Strategy, Issues Found)
- `_build_comparison_summary()` extracts structured sections for pipeline output
- Each agent's output saved separately as `code-{agent-name}.md`

**Entry points:**
- `python task-claw.py "prompt"` — run pipeline once, exit
- `python task-claw.py` — polling mode (monitors tasks.json / ideas.json)
- `POST /trigger {"prompt": "..."}` — pipeline via HTTP
- `POST /trigger` (no body) — wake the polling loop

**Threading model:** HTTP trigger server runs in a daemon thread. Research jobs each spawn their own daemon thread. Team members within a pipeline stage run in `ThreadPoolExecutor`. The main loop uses `threading.Event` for interruptible sleep. Shared state is guarded by `status_lock` and `research_lock`.

**Web UI:** Self-contained web interface served from `web/` directory at `http://localhost:8099/`. Tasks page (`/`) for task/idea CRUD with photos, filters, agent status. Pipeline page (`/pipeline.html`) for live monitoring, history, config editing.

**Data storage:** `data/` directory (auto-created, gitignored) contains `tasks.json`, `ideas.json`, and `photos/`. No external dependencies (Node-RED removed).

**State files (auto-generated, not committed):**
- `data/` — tasks.json, ideas.json, photos/ (self-contained storage)
- `agent-state.json` — runtime state (daily API call count, task status cache)
- `agent.log` — rolling log
- `security-reviews/` — per-task security audit reports
- `research-output/` — research results for ideas

## Provider System

Providers are defined in `providers.json`. Each entry specifies a binary, optional subcommand, and arg templates for five phases (`plan_args`, `implement_args`, `security_args`, `test_args`, `review_args`). The `{prompt}` placeholder is replaced at runtime. Default provider is `claude`.

**Resolution priority for provider selection** (`get_provider_for_phase()`):
1. Per-task `cli_provider` field in the task JSON
2. Phase-specific env var (`CLI_PLAN_PROVIDER`, `CLI_IMPLEMENT_PROVIDER`, `CLI_SECURITY_PROVIDER`, `CLI_TEST_PROVIDER`, `CLI_REVIEW_PROVIDER`)
3. Global `CLI_PROVIDER` env var
4. `default_provider` in `providers.json`

To add a custom provider, add an entry to `providers.json` — no code changes needed.

## Configuration

All config is read from `.env` at startup via `os.environ.setdefault` (existing env vars are not overridden). See `.env.example` for all options.

Key env vars:
| Variable | Default | Purpose |
|---|---|---|
| `PROJECT_DIR` | agent dir | Target project the agent edits |
| `TASKS_FILE` | `data/tasks.json` | Task queue |
| `IDEAS_FILE` | `data/ideas.json` | Idea queue |
| `GITHUB_TOKEN` | — | For `github_models` PM backend + git push |
| `ANTHROPIC_API_KEY` | — | For `anthropic` PM backend |
| `CLI_PROVIDER` | `claude` | Default CLI provider |
| `AGENT_POLL_INTERVAL` | `3600` | Seconds between poll cycles |
| `AGENT_MAX_CALLS` | `10` | Daily API call cap |
| `AGENT_TRIGGER_PORT` | `8099` | HTTP trigger server port |
| `PIPELINE_FILE` | `pipeline.json` | Pipeline + PM config |
| `PIPELINE_MANAGER_TIMEOUT` | `300` | PM API call timeout (seconds) |
| `PIPELINE_MAX_REVISE` | `1` | Max times PM can send a stage back for rework |
| `RESTART_SERVICE_MAP` | — | Post-deploy docker restarts, e.g. `web/:web,api/:api` |

## HTTP API

The agent exposes an HTTP server on `AGENT_TRIGGER_PORT` (default 8099):

**Web UI:**
| Path | Description |
|---|---|
| `/` | Tasks + Ideas page |
| `/pipeline.html` | Pipeline monitor + config |
| `/css/*`, `/js/*` | Static assets from `web/` |
| `/photos/*` | Uploaded photos from `data/photos/` |

**Pipeline/Agent:**
| Endpoint | Method | Description |
|---|---|---|
| `/trigger` | POST | `{"prompt":"..."}` → run pipeline; no body → wake polling loop |
| `/status` | GET | Agent state, version, provider list, pipeline stage config |
| `/implement/{id}` | POST | Run pipeline from code stage for a planned task/idea |
| `/research` | POST | Start background research for an idea |
| `/research-status/{id}` | GET | Poll research job status |
| `/pipeline-output/{id}` | GET | List/view stage outputs for a pipeline run |
| `/security-report/{id}` | GET | View security review report |

**CRUD API:**
| Endpoint | Method | Description |
|---|---|---|
| `/api/tasks` | GET/POST | List all tasks / create new task |
| `/api/tasks/{id}` | PUT/DELETE | Update / delete task |
| `/api/ideas` | GET/POST | List all ideas / create new idea |
| `/api/ideas/{id}` | PUT/DELETE | Update / delete idea |
| `/api/photos/upload` | POST | Multipart photo upload |
| `/api/photos/{file}` | DELETE | Delete a photo |
| `/api/pipeline-history` | GET | List completed pipeline runs |
| `/api/pipeline-stats` | GET | Per-stage stats (CLI calls, subagents, tools) |
| `/api/config/pipeline` | GET/PUT | Read/write pipeline.json |
| `/api/config/providers` | GET/PUT | Read/write providers.json |
| `/api/skills` | GET/POST | List all skills (user + env) / create new skill |
| `/api/skills/{id}` | PUT/DELETE | Update / delete a user-defined skill |
| `/api/skills/{id}/run` | POST | Execute a skill with `{"input":"..."}` |
| `/api/skills/{id}/runs` | GET | List completed runs for a skill |
| `/skill-output/{run_id}` | GET | View full output of a skill run |

## Task / Idea JSON Schema

Items in `tasks.json` / `ideas.json` use these fields relevant to the agent:
- `id` — unique identifier
- `status` — `open` → `grabbed` → `in-progress` → `done` / `security-blocked` / `pushed-to-production`
- `title`, `description` — used to build prompts
- `cli_provider` — optional per-task provider override
- `plan` — populated after the plan stage

## Testing

```bash
python -m unittest test_pipeline -v       # run all tests
python -m unittest test_pipeline.TestIsGarbageOutput -v  # single class
```

`test_pipeline.py` uses `importlib` to import `task-claw.py` and `unittest.mock.patch.object` for mocking. Tests cover pure functions (garbage detection, failure detection, context capping, prompt building, provider commands, JSON parsing) and pipeline flow scenarios (happy path, PM failover, garbage retry, test-code loopback, publish gating, security blocking).

## Custom Skills System

Skills are named prompt templates executed through CLI providers. Two sources:

1. **User-defined** (`skills.json`) — create/edit/delete via web UI or API
2. **Environment-discovered** (`.claude/skills/*/SKILL.md`) — auto-detected, read-only in UI

**Skill JSON schema** (in `skills.json`):
- `name` — display name
- `description` — what the skill does
- `prompt` — template with optional `{input}` placeholder
- `provider` — CLI provider override (null = use default)
- `phase` — which provider phase args to use (`implement`, `plan`, `test`, `review`)
- `tags` — categorization labels
- `timeout` — execution timeout in seconds

Skills are invoked via `POST /api/skills/{id}/run` with `{"input": "..."}`. Output is saved to `skill-output/{run_id}/output.md`. The web UI on `/pipeline.html` has a Skills section for management and execution.

## Claude Code Skills

Skills in `.claude/skills/`:
- **cost-estimator** — Estimate pipeline run costs based on current config
- **skillswarm** — Orchestrate parallel pipeline runs for complex tasks
- **agent-compare** — Compare CLI providers by running the same prompt through each
- **provider-setup** — Guided installation and configuration of CLI providers
