use regex::Regex;
use tracing::{info, warn};

use crate::cli::{run_cli_command, write_prompt_file};
use crate::config::AppConfig;
use crate::prompts::get_prompt;
use crate::provider::get_provider_for_phase;
use crate::types::OverseerResult;

/// Run a team of CLI providers in parallel for a pipeline stage.
/// Returns list of (provider_name, output) for successful runs only.
pub async fn run_team(
    stage_name: &str,
    prompt: &str,
    team_provider_names: &[String],
    context: &str,
    _timeout: Option<u64>,
    task_id: &str,
    config: &AppConfig,
) -> Vec<(String, String)> {
    let phase = match stage_name {
        "rewrite" | "plan" => "plan",
        "code" => "implement",
        "simplify" => "simplify",
        "test" => "test",
        "review" => "review",
        _ => "implement",
    };

    let combined_prompt = if !context.is_empty() {
        format!("{}\n\n{}", prompt, context).trim().to_string()
    } else {
        prompt.trim().to_string()
    };

    // Write prompt to file
    let prompt_file = if !task_id.is_empty() {
        Some(write_prompt_file(
            &combined_prompt,
            task_id,
            stage_name,
            phase,
            &config.pipeline_output_dir,
        ))
    } else {
        None
    };

    let mut handles = Vec::new();
    for provider_name in team_provider_names {
        let name = provider_name.clone();
        let config = config.clone();
        let prompt = combined_prompt.clone();
        let pf = prompt_file.clone();
        let phase = phase.to_string();

        handles.push(tokio::spawn(async move {
            match get_provider_for_phase(&config, &phase, Some(&name)) {
                Ok(provider) => {
                    let (success, output) = run_cli_command(
                        &provider,
                        &phase,
                        &prompt,
                        None,
                        pf.as_deref(),
                        &config.project_dir,
                    )
                    .await;
                    if success && !output.is_empty() {
                        Some((name, output))
                    } else {
                        warn!("Team member '{}' failed or empty", name);
                        None
                    }
                }
                Err(e) => {
                    warn!("Team member '{}' error: {}", name, e);
                    None
                }
            }
        }));
    }

    let mut results = Vec::new();
    for handle in handles {
        match handle.await {
            Ok(Some(result)) => results.push(result),
            Ok(None) => {}
            Err(e) => warn!("Team future error: {}", e),
        }
    }
    results
}

/// Cross-review: each agent reviews the OTHER agent's implementation.
pub async fn cross_review_code(
    team_outputs: &[(String, String)],
    plan_context: &str,
    original_prompt: &str,
    _timeout: Option<u64>,
    config: &AppConfig,
) -> Vec<(String, String)> {
    if team_outputs.len() < 2 {
        return vec![];
    }

    info!(
        "Cross-review: {} implementations to compare",
        team_outputs.len()
    );

    let mut handles = Vec::new();
    for reviewer_idx in 0..team_outputs.len() {
        let reviewer_name = team_outputs[reviewer_idx].0.clone();
        let others: Vec<(String, String)> = team_outputs
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != reviewer_idx)
            .map(|(_, (n, c))| (n.clone(), c.clone()))
            .collect();

        let plan_ctx = plan_context.to_string();
        let orig_prompt = original_prompt.to_string();
        let config = config.clone();

        handles.push(tokio::spawn(async move {
            let other_blocks = others
                .iter()
                .map(|(name, code)| format!("--- Implementation by {} ---\n{}", name, code))
                .collect::<Vec<_>>()
                .join("\n\n");

            let cr_system = get_prompt("pm_system", Some("cross_reviewer"), "");
            let review_prompt = format!(
                "{}\n\n\
                 Original request: {}\n\n\
                 Plan context:\n{}\n\n\
                 Implementations to review:\n\n{}\n\n\
                 Review each implementation for correctness, conventions, and security.\n\n\
                 ## Agreement Points\n[shared approaches]\n\n\
                 ## Divergences\n[differences with file:line refs]\n\n\
                 ## Issues Found\n[bugs, security, convention violations]\n\n\
                 ## Winner Per Component\n[which is better per feature, and why]\n\n\
                 ## Recommended Merge Strategy\n[how to combine the best parts]",
                cr_system, orig_prompt, plan_ctx, other_blocks
            );

            match get_provider_for_phase(&config, "review", Some(&reviewer_name)) {
                Ok(provider) => {
                    let (success, output) = run_cli_command(
                        &provider,
                        "review",
                        &review_prompt,
                        None,
                        None,
                        &config.project_dir,
                    )
                    .await;
                    if success && !output.is_empty() {
                        Some((format!("review-by-{}", reviewer_name), output))
                    } else {
                        warn!("Cross-review by '{}' failed or empty", reviewer_name);
                        None
                    }
                }
                Err(e) => {
                    warn!("Cross-review by '{}' error: {}", reviewer_name, e);
                    None
                }
            }
        }));
    }

    let mut reviews = Vec::new();
    for handle in handles {
        match handle.await {
            Ok(Some(result)) => reviews.push(result),
            Ok(None) => {}
            Err(e) => warn!("Cross-review future error: {}", e),
        }
    }

    info!("Cross-review complete: {} reviews collected", reviews.len());
    reviews
}

/// Extract structured sections from cross-reviews and build a comparison summary.
pub fn build_comparison_summary(
    _team_outputs: &[(String, String)],
    cross_reviews: &[(String, String)],
) -> String {
    let mut summary_parts = Vec::new();
    for (reviewer_name, review) in cross_reviews {
        summary_parts.push(format!("### {}\n", reviewer_name));
        for section in &[
            "Agreement Points",
            "Divergences",
            "Winner Per Component",
            "Recommended Merge Strategy",
        ] {
            let pattern = format!(r"(?is)##\s*{}\s*\n(.*?)(?=\n##|\z)", regex::escape(section));
            if let Ok(re) = Regex::new(&pattern) {
                if let Some(m) = re.captures(review) {
                    let content = m[1].trim();
                    let truncated = if content.len() > 500 {
                        &content[..500]
                    } else {
                        content
                    };
                    summary_parts.push(format!("**{}:** {}\n", section, truncated));
                }
            }
        }
    }

    if summary_parts.is_empty() {
        "No structured comparison available.".to_string()
    } else {
        summary_parts.join("\n")
    }
}

/// PM deep merge using both implementations + cross-reviews.
pub async fn pm_merge_with_reviews(
    stage_name: &str,
    original_prompt: &str,
    context: &str,
    team_outputs: &[(String, String)],
    cross_reviews: &[(String, String)],
    pm_cfg: &crate::types::PmConfig,
    config: &AppConfig,
    http_client: &reqwest::Client,
) -> OverseerResult {
    let impl_blocks = team_outputs
        .iter()
        .map(|(name, output)| format!("--- Implementation by {} ---\n{}", name, output))
        .collect::<Vec<_>>()
        .join("\n\n");
    let review_blocks = cross_reviews
        .iter()
        .map(|(name, output)| format!("--- {} ---\n{}", name, output))
        .collect::<Vec<_>>()
        .join("\n\n");

    let system_msg = get_prompt("pm_system", Some("merger"), "");
    let user_msg = format!(
        "Stage: {}\nOriginal user request: {}\n\n\
         Prior pipeline context:\n{}\n\n\
         === IMPLEMENTATIONS ===\n\n{}\n\n\
         === CROSS-REVIEWS ===\n\n{}\n\n\
         Perform a deep merge:\n\
         1. **Compare**: differences and agreements\n\
         2. **Gap analysis**: gaps in each implementation\n\
         3. **Strength mapping**: strongest contributions\n\
         4. **Merged result**: best unified implementation\n\
         5. **Verdict**: APPROVE or REVISE\n\n\
         Return:\n\
         ## Comparison\n[analysis]\n\n\
         ## Verdict\nAPPROVE or REVISE\n\n\
         ## Issues\n[problems, or 'None']\n\n\
         ## Synthesis\n[merged implementation]\n\n\
         ## Handoff to next stage\n[context]",
        stage_name, original_prompt, context, impl_blocks, review_blocks
    );

    info!(
        "PM [deep-merge/{}]: merging {} implementations with {} cross-reviews…",
        stage_name,
        team_outputs.len(),
        cross_reviews.len()
    );

    match super::pm::pm_api_call(&system_msg, &user_msg, pm_cfg, config, http_client).await {
        Ok(result) => super::pm::parse_overseer_response(&result),
        Err(e) => {
            warn!("PM deep-merge failed ({}) — falling back to basic oversight", e);
            super::pm::pm_oversee_stage(
                stage_name,
                original_prompt,
                context,
                team_outputs,
                pm_cfg,
                config,
                http_client,
                None,
            )
            .await
        }
    }
}
