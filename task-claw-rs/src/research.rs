use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{info, warn, error};

use crate::cli::run_cli_command;
use crate::config::AppConfig;
use crate::provider::get_provider_for_phase;
use crate::state;
use crate::types::ResearchJob;

/// Run background research for an idea.
pub async fn run_research(
    idea_id: String,
    title: String,
    description: String,
    config: AppConfig,
    research_jobs: Arc<RwLock<std::collections::HashMap<String, ResearchJob>>>,
) {
    let prompt = format!(
        "Research the following idea comprehensively. \
         Search the web for relevant APIs, tools, libraries, frameworks, and approaches. \
         Search GitHub for reference implementations and examples. \
         Provide a detailed research report with: \
         1) Overview of available solutions, \
         2) Recommended tools/APIs with links, \
         3) Implementation approaches, \
         4) Potential challenges and considerations. \
         \n\nIdea: {}\n\nDetails: {}",
        title,
        if description.is_empty() {
            "No additional details provided."
        } else {
            &description
        }
    );

    info!("Starting research for idea: {}", title);
    {
        let mut jobs = research_jobs.write().await;
        jobs.insert(
            idea_id.clone(),
            ResearchJob {
                status: "researching".into(),
                result: None,
            },
        );
    }

    match get_provider_for_phase(&config, "implement", None) {
        Ok(provider) => {
            let (success, output) =
                run_cli_command(&provider, "implement", &prompt, None, None, &config.project_dir)
                    .await;

            if success && !output.is_empty() {
                // Save research to idea
                let file_lock = tokio::sync::Mutex::new(());
                let _guard = file_lock.lock().await;
                let mut ideas = state::load_ideas(&config.ideas_file);
                for idea in ideas.iter_mut() {
                    if idea.id == idea_id {
                        idea.extra.insert(
                            "research".into(),
                            serde_json::Value::String(output.clone()),
                        );
                        idea.extra.insert(
                            "research_status".into(),
                            serde_json::Value::String("done".into()),
                        );
                        idea.extra.insert(
                            "researched_at".into(),
                            serde_json::Value::String(chrono::Utc::now().to_rfc3339()),
                        );
                        idea.updated = Some(state::ts_ms());
                        break;
                    }
                }
                state::save_ideas(&config.ideas_file, &ideas).ok();

                let mut jobs = research_jobs.write().await;
                jobs.insert(
                    idea_id,
                    ResearchJob {
                        status: "done".into(),
                        result: Some(output),
                    },
                );
                info!("Research complete for idea: {}", title);
            } else {
                warn!("Research produced no output for: {}", title);
                let mut jobs = research_jobs.write().await;
                jobs.insert(
                    idea_id,
                    ResearchJob {
                        status: "error".into(),
                        result: Some("No output.".into()),
                    },
                );
            }
        }
        Err(e) => {
            error!("Research failed for {}: {}", title, e);
            let mut jobs = research_jobs.write().await;
            jobs.insert(
                idea_id,
                ResearchJob {
                    status: "error".into(),
                    result: Some(e.to_string()),
                },
            );
        }
    }
}
