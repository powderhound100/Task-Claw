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

**Entry points:**
- `python task-claw.py "prompt"` — run pipeline once, exit
- `python task-claw.py` — polling mode (monitors tasks.json / ideas.json)
- `POST /trigger {"prompt": "..."}` — pipeline via HTTP
- `POST /trigger` (no body) — wake the polling loop

**Threading model:** HTTP trigger server runs in a daemon thread. Research jobs each spawn their own daemon thread. Team members within a pipeline stage run in `ThreadPoolExecutor`. The main loop uses `threading.Event` for interruptible sleep. Shared state is guarded by `status_lock` and `research_lock`.

**State files (auto-generated, not committed):**
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
| `TASKS_FILE` | `$PROJECT_DIR/nodered/data/tasks.json` | Task queue |
| `IDEAS_FILE` | `$PROJECT_DIR/nodered/data/ideas.json` | Idea queue |
| `GITHUB_TOKEN` | — | Required for GitHub Models PM backend + git push |
| `CLI_PROVIDER` | `claude` | Default CLI provider |
| `AGENT_POLL_INTERVAL` | `3600` | Seconds between poll cycles |
| `AGENT_MAX_CALLS` | `10` | Daily API call cap |
| `AGENT_TRIGGER_PORT` | `8099` | HTTP trigger server port |
| `PIPELINE_FILE` | `pipeline.json` | Pipeline + PM config |
| `PIPELINE_MANAGER_TIMEOUT` | `300` | PM API call timeout (seconds) |
| `PIPELINE_MAX_REVISE` | `1` | Max times PM can send a stage back for rework |

## HTTP API

The agent exposes a small HTTP server on `AGENT_TRIGGER_PORT`:

| Endpoint | Method | Description |
|---|---|---|
| `/trigger` | POST | `{"prompt":"..."}` → run pipeline; no body → wake polling loop |
| `/status` | GET | Agent state, provider list, pipeline stage config |
| `/implement/{id}` | POST | Run pipeline from code stage for a planned task/idea |
| `/research` | POST | Start background research for an idea |
| `/research-status/{id}` | GET | Poll research job status |

## Task / Idea JSON Schema

Items in `tasks.json` / `ideas.json` use these fields relevant to the agent:
- `id` — unique identifier
- `status` — `open` → `grabbed` → `in-progress` → `done` / `security-blocked` / `pushed-to-production`
- `title`, `description` — used to build prompts
- `cli_provider` — optional per-task provider override
- `plan` — populated after the plan stage
