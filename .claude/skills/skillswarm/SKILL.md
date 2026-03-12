# Skillswarm

## Description
Orchestrates parallel pipeline runs by decomposing a large task into independent subtasks, creating task entries, triggering pipelines for each, and collecting results.

## Triggers
- "swarm"
- "parallel tasks"
- "run multiple pipelines"
- "batch process"
- "split and run"
- "fan out"

## Instructions

When the user wants to run multiple pipeline tasks in parallel, follow these steps:

### 1. Check Budget

Before doing anything, check available budget:

```bash
curl -s http://localhost:8099/status
```

From the response, extract:
- `api_calls_today` — calls used so far
- `api_limit` — daily cap (from `AGENT_MAX_CALLS`)
- `available = api_limit - api_calls_today`

If available calls are fewer than the number of planned subtasks, warn the user and ask whether to proceed with a reduced set or abort.

### 2. Analyze and Decompose

Take the user's task and break it into independent subtasks. Rules:
- Each subtask must be self-contained (no dependencies between subtasks)
- Each subtask should map to a single pipeline run
- Keep subtasks focused — one clear deliverable each
- Avoid subtasks that modify the same files (merge conflicts)

Present the proposed subtasks to the user for confirmation before proceeding.

### 3. Create Task Entries

For each subtask, add an entry to `data/tasks.json`:

```bash
curl -s -X POST http://localhost:8099/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Subtask title", "description": "Detailed subtask description", "status": "open"}'
```

Record the returned task IDs.

### 4. Trigger Pipelines

For each subtask, trigger a pipeline run:

```bash
curl -s -X POST http://localhost:8099/trigger \
  -H "Content-Type: application/json" \
  -d '{"prompt": "subtask description here"}'
```

Note: The agent processes tasks sequentially (one pipeline at a time), but triggering multiple tasks queues them. The agent will pick them up in order during its polling cycle.

### 5. Monitor Progress

Poll the agent status to track progress:

```bash
curl -s http://localhost:8099/status
```

Check for:
- `pipeline_active` — whether a pipeline is currently running
- `current_stage` — which stage is in progress
- Task statuses via the tasks API

Also check individual task status:

```bash
curl -s http://localhost:8099/api/tasks
```

Look for status transitions: `open` -> `grabbed` -> `in-progress` -> `done` / `security-blocked`.

### 6. Collect Results

Once all tasks complete, gather results:

For each completed task, check pipeline output:

```bash
curl -s http://localhost:8099/pipeline-output/<task-id>
```

And security reports if applicable:

```bash
curl -s http://localhost:8099/security-report/<task-id>
```

### 7. Synthesize

Present a summary to the user:
- How many subtasks succeeded vs failed vs security-blocked
- Key outputs from each subtask
- Any issues that need manual attention
- Total API calls consumed

### Important Caveats

- The Task-Claw agent runs pipelines sequentially, not in true parallel. "Swarm" means queuing multiple tasks, not concurrent execution.
- Each pipeline run consumes API calls. A swarm of N tasks uses roughly N * calls_per_pipeline.
- If a subtask gets `security-blocked`, it needs manual review before it can proceed.
- Always check budget before and during the swarm to avoid hitting the daily cap mid-run.
