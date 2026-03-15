"""
Task-Claw — Multi-Provider Coding Agent
Monitors tasks.json / ideas.json for work items, plans with GPT-4o or any
coding CLI, implements with a pluggable CLI provider, runs a security review,
and pushes to production.

Supported CLI providers are defined in providers.json.
"""
AGENT_VERSION = "2026.03.13-v10"  # bump this to verify we're running the right code

import json
import mimetypes
import os
import random
import re
import sys
import time
import subprocess
import logging
import threading
import uuid
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone
from shutil import which

import concurrent.futures

import requests

# ── Resolve paths relative to this script ───────────────────────────────────
AGENT_DIR = Path(__file__).resolve().parent

# ── Load .env file ──────────────────────────────────────────────────────────
ENV_FILE = AGENT_DIR / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

# ── Config ──────────────────────────────────────────────────────────────────
_project_dir_setting = Path(os.environ.get("PROJECT_DIR", str(AGENT_DIR)))
PROJECT_DIR   = _project_dir_setting if _project_dir_setting.is_dir() else AGENT_DIR
DATA_DIR      = AGENT_DIR / "data"
PHOTOS_DIR    = DATA_DIR / "photos"
TASKS_FILE    = Path(os.environ.get("TASKS_FILE", str(DATA_DIR / "tasks.json")))
IDEAS_FILE    = Path(os.environ.get("IDEAS_FILE", str(DATA_DIR / "ideas.json")))
STATE_FILE    = AGENT_DIR / "agent-state.json"
LOG_FILE      = AGENT_DIR / "agent.log"
POLL_INTERVAL = int(os.environ.get("AGENT_POLL_INTERVAL", "3600"))
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
TRIGGER_PORT  = int(os.environ.get("AGENT_TRIGGER_PORT", "8099"))

GITHUB_MODELS_URL = os.environ.get("GITHUB_MODELS_URL",
                                    "https://models.inference.ai.azure.com/chat/completions")
GITHUB_MODELS_MODEL = os.environ.get("GITHUB_MODELS_MODEL", "gpt-4o")
MAX_API_CALLS_PER_DAY = int(os.environ.get("AGENT_MAX_CALLS", "10"))

SESSION_DIR = Path(os.environ.get("AGENT_SESSION_DIR",
                                   str(Path.home() / ".copilot" / "session-state")))
AUTO_IMPLEMENT_DEFAULT = os.environ.get("AGENT_AUTO_IMPLEMENT_DEFAULT", "true").lower() == "true"

# ── Runtime directories ─────────────────────────────────────────────────────
DATA_DIR.mkdir(exist_ok=True)
PHOTOS_DIR.mkdir(exist_ok=True)
WEB_DIR = AGENT_DIR / "web"
# Pre-resolve for path-traversal checks in _serve_file (avoid resolving on every request)
_WEB_DIR_RESOLVED = str(WEB_DIR.resolve())
_PHOTOS_DIR_RESOLVED = str(PHOTOS_DIR.resolve())
RESEARCH_DIR = AGENT_DIR / "research-output"
RESEARCH_DIR.mkdir(exist_ok=True)
SECURITY_REVIEW_DIR = AGENT_DIR / "security-reviews"
SECURITY_REVIEW_DIR.mkdir(exist_ok=True)
PIPELINE_OUTPUT_DIR = AGENT_DIR / "pipeline-output"
PIPELINE_OUTPUT_DIR.mkdir(exist_ok=True)


# ── Pipeline stats (subagent call tracking) ────────────────────────────────
_pipeline_stats_lock = threading.Lock()
_pipeline_stats: dict = {}  # {stage: {"cli_calls": int, "subagents": int, "tool_calls": {}}}


def _reset_pipeline_stats():
    """Reset stats at the start of a pipeline run."""
    with _pipeline_stats_lock:
        _pipeline_stats.clear()


def _record_cli_call(phase: str, subagent_count: int = 0,
                     tool_counts: dict | None = None):
    """Record a CLI invocation and any subagent/tool usage detected in its output."""
    # Map CLI phase back to pipeline stage name
    stage = {"plan": "plan", "implement": "code", "simplify": "simplify",
             "security": "review", "test": "test", "review": "review"}.get(phase, phase)
    with _pipeline_stats_lock:
        if stage not in _pipeline_stats:
            _pipeline_stats[stage] = {"cli_calls": 0, "subagents": 0, "tool_calls": {}}
        _pipeline_stats[stage]["cli_calls"] += 1
        _pipeline_stats[stage]["subagents"] += subagent_count
        if tool_counts:
            for tool, count in tool_counts.items():
                _pipeline_stats[stage]["tool_calls"][tool] = (
                    _pipeline_stats[stage]["tool_calls"].get(tool, 0) + count
                )


def _parse_claude_json_output(raw: str) -> tuple[str, int, dict]:
    """
    Parse Claude Code --output-format json output.
    Returns (text_output, subagent_count, tool_counts_dict).
    If parsing fails, returns the raw string unchanged with zero counts.
    """
    try:
        messages = json.loads(raw)
        if not isinstance(messages, list):
            return raw, 0, {}
    except (json.JSONDecodeError, TypeError):
        return raw, 0, {}

    text_parts = []
    subagent_count = 0
    tool_counts: dict = {}

    for msg in messages:
        # Only look at assistant messages
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("type", "")
        if role not in ("assistant",):
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            text_parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_name = block.get("name", "unknown")
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
                if tool_name == "Agent":
                    subagent_count += 1

    text_output = "\n".join(text_parts).strip()
    # If we got no text from parsing, fall back to raw
    if not text_output:
        return raw, subagent_count, tool_counts
    return text_output, subagent_count, tool_counts


def _get_pipeline_stats_summary() -> dict:
    """Return a copy of current pipeline stats."""
    with _pipeline_stats_lock:
        return {stage: dict(data) for stage, data in _pipeline_stats.items()}


def _get_stage_stats(stage_name: str) -> dict:
    """Get stats for a specific pipeline stage. Returns copy."""
    with _pipeline_stats_lock:
        data = _pipeline_stats.get(stage_name, {"cli_calls": 0, "subagents": 0, "tool_calls": {}})
        return {"cli_calls": data["cli_calls"], "subagents": data["subagents"],
                "tool_calls": dict(data.get("tool_calls", {}))}


# ═══════════════════════════════════════════════════════════════════════════
#  CLI PROVIDER SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

PROVIDERS_FILE = AGENT_DIR / "providers.json"
SKILLS_FILE = AGENT_DIR / "skills.json"
SKILLS_OUTPUT_DIR = AGENT_DIR / "skill-output"
SKILLS_OUTPUT_DIR.mkdir(exist_ok=True)


def _load_json_file(path: Path, default: dict, label: str = "") -> dict:
    """Load a JSON config file. Returns *default* if absent or unreadable."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("Could not load %s: %s", label or path.name, e)
        return default


def _load_providers() -> dict:
    """Load provider definitions from providers.json."""
    return _load_json_file(PROVIDERS_FILE,
                           {"providers": {}, "default_provider": "claude"},
                           "providers.json")


def _get_provider(name: str | None = None) -> dict:
    """Get a provider config by name, falling back to default."""
    cfg = _load_providers()
    providers = cfg.get("providers", {})
    name = (name or "").strip().lower() or cfg.get("default_provider", "claude")
    if name in providers:
        return providers[name]
    # Fuzzy match (e.g. "github-copilot" → "copilot")
    for key, val in providers.items():
        if name in key or key in name:
            return val
    raise ValueError(f"Unknown CLI provider: {name}. "
                     f"Available: {', '.join(providers.keys())}")


def get_provider_for_phase(phase: str, task_override: str | None = None) -> dict:
    """
    Resolve provider for a given phase (plan / implement / security).
    Priority: task-level override → phase env var → global env var → providers.json default.
    """
    if task_override:
        return _get_provider(task_override)

    env_map = {
        "plan":      "CLI_PLAN_PROVIDER",
        "implement": "CLI_IMPLEMENT_PROVIDER",
        "simplify":  "CLI_IMPLEMENT_PROVIDER",
        "security":  "CLI_SECURITY_PROVIDER",
        "test":      "CLI_TEST_PROVIDER",
        "review":    "CLI_REVIEW_PROVIDER",
        "rewrite":   "CLI_PLAN_PROVIDER",
    }
    env_key = env_map.get(phase, "CLI_PROVIDER")
    provider_name = os.environ.get(env_key) or os.environ.get("CLI_PROVIDER")
    return _get_provider(provider_name)




# ═══════════════════════════════════════════════════════════════════════════
#  SKILLS SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

_skills_lock = threading.Lock()
_skill_runs: dict = {}  # {run_id: {status, skill_id, output, started, ...}}


def _load_skills() -> dict:
    """Load user-defined skills from skills.json."""
    return _load_json_file(SKILLS_FILE, {"skills": {}}, "skills.json")


def _save_skills(data: dict):
    """Write skills config back to skills.json."""
    SKILLS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _discover_env_skills() -> dict:
    """
    Auto-discover skills from .claude/skills/ directories in PROJECT_DIR.
    Parses SKILL.md files for name, description, and instructions.
    Returns dict of {skill_id: skill_definition}.
    """
    discovered = {}
    skills_root = PROJECT_DIR / ".claude" / "skills"
    if not skills_root.is_dir():
        return discovered

    for skill_dir in skills_root.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
            # Parse name from first heading
            name_match = re.search(r"^#\s+(.+)", text, re.MULTILINE)
            name = name_match.group(1).strip() if name_match else skill_dir.name

            # Parse description from ## Description section
            desc_match = re.search(
                r"## Description\n+(.+?)(?=\n##|\Z)", text, re.DOTALL
            )
            description = desc_match.group(1).strip().split("\n")[0] if desc_match else ""

            # Parse triggers
            triggers = []
            trig_match = re.search(
                r"## Triggers\n+(.+?)(?=\n##|\Z)", text, re.DOTALL
            )
            if trig_match:
                for line in trig_match.group(1).strip().splitlines():
                    line = line.strip().lstrip("- ").strip('"')
                    if line:
                        triggers.append(line)

            skill_id = f"env:{skill_dir.name}"
            discovered[skill_id] = {
                "name": name,
                "description": description,
                "prompt": f"Follow the instructions in {skill_file} to complete this task. {{input}}",
                "provider": None,
                "timeout": 300,
                "phase": "implement",
                "tags": ["environment"],
                "source": "environment",
                "triggers": triggers,
                "skill_file": str(skill_file),
            }
        except Exception as e:
            logging.warning("Could not parse skill %s: %s", skill_dir.name, e)

    return discovered


def get_all_skills() -> dict:
    """Get merged dict of user-defined + environment-discovered skills."""
    user_skills = _load_skills().get("skills", {})
    env_skills = _discover_env_skills()
    # User skills override env skills with same id
    merged = {}
    merged.update(env_skills)
    merged.update(user_skills)
    return merged


def run_skill(skill_id: str, input_text: str = "",
              provider_override: str | None = None) -> dict:
    """
    Execute a skill by running its prompt through a CLI provider.
    Returns {"success": bool, "output": str, "run_id": str, "elapsed": float}.
    """
    all_skills = get_all_skills()
    if skill_id not in all_skills:
        return {"success": False, "output": f"Skill '{skill_id}' not found",
                "run_id": "", "elapsed": 0}

    skill = all_skills[skill_id]
    run_id = f"skill-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    # Build the final prompt
    prompt_template = skill.get("prompt", "")
    prompt = prompt_template.replace("{input}", input_text).strip()
    if not prompt:
        return {"success": False, "output": "Skill has no prompt template",
                "run_id": run_id, "elapsed": 0}

    phase = skill.get("phase", "implement")
    provider_name = provider_override or skill.get("provider")
    provider = get_provider_for_phase(phase, provider_name)

    # Track the run
    with _skills_lock:
        _skill_runs[run_id] = {
            "status": "running",
            "skill_id": skill_id,
            "skill_name": skill.get("name", skill_id),
            "input": input_text[:200],
            "started": datetime.now(timezone.utc).isoformat(),
            "output": "",
        }

    log.info(">>> Skill '%s' started (run_id=%s, provider=%s)",
             skill.get("name", skill_id), run_id, provider.get("name", "?"))

    start = time.time()
    try:
        # Write prompt file for long prompts
        prompt_file = None
        if len(prompt) > _PROMPT_FILE_THRESHOLD:
            out_dir = SKILLS_OUTPUT_DIR / run_id
            out_dir.mkdir(parents=True, exist_ok=True)
            prompt_file = out_dir / ".prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")

        success, output = run_cli_command(provider, phase, prompt,
                                          prompt_file=prompt_file)
        elapsed = round(time.time() - start, 1)

        # Save output
        out_dir = SKILLS_OUTPUT_DIR / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "output.md").write_text(output or "", encoding="utf-8")

        with _skills_lock:
            _skill_runs[run_id] = {
                "status": "done" if success else "failed",
                "skill_id": skill_id,
                "skill_name": skill.get("name", skill_id),
                "input": input_text[:200],
                "started": _skill_runs[run_id]["started"],
                "finished": datetime.now(timezone.utc).isoformat(),
                "output": output[:500] if output else "",
                "elapsed": elapsed,
                "success": success,
            }

        log.info("<<< Skill '%s' %s in %.1fs (run_id=%s)",
                 skill.get("name", skill_id),
                 "succeeded" if success else "failed", elapsed, run_id)

        return {"success": success, "output": output or "",
                "run_id": run_id, "elapsed": elapsed}

    except Exception as e:
        elapsed = round(time.time() - start, 1)
        log.error("Skill '%s' error: %s", skill_id, e)
        with _skills_lock:
            _skill_runs[run_id] = {
                "status": "error",
                "skill_id": skill_id,
                "skill_name": skill.get("name", skill_id),
                "input": input_text[:200],
                "started": _skill_runs.get(run_id, {}).get("started", ""),
                "finished": datetime.now(timezone.utc).isoformat(),
                "output": str(e),
                "elapsed": elapsed,
                "success": False,
            }
        return {"success": False, "output": str(e),
                "run_id": run_id, "elapsed": elapsed}


def _write_prompt_file(prompt: str, task_id: str, stage: str, phase: str) -> Path:
    """Write the prompt to a temp file and return its path."""
    task_dir = PIPELINE_OUTPUT_DIR / (task_id or "scratch")
    task_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = task_dir / f".prompt-{stage}-{phase}.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    return prompt_file


_PROMPT_FILE_THRESHOLD = 6000  # chars — auto-switch to file-based prompt above this


def build_cli_command(provider: dict, phase: str, prompt: str,
                      prompt_file: Path | None = None) -> list[str]:
    """
    Build the full CLI command list from a provider config.
    Replaces {prompt} placeholder in args with the actual prompt text,
    and {prompt_file} with the path to a file containing the prompt.

    When a prompt_file is provided and the prompt exceeds _PROMPT_FILE_THRESHOLD
    chars, automatically substitutes "-p {prompt}" with file-based input to avoid
    OS command-line length limits (Windows: 8191 for cmd.exe, 32K for CreateProcess).
    """
    binary = provider["binary"]
    # Resolve .cmd/.bat/.exe on Windows so subprocess can find it
    resolved = which(binary)
    if resolved:
        binary = resolved
    sub = provider.get("subcommand", [])

    arg_key = {
        "plan":      "plan_args",
        "implement": "implement_args",
        "simplify":  "simplify_args",
        "security":  "security_args",
        "test":      "test_args",
        "review":    "review_args",
    }.get(phase, "implement_args")

    # Fallback: test/review → plan_args, simplify → implement_args if not defined
    if arg_key not in provider and phase in ("test", "review"):
        arg_key = "plan_args"
    if arg_key not in provider and phase == "simplify":
        arg_key = "implement_args"

    args = list(provider.get(arg_key, ["-p", "{prompt}"]))

    # Auto-switch: when prompt is long and a file exists, replace inline "-p {prompt}"
    # with file-based input to avoid command-line truncation on Windows.
    use_file = (prompt_file and len(prompt) > _PROMPT_FILE_THRESHOLD)
    if use_file:
        bin_name = Path(binary).stem.lower()
        new_args = []
        i = 0
        swapped = False
        while i < len(args):
            if args[i] in ("-p", "--prompt", "--message") and i + 1 < len(args) and "{prompt}" in args[i + 1]:
                # Replace with file-based flag for known CLIs
                if bin_name == "claude":
                    new_args.extend(["--prompt-file", str(prompt_file)])
                elif bin_name == "aider":
                    new_args.extend(["--message-file", str(prompt_file)])
                else:
                    # Generic fallback: read file content into the arg
                    new_args.extend([args[i], prompt])
                    i += 2
                    continue
                swapped = True
                i += 2
                continue
            new_args.append(args[i].replace("{prompt_file}", str(prompt_file)).replace("{prompt}", prompt))
            i += 1
        if swapped:
            log.info("   Auto-switched to prompt file (%d chars > %d threshold): %s",
                     len(prompt), _PROMPT_FILE_THRESHOLD, prompt_file)
            args = new_args
        else:
            # No "-p {prompt}" found to swap; fall through to normal substitution
            pf_str = str(prompt_file) if prompt_file else ""
            args = [a.replace("{prompt_file}", pf_str).replace("{prompt}", prompt) for a in args]
    else:
        pf_str = str(prompt_file) if prompt_file else ""
        args = [a.replace("{prompt_file}", pf_str).replace("{prompt}", prompt) for a in args]

    return [binary] + sub + args


def get_timeout(provider: dict, phase: str) -> int | None:
    """Get timeout for a phase, checking env overrides then provider config.
    Returns None (no timeout) when the resolved value is 0."""
    env_map = {
        "plan":      ("PIPELINE_PLAN_TIMEOUT",      "COPILOT_PLAN_TIMEOUT"),
        "implement": ("PIPELINE_CODE_TIMEOUT",      "COPILOT_TIMEOUT"),
        "simplify":  ("PIPELINE_SIMPLIFY_TIMEOUT",  "COPILOT_TIMEOUT"),
        "security":  ("PIPELINE_REVIEW_TIMEOUT",    "COPILOT_SECURITY_TIMEOUT"),
        "test":      ("PIPELINE_TEST_TIMEOUT",      "COPILOT_SECURITY_TIMEOUT"),
        "review":    ("PIPELINE_REVIEW_TIMEOUT",    "COPILOT_SECURITY_TIMEOUT"),
    }
    for env_key in env_map.get(phase, ("COPILOT_TIMEOUT",)):
        env_val = os.environ.get(env_key)
        if env_val:
            v = int(env_val)
            return None if v == 0 else v

    timeout_key = {
        "plan":      "plan_timeout",
        "implement": "implement_timeout",
        "security":  "security_timeout",
        "test":      "test_timeout",
        "review":    "review_timeout",
    }.get(phase, "implement_timeout")

    v = int(provider.get(timeout_key, 600))
    return None if v == 0 else v


def list_available_providers() -> dict[str, str]:
    """Return dict of provider_key → provider_name for UI/status."""
    cfg = _load_providers()
    return {k: v.get("name", k) for k, v in cfg.get("providers", {}).items()}


# ═══════════════════════════════════════════════════════════════════════════
#  PLANNING BACKEND
# ═══════════════════════════════════════════════════════════════════════════

class PlanningBackend(Enum):
    CLI = "cli"              # any CLI provider
    GPT4O_API = "gpt4o_api"  # GitHub Models REST API
    CUSTOM_API = "custom_api"

    # Legacy aliases
    CLI_COPILOT = "cli_copilot"

DEFAULT_PLANNING_BACKEND = os.environ.get("AGENT_DEFAULT_PLANNING_BACKEND", "cli")

# Normalise legacy values
_BACKEND_ALIASES = {"cli_copilot": "cli", "copilot": "cli"}

def _normalise_backend(val: str) -> str:
    return _BACKEND_ALIASES.get(val.lower().strip(), val.lower().strip())


# ═══════════════════════════════════════════════════════════════════════════
#  PIPELINE — config, helpers, and orchestrator
# ═══════════════════════════════════════════════════════════════════════════

_PIPELINE_DEFAULT: dict = {
    "program_manager": {
        "backend": "github_models",
        "model": "gpt-4o",
        "max_tokens": 4096,
        "temperature": 0.3,
    },
    "stages": {
        "rewrite": {"enabled": True, "timeout": 120},
        "plan":    {"enabled": True, "team": ["claude"], "timeout": 900},
        "code":    {"enabled": True, "team": ["claude"], "timeout": 600},
        "test":    {"enabled": True, "team": ["claude"], "timeout": 300},
        "review":  {"enabled": True, "team": ["claude"], "timeout": 300},
    },
    "publish": {"enabled": True, "auto_push": True, "block_on_severity": "high"},
}


def load_pipeline() -> dict:
    """Load pipeline.json; returns built-in default if absent or unreadable."""
    pipeline_file = Path(os.environ.get("PIPELINE_FILE", str(AGENT_DIR / "pipeline.json")))
    return _load_json_file(pipeline_file, _PIPELINE_DEFAULT, "pipeline.json")


def _clean_env() -> dict:
    """Return a copy of os.environ without vars that block nested CLI sessions."""
    env = dict(os.environ)
    # Claude Code refuses to launch inside another Claude Code session
    for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION"):
        env.pop(key, None)
    return env


# ── Externalized prompt templates ────────────────────────────────────────────
# Loaded from prompts.json at startup; inline fallbacks keep the agent
# functional when the file is missing.  CLI prompts MUST stay short
# (<500 chars of instructions) to avoid triggering permission prompts.

_CLI_PROMPT_WARN_CHARS = 500  # instruction-only threshold for CLI prompts

# Single source of truth for fallback strings (used when prompts.json is absent).
# These match the values in prompts.json — edit ONE place, not both.
_FALLBACK_PROMPTS: dict = {
    "pm_system": {
        "director": (
            "You are a senior Program Manager directing an AI coding team through a "
            "multi-stage pipeline (plan → code → simplify → test → review → publish). "
            "You write precise, actionable task briefs."),
        "overseer": (
            "You are a senior Program Manager overseeing an AI coding pipeline. "
            "Prioritize technical accuracy — disagree when output is wrong. "
            "You MUST NOT approve work that is incomplete, incorrect, or has drifted."),
        "merger": (
            "You are a senior PM overseeing an AI coding pipeline. "
            "Produce a merged result from multiple implementations + cross-reviews. "
            "Fix ALL identified issues. Do NOT approve code with known bugs."),
        "rewriter": "You are a senior PM preparing a user request for an AI coding pipeline.",
        "cross_reviewer": (
            "You are a senior code reviewer. Prioritize technical accuracy. "
            "Disagree when code is wrong."),
    },
    "cli_prompts": {
        "plan": (
            "I need to understand the codebase before making changes.\n\n"
            "Task context: {prompt}\n\n"
            "Output a step-by-step implementation plan with:\n"
            "- Actionable verb-led steps with specific files\n"
            "- Incremental build order\n"
            "- Testing strategy"),
        "code_suffix": (
            "\n\nFollow existing conventions. Use descriptive names. "
            "Fix root causes. Keep changes minimal."),
        "simplify": (
            "Run git diff to see recent changes. Review for quality, then fix "
            "issues found: duplicate logic, bad naming, empty catch blocks, dead "
            "code, deep nesting. Cap fix iterations at 3 per file.\n\n"
            "Original task: {prompt}"),
        "test": (
            "Run git diff to see recent changes, then verify they work correctly. "
            "If tests fail, fix the code not the tests. Report specific pass/fail "
            "results.\n\nOriginal task: {prompt}"),
        "review": (
            "Run git diff to see recent changes. Perform a defensive security "
            "audit: check for hardcoded secrets, PII exposure, injection "
            "vulnerabilities. Rate findings LOW/MEDIUM/HIGH.\n\n"
            "Original task: {prompt}"),
    },
    "rewrite_format": (
        "Rewrite the following user request to be clear and actionable for a coding AI "
        "pipeline that WRITES CODE. If the request is vague or exploratory "
        "(research/investigate/look into), convert it into a concrete coding task: "
        "diagnose the root cause AND fix it. Structure as: WHAT, WHERE, WHY, CONSTRAINTS. "
        "Return only the rewritten prompt.\n\nOriginal request:\n{prompt}"),
}

# Loaded once at init (before threads spawn); None = not yet loaded.
_PROMPTS: dict | None = None


def _load_prompts() -> dict:
    """Load prompts.json once at first call; return cached dict thereafter."""
    global _PROMPTS
    if _PROMPTS is not None:
        return _PROMPTS
    pf = Path(os.environ.get("PROMPTS_FILE", str(AGENT_DIR / "prompts.json")))
    _PROMPTS = _load_json_file(pf, {}, "prompts.json")
    if _PROMPTS:
        log.info("Loaded %d prompt sections from %s", len(_PROMPTS), pf)
    return _PROMPTS


def _get_prompt(section: str, key: str | None = None, fallback: str = "") -> str:
    """Get a prompt template from prompts.json or _FALLBACK_PROMPTS.

    When *key* is None, looks up a top-level key (e.g. "rewrite_format").
    When *key* is given, looks up section[key] (e.g. "pm_system"/"director").
    Falls back to _FALLBACK_PROMPTS, then to *fallback*.
    """
    prompts = _load_prompts()
    if key is None:
        result = prompts.get(section)
        if result is not None:
            return result
        return _FALLBACK_PROMPTS.get(section, fallback)
    result = prompts.get(section, {}).get(key)
    if result is not None:
        return result
    return _FALLBACK_PROMPTS.get(section, {}).get(key, fallback)


def _warn_cli_prompt_size(stage: str, prompt: str, dynamic_len: int = 0):
    """Log a warning if CLI prompt instructions exceed safe threshold."""
    instruction_len = len(prompt) - dynamic_len
    if instruction_len > _CLI_PROMPT_WARN_CHARS:
        log.warning("   ⚠️ CLI prompt for '%s' has %d instruction chars "
                     "(threshold %d) — risk of permission prompts",
                     stage, instruction_len, _CLI_PROMPT_WARN_CHARS)


def run_cli_command(provider: dict, phase: str, prompt: str,
                    cwd: str | None = None,
                    prompt_file: Path | None = None) -> tuple[bool, str]:
    """Run a CLI provider command. Returns (success, output_text)."""
    cmd = build_cli_command(provider, phase, prompt, prompt_file=prompt_file)
    timeout = get_timeout(provider, phase)
    work_dir = cwd or str(PROJECT_DIR)
    # Fall back to agent dir if target dir doesn't exist
    if not Path(work_dir).is_dir():
        log.warning("   cwd '%s' does not exist — falling back to %s", work_dir, AGENT_DIR)
        work_dir = str(AGENT_DIR)
    timeout_label = f"{timeout}s" if timeout else "no timeout"
    # Log full command (with prompt truncated to 100 chars)
    cmd_display = []
    for c in cmd:
        cmd_display.append(c[:100] + "…" if len(c) > 100 else c)
    log.info("   CLI [%s/%s]: %s (%s)",
             provider.get("name", "?"), phase, " ".join(cmd_display), timeout_label)
    try:
        result = subprocess.run(
            cmd, cwd=work_dir,
            capture_output=True, text=True, timeout=timeout,
            env=_clean_env(), encoding="utf-8", errors="replace",
        )
        log.info("   Exit code: %d | output: %d chars", result.returncode, len(result.stdout or ""))
        if result.stdout and result.stdout.strip():
            out = result.stdout.strip()
            snippet = out[:500] + ("\n…[truncated]" if len(out) > 500 else "")
            log.info("   Output preview:\n%s", snippet)
        if result.stderr and result.stderr.strip():
            log.warning("   Stderr: %s", result.stderr.strip()[-500:])
        output = result.stdout.strip() if result.stdout else result.stderr.strip()
        # Parse Claude JSON output for subagent/tool tracking
        subagent_count, tool_counts = 0, {}
        is_claude = provider.get("binary", "") == "claude"
        if is_claude and output and output.startswith("["):
            text_output, subagent_count, tool_counts = _parse_claude_json_output(output)
            if text_output != output:  # successfully parsed JSON
                output = text_output
                log.info("   📊 Parsed Claude JSON: %d subagents, tools: %s",
                         subagent_count, tool_counts)
        _record_cli_call(phase, subagent_count, tool_counts)
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        log.error("   Timed out after %s", timeout_label)
        _record_cli_call(phase)
        return False, f"Timed out after {timeout_label}"
    except Exception as e:
        log.error("   CLI error: %s", e)
        _record_cli_call(phase)
        return False, str(e)


def _pm_api_call(system_msg: str, user_msg: str, pm_cfg: dict,
                  _retries: int = 3, _backoff: float = 2.0) -> str:
    """Low-level PM API call with retry on 429/5xx. Raises on persistent failure."""
    backend = pm_cfg.get("backend", "github_models")
    model = pm_cfg.get("model", "gpt-4o")
    max_tokens = pm_cfg.get("max_tokens", 4096)
    temperature = pm_cfg.get("temperature", 0.3)
    pm_timeout = int(os.environ.get("PIPELINE_MANAGER_TIMEOUT", "300"))
    last_err = None

    # Build request params based on backend
    if backend == "github_models":
        if not GITHUB_TOKEN:
            raise ValueError("GITHUB_TOKEN not set")
        url = GITHUB_MODELS_URL
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}",
                   "Content-Type": "application/json"}
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system_msg},
                             {"role": "user",   "content": user_msg}]}
        extract = lambda r: r.json()["choices"][0]["message"]["content"]

    elif backend == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens, "system": system_msg,
                "messages": [{"role": "user", "content": user_msg}]}
        extract = lambda r: r.json()["content"][0]["text"]

    elif backend == "openai_compatible":
        url = os.environ.get("PIPELINE_PM_URL",
                             "http://localhost:11434/v1/chat/completions")
        key = os.environ.get("PIPELINE_PM_KEY") or os.environ.get("OPENAI_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}",
                   "Content-Type": "application/json"}
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system_msg},
                             {"role": "user",   "content": user_msg}]}
        extract = lambda r: r.json()["choices"][0]["message"]["content"]

    else:
        raise ValueError(f"Unknown PM backend: {backend}")

    # Retry loop with exponential backoff on 429 / 5xx
    for attempt in range(_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=pm_timeout)
            resp.raise_for_status()
            return extract(resp)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", 0)
            if status in (429, 500, 502, 503, 504) and attempt < _retries:
                wait = _backoff * (2 ** attempt) + random.uniform(0, 1)
                log.warning("   PM API %d — retrying in %.1fs (attempt %d/%d)",
                            status, wait, attempt + 1, _retries)
                time.sleep(wait)
                last_err = e
                continue
            raise
        except requests.exceptions.Timeout as e:
            if attempt < _retries:
                wait = _backoff * (2 ** attempt) + random.uniform(0, 1)
                log.warning("   PM API timeout — retrying in %.1fs (attempt %d/%d)",
                            wait, attempt + 1, _retries)
                time.sleep(wait)
                last_err = e
                continue
            raise
    raise last_err  # should not reach here, but just in case


def pm_direct_team(stage_name: str, original_prompt: str, context: str,
                   team: list, pm_cfg: dict) -> tuple[str, bool]:
    """
    PM acts as director BEFORE the team runs: given the stage, request, and all
    prior context, it writes a precise task brief for the team to execute.
    Falls back to a structured direct prompt on failure.

    Returns (brief, pm_succeeded).
    """
    system_msg = _get_prompt("pm_system", "director")
    guidance = _get_prompt("pm_stage_guidance", stage_name)
    user_msg = (
        f"Stage: {stage_name}\n"
        f"Team members: {', '.join(team)}\n"
        f"Original user request: {original_prompt}\n\n"
        f"Prior pipeline context:\n{context}\n\n"
        f"{guidance}\n\n"
        "Write a clear, specific task brief for the team. "
        "Include: what to do, which files/components to focus on, constraints from prior stages, "
        "and expected output format. Be concise and actionable. "
        "Return only the task brief — no preamble."
    )
    log.info("   PM [direct/%s]: generating task brief for team %s…", stage_name, team)
    try:
        brief = _pm_api_call(system_msg, user_msg, pm_cfg)
        log.info("   PM brief (%d chars): %s…", len(brief), brief[:300])
        return brief, True
    except Exception as e:
        log.warning("⚠️ PM direction failed (%s) — building direct prompt", e)
        return _build_direct_prompt(stage_name, original_prompt, context), False


_GARBAGE_STRONG = [
    "what would you like", "could you share", "could you describe",
    "could you provide", "could you grant", "could you tell",
    "could you give", "could you let", "could you help",
    "could you approve", "could you confirm", "could you try",
    "please describe", "please provide", "please share",
    "please approve", "i don't see", "i don't have",
    "what bug are you", "message might be incomplete",
    "message got cut off", "message may have been cut off",
    "message was cut off", "message appears to be cut off",
    "got cut off", "was cut off", "appears incomplete",
    "no content after it", "but no content", "but no details",
    "plan handoff", "no plan content",
    "what do you want", "how can i help", "can you clarify",
    "what specific", "what exactly", "can you tell me",
    "need more context", "i'd be happy to help", "i'll need to know",
    "grant permission", "i need access", "need write permission",
    "need permission", "approve the edit", "can you confirm",
    "i need write", "write permission",
]

_GARBAGE_WEAK = [
    "more information", "more details",
    "permission was denied", "permission denied",
]

# Combined flat list for line-level filtering in _clean_stage_output
_GARBAGE_PATTERNS = _GARBAGE_STRONG + _GARBAGE_WEAK


def _is_garbage_output(team_outputs: list) -> bool:
    """Check if team output is garbage (questions, too short, permission asks, etc.).
    Uses tiered scoring: strong signals (multi-word phrases) = 2pts, weak signals = 1pt.
    Weak signals only count if output is short (<500 chars) or has question marks.
    Threshold = 2 points.
    """
    total_len = sum(len(out) for _, out in team_outputs)
    if total_len < 100:  # Trivially short — almost always a permission denial or empty run
        return True
    all_output = " ".join(out.lower() for _, out in team_outputs)
    score = 0
    for pat in _GARBAGE_STRONG:
        if pat in all_output:
            score += 2
    if score >= 2:
        return True
    # Weak signals only count if output is short or contains question marks
    is_short_or_questioning = total_len < 500 or '?' in all_output
    if is_short_or_questioning:
        for pat in _GARBAGE_WEAK:
            if pat in all_output:
                score += 1
    return score >= 2


def pm_oversee_stage(stage_name: str, original_prompt: str, context: str,
                     team_outputs: list, pm_cfg: dict) -> dict:
    """
    PM acts as overseer AFTER the team runs. Instead of just picking/merging,
    the PM verifies stage output against requirements, flags quality issues,
    and decides whether to APPROVE (pass to next stage) or REVISE (needs rework).

    Returns {"verdict": "approve"|"revise", "synthesis": str, "handoff": str,
             "issues": list[str], "full_response": str}
    Falls back to approve with concatenated outputs on API failure.
    """
    agent_blocks = "\n\n".join(
        f"--- Agent: {name} ---\n{output}" for name, output in team_outputs
    )
    system_msg = _get_prompt("pm_system", "overseer")
    criteria = _get_prompt("pm_stage_criteria", stage_name)
    user_msg = (
        f"Stage: {stage_name}\n"
        f"Original user request: {original_prompt}\n\n"
        f"Prior pipeline context:\n{context}\n\n"
        f"Agent outputs:\n\n{agent_blocks}\n\n"
        f"{criteria}\n\n"
        "Evaluate rigorously:\n"
        "1. **Requirements check**: missed requirements or gaps?\n"
        "2. **Quality check**: bugs, incomplete work?\n"
        "3. **Drift check**: deviated from plan?\n"
        "4. **Verdict**: APPROVE only if ALL standards met. REVISE if in doubt.\n\n"
        "## Verdict\nAPPROVE or REVISE\n\n"
        "## Issues\n[problems, or 'None']\n\n"
        "## Synthesis\n[best combined output]\n\n"
        "## Handoff to next stage\n[context for next team]"
    )

    for name, output in team_outputs:
        log.info("   Agent output [%s]: %d chars", name, len(output))
    log.info("   PM [oversee/%s]: evaluating %d agent output(s)…",
             stage_name, len(team_outputs))
    # ── First: check team output for obvious garbage BEFORE calling PM ─────
    total_output_len = sum(len(out) for _, out in team_outputs)
    looks_garbage = _is_garbage_output(team_outputs)

    if looks_garbage:
        log.warning("   ❌ Team output is garbage (%d chars, matches patterns) — REVISE",
                     total_output_len)
        fallback = "\n\n".join(f"## {name}\n{output}" for name, output in team_outputs)
        return {
            "verdict": "revise",
            "synthesis": fallback,
            "handoff": "Team output was garbage — agent asked questions or gave no content.",
            "issues": ["Team output is a clarification question, not actual work."],
            "full_response": fallback,
            "pm_succeeded": False,
        }

    try:
        result = _pm_api_call(system_msg, user_msg, pm_cfg)
        parsed = _parse_overseer_response(result)
        parsed["pm_succeeded"] = True
        log.info("   PM verdict: %s | issues: %d", parsed["verdict"].upper(), len(parsed.get("issues", [])))
        if parsed.get("issues"):
            for issue in parsed["issues"]:
                log.info("     ⚠️  %s", issue)
        if parsed.get("handoff"):
            log.info("   Handoff (%d chars): %s…", len(parsed["handoff"]), parsed["handoff"][:200])
        return parsed
    except Exception as e:
        log.warning("⚠️ PM oversight failed (%s) — auto-approving (output passed garbage check)", e)
        fallback = "\n\n".join(f"## {name}\n{output}" for name, output in team_outputs)
        return {
            "verdict": "approve",
            "synthesis": fallback,
            "handoff": fallback,
            "issues": [],
            "full_response": fallback,
            "pm_succeeded": False,
        }


def _parse_overseer_response(text: str) -> dict:
    """Parse the PM overseer's structured response into a dict."""
    result = {
        "verdict": "approve",
        "synthesis": "",
        "handoff": "",
        "issues": [],
        "full_response": text,
    }

    # Extract verdict
    verdict_m = re.search(r'##\s*Verdict\s*\n\s*(APPROVE|REVISE)', text, re.IGNORECASE)
    if verdict_m:
        result["verdict"] = verdict_m.group(1).strip().lower()
    else:
        # No ## Verdict header — heuristic: look for explicit approval signals
        lower = text.lower()
        approval_signals = ["approved", "looks good", "meets requirements",
                            "lgtm", "well done", "production-ready"]
        if any(sig in lower for sig in approval_signals):
            log.warning("   PM response missing ## Verdict — inferred APPROVE from text signals")
            result["verdict"] = "approve"
        else:
            log.warning("   PM response missing ## Verdict and no approval signals — defaulting to REVISE")
            result["verdict"] = "revise"
            result["issues"].append("PM response was unstructured — flagged for review")

    # Extract issues
    issues_m = re.search(r'##\s*Issues\s*\n(.*?)(?=\n##|\Z)', text, re.DOTALL)
    if issues_m:
        issues_text = issues_m.group(1).strip()
        if issues_text.lower() != "none":
            result["issues"] = [
                line.strip().lstrip("-*• ").strip()
                for line in issues_text.splitlines()
                if line.strip() and line.strip() not in ("-", "*", "•")
            ]

    # Extract synthesis
    synth_m = re.search(r'##\s*Synthesis\s*\n(.*?)(?=\n##|\Z)', text, re.DOTALL)
    if synth_m:
        result["synthesis"] = synth_m.group(1).strip()

    # Extract handoff
    handoff_m = re.search(r'##\s*Handoff[^\n]*\n(.*?)(?=\n##|\Z)', text, re.DOTALL)
    if handoff_m:
        result["handoff"] = handoff_m.group(1).strip()

    # Fallback: if no synthesis extracted, use full response
    if not result["synthesis"]:
        result["synthesis"] = text
    if not result["handoff"]:
        result["handoff"] = result["synthesis"]

    return result


def cross_review_code(team_outputs: list, plan_context: str,
                      original_prompt: str, timeout: int) -> list:
    """
    When multiple agents produce code, run a cross-review: each agent reviews
    the OTHER agent's implementation against the plan.

    Returns list of (reviewer_name, review_output) tuples.
    Only runs when there are 2+ team outputs.
    """
    if len(team_outputs) < 2:
        return []

    log.info("   🔄 Cross-review: %d implementations to compare",
             len(team_outputs))

    def _review_other(reviewer_idx: int):
        reviewer_name, _reviewer_code = team_outputs[reviewer_idx]
        # Collect all OTHER implementations for this reviewer to examine
        others = [
            (name, code) for i, (name, code) in enumerate(team_outputs)
            if i != reviewer_idx
        ]
        other_blocks = "\n\n".join(
            f"--- Implementation by {name} ---\n{code}" for name, code in others
        )

        cr_system = _get_prompt("pm_system", "cross_reviewer")
        review_prompt = (
            f"{cr_system}\n\n"
            f"Original request: {original_prompt}\n\n"
            f"Plan context:\n{plan_context}\n\n"
            f"Implementations to review:\n\n{other_blocks}\n\n"
            "Review each implementation for correctness, conventions, and security.\n\n"
            "## Agreement Points\n[shared approaches]\n\n"
            "## Divergences\n[differences with file:line refs]\n\n"
            "## Issues Found\n[bugs, security, convention violations]\n\n"
            "## Winner Per Component\n[which is better per feature, and why]\n\n"
            "## Recommended Merge Strategy\n[how to combine the best parts]"
        )
        try:
            provider = get_provider_for_phase("review", reviewer_name)
            success, output = run_cli_command(provider, "review", review_prompt)
            if success and output:
                return f"review-by-{reviewer_name}", output
            log.warning("   Cross-review by '%s' failed or empty", reviewer_name)
            return None
        except Exception as e:
            log.warning("   Cross-review by '%s' error: %s", reviewer_name, e)
            return None

    reviews = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(team_outputs))
    ) as ex:
        futures = {
            ex.submit(_review_other, i): team_outputs[i][0]
            for i in range(len(team_outputs))
        }
        for fut in concurrent.futures.as_completed(futures, timeout=None if timeout is None else timeout + 30):
            try:
                res = fut.result()
                if res:
                    reviews.append(res)
            except Exception as e:
                log.warning("   Cross-review future error: %s", e)

    log.info("   🔄 Cross-review complete: %d reviews collected", len(reviews))
    return reviews


def _build_comparison_summary(team_outputs: list, cross_reviews: list) -> str:
    """Extract structured sections from cross-reviews and build a comparison summary."""
    summary_parts = []
    for reviewer_name, review in cross_reviews:
        summary_parts.append(f"### {reviewer_name}\n")
        for section in ["Agreement Points", "Divergences", "Winner Per Component",
                        "Recommended Merge Strategy"]:
            m = re.search(rf'##\s*{section}\s*\n(.*?)(?=\n##|\Z)', review, re.DOTALL)
            if m:
                summary_parts.append(f"**{section}:** {m.group(1).strip()[:500]}\n")
    return "\n".join(summary_parts) if summary_parts else "No structured comparison available."


def pm_merge_with_reviews(stage_name: str, original_prompt: str, context: str,
                          team_outputs: list, cross_reviews: list,
                          pm_cfg: dict) -> dict:
    """
    PM receives both implementations AND cross-reviews, then produces a deep
    merge that leverages the best parts of each implementation.

    Returns same dict format as pm_oversee_stage().
    """
    impl_blocks = "\n\n".join(
        f"--- Implementation by {name} ---\n{output}"
        for name, output in team_outputs
    )
    review_blocks = "\n\n".join(
        f"--- {name} ---\n{output}"
        for name, output in cross_reviews
    )

    system_msg = _get_prompt("pm_system", "merger")
    user_msg = (
        f"Stage: {stage_name}\n"
        f"Original user request: {original_prompt}\n\n"
        f"Prior pipeline context:\n{context}\n\n"
        f"=== IMPLEMENTATIONS ===\n\n{impl_blocks}\n\n"
        f"=== CROSS-REVIEWS ===\n\n{review_blocks}\n\n"
        "Perform a deep merge:\n"
        "1. **Compare**: What does each implementation do differently? "
        "Where do they agree vs. diverge?\n"
        "2. **Gap analysis**: Using the cross-reviews, identify gaps in EACH implementation. "
        "What did Agent A catch that Agent B missed, and vice versa?\n"
        "3. **Strength mapping**: What is each implementation's strongest contribution?\n"
        "4. **Merged result**: Produce the best unified implementation that:\n"
        "   - Takes the strongest approach for each component\n"
        "   - Fills gaps identified in the cross-reviews\n"
        "   - Resolves any contradictions between implementations\n"
        "5. **Verdict**: APPROVE if the merged result is production-ready, REVISE if not.\n\n"
        "Return your response in these sections:\n"
        "## Comparison\n[brief analysis of differences]\n\n"
        "## Verdict\nAPPROVE or REVISE\n\n"
        "## Issues\n[remaining problems, or 'None']\n\n"
        "## Synthesis\n[the merged implementation — this is what gets used]\n\n"
        "## Handoff to next stage\n[context for the next team]"
    )

    log.info("   PM [deep-merge/%s]: merging %d implementations with %d cross-reviews…",
             stage_name, len(team_outputs), len(cross_reviews))
    try:
        result = _pm_api_call(system_msg, user_msg, pm_cfg)
        return _parse_overseer_response(result)
    except Exception as e:
        log.warning("⚠️ PM deep-merge failed (%s) — falling back to basic oversight", e)
        return pm_oversee_stage(
            stage_name, original_prompt, context, team_outputs, pm_cfg
        )


_NON_INTERACTIVE_FOOTER = ""  # Removed: was causing Claude to prompt for permissions instead of acting


def run_team(stage_name: str, prompt: str, team_provider_names: list,
             context: str, timeout: int | None,
             task_id: str = "") -> list:
    """
    Run a team of CLI providers in parallel for a pipeline stage.
    Returns list of (provider_name, output) for successful runs only.

    Stage outputs from prior stages are saved to files on disk.
    The prompt references these files so CLI agents can read full context
    without cramming everything into the command-line argument.
    """
    phase_map = {
        "rewrite":  "plan",
        "plan":     "plan",
        "code":     "implement",
        "simplify": "simplify",
        "test":     "test",
        "review":   "review",
    }
    phase = phase_map.get(stage_name, "implement")

    # Keep prompt clean and short — verbose prompts confuse Claude Code
    combined_prompt = prompt
    if context:
        combined_prompt = f"{prompt}\n\n{context}"
    combined_prompt = combined_prompt.strip()

    # Write prompt to file so CLI can read it instead of relying on cmd-line arg
    prompt_file = None
    if task_id:
        prompt_file = _write_prompt_file(combined_prompt, task_id, stage_name, phase)

    def _run_one(provider_name: str):
        try:
            provider = get_provider_for_phase(phase, provider_name)
            success, output = run_cli_command(
                provider, phase, combined_prompt, prompt_file=prompt_file
            )
            if success and output:
                return provider_name, output
            log.warning("   Team member '%s' failed or empty", provider_name)
            return None
        except Exception as e:
            log.warning("   Team member '%s' error: %s", provider_name, e)
            return None

    results = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(team_provider_names))
    ) as ex:
        futures = {ex.submit(_run_one, name): name for name in team_provider_names}
        for fut in concurrent.futures.as_completed(futures, timeout=None if timeout is None else timeout + 30):
            try:
                res = fut.result()
                if res:
                    results.append(res)
            except Exception as e:
                log.warning("   Team future error: %s", e)
    return results


def rewrite_prompt(raw_prompt: str, pm_cfg: dict) -> str:
    """Have the PM rewrite the raw prompt for clarity. Falls back to original on failure."""
    system_msg = _get_prompt("pm_system", "rewriter")
    rewrite_fmt = _get_prompt("rewrite_format")
    user_msg = rewrite_fmt.replace("{prompt}", raw_prompt)
    log.info("   PM [rewrite]: clarifying prompt (%d chars)…", len(raw_prompt))
    try:
        result = _pm_api_call(system_msg, user_msg, pm_cfg)
        rewritten = result.strip() or raw_prompt
        log.info("   Rewritten prompt (%d chars): %s…", len(rewritten), rewritten[:300])
        return rewritten
    except Exception as e:
        log.warning("⚠️ PM rewrite failed (%s) — using original prompt", e)
        return raw_prompt


def _cap_context(context: str, max_chars: int = 12000) -> str:
    """Cap context to max_chars by dropping oldest non-plan sections first."""
    if len(context) <= max_chars:
        return context
    # Split on === ... === delimiters (actual format used in pipeline)
    sections = re.split(r'(?==== )', context)
    if len(sections) <= 1:
        # Fallback: try ## headers
        sections = re.split(r'(?=## [A-Z])', context)
    # Never drop the Plan section
    while len("".join(sections)) > max_chars and len(sections) > 1:
        # Find oldest non-plan section to drop
        dropped = False
        for i in range(len(sections)):
            if 'plan' not in sections[i].lower():
                sections.pop(i)
                dropped = True
                break
        if not dropped:
            sections.pop(0)  # all plan sections, drop oldest
    return "".join(sections)


MAX_REVISE_ATTEMPTS = int(os.environ.get("PIPELINE_MAX_REVISE", "1"))
MAX_TEST_FIX_ATTEMPTS = 1  # Single code-fix attempt after test failures
PIPELINE_WALLCLOCK_TIMEOUT = int(os.environ.get("PIPELINE_WALLCLOCK_TIMEOUT", "3600"))  # seconds, 0=no limit


def _pm_health_check(pm_cfg: dict) -> bool:
    """Quick check if PM backend config looks valid (no API call wasted)."""
    backend = pm_cfg.get("backend", "github_models")
    if backend == "github_models" and not GITHUB_TOKEN:
        log.warning("⚠️ PM health check: no GITHUB_TOKEN set")
        return False
    return True


def _clean_stage_output(output: str) -> str:
    """Strip garbage lines from stage output before appending to context."""
    lines = output.split('\n')
    clean = [l for l in lines if not any(p in l.lower() for p in _GARBAGE_PATTERNS)]
    return '\n'.join(clean)


def _extract_plan_context(context: str) -> str:
    """Extract just the plan section from pipeline context."""
    m = re.search(r'(=== Plan[^\n]*===.*?=== End plan ===)', context,
                  re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1)
    # Fallback: try ## headers
    m = re.search(r'(## Plan.*?)(?=\n## [A-Z]|\Z)', context, re.DOTALL)
    return m.group(1) if m else ""


def _build_direct_prompt(stage: str, original_prompt: str, context: str) -> str:
    """
    Build a clean prompt for a CLI agent. Keep it SHORT — verbose meta-instructions
    cause Claude Code to prompt for permissions instead of acting.

    CLI prompts are loaded from prompts.json["cli_prompts"] with inline fallbacks.
    A size guard warns if instruction chars exceed _CLI_PROMPT_WARN_CHARS.
    """
    # For plan stage — analyze codebase, output a plan.
    if stage == "plan":
        tmpl = _get_prompt("cli_prompts", "plan")
        result = tmpl.replace("{prompt}", original_prompt)
        _warn_cli_prompt_size(stage, result, len(original_prompt))
        return result

    # For code stage — task + short conventions suffix
    if stage == "code":
        prompt = original_prompt
        if context:
            clean = re.sub(r'## [A-Z]+ HANDOFF\n(?:## \w+\n)?', '', context)
            lines = [l for l in clean.strip().split('\n')
                     if not any(p in l.lower() for p in _GARBAGE_PATTERNS)]
            clean = '\n'.join(lines).strip()
            if clean and len(clean) > 50:
                prompt += f"\n\nPlan:\n{clean[-3000:]}"
        suffix = _get_prompt("cli_prompts", "code_suffix")
        result = prompt + suffix
        _warn_cli_prompt_size(stage, result, len(prompt))
        return result

    # For simplify, test, review — unified lookup through _get_prompt
    if stage in ("simplify", "test", "review"):
        tmpl = _get_prompt("cli_prompts", stage)
        result = tmpl.replace("{prompt}", original_prompt)
        _warn_cli_prompt_size(stage, result, len(original_prompt))
        return result

    # Fallback
    return original_prompt


def _test_found_failures(test_output: str) -> bool:
    """Check if test stage output indicates failures that need code fixes."""
    lower = test_output.lower()
    # Short keywords use word-boundary matching to avoid false positives
    # (e.g. "errorHandler", "assertValid", "bugfix")
    _short_failure_kws = [
        "fail", "error", "broken", "bug", "crash", "assert",
    ]
    _long_failure_patterns = [
        "exception", "traceback", "not working", "does not work", "undefined",
        "typeerror", "syntaxerror", "referenceerror", "attributeerror",
        "fix needed", "needs fix", "issue found", "issues found",
    ]
    # Don't trigger on "no failures" or "all tests passed"
    pass_patterns = [
        "no fail", "no error", "all pass", "tests pass", "0 fail",
        "0 errors", "no issues", "success", "everything passed",
        "all tests pass",
    ]
    if any(pp in lower for pp in pass_patterns):
        return False
    # Check long patterns with substring match
    if any(fp in lower for fp in _long_failure_patterns):
        return True
    # Check short keywords with word boundary
    for kw in _short_failure_kws:
        if re.search(r'\b' + kw + r'\b', lower):
            return True
    return False


def _extract_test_failures(output: str, max_chars: int = 4000) -> str:
    """Extract relevant test failure lines with context. Falls back to tail truncation."""
    lines = output.split('\n')
    failure_pats = re.compile(
        r'(fail|error|traceback|assert|exception|syntaxerror|typeerror'
        r'|referenceerror|attributeerror|not working|does not work)',
        re.IGNORECASE
    )
    relevant = []
    for i, line in enumerate(lines):
        if failure_pats.search(line):
            # Include 2 lines before and after for context
            start = max(0, i - 2)
            end = min(len(lines), i + 3)
            for j in range(start, end):
                if j not in [r[0] for r in relevant]:
                    relevant.append((j, lines[j]))
    if relevant:
        result = '\n'.join(line for _, line in sorted(relevant, key=lambda x: x[0]))
        return result[:max_chars]
    # Fallback: last max_chars of output
    return output[-max_chars:]


def _save_stage_output(task_id: str, stage: str, content: str,
                       notes: str = "") -> Path:
    """Save stage output to pipeline-output/{task_id}/{stage}.md for cross-stage reference."""
    task_dir = PIPELINE_OUTPUT_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    out_file = task_dir / f"{stage}.md"
    header = f"# Pipeline Stage: {stage}\n# Task: {task_id}\n# Saved: {datetime.now().isoformat()}\n\n"
    if notes:
        header += f"## Notes\n{notes}\n\n"
    header += f"## Output\n"
    out_file.write_text(header + content, encoding="utf-8")
    log.info("   💾 Stage '%s' output saved → %s (%d chars)", stage, out_file, len(content))
    return out_file


def _load_stage_output(task_id: str, stage: str) -> str | None:
    """Load a previously saved stage output. Returns None if not found."""
    out_file = PIPELINE_OUTPUT_DIR / task_id / f"{stage}.md"
    if out_file.exists():
        return out_file.read_text(encoding="utf-8")
    return None


def _stage_output_path(task_id: str, stage: str) -> Path:
    """Return the path where a stage's output file would be."""
    return PIPELINE_OUTPUT_DIR / task_id / f"{stage}.md"


def run_pipeline(prompt: str, task_id: str | None = None,
                 pipeline_cfg: dict | None = None,
                 start_stage: str | None = None) -> dict:
    """
    Full pipeline: rewrite → plan → code → simplify → test → review → publish.

    The Program Manager oversees the entire flow:
    - BEFORE each stage: writes a directed task brief for the team
    - AFTER each stage: verifies output quality, checks for requirement gaps
      and drift, and decides APPROVE or REVISE
    - For the CODE stage with 2+ team members: runs cross-review where each
      agent reviews the other's implementation, then PM does a deep merge
      leveraging both solutions and the cross-reviews

    start_stage: skip all stages before this one ('plan', 'code', 'test', 'review').
    Returns {"success": bool, "stage_results": {...}, "published": bool, "error": str|None}
    """
    cfg = pipeline_cfg or load_pipeline()
    stages_cfg = cfg.get("stages", {})
    pm_cfg = cfg.get("program_manager", {})
    publish_cfg = cfg.get("publish", {})

    context = ""
    stage_results: dict = {}
    stage_log: list = []          # summary entry per stage
    pipeline_start = time.time()
    _reset_pipeline_stats()
    original_prompt = prompt
    tid = task_id or f"pipeline-{int(time.time())}"
    pm_consecutive_failures = 0   # track PM failures to switch to direct mode
    test_passed = True            # Phase 1a: gate publish on test results
    code_made_changes = False     # track if code stage actually modified files

    # Phase 5e: expose pipeline start time for web UI elapsed display
    with status_lock:
        agent_status["pipeline_started"] = datetime.now(timezone.utc).isoformat()

    # ── Canary: write a marker file to prove this code ran ──────────────
    canary = AGENT_DIR / "_pipeline_canary.txt"
    canary.write_text(f"Pipeline code version: {AGENT_VERSION}\n"
                      f"Time: {datetime.now().isoformat()}\n"
                      f"Has garbage_detect: True\n"
                      f"Has direct_mode: True\n"
                      f"Has _NON_INTERACTIVE_FOOTER: True\n",
                      encoding="utf-8")
    log.info("CANARY: Pipeline code %s loaded (canary written to %s)", AGENT_VERSION, canary)

    STAGE_ORDER = ["rewrite", "plan", "code", "simplify", "test", "review"]
    skip = bool(start_stage)

    # ── PM health check — lightweight config check (no API call wasted) ──
    pm_available = _pm_health_check(pm_cfg)
    if not pm_available:
        log.warning("⚠️ PM backend unavailable — running pipeline in DIRECT mode (no PM oversight)")
    else:
        log.info("✅ PM backend config OK — will switch to DIRECT mode if API fails")

    log.info("🚀 Pipeline starting for: %s (start_stage=%s, pm=%s)",
             tid, start_stage or "rewrite", "yes" if pm_available else "DIRECT")

    for stage in STAGE_ORDER:
        if skip:
            if stage == start_stage:
                skip = False
            else:
                log.info("   ⏭️ Skipping stage '%s'", stage)
                continue

        stage_cfg = stages_cfg.get(stage, {})
        if not stage_cfg.get("enabled", True):
            log.info("   ⏭️ Stage '%s' disabled", stage)
            continue

        # Skip verification stages if code stage made no file changes
        if stage in ("simplify", "test") and not code_made_changes:
            log.info("   ⏭️ Skipping '%s' — code stage made no file changes", stage)
            stage_log.append({"stage": stage, "elapsed": 0,
                               "verdict": "skipped", "issues": [],
                               "team": [], "note": "No code changes to verify"})
            continue

        # Pipeline-level wallclock safety: abort if total time exceeds limit
        if PIPELINE_WALLCLOCK_TIMEOUT and (time.time() - pipeline_start) > PIPELINE_WALLCLOCK_TIMEOUT:
            log.error("⏰ Pipeline wallclock timeout (%ds) exceeded — aborting at stage '%s'",
                      PIPELINE_WALLCLOCK_TIMEOUT, stage)
            stage_log.append({"stage": stage, "elapsed": 0,
                               "verdict": "timeout", "issues": ["Pipeline wallclock timeout"],
                               "team": [], "note": "Aborted — pipeline ran too long"})
            break

        _t = int(os.environ.get(
            f"PIPELINE_{stage.upper()}_TIMEOUT",
            str(stage_cfg.get("timeout", 300))
        ))
        timeout = None if _t == 0 else _t
        team = stage_cfg.get("team", ["claude"])

        timeout_label = f"{timeout}s" if timeout else "no timeout"
        log.info("─" * 50)
        log.info("   ▶️ Stage: %-8s | team: %s | timeout: %s", stage, team, timeout_label)
        _stage_start = time.time()
        with status_lock:
            agent_status["state"] = f"pipeline:{stage}"
            agent_status["current_stage"] = stage
            agent_status["stage_log"] = list(stage_log)  # snapshot for web UI

        # ── Rewrite: PM-only, no CLI team ───────────────────────────────────
        if stage == "rewrite":
            if pm_available and pm_consecutive_failures < 2:
                old_prompt = prompt
                prompt = rewrite_prompt(original_prompt, pm_cfg)
                if prompt == original_prompt:
                    # rewrite_prompt returns original on failure — count as PM failure
                    pm_consecutive_failures += 1
                    log.warning("   PM rewrite failed (%d consecutive)", pm_consecutive_failures)
                else:
                    pm_consecutive_failures = 0
            else:
                log.info("   [DIRECT] Skipping PM rewrite — using original prompt")
            stage_results["rewrite"] = prompt
            _save_stage_output(tid, "rewrite", prompt,
                               notes="PM-rewritten prompt for clarity." if pm_available else "Direct mode — original prompt.")
            log.info("   Rewritten prompt (%d chars)", len(prompt))
            stage_log.append({"stage": "rewrite", "elapsed": round(time.time() - _stage_start, 1),
                               "verdict": "done", "issues": [], "team": ["pm" if pm_available else "direct"],
                               "note": prompt[:200],
                               "output": prompt[:2000],
                               "output_file": str(_stage_output_path(tid, "rewrite"))})
            with status_lock:
                agent_status["stage_log"] = list(stage_log)
            continue

        # ── Review: use structured security review, PM oversees verdict ─────
        if stage == "review":
            review = run_security_review(tid, (prompt[:80] or tid))
            team_outputs = [("security-review", review.get("report", "No report."))]
            pm_result = pm_oversee_stage(
                stage, original_prompt, context, team_outputs, pm_cfg
            )
            stage_results["review"] = pm_result["full_response"]
            _save_stage_output(tid, "review", pm_result["full_response"],
                               notes=f"Verdict: {pm_result['verdict']}")
            context += f"\n\n=== Review stage output ===\n{pm_result['handoff']}\n=== End review ==="
            context = _cap_context(context)

            if pm_result["issues"]:
                log.info("   PM flagged %d review issues: %s",
                         len(pm_result["issues"]),
                         "; ".join(pm_result["issues"][:3]))

            action = _handle_security_findings(
                review, tid, prompt[:80] or tid, [], False
            )
            stage_log.append({"stage": "review", "elapsed": round(time.time() - _stage_start, 1),
                               "verdict": "blocked" if action == "blocked" else pm_result["verdict"],
                               "issues": pm_result.get("issues", []),
                               "team": ["security-review"],
                               "note": review.get("report", "")[:200],
                               "output": review.get("report", "")[:2000],
                               "output_file": str(_stage_output_path(tid, "review"))})
            with status_lock:
                agent_status["stage_log"] = list(stage_log)
            if action == "blocked":
                log.warning("🔒 Pipeline blocked by security review for: %s", tid)
                return {
                    "success": False,
                    "stage_results": stage_results,
                    "stage_log": stage_log,
                    "published": False,
                    "error": "Blocked by security review (HIGH severity)",
                }
            continue

        # ── Plan / Code / Test ─────────────────────────────────────────────
        # Use direct mode if PM is unavailable or has failed consecutively
        use_direct = not pm_available or pm_consecutive_failures >= 2

        if use_direct:
            # ── DIRECT mode: no PM, send actionable prompts straight to CLI ──
            direct_prompt = _build_direct_prompt(stage, prompt, context)
            log.info("   [DIRECT] Built prompt for '%s' (%d chars)", stage, len(direct_prompt))
            team_outputs = run_team(stage, direct_prompt, team, "",
                                    timeout, task_id=tid)

            if not team_outputs:
                log.warning("⚠️ Stage '%s' — no team output, continuing", stage)
                stage_results[stage] = ""
            elif _is_garbage_output(team_outputs):
                # ── Garbage detected in direct mode — retry with plan context preserved ──
                log.warning("   ❌ [DIRECT] Garbage output from '%s' — retrying with plan context only", stage)
                plan_ctx = _extract_plan_context(context)
                clean_prompt = _build_direct_prompt(stage, prompt, plan_ctx)
                retry_outputs = run_team(stage, clean_prompt, team, "", timeout, task_id=tid)
                if retry_outputs and not _is_garbage_output(retry_outputs):
                    if stage in ("code", "simplify"):
                        _restart_changed_services()
                    combined = "\n\n".join(out for _, out in retry_outputs)
                    stage_results[stage] = combined
                    _save_stage_output(tid, stage, combined, notes="Direct mode — retry after garbage.")
                    clean_out = _clean_stage_output(combined)
                    context += f"\n\n=== {stage.capitalize()} stage output ===\n{clean_out[-3000:]}\n=== End {stage} ==="
                    context = _cap_context(context)
                else:
                    log.warning("   ❌ [DIRECT] Retry also garbage for '%s' — skipping", stage)
                    stage_results[stage] = ""
            else:
                if stage in ("code", "simplify"):
                    _restart_changed_services()
                combined = "\n\n".join(out for _, out in team_outputs)
                stage_results[stage] = combined
                _save_stage_output(tid, stage, combined,
                                   notes="Direct mode — no PM oversight.")
                clean_out = _clean_stage_output(combined)
                context += f"\n\n=== {stage.capitalize()} stage output ===\n{clean_out[-3000:]}\n=== End {stage} ==="
                context = _cap_context(context)

                # ── Test→Code loopback: if test found failures, re-run code ──
                if stage == "test" and _test_found_failures(combined):
                    log.warning("   🔄 Test found failures — looping back to code stage")
                    test_passed = False
                    code_team = stages_cfg.get("code", {}).get("team", ["claude"])
                    code_timeout_val = int(os.environ.get("PIPELINE_CODE_TIMEOUT",
                                          str(stages_cfg.get("code", {}).get("timeout", 300))))
                    code_timeout = None if code_timeout_val == 0 else code_timeout_val
                    test_failures = _extract_test_failures(combined)
                    fix_prompt = (
                        "The previous code changes caused test failures. "
                        "Fix ONLY the failing tests — do not rewrite unrelated code.\n\n"
                        f"Original task: {prompt}\n\n"
                        f"Test failures:\n{test_failures}"
                    )
                    fix_outputs = run_team("code", fix_prompt, code_team, "",
                                          code_timeout, task_id=tid)
                    if fix_outputs and _is_garbage_output(fix_outputs):
                        log.warning("   ❌ Code-fix produced garbage (permission request?) — discarding")
                        fix_outputs = None
                    if fix_outputs:
                        _restart_changed_services()
                        fix_combined = "\n\n".join(out for _, out in fix_outputs)
                        _save_stage_output(tid, "code-fix", fix_combined,
                                           notes="Code fix after test failures.")
                        # Re-check if fix resolved the issues
                        test_passed = not _test_found_failures(fix_combined)
                        context += f"\n\n=== Code fix output ===\n{fix_combined[-2000:]}\n=== End code fix ==="
                        context = _cap_context(context)
                        stage_log.append({"stage": "code-fix", "elapsed": 0,
                                          "verdict": "direct", "issues": [],
                                          "team": code_team,
                                          "note": fix_combined[:200],
                                          "output": fix_combined[:2000],
                                          "output_file": str(_stage_output_path(tid, "code-fix"))})
                        with status_lock:
                            agent_status["stage_log"] = list(stage_log)

            # After code stage, check if files actually changed
            if stage == "code":
                code_made_changes = _has_uncommitted_changes()
                if not code_made_changes:
                    log.warning("   ⚠️ Code stage produced output but no file changes detected")

            elapsed = time.time() - _stage_start
            log.info("   ✅ Stage '%s' done in %.0fs — DIRECT", stage, elapsed)
            stage_log.append({"stage": stage, "elapsed": round(elapsed, 1),
                               "verdict": "direct", "issues": [], "team": team,
                               "note": (stage_results.get(stage) or "")[:200],
                               "output": (stage_results.get(stage) or "")[:2000],
                               "output_file": str(_stage_output_path(tid, stage))})
            with status_lock:
                agent_status["stage_log"] = list(stage_log)
            continue

        # ── Full PM mode: PM directs → team runs → PM oversees ───────────
        for attempt in range(1 + MAX_REVISE_ATTEMPTS):
            directed_prompt, pm_ok = pm_direct_team(stage, prompt, context, team, pm_cfg)
            if not pm_ok:
                pm_consecutive_failures += 1
                log.warning("   PM failed (%d consecutive) — prompt is direct-mode fallback",
                            pm_consecutive_failures)
            else:
                pm_consecutive_failures = 0

            # Don't pass raw context separately — it's already in the directed prompt
            team_outputs = run_team(stage, directed_prompt, team, "",
                                    timeout, task_id=tid)

            if not team_outputs:
                log.warning("⚠️ Stage '%s' — no team output, continuing", stage)
                stage_results[stage] = ""
                break

            # After code/simplify stage, restart any changed services
            if stage in ("code", "simplify"):
                _restart_changed_services()

            # ── Code stage with 2+ agents: cross-review + deep merge ────────
            if stage == "code" and len(team_outputs) >= 2:
                # Save each agent's output separately
                for agent_name, agent_output in team_outputs:
                    _save_stage_output(tid, f"code-{agent_name}", agent_output,
                                       notes=f"Individual implementation by {agent_name}")
                cross_reviews = cross_review_code(
                    team_outputs, context, original_prompt, timeout
                )
                if cross_reviews:
                    pm_result = pm_merge_with_reviews(
                        stage, original_prompt, context,
                        team_outputs, cross_reviews, pm_cfg
                    )
                    pm_result["team_outputs"] = team_outputs
                    pm_result["cross_reviews"] = cross_reviews
                    pm_result["comparison_summary"] = _build_comparison_summary(
                        team_outputs, cross_reviews)
                else:
                    pm_result = pm_oversee_stage(
                        stage, original_prompt, context, team_outputs, pm_cfg
                    )
            else:
                # ── Standard oversight for plan/test or single-agent code ───
                pm_result = pm_oversee_stage(
                    stage, original_prompt, context, team_outputs, pm_cfg
                )

            # Track PM API success
            if not pm_result.get("pm_succeeded", True):
                pm_consecutive_failures += 1
                log.warning("   PM oversight failed (%d consecutive)", pm_consecutive_failures)
            else:
                pm_consecutive_failures = 0

            # ── PM quality gate ─────────────────────────────────────────────
            verdict = pm_result["verdict"]
            issues = pm_result["issues"]

            if issues:
                log.info("   PM flagged %d issues in '%s': %s",
                         len(issues), stage, "; ".join(issues[:3]))

            if verdict == "revise" and attempt < MAX_REVISE_ATTEMPTS:
                log.warning("   🔄 PM verdict: REVISE (attempt %d/%d) — re-running stage '%s'",
                            attempt + 1, MAX_REVISE_ATTEMPTS, stage)
                issues_text = "\n".join(f"- {iss}" for iss in issues)
                context += (
                    f"\n\nThe previous {stage} attempt was rejected. "
                    f"Issues found:\n{issues_text}\n\n"
                    f"Guidance for retry:\n{pm_result['handoff']}"
                )
                context = _cap_context(context)
                continue  # retry the stage

            if verdict == "revise":
                log.warning("   ⚠️ PM verdict: REVISE but max attempts reached — proceeding with best effort")
                # Safety net: if stage output is garbage after all retries, don't
                # feed it forward — use original prompt as context instead of garbage.
                if _is_garbage_output(team_outputs):
                    log.warning("   🛡️ Stage '%s' output is garbage after all retries — "
                                "dropping garbage from context, using original prompt", stage)
                    pm_result["handoff"] = f"Stage {stage} produced no usable output. Original task: {prompt}"
                    pm_result["synthesis"] = pm_result["handoff"]

            # Track test results for publish gating
            if stage == "test":
                test_passed = verdict != "revise"

            # After code stage, check if files actually changed
            if stage == "code":
                code_made_changes = _has_uncommitted_changes()
                if not code_made_changes:
                    log.warning("   ⚠️ Code stage produced output but no file changes detected")

            # Approved (or max attempts reached) — record and move on
            stage_results[stage] = pm_result["full_response"]
            _save_stage_output(tid, stage, pm_result["full_response"],
                               notes=f"Verdict: {verdict} | Issues: {len(issues)}")
            clean_handoff = _clean_stage_output(pm_result['handoff'])
            context += f"\n\n=== {stage.capitalize()} stage output ===\n{clean_handoff}\n=== End {stage} ==="
            context = _cap_context(context)
            elapsed = time.time() - _stage_start
            log.info("   ✅ Stage '%s' done in %.0fs — PM: %s", stage, elapsed, verdict.upper())
            stage_log.append({"stage": stage, "elapsed": round(elapsed, 1),
                               "verdict": verdict, "issues": issues, "team": team,
                               "note": pm_result.get("handoff", "")[:200],
                               "output": pm_result.get("synthesis", "")[:2000],
                               "output_file": str(_stage_output_path(tid, stage))})
            with status_lock:
                agent_status["stage_log"] = list(stage_log)
            break

    # ── Publish ──────────────────────────────────────────────────────────────
    published = False
    if not test_passed:
        log.warning("⛔ Skipping publish — test stage indicated failures")
    elif publish_cfg.get("enabled", True) and publish_cfg.get("auto_push", True):
        title = prompt[:80] if not task_id else task_id
        log.info("📤 Publishing: %s", title)
        published = _git_commit_and_push(tid, title, label="pipeline")

    with status_lock:
        agent_status["state"] = "idle"
        agent_status["stage_log"] = []
        agent_status.pop("pipeline_started", None)

    # ── Pipeline stats summary ──────────────────────────────────────────────
    stats = _get_pipeline_stats_summary()
    total_cli = sum(s.get("cli_calls", 0) for s in stats.values())
    total_sub = sum(s.get("subagents", 0) for s in stats.values())
    log.info("📊 Pipeline stats: %d CLI calls, %d subagents spawned", total_cli, total_sub)
    for stage_name, sdata in stats.items():
        tools_str = ", ".join(f"{t}={c}" for t, c in sorted(sdata.get("tool_calls", {}).items()))
        log.info("   %-10s: %d CLI calls, %d subagents%s",
                 stage_name, sdata["cli_calls"], sdata["subagents"],
                 f" | tools: {tools_str}" if tools_str else "")

    log.info("✅ Pipeline complete for: %s (published=%s)", tid, published)
    return {"success": True, "stage_results": stage_results, "stage_log": stage_log,
            "pipeline_elapsed": round(time.time() - pipeline_start, 1),
            "published": published, "error": None, "stats": stats}


# ═══════════════════════════════════════════════════════════════════════════
#  THREADING / STATUS
# ═══════════════════════════════════════════════════════════════════════════

trigger_event = threading.Event()
agent_status = {
    "state": "starting",
    "current_task": None,
    "current_stage": None,
    "last_run": None,
    "last_trigger": None,
    "tasks_pending": 0,
    "ideas_pending": 0,
    "api_calls_today": 0,
    "api_limit": MAX_API_CALLS_PER_DAY,
}
status_lock = threading.Lock()

research_jobs: dict = {}
research_lock = threading.Lock()

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(
            open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        ),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("task-claw")


# ═══════════════════════════════════════════════════════════════════════════
#  HTTP TRIGGER SERVER
# ═══════════════════════════════════════════════════════════════════════════

class TriggerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("HTTP: " + fmt % args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, file_path: Path, allowed_root_resolved: str):
        """Serve a static file with path-traversal protection."""
        try:
            resolved = file_path.resolve()
            if not str(resolved).startswith(allowed_root_resolved):
                self._json(403, {"ok": False, "error": "Forbidden"})
                return
            if not resolved.is_file():
                self._json(404, {"ok": False, "error": "Not found"})
                return
            content_type, _ = mimetypes.guess_type(str(resolved))
            content_type = content_type or "application/octet-stream"
            data = resolved.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path.rstrip("/") == "/trigger":
            body = self._read_body()
            if body is None:
                return
            prompt = body.get("prompt") if body else None
            if prompt:
                log.info("🚨 Trigger with prompt — launching pipeline: %s", prompt[:100])
                threading.Thread(
                    target=run_pipeline, args=(prompt,), daemon=True
                ).start()
                self._json(200, {"ok": True, "message": "Pipeline started!",
                                 "prompt": prompt[:100]})
                return
            force = body.get("force") if body else False
            trigger_event.set()
            with status_lock:
                agent_status["last_trigger"] = datetime.now().isoformat()
                if force:
                    agent_status["force_no_age_filter"] = True
            log.info("🚨 Manual trigger received — waking agent!%s", " (force=no age filter)" if force else "")
            self._json(200, {"ok": True, "message": "Agent triggered!"})

        elif self.path.startswith("/implement/"):
            resource_id = self.path.split("/implement/", 1)[1].rstrip("/")
            if not resource_id:
                self._json(400, {"ok": False, "error": "Missing task/idea ID"})
                return
            tasks = load_tasks()
            ideas = load_ideas()
            state = load_state()
            target, is_idea = _find_item(resource_id, tasks, ideas)
            if not target:
                self._json(404, {"ok": False, "error": f"Task/idea {resource_id} not found"})
                return
            if target.get("status") != "planned":
                self._json(400, {"ok": False, "error": f"Not in 'planned' state (current: {target.get('status')})"})
                return
            if not target.get("plan"):
                self._json(400, {"ok": False, "error": "No plan found"})
                return
            coll = ideas if is_idea else tasks
            threading.Thread(
                target=_implement_planned_task,
                args=(resource_id, target, coll, is_idea, state),
                daemon=True,
            ).start()
            self._json(200, {"ok": True, "message": f"Implementation started for {target.get('title', resource_id)}!"})

        elif self.path.rstrip("/") == "/research":
            body = self._read_body()
            if body is None:
                return
            idea_id, title, desc = body.get("id"), body.get("title", ""), body.get("description", "")
            if not idea_id or not title:
                self._json(400, {"ok": False, "error": "Missing id or title"})
                return
            with research_lock:
                if research_jobs.get(idea_id, {}).get("status") == "researching":
                    self._json(409, {"ok": False, "error": "Research already in progress"})
                    return
            ideas = load_ideas()
            for idea in ideas:
                if idea.get("id") == idea_id:
                    idea["research_status"] = "researching"
                    idea["updated"] = _ts_ms()
                    break
            save_ideas(ideas)
            threading.Thread(target=run_research, args=(idea_id, title, desc), daemon=True).start()
            log.info("🔬 Research triggered for idea: %s", title)
            self._json(200, {"ok": True, "message": "Research started!"})

        elif self.path.rstrip("/") == "/api/tasks":
            self._create_item(load_tasks, save_tasks)

        elif self.path.rstrip("/") == "/api/ideas":
            self._create_item(load_ideas, save_ideas)

        elif self.path.rstrip("/") == "/api/photos/upload":
            self._handle_photo_upload()

        elif self.path.rstrip("/") == "/api/skills":
            body = self._read_body()
            if body is None:
                return
            skill_id = body.get("id") or generateId()
            data = _load_skills()
            skills = data.get("skills", {})
            skills[skill_id] = {
                "name": body.get("name", skill_id),
                "description": body.get("description", ""),
                "prompt": body.get("prompt", ""),
                "provider": body.get("provider"),
                "timeout": body.get("timeout", 300),
                "phase": body.get("phase", "implement"),
                "tags": body.get("tags", []),
            }
            data["skills"] = skills
            _save_skills(data)
            self._json(201, {"ok": True, "id": skill_id})

        elif self.path.startswith("/api/skills/") and self.path.rstrip("/").endswith("/run"):
            parts = self.path.split("/api/skills/", 1)[1].rstrip("/")
            skill_id = parts.rsplit("/run", 1)[0]
            body = self._read_body()
            if body is None:
                return
            input_text = body.get("input", "")
            provider_override = body.get("provider")
            threading.Thread(
                target=run_skill,
                args=(skill_id, input_text, provider_override),
                daemon=True,
            ).start()
            self._json(200, {"ok": True, "message": f"Skill '{skill_id}' started"})

        else:
            self._json(404, {"ok": False, "error": "Not found"})

    def do_PUT(self):
        if self.path.startswith("/api/tasks/"):
            item_id = self.path.split("/api/tasks/", 1)[1].rstrip("/")
            self._update_item(item_id, load_tasks, save_tasks, "Task")

        elif self.path.startswith("/api/ideas/"):
            item_id = self.path.split("/api/ideas/", 1)[1].rstrip("/")
            self._update_item(item_id, load_ideas, save_ideas, "Idea")

        elif self.path.rstrip("/") == "/api/config/pipeline":
            body = self._read_body()
            if body is None:
                return
            pipeline_file = Path(os.environ.get("PIPELINE_FILE", str(AGENT_DIR / "pipeline.json")))
            pipeline_file.write_text(json.dumps(body, indent=2), encoding="utf-8")
            self._json(200, {"ok": True})

        elif self.path.rstrip("/") == "/api/config/providers":
            body = self._read_body()
            if body is None:
                return
            PROVIDERS_FILE.write_text(json.dumps(body, indent=2), encoding="utf-8")
            self._json(200, {"ok": True})

        elif self.path.startswith("/api/skills/"):
            skill_id = self.path.split("/api/skills/", 1)[1].rstrip("/")
            body = self._read_body()
            if body is None:
                return
            data = _load_skills()
            skills = data.get("skills", {})
            if skill_id not in skills:
                self._json(404, {"ok": False, "error": "Skill not found"})
                return
            skills[skill_id].update({
                k: body[k] for k in ("name", "description", "prompt", "provider",
                                      "timeout", "phase", "tags")
                if k in body
            })
            data["skills"] = skills
            _save_skills(data)
            self._json(200, {"ok": True})

        else:
            self._json(404, {"ok": False, "error": "Not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/tasks/"):
            item_id = self.path.split("/api/tasks/", 1)[1].rstrip("/")
            self._delete_item(item_id, load_tasks, save_tasks)

        elif self.path.startswith("/api/ideas/"):
            item_id = self.path.split("/api/ideas/", 1)[1].rstrip("/")
            self._delete_item(item_id, load_ideas, save_ideas)

        elif self.path.startswith("/api/skills/"):
            skill_id = self.path.split("/api/skills/", 1)[1].rstrip("/")
            data = _load_skills()
            skills = data.get("skills", {})
            if skill_id in skills:
                del skills[skill_id]
                data["skills"] = skills
                _save_skills(data)
            self._json(200, {"ok": True})

        elif self.path.startswith("/api/photos/"):
            filename = self.path.split("/api/photos/", 1)[1].rstrip("/")
            photo_path = (PHOTOS_DIR / filename).resolve()
            if not str(photo_path).startswith(_PHOTOS_DIR_RESOLVED):
                self._json(403, {"ok": False, "error": "Forbidden"})
                return
            if photo_path.exists():
                photo_path.unlink()
            self._json(200, {"ok": True})

        else:
            self._json(404, {"ok": False, "error": "Not found"})

    def do_GET(self):
        path = self.path.split("?")[0]  # strip query params

        # ── Static file serving ─────────────────────────────────────────
        if path in ("/", "/index.html"):
            self._serve_file(WEB_DIR / "index.html", _WEB_DIR_RESOLVED)
            return
        if path == "/pipeline.html":
            self._serve_file(WEB_DIR / "pipeline.html", _WEB_DIR_RESOLVED)
            return
        if path.startswith("/css/") or path.startswith("/js/"):
            self._serve_file(WEB_DIR / path.lstrip("/"), _WEB_DIR_RESOLVED)
            return
        if path.startswith("/photos/"):
            self._serve_file(PHOTOS_DIR / path.split("/photos/", 1)[1], _PHOTOS_DIR_RESOLVED)
            return

        # ── API endpoints ───────────────────────────────────────────────
        if path.rstrip("/") == "/status":
            with status_lock:
                snap = dict(agent_status)
            snap["version"] = AGENT_VERSION
            providers_cfg = _load_providers()
            snap["providers"] = {k: v.get("name", k) for k, v in providers_cfg.get("providers", {}).items()}
            snap["default_provider"] = os.environ.get("CLI_PROVIDER",
                                        providers_cfg.get("default_provider", "claude"))
            snap["default_planning_backend"] = DEFAULT_PLANNING_BACKEND
            snap["session_dir"] = str(SESSION_DIR)
            pipeline_cfg = load_pipeline()
            snap["pipeline_stages"] = {
                name: {
                    "enabled": cfg.get("enabled", True),
                    "team":    cfg.get("team", ["claude"]),
                    "timeout": cfg.get("timeout", 300),
                }
                for name, cfg in pipeline_cfg.get("stages", {}).items()
            }
            snap["pipeline_pm_backend"] = (
                pipeline_cfg.get("program_manager", {}).get("backend", "github_models")
            )
            self._json(200, snap)

        elif path.rstrip("/") == "/api/tasks":
            self._json(200, load_tasks())

        elif path.rstrip("/") == "/api/ideas":
            self._json(200, load_ideas())

        elif path.rstrip("/") == "/api/pipeline-history":
            runs = []
            if PIPELINE_OUTPUT_DIR.exists():
                dirs = []
                for d in PIPELINE_OUTPUT_DIR.iterdir():
                    if d.is_dir():
                        dirs.append((d, d.stat().st_mtime))
                dirs.sort(key=lambda x: x[1], reverse=True)
                for d, mtime in dirs[:50]:  # limit to most recent 50
                    runs.append({
                        "task_id": d.name,
                        "timestamp": datetime.fromtimestamp(mtime).isoformat(),
                        "stages": [f.stem for f in sorted(d.glob("*.md")) if not f.name.startswith(".")],
                    })
            self._json(200, {"runs": runs})

        elif path.rstrip("/") == "/api/pipeline-stats":
            self._json(200, {"stats": _get_pipeline_stats_summary()})

        elif path.rstrip("/") == "/api/config/pipeline":
            self._json(200, load_pipeline())

        elif path.rstrip("/") == "/api/config/providers":
            self._json(200, _load_providers())

        elif path.rstrip("/") == "/api/skills":
            all_skills = get_all_skills()
            result = []
            for sid, skill in all_skills.items():
                entry = dict(skill)
                entry["id"] = sid
                result.append(entry)
            self._json(200, {"skills": result})

        elif path.startswith("/api/skills/") and path.rstrip("/").endswith("/runs"):
            skill_id = path.split("/api/skills/", 1)[1].rstrip("/").rsplit("/runs", 1)[0]
            with _skills_lock:
                runs = [
                    dict(r, run_id=rid)
                    for rid, r in _skill_runs.items()
                    if r.get("skill_id") == skill_id
                ]
            self._json(200, {"runs": runs})

        elif path.startswith("/skill-output/"):
            run_id = path.split("/skill-output/", 1)[1].rstrip("/")
            out_dir = SKILLS_OUTPUT_DIR / run_id
            out_file = out_dir / "output.md"
            if out_file.exists():
                self._json(200, {"ok": True, "run_id": run_id,
                                 "output": out_file.read_text(encoding="utf-8")})
            else:
                with _skills_lock:
                    run = _skill_runs.get(run_id)
                if run:
                    self._json(200, {"ok": True, "run_id": run_id,
                                     "status": run.get("status", "unknown"),
                                     "output": run.get("output", "")})
                else:
                    self._json(404, {"ok": False, "error": "Skill run not found"})

        elif path.startswith("/research-status/"):
            idea_id = path.split("/research-status/", 1)[1].rstrip("/")
            with research_lock:
                job = research_jobs.get(idea_id, {"status": "idle", "result": None})
            self._json(200, job)

        elif path.startswith("/pipeline-output/"):
            parts = path.split("/pipeline-output/", 1)[1].rstrip("/").split("/", 1)
            task_id = parts[0]
            stage = parts[1] if len(parts) > 1 else None
            task_dir = PIPELINE_OUTPUT_DIR / task_id
            if not task_dir.exists():
                self._json(404, {"ok": False, "error": "No pipeline output for this task"})
            elif stage:
                out_file = task_dir / f"{stage}.md"
                if out_file.exists():
                    self._json(200, {"ok": True, "stage": stage,
                                     "content": out_file.read_text(encoding="utf-8")})
                else:
                    self._json(404, {"ok": False, "error": f"No output for stage '{stage}'"})
            else:
                files = {}
                for f in sorted(task_dir.glob("*.md")):
                    if not f.name.startswith("."):
                        files[f.stem] = f.read_text(encoding="utf-8")
                self._json(200, {"ok": True, "task_id": task_id, "stages": files})

        elif path.startswith("/security-report/"):
            task_id = path.split("/security-report/", 1)[1].rstrip("/")
            report_text = None
            for candidate in SECURITY_REVIEW_DIR.iterdir():
                if candidate.stem.startswith(task_id) or task_id in candidate.stem:
                    try:
                        report_text = candidate.read_text(encoding="utf-8")
                    except Exception:
                        pass
                    break
            if report_text is not None:
                self._json(200, {"ok": True, "report": report_text})
            else:
                self._json(404, {"ok": False, "error": "No security report found for this task"})

        else:
            self._json(404, {"ok": False, "error": "Not found"})

    # ── CRUD helpers ──────────────────────────────────────────────────

    def _create_item(self, loader, saver):
        """Create a new task or idea."""
        body = self._read_body()
        if body is None:
            return
        body["id"] = body.get("id") or generateId()
        body.setdefault("created", _ts_ms())
        body.setdefault("updated", _ts_ms())
        body.setdefault("status", "open")
        items = loader()
        items.insert(0, body)
        saver(items)
        self._json(201, body)

    def _update_item(self, item_id: str, loader, saver, label: str):
        """Update a task or idea by ID."""
        body = self._read_body()
        if body is None:
            return
        items = loader()
        for item in items:
            if item.get("id") == item_id:
                item.update(body)
                item["updated"] = _ts_ms()
                saver(items)
                self._json(200, {"ok": True})
                return
        self._json(404, {"ok": False, "error": f"{label} not found"})

    def _delete_item(self, item_id: str, loader, saver):
        """Delete a task or idea by ID."""
        items = loader()
        filtered = [i for i in items if i.get("id") != item_id]
        if len(filtered) < len(items):
            saver(filtered)
        self._json(200, {"ok": True})

    def _read_body(self) -> dict | None:
        cl = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(cl) if cl else b""
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            self._json(400, {"ok": False, "error": "Invalid JSON"})
            return None

    def _handle_photo_upload(self):
        """Parse multipart form data and save uploaded photo."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json(400, {"ok": False, "error": "Expected multipart/form-data"})
            return
        cl = int(self.headers.get("Content-Length", 0))
        if cl <= 0 or cl > 10 * 1024 * 1024:  # 10MB limit
            self._json(400, {"ok": False, "error": "Invalid content length (max 10MB)"})
            return
        raw = self.rfile.read(cl)
        # Extract boundary from Content-Type
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part.split("=", 1)[1].strip().strip('"')
                break
        if not boundary:
            self._json(400, {"ok": False, "error": "No boundary in multipart"})
            return
        # Split on boundary and find the file part
        boundary_bytes = b"--" + boundary.encode()
        parts = raw.split(boundary_bytes)
        for part in parts:
            if b"filename=" not in part:
                continue
            # Split headers from body
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            headers_raw = part[:header_end].decode("utf-8", errors="replace")
            file_data = part[header_end + 4:]
            # Remove trailing \r\n-- if present
            if file_data.endswith(b"\r\n"):
                file_data = file_data[:-2]
            if file_data.endswith(b"--"):
                file_data = file_data[:-2]
            if file_data.endswith(b"\r\n"):
                file_data = file_data[:-2]
            # Extract original filename for extension
            ext = ".jpg"
            for h in headers_raw.split("\r\n"):
                if "filename=" in h:
                    fname = h.split("filename=", 1)[1].strip().strip('"')
                    if "." in fname:
                        ext = "." + fname.rsplit(".", 1)[1].lower()
                    break
            # Save with uuid prefix
            safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
            (PHOTOS_DIR / safe_name).write_bytes(file_data)
            self._json(200, {"ok": True, "filename": safe_name})
            return
        self._json(400, {"ok": False, "error": "No file found in upload"})


def generateId() -> str:
    """Generate a unique ID for tasks/ideas."""
    return f"{int(time.time() * 1000):x}-{uuid.uuid4().hex[:6]}"


def start_trigger_server():
    try:
        srv = HTTPServer(("0.0.0.0", TRIGGER_PORT), TriggerHandler)
        srv.timeout = 1
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("🔔 Trigger server listening on port %d", TRIGGER_PORT)
    except Exception as e:
        log.warning("⚠️ Could not start trigger server on port %d: %s", TRIGGER_PORT, e)


def interruptible_sleep(seconds: float):
    trigger_event.wait(timeout=seconds)
    triggered = trigger_event.is_set()
    trigger_event.clear()
    return triggered


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _ts_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def _find_item(item_id: str, tasks: list, ideas: list) -> tuple[dict | None, bool]:
    for t in tasks:
        if t.get("id") == item_id:
            return t, False
    for i in ideas:
        if i.get("id") == item_id:
            return i, True
    return None, False


# ── State I/O ───────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            today = datetime.now().strftime("%Y-%m-%d")
            if state.get("api_date") != today:
                state["api_calls_today"] = 0
                state["api_date"] = today
            return state
        except Exception:
            pass
    return {"processed": [], "api_calls_today": 0, "api_date": datetime.now().strftime("%Y-%m-%d")}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── Tasks / Ideas I/O ──────────────────────────────────────────────────────
def load_tasks() -> list:
    if not TASKS_FILE.exists():
        return []
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not read tasks file: %s", e)
        return []

def save_tasks(tasks: list):
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks, indent=2), encoding="utf-8")

def load_ideas() -> list:
    if not IDEAS_FILE.exists():
        return []
    try:
        return json.loads(IDEAS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not read ideas file: %s", e)
        return []

def save_ideas(ideas: list):
    IDEAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    IDEAS_FILE.write_text(json.dumps(ideas, indent=2), encoding="utf-8")


# ── Status helpers ──────────────────────────────────────────────────────────
def _update_task_status(task_id: str, tasks: list, status: str, note: str):
    for t in tasks:
        if t.get("id") == task_id:
            t["status"] = status
            t["updated"] = _ts_ms()
            if note:
                t["ai_analysis"] = t.get("ai_analysis", "") + f"\n\n---\n**Agent note:** {note}"
            break
    save_tasks(tasks)

def _update_idea_status(idea_id: str, ideas: list, status: str, note: str):
    for i in ideas:
        if i.get("id") == idea_id:
            i["status"] = status
            i["updated"] = _ts_ms()
            if note:
                existing = i.get("plan", "") or ""
                i["plan"] = (existing + f"\n\n---\n**Agent note:** {note}") if existing else f"**Agent note:** {note}"
            break
    save_ideas(ideas)


# ═══════════════════════════════════════════════════════════════════════════
#  GPT-4o via GitHub Models
# ═══════════════════════════════════════════════════════════════════════════

def call_gpt4o(system_prompt: str, user_prompt: str, state: dict) -> str | None:
    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN not set — cannot call GPT-4o")
        return None
    if state.get("api_calls_today", 0) >= MAX_API_CALLS_PER_DAY:
        log.warning("⚠️ Daily API limit reached (%d/%d)", state["api_calls_today"], MAX_API_CALLS_PER_DAY)
        return None
    try:
        resp = requests.post(
            GITHUB_MODELS_URL,
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"},
            json={
                "model": GITHUB_MODELS_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 1024,
            },
            timeout=30,
        )
        resp.raise_for_status()
        state["api_calls_today"] = state.get("api_calls_today", 0) + 1
        save_state(state)
        log.info("📊 API calls today: %d/%d", state["api_calls_today"], MAX_API_CALLS_PER_DAY)
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error("GPT-4o call failed: %s", e)
        return None



# ═══════════════════════════════════════════════════════════════════════════
#  RESEARCH RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def run_research(idea_id: str, title: str, description: str):
    output_file = RESEARCH_DIR / f"{idea_id}.md"
    prompt = (
        f"Research the following idea comprehensively. "
        f"Search the web for relevant APIs, tools, libraries, frameworks, and approaches. "
        f"Search GitHub for reference implementations and examples. "
        f"Provide a detailed research report with: "
        f"1) Overview of available solutions, "
        f"2) Recommended tools/APIs with links, "
        f"3) Implementation approaches, "
        f"4) Potential challenges and considerations. "
        f"\n\nIdea: {title}\n\nDetails: {description or 'No additional details provided.'}"
    )

    log.info("🔬 Starting research for idea: %s", title)
    with research_lock:
        research_jobs[idea_id] = {"status": "researching", "result": None}

    try:
        provider = get_provider_for_phase("implement")
        cmd = build_cli_command(provider, "implement", prompt)
        timeout = get_timeout(provider, "implement")

        result = subprocess.run(
            cmd, cwd=str(PROJECT_DIR),
            capture_output=True, text=True, timeout=timeout,
            env=_clean_env(), encoding="utf-8", errors="replace",
        )
        text = (output_file.read_text(encoding="utf-8") if output_file.exists()
                else result.stdout.strip() if result.stdout else None)
        if text:
            ideas = load_ideas()
            for idea in ideas:
                if idea.get("id") == idea_id:
                    idea["research"] = text
                    idea["research_status"] = "done"
                    idea["researched_at"] = datetime.now().isoformat()
                    idea["updated"] = _ts_ms()
                    break
            save_ideas(ideas)
            with research_lock:
                research_jobs[idea_id] = {"status": "done", "result": text}
            log.info("✅ Research complete for idea: %s", title)
        else:
            log.warning("⚠️ Research produced no output for: %s", title)
            with research_lock:
                research_jobs[idea_id] = {"status": "error", "result": "No output."}
            ideas = load_ideas()
            for idea in ideas:
                if idea.get("id") == idea_id:
                    idea["research_status"] = "error"
                    idea["updated"] = _ts_ms()
                    break
            save_ideas(ideas)
    except subprocess.TimeoutExpired:
        log.error("⏰ Research timed out for: %s", title)
        with research_lock:
            research_jobs[idea_id] = {"status": "error", "result": "Timed out."}
    except Exception as e:
        log.error("❌ Research failed for %s: %s", title, e)
        with research_lock:
            research_jobs[idea_id] = {"status": "error", "result": str(e)}
    finally:
        try:
            if output_file.exists():
                output_file.unlink()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
#  CLI LAUNCH — IMPLEMENT
# ═══════════════════════════════════════════════════════════════════════════

def launch_cli_implement(prompt: str, task_id: str, tasks: list,
                         is_idea: bool = False, plan_path: str | None = None,
                         provider_override: str | None = None) -> bool:
    """Run a single CLI implement step. Security review and push are handled by run_pipeline."""
    provider = get_provider_for_phase("implement", provider_override)
    label = "idea" if is_idea else "task"

    log.info("🚀 Running %s (implement) for %s %s…",
             provider.get("name", "CLI"), label, task_id)
    log.info("   Prompt: %s", prompt[:200])

    if plan_path and Path(plan_path).exists():
        safe_prompt = f"Implement the plan at {plan_path}. {prompt}".replace('"', "'")
    else:
        safe_prompt = prompt.replace('"', "'")

    success, output = run_cli_command(provider, "implement", safe_prompt)

    if not success:
        log.warning("⚠️ CLI exited with error for %s %s", label, task_id)
        err_snippet = output[-300:] if output else "no output"
        if is_idea:
            _update_idea_status(task_id, tasks, "open",
                f"CLI failed. Needs manual attention.\n{err_snippet}")
        else:
            _update_task_status(task_id, tasks, "open",
                f"CLI failed. Needs manual attention.\n{err_snippet}")
        return False

    _restart_changed_services()
    log.info("✅ CLI completed successfully for %s", task_id)
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  SECURITY REVIEW
# ═══════════════════════════════════════════════════════════════════════════

def run_security_review(task_id: str, title: str) -> dict:
    """Spin up a fresh CLI to audit staged/unstaged changes for security issues."""
    log.info("🔒 Running security review for: %s", title)

    try:
        diff_text = ""
        for diff_cmd in [["git", "diff", "--cached"], ["git", "diff"]]:
            r = subprocess.run(diff_cmd, cwd=str(PROJECT_DIR),
                               capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
            if r.stdout and r.stdout.strip():
                diff_text = r.stdout.strip()
                break
        if not diff_text:
            log.info("🔒 No diff to review — skipping")
            return {"passed": True, "severity": "none", "findings": [], "report": "No changes."}
    except Exception as e:
        log.warning("⚠️ Could not get diff: %s", e)
        return {"passed": True, "severity": "none", "findings": [], "report": str(e)}

    if len(diff_text) > 15000:
        diff_text = diff_text[:15000] + "\n\n... (truncated)"

    review_prompt = (
        "You are a security auditor. Review this git diff.\n\n"
        "RESPOND ONLY WITH VALID JSON — no markdown, no code fences.\n\n"
        "Check for:\n"
        "1. Hardcoded secrets, API keys, tokens, passwords\n"
        "2. Exposed IP addresses or internal network details\n"
        "3. Insecure HTTP endpoints (missing auth, CORS wildcards)\n"
        "4. Dangerous shell commands or code injection vectors\n"
        "5. Known vulnerable libraries or insecure dependency versions\n"
        "6. Overly permissive file/network permissions\n"
        "7. Secrets logged to console or files\n\n"
        "Rate each: low / medium / high.\n\n"
        'Return JSON: {"passed": true/false, "severity": "none"/"low"/"medium"/"high", '
        '"findings": [{"severity": "...", "file": "...", "line": "...", "issue": "...", "fix": "..."}]}\n\n'
        f"DIFF:\n{diff_text}"
    )

    try:
        provider = get_provider_for_phase("security")
        cmd = build_cli_command(provider, "security", review_prompt)
        timeout = get_timeout(provider, "security")

        result = subprocess.run(cmd, cwd=str(PROJECT_DIR),
                                capture_output=True, text=True, timeout=timeout,
                                env=_clean_env(), encoding="utf-8", errors="replace")

        log.info("🔒 Security review exit code: %d", result.returncode)
        output = result.stdout.strip() if result.stdout else ""

        if not output:
            log.warning("⚠️ Security review produced no output")
            return {"passed": True, "severity": "none", "findings": [], "report": "No output."}

        review_data = _parse_security_json(output)
        review_file = SECURITY_REVIEW_DIR / f"{task_id}-review.json"

        if review_data:
            review_data["report"] = output
            review_file.write_text(json.dumps(review_data, indent=2), encoding="utf-8")
            log.info("🔒 Result: severity=%s, findings=%d, passed=%s",
                     review_data.get("severity", "none"),
                     len(review_data.get("findings", [])),
                     review_data.get("passed", True))
            for f in review_data.get("findings", []):
                log.info("   🔍 [%s] %s — %s",
                         f.get("severity", "?").upper(), f.get("file", "?"), f.get("issue", "?"))
            return review_data
        else:
            log.warning("⚠️ Could not parse security JSON — treating as passed")
            review_file.write_text(output, encoding="utf-8")
            return {"passed": True, "severity": "none", "findings": [], "report": output}

    except subprocess.TimeoutExpired:
        log.warning("⏰ Security review timed out — allowing push")
        return {"passed": True, "severity": "none", "findings": [], "report": "Timed out."}
    except Exception as e:
        log.warning("⚠️ Security review error: %s — allowing push", e)
        return {"passed": True, "severity": "none", "findings": [], "report": str(e)}


def _parse_security_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in [
        r'```json\s*\n(.*?)\n\s*```',
        r'```\s*\n(.*?)\n\s*```',
        r'(\{[^{}]*"passed"[^{}]*"findings"[^{}]*\[.*?\]\s*\})',
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def _handle_security_findings(review: dict, task_id: str, title: str,
                              collection: list, is_idea: bool) -> str:
    """Returns 'publish' | 'fixed' | 'blocked'."""
    severity = review.get("severity", "none")
    findings = review.get("findings", [])
    label = "idea" if is_idea else "task"

    if severity == "none" or not findings:
        log.info("🔒✅ No security issues — clear to push")
        return "publish"

    high   = [f for f in findings if f.get("severity") == "high"]
    medium = [f for f in findings if f.get("severity") == "medium"]
    low    = [f for f in findings if f.get("severity") == "low"]

    log.info("🔒 Findings: %d high, %d medium, %d low", len(high), len(medium), len(low))

    # HIGH → block + revert + notify
    if high:
        log.warning("🔒🚫 HIGH severity — blocking push!")
        txt = "\n".join(f"• [{f.get('severity','?').upper()}] {f.get('file','?')}: {f.get('issue','?')}"
                        for f in findings)
        try:
            subprocess.run(["git", "reset", "HEAD", "--hard"],
                           cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=30)
            log.info("🔒 Changes reverted")
        except Exception as e:
            log.warning("⚠️ Could not revert: %s", e)
        if is_idea:
            _update_idea_status(task_id, collection, "security-blocked", f"HIGH security:\n{txt}")
        else:
            _update_task_status(task_id, collection, "security-blocked", f"HIGH security:\n{txt}")
        return "blocked"

    # LOW / MEDIUM → auto-fix then publish
    fix_lines = ["Fix these security issues:\n"] + [
        f"- [{f.get('severity','?').upper()}] {f.get('file','?')}: "
        f"{f.get('issue','?')} — Fix: {f.get('fix','N/A')}"
        for f in findings
    ]
    fix_prompt = "\n".join(fix_lines)

    log.info("🔒🔧 Attempting auto-fix for %d issue(s)…", len(medium) + len(low))
    try:
        provider = get_provider_for_phase("implement")
        cmd = build_cli_command(provider, "implement", fix_prompt)
        timeout = get_timeout(provider, "implement")
        r = subprocess.run(cmd, cwd=str(PROJECT_DIR),
                           capture_output=True, text=True, timeout=timeout,
                           env=_clean_env(), encoding="utf-8", errors="replace")
        if r.returncode == 0:
            log.info("🔒✅ Security issues auto-fixed")
            txt = "\n".join(f"• {f.get('file','?')}: {f.get('issue','?')}" for f in findings)
            return "fixed"
        else:
            log.warning("⚠️ Auto-fix failed (exit %d)", r.returncode)
    except subprocess.TimeoutExpired:
        log.warning("⏰ Security auto-fix timed out")
    except Exception as e:
        log.warning("⚠️ Security auto-fix error: %s", e)

    return "publish"


# ═══════════════════════════════════════════════════════════════════════════
#  GIT HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _git_pull() -> bool:
    try:
        r = subprocess.run(["git", "pull", "--ff-only"],
                           cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            out = r.stdout.strip()
            if "Already up to date" in out:
                log.info("   Git: already up to date")
            else:
                log.info("⬇️ Git pull: %s", out.splitlines()[-1] if out else "done")
            return True
        log.warning("⚠️ Git pull failed: %s", r.stderr.strip())
        return False
    except subprocess.TimeoutExpired:
        log.warning("⚠️ Git pull timed out")
        return False
    except Exception as e:
        log.warning("⚠️ Git pull error: %s", e)
        return False


def _has_uncommitted_changes() -> bool:
    """Check if the PROJECT_DIR working tree has any uncommitted changes."""
    try:
        # Check both staged and unstaged changes
        r = subprocess.run(["git", "status", "--porcelain"],
                           cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=10)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _restart_changed_services():
    service_map = {
        "webui/": "webui", "homeassistant/": "homeassistant",
        "nodered/": "nodered", "zigbee2mqtt/": "zigbee2mqtt",
        "mosquitto/": "mosquitto",
    }
    try:
        diff = subprocess.run(["git", "diff", "--name-only"],
                              cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=10)
        changed = diff.stdout.strip().splitlines()
        restarted = set()
        for path in changed:
            for prefix, svc in service_map.items():
                if path.startswith(prefix) and svc not in restarted:
                    log.info("🔄 Restarting %s (files changed in %s)", svc, prefix)
                    subprocess.run(["docker", "compose", "restart", svc],
                                   cwd=str(PROJECT_DIR), timeout=60)
                    restarted.add(svc)
        if not restarted:
            log.info("   No Docker restarts needed")
    except Exception as e:
        log.warning("Could not auto-restart services: %s", e)


def _git_commit_and_push(task_id: str, title: str, label: str = "implementation",
                         backend: str = "") -> bool:
    try:
        subprocess.run(["git", "add", "-A"],
                       cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=30)
        st = subprocess.run(["git", "diff", "--cached", "--quiet"],
                            cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=10)
        if st.returncode == 0:
            log.info("   No staged changes to commit")
            return True

        backend_note = f"Backend: {backend}\n" if backend else ""
        msg = (f"🤖 Agent [{label}]: {title}\n\n"
               f"Task: {task_id}\n{backend_note}"
               f"Automatically {label} by Task-Claw agent.\n\n"
               f"Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>")
        c = subprocess.run(["git", "commit", "-m", msg],
                           cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=30)
        if c.returncode != 0:
            log.warning("⚠️ Git commit failed: %s", c.stderr)
            return False

        _git_pull()
        p = subprocess.run(["git", "push"],
                           cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            log.warning("⚠️ Git push failed: %s", p.stderr)
            return False

        log.info("✅ Changes committed and pushed")
        return True
    except Exception as e:
        log.warning("Git commit/push failed: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  MANUAL IMPLEMENTATION HELPER
# ═══════════════════════════════════════════════════════════════════════════

def _implement_planned_task(task_id: str, target: dict, collection: list,
                            is_idea: bool, state: dict):
    title = target.get("title", task_id)
    plan = target.get("plan", "")

    log.info("🚀 Starting manual implementation for: %s", title)
    if is_idea:
        _update_idea_status(task_id, collection, "in-progress", "Manual implementation started")
    else:
        _update_task_status(task_id, collection, "in-progress", "Manual implementation started")
    for item in collection:
        if item.get("id") == task_id:
            item["implementation_started_at"] = datetime.now().isoformat()
            break
    (save_ideas if is_idea else save_tasks)(collection)

    # Start from code stage — the plan is already done, use it as context
    prompt = f"Implement the following plan for: {title}\n\n{plan}"
    result = run_pipeline(prompt, task_id=task_id, start_stage="code")

    for item in collection:
        if item.get("id") == task_id:
            item["implementation_completed_at"] = datetime.now().isoformat()
            break
    (save_ideas if is_idea else save_tasks)(collection)

    if result["success"]:
        status = "pushed-to-production" if result["published"] else "done"
        if is_idea:
            _update_idea_status(task_id, collection, status, "Manual implementation completed.")
        else:
            _update_task_status(task_id, collection, status, "Manual implementation completed.")
    else:
        err = result.get("error", "Pipeline failed")
        if is_idea:
            _update_idea_status(task_id, collection, "open", f"Implementation failed: {err}")
        else:
            _update_task_status(task_id, collection, "open", f"Implementation failed: {err}")


# ═══════════════════════════════════════════════════════════════════════════
#  PROCESS TASK / IDEA
# ═══════════════════════════════════════════════════════════════════════════


def _extract_next_steps(analysis: str) -> str:
    lines = analysis.split("\n")
    capturing = False
    result = []
    for line in lines:
        lower = line.lower().strip()
        if "next steps" in lower and ("**" in line or "#" in line):
            capturing = True
            continue
        if capturing:
            if line.strip().startswith("**") and line.strip().endswith("**") and "next" not in lower:
                break
            if line.strip().startswith("#") and "next" not in lower:
                break
            result.append(line)
    return "\n".join(result).strip() if result else ""


def _build_photo_context(task: dict) -> str:
    photos = task.get("photos", [])
    if not photos:
        return ""
    photo_list = ", ".join(photos)
    return (f"\n\nAttached Photos ({len(photos)}): {photo_list}"
            f"\nPhotos are stored in {PROJECT_DIR / 'task-photos'} — reference them for visual context.")


def process_task(task: dict, tasks: list, state: dict):
    task_type = task.get("type", "unknown")
    title = task.get("title", "(no title)")
    desc = task.get("description", "(no description)")
    priority = task.get("priority", "medium")
    task_id = task.get("id", "")

    log.info("=" * 60)
    log.info("📋 New %s: %s", task_type, title)
    log.info("   Priority: %s | ID: %s", priority, task_id)
    log.info("   Description: %s", desc[:200])

    _update_task_status(task_id, tasks, "grabbed", "Agent has picked up this task.")
    # Description first — Claude works best when the task is front and center
    prompt = f"{title}: {desc}" if desc and desc != title else title
    photos = task.get("photos", [])
    if photos:
        prompt += f"\n\nAttached Photos ({len(photos)}): {', '.join(photos)}"
        prompt += f"\nPhotos in {PROJECT_DIR / 'task-photos'}"

    _update_task_status(task_id, tasks, "in-progress", "Pipeline started.")
    result = run_pipeline(prompt, task_id=task_id)

    plan_text = result.get("stage_results", {}).get("plan", "")
    # Collect output file paths for each stage
    output_files = {}
    task_output_dir = PIPELINE_OUTPUT_DIR / task_id
    if task_output_dir.exists():
        for f in sorted(task_output_dir.glob("*.md")):
            if not f.name.startswith("."):
                output_files[f.stem] = str(f)

    for t in tasks:
        if t.get("id") == task_id:
            if plan_text:
                t["plan"] = plan_text
                t["planning_completed_at"] = datetime.now().isoformat()
            t["implementation_completed_at"] = datetime.now().isoformat()
            t["pipeline_summary"] = {
                "stages": result.get("stage_log", []),
                "elapsed": result.get("pipeline_elapsed", 0),
                "published": result.get("published", False),
                "success": result.get("success", False),
                "ran_at": datetime.now().isoformat(),
                "output_files": output_files,
                "output_dir": str(task_output_dir),
            }
            t["updated"] = _ts_ms()
            break
    save_tasks(tasks)

    if result["success"]:
        status = "pushed-to-production" if result["published"] else "done"
        _update_task_status(task_id, tasks, status, "Pipeline completed.")
    else:
        err = result.get("error", "Pipeline failed")
        new_status = "security-blocked" if "security" in err.lower() else "open"
        _update_task_status(task_id, tasks, new_status, f"Pipeline failed: {err}")


def process_idea(idea: dict, ideas: list, state: dict):
    title = idea.get("title", "(no title)")
    desc = idea.get("description", "(no description)")
    idea_id = idea.get("id", "")

    log.info("=" * 60)
    log.info("💡 New idea: %s", title)
    log.info("   ID: %s", idea_id)

    _update_idea_status(idea_id, ideas, "planning", "Agent is analyzing this idea.")
    prompt = f"Idea: {title}\nDescription: {desc}"
    _update_idea_status(idea_id, ideas, "in-progress", "Pipeline started.")
    result = run_pipeline(prompt, task_id=idea_id)

    plan_text = result.get("stage_results", {}).get("plan", "")
    for i in ideas:
        if i.get("id") == idea_id:
            if plan_text:
                i["plan"] = plan_text
                i["planning_completed_at"] = datetime.now().isoformat()
            i["implementation_completed_at"] = datetime.now().isoformat()
            i["updated"] = _ts_ms()
            break
    save_ideas(ideas)

    if result["success"]:
        status = "pushed-to-production" if result["published"] else "done"
        _update_idea_status(idea_id, ideas, status, "Pipeline completed.")
    else:
        err = result.get("error", "Pipeline failed")
        new_status = "security-blocked" if "security" in err.lower() else "open"
        _update_idea_status(idea_id, ideas, new_status, f"Pipeline failed: {err}")



# ═══════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════

def main():
    # ── Direct CLI invocation: python task-claw.py "my prompt" ──────────────
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        prompt_arg = " ".join(sys.argv[1:])
        log.info("🦀 Task-Claw direct pipeline mode: %s", prompt_arg[:100])
        result = run_pipeline(prompt_arg)
        log.info("Pipeline result: success=%s, published=%s, error=%s",
                 result["success"], result["published"], result.get("error"))
        sys.exit(0 if result["success"] else 1)

    log.info("🦀 Task-Claw Agent starting… (version %s)", AGENT_VERSION)
    log.info("   Project dir:  %s", PROJECT_DIR)
    log.info("   Tasks file:   %s", TASKS_FILE)
    log.info("   Ideas file:   %s", IDEAS_FILE)
    log.info("   Poll interval: %ds (%s)", POLL_INTERVAL,
             f"{POLL_INTERVAL // 3600}h" if POLL_INTERVAL >= 3600 else f"{POLL_INTERVAL // 60}m")
    log.info("   API cap:      %d calls/day", MAX_API_CALLS_PER_DAY)
    log.info("   Trigger port: %d", TRIGGER_PORT)
    log.info("   GitHub token: %s", "✅ set" if GITHUB_TOKEN else "❌ NOT SET")

    providers = list_available_providers()
    default = os.environ.get("CLI_PROVIDER", _load_providers().get("default_provider", "copilot"))
    log.info("   CLI providers: %s", ", ".join(f"{k} ({v})" for k, v in providers.items()))
    log.info("   Default provider: %s", default)
    log.info("")

    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN is required! Set it in .env")
        sys.exit(1)

    start_trigger_server()
    state = load_state()

    with status_lock:
        agent_status["state"] = "idle"
        agent_status["api_calls_today"] = state.get("api_calls_today", 0)

    while True:
        try:
            state = load_state()

            # Check if force trigger requested (skip age filter)
            with status_lock:
                skip_age_filter = agent_status.pop("force_no_age_filter", False)

            max_age_ms = int(os.environ.get("AGENT_MAX_TASK_AGE_HOURS", "8")) * 3600 * 1000
            cutoff_ms = _ts_ms() - max_age_ms

            tasks = load_tasks()
            new_tasks = [
                t for t in tasks
                if t.get("id") and t["id"] not in state["processed"]
                and t.get("status", "open") == "open"
                and (skip_age_filter or (t.get("created") or t.get("updated") or 0) >= cutoff_ms)
            ] if tasks else []

            ideas = load_ideas()
            new_ideas = [
                i for i in ideas
                if i.get("id") and i["id"] not in state["processed"]
                and i.get("status", "open") == "open"
                and (skip_age_filter or (i.get("created") or i.get("updated") or 0) >= cutoff_ms)
            ] if ideas else []

            with status_lock:
                agent_status["tasks_pending"] = len(new_tasks)
                agent_status["ideas_pending"] = len(new_ideas)
                agent_status["api_calls_today"] = state.get("api_calls_today", 0)
                agent_status["last_run"] = datetime.now().isoformat()

            if new_tasks or new_ideas:
                log.info("🔍 Found %d new task(s) and %d new idea(s)!",
                         len(new_tasks), len(new_ideas))

                log.info("⬇️ Pulling latest changes…")
                _git_pull()

                for task in new_tasks:
                    log.info("▶️ Starting task: %s", task.get("title", task.get("id")))
                    with status_lock:
                        agent_status["state"] = "processing"
                        agent_status["current_task"] = task.get("title", task.get("id"))
                    process_task(task, tasks, state)
                    state["processed"].append(task["id"])
                    save_state(state)

                for idea in new_ideas:
                    log.info("▶️ Starting idea: %s", idea.get("title", idea.get("id")))
                    with status_lock:
                        agent_status["state"] = "processing"
                        agent_status["current_task"] = idea.get("title", idea.get("id"))
                    process_idea(idea, ideas, state)
                    state["processed"].append(idea["id"])
                    save_state(state)
            else:
                log.debug("No new tasks or ideas — sleeping %ds", POLL_INTERVAL)

            with status_lock:
                agent_status["state"] = "idle"
                agent_status["current_task"] = None
                agent_status["current_stage"] = None

        except KeyboardInterrupt:
            log.info("\n👋 Agent stopped by user")
            break
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)
            with status_lock:
                agent_status["state"] = "error"

        triggered = interruptible_sleep(POLL_INTERVAL)
        if triggered:
            log.info("🚨 Woke up from trigger!")


if __name__ == "__main__":
    main()
