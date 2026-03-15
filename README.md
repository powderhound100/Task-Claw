# 🦀 Task-Claw

**A multi-provider autonomous coding agent** that monitors a task queue, plans with AI, implements with any coding CLI, runs a security review, and pushes to production.

## Supported CLI Providers

| Provider | Binary | Plan | Implement | Notes |
|----------|--------|------|-----------|-------|
| **GitHub Copilot CLI** | `gh copilot` | `-p "[[PLAN]] ..."` | `-p "..." --yolo` | Default provider |
| **Copilot (direct)** | `copilot` | `-p "..."` | `-p "..." --yolo` | Without `gh` wrapper |
| **Claude Code** | `claude` | `-p "..."` | `-p "..." --dangerouslySkipPermissions` | Anthropic |
| **Aider** | `aider` | `--message "..." --dry-run` | `--message "..." --yes` | Any LLM backend |
| **OpenAI Codex CLI** | `codex` | `-p "..."` | `-p "..." --full-auto` | OpenAI |
| **Google Gemini CLI** | `gemini` | `-p "..."` | `-p "..." --sandbox` | Google |
| **Amazon Q Developer** | `q chat` | `--prompt "..."` | `--prompt "..." --trust-all-tools` | AWS |

Add your own providers in `providers.json`.

## Workflow

```
Task Queue (tasks.json / ideas.json)
  ↓
📐 PLAN  — CLI provider or GPT-4o API generates implementation plan
  ↓
🚀 IMPLEMENT — CLI provider executes the plan (--yolo / auto mode)
  ↓
🔒 SECURITY REVIEW — Fresh CLI audits diff for secrets, IPs, bad deps
  ↓  Low/Medium: auto-fix → push
  ↓  High: revert → block → notify user
  ↓
🚀 GIT PUSH — Commit and push to production
```

## Quick Start

1. **Clone and configure:**
   ```bash
   git clone https://github.com/powderhound100/Task-Claw.git
   cd Task-Claw
   cp .env.example .env
   # Edit .env with your GITHUB_TOKEN and PROJECT_DIR
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

## Configuration

### `.env` — Main config

```env
GITHUB_TOKEN=ghp_...        # Required: GitHub token for GPT-4o API
PROJECT_DIR=C:\MyProject     # Project the agent manages
TASKS_FILE=path/to/tasks.json
IDEAS_FILE=path/to/ideas.json
CLI_PROVIDER=copilot         # Default CLI provider
```

### Per-phase provider override

```env
CLI_PLAN_PROVIDER=copilot       # Use Copilot for planning
CLI_IMPLEMENT_PROVIDER=claude   # Use Claude Code for implementation
CLI_SECURITY_PROVIDER=copilot   # Use Copilot for security review
```

### Per-task provider override

In `tasks.json`, set `cli_provider` on individual tasks:

```json
{
  "id": "task-123",
  "title": "Add dark mode",
  "cli_provider": "claude",
  "planning_backend": "cli"
}
```

### `providers.json` — Define CLI providers

Each provider defines a binary, subcommands, and argument templates for each phase.
Use `{prompt}` as a placeholder for the actual prompt text.

```json
{
  "providers": {
    "my-custom-cli": {
      "name": "My Custom CLI",
      "binary": "my-cli",
      "subcommand": [],
      "plan_args": ["--plan", "{prompt}"],
      "implement_args": ["--execute", "{prompt}"],
      "security_args": ["--review", "{prompt}"],
      "plan_timeout": 900,
      "implement_timeout": 600,
      "security_timeout": 300
    }
  }
}
```

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/trigger` | POST | Wake agent immediately |
| `/status` | GET | Agent status + provider info |
| `/implement/{id}` | POST | Manually trigger implementation for a planned task |
| `/research` | POST | Start research for an idea |
| `/research-status/{id}` | GET | Check research progress |

## Security Review

After every implementation, a **fresh CLI instance** audits the git diff for:
- Hardcoded secrets, API keys, tokens, passwords
- Exposed IP addresses or internal network details
- Insecure HTTP endpoints, CORS misconfigurations
- Dangerous shell commands or injection vectors
- Known vulnerable dependencies
- Overly permissive permissions

**Severity-based response:**
- **Low / Medium** → Auto-fix with CLI, then push
- **High** → Revert all changes, block push

Reviews are saved in `security-reviews/`.

## Architecture

```
Task-Claw/
├── task-claw.py          # Main agent
├── providers.json        # CLI provider definitions
├── .env                  # Config + secrets (not committed)
├── .env.example          # Template
├── Start-TaskClaw.bat    # Windows launcher
├── agent-state.json      # Runtime state (auto-generated)
├── agent.log             # Logs
├── security-reviews/     # Security audit reports
└── research-output/      # Research results
```
