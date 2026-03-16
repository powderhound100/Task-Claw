# Task-Claw Pipeline Reference

Complete documentation for the multi-agent pipeline — every stage, every safety gate, every configuration knob.

---

## Table of Contents

1. [Overview](#overview)
2. [How the PM Manages the Team](#how-the-pm-manages-the-team)
3. [Stage Reference](#stage-reference)
   - [Rewrite](#1-rewrite)
   - [Plan](#2-plan)
   - [Code](#3-code)
   - [Simplify](#4-simplify)
   - [Test](#5-test)
   - [Review](#6-review)
   - [Publish](#7-publish)
4. [Multi-Agent Team Execution](#multi-agent-team-execution)
5. [Cross-Review and Deep Merge](#cross-review-and-deep-merge)
6. [PM Failover and Direct Mode](#pm-failover-and-direct-mode)
7. [Safety Gates](#safety-gates)
8. [Automatic Recovery Loops](#automatic-recovery-loops)
9. [Context Management](#context-management)
10. [Configuration Reference](#configuration-reference)

---

## Overview

The pipeline is a **PM-supervised, multi-agent assembly line**. Think of it as a small engineering org that spins up for every task:

```
Your Prompt
    ↓
[Rewrite]  → PM sharpens the request into a clear spec
[Plan]     → Team plans · PM quality-gates              ↺ REVISE if needed
[Code]     → Parallel implementation · cross-review      ↺ REVISE if needed
[Simplify] → Refactor for quality and efficiency
[Test]     → Automated testing · failure loopback        ↺ REVISE if needed
[Review]   → Security audit · HIGH severity blocks push
[Publish]  → git commit + push
```

**Roles in the org:**

| Role | Who | What they do |
|------|-----|--------------|
| Program Manager | API-backed LLM (GPT-4o, Claude, etc.) | Writes briefs, quality-gates output, merges competing implementations |
| Developers | CLI agents (Claude Code, Copilot, Aider, etc.) | Stateless — receive a brief, produce output |
| QA | Same CLI agents, test phase | Run tests, report failures |
| Security Reviewer | Structured security audit via CLI | Categorized audit with LOW/MEDIUM/HIGH ratings |

**Skip paths** — stages that don't need to run are skipped automatically:
- Code stage produces no file diff → skip Simplify and Test
- `start_stage` parameter → skip all stages before the named one
- Stage `"enabled": false` in `pipeline.json` → skip
- Pipeline wallclock timeout exceeded → abort remaining stages

---

## How the PM Manages the Team

The Program Manager is not a rubber-stamper. It plays three distinct roles at every stage:

### 1. Director — writes the task brief

Before the team runs, the PM writes a targeted brief that includes:
- What the stage needs to produce
- Relevant context from all prior stages
- Quality criteria and constraints

This is what gets sent to the CLI agents, not the raw user prompt.

### 2. Team runs in parallel

CLI agents are stateless tools. They each receive the PM's brief and work independently. When multiple agents are configured, they run concurrently via `ThreadPoolExecutor`. Individual agent failure doesn't kill the stage — the other agents' output is still used.

### 3. Overseer — quality gates the output

After the team runs, the PM reviews the output and returns:
- **APPROVE** → output meets the bar, move to the next stage
- **REVISE** → output has gaps, bugs, or drift; re-run the stage with PM feedback

The PM checks:
- Are all requirements from the original prompt met?
- Is there any scope drift from the plan?
- Is the output complete and correct?
- Are there any obvious bugs or security issues?

If the PM returns REVISE, the stage re-runs with the issues appended to the prompt (up to `PIPELINE_MAX_REVISE` attempts, default 1). After the maximum, the pipeline uses the best available output and continues.

---

## Stage Reference

### 1. Rewrite

**Purpose:** Turn a vague user prompt into a precise, actionable spec before any agent sees it.

**Who runs it:** PM only (no CLI team). If PM is unavailable, uses the original prompt unchanged.

**What the PM does:**
- Converts exploratory requests ("look into why X is slow") into concrete coding tasks ("profile X and fix the root cause")
- Structures the output as: WHAT, WHERE, WHY, CONSTRAINTS
- Returns only the rewritten prompt — no commentary

**What gets saved:** `pipeline-output/{id}/rewrite.md`

**Configuration:**
```json
"stages": {
  "rewrite": {
    "enabled": true,
    "timeout": 120
  }
}
```

**Optional: requirement extraction**

If `extract_requirements: true` is set in the `program_manager` config, the PM extracts a numbered list of discrete, testable requirements from the rewritten prompt. These are carried through the pipeline and checked at each oversight step.

---

### 2. Plan

**Purpose:** Produce a step-by-step implementation plan before any code is written.

**Who runs it:** CLI team (default: `["claude"]`)

**Prompt format:**
```
Understand the codebase before making changes.

Task context: {rewritten_prompt}

Output a step-by-step implementation plan with:
- Actionable verb-led steps with specific files
- Incremental build order
- Testing strategy
```

**PM oversight:** The PM checks that the plan covers all requirements extracted in the Rewrite stage. REVISE if any are missing or if the approach is technically incorrect.

**What gets saved:** `pipeline-output/{id}/plan.md`

**Configuration:**
```json
"stages": {
  "plan": {
    "enabled": true,
    "team": ["claude"],
    "timeout": 900
  }
}
```

---

### 3. Code

**Purpose:** Implement the plan. The most complex stage — supports parallel agents, cross-review, and PM deep merge.

**Who runs it:** CLI team (configure multiple agents for cross-review)

**Single agent:** The agent implements the plan, the PM oversees and approves or revises.

**Multiple agents:** Each agent implements independently. Then:
1. Each agent reviews the other's code (cross-review)
2. The PM deep-merges both implementations using the cross-review findings
3. PM verdict: APPROVE or REVISE

See [Cross-Review and Deep Merge](#cross-review-and-deep-merge) for details.

**After the code stage runs**, the pipeline checks `git diff` for actual file changes. If no files changed, Simplify and Test are skipped — there's nothing to verify.

**What gets saved:**
- `pipeline-output/{id}/code.md` — merged or single-agent output
- `pipeline-output/{id}/code-{agent}.md` — per-agent output in multi-agent mode

**Configuration:**
```json
"stages": {
  "code": {
    "enabled": true,
    "team": ["claude", "copilot"],
    "timeout": 600
  }
}
```

---

### 4. Simplify

**Purpose:** Refactor the code produced in the Code stage for quality, efficiency, and cleanliness.

**Who runs it:** CLI team

**What the agent does:**
- Runs `git diff` to see recent changes
- Reviews for: duplicate logic, bad naming, empty catch blocks, dead code, deep nesting
- Applies fixes — capped at 3 fix iterations per file
- Reports what was changed

**Skipped when:** Code stage produced no file changes.

**PM oversight:** Standard — APPROVE or REVISE.

**What gets saved:** `pipeline-output/{id}/simplify.md`

**Configuration:**
```json
"stages": {
  "simplify": {
    "enabled": true,
    "team": ["claude"],
    "timeout": 300
  }
}
```

---

### 5. Test

**Purpose:** Verify the implementation works correctly.

**Who runs it:** CLI team

**What the agent does:**
- Runs `git diff` to see recent changes
- Runs existing tests or writes and runs targeted tests
- Reports specific pass/fail results
- If tests fail: fixes the code (not the tests)

**Failure loopback:** If the test output contains failure patterns (`FAILED`, `Error:`, `AssertionError`, etc.), the pipeline re-runs the Code stage with targeted failure context before continuing. See [Automatic Recovery Loops](#automatic-recovery-loops).

**Skipped when:** Code stage produced no file changes.

**Publish gate:** If the test stage reports failures and the loopback doesn't resolve them, `test_passed = False`. The Publish stage is skipped.

**What gets saved:** `pipeline-output/{id}/test.md`

**Configuration:**
```json
"stages": {
  "test": {
    "enabled": true,
    "team": ["claude"],
    "timeout": 300
  }
}
```

---

### 6. Review

**Purpose:** Structured security audit of every change before it ships.

**Who runs it:** Structured audit via CLI (not a general prompt — specific categories checked)

**Security categories audited:**

| # | Category |
|---|----------|
| 1 | Input validation |
| 2 | SQL / command injection |
| 3 | XSS / output encoding |
| 4 | File system access |
| 5 | Authentication / authorization |
| 6 | Error handling / info leakage |
| 7 | Dependency vulnerabilities |

**Severity ratings:** Each finding is rated LOW, MEDIUM, or HIGH.

**HIGH severity blocks publish.** The task is marked `security-blocked` and the pipeline returns early without committing.

**PM oversight:** The PM reviews the security report and provides a verdict. Even if the PM approves, HIGH severity findings still block publish — the PM cannot override the security gate.

**What gets saved:**
- `pipeline-output/{id}/review.md`
- `security-reviews/{id}.json` — structured JSON report

**Configuration:**
```json
"publish": {
  "block_on_severity": "high"
}
```

---

### 7. Publish

**Purpose:** Commit and push the changes to the remote repository.

**Gates that must pass:**
- `test_passed` must be `True` (no unresolved test failures)
- Security review must not have returned HIGH severity findings

**What it does:**
1. `git pull` to sync with remote
2. Stage all changed files
3. Commit with a message referencing the task/pipeline ID
4. Push to the remote branch

**If either gate fails:** Publish is skipped. The pipeline returns `{"published": false}`. The task is marked `security-blocked` or left in `in-progress` depending on the failure type.

**Configuration:**
```json
"publish": {
  "enabled": true,
  "auto_push": true,
  "block_on_severity": "high"
}
```

Set `"auto_push": false` to commit without pushing, or `"enabled": false` to skip the entire stage.

---

## Multi-Agent Team Execution

Any pipeline stage can run multiple agents in parallel by listing more than one provider in the `team` array:

```json
"code": {
  "team": ["claude", "copilot"],
  "timeout": 600
}
```

**How it works:**

```
PM Task Brief
    |
    +──────────────────────┐
    ↓                      ↓
[claude]              [copilot]      ← both get same brief, run concurrently
    |                      |
    +──────────────────────+
                 ↓
         collect results
         (agent failure = skip that agent, use others)
```

Each agent is stateless — it gets the PM's brief, works in its own subprocess, and returns output. The `ThreadPoolExecutor` timeout is set to the stage timeout plus a 30-second buffer for cleanup.

**For the Code stage with 2+ agents**, the parallel outputs feed into [Cross-Review and Deep Merge](#cross-review-and-deep-merge) rather than going straight to PM oversight.

For all other stages, the PM oversees the combined outputs from all agents.

---

## Cross-Review and Deep Merge

When 2 or more agents produce code implementations, Task-Claw runs a debate before the PM merges:

### Step 1: Cross-review (parallel)

Each agent reviews the other's implementation using a structured format:

```
## Agreement Points
(shared approaches, consistent decisions)

## Divergences
(differences with file:line references)

## Issues Found
(bugs, security problems, convention violations)

## Winner Per Component
(which implementation is better for each feature, and why)

## Recommended Merge
(how to combine the best of both)
```

Agent A reviews Agent B's code. Agent B reviews Agent A's code. Both run concurrently.

### Step 2: PM deep merge

The PM receives:
- Agent A's full implementation
- Agent B's full implementation
- Agent A's review of B
- Agent B's review of A

The PM then performs a 5-step merge:
1. **Compare** — what does each implementation do differently?
2. **Gap analysis** — what did A catch that B missed, and vice versa?
3. **Strength mapping** — the best contribution from each agent
4. **Merged result** — unified implementation taking the best of both
5. **Verdict** — APPROVE if production-ready, REVISE if not

### Why this works

Two agents independently implementing the same spec will disagree on things — data structures, error handling, naming, edge cases. The disagreements surface the hard decisions that a single agent would have made silently. Cross-review forces those decisions to be explicit, and the PM merge produces an implementation that's better than either agent would have written alone.

---

## PM Failover and Direct Mode

The PM is an API-backed LLM. When the API is unavailable (rate limit, network issue, missing key), Task-Claw switches to **direct mode** rather than failing.

### Failover cascade

```
PM health check (config validation, no API call wasted)
    |
    +── PM config OK?
         |
        no ──→ DIRECT MODE immediately (log warning)
         |
        yes
         |
         ↓
    Each PM API call attempt
         |
         +── Success? ──→ reset failure count
         |
         +── Failure? ──→ pm_consecutive_failures++
                              |
                              +── failures >= 2? ──→ DIRECT MODE for rest of pipeline
                              |
                              +── failures < 2? ──→ continue trying PM
```

### What changes in direct mode

In direct mode, the pipeline skips all PM API calls and sends actionable prompts directly to the CLI team:

| Stage | Direct mode prompt |
|-------|--------------------|
| Plan | "Output an implementation plan — do not edit files." |
| Code | The rewritten prompt (task only — Claude knows how to code) |
| Simplify | "Run git diff, review for quality, fix issues, cap at 3 iterations per file." |
| Test | "Run git diff, verify changes work, report pass/fail." |
| Review | "Run git diff, perform a security audit, rate findings LOW/MEDIUM/HIGH." |

Garbage detection still runs in direct mode. If an agent returns a permission request or a question instead of doing the work, the stage retries with cleaned context before continuing.

---

## Safety Gates

### Gate 1: Code stage produced no changes

If `git diff` after the Code stage shows no changed files, the pipeline skips Simplify and Test. Nothing was implemented — there's nothing to verify.

### Gate 2: Test failures block publish

If the Test stage reports failures and the test→code loopback doesn't resolve them, `test_passed = False`. The Publish stage is skipped entirely. The pipeline still completes successfully — it just doesn't push broken code.

### Gate 3: HIGH security findings block publish

The security review runs regardless of test results. If any finding is rated HIGH, the Publish stage is skipped and the task is marked `security-blocked`. The security report is saved to `security-reviews/{id}.json` for review.

### Gate 4: Garbage detection blocks PM approval

If a CLI agent returns a garbage response (permission prompt, a question, "could you approve/grant/describe..."), the PM is not called to oversee it. The garbage is detected before PM oversight, and the stage retries with a clean prompt. This prevents the PM from rubber-stamping output that was never produced.

**Garbage scoring:**

| Signal | Points | When counted |
|--------|--------|--------------|
| Strong patterns (`"could you approve"`, `"write permission"`, `"please describe"`) | 2 | Always |
| Weak patterns (short questions, `"?"`) | 1 | Only if output < 500 chars or contains `?` |
| Trivially short (< 100 chars total) | auto-flag | Always |

Score ≥ 2 → garbage.

---

## Automatic Recovery Loops

### Test → Code loopback

When test failures are detected, the pipeline loops back to re-run the Code stage with targeted context:

```
Test stage output
    ↓
_test_found_failures() — word-boundary matching to avoid false positives
    ↓
    +── no failures ──→ continue to Review
    |
    +── failures detected
         ↓
    _extract_test_failures() — extracts just the failure block (≤ 4000 chars)
         ↓
    Re-run Code stage:
    "Fix ONLY the failing tests — do not rewrite unrelated code.
     Original task: {prompt}
     Test failures: {failures}"
         ↓
    Re-check for failures in fix output
         ↓
    test_passed = (no failures found in fix)
```

### Garbage retry

When a CLI agent returns garbage in direct mode:

```
CLI output
    ↓
_is_garbage_output() → score >= 2?
    ↓
    +── no ──→ use output
    |
    +── yes
         ↓
    _extract_plan_context() — extract just the plan section from context
         ↓
    Rebuild clean direct prompt (no garbage in context)
         ↓
    Retry stage
         ↓
    +── still garbage ──→ accept empty, log warning, continue
    |
    +── clean output ──→ use retry output
```

### PM revision loop

```
PM oversees stage output
    ↓
verdict = APPROVE or REVISE
    ↓
    +── APPROVE ──→ move to next stage
    |
    +── REVISE
         ↓
         attempt < PIPELINE_MAX_REVISE?
         ↓
         +── yes ──→ append issues to context, re-run stage
         |
         +── no  ──→ accept current output, log warning, continue
```

---

## Context Management

Every stage appends its output to a running `context` string passed forward through the pipeline. This gives each stage awareness of what happened before.

**Context format:**
```
=== Plan stage output ===
{plan handoff summary}
=== End plan ===

=== Code stage output ===
{code handoff summary}
=== End code ===
```

**Context cap:** The context is capped at 12,000 characters before each stage. When it exceeds the cap:
1. The context is split on `=== ... ===` delimiters
2. The **plan section is always preserved** (last to be dropped — it's the blueprint)
3. Oldest non-plan sections are evicted first

**Output cleaning:** Before appending to context, each stage output is cleaned with `_clean_stage_output()`, which strips lines matching garbage patterns. This prevents garbage from contaminating downstream stages even when the garbage wasn't enough to trigger a retry.

---

## Configuration Reference

### pipeline.json

```json
{
  "program_manager": {
    "backend": "github_models",
    "model": "gpt-4o",
    "max_tokens": 4096,
    "temperature": 0.3,
    "extract_requirements": false
  },
  "stages": {
    "rewrite":  { "enabled": true, "timeout": 120 },
    "plan":     { "enabled": true, "team": ["claude"], "timeout": 900 },
    "code":     { "enabled": true, "team": ["claude"], "timeout": 600 },
    "simplify": { "enabled": true, "team": ["claude"], "timeout": 300 },
    "test":     { "enabled": true, "team": ["claude"], "timeout": 300 },
    "review":   { "enabled": true, "team": ["claude"], "timeout": 300 }
  },
  "publish": {
    "enabled": true,
    "auto_push": true,
    "block_on_severity": "high"
  },
  "hooks": {
    "on_stage_start": [],
    "on_stage_end": [],
    "on_verdict": []
  }
}
```

**PM backends:**

| Backend | Key | Required credential |
|---------|-----|---------------------|
| GitHub Models (GPT-4o) | `github_models` | `GITHUB_TOKEN` |
| Anthropic API (Claude) | `anthropic` | `ANTHROPIC_API_KEY` |
| Any OpenAI-compatible endpoint | `openai_compatible` | `PIPELINE_PM_URL` + optional `PIPELINE_PM_KEY` |

### Key environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PROJECT_DIR` | agent dir | The project the agents edit |
| `GITHUB_TOKEN` | — | GitHub Models PM backend + git push auth |
| `ANTHROPIC_API_KEY` | — | Anthropic PM backend |
| `CLI_PROVIDER` | `claude` | Default provider for all stages |
| `CLI_PLAN_PROVIDER` | — | Override provider for Plan stage |
| `CLI_IMPLEMENT_PROVIDER` | — | Override provider for Code + Simplify stages |
| `CLI_TEST_PROVIDER` | — | Override provider for Test stage |
| `CLI_SECURITY_PROVIDER` | — | Override provider for security audit |
| `CLI_REVIEW_PROVIDER` | — | Override provider for Review stage |
| `PIPELINE_MAX_REVISE` | `1` | Max PM revision attempts per stage |
| `PIPELINE_MANAGER_TIMEOUT` | `300` | PM API call timeout in seconds |
| `PIPELINE_WALLCLOCK_TIMEOUT` | `3600` | Total pipeline time limit in seconds |
| `PIPELINE_CODE_TIMEOUT` | — | Override Code stage timeout |
| `PIPELINE_PLAN_TIMEOUT` | — | Override Plan stage timeout |
| `PIPELINE_TEST_TIMEOUT` | — | Override Test stage timeout |
| `PIPELINE_REVIEW_TIMEOUT` | — | Override Review stage timeout |

### Provider resolution priority

For each stage, the provider is resolved in this order (first match wins):

1. Per-task `cli_provider` field in the task JSON
2. Phase-specific env var (`CLI_PLAN_PROVIDER`, `CLI_IMPLEMENT_PROVIDER`, etc.)
3. Global `CLI_PROVIDER` env var
4. `default_provider` in `providers.json`

### Hooks

Webhooks can inject context or override PM verdicts at key pipeline events:

```json
"hooks": {
  "on_stage_start": [
    {
      "type": "webhook",
      "url": "https://your-server/hook",
      "timeout": 5,
      "can_override": false
    }
  ],
  "on_stage_end": [...],
  "on_verdict": [
    {
      "type": "webhook",
      "url": "https://your-server/verdict",
      "can_override": true
    }
  ]
}
```

**`on_stage_start`** — fires before the team runs. Response may include `{"inject_context": "..."}` to add context for that stage.

**`on_stage_end`** — fires after the stage output is saved. Informational only.

**`on_verdict`** — fires after PM oversight. If `can_override: true`, the hook response may include `{"override_verdict": "approve"}` or `{"override_verdict": "revise"}` to change the PM's verdict.

Hook failures are logged and never block the pipeline.

---

## Stage Output Files

Every pipeline run saves output from each stage under `pipeline-output/{task-id}/`:

| File | Contents |
|------|----------|
| `rewrite.md` | PM-rewritten prompt |
| `plan.md` | Implementation plan (PM handoff summary) |
| `code.md` | Code stage output (merged if multi-agent) |
| `code-{agent}.md` | Per-agent output in multi-agent mode |
| `simplify.md` | Simplification pass output |
| `code-fix.md` | Code fix output from test→code loopback (if triggered) |
| `test.md` | Test results |
| `review.md` | Security review PM summary |

Security reports are also saved separately to `security-reviews/{task-id}.json` for structured access via the API (`GET /security-report/{id}`).

---

See [`CLAUDE.md`](../CLAUDE.md) for the complete HTTP API reference, threading model details, and the skills system.
