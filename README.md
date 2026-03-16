<div align="center">

# Task-Claw

**Your backlog, on autopilot.**

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Single File](https://img.shields.io/badge/architecture-single%20file-orange.svg)](#architecture)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/powderhound100/Task-Claw/pulls)

Drop a task. Walk away. Task-Claw plans it, codes it, tests it, security-audits it, and pushes it to production.

**One Python file. One dependency. Zero lock-in.**

[Quick Start](#get-running-in-2-minutes) · [How It Works](#the-pipeline) · [Providers](#bring-any-ai--or-all-of-them) · [Web UI](#web-dashboard) · [API](#http-api)

</div>

---

## What Is This?

Task-Claw is a **fully autonomous multi-agent coding pipeline**. It uses AI coding tools you already have installed — Claude Code, GitHub Copilot, Aider, Codex, Gemini, Amazon Q — and orchestrates them through a PM-supervised pipeline that plans, implements, tests, reviews, and ships code without human intervention.

**AI coding assistants are co-pilots. This is the pilot.**

### Highlights

- **PM-supervised pipeline** — An AI Program Manager rewrites vague requests into specs, directs agents, and quality-gates every stage. Work gets sent back for revision until it's actually good.
- **Multi-agent code generation** — Two agents implement independently, cross-review each other, and the PM deep-merges the best of both.
- **Self-healing tests** — If tests fail, the code stage re-runs automatically with targeted failure context.
- **Security-first shipping** — Every change gets a structured security audit. HIGH severity findings block the push.
- **Provider-agnostic** — Use one AI tool or mix them per stage. Add new providers in `providers.json` without touching code.
- **Web dashboard included** — Task management, live pipeline monitoring, config editing. No Node, no build step.

---

## Get Running in 2 Minutes

```bash
git clone https://github.com/powderhound100/Task-Claw.git
cd Task-Claw
cp .env.example .env          # Set at least one API key
pip install requests           # The only dependency
python task-claw.py            # Done
```

Open **http://localhost:8099** — you're looking at the dashboard.

---

## The Pipeline

Describe what you want. Task-Claw handles the rest.

```bash
python task-claw.py "Add a /health endpoint that returns uptime and version"
```

```
Your Prompt
    ↓
[Rewrite]  → PM sharpens the request
[Plan]     → Team plans · PM quality-gates              ↺ REVISE
[Code]     → Parallel implementation · cross-review      ↺ REVISE
[Simplify] → Refactor for reuse, quality, efficiency
[Test]     → Automated testing · failure loopback        ↺ REVISE
[Review]   → Security audit (blocks HIGH severity)
[Publish]  → git commit + push
```

The PM doesn't rubber-stamp. It catches requirement gaps, scope drift, and quality issues — sending stages back for rework when needed.

> **Three ways to trigger:**
> CLI one-shot · Web UI button · `POST /trigger {"prompt":"..."}`

---

## Bring Any AI — Or All of Them

| Provider | Command | |
|----------|---------|---|
| **Claude Code** | `claude` | Default |
| **GitHub Copilot** | `gh copilot` | Via `gh` extension |
| **Aider** | `aider` | Any LLM backend |
| **OpenAI Codex CLI** | `codex` | |
| **Google Gemini CLI** | `gemini` | |
| **Amazon Q Developer** | `q chat` | |

Mix and match per pipeline stage:

```env
CLI_PLAN_PROVIDER=copilot
CLI_IMPLEMENT_PROVIDER=claude
CLI_TEST_PROVIDER=gemini
```

Or override per-task from the web UI. Add custom providers in `providers.json` — zero code changes.

---

## Web Dashboard

Ships with a self-contained web UI. No npm. No bundler. Just open the browser.

**Tasks page** (`/`) — Create, edit, and prioritize tasks and ideas. Photo uploads, status filters, one-click agent trigger.

**Pipeline page** (`/pipeline.html`) — Live stage execution, run history, per-stage stats, skills management, config editing — all in-browser.

<!-- Screenshots welcome! Add web UI screenshots here to dramatically improve conversion. -->

---

## Custom Skills

Define reusable prompt templates and run them through any provider:

```json
{
  "name": "explain-function",
  "prompt": "Explain what this function does and suggest improvements:\n{input}",
  "provider": "claude",
  "phase": "implement"
}
```

Create in the web UI or via API. Every run saves full output history. Built-in skills include cost estimation, multi-provider comparison, and parallel pipeline orchestration.

---

## HTTP API

Every UI feature is backed by a REST endpoint. Integrate Task-Claw into your existing workflow.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/trigger` | POST | Run pipeline from a prompt |
| `/status` | GET | Agent state + provider list |
| `/implement/{id}` | POST | Code stage for a planned task |
| `/research` | POST | Background research on an idea |
| `/api/tasks` | GET/POST | Task CRUD |
| `/api/ideas` | GET/POST | Idea CRUD |
| `/api/skills` | GET/POST | Skill CRUD + execution |
| `/api/pipeline-history` | GET | Completed runs |
| `/api/config/*` | GET/PUT | Live config editing |

<details>
<summary><strong>See full API reference</strong></summary>

See [`CLAUDE.md`](CLAUDE.md) for complete endpoint documentation including research status polling, pipeline output viewing, security reports, photo management, and pipeline stats.

</details>

---

## Architecture

One file. Seriously.

```
Task-Claw/
├── task-claw.py          # The entire agent (~1100 lines)
├── web/                  # Self-contained web UI
├── providers.json        # CLI provider definitions
├── pipeline.json         # Pipeline + PM config
├── prompts.json          # All prompt templates (editable)
├── skills.json           # User-defined skills
├── .env                  # Your config (not committed)
└── test_pipeline.py      # Unit + E2E tests
```

No modules. No packages. No transpilation. Fork it, hack it, ship it.

---

## Configuration

All config in `.env` ([see `.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `PROJECT_DIR` | agent dir | Project the agent edits |
| `CLI_PROVIDER` | `claude` | Default coding tool |
| `AGENT_MAX_CALLS` | `10` | Daily PM API call cap |
| `AGENT_TRIGGER_PORT` | `8099` | Web UI + API port |
| `PIPELINE_MAX_REVISE` | `1` | Max revision loops per stage |

Pipeline stages, PM backend, team size, and publish behavior are configurable in `pipeline.json` — editable live from the web dashboard.

---

## Security

- API key authentication for mutating endpoints
- CORS origin restriction
- Path traversal protection on all file routes
- 2 MB request body limits
- Security review stage blocks HIGH severity findings from production

## Testing

```bash
python -m unittest test_pipeline -v   # Unit tests
python -m unittest test_e2e -v        # E2E with mocks
```

---

<div align="center">

**Built for developers who'd rather ship than babysit.**

[Get Started](#get-running-in-2-minutes) · [Report a Bug](https://github.com/powderhound100/Task-Claw/issues) · [Contribute](https://github.com/powderhound100/Task-Claw/pulls)

</div>
