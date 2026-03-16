<div align="center">

# Task-Claw

**Vibe code. Ship like a senior engineering team.**

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Single File](https://img.shields.io/badge/architecture-single%20file-orange.svg)](#architecture)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/powderhound100/Task-Claw/pulls)

Describe what you want. Task-Claw spins up an AI dev team — PM, parallel developers, QA, security reviewer — orchestrates them through a quality-gated pipeline, and ships when the code is actually ready.

**One Python file. One dependency. Zero lock-in.**

[Quick Start](#get-running-in-2-minutes) · [The Pipeline](#the-pipeline) · [Providers](#bring-any-ai--or-all-of-them) · [Web UI](#web-dashboard) · [API](#http-api)

</div>

---

## Built for vibe-coders who need production-grade guardrails

You move fast. You describe features in plain English and expect working code. That's the right instinct — but shipping without a plan, code review, tests, or a security pass is how fast turns into fragile.

Task-Claw gives you the workflow of a disciplined engineering org without the overhead of managing one:

- **A PM that actually manages** — not a rubber-stamper. It rewrites vague requests into specs, directs the team, catches scope drift, and sends work back for revision until it meets the bar.
- **A dev team that debates** — two agents implement independently and cross-review each other's code. The PM deep-merges the best of both.
- **A QA loop that doesn't give up** — if tests fail, the code stage re-runs automatically with targeted failure context.
- **A security reviewer that can block a push** — every change gets a structured audit. HIGH severity findings don't ship.
- **Scales to the task** — one agent for a quick fix, a full team with cross-review and revision loops for complex features.

---

## What Is This?

Task-Claw is a **fully autonomous multi-agent coding pipeline**. It uses AI coding tools you already have installed — Claude Code, GitHub Copilot, Aider, Codex, Gemini, Amazon Q — and orchestrates them through a PM-supervised pipeline that plans, implements, tests, reviews, and ships code without human intervention.

**AI coding assistants are co-pilots. This is the engineering org.**

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

Describe what you want. Task-Claw assembles the team and runs the process.

```bash
python task-claw.py "Add a /health endpoint that returns uptime and version"
```

```
Your Prompt
    ↓
[Rewrite]  → PM sharpens the request into a clear spec
[Plan]     → Team plans · PM quality-gates              ↺ REVISE if needed
[Code]     → Parallel implementation · cross-review      ↺ REVISE if needed
[Simplify] → Refactor for reuse, quality, efficiency
[Test]     → Automated testing · failure loopback        ↺ REVISE if needed
[Review]   → Security audit · HIGH severity blocks push
[Publish]  → git commit + push
```

The PM doesn't just observe — it catches requirement gaps, scope drift, and quality shortfalls, sending stages back for rework before anything moves forward.

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

Mix and match per pipeline stage — put your fastest model on planning, your strongest on implementation:

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

**Keep vibing. Task-Claw handles the engineering rigor.**

[Get Started](#get-running-in-2-minutes) · [Report a Bug](https://github.com/powderhound100/Task-Claw/issues) · [Contribute](https://github.com/powderhound100/Task-Claw/pulls)

</div>
