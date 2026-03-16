pub mod routes_api;
pub mod routes_pipeline;
pub mod routes_static;

use std::collections::HashMap;
use std::sync::Arc;

use axum::Router;
use tokio::sync::{Notify, RwLock, Semaphore};
use tokio_util::sync::CancellationToken;
use tower_http::cors::{Any, CorsLayer};
use tracing::info;

use crate::config::AppConfig;
use crate::pipeline::stats::PipelineStatsTracker;
use crate::types::*;

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<AppConfig>,
    pub agent_status: Arc<RwLock<AgentStatus>>,
    pub research_jobs: Arc<RwLock<HashMap<String, ResearchJob>>>,
    pub skill_runs: Arc<RwLock<HashMap<String, SkillRun>>>,
    pub pipeline_stats: Arc<PipelineStatsTracker>,
    pub pipeline_semaphore: Arc<Semaphore>,
    pub file_io: Arc<tokio::sync::Mutex<()>>,
    pub trigger_notify: Arc<Notify>,
    pub cancel_token: CancellationToken,
    pub http_client: reqwest::Client,
}

impl AppState {
    pub fn new(config: AppConfig) -> Self {
        let mut status = AgentStatus::default();
        status.api_limit = config.max_api_calls_per_day;

        Self {
            config: Arc::new(config),
            agent_status: Arc::new(RwLock::new(status)),
            research_jobs: Arc::new(RwLock::new(HashMap::new())),
            skill_runs: Arc::new(RwLock::new(HashMap::new())),
            pipeline_stats: Arc::new(PipelineStatsTracker::new()),
            pipeline_semaphore: Arc::new(Semaphore::new(2)),
            file_io: Arc::new(tokio::sync::Mutex::new(())),
            trigger_notify: Arc::new(Notify::new()),
            cancel_token: CancellationToken::new(),
            http_client: reqwest::Client::new(),
        }
    }
}

/// Build the full router with all routes.
pub fn build_router(state: AppState) -> Router {
    let cors = if !state.config.cors_origin.is_empty() {
        CorsLayer::new()
            .allow_origin(
                state
                    .config
                    .cors_origin
                    .parse::<axum::http::HeaderValue>()
                    .unwrap_or_else(|_| axum::http::HeaderValue::from_static("*")),
            )
            .allow_methods(Any)
            .allow_headers(Any)
    } else {
        CorsLayer::new()
            .allow_origin(Any)
            .allow_methods(Any)
            .allow_headers(Any)
    };

    Router::new()
        // Static routes
        .merge(routes_static::routes())
        // API routes
        .merge(routes_api::routes())
        // Pipeline routes
        .merge(routes_pipeline::routes())
        .layer(cors)
        .with_state(state)
}

/// Start the HTTP server.
pub async fn start_server(state: AppState) -> anyhow::Result<()> {
    let port = state.config.trigger_port;
    let app = build_router(state);

    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{}", port)).await?;
    info!("HTTP server listening on port {}", port);

    axum::serve(listener, app).await?;
    Ok(())
}
