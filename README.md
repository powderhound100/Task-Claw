# Task-Claw

**A standalone, multi-provider autonomous coding agent** with a built-in web UI. It monitors a task queue, orchestrates a multi-stage pipeline (plan, code, test, review), runs security audits, and pushes to production — all from a single Python file.

## Quick Start

1. **Clone and configure:**
   ```bash
   git clone https://github.com/powderhound100/Task-Claw.git
   cd Task-Claw
   cp .env.example .env
   # Edit .env — set at least one API key (GITHUB_TOKEN or ANTHROPIC_API_KEY)
   ```

2. **Install dependency:**
   ```bash
   pip install requests
   ```

3. **Run:**
   ```bash
   python task-claw.py
   # Or on Windows: Start-TaskClaw.bat
   ```

4. **Open the web UI:** `http://localhost:8099/`

## Web UI

Task-Claw includes a self-contained web interface (no external dependencies):

- **Tasks page** (`/`) — Create, edit, delete tasks and ideas. Photo uploads, status filters, agent trigger button.
- **Pipeline page** (`/pipeline.html`) — Live pipeline monitor, stage stats, run history, skills management, config editor.

## Pipeline

The multi-agent pipeline orchestrated by a Program Manager (PM):

```
User Prompt
    |
[Rewrite] -- PM rewrites prompt for clarity
    |
[Plan]    -- PM directs team to plan, oversees quality
    |                                  <- REVISE if needed
[Code]    -- PM directs team to implement in parallel
    |       -> cross-review -> PM deep-merge -> quality gate
    |                                  <- REVISE if needed
[Simplify]-- Review changed code for reuse, quality, efficiency
    |
[Test]    -- PM directs team to test, oversees results
    |                                  <- REVISE if needed
[Review]  -- Structured security audit -> PM verdict
    |
[Publish] -- git commit + push (blocked if HIGH severity)
```

Run a one-shot pipeline from the CLI:
```bash
python task-claw.py "Add a /health endpoint to the API"
```

Or trigger via HTTP:
```bash
curl -X POST http://localhost:8099/trigger -d '{"prompt":"Add a /health endpoint"}'
```

## Supported CLI Providers

| Provider | Binary | Notes |
|----------|--------|-------|
| **Claude Code** | `claude` | Anthropic (default) |
| **GitHub Copilot CLI** | `gh copilot` | Requires `gh` CLI with copilot extension |
| **Copilot (direct)** | `copilot` | Without `gh` wrapper |
| **Aider** | `aider` | Any LLM backend |
| **OpenAI Codex CLI** | `codex` | OpenAI |
| **Google Gemini CLI** | `gemini` | Google |
| **Amazon Q Developer** | `q chat` | AWS |

Add your own providers in `providers.json` — no code changes needed.

## Configuration

### `.env` — Main config

```env
# At least one API key required for PM backend:
GITHUB_TOKEN=ghp_...            # For 'github_models' PM backend + git push
# ANTHROPIC_API_KEY=sk-ant-...  # For 'anthropic' PM backend

# Project the agent manages (defaults to Task-Claw dir itself)
# PROJECT_DIR=C:\MyProject

CLI_PROVIDER=claude             # Default CLI provider
AGENT_MAX_CALLS=10              # Daily PM API call cap
AGENT_TRIGGER_PORT=8099         # Web UI + API port
```

See `.env.example` for all options.

### Per-phase provider override

```env
CLI_PLAN_PROVIDER=copilot       # Use Copilot for planning
CLI_IMPLEMENT_PROVIDER=claude   # Use Claude Code for implementation
```

### Per-task provider override

In the web UI or tasks.json, set `cli_provider` on individual tasks.

### `pipeline.json` — Pipeline + PM config

Configure PM backend (`github_models`, `anthropic`, or `openai_compatible`), enable/disable stages, set team members per stage, and control publish behavior.

### `providers.json` — CLI provider definitions

Each provider defines a binary, subcommands, and argument templates for each phase. Use `{prompt}` as a placeholder.

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/trigger` | POST | `{"prompt":"..."}` runs pipeline; no body wakes polling loop |
| `/status` | GET | Agent state, version, provider list |
| `/implement/{id}` | POST | Run pipeline from code stage for a planned task |
| `/research` | POST | Start background research for an idea |
| `/api/tasks` | GET/POST | List / create tasks |
| `/api/ideas` | GET/POST | List / create ideas |
| `/api/skills` | GET/POST | List / create skills |
| `/api/pipeline-history` | GET | Completed pipeline runs |
| `/api/config/pipeline` | GET/PUT | Read/write pipeline.json |
| `/api/config/providers` | GET/PUT | Read/write providers.json |

See `CLAUDE.md` for the full API reference.

## Architecture

```
Task-Claw/
├── task-claw.py          # Entire agent (single file)
├── web/                  # Self-contained web UI
│   ├── index.html        # Tasks + Ideas page
│   ├── pipeline.html     # Pipeline monitor + config
│   ├── css/              # Stylesheets
│   └── js/               # Client-side JS
├── providers.json        # CLI provider definitions
├── pipeline.json         # Pipeline stage + PM config
├── prompts.json          # Externalized prompt templates
├── skills.json           # User-defined skills
├── .env                  # Config + secrets (not committed)
├── .env.example          # Template
├── Start-TaskClaw.bat    # Windows launcher
├── data/                 # Auto-created, gitignored
│   ├── tasks.json        # Task queue
│   ├── ideas.json        # Idea queue
│   └── photos/           # Uploaded images
├── agent-state.json      # Runtime state (auto-generated)
├── agent.log             # Rolling log
├── security-reviews/     # Per-task security audit reports
├── pipeline-output/      # Stage outputs per run
├── skill-output/         # Skill execution results
└── test_pipeline.py      # Unit tests
```

## Testing

```bash
python -m unittest test_pipeline -v       # unit tests
python -m unittest test_e2e -v            # E2E tests with mocks
```

## Security

- Optional `API_KEY` env var gates all mutating endpoints
- `CORS_ORIGIN` restricts cross-origin requests
- Path traversal protection on all file-serving endpoints
- Request body size limits (2 MB)
- Security review stage blocks HIGH severity findings from being pushed
