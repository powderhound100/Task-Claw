# Cost Estimator

## Description
Estimates the cost of a Task-Claw pipeline run based on enabled stages, team sizes, provider configuration, and PM oversight settings.

## Triggers
- "estimate cost"
- "how much will this cost"
- "pipeline pricing"
- "budget"
- "cost estimate"
- "how expensive"

## Instructions

When the user asks about pipeline costs, follow these steps:

### 1. Read Configuration

Read both configuration files from the project root (`D:/Task-Claw`):

- **`pipeline.json`** — extract:
  - `stages` object: which stages are enabled (`"enabled": true/false`)
  - `team_size` per stage (number of parallel CLI agents)
  - `pm` config: whether PM oversight is enabled, which model is used
- **`providers.json`** — extract:
  - `default_provider` and available providers
  - Which binary each provider uses

Also check `.env` or `.env.example` for:
- `AGENT_MAX_CALLS` (daily API call cap, default 10)
- `CLI_PROVIDER` (default provider override)

### 2. Calculate Per-Stage Costs

Use these cost estimates per invocation:

**PM calls (GPT-4o via GitHub Models):** ~$0.003 per call
- Rewrite stage: 1 PM call (always)
- Each enabled stage with PM oversight: 2 PM calls (pm_direct_team + pm_oversee_stage)
- If PM is disabled: $0 for PM calls

**CLI provider calls — cost per invocation by stage:**

| Provider | Plan | Code | Simplify | Test | Review |
|----------|------|------|----------|------|--------|
| `claude` | $0.15 | $0.30 | $0.15 | $0.10 | $0.10 |
| `copilot` | included | included | included | included | included |
| `aider` | $0.05-$0.20 | $0.10-$0.20 | $0.05-$0.20 | $0.05-$0.10 | $0.05-$0.10 |
| Others | unknown | unknown | unknown | unknown | unknown |

**Per-stage formula:**
```
stage_cost = (team_size * cli_cost_per_call) + (pm_calls * $0.003)
```

For the code stage, also account for cross-review (adds `team_size` extra CLI calls if team_size > 1).

### 3. Calculate Total

```
total = sum of all enabled stage costs
```

If `PIPELINE_MAX_REVISE` > 0, note the worst-case cost:
```
worst_case = total * (1 + PIPELINE_MAX_REVISE)
```

### 4. Show Daily Budget

```
daily_budget = AGENT_MAX_CALLS * average_cost_per_pipeline_run
```

### 5. Present Results

Output a clear breakdown:

```
Pipeline Cost Estimate
======================

Provider: <default_provider>
PM: <enabled/disabled> (<model>)

Stage          | Agents | CLI Cost | PM Cost | Stage Total
---------------|--------|----------|---------|------------
Rewrite        |   0    |  $0.00   |  $0.003 |  $0.003
Plan           |   N    |  $X.XX   |  $0.006 |  $X.XX
Code           |   N    |  $X.XX   |  $0.006 |  $X.XX
Simplify       |   N    |  $X.XX   |  $0.006 |  $X.XX
Test           |   N    |  $X.XX   |  $0.006 |  $X.XX
Review         |   N    |  $X.XX   |  $0.006 |  $X.XX
               |        |          |         |
TOTAL          |        |          |         |  $X.XX
Worst case (w/ revisions):                      $X.XX

Daily budget: X calls * $X.XX/run = $X.XX/day
```

For `copilot` provider, show "included" instead of dollar amounts for CLI costs.
For unknown providers, show "check provider pricing" and only calculate PM costs.
