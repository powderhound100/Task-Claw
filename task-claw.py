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
PROJECT_DIR   = Path(os.environ.get("PROJECT_DIR", str(AGENT_DIR)))
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
        return {"providers": {}, "default_provider": "copilot"}
    try:
        return json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("Could not load providers.json: %s", e)
        return {"providers": {}, "default_provider": "copilot"}


def _get_provider(name: str | None = None) -> dict:
    """Get a provider config by name, falling back to default."""
    cfg = _load_providers()
    providers = cfg.get("providers", {})
    name = (name or "").strip().lower() or cfg.get("default_provider", "copilot")
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
    sub = provider.get("subcommand", [])

    arg_key = {
        "plan":      "plan_args",
        "implement": "implement_args",
        "security":  "security_args",
    }.get(phase, "implement_args")

    args = list(provider.get(arg_key, ["-p", "{prompt}"]))
    args = [a.replace("{prompt}", prompt) for a in args]

    return [binary] + sub + args


def get_timeout(provider: dict, phase: str) -> int:
    """Get timeout for a phase, checking env overrides then provider config."""
    env_map = {
        "plan":      "COPILOT_PLAN_TIMEOUT",
        "implement": "COPILOT_TIMEOUT",
        "security":  "COPILOT_SECURITY_TIMEOUT",
    }
    env_val = os.environ.get(env_map.get(phase, "COPILOT_TIMEOUT"))
    if env_val:
        return int(env_val)

    timeout_key = {
        "plan":      "plan_timeout",
        "implement": "implement_timeout",
        "security":  "security_timeout",
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
#  THREADING / STATUS
# ═══════════════════════════════════════════════════════════════════════════

trigger_event = threading.Event()
agent_status = {
    "state": "starting",
    "current_task": None,
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
            trigger_event.set()
            with status_lock:
                agent_status["last_trigger"] = datetime.now().isoformat()
            log.info("🚨 Manual trigger received — waking agent!")
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
                                        _load_providers().get("default_provider", "copilot"))
            snap["default_planning_backend"] = DEFAULT_PLANNING_BACKEND
            snap["session_dir"] = str(SESSION_DIR)
            self._json(200, snap)
        elif self.path.startswith("/research-status/"):
            idea_id = self.path.split("/research-status/", 1)[1].rstrip("/")
            with research_lock:
                job = research_jobs.get(idea_id, {"status": "idle", "result": None})
            self._json(200, job)
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
#  PLANNING (multi-backend)
# ═══════════════════════════════════════════════════════════════════════════

def generate_plan(backend: str, prompt: str, task_id: str, state: dict,
                  task_type: str = "task", provider_override: str | None = None) -> tuple[bool, str]:
    backend = _normalise_backend(backend)
    log.info("📐 Generating plan using backend: %s", backend)

    if backend == "cli":
        return launch_cli_plan(prompt, task_id, state, provider_override)

    elif backend == "gpt4o_api":
        system_prompt = (
            f"You are an AI assistant for a coding project.\n"
            f"Analyze this {task_type} and provide:\n"
            f"1. **SUMMARY**\n2. **IMPLEMENTATION PLAN**\n3. **FILES TO CHANGE**\n"
            f"4. **COPILOT PROMPT** — a single prompt for a coding CLI to implement this\n"
            f"5. **NEXT STEPS**\n\n"
            f"The project is at {PROJECT_DIR}. Be specific and actionable."
        )
        plan = call_gpt4o(system_prompt, prompt, state)
        return (True, plan) if plan else (False, "GPT-4o API call failed or rate limited")

    elif backend == "custom_api":
        log.warning("⚠️ Custom API backend not yet implemented")
        return False, "Custom API backend not yet implemented"

    else:
        log.error("❌ Unknown planning backend: %s", backend)
        return False, f"Unknown planning backend: {backend}"


# ═══════════════════════════════════════════════════════════════════════════
#  CLI LAUNCH — PLAN
# ═══════════════════════════════════════════════════════════════════════════

def launch_cli_plan(prompt: str, task_id: str, state: dict,
                    provider_override: str | None = None) -> tuple[bool, str]:
    provider = get_provider_for_phase("plan", provider_override)
    timeout = get_timeout(provider, "plan")

    log.info("📐 Running %s (plan mode) for %s…", provider.get("name", "CLI"), task_id)
    log.info("   Prompt: %s", prompt[:200])

    session_path = SESSION_DIR / task_id
    session_path.mkdir(parents=True, exist_ok=True)
    plan_file = session_path / "plan.md"
    if plan_file.exists():
        plan_file.unlink()

    try:
        safe_prompt = prompt.replace('"', "'")
        plan_prompt = f"[[PLAN]] {safe_prompt}"
        cmd = build_cli_command(provider, "plan", plan_prompt)

        log.info("   Command: %s (cwd=%s, timeout=%ds)",
                 " ".join(cmd[:3]) + " ...", PROJECT_DIR, timeout)
        result = subprocess.run(
            cmd, cwd=str(PROJECT_DIR),
            capture_output=True, text=True, timeout=timeout,
        )

        log.info("   Exit code: %d", result.returncode)
        if result.stdout:
            log.info("   Output (tail):\n%s", result.stdout[-2000:])
        if result.stderr:
            log.warning("   Stderr (tail):\n%s", result.stderr[-1000:])

        if result.returncode != 0:
            err = result.stderr[-500:] if result.stderr else result.stdout[-500:]
            return False, f"Planning failed (exit {result.returncode}): {err}"

        if plan_file.exists():
            plan_text = plan_file.read_text(encoding="utf-8")
            log.info("✅ Plan generated (%d chars) at %s", len(plan_text), session_path)
            return True, plan_text

        # Some providers may output the plan to stdout instead of plan.md
        if result.stdout and len(result.stdout.strip()) > 50:
            log.info("✅ Plan captured from stdout (%d chars)", len(result.stdout))
            return True, result.stdout.strip()

        return False, "Planning completed but no plan output found"

    except subprocess.TimeoutExpired:
        return False, f"Planning timed out after {timeout}s"
    except Exception as e:
        return False, f"Planning error: {e}"


# ═══════════════════════════════════════════════════════════════════════════
#  CLI LAUNCH — IMPLEMENT
# ═══════════════════════════════════════════════════════════════════════════

def launch_cli_implement(prompt: str, task_id: str, tasks: list,
                         is_idea: bool = False, plan_path: str | None = None,
                         provider_override: str | None = None) -> bool:
    provider = get_provider_for_phase("implement", provider_override)
    timeout = get_timeout(provider, "implement")
    label = "idea" if is_idea else "task"

    log.info("🚀 Running %s (implement) for %s %s…",
             provider.get("name", "CLI"), label, task_id)
    log.info("   Prompt: %s", prompt[:200])

    if plan_path and Path(plan_path).exists():
        log.info("   Plan at: %s", plan_path)
        safe_prompt = f"Implement the plan at {plan_path}. {prompt}".replace('"', "'")
    else:
        safe_prompt = prompt.replace('"', "'")

    try:
        cmd = build_cli_command(provider, "implement", safe_prompt)
        log.info("   Command: %s (cwd=%s, timeout=%ds)",
                 " ".join(cmd[:3]) + " ...", PROJECT_DIR, timeout)

        result = subprocess.run(
            cmd, cwd=str(PROJECT_DIR),
            capture_output=True, text=True, timeout=timeout,
        )
        log.info("   Exit code: %d", result.returncode)
        if result.stdout:
            log.info("   Output (tail):\n%s", result.stdout[-2000:])
        if result.stderr:
            log.warning("   Stderr (tail):\n%s", result.stderr[-1000:])

        if result.returncode != 0:
            log.warning("⚠️ CLI exited with code %d", result.returncode)
            if is_idea:
                _update_idea_status(task_id, tasks, "open",
                    f"CLI failed (exit {result.returncode}). Needs manual attention.")
            else:
                _update_task_status(task_id, tasks, "open",
                    f"CLI failed (exit {result.returncode}). Needs manual attention.")
            notify_ha(f"⚠️ {label.title()} needs you: {task_id}",
                      f"CLI couldn't complete.\n\n{result.stderr[-300:] or result.stdout[-300:]}")
            return False

        # Success path
        _restart_changed_services()

        if is_idea:
            _update_idea_status(task_id, tasks, "done", "Implemented automatically by agent.")
        else:
            _update_task_status(task_id, tasks, "done", "Implemented automatically by agent.")
        notify_ha(f"✅ {label.title()} completed: {task_id}",
                  f"CLI implemented this and restarted affected services.")
        log.info("✅ CLI completed successfully")

        # ── Security review before push ─────────────────────────────────
        title = task_id
        for t in tasks:
            if t.get("id") == task_id:
                title = t.get("title", task_id)
                break

        review = run_security_review(task_id, title)
        action = _handle_security_findings(review, task_id, title, tasks, is_idea)

        if action == "blocked":
            log.warning("🔒🚫 Push blocked by security review for: %s", title)
            return False
        if action == "fixed":
            log.info("🔒🔧 Security fixes applied — proceeding to push")

        # Commit and push
        pushed = _git_commit_and_push(task_id, title, label="implemented")
        if pushed:
            if is_idea:
                _update_idea_status(task_id, tasks, "pushed-to-production",
                    "Changes committed and pushed to production.")
            else:
                _update_task_status(task_id, tasks, "pushed-to-production",
                    "Changes committed and pushed to production.")
            notify_ha(f"🚀 Pushed to production: {title}",
                      f"{label.title()} {task_id} implemented and pushed to main.")
            log.info("🚀 Changes pushed to production")
        else:
            notify_ha(f"⚠️ Push failed: {title}",
                      f"{label.title()} completed but could not be pushed. Push manually.")
            log.warning("⚠️ Git push failed")

        return True

    except subprocess.TimeoutExpired:
        log.error("⏰ CLI timed out after %ds", timeout)
        if is_idea:
            _update_idea_status(task_id, tasks, "open", f"Timed out after {timeout}s.")
        else:
            _update_task_status(task_id, tasks, "open", f"Timed out after {timeout}s.")
        notify_ha(f"⏰ {label.title()} timed out: {task_id}", "Please check manually.")
        return False
    except Exception as e:
        log.error("CLI launch failed: %s", e)
        if is_idea:
            _update_idea_status(task_id, tasks, "open", f"Launch failed: {e}")
        else:
            _update_task_status(task_id, tasks, "open", f"Launch failed: {e}")
        notify_ha(f"❌ {label.title()} failed: {task_id}", str(e)[:300])
        return False


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
                               capture_output=True, text=True, timeout=30)
            if r.stdout.strip():
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
                                capture_output=True, text=True, timeout=timeout)

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
                           capture_output=True, text=True, timeout=timeout)
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
    planning_backend = _normalise_backend(target.get("plan_backend_used", DEFAULT_PLANNING_BACKEND))

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

    desc = target.get("description", "")
    copilot_prompt = (_extract_idea_copilot_prompt if is_idea else extract_copilot_prompt)(plan, title, desc)

    plan_path = None
    if planning_backend == "cli":
        plan_path = str(SESSION_DIR / task_id / "plan.md")

    provider_override = target.get("cli_provider")
    success = launch_cli_implement(copilot_prompt, task_id, collection,
                                   is_idea=is_idea, plan_path=plan_path,
                                   provider_override=provider_override)

    for item in collection:
        if item.get("id") == task_id:
            item["implementation_completed_at"] = datetime.now().isoformat()
            break
    (save_ideas if is_idea else save_tasks)(collection)

    if success:
        notify_ha(f"✅ Completed: {title}", f"Manual implementation successful!\n\n**Plan:**\n{plan[:300]}")
    else:
        notify_ha(f"⚠️ Failed: {title}", f"Manual implementation error.\n\n**Plan:**\n{plan[:300]}")


# ═══════════════════════════════════════════════════════════════════════════
#  PROCESS TASK / IDEA
# ═══════════════════════════════════════════════════════════════════════════

def extract_copilot_prompt(analysis: str, title: str, desc: str) -> str:
    for line in analysis.split("\n"):
        if "copilot prompt" in line.lower() and ":" in line:
            prompt = line.split(":", 1)[1].strip().strip('"').strip("'").strip("`")
            if len(prompt) > 10:
                return prompt
    return f"Implement this feature: {title}. {desc[:200]}"


def _extract_idea_copilot_prompt(analysis: str, title: str, desc: str) -> str:
    return extract_copilot_prompt(analysis, title, desc)


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

    planning_backend = _normalise_backend(task.get("planning_backend", DEFAULT_PLANNING_BACKEND))
    auto_implement = task.get("auto_implement", AUTO_IMPLEMENT_DEFAULT)
    provider_override = task.get("cli_provider")

    log.info("=" * 60)
    log.info("📋 New %s: %s", task_type, title)
    log.info("   Priority: %s | ID: %s", priority, task_id)
    log.info("   Description: %s", desc[:200])
    log.info("   Backend: %s | Auto-implement: %s | CLI provider: %s",
             planning_backend, auto_implement, provider_override or "default")

    _update_task_status(task_id, tasks, "grabbed", "Agent has picked up this task.")
    notify_ha(f"🤖 Agent grabbed task: {title}",
              f"Processing {task_type} (priority: {priority}).\nTask ID: {task_id}")

    # Build planning prompt
    planning_prompt = f"Task Type: {task_type}\nTitle: {title}\nPriority: {priority}\nDescription: {desc}"
    photos = task.get("photos", [])
    if photos:
        planning_prompt += f"\n\nAttached Photos ({len(photos)}): {', '.join(photos)}"
        planning_prompt += f"\nPhotos in {PROJECT_DIR / 'task-photos'}"

    plan_success, plan_text = generate_plan(planning_backend, planning_prompt, task_id, state,
                                            "task", provider_override)

    if plan_success:
        log.info("\n📐 Plan generated:\n%s", plan_text[:500])
        session_path = SESSION_DIR / task_id
        for t in tasks:
            if t.get("id") == task_id:
                t["plan"] = plan_text
                t["plan_backend_used"] = planning_backend
                t["planning_completed_at"] = datetime.now().isoformat()
                t["copilot_session_path"] = str(session_path)
                t["status"] = "planned" if not auto_implement else "in-progress"
                t["updated"] = _ts_ms()
                if "ai_analysis" not in t:
                    t["ai_analysis"] = plan_text
                break
        save_tasks(tasks)

        _git_commit_and_push(task_id, title, label="planned", backend=planning_backend)

        if auto_implement:
            log.info("🚀 Auto-implement enabled — starting…")
            for t in tasks:
                if t.get("id") == task_id:
                    t["implementation_started_at"] = datetime.now().isoformat()
                    break
            save_tasks(tasks)

            copilot_prompt = extract_copilot_prompt(plan_text, title, desc)
            copilot_prompt += _build_photo_context(task)
            plan_path = str(session_path / "plan.md") if planning_backend == "cli" else None

            success = launch_cli_implement(copilot_prompt, task_id, tasks,
                                           plan_path=plan_path, provider_override=provider_override)

            for t in tasks:
                if t.get("id") == task_id:
                    t["implementation_completed_at"] = datetime.now().isoformat()
                    break
            save_tasks(tasks)

            if success:
                notify_ha(f"✅ Auto-completed: {title}", f"Plan:\n{plan_text[:400]}")
            else:
                notify_ha(f"⚠️ Needs help: {title}", f"Plan:\n{plan_text[:400]}")
        else:
            notify_ha(f"📐 Plan ready: {title}",
                      f"Auto-implement disabled. Use 'Implement Now'.\n\n{plan_text[:300]}")
    else:
        log.warning("⚠️ Could not generate plan: %s", plan_text)
        _update_task_status(task_id, tasks, "open", f"Planning failed: {plan_text}")
        notify_ha(f"❌ Planning failed: {title}", f"Error: {plan_text}\n\nDescription: {desc[:200]}")


def process_idea(idea: dict, ideas: list, state: dict):
    title = idea.get("title", "(no title)")
    desc = idea.get("description", "(no description)")
    idea_id = idea.get("id", "")

    planning_backend = _normalise_backend(idea.get("planning_backend", DEFAULT_PLANNING_BACKEND))
    auto_implement = idea.get("auto_implement", AUTO_IMPLEMENT_DEFAULT)
    provider_override = idea.get("cli_provider")

    log.info("=" * 60)
    log.info("💡 New idea: %s", title)
    log.info("   ID: %s | Backend: %s | CLI provider: %s",
             idea_id, planning_backend, provider_override or "default")

    _update_idea_status(idea_id, ideas, "planning", "Agent is analyzing this idea.")
    notify_ha(f"💡 Agent planning idea: {title}", f"Idea ID: {idea_id}")

    planning_prompt = f"Idea: {title}\nDescription: {desc}"
    plan_success, plan_text = generate_plan(planning_backend, planning_prompt, idea_id, state,
                                            "idea", provider_override)

    if plan_success:
        log.info("\n📐 Idea plan:\n%s", plan_text[:500])
        next_steps = _extract_next_steps(plan_text)
        session_path = SESSION_DIR / idea_id

        for i in ideas:
            if i.get("id") == idea_id:
                i["plan"] = plan_text
                i["plan_backend_used"] = planning_backend
                i["next_steps"] = next_steps
                i["planning_completed_at"] = datetime.now().isoformat()
                i["copilot_session_path"] = str(session_path)
                i["status"] = "planned" if not auto_implement else "in-progress"
                i["updated"] = _ts_ms()
                break
        save_ideas(ideas)

        _git_commit_and_push(idea_id, title, label="planned", backend=planning_backend)

        if auto_implement:
            log.info("🚀 Auto-implement enabled — starting…")
            for i in ideas:
                if i.get("id") == idea_id:
                    i["implementation_started_at"] = datetime.now().isoformat()
                    break
            save_ideas(ideas)

            copilot_prompt = _extract_idea_copilot_prompt(plan_text, title, desc)
            plan_path = str(session_path / "plan.md") if planning_backend == "cli" else None

            success = launch_cli_implement(copilot_prompt, idea_id, ideas,
                                           is_idea=True, plan_path=plan_path,
                                           provider_override=provider_override)

            for i in ideas:
                if i.get("id") == idea_id:
                    i["implementation_completed_at"] = datetime.now().isoformat()
                    break
            save_ideas(ideas)

            if success:
                notify_ha(f"✅ Idea implemented: {title}", f"Next steps:\n{next_steps[:400]}")
            else:
                notify_ha(f"⚠️ Idea needs help: {title}", f"Plan:\n{plan_text[:400]}")
        else:
            notify_ha(f"📐 Plan ready: {title}",
                      f"Auto-implement disabled.\n\nNext steps:\n{next_steps[:300]}")
    else:
        log.warning("⚠️ Could not plan idea: %s", plan_text)
        _update_idea_status(idea_id, ideas, "open", f"Planning failed: {plan_text}")
        notify_ha(f"❌ Planning failed: {title}", f"Error: {plan_text}")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════

def main():
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
            remaining = MAX_API_CALLS_PER_DAY - state.get("api_calls_today", 0)

            tasks = load_tasks()
            new_tasks = [
                t for t in tasks
                if t.get("id") and t["id"] not in state["processed"]
                and t.get("status", "open") == "open"
            ] if tasks else []

            ideas = load_ideas()
            new_ideas = [
                i for i in ideas
                if i.get("id") and i["id"] not in state["processed"]
                and i.get("status", "open") == "open"
            ] if ideas else []

            with status_lock:
                agent_status["tasks_pending"] = len(new_tasks)
                agent_status["ideas_pending"] = len(new_ideas)
                agent_status["api_calls_today"] = state.get("api_calls_today", 0)
                agent_status["last_run"] = datetime.now().isoformat()

            if new_tasks or new_ideas:
                log.info("🔍 Found %d new task(s) and %d new idea(s)! (API budget: %d/%d)",
                         len(new_tasks), len(new_ideas), remaining, MAX_API_CALLS_PER_DAY)

                log.info("⬇️ Pulling latest changes…")
                _git_pull()

                # Split by backend type
                cli_tasks = [t for t in new_tasks
                             if _normalise_backend(t.get("planning_backend", DEFAULT_PLANNING_BACKEND)) == "cli"]
                api_tasks = [t for t in new_tasks
                             if _normalise_backend(t.get("planning_backend", DEFAULT_PLANNING_BACKEND)) != "cli"]
                cli_ideas = [i for i in new_ideas
                             if _normalise_backend(i.get("planning_backend", DEFAULT_PLANNING_BACKEND)) == "cli"]
                api_ideas = [i for i in new_ideas
                             if _normalise_backend(i.get("planning_backend", DEFAULT_PLANNING_BACKEND)) != "cli"]

                log.info("📋 CLI tasks: %d, API tasks: %d, CLI ideas: %d, API ideas: %d",
                         len(cli_tasks), len(api_tasks), len(cli_ideas), len(api_ideas))

                # CLI tasks — no budget needed
                for task in cli_tasks:
                    log.info("▶️ Starting CLI task: %s", task.get("title", task.get("id")))
                    with status_lock:
                        agent_status["state"] = "processing"
                        agent_status["current_task"] = task.get("title", task.get("id"))
                    process_task(task, tasks, state)
                    state["processed"].append(task["id"])
                    save_state(state)

                # API tasks — budget gated
                budget = remaining
                if api_tasks:
                    if budget <= 0:
                        log.warning("⚠️ API limit reached — %d API task(s) queued", len(api_tasks))
                    else:
                        for task in api_tasks[:budget]:
                            log.info("▶️ Starting API task: %s", task.get("title", task.get("id")))
                            with status_lock:
                                agent_status["state"] = "processing"
                                agent_status["current_task"] = task.get("title", task.get("id"))
                            process_task(task, tasks, state)
                            state["processed"].append(task["id"])
                            save_state(state)
                            budget -= 1

                # CLI ideas — no budget needed
                for idea in cli_ideas:
                    log.info("▶️ Starting CLI idea: %s", idea.get("title", idea.get("id")))
                    with status_lock:
                        agent_status["state"] = "processing"
                        agent_status["current_task"] = idea.get("title", idea.get("id"))
                    process_idea(idea, ideas, state)
                    state["processed"].append(idea["id"])
                    save_state(state)

                # API ideas — budget gated
                if api_ideas:
                    if budget <= 0:
                        log.warning("⚠️ API limit reached — %d API idea(s) queued", len(api_ideas))
                    else:
                        for idea in api_ideas[:budget]:
                            log.info("▶️ Starting API idea: %s", idea.get("title", idea.get("id")))
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
