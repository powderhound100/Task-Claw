mod cli;
mod config;
mod error;
mod git;
mod pipeline;
mod prompts;
mod provider;
mod research;
mod security;
mod server;
mod services;
mod skills;
mod state;
mod types;

use std::time::Duration;

use clap::Parser;
use tracing::{info, error};

use config::AppConfig;
use server::AppState;

#[derive(Parser)]
#[command(name = "task-claw", about = "Multi-Provider Coding Agent")]
struct Cli {
    /// Run pipeline with this prompt and exit
    prompt: Vec<String>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Load .env
    dotenvy::dotenv().ok();

    // Initialize tracing
    tracing_subscriber::fmt()
        .with_target(false)
        .with_timer(tracing_subscriber::fmt::time::SystemTime)
        .init();

    let args = Cli::parse();
    let config = AppConfig::from_env();
    config.ensure_dirs()?;

    // Initialize prompts
    let prompts_file = config.agent_dir.join("prompts.json");
    prompts::init_prompts(&prompts_file);

    let app_state = AppState::new(config.clone());

    // ── Direct CLI invocation: task-claw "my prompt" ──────────────────
    if !args.prompt.is_empty() {
        let prompt = args.prompt.join(" ");
        info!("Task-Claw direct pipeline mode: {}", &prompt[..100.min(prompt.len())]);

        let result = pipeline::run_pipeline(
            &prompt,
            None,
            None,
            None,
            &config,
            &app_state.agent_status,
            &app_state.http_client,
            &app_state.pipeline_stats,
        )
        .await;

        info!(
            "Pipeline result: success={}, published={}, error={:?}",
            result.success, result.published, result.error
        );
        std::process::exit(if result.success { 0 } else { 1 });
    }

    // ── Polling mode ─────────────────────────────────────────────────
    info!("Task-Claw Agent starting… (version {})", config.agent_version);
    info!("  Project dir:   {}", config.project_dir.display());
    info!("  Tasks file:    {}", config.tasks_file.display());
    info!("  Ideas file:    {}", config.ideas_file.display());
    info!("  Poll interval: {}s", config.poll_interval);
    info!("  API cap:       {} calls/day", config.max_api_calls_per_day);
    info!("  Trigger port:  {}", config.trigger_port);
    info!(
        "  GitHub token:  {}",
        if config.github_token.is_empty() { "NOT SET" } else { "set" }
    );

    let providers = provider::list_available_providers(&config);
    let default = std::env::var("CLI_PROVIDER").unwrap_or_else(|_| {
        provider::load_providers(&config).default_provider
    });
    info!(
        "  CLI providers: {}",
        providers
            .iter()
            .map(|(k, v)| format!("{} ({})", k, v))
            .collect::<Vec<_>>()
            .join(", ")
    );
    info!("  Default provider: {}", default);

    {
        let mut status = app_state.agent_status.write().await;
        status.state = "idle".into();
        let agent_state = state::load_state(&config.state_file);
        status.api_calls_today = agent_state.api_calls_today;
    }

    // Start HTTP server in background
    let server_state = app_state.clone();
    let _cancel = app_state.cancel_token.clone();
    tokio::spawn(async move {
        if let Err(e) = server::start_server(server_state).await {
            error!("HTTP server error: {}", e);
        }
    });

    // Polling loop
    let poll_interval = Duration::from_secs(config.poll_interval);

    loop {
        tokio::select! {
            _ = app_state.cancel_token.cancelled() => {
                info!("Agent stopped by cancellation");
                break;
            }
            _ = tokio::signal::ctrl_c() => {
                info!("Agent stopped by user");
                app_state.cancel_token.cancel();
                break;
            }
            _ = async {
                // Poll cycle
                poll_cycle(&app_state).await;
                // Wait for either poll interval or trigger notification
                tokio::select! {
                    _ = tokio::time::sleep(poll_interval) => {}
                    _ = app_state.trigger_notify.notified() => {
                        info!("Woke up from trigger!");
                    }
                }
            } => {}
        }
    }

    Ok(())
}

async fn poll_cycle(state: &AppState) {
    let config = &state.config;

    let skip_age_filter = {
        let mut status = state.agent_status.write().await;
        let val = status.force_no_age_filter;
        status.force_no_age_filter = false;
        val
    };

    let max_age_hours: i64 = std::env::var("AGENT_MAX_TASK_AGE_HOURS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8);
    let max_age_ms = max_age_hours * 3600 * 1000;
    let cutoff_ms = state::ts_ms() - max_age_ms;

    let _lock = state.file_io.lock().await;
    let agent_state = state::load_state(&config.state_file);
    let tasks = state::load_tasks(&config.tasks_file);
    let ideas = state::load_ideas(&config.ideas_file);
    drop(_lock);

    let new_tasks: Vec<_> = tasks
        .iter()
        .filter(|t| {
            !t.id.is_empty()
                && !agent_state.processed.contains(&t.id)
                && t.status == "open"
                && (skip_age_filter
                    || t.created.unwrap_or(0).max(t.updated.unwrap_or(0)) >= cutoff_ms)
        })
        .collect();

    let new_ideas: Vec<_> = ideas
        .iter()
        .filter(|i| {
            !i.id.is_empty()
                && !agent_state.processed.contains(&i.id)
                && i.status == "open"
                && (skip_age_filter
                    || i.created.unwrap_or(0).max(i.updated.unwrap_or(0)) >= cutoff_ms)
        })
        .collect();

    {
        let mut status = state.agent_status.write().await;
        status.tasks_pending = new_tasks.len();
        status.ideas_pending = new_ideas.len();
        status.api_calls_today = agent_state.api_calls_today;
        status.last_run = Some(chrono::Utc::now().to_rfc3339());
    }

    if new_tasks.is_empty() && new_ideas.is_empty() {
        return;
    }

    info!(
        "Found {} new task(s) and {} new idea(s)!",
        new_tasks.len(),
        new_ideas.len()
    );

    git::git_pull(config).await;

    // Process tasks
    for task in &new_tasks {
        info!("Starting task: {}", task.title);
        {
            let mut status = state.agent_status.write().await;
            status.state = "processing".into();
            status.current_task = Some(task.title.clone());
        }

        let prompt = if !task.description.is_empty() && task.description != task.title {
            format!("{}: {}", task.title, task.description)
        } else {
            task.title.clone()
        };

        let result = pipeline::run_pipeline(
            &prompt,
            Some(&task.id),
            None,
            None,
            config,
            &state.agent_status,
            &state.http_client,
            &state.pipeline_stats,
        )
        .await;

        // Update state
        let _lock = state.file_io.lock().await;
        let mut agent_state = state::load_state(&config.state_file);
        agent_state.processed.push(task.id.clone());
        state::save_state(&config.state_file, &agent_state).ok();

        let mut tasks = state::load_tasks(&config.tasks_file);
        if result.success {
            let status = if result.published {
                "pushed-to-production"
            } else {
                "done"
            };
            state::update_task_status(&mut tasks, &task.id, status, "Pipeline completed.");
        } else {
            let err = result.error.as_deref().unwrap_or("Pipeline failed");
            let new_status = if err.to_lowercase().contains("security") {
                "security-blocked"
            } else {
                "open"
            };
            state::update_task_status(
                &mut tasks,
                &task.id,
                new_status,
                &format!("Pipeline failed: {}", err),
            );
        }
        state::save_tasks(&config.tasks_file, &tasks).ok();
    }

    // Process ideas
    for idea in &new_ideas {
        info!("Starting idea: {}", idea.title);
        {
            let mut status = state.agent_status.write().await;
            status.state = "processing".into();
            status.current_task = Some(idea.title.clone());
        }

        let prompt = format!("Idea: {}\nDescription: {}", idea.title, idea.description);

        let result = pipeline::run_pipeline(
            &prompt,
            Some(&idea.id),
            None,
            None,
            config,
            &state.agent_status,
            &state.http_client,
            &state.pipeline_stats,
        )
        .await;

        let _lock = state.file_io.lock().await;
        let mut agent_state = state::load_state(&config.state_file);
        agent_state.processed.push(idea.id.clone());
        state::save_state(&config.state_file, &agent_state).ok();

        let mut ideas = state::load_ideas(&config.ideas_file);
        if result.success {
            let status = if result.published {
                "pushed-to-production"
            } else {
                "done"
            };
            state::update_idea_status(&mut ideas, &idea.id, status, "Pipeline completed.");
        } else {
            let err = result.error.as_deref().unwrap_or("Pipeline failed");
            let new_status = if err.to_lowercase().contains("security") {
                "security-blocked"
            } else {
                "open"
            };
            state::update_idea_status(
                &mut ideas,
                &idea.id,
                new_status,
                &format!("Pipeline failed: {}", err),
            );
        }
        state::save_ideas(&config.ideas_file, &ideas).ok();
    }

    {
        let mut status = state.agent_status.write().await;
        status.state = "idle".into();
        status.current_task = None;
        status.current_stage = None;
    }
}
