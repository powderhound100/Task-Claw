use axum::{
    Router,
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use serde_json::json;
use tracing::info;

use super::AppState;
use crate::pipeline;
use crate::provider;
use crate::research;
use crate::state;

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/trigger", axum::routing::post(trigger))
        .route("/status", axum::routing::get(status))
        .route("/implement/{id}", axum::routing::post(implement))
        .route("/research", axum::routing::post(start_research))
        .route("/research-status/{id}", axum::routing::get(research_status))
        .route("/pipeline-output/{*path}", axum::routing::get(pipeline_output))
        .route("/security-report/{id}", axum::routing::get(security_report))
        .route("/skill-output/{id}", axum::routing::get(skill_output))
}

async fn trigger(
    State(state): State<AppState>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    let prompt = body.get("prompt").and_then(|v| v.as_str()).map(|s| s.to_string());

    if let Some(prompt) = prompt {
        // Check semaphore
        let permit = match state.pipeline_semaphore.clone().try_acquire_owned() {
            Ok(p) => p,
            Err(_) => {
                return (
                    StatusCode::TOO_MANY_REQUESTS,
                    Json(json!({"ok": false, "error": "Pipeline at capacity"})),
                );
            }
        };

        let prompt_preview: String = prompt.chars().take(100).collect();
        info!("Trigger with prompt — launching pipeline: {}", prompt_preview);

        let config = (*state.config).clone();
        let agent_status = state.agent_status.clone();
        let http_client = state.http_client.clone();
        let stats = state.pipeline_stats.clone();

        tokio::spawn(async move {
            let _permit = permit; // held for duration
            pipeline::run_pipeline(
                &prompt,
                None,
                None,
                None,
                &config,
                &agent_status,
                &http_client,
                &stats,
            )
            .await;
        });

        return (
            StatusCode::OK,
            Json(json!({"ok": true, "message": "Pipeline started!", "prompt": prompt_preview})),
        );
    }

    // No prompt → wake polling loop
    let force = body.get("force").and_then(|v| v.as_bool()).unwrap_or(false);
    state.trigger_notify.notify_one();
    {
        let mut status = state.agent_status.write().await;
        status.last_trigger = Some(chrono::Utc::now().to_rfc3339());
        if force {
            status.force_no_age_filter = true;
        }
    }
    info!("Manual trigger received — waking agent!{}", if force { " (force=no age filter)" } else { "" });
    (StatusCode::OK, Json(json!({"ok": true, "message": "Agent triggered!"})))
}

async fn status(State(state): State<AppState>) -> impl IntoResponse {
    let snap = {
        let status = state.agent_status.read().await;
        status.clone()
    };
    let mut result = serde_json::to_value(&snap).unwrap_or(json!({}));
    result["version"] = json!(state.config.agent_version);

    let providers_cfg = provider::load_providers(&state.config);
    let providers_map: serde_json::Map<String, serde_json::Value> = providers_cfg
        .providers
        .iter()
        .map(|(k, v)| (k.clone(), json!(if v.name.is_empty() { k.clone() } else { v.name.clone() })))
        .collect();
    result["providers"] = json!(providers_map);
    result["default_provider"] = json!(
        std::env::var("CLI_PROVIDER").unwrap_or(providers_cfg.default_provider.clone())
    );

    let pipeline_cfg = pipeline::load_pipeline_config(&state.config);
    let stages_map: serde_json::Map<String, serde_json::Value> = pipeline_cfg
        .stages
        .iter()
        .map(|(name, cfg)| {
            (
                name.clone(),
                json!({
                    "enabled": cfg.enabled,
                    "team": cfg.team,
                    "timeout": cfg.timeout,
                }),
            )
        })
        .collect();
    result["pipeline_stages"] = json!(stages_map);
    result["pipeline_pm_backend"] = json!(pipeline_cfg.program_manager.backend);

    Json(result)
}

async fn implement(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> impl IntoResponse {
    let _lock = state.file_io.lock().await;
    let tasks = state::load_tasks(&state.config.tasks_file);
    let ideas = state::load_ideas(&state.config.ideas_file);

    let (target, _is_idea) = match state::find_item(&id, &tasks, &ideas) {
        Some((item, is_idea)) => (item.clone(), is_idea),
        None => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"ok": false, "error": format!("Task/idea {} not found", id)})),
            );
        }
    };
    drop(_lock);

    if target.status != "planned" {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"ok": false, "error": format!("Not in 'planned' state (current: {})", target.status)})),
        );
    }

    if target.plan.as_ref().map_or(true, |p| p.is_empty()) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"ok": false, "error": "No plan found"})),
        );
    }

    let permit = match state.pipeline_semaphore.clone().try_acquire_owned() {
        Ok(p) => p,
        Err(_) => {
            return (
                StatusCode::TOO_MANY_REQUESTS,
                Json(json!({"ok": false, "error": "Pipeline at capacity"})),
            );
        }
    };

    let config = (*state.config).clone();
    let agent_status = state.agent_status.clone();
    let http_client = state.http_client.clone();
    let stats = state.pipeline_stats.clone();
    let title = target.title.clone();
    let plan = target.plan.clone().unwrap_or_default();
    let task_id = id.clone();

    tokio::spawn(async move {
        let _permit = permit;
        let prompt = format!("Implement the following plan for: {}\n\n{}", title, plan);
        pipeline::run_pipeline(
            &prompt,
            Some(&task_id),
            None,
            Some("code"),
            &config,
            &agent_status,
            &http_client,
            &stats,
        )
        .await;
    });

    (
        StatusCode::OK,
        Json(json!({"ok": true, "message": format!("Implementation started for {}!", target.title)})),
    )
}

async fn start_research(
    State(state): State<AppState>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    let idea_id = body.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let title = body.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let desc = body.get("description").and_then(|v| v.as_str()).unwrap_or("").to_string();

    if idea_id.is_empty() || title.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"ok": false, "error": "Missing id or title"})),
        );
    }

    {
        let jobs = state.research_jobs.read().await;
        if jobs.get(&idea_id).map_or(false, |j| j.status == "researching") {
            return (
                StatusCode::CONFLICT,
                Json(json!({"ok": false, "error": "Research already in progress"})),
            );
        }
    }

    let config = (*state.config).clone();
    let jobs = state.research_jobs.clone();

    tokio::spawn(async move {
        research::run_research(idea_id, title, desc, config, jobs).await;
    });

    (StatusCode::OK, Json(json!({"ok": true, "message": "Research started!"})))
}

async fn research_status(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> impl IntoResponse {
    let jobs = state.research_jobs.read().await;
    let job = jobs
        .get(&id)
        .cloned()
        .unwrap_or(crate::types::ResearchJob {
            status: "idle".into(),
            result: None,
        });
    Json(job)
}

async fn pipeline_output(
    State(state): State<AppState>,
    Path(path): Path<String>,
) -> impl IntoResponse {
    let parts: Vec<&str> = path.splitn(2, '/').collect();
    let task_id = parts[0];
    let stage = parts.get(1).copied();

    let task_dir = state.config.pipeline_output_dir.join(task_id);

    // Path traversal guard
    if let Ok(resolved) = task_dir.canonicalize() {
        if let Ok(root) = state.config.pipeline_output_dir.canonicalize() {
            if !resolved.starts_with(&root) {
                return Json(json!({"ok": false, "error": "Forbidden"}));
            }
        }
    }

    if !task_dir.exists() {
        return Json(json!({"ok": false, "error": "No pipeline output for this task"}));
    }

    if let Some(stage) = stage {
        let out_file = task_dir.join(format!("{}.md", stage));
        if out_file.exists() {
            match std::fs::read_to_string(&out_file) {
                Ok(content) => Json(json!({"ok": true, "stage": stage, "content": content})),
                Err(_) => Json(json!({"ok": false, "error": "Read error"})),
            }
        } else {
            Json(json!({"ok": false, "error": format!("No output for stage '{}'", stage)}))
        }
    } else {
        let mut files = serde_json::Map::new();
        if let Ok(entries) = std::fs::read_dir(&task_dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.extension().map_or(false, |e| e == "md")
                    && !entry.file_name().to_string_lossy().starts_with('.')
                {
                    if let Ok(content) = std::fs::read_to_string(&p) {
                        let stem = p.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
                        files.insert(stem, json!(content));
                    }
                }
            }
        }
        Json(json!({"ok": true, "task_id": task_id, "stages": files}))
    }
}

async fn security_report(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> impl IntoResponse {
    let dir = &state.config.security_review_dir;
    let candidates = [format!("{}-review", id), id.clone()];

    for stem in &candidates {
        if let Ok(entries) = std::fs::read_dir(dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.file_stem().map(|s| s.to_string_lossy().to_string()).as_deref() == Some(stem) {
                    // Path traversal guard
                    if let Ok(resolved) = p.canonicalize() {
                        if let Ok(root) = dir.canonicalize() {
                            if !resolved.starts_with(&root) {
                                break;
                            }
                        }
                    }
                    if let Ok(content) = std::fs::read_to_string(&p) {
                        return Json(json!({"ok": true, "report": content}));
                    }
                }
            }
        }
    }
    Json(json!({"ok": false, "error": "No security report found for this task"}))
}

async fn skill_output(
    State(state): State<AppState>,
    Path(run_id): Path<String>,
) -> impl IntoResponse {
    let out_dir = state.config.skills_output_dir.join(&run_id);
    let out_file = out_dir.join("output.md");

    if out_file.exists() {
        match std::fs::read_to_string(&out_file) {
            Ok(content) => Json(json!({"ok": true, "run_id": run_id, "output": content})),
            Err(_) => Json(json!({"ok": false, "error": "Read error"})),
        }
    } else {
        let runs = state.skill_runs.read().await;
        if let Some(run) = runs.get(&run_id) {
            Json(json!({
                "ok": true,
                "run_id": run_id,
                "status": run.status,
                "output": run.output,
            }))
        } else {
            Json(json!({"ok": false, "error": "Skill run not found"}))
        }
    }
}
