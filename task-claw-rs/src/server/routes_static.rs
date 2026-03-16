use axum::{
    Router,
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use std::path::PathBuf;
use tower_http::services::ServeDir;

use super::AppState;
use crate::config::AppConfig;

pub fn routes(config: &AppConfig) -> Router<AppState> {
    Router::new()
        .route("/", axum::routing::get(serve_index))
        .route("/index.html", axum::routing::get(serve_index))
        .route("/pipeline.html", axum::routing::get(serve_pipeline))
        .nest_service("/css", ServeDir::new(config.web_dir.join("css")))
        .nest_service("/js", ServeDir::new(config.web_dir.join("js")))
        .nest_service("/photos", ServeDir::new(config.photos_dir.clone()))
}

async fn serve_index(State(state): State<AppState>) -> Response {
    serve_file(&state.config.web_dir.join("index.html"), &state.config.web_dir).await
}

async fn serve_pipeline(State(state): State<AppState>) -> Response {
    serve_file(
        &state.config.web_dir.join("pipeline.html"),
        &state.config.web_dir,
    )
    .await
}

async fn serve_file(file_path: &PathBuf, allowed_root: &PathBuf) -> Response {
    // Path traversal guard
    let resolved = match file_path.canonicalize() {
        Ok(p) => p,
        Err(_) => {
            return (
                StatusCode::NOT_FOUND,
                axum::Json(serde_json::json!({"ok": false, "error": "Not found"})),
            )
                .into_response();
        }
    };

    let root_resolved = match allowed_root.canonicalize() {
        Ok(p) => p,
        Err(_) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                axum::Json(serde_json::json!({"ok": false, "error": "Server error"})),
            )
                .into_response();
        }
    };

    if !resolved.starts_with(&root_resolved) {
        return (
            StatusCode::FORBIDDEN,
            axum::Json(serde_json::json!({"ok": false, "error": "Forbidden"})),
        )
            .into_response();
    }

    if !resolved.is_file() {
        return (
            StatusCode::NOT_FOUND,
            axum::Json(serde_json::json!({"ok": false, "error": "Not found"})),
        )
            .into_response();
    }

    match tokio::fs::read(&resolved).await {
        Ok(data) => {
            let content_type = mime_guess::from_path(&resolved)
                .first_or_octet_stream()
                .to_string();
            (
                StatusCode::OK,
                [(axum::http::header::CONTENT_TYPE, content_type)],
                data,
            )
                .into_response()
        }
        Err(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            axum::Json(serde_json::json!({"ok": false, "error": "Read error"})),
        )
            .into_response(),
    }
}
