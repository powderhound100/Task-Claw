"""
Task-Claw — Multi-Provider Coding Agent
Monitors tasks.json / ideas.json for work items, plans with GPT-4o or any
coding CLI, implements with a pluggable CLI provider, runs a security review,
and pushes to production.

Supported CLI providers are defined in providers.json.
"""

import json
import os
import re
import sys
import time
import subprocess
import logging
import threading
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
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
TASKS_FILE    = Path(os.environ.get("TASKS_FILE", str(PROJECT_DIR / "nodered" / "data" / "tasks.json")))
IDEAS_FILE    = Path(os.environ.get("IDEAS_FILE", str(PROJECT_DIR / "nodered" / "data" / "ideas.json")))
STATE_FILE    = AGENT_DIR / "agent-state.json"
LOG_FILE      = AGENT_DIR / "agent.log"
POLL_INTERVAL = int(os.environ.get("AGENT_POLL_INTERVAL", "3600"))
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
HA_URL        = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN      = os.environ.get("HA_TOKEN", "")
TRIGGER_PORT  = int(os.environ.get("AGENT_TRIGGER_PORT", "8099"))

GITHUB_MODELS_URL = os.environ.get("GITHUB_MODELS_URL",
                                    "https://models.inference.ai.azure.com/chat/completions")
GITHUB_MODELS_MODEL = os.environ.get("GITHUB_MODELS_MODEL", "gpt-4o")
MAX_API_CALLS_PER_DAY = int(os.environ.get("AGENT_MAX_CALLS", "10"))

SESSION_DIR = Path(os.environ.get("AGENT_SESSION_DIR",
                                   str(Path.home() / ".copilot" / "session-state")))
AUTO_IMPLEMENT_DEFAULT = os.environ.get("AGENT_AUTO_IMPLEMENT_DEFAULT", "true").lower() == "true"

# ── Runtime directories ─────────────────────────────────────────────────────
RESEARCH_DIR = AGENT_DIR / "research-output"
RESEARCH_DIR.mkdir(exist_ok=True)
SECURITY_REVIEW_DIR = AGENT_DIR / "security-reviews"
SECURITY_REVIEW_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
#  CLI PROVIDER SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

PROVIDERS_FILE = AGENT_DIR / "providers.json"

def _load_providers() -> dict:
    """Load provider definitions from providers.json."""
    if not PROVIDERS_FILE.exists():
        return {"providers": {}, "default_provider": "claude"}
    try:
        return json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("Could not load providers.json: %s", e)
        return {"providers": {}, "default_provider": "claude"}


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
        "security":  "CLI_SECURITY_PROVIDER",
        "test":      "CLI_TEST_PROVIDER",
        "review":    "CLI_REVIEW_PROVIDER",
        "rewrite":   "CLI_PLAN_PROVIDER",
    }
    env_key = env_map.get(phase, "CLI_PROVIDER")
    provider_name = os.environ.get(env_key) or os.environ.get("CLI_PROVIDER")
    return _get_provider(provider_name)


def build_cli_command(provider: dict, phase: str, prompt: str) -> list[str]:
    """
    Build the full CLI command list from a provider config.
    Replaces {prompt} placeholder in args with the actual prompt text.
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
        "security":  "security_args",
        "test":      "test_args",
        "review":    "review_args",
    }.get(phase, "implement_args")

    # Fallback: test/review → plan_args if not defined
    if arg_key not in provider and phase in ("test", "review"):
        arg_key = "plan_args"

    args = list(provider.get(arg_key, ["-p", "{prompt}"]))
    args = [a.replace("{prompt}", prompt) for a in args]

    return [binary] + sub + args


def get_timeout(provider: dict, phase: str) -> int:
    """Get timeout for a phase, checking env overrides then provider config."""
    env_map = {
        "plan":      ("PIPELINE_PLAN_TIMEOUT",    "COPILOT_PLAN_TIMEOUT"),
        "implement": ("PIPELINE_CODE_TIMEOUT",    "COPILOT_TIMEOUT"),
        "security":  ("PIPELINE_REVIEW_TIMEOUT",  "COPILOT_SECURITY_TIMEOUT"),
        "test":      ("PIPELINE_TEST_TIMEOUT",    "COPILOT_SECURITY_TIMEOUT"),
        "review":    ("PIPELINE_REVIEW_TIMEOUT",  "COPILOT_SECURITY_TIMEOUT"),
    }
    for env_key in env_map.get(phase, ("COPILOT_TIMEOUT",)):
        env_val = os.environ.get(env_key)
        if env_val:
            return int(env_val)

    timeout_key = {
        "plan":      "plan_timeout",
        "implement": "implement_timeout",
        "security":  "security_timeout",
        "test":      "test_timeout",
        "review":    "review_timeout",
    }.get(phase, "implement_timeout")

    return int(provider.get(timeout_key, 600))


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
    if not pipeline_file.exists():
        return _PIPELINE_DEFAULT
    try:
        return json.loads(pipeline_file.read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("Could not load pipeline.json: %s — using defaults", e)
        return _PIPELINE_DEFAULT


def _clean_env() -> dict:
    """Return a copy of os.environ without vars that block nested CLI sessions."""
    env = dict(os.environ)
    # Claude Code refuses to launch inside another Claude Code session
    for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION"):
        env.pop(key, None)
    return env


def run_cli_command(provider: dict, phase: str, prompt: str,
                    cwd: str | None = None) -> tuple[bool, str]:
    """Run a CLI provider command. Returns (success, output_text)."""
    cmd = build_cli_command(provider, phase, prompt)
    timeout = get_timeout(provider, phase)
    work_dir = cwd or str(PROJECT_DIR)
    # Fall back to agent dir if target dir doesn't exist
    if not Path(work_dir).is_dir():
        log.warning("   cwd '%s' does not exist — falling back to %s", work_dir, AGENT_DIR)
        work_dir = str(AGENT_DIR)
    log.info("   CLI [%s/%s]: %s ... (timeout=%ds)",
             provider.get("name", "?"), phase, " ".join(cmd[:3]), timeout)
    try:
        result = subprocess.run(
            cmd, cwd=work_dir,
            capture_output=True, text=True, timeout=timeout,
            env=_clean_env(), encoding="utf-8", errors="replace",
        )
        log.info("   Exit code: %d", result.returncode)
        if result.stdout:
            log.debug("   Stdout tail: %s", result.stdout[-1000:])
        if result.stderr:
            log.warning("   Stderr tail: %s", result.stderr[-500:])
        output = result.stdout.strip() if result.stdout else result.stderr.strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        log.error("   Timed out after %ds", timeout)
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        log.error("   CLI error: %s", e)
        return False, str(e)


def _pm_api_call(system_msg: str, user_msg: str, pm_cfg: dict) -> str:
    """Low-level PM API call. Raises on failure."""
    backend = pm_cfg.get("backend", "github_models")
    model = pm_cfg.get("model", "gpt-4o")
    max_tokens = pm_cfg.get("max_tokens", 4096)
    temperature = pm_cfg.get("temperature", 0.3)
    pm_timeout = int(os.environ.get("PIPELINE_MANAGER_TIMEOUT", "300"))

    if backend == "github_models":
        if not GITHUB_TOKEN:
            raise ValueError("GITHUB_TOKEN not set")
        resp = requests.post(
            GITHUB_MODELS_URL,
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=pm_timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    elif backend == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system_msg,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=pm_timeout,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    elif backend == "openai_compatible":
        url = os.environ.get("PIPELINE_PM_URL",
                             "http://localhost:11434/v1/chat/completions")
        key = os.environ.get("PIPELINE_PM_KEY") or os.environ.get("OPENAI_API_KEY", "")
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=pm_timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    else:
        raise ValueError(f"Unknown PM backend: {backend}")


def pm_direct_team(stage_name: str, original_prompt: str, context: str,
                   team: list, pm_cfg: dict) -> str:
    """
    PM acts as director BEFORE the team runs: given the stage, request, and all
    prior context, it writes a precise task brief for the team to execute.
    Falls back to the original prompt on failure.
    """
    system_msg = "You are a Program Manager directing an AI coding team."
    user_msg = (
        f"Stage: {stage_name}\n"
        f"Team members: {', '.join(team)}\n"
        f"Original user request: {original_prompt}\n\n"
        f"Prior pipeline context:\n{context}\n\n"
        "Write a clear, specific task brief for the team to execute in this stage. "
        "Include: what to do, what files/components to focus on, any constraints from prior stages, "
        "and the expected output format. Be concise and actionable. "
        "Return only the task brief — no preamble."
    )
    log.info("   PM [direct/%s]: generating task brief for team %s…", stage_name, team)
    try:
        return _pm_api_call(system_msg, user_msg, pm_cfg)
    except Exception as e:
        log.warning("⚠️ PM direction failed (%s) — using original prompt", e)
        return original_prompt


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
    system_msg = (
        "You are a Program Manager overseeing an AI coding pipeline. "
        "Your job is NOT to simply pick a winner — you oversee quality, verify "
        "that stage outputs meet requirements, identify gaps or drift from the "
        "original request, and decide whether the stage passes your quality gate."
    )
    user_msg = (
        f"Stage: {stage_name}\n"
        f"Original user request: {original_prompt}\n\n"
        f"Prior pipeline context:\n{context}\n\n"
        f"The following agents worked on this stage simultaneously:\n\n"
        f"{agent_blocks}\n\n"
        "As the PM overseeing this pipeline, evaluate the stage output(s):\n\n"
        "1. **Requirements check**: Does the output fully address the original request? "
        "List any missed requirements or gaps.\n"
        "2. **Quality check**: Is the output correct, complete, and production-ready? "
        "Flag any issues, bugs, or incomplete work.\n"
        "3. **Drift check**: Has the implementation drifted from the plan or prior stage context? "
        "Note any deviations.\n"
        "4. **Verdict**: APPROVE if the output meets quality standards, or REVISE if it needs rework.\n\n"
        "Return your response in these sections:\n"
        "## Verdict\nAPPROVE or REVISE\n\n"
        "## Issues\n[bullet list of any problems found, or 'None']\n\n"
        "## Synthesis\n[the best combined output, incorporating strengths from all agents]\n\n"
        "## Handoff to next stage\n[precise instructions/context for the next team, "
        "including any issues the next stage should be aware of]"
    )

    log.info("   PM [oversee/%s]: evaluating %d agent output(s)…",
             stage_name, len(team_outputs))
    try:
        result = _pm_api_call(system_msg, user_msg, pm_cfg)
        return _parse_overseer_response(result)
    except Exception as e:
        log.warning("⚠️ PM oversight failed (%s) — auto-approving with concatenated outputs", e)
        fallback = "\n\n".join(f"## {name}\n{output}" for name, output in team_outputs)
        return {
            "verdict": "approve",
            "synthesis": fallback,
            "handoff": fallback,
            "issues": [],
            "full_response": fallback,
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

        review_prompt = (
            f"You are reviewing code produced by other agents.\n\n"
            f"Original request: {original_prompt}\n\n"
            f"Plan context:\n{plan_context}\n\n"
            f"Implementations to review:\n\n{other_blocks}\n\n"
            "Perform a thorough code review. For each implementation:\n"
            "1. **Correctness**: Does it fulfill the plan and original request?\n"
            "2. **Gaps**: What requirements or edge cases are missing?\n"
            "3. **Bugs**: Any logic errors, off-by-one, null checks, etc.?\n"
            "4. **Strengths**: What does this implementation do particularly well?\n"
            "5. **Suggestions**: Specific improvements with code snippets where helpful.\n\n"
            "Be specific and actionable. Reference file names and line numbers where possible."
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
        for fut in concurrent.futures.as_completed(futures, timeout=timeout + 30):
            try:
                res = fut.result()
                if res:
                    reviews.append(res)
            except Exception as e:
                log.warning("   Cross-review future error: %s", e)

    log.info("   🔄 Cross-review complete: %d reviews collected", len(reviews))
    return reviews


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

    system_msg = (
        "You are a Program Manager overseeing an AI coding pipeline. "
        "Multiple agents have produced implementations AND reviewed each other's work. "
        "Your job is to deeply analyze both implementations, leverage the cross-reviews, "
        "and produce a merged result that takes the best elements from each."
    )
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


def run_team(stage_name: str, prompt: str, team_provider_names: list,
             context: str, timeout: int) -> list:
    """
    Run a team of CLI providers in parallel for a pipeline stage.
    Returns list of (provider_name, output) for successful runs only.
    """
    phase_map = {
        "rewrite": "plan",
        "plan":    "plan",
        "code":    "implement",
        "test":    "test",
        "review":  "review",
    }
    phase = phase_map.get(stage_name, "implement")
    combined_prompt = f"{context}\n\n{prompt}".strip() if context else prompt

    def _run_one(provider_name: str):
        try:
            provider = get_provider_for_phase(phase, provider_name)
            success, output = run_cli_command(provider, phase, combined_prompt)
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
        for fut in concurrent.futures.as_completed(futures, timeout=timeout + 30):
            try:
                res = fut.result()
                if res:
                    results.append(res)
            except Exception as e:
                log.warning("   Team future error: %s", e)
    return results


def rewrite_prompt(raw_prompt: str, pm_cfg: dict) -> str:
    """Have the PM rewrite the raw prompt for clarity. Falls back to original on failure."""
    system_msg = "You are a Program Manager preparing a user request for an AI coding pipeline."
    user_msg = (
        "Rewrite the following user request to be maximally clear, specific, and actionable "
        "for a coding AI pipeline. Preserve all intent. "
        "Return only the rewritten prompt — no explanation, no preamble.\n\n"
        f"Original request:\n{raw_prompt}"
    )
    log.info("   PM [rewrite]: clarifying prompt…")
    try:
        result = _pm_api_call(system_msg, user_msg, pm_cfg)
        return result.strip() or raw_prompt
    except Exception as e:
        log.warning("⚠️ PM rewrite failed (%s) — using original prompt", e)
        return raw_prompt


def _cap_context(context: str, max_chars: int = 12000) -> str:
    """Cap context to max_chars by dropping oldest ## sections first."""
    if len(context) <= max_chars:
        return context
    sections = re.split(r'(?=## [A-Z])', context)
    while len("".join(sections)) > max_chars and len(sections) > 1:
        sections.pop(0)
    return "".join(sections)


MAX_REVISE_ATTEMPTS = int(os.environ.get("PIPELINE_MAX_REVISE", "1"))


def run_pipeline(prompt: str, task_id: str | None = None,
                 pipeline_cfg: dict | None = None,
                 start_stage: str | None = None) -> dict:
    """
    Full pipeline: rewrite → plan → code → test → review → publish.

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
    original_prompt = prompt
    tid = task_id or f"pipeline-{int(time.time())}"

    STAGE_ORDER = ["rewrite", "plan", "code", "test", "review"]
    skip = bool(start_stage)

    log.info("🚀 Pipeline starting for: %s (start_stage=%s)", tid, start_stage or "rewrite")

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

        timeout = int(os.environ.get(
            f"PIPELINE_{stage.upper()}_TIMEOUT",
            str(stage_cfg.get("timeout", 300))
        ))
        team = stage_cfg.get("team", ["claude"])

        log.info("   ▶️ Stage: %-8s | team: %s | timeout: %ds", stage, team, timeout)
        with status_lock:
            agent_status["state"] = f"pipeline:{stage}"
            agent_status["current_stage"] = stage

        # ── Rewrite: PM-only, no CLI team ───────────────────────────────────
        if stage == "rewrite":
            prompt = rewrite_prompt(original_prompt, pm_cfg)
            stage_results["rewrite"] = prompt
            log.info("   Rewritten prompt (%d chars)", len(prompt))
            continue

        # ── Review: use structured security review, PM oversees verdict ─────
        if stage == "review":
            review = run_security_review(tid, (prompt[:80] or tid))
            team_outputs = [("security-review", review.get("report", "No report."))]
            pm_result = pm_oversee_stage(
                stage, original_prompt, context, team_outputs, pm_cfg
            )
            stage_results["review"] = pm_result["full_response"]
            context += f"\n\n## REVIEW HANDOFF\n{pm_result['handoff']}"
            context = _cap_context(context)

            if pm_result["issues"]:
                log.info("   PM flagged %d review issues: %s",
                         len(pm_result["issues"]),
                         "; ".join(pm_result["issues"][:3]))

            action = _handle_security_findings(
                review, tid, prompt[:80] or tid, [], False
            )
            if action == "blocked":
                log.warning("🔒 Pipeline blocked by security review for: %s", tid)
                return {
                    "success": False,
                    "stage_results": stage_results,
                    "published": False,
                    "error": "Blocked by security review (HIGH severity)",
                }
            continue

        # ── Plan / Code / Test: PM directs → team runs → PM oversees ────────
        for attempt in range(1 + MAX_REVISE_ATTEMPTS):
            directed_prompt = pm_direct_team(stage, prompt, context, team, pm_cfg)
            team_outputs = run_team(stage, directed_prompt, team, context, timeout)

            if not team_outputs:
                log.warning("⚠️ Stage '%s' — no team output, continuing", stage)
                stage_results[stage] = ""
                break

            # After code stage, restart any changed services
            if stage == "code":
                _restart_changed_services()

            # ── Code stage with 2+ agents: cross-review + deep merge ────────
            if stage == "code" and len(team_outputs) >= 2:
                cross_reviews = cross_review_code(
                    team_outputs, context, original_prompt, timeout
                )
                if cross_reviews:
                    pm_result = pm_merge_with_reviews(
                        stage, original_prompt, context,
                        team_outputs, cross_reviews, pm_cfg
                    )
                else:
                    # Cross-review failed, fall back to standard oversight
                    pm_result = pm_oversee_stage(
                        stage, original_prompt, context, team_outputs, pm_cfg
                    )
            else:
                # ── Standard oversight for plan/test or single-agent code ───
                pm_result = pm_oversee_stage(
                    stage, original_prompt, context, team_outputs, pm_cfg
                )

            # ── PM quality gate ─────────────────────────────────────────────
            verdict = pm_result["verdict"]
            issues = pm_result["issues"]

            if issues:
                log.info("   PM flagged %d issues in '%s': %s",
                         len(issues), stage, "; ".join(issues[:3]))

            if verdict == "revise" and attempt < MAX_REVISE_ATTEMPTS:
                log.warning("   🔄 PM verdict: REVISE (attempt %d/%d) — re-running stage '%s'",
                            attempt + 1, MAX_REVISE_ATTEMPTS, stage)
                # Inject the PM's feedback into context so the retry is informed
                context += (
                    f"\n\n## PM REVISION REQUEST ({stage})\n"
                    f"Issues found:\n" +
                    "\n".join(f"- {iss}" for iss in issues) +
                    f"\n\nPM guidance:\n{pm_result['handoff']}"
                )
                context = _cap_context(context)
                continue  # retry the stage

            if verdict == "revise":
                log.warning("   ⚠️ PM verdict: REVISE but max attempts reached — proceeding with best effort")

            # Approved (or max attempts reached) — record and move on
            stage_results[stage] = pm_result["full_response"]
            context += f"\n\n## {stage.upper()} HANDOFF\n{pm_result['handoff']}"
            context = _cap_context(context)
            log.info("   ✅ PM verdict: %s for stage '%s'", verdict.upper(), stage)
            break

    # ── Publish ──────────────────────────────────────────────────────────────
    published = False
    if publish_cfg.get("enabled", True) and publish_cfg.get("auto_push", True):
        title = prompt[:80] if not task_id else task_id
        log.info("📤 Publishing: %s", title)
        published = _git_commit_and_push(tid, title, label="pipeline")

    with status_lock:
        agent_status["state"] = "idle"

    log.info("✅ Pipeline complete for: %s (published=%s)", tid, published)
    return {"success": True, "stage_results": stage_results,
            "published": published, "error": None}


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

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
        else:
            self._json(404, {"ok": False, "error": "Not found"})

    def do_GET(self):
        if self.path.rstrip("/") == "/status":
            with status_lock:
                snap = dict(agent_status)
            snap["providers"] = list_available_providers()
            snap["default_provider"] = os.environ.get("CLI_PROVIDER",
                                        _load_providers().get("default_provider", "claude"))
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
        elif self.path.startswith("/research-status/"):
            idea_id = self.path.split("/research-status/", 1)[1].rstrip("/")
            with research_lock:
                job = research_jobs.get(idea_id, {"status": "idle", "result": None})
            self._json(200, job)
        elif self.path.startswith("/security-report/"):
            task_id = self.path.split("/security-report/", 1)[1].rstrip("/")
            # Look for any file in SECURITY_REVIEW_DIR that starts with the task id
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

    # helpers
    def _read_body(self) -> dict | None:
        cl = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(cl) if cl else b""
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            self._json(400, {"ok": False, "error": "Invalid JSON"})
            return None


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
    TASKS_FILE.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    try:
        requests.post("http://localhost:3000/api/tasks-save", json=tasks, timeout=5)
    except Exception:
        pass

def load_ideas() -> list:
    if not IDEAS_FILE.exists():
        return []
    try:
        return json.loads(IDEAS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not read ideas file: %s", e)
        return []

def save_ideas(ideas: list):
    IDEAS_FILE.write_text(json.dumps(ideas, indent=2), encoding="utf-8")
    try:
        requests.post("http://localhost:3000/api/ideas-save", json=ideas, timeout=5)
    except Exception:
        pass


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
#  HOME ASSISTANT NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════

def notify_ha(title: str, message: str):
    if not HA_TOKEN:
        log.info("(HA_TOKEN not set — skipping notification)")
        return
    try:
        requests.post(
            f"{HA_URL}/api/services/persistent_notification/create",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            json={"title": title, "message": message},
            timeout=5,
        )
        log.info("✅ Home Assistant notification sent")
    except Exception as e:
        log.warning("HA notification failed: %s", e)


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
        notify_ha(f"⚠️ {label.title()} needs you: {task_id}",
                  f"CLI couldn't complete.\n\n{err_snippet}")
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
        notify_ha(f"🚨 SECURITY BLOCK: {title}",
                  f"High-severity issues in {label} {task_id}.\nNot pushed.\n\n{txt}")
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
            notify_ha(f"🔒 Security fixes applied: {title}",
                      f"Auto-fixed {len(findings)} issue(s).\n\n{txt}")
            return "fixed"
        else:
            log.warning("⚠️ Auto-fix failed (exit %d)", r.returncode)
    except subprocess.TimeoutExpired:
        log.warning("⏰ Security auto-fix timed out")
    except Exception as e:
        log.warning("⚠️ Security auto-fix error: %s", e)

    if medium:
        notify_ha(f"⚠️ Security review: {title}",
                  "Medium-severity issues found but auto-fix failed. Pushed anyway — review manually.")
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
    notify_ha(f"🚀 Implementing: {title}", f"Manual implementation triggered.\n\n**Plan:**\n{plan[:300]}")

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
        notify_ha(f"✅ Completed: {title}", f"Manual implementation successful!\n\n**Plan:**\n{plan[:300]}")
    else:
        err = result.get("error", "Pipeline failed")
        if is_idea:
            _update_idea_status(task_id, collection, "open", f"Implementation failed: {err}")
        else:
            _update_task_status(task_id, collection, "open", f"Implementation failed: {err}")
        notify_ha(f"⚠️ Failed: {title}", f"Manual implementation error.\n\n{err}")


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
    notify_ha(f"🤖 Agent grabbed task: {title}",
              f"Processing {task_type} (priority: {priority}).\nTask ID: {task_id}")

    prompt = f"Task Type: {task_type}\nTitle: {title}\nPriority: {priority}\nDescription: {desc}"
    photos = task.get("photos", [])
    if photos:
        prompt += f"\n\nAttached Photos ({len(photos)}): {', '.join(photos)}"
        prompt += f"\nPhotos in {PROJECT_DIR / 'task-photos'}"

    _update_task_status(task_id, tasks, "in-progress", "Pipeline started.")
    result = run_pipeline(prompt, task_id=task_id)

    plan_text = result.get("stage_results", {}).get("plan", "")
    for t in tasks:
        if t.get("id") == task_id:
            if plan_text:
                t["plan"] = plan_text
                t["planning_completed_at"] = datetime.now().isoformat()
            t["implementation_completed_at"] = datetime.now().isoformat()
            t["updated"] = _ts_ms()
            break
    save_tasks(tasks)

    if result["success"]:
        status = "pushed-to-production" if result["published"] else "done"
        _update_task_status(task_id, tasks, status, "Pipeline completed.")
        notify_ha(f"✅ Task completed: {title}",
                  f"Pipeline finished. Published: {result['published']}")
    else:
        err = result.get("error", "Pipeline failed")
        new_status = "security-blocked" if "security" in err.lower() else "open"
        _update_task_status(task_id, tasks, new_status, f"Pipeline failed: {err}")
        notify_ha(f"❌ Task failed: {title}", err)


def process_idea(idea: dict, ideas: list, state: dict):
    title = idea.get("title", "(no title)")
    desc = idea.get("description", "(no description)")
    idea_id = idea.get("id", "")

    log.info("=" * 60)
    log.info("💡 New idea: %s", title)
    log.info("   ID: %s", idea_id)

    _update_idea_status(idea_id, ideas, "planning", "Agent is analyzing this idea.")
    notify_ha(f"💡 Agent planning idea: {title}", f"Idea ID: {idea_id}")

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
        notify_ha(f"✅ Idea completed: {title}",
                  f"Pipeline finished. Published: {result['published']}")
    else:
        err = result.get("error", "Pipeline failed")
        new_status = "security-blocked" if "security" in err.lower() else "open"
        _update_idea_status(idea_id, ideas, new_status, f"Pipeline failed: {err}")
        notify_ha(f"❌ Idea failed: {title}", err)


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

    log.info("🦀 Task-Claw Agent starting…")
    log.info("   Project dir:  %s", PROJECT_DIR)
    log.info("   Tasks file:   %s", TASKS_FILE)
    log.info("   Ideas file:   %s", IDEAS_FILE)
    log.info("   Poll interval: %ds (%s)", POLL_INTERVAL,
             f"{POLL_INTERVAL // 3600}h" if POLL_INTERVAL >= 3600 else f"{POLL_INTERVAL // 60}m")
    log.info("   API cap:      %d calls/day", MAX_API_CALLS_PER_DAY)
    log.info("   Trigger port: %d", TRIGGER_PORT)
    log.info("   GitHub token: %s", "✅ set" if GITHUB_TOKEN else "❌ NOT SET")
    log.info("   HA token:     %s", "✅ set" if HA_TOKEN else "❌ not set")

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
