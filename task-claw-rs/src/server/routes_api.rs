use axum::{
    Router,
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use serde_json::json;

use super::AppState;
use crate::config::AppConfig;
use crate::pipeline;
use crate::provider;
use crate::skills;
use crate::state;
use crate::types::*;

pub fn routes() -> Router<AppState> {
    Router::new()
        // Tasks CRUD
        .route("/api/tasks", axum::routing::get(list_tasks).post(create_task))
        .route("/api/tasks/{id}", axum::routing::put(update_task).delete(delete_task))
        // Ideas CRUD
        .route("/api/ideas", axum::routing::get(list_ideas).post(create_idea))
        .route("/api/ideas/{id}", axum::routing::put(update_idea).delete(delete_idea))
        // Skills
        .route("/api/skills", axum::routing::get(list_skills).post(create_skill))
        .route("/api/skills/{id}", axum::routing::put(update_skill).delete(delete_skill))
        .route("/api/skills/{id}/run", axum::routing::post(run_skill))
        .route("/api/skills/{id}/runs", axum::routing::get(list_skill_runs))
        // Photos
        .route("/api/photos/upload", axum::routing::post(upload_photo))
        .route("/api/photos/{file}", axum::routing::delete(delete_photo))
        // Config
        .route(
            "/api/config/pipeline",
            axum::routing::get(get_pipeline_config).put(put_pipeline_config),
        )
        .route(
            "/api/config/providers",
            axum::routing::get(get_providers_config).put(put_providers_config),
        )
        // Pipeline history & stats
        .route("/api/pipeline-history", axum::routing::get(pipeline_history))
        .route("/api/pipeline-stats", axum::routing::get(pipeline_stats))
}

// ── Tasks ────────────────────────────────────────────────────────────────

async fn list_tasks(State(state): State<AppState>) -> impl IntoResponse {
    let _lock = state.file_io.lock().await;
    let tasks = state::load_tasks(&state.config.tasks_file);
    Json(tasks)
}

async fn create_task(State(state): State<AppState>, Json(mut body): Json<serde_json::Value>) -> impl IntoResponse {
    if body.get("id").and_then(|v| v.as_str()).unwrap_or("").is_empty() {
        body["id"] = json!(state::generate_id());
    }
    if body.get("created").is_none() {
        body["created"] = json!(state::ts_ms());
    }
    if body.get("updated").is_none() {
        body["updated"] = json!(state::ts_ms());
    }
    if body.get("status").is_none() {
        body["status"] = json!("open");
    }

    let _lock = state.file_io.lock().await;
    let mut tasks: Vec<serde_json::Value> = state::load_json_file(
        &state.config.tasks_file,
        "tasks.json",
    )
    .unwrap_or_default();
    tasks.insert(0, body.clone());
    state::save_json_file(&state.config.tasks_file, &tasks).ok();
    (StatusCode::CREATED, Json(body))
}

async fn update_task(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    let _lock = state.file_io.lock().await;
    let mut tasks: Vec<serde_json::Value> = state::load_json_file(
        &state.config.tasks_file,
        "tasks.json",
    )
    .unwrap_or_default();

    for item in tasks.iter_mut() {
        if item.get("id").and_then(|v| v.as_str()) == Some(&id) {
            if let (Some(obj), Some(updates)) = (item.as_object_mut(), body.as_object()) {
                for (k, v) in updates {
                    obj.insert(k.clone(), v.clone());
                }
                obj.insert("updated".into(), json!(state::ts_ms()));
            }
            state::save_json_file(&state.config.tasks_file, &tasks).ok();
            return (StatusCode::OK, Json(json!({"ok": true})));
        }
    }
    (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "Task not found"})))
}

async fn delete_task(State(state): State<AppState>, Path(id): Path<String>) -> impl IntoResponse {
    let _lock = state.file_io.lock().await;
    let mut tasks: Vec<serde_json::Value> = state::load_json_file(
        &state.config.tasks_file,
        "tasks.json",
    )
    .unwrap_or_default();
    let before = tasks.len();
    tasks.retain(|t| t.get("id").and_then(|v| v.as_str()) != Some(&id));
    if tasks.len() < before {
        state::save_json_file(&state.config.tasks_file, &tasks).ok();
    }
    Json(json!({"ok": true}))
}

// ── Ideas ────────────────────────────────────────────────────────────────

async fn list_ideas(State(state): State<AppState>) -> impl IntoResponse {
    let _lock = state.file_io.lock().await;
    let ideas = state::load_ideas(&state.config.ideas_file);
    Json(ideas)
}

async fn create_idea(State(state): State<AppState>, Json(mut body): Json<serde_json::Value>) -> impl IntoResponse {
    if body.get("id").and_then(|v| v.as_str()).unwrap_or("").is_empty() {
        body["id"] = json!(state::generate_id());
    }
    if body.get("created").is_none() {
        body["created"] = json!(state::ts_ms());
    }
    if body.get("updated").is_none() {
        body["updated"] = json!(state::ts_ms());
    }
    if body.get("status").is_none() {
        body["status"] = json!("open");
    }

    let _lock = state.file_io.lock().await;
    let mut ideas: Vec<serde_json::Value> = state::load_json_file(
        &state.config.ideas_file,
        "ideas.json",
    )
    .unwrap_or_default();
    ideas.insert(0, body.clone());
    state::save_json_file(&state.config.ideas_file, &ideas).ok();
    (StatusCode::CREATED, Json(body))
}

async fn update_idea(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    let _lock = state.file_io.lock().await;
    let mut ideas: Vec<serde_json::Value> = state::load_json_file(
        &state.config.ideas_file,
        "ideas.json",
    )
    .unwrap_or_default();

    for item in ideas.iter_mut() {
        if item.get("id").and_then(|v| v.as_str()) == Some(&id) {
            if let (Some(obj), Some(updates)) = (item.as_object_mut(), body.as_object()) {
                for (k, v) in updates {
                    obj.insert(k.clone(), v.clone());
                }
                obj.insert("updated".into(), json!(state::ts_ms()));
            }
            state::save_json_file(&state.config.ideas_file, &ideas).ok();
            return (StatusCode::OK, Json(json!({"ok": true})));
        }
    }
    (StatusCode::NOT_FOUND, Json(json!({"ok": false, "error": "Idea not found"})))
}

async fn delete_idea(State(state): State<AppState>, Path(id): Path<String>) -> impl IntoResponse {
    let _lock = state.file_io.lock().await;
    let mut ideas: Vec<serde_json::Value> = state::load_json_file(
        &state.config.ideas_file,
        "ideas.json",
    )
    .unwrap_or_default();
    let before = ideas.len();
    ideas.retain(|i| i.get("id").and_then(|v| v.as_str()) != Some(&id));
    if ideas.len() < before {
        state::save_json_file(&state.config.ideas_file, &ideas).ok();
    }
    Json(json!({"ok": true}))
}

// ── Skills ───────────────────────────────────────────────────────────────

async fn list_skills(State(state): State<AppState>) -> impl IntoResponse {
    let all = skills::get_all_skills(&state.config);
    let result: Vec<serde_json::Value> = all
        .iter()
        .map(|(id, skill)| {
            let mut v = serde_json::to_value(skill).unwrap_or(json!({}));
            v["id"] = json!(id);
            v
        })
        .collect();
    Json(json!({"skills": result}))
}

async fn create_skill(
    State(state): State<AppState>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    let skill_id = body
        .get("id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .unwrap_or_else(state::generate_id);

    let mut data = skills::load_skills(&state.config);
    data.skills.insert(
        skill_id.clone(),
        Skill {
            name: body.get("name").and_then(|v| v.as_str()).unwrap_or(&skill_id).to_string(),
            description: body.get("description").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            prompt: body.get("prompt").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            provider: body.get("provider").and_then(|v| v.as_str()).map(|s| s.to_string()),
            timeout: body.get("timeout").and_then(|v| v.as_u64()).unwrap_or(300),
            phase: body.get("phase").and_then(|v| v.as_str()).unwrap_or("implement").to_string(),
            tags: body
                .get("tags")
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect())
                .unwrap_or_default(),
            source: None,
            triggers: None,
            skill_file: None,
        },
    );
    skills::save_skills(&state.config, &data).ok();
    (StatusCode::CREATED, Json(json!({"ok": true, "id": skill_id})))
}

async fn update_skill(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    let mut data = skills::load_skills(&state.config);
    if let Some(skill) = data.skills.get_mut(&id) {
        if let Some(v) = body.get("name").and_then(|v| v.as_str()) {
            skill.name = v.to_string();
        }
        if let Some(v) = body.get("description").and_then(|v| v.as_str()) {
            skill.description = v.to_string();
        }
        if let Some(v) = body.get("prompt").and_then(|v| v.as_str()) {
            skill.prompt = v.to_string();
        }
        if let Some(v) = body.get("provider") {
            skill.provider = v.as_str().map(|s| s.to_string());
        }
        if let Some(v) = body.get("timeout").and_then(|v| v.as_u64()) {
            skill.timeout = v;
        }
        if let Some(v) = body.get("phase").and_then(|v| v.as_str()) {
            skill.phase = v.to_string();
        }
        if let Some(v) = body.get("tags").and_then(|v| v.as_array()) {
            skill.tags = v.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect();
        }
        skills::save_skills(&state.config, &data).ok();
        Json(json!({"ok": true}))
    } else {
        Json(json!({"ok": false, "error": "Skill not found"}))
    }
}

async fn delete_skill(State(state): State<AppState>, Path(id): Path<String>) -> impl IntoResponse {
    let mut data = skills::load_skills(&state.config);
    data.skills.remove(&id);
    skills::save_skills(&state.config, &data).ok();
    Json(json!({"ok": true}))
}

async fn run_skill(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    let input = body.get("input").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let provider_override = body.get("provider").and_then(|v| v.as_str()).map(|s| s.to_string());
    let config = (*state.config).clone();
    let id_clone = id.clone();

    tokio::spawn(async move {
        skills::run_skill(&id_clone, &input, provider_override.as_deref(), &config).await;
    });

    Json(json!({"ok": true, "message": format!("Skill '{}' started", id)}))
}

async fn list_skill_runs(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> impl IntoResponse {
    let runs = state.skill_runs.read().await;
    let matching: Vec<serde_json::Value> = runs
        .iter()
        .filter(|(_, r)| r.skill_id == id)
        .map(|(rid, r)| {
            let mut v = serde_json::to_value(r).unwrap_or(json!({}));
            v["run_id"] = json!(rid);
            v
        })
        .collect();
    Json(json!({"runs": matching}))
}

// ── Photos ───────────────────────────────────────────────────────────────

async fn upload_photo(
    State(state): State<AppState>,
    mut multipart: axum::extract::Multipart,
) -> impl IntoResponse {
    while let Ok(Some(field)) = multipart.next_field().await {
        let filename = field.file_name().unwrap_or("upload.jpg").to_string();
        let ext = if let Some(dot_pos) = filename.rfind('.') {
            let candidate = filename[dot_pos..].to_lowercase();
            if AppConfig::allowed_photo_extensions().contains(&candidate.as_str()) {
                candidate
            } else {
                ".jpg".to_string()
            }
        } else {
            ".jpg".to_string()
        };

        match field.bytes().await {
            Ok(data) => {
                if data.len() > 10 * 1024 * 1024 {
                    return (
                        StatusCode::BAD_REQUEST,
                        Json(json!({"ok": false, "error": "File too large (max 10MB)"})),
                    );
                }
                let safe_name = format!("{}{}", &uuid::Uuid::new_v4().to_string()[..12], ext);
                let photo_path = state.config.photos_dir.join(&safe_name);
                if let Err(e) = tokio::fs::write(&photo_path, &data).await {
                    return (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        Json(json!({"ok": false, "error": e.to_string()})),
                    );
                }
                return (StatusCode::OK, Json(json!({"ok": true, "filename": safe_name})));
            }
            Err(e) => {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(json!({"ok": false, "error": e.to_string()})),
                );
            }
        }
    }
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"ok": false, "error": "No file found in upload"})),
    )
}

async fn delete_photo(
    State(state): State<AppState>,
    Path(filename): Path<String>,
) -> impl IntoResponse {
    let photo_path = state.config.photos_dir.join(&filename);
    // Path traversal guard
    if let Ok(resolved) = photo_path.canonicalize() {
        if let Ok(root) = state.config.photos_dir.canonicalize() {
            if resolved.starts_with(&root) {
                tokio::fs::remove_file(&resolved).await.ok();
            }
        }
    }
    Json(json!({"ok": true}))
}

// ── Config ───────────────────────────────────────────────────────────────

async fn get_pipeline_config(State(state): State<AppState>) -> impl IntoResponse {
    let cfg = pipeline::load_pipeline_config(&state.config);
    Json(cfg)
}

async fn put_pipeline_config(
    State(state): State<AppState>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    if !body.get("stages").map_or(false, |v| v.is_object()) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"ok": false, "error": "Pipeline config must have 'stages' dict"})),
        );
    }
    let json = serde_json::to_string_pretty(&body).unwrap_or_default();
    std::fs::write(&state.config.pipeline_file, json).ok();
    (StatusCode::OK, Json(json!({"ok": true})))
}

async fn get_providers_config(State(state): State<AppState>) -> impl IntoResponse {
    let cfg = provider::load_providers(&state.config);
    Json(cfg)
}

async fn put_providers_config(
    State(state): State<AppState>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    let providers = match body.get("providers").and_then(|v| v.as_object()) {
        Some(p) => p,
        None => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"ok": false, "error": "Must have 'providers' dict"})),
            );
        }
    };

    // Validate binaries
    for (name, prov) in providers {
        let binary = match prov.get("binary").and_then(|v| v.as_str()) {
            Some(b) => b,
            None => {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(json!({"ok": false, "error": format!("Provider '{}' missing 'binary'", name)})),
                );
            }
        };
        let stem = std::path::Path::new(binary)
            .file_stem()
            .map(|s| s.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        if !AppConfig::allowed_provider_binaries().contains(&stem.as_str()) {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"ok": false, "error": format!(
                    "Provider binary '{}' not allowed. Allowed: {}",
                    binary,
                    AppConfig::allowed_provider_binaries().join(", ")
                )})),
            );
        }
    }

    let json = serde_json::to_string_pretty(&body).unwrap_or_default();
    std::fs::write(&state.config.providers_file, json).ok();
    (StatusCode::OK, Json(json!({"ok": true})))
}

// ── Pipeline History & Stats ─────────────────────────────────────────────

async fn pipeline_history(State(state): State<AppState>) -> impl IntoResponse {
    let mut runs = Vec::new();
    let dir = &state.config.pipeline_output_dir;
    if dir.exists() {
        let mut dirs: Vec<_> = std::fs::read_dir(dir)
            .into_iter()
            .flatten()
            .flatten()
            .filter(|e| e.path().is_dir())
            .filter_map(|e| {
                let mtime = e.metadata().ok()?.modified().ok()?;
                Some((e.path(), mtime))
            })
            .collect();
        dirs.sort_by(|a, b| b.1.cmp(&a.1));

        for (d, mtime) in dirs.iter().take(50) {
            let name = d.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
            let timestamp = chrono::DateTime::<chrono::Utc>::from(*mtime).to_rfc3339();
            let stages: Vec<String> = std::fs::read_dir(d)
                .into_iter()
                .flatten()
                .flatten()
                .filter(|e| {
                    let p = e.path();
                    p.extension().map_or(false, |ext| ext == "md")
                        && !e.file_name().to_string_lossy().starts_with('.')
                })
                .map(|e| {
                    e.path()
                        .file_stem()
                        .map(|s| s.to_string_lossy().to_string())
                        .unwrap_or_default()
                })
                .collect();
            runs.push(json!({
                "task_id": name,
                "timestamp": timestamp,
                "stages": stages,
            }));
        }
    }
    Json(json!({"runs": runs}))
}

async fn pipeline_stats(State(state): State<AppState>) -> impl IntoResponse {
    let stats = state.pipeline_stats.get_summary();
    Json(json!({"stats": stats}))
}
