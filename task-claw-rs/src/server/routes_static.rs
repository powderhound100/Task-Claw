use axum::{
    Router,
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use std::path::PathBuf;

use super::AppState;

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/", axum::routing::get(serve_index))
        .route("/index.html", axum::routing::get(serve_index))
        .route("/pipeline.html", axum::routing::get(serve_pipeline))
        .route("/css/{*path}", axum::routing::get(serve_web_asset))
        .route("/js/{*path}", axum::routing::get(serve_web_asset))
        .route("/photos/{*path}", axum::routing::get(serve_photo))
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

async fn serve_web_asset(
    State(state): State<AppState>,
    axum::extract::Path(path): axum::extract::Path<String>,
) -> Response {
    // Reconstruct the path from the URI since we get just the wildcard part
    let uri_path = format!("{}", path);
    let file_path = state.config.web_dir.join(&uri_path);
    serve_file(&file_path, &state.config.web_dir).await
}

async fn serve_photo(
    State(state): State<AppState>,
    axum::extract::Path(path): axum::extract::Path<String>,
) -> Response {
    let file_path = state.config.photos_dir.join(&path);
    serve_file(&file_path, &state.config.photos_dir).await
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
