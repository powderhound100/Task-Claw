# Provider Setup

## Description
Guides installation and configuration of CLI providers for the Task-Claw pipeline. Covers prerequisites, installation, API keys, verification, and Task-Claw integration for each supported provider.

## Triggers
- "setup provider"
- "install copilot"
- "install claude"
- "configure aider"
- "add provider"
- "setup claude"
- "setup codex"
- "setup gemini"
- "setup amazon q"
- "provider installation"

## Instructions

When the user asks to set up a provider, identify which provider they want and follow the corresponding section below. If they say "all" or don't specify, present the list and ask which ones to install.

First, read `D:/Task-Claw/providers.json` to show which providers are already configured in Task-Claw.

---

### GitHub Copilot CLI

**Prerequisites:**
- GitHub CLI (`gh`) installed: https://cli.github.com/
- GitHub account with active Copilot subscription (Individual, Business, or Enterprise)

**Installation:**
```bash
gh extension install github/gh-copilot
```

**Authentication:**
```bash
gh auth login
gh auth status  # verify
```

Copilot uses your GitHub authentication — no separate API key needed.

**Verification:**
```bash
gh copilot suggest -t shell "list files in current directory"
```

**Task-Claw Integration:**
- Already configured in `providers.json` as `copilot`
- Set as default: add `CLI_PROVIDER=copilot` to `D:/Task-Claw/.env`
- Or per-task: set `"cli_provider": "copilot"` in the task JSON
- Cost: included in GitHub Copilot subscription

**Troubleshooting:**
- "gh copilot: command not found" — run `gh extension list` to verify installation
- Authentication errors — run `gh auth refresh`
- Rate limits — Copilot CLI has generous limits but may throttle under heavy use

---

### Claude Code

**Prerequisites:**
- Node.js 18+ installed
- Anthropic API key from https://console.anthropic.com/

**Installation:**
```bash
npm install -g @anthropic-ai/claude-code
```

**API Key Configuration:**
```bash
# Set the API key (the CLI will prompt on first run if not set)
export ANTHROPIC_API_KEY="sk-ant-..."
```

On Windows, set it permanently:
```bash
setx ANTHROPIC_API_KEY "sk-ant-..."
```

**Verification:**
```bash
claude --version
claude -p "Say hello" --output-format text
```

**Task-Claw Integration:**
- Already configured in `providers.json` as `claude` (default provider)
- Set as default: add `CLI_PROVIDER=claude` to `D:/Task-Claw/.env` (or leave unset — it is the default)
- The pipeline uses `--dangerously-skip-permissions` flag for non-interactive operation
- Cost: ~$0.05-$0.30 per pipeline call depending on stage complexity

**Troubleshooting:**
- "claude: command not found" — ensure npm global bin is in PATH (`npm bin -g`)
- API key errors — verify key at https://console.anthropic.com/
- Nested session issues — Task-Claw automatically removes the `CLAUDECODE` env var for subprocess calls
- Permission prompts in pipeline — ensure prompts are kept short and simple (see CLAUDE.md memory notes)

---

### Aider

**Prerequisites:**
- Python 3.9+ installed
- An LLM API key (OpenAI, Anthropic, or other supported backend)

**Installation:**
```bash
pip install aider-chat
```

**API Key Configuration (choose one backend):**

For OpenAI:
```bash
export OPENAI_API_KEY="sk-..."
```

For Anthropic:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

For other backends, see: https://aider.chat/docs/llms.html

**Verification:**
```bash
aider --version
aider --message "Say hello" --yes-always --no-git
```

**Task-Claw Integration:**
- Already configured in `providers.json` as `aider`
- Set as default: add `CLI_PROVIDER=aider` to `D:/Task-Claw/.env`
- Aider uses `--message` for non-interactive operation and `--yes-always` to skip confirmations
- Cost: depends on underlying LLM — ~$0.05-$0.20 per call with GPT-4o, less with smaller models

**Troubleshooting:**
- "aider: command not found" — ensure Python scripts directory is in PATH
- Model errors — check that the correct API key env var is set for your chosen backend
- Git issues — aider tries to commit by default; Task-Claw passes `--no-git` in some configurations

---

### OpenAI Codex CLI

**Prerequisites:**
- Node.js 18+ installed
- OpenAI API key from https://platform.openai.com/api-keys

**Installation:**
```bash
npm install -g @openai/codex
```

**API Key Configuration:**
```bash
export OPENAI_API_KEY="sk-..."
```

On Windows:
```bash
setx OPENAI_API_KEY "sk-..."
```

**Verification:**
```bash
codex --version
codex -q "Say hello"
```

**Task-Claw Integration:**
- Add to `providers.json` if not already present:
```json
"codex": {
    "binary": "codex",
    "plan_args": ["-q", "--full-auto", "{prompt} — Output an implementation plan only, do not edit any files."],
    "implement_args": ["-q", "--full-auto", "{prompt}"],
    "security_args": ["-q", "--full-auto", "{prompt}"],
    "test_args": ["-q", "--full-auto", "{prompt}"],
    "review_args": ["-q", "--full-auto", "{prompt}"]
}
```
- Set as default: add `CLI_PROVIDER=codex` to `D:/Task-Claw/.env`
- Cost: depends on OpenAI model pricing

**Troubleshooting:**
- "codex: command not found" — ensure npm global bin is in PATH
- API key errors — verify at https://platform.openai.com/

---

### Google Gemini CLI

**Prerequisites:**
- Node.js 18+ installed
- Google AI API key from https://aistudio.google.com/apikey

**Installation:**
```bash
npm install -g @anthropic-ai/claude-code  # placeholder — update when Gemini CLI is released
# Or use the Google AI SDK approach:
npm install -g @google/generative-ai
```

Note: Google Gemini CLI tooling is evolving. Check https://ai.google.dev/ for the latest CLI options.

**API Key Configuration:**
```bash
export GOOGLE_API_KEY="AI..."
```

**Task-Claw Integration:**
- Add to `providers.json` with appropriate binary and args once the CLI is installed
- Cost: varies by model (Gemini Pro, Gemini Ultra)

**Troubleshooting:**
- Check Google AI Studio for API key status and quota

---

### Amazon Q Developer

**Prerequisites:**
- AWS CLI v2 installed: https://aws.amazon.com/cli/
- AWS account with appropriate IAM permissions
- AWS Builder ID or IAM Identity Center credentials

**Installation:**

On Windows:
```bash
# Install via MSI from https://aws.amazon.com/q/developer/
# Or via winget:
winget install Amazon.QDeveloper
```

On macOS:
```bash
brew install amazon-q
```

On Linux:
```bash
# Download from https://aws.amazon.com/q/developer/
```

**Authentication:**
```bash
q login
# Follow the browser-based authentication flow
```

Or configure with AWS credentials:
```bash
aws configure
# Set region, access key, secret key
```

**Verification:**
```bash
q chat "Say hello"
```

**Task-Claw Integration:**
- Add to `providers.json`:
```json
"amazon-q": {
    "binary": "q",
    "plan_args": ["chat", "{prompt} — Output an implementation plan only, do not edit any files."],
    "implement_args": ["chat", "{prompt}"],
    "security_args": ["chat", "{prompt}"],
    "test_args": ["chat", "{prompt}"],
    "review_args": ["chat", "{prompt}"]
}
```
- Set as default: add `CLI_PROVIDER=amazon-q` to `D:/Task-Claw/.env`
- Cost: included with AWS account (subject to service limits)

**Troubleshooting:**
- "q: command not found" — ensure Amazon Q is installed and in PATH
- Authentication errors — run `q login` again or check AWS credentials
- IAM permissions — ensure your role has `q:SendMessage` permission

---

### General Integration Steps

After installing any provider:

1. **Verify the binary is in PATH:**
   ```bash
   which <binary-name>
   ```

2. **Check providers.json** — read `D:/Task-Claw/providers.json` to see if the provider is already configured. If not, add an entry following the existing format.

3. **Set as default (optional):** Add `CLI_PROVIDER=<name>` to `D:/Task-Claw/.env`

4. **Test with Task-Claw:** Run a quick pipeline:
   ```bash
   curl -X POST http://localhost:8099/trigger \
     -H "Content-Type: application/json" \
     -d '{"prompt": "Add a comment to the top of task-claw.py saying hello"}'
   ```

5. **Per-task override:** Set `"cli_provider": "<name>"` on any task in `data/tasks.json` to use a specific provider for just that task.
