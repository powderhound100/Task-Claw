# Task-Claw Rust Rewrite Plan

## Context

Task-Claw is a ~2,280-line single-file Python multi-agent coding orchestration platform (`task-claw.py`) with one runtime dependency (`requests`). It monitors task/idea queues, runs them through a PM-overseen pipeline (rewrite → plan → code → test → review → publish), and exposes an HTTP API. The goal is a full Rust rewrite producing a single statically-linked binary with equivalent functionality, better performance, and type safety.

---

## Technology Stack

| Concern | Crate | Replaces |
|---|---|---|
| Async runtime | `tokio` (full) | `threading` + `concurrent.futures` |
| HTTP server | `axum` + `tower-http` (cors) | `http.server.BaseHTTPRequestHandler` |
| HTTP client | `reqwest` | `requests` |
| JSON | `serde` + `serde_json` | `json` |
| CLI parsing | `clap` (derive) | `sys.argv` |
| Logging | `tracing` + `tracing-subscriber` | `logging` |
| .env loading | `dotenvy` | manual `.env` parsing |
| Errors | `anyhow` + `thiserror` | `try/except` |
| Regex | `regex` | `re` |
| Time | `chrono` | `datetime` |
| Process exec | `tokio::process` | `subprocess` |
| Binary lookup | `which` | `shutil.which` |
| Graceful shutdown | `tokio-util` (CancellationToken) | — |

---

## Project Structure (single crate)

```
task-claw-rs/
├── Cargo.toml
├── src/
│   ├── main.rs              # Entry point, CLI parsing, polling loop
│   ├── config.rs            # AppConfig from .env + env vars
│   ├── types.rs             # Shared types, enums, serde models
│   ├── provider.rs          # providers.json loading, resolution, command building, execution
│   ├── state.rs             # agent-state.json, tasks.json, ideas.json I/O
│   ├── server.rs            # axum HTTP routes (all endpoints)
│   ├── git.rs               # git pull/add/commit/push/diff
│   ├── security.rs          # Security review + findings handler
│   ├── integrations.rs      # Home Assistant notifications, Docker restart
│   ├── research.rs          # Background research jobs
│   └── pipeline/
│       ├── mod.rs           # run_pipeline() orchestrator
│       ├── pm.rs            # PM API calls (direct, oversee, merge, rewrite)
│       ├── team.rs          # run_team(), cross_review_code()
│       ├── stages.rs        # Stage-specific prompt/logic
│       ├── garbage.rs       # Garbage detection patterns
│       └── context.rs       # Context building + capping
```

---

## Key Design Decisions

1. **Single crate, not workspace** — subsystems are tightly coupled; one binary output
2. **Async everywhere** — `tokio::spawn` replaces daemon threads, `JoinSet` replaces `ThreadPoolExecutor`, `tokio::select!` replaces `threading.Event`
3. **`#[serde(flatten)]` for Task/Idea** — preserves unknown JSON fields through read-modify-write (critical for compatibility with web UI)
4. **`Arc<AppConfig>`** — passed explicitly instead of module-level globals; enables testing
5. **Shared state via `Arc<RwLock<...>>`** — replaces Python's `status_lock`/`research_lock` globals

---

## Key Type Mappings

| Python | Rust |
|---|---|
| `dict` with dynamic keys | Typed struct + `#[serde(flatten)] HashMap<String, Value>` |
| `threading.Thread(daemon=True)` | `tokio::spawn()` |
| `threading.Lock` | `tokio::sync::RwLock` |
| `threading.Event` | `tokio::sync::Notify` |
| `ThreadPoolExecutor` | `tokio::task::JoinSet` |
| `subprocess.run()` | `tokio::process::Command` |
| Module globals | `Arc<AppConfig>` |

---

## Implementation Phases

### Phase 1: Foundation
**Files**: `main.rs`, `config.rs`, `types.rs`, `provider.rs`
- Cargo.toml with all deps
- `AppConfig` loading from `.env` + env vars (maps to Python lines 42-68)
- `providers.json` deserialization + provider resolution (`get_provider_for_phase`)
- `build_cli_command` + `run_cli_command` (Python lines 134-303)
- CLI arg parsing: `task-claw "prompt"` vs polling mode
- **Verify**: load config, resolve provider, execute subprocess

### Phase 2: State Management
**Files**: `state.rs`
- `Task` / `Idea` serde models with `#[serde(flatten)]` for extra fields
- `load_tasks`, `save_tasks`, `load_ideas`, `save_ideas` with Mutex for file safety
- `load_state`, `save_state` for `agent-state.json`
- `load_pipeline` for `pipeline.json`
- Status update helpers
- **Verify**: round-trip existing JSON files without data loss

### Phase 3: PM Backend
**Files**: `pipeline/pm.rs`, `pipeline/garbage.rs`
- `pm_api_call` with 3 backends (github_models, anthropic, openai_compatible)
- Retry logic with exponential backoff on 429/5xx
- `pm_direct_team`, `pm_oversee_stage`, `pm_merge_with_reviews`, `rewrite_prompt`
- `_parse_overseer_response` regex extraction (Verdict/Issues/Synthesis/Handoff)
- Garbage detection: 28 patterns + length check
- **Verify**: PM API calls + structured response parsing

### Phase 4: Pipeline Core (most complex)
**Files**: `pipeline/mod.rs`, `pipeline/team.rs`, `pipeline/stages.rs`, `pipeline/context.rs`
- `run_team` with `JoinSet` for parallel CLI execution
- `cross_review_code` with parallel reviews
- `_build_direct_prompt` per stage
- Stage output save/load to `pipeline-output/{task_id}/`
- Context capping (`_cap_context`)
- Test failure detection (`_test_found_failures`)
- Full `run_pipeline` orchestrator (Python lines 892-1216):
  - Stage iteration with skip, PM direct → team → oversee flow
  - REVISE loop, test→code loopback, direct mode fallback
- **Verify**: full pipeline end-to-end

### Phase 5: HTTP Server
**Files**: `server.rs`
- axum routes matching all Python endpoints:
  - POST: `/trigger`, `/implement/{id}`, `/research`
  - GET: `/status`, `/research-status/{id}`, `/pipeline-output/...`, `/security-report/{id}`
- CORS via `tower-http`
- `AppState` shared between routes and main loop
- **Verify**: all endpoints respond correctly

### Phase 6: Integrations
**Files**: `git.rs`, `integrations.rs`, `security.rs`, `research.rs`
- Git ops: pull, add, commit, push (Python lines 1893-1971)
- Docker compose restart for changed services
- Home Assistant notification via REST
- Security review: run `git diff`, call provider, parse JSON, handle findings
- Research background jobs
- **Verify**: git operations, notifications, security review

### Phase 7: Main Loop & Polish
**Files**: `main.rs`
- Polling loop with `tokio::select!` for interruptible sleep
- Task/idea filtering (age, status, processed list)
- `process_task` / `process_idea`
- Graceful shutdown via `CancellationToken`
- Canary file writing
- **Verify**: full polling mode, HTTP trigger wakes loop

### Phase 8: Testing
- Unit tests: garbage detection, context capping, provider resolution, prompt building, overseer parsing, security JSON parsing, failure detection
- Integration tests: JSON round-trip with real data files
- Mock PM tests with `wiremock`
- E2E: mock provider script through full pipeline
- Windows-specific: path handling, `.cmd`/`.exe` resolution

---

## Risk Areas

| Risk | Mitigation |
|---|---|
| JSON round-trip data loss | `#[serde(flatten)]` + test with real production JSON files |
| Windows binary resolution (.cmd/.bat) | `which` crate + explicit Windows testing |
| Subprocess encoding | `String::from_utf8_lossy` (matches Python's `errors="replace"`) |
| PM response regex parsing | Port exact patterns from Python, add unit tests |
| Concurrent file access | `tokio::sync::Mutex` around task/idea file I/O |

---

## Critical Source Files

| File | Role in Migration |
|---|---|
| `task-claw.py` | Entire source; lines 892-1216 (`run_pipeline`) most complex |
| `providers.json` | Defines Provider struct shape (7 providers) |
| `pipeline.json` | Defines PipelineConfig + PM backend config |
| `.env.example` | Documents all env vars AppConfig must support |

---

## Verification

1. **Config loading**: Load existing `.env`, `providers.json`, `pipeline.json` — assert all values parsed
2. **JSON round-trip**: Load real `tasks.json` → serialize → deserialize → compare (no field loss)
3. **Provider execution**: Run `claude --version` or equivalent through `run_cli_command`
4. **PM API**: Call GitHub Models with a test prompt, verify response parsing
5. **Pipeline E2E**: Run `task-claw-rs "Add a comment to README"` — verify all stages complete
6. **HTTP API**: `curl localhost:8099/status` — verify JSON response matches Python format
7. **Compatibility**: Run both Python and Rust against same config/data, compare outputs
