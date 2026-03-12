# Agent Compare

## Description
Compares CLI providers by running the same prompt through each provider's plan phase and presenting a side-by-side analysis of approach, quality, length, and speed.

## Triggers
- "compare agents"
- "compare providers"
- "which agent is better"
- "benchmark providers"
- "provider comparison"
- "test all providers"

## Instructions

When the user wants to compare CLI providers, follow these steps:

### 1. Read Provider Configuration

Read `D:/Task-Claw/providers.json` to get the list of available providers. Each provider entry has:
- `binary` — the CLI executable
- `subcommand` — optional subcommand (e.g., `code` for `gh copilot`)
- `plan_args` — argument template for the plan phase
- `{prompt}` placeholder in the args

Also read `D:/Task-Claw/pipeline.json` for any relevant pipeline settings.

### 2. Determine Which Providers to Test

Check which providers are actually installed and available:

```bash
which <binary> 2>/dev/null || echo "not found"
```

For each provider in `providers.json`, verify the binary exists. Skip providers that are not installed, but note them in the output.

### 3. Construct the Test Prompt

Use the user's prompt, or if none provided, use a standard benchmark prompt like:
> "Create a plan to add input validation to a REST API endpoint that accepts JSON with fields: name (string, required), email (string, valid format), age (integer, 1-150)."

This is a read-only planning task — safe to run against any provider.

### 4. Run Each Provider

For each available provider, construct and run the plan command by following the `plan_args` template from `providers.json`. Replace `{prompt}` with the test prompt.

Example command construction for the `claude` provider:
```bash
claude --dangerously-skip-permissions -p "the prompt here" --output-format text
```

Example for `copilot`:
```bash
gh copilot suggest -t shell "the prompt here"
```

Time each execution:
```bash
start_time=$(date +%s%N)
# run command
end_time=$(date +%s%N)
elapsed=$(( (end_time - start_time) / 1000000 ))  # milliseconds
```

Capture both stdout and stderr. Set a timeout of 120 seconds per provider to avoid hanging.

Important: Remove the `CLAUDECODE` environment variable before running nested Claude commands to avoid blocking:
```bash
unset CLAUDECODE
```

### 5. Analyze Outputs

For each provider's output, evaluate:
- **Length**: character count and approximate word count
- **Structure**: does it use headings, bullet points, numbered steps?
- **Specificity**: does it reference actual file names, functions, or concrete implementation details?
- **Completeness**: does it cover edge cases, error handling, testing?
- **Time**: how long did it take?

### 6. Present Comparison

Output a comparison table:

```
Provider Comparison: Plan Phase
================================

Prompt: "<the test prompt>"

Provider    | Status    | Time    | Words | Structure | Notes
------------|-----------|---------|-------|-----------|------
claude      | success   | 12.3s   | 450   | excellent | Detailed steps with file paths
copilot     | success   | 3.1s    | 120   | basic     | High-level suggestions only
aider       | not found | -       | -     | -         | Binary not installed
codex       | success   | 8.7s    | 310   | good      | Good structure, less detail
```

Then provide a brief qualitative summary:
- Which provider gave the most actionable plan?
- Which was fastest?
- Which had the best cost/quality tradeoff?
- Recommendation for different use cases (quick tasks vs complex features)

### Important Caveats

- Only run the **plan phase** — this is read-only and safe. Never run code/implement phases as a comparison since they modify files.
- Each provider call counts against rate limits and API budgets.
- Some providers (like `copilot`) may require interactive input — if a command hangs, kill it after the timeout.
- Provider output quality varies significantly by prompt complexity. A single benchmark is indicative but not definitive.
