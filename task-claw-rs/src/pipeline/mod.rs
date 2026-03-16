pub mod context;
pub mod garbage;
pub mod hooks;
pub mod output;
pub mod pm;
pub mod stages;
pub mod stats;
pub mod team;

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use chrono::Utc;
use tokio::sync::RwLock;
use tracing::{info, warn, error};

use crate::config::AppConfig;
use crate::git;
use crate::security;
use crate::services;
use crate::types::*;

/// Full pipeline: rewrite -> plan -> code -> simplify -> test -> review -> publish.
pub async fn run_pipeline(
    prompt: &str,
    task_id: Option<&str>,
    pipeline_cfg: Option<&PipelineConfig>,
    start_stage: Option<&str>,
    config: &AppConfig,
    agent_status: &Arc<RwLock<AgentStatus>>,
    http_client: &reqwest::Client,
    stats_tracker: &stats::PipelineStatsTracker,
) -> PipelineResult {
    let default_cfg = PipelineConfig::default();
    let _cfg = pipeline_cfg.unwrap_or_else(|| {
        // Load from file at runtime
        &default_cfg
    });
    // Actually load from file
    let loaded_cfg = load_pipeline_config(config);
    let cfg = pipeline_cfg.unwrap_or(&loaded_cfg);

    let stages_cfg = &cfg.stages;
    let pm_cfg = &cfg.program_manager;
    let publish_cfg = &cfg.publish;

    let mut context = String::new();
    let mut stage_results: HashMap<String, String> = HashMap::new();
    let mut stage_log: Vec<StageLogEntry> = Vec::new();
    let pipeline_start = Instant::now();
    stats_tracker.reset();
    let original_prompt = prompt.to_string();
    let mut prompt = prompt.to_string();
    let tid = task_id
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("pipeline-{}", chrono::Utc::now().timestamp()));
    let mut pm_consecutive_failures: usize = 0;
    let mut test_passed = true;
    let mut code_made_changes = false;
    let mut extracted_requirements: Vec<String> = Vec::new();

    // Expose pipeline start time
    {
        let mut status = agent_status.write().await;
        status.pipeline_started = Some(Utc::now().to_rfc3339());
    }

    let stage_order = ["rewrite", "plan", "code", "simplify", "test", "review"];
    let mut skip = start_stage.is_some();

    // PM health check
    let pm_available = pm::pm_health_check(pm_cfg, config);
    if !pm_available {
        warn!("PM backend unavailable — running pipeline in DIRECT mode");
    } else {
        info!("PM backend config OK");
    }

    info!(
        "Pipeline starting for: {} (start_stage={}, pm={})",
        tid,
        start_stage.unwrap_or("rewrite"),
        if pm_available { "yes" } else { "DIRECT" }
    );

    for stage in &stage_order {
        if skip {
            if Some(*stage) == start_stage {
                skip = false;
            } else {
                info!("Skipping stage '{}'", stage);
                continue;
            }
        }

        let stage_cfg = stages_cfg.get(*stage).cloned().unwrap_or_default();
        if !stage_cfg.enabled {
            info!("Stage '{}' disabled", stage);
            continue;
        }

        // Skip verification stages if code made no changes
        if (*stage == "simplify" || *stage == "test") && !code_made_changes {
            info!("Skipping '{}' — code stage made no file changes", stage);
            stage_log.push(StageLogEntry {
                stage: stage.to_string(),
                elapsed: 0.0,
                verdict: "skipped".into(),
                issues: vec![],
                team: vec![],
                note: "No code changes to verify".into(),
                output: String::new(),
                output_file: None,
            });
            continue;
        }

        // Wallclock timeout
        if config.pipeline_wallclock_timeout > 0
            && pipeline_start.elapsed().as_secs() > config.pipeline_wallclock_timeout
        {
            error!(
                "Pipeline wallclock timeout ({}s) exceeded — aborting at stage '{}'",
                config.pipeline_wallclock_timeout, stage
            );
            stage_log.push(StageLogEntry {
                stage: stage.to_string(),
                elapsed: 0.0,
                verdict: "timeout".into(),
                issues: vec!["Pipeline wallclock timeout".into()],
                team: vec![],
                note: "Aborted — pipeline ran too long".into(),
                output: String::new(),
                output_file: None,
            });
            break;
        }

        let env_timeout_key = format!("PIPELINE_{}_TIMEOUT", stage.to_uppercase());
        let timeout_val = std::env::var(&env_timeout_key)
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(stage_cfg.timeout);
        let timeout = if timeout_val == 0 {
            None
        } else {
            Some(timeout_val)
        };
        let team_names = if stage_cfg.team.is_empty() {
            vec!["claude".to_string()]
        } else {
            stage_cfg.team.clone()
        };

        let stage_start = Instant::now();
        {
            let mut status = agent_status.write().await;
            status.state = format!("pipeline:{}", stage);
            status.current_stage = Some(stage.to_string());
            status.stage_log = stage_log.clone();
        }

        // ── Rewrite: PM-only, no CLI team ───────────────────────────────
        if *stage == "rewrite" {
            if pm_available && pm_consecutive_failures < 2 {
                let new_prompt =
                    pm::rewrite_prompt(&original_prompt, pm_cfg, config, http_client).await;
                if new_prompt == original_prompt {
                    pm_consecutive_failures += 1;
                    warn!("PM rewrite failed ({} consecutive)", pm_consecutive_failures);
                } else {
                    pm_consecutive_failures = 0;
                    prompt = new_prompt;
                }
            } else {
                info!("[DIRECT] Skipping PM rewrite — using original prompt");
            }
            stage_results.insert("rewrite".into(), prompt.clone());
            output::save_stage_output(
                &config.pipeline_output_dir,
                &tid,
                "rewrite",
                &prompt,
                if pm_available {
                    "PM-rewritten prompt for clarity."
                } else {
                    "Direct mode — original prompt."
                },
            );

            // Extract requirements after rewrite
            if pm_cfg.extract_requirements && pm_available {
                extracted_requirements =
                    pm::pm_extract_requirements(&prompt, pm_cfg, config, http_client).await;
            }

            stage_log.push(StageLogEntry {
                stage: "rewrite".into(),
                elapsed: stage_start.elapsed().as_secs_f64(),
                verdict: "done".into(),
                issues: vec![],
                team: vec![if pm_available { "pm" } else { "direct" }.into()],
                note: prompt.chars().take(200).collect(),
                output: prompt.chars().take(2000).collect(),
                output_file: Some(
                    output::stage_output_path(&config.pipeline_output_dir, &tid, "rewrite")
                        .to_string_lossy()
                        .to_string(),
                ),
            });
            let mut status = agent_status.write().await;
            status.stage_log = stage_log.clone();
            continue;
        }

        // ── Review: use structured security review ──────────────────────
        if *stage == "review" {
            let review = security::run_security_review(&tid, &prompt[..80.min(prompt.len())], config).await;
            let team_outputs = vec![("security-review".to_string(), review.report.clone())];
            let pm_result = pm::pm_oversee_stage(
                stage,
                &original_prompt,
                &context,
                &team_outputs,
                pm_cfg,
                config,
                http_client,
                None,
            )
            .await;

            stage_results.insert("review".into(), pm_result.full_response.clone());
            output::save_stage_output(
                &config.pipeline_output_dir,
                &tid,
                "review",
                &pm_result.full_response,
                &format!("Verdict: {}", pm_result.verdict),
            );
            context.push_str(&format!(
                "\n\n=== Review stage output ===\n{}\n=== End review ===",
                pm_result.handoff
            ));
            context = self::context::cap_context(&context, 12000);

            let action =
                security::handle_security_findings(&review, &tid, config).await;

            let verdict = if action == "blocked" {
                "blocked"
            } else {
                &pm_result.verdict
            };
            stage_log.push(StageLogEntry {
                stage: "review".into(),
                elapsed: stage_start.elapsed().as_secs_f64(),
                verdict: verdict.to_string(),
                issues: pm_result.issues.clone(),
                team: vec!["security-review".into()],
                note: review.report.chars().take(200).collect(),
                output: review.report.chars().take(2000).collect(),
                output_file: Some(
                    output::stage_output_path(&config.pipeline_output_dir, &tid, "review")
                        .to_string_lossy()
                        .to_string(),
                ),
            });
            let mut status = agent_status.write().await;
            status.stage_log = stage_log.clone();

            if action == "blocked" {
                warn!("Pipeline blocked by security review for: {}", tid);
                return PipelineResult {
                    success: false,
                    stage_results,
                    stage_log,
                    pipeline_elapsed: pipeline_start.elapsed().as_secs_f64(),
                    published: false,
                    error: Some("Blocked by security review (HIGH severity)".into()),
                    stats: stats_tracker.get_summary(),
                };
            }
            continue;
        }

        // ── Plan / Code / Simplify / Test ─────────────────────────────
        let use_direct = !pm_available || pm_consecutive_failures >= 2;

        // Fire on_stage_start hooks
        let hook_data = serde_json::json!({
            "task_id": tid,
            "stage": stage,
            "team": team_names,
            "prompt_preview": &prompt[..500.min(prompt.len())],
        });
        let hook_responses =
            hooks::fire_hooks("on_stage_start", hook_data, &cfg.hooks, http_client).await;
        for hr in &hook_responses {
            if let Some(injected) = hr.get("inject_context").and_then(|v| v.as_str()) {
                context.push_str(&format!(
                    "\n\n=== Hook injected context ===\n{}\n=== End hook context ===",
                    injected
                ));
                info!("Hook injected {} chars of context", injected.len());
            }
        }

        if use_direct {
            // DIRECT mode
            let direct_prompt = stages::build_direct_prompt(stage, &prompt, &context);
            info!("[DIRECT] Built prompt for '{}' ({} chars)", stage, direct_prompt.len());
            let team_outputs =
                team::run_team(stage, &direct_prompt, &team_names, "", timeout, &tid, config).await;

            if team_outputs.is_empty() {
                warn!("Stage '{}' — no team output, continuing", stage);
                stage_results.insert(stage.to_string(), String::new());
            } else if garbage::is_garbage_output(&team_outputs) {
                // Retry with plan context
                warn!("[DIRECT] Garbage output from '{}' — retrying with plan context", stage);
                let plan_ctx = context::extract_plan_context(&context);
                let clean_prompt = stages::build_direct_prompt(stage, &prompt, &plan_ctx);
                let retry_outputs =
                    team::run_team(stage, &clean_prompt, &team_names, "", timeout, &tid, config)
                        .await;

                if !retry_outputs.is_empty() && !garbage::is_garbage_output(&retry_outputs) {
                    if *stage == "code" || *stage == "simplify" {
                        services::restart_changed_services(config).await;
                    }
                    let combined: String = retry_outputs
                        .iter()
                        .map(|(_, out)| out.as_str())
                        .collect::<Vec<_>>()
                        .join("\n\n");
                    stage_results.insert(stage.to_string(), combined.clone());
                    output::save_stage_output(
                        &config.pipeline_output_dir,
                        &tid,
                        stage,
                        &combined,
                        "Direct mode — retry after garbage.",
                    );
                    let clean_out = garbage::clean_stage_output(&combined);
                    let tail = if clean_out.len() > 3000 {
                        &clean_out[clean_out.len() - 3000..]
                    } else {
                        &clean_out
                    };
                    context.push_str(&format!(
                        "\n\n=== {} stage output ===\n{}\n=== End {} ===",
                        capitalize(stage),
                        tail,
                        stage
                    ));
                    context = self::context::cap_context(&context, 12000);
                } else {
                    warn!("[DIRECT] Retry also garbage for '{}' — skipping", stage);
                    stage_results.insert(stage.to_string(), String::new());
                }
            } else {
                if *stage == "code" || *stage == "simplify" {
                    services::restart_changed_services(config).await;
                }
                let combined: String = team_outputs
                    .iter()
                    .map(|(_, out)| out.as_str())
                    .collect::<Vec<_>>()
                    .join("\n\n");
                stage_results.insert(stage.to_string(), combined.clone());
                output::save_stage_output(
                    &config.pipeline_output_dir,
                    &tid,
                    stage,
                    &combined,
                    "Direct mode — no PM oversight.",
                );
                let clean_out = garbage::clean_stage_output(&combined);
                let tail = if clean_out.len() > 3000 {
                    &clean_out[clean_out.len() - 3000..]
                } else {
                    &clean_out
                };
                context.push_str(&format!(
                    "\n\n=== {} stage output ===\n{}\n=== End {} ===",
                    capitalize(stage),
                    tail,
                    stage
                ));
                context = self::context::cap_context(&context, 12000);

                // Test→Code loopback
                if *stage == "test" && context::test_found_failures(&combined) {
                    warn!("Test found failures — looping back to code stage");
                    test_passed = false;
                    let code_team = stages_cfg
                        .get("code")
                        .map(|c| c.team.clone())
                        .unwrap_or_else(|| vec!["claude".into()]);
                    let code_timeout_val = std::env::var("PIPELINE_CODE_TIMEOUT")
                        .ok()
                        .and_then(|v| v.parse().ok())
                        .unwrap_or(
                            stages_cfg
                                .get("code")
                                .map(|c| c.timeout)
                                .unwrap_or(300),
                        );
                    let code_timeout = if code_timeout_val == 0 {
                        None
                    } else {
                        Some(code_timeout_val)
                    };
                    let test_failures = context::extract_test_failures(&combined, 4000);
                    let fix_prompt = format!(
                        "The previous code changes caused test failures. \
                         Fix ONLY the failing tests — do not rewrite unrelated code.\n\n\
                         Original task: {}\n\nTest failures:\n{}",
                        prompt, test_failures
                    );
                    let fix_outputs = team::run_team(
                        "code",
                        &fix_prompt,
                        &code_team,
                        "",
                        code_timeout,
                        &tid,
                        config,
                    )
                    .await;

                    if !fix_outputs.is_empty() && !garbage::is_garbage_output(&fix_outputs) {
                        services::restart_changed_services(config).await;
                        let fix_combined: String = fix_outputs
                            .iter()
                            .map(|(_, out)| out.as_str())
                            .collect::<Vec<_>>()
                            .join("\n\n");
                        output::save_stage_output(
                            &config.pipeline_output_dir,
                            &tid,
                            "code-fix",
                            &fix_combined,
                            "Code fix after test failures.",
                        );
                        test_passed = !context::test_found_failures(&fix_combined);
                        let tail = if fix_combined.len() > 2000 {
                            &fix_combined[fix_combined.len() - 2000..]
                        } else {
                            &fix_combined
                        };
                        context.push_str(&format!(
                            "\n\n=== Code fix output ===\n{}\n=== End code fix ===",
                            tail
                        ));
                        context = self::context::cap_context(&context, 12000);
                    }
                }
            }

            // After code stage, check if files changed
            if *stage == "code" {
                code_made_changes = git::has_uncommitted_changes(config).await;
                if !code_made_changes {
                    warn!("Code stage produced output but no file changes detected");
                }
            }

            let elapsed = stage_start.elapsed().as_secs_f64();
            info!("Stage '{}' done in {:.0}s — DIRECT", stage, elapsed);
            stage_log.push(StageLogEntry {
                stage: stage.to_string(),
                elapsed,
                verdict: "direct".into(),
                issues: vec![],
                team: team_names.clone(),
                note: stage_results
                    .get(*stage)
                    .map(|s| s.chars().take(200).collect())
                    .unwrap_or_default(),
                output: stage_results
                    .get(*stage)
                    .map(|s| s.chars().take(2000).collect())
                    .unwrap_or_default(),
                output_file: Some(
                    output::stage_output_path(&config.pipeline_output_dir, &tid, stage)
                        .to_string_lossy()
                        .to_string(),
                ),
            });
            let mut status = agent_status.write().await;
            status.stage_log = stage_log.clone();
            continue;
        }

        // ── Full PM mode ─────────────────────────────────────────────
        let max_revise = config.pipeline_max_revise;
        for attempt in 0..=max_revise {
            let (directed_prompt, pm_ok) = pm::pm_direct_team(
                stage,
                &prompt,
                &context,
                &team_names,
                pm_cfg,
                config,
                http_client,
            )
            .await;
            if !pm_ok {
                pm_consecutive_failures += 1;
                warn!(
                    "PM failed ({} consecutive) — prompt is direct-mode fallback",
                    pm_consecutive_failures
                );
            } else {
                pm_consecutive_failures = 0;
            }

            let team_outputs =
                team::run_team(stage, &directed_prompt, &team_names, "", timeout, &tid, config)
                    .await;

            if team_outputs.is_empty() {
                warn!("Stage '{}' — no team output, continuing", stage);
                stage_results.insert(stage.to_string(), String::new());
                break;
            }

            if *stage == "code" || *stage == "simplify" {
                services::restart_changed_services(config).await;
            }

            // Code stage with 2+ agents: cross-review + deep merge
            let pm_result = if *stage == "code" && team_outputs.len() >= 2 {
                for (name, agent_output) in &team_outputs {
                    output::save_stage_output(
                        &config.pipeline_output_dir,
                        &tid,
                        &format!("code-{}", name),
                        agent_output,
                        &format!("Individual implementation by {}", name),
                    );
                }
                let cross_reviews = team::cross_review_code(
                    &team_outputs,
                    &context,
                    &original_prompt,
                    timeout,
                    config,
                )
                .await;
                if !cross_reviews.is_empty() {
                    let mut result = team::pm_merge_with_reviews(
                        stage,
                        &original_prompt,
                        &context,
                        &team_outputs,
                        &cross_reviews,
                        pm_cfg,
                        config,
                        http_client,
                    )
                    .await;
                    result.team_outputs = Some(team_outputs.clone());
                    result.cross_reviews = Some(cross_reviews.clone());
                    result.comparison_summary =
                        Some(team::build_comparison_summary(&team_outputs, &cross_reviews));
                    result
                } else {
                    pm::pm_oversee_stage(
                        stage,
                        &original_prompt,
                        &context,
                        &team_outputs,
                        pm_cfg,
                        config,
                        http_client,
                        Some(&extracted_requirements),
                    )
                    .await
                }
            } else {
                pm::pm_oversee_stage(
                    stage,
                    &original_prompt,
                    &context,
                    &team_outputs,
                    pm_cfg,
                    config,
                    http_client,
                    Some(&extracted_requirements),
                )
                .await
            };

            if !pm_result.pm_succeeded {
                pm_consecutive_failures += 1;
                warn!("PM oversight failed ({} consecutive)", pm_consecutive_failures);
            } else {
                pm_consecutive_failures = 0;
            }

            let mut verdict = pm_result.verdict.clone();
            let issues = pm_result.issues.clone();

            if !issues.is_empty() {
                info!(
                    "PM flagged {} issues in '{}': {}",
                    issues.len(),
                    stage,
                    issues.iter().take(3).cloned().collect::<Vec<_>>().join("; ")
                );
            }

            // Fire on_verdict hooks
            let verdict_data = serde_json::json!({
                "task_id": tid,
                "stage": stage,
                "verdict": verdict,
                "issues": issues,
                "attempt": attempt,
                "max_attempts": 1 + max_revise,
            });
            let verdict_responses =
                hooks::fire_hooks("on_verdict", verdict_data, &cfg.hooks, http_client).await;
            for vr in &verdict_responses {
                if let Some(override_verdict) = vr.get("override_verdict").and_then(|v| v.as_str())
                {
                    if override_verdict == "approve" || override_verdict == "revise" {
                        let can_override = cfg
                            .hooks
                            .on_verdict
                            .iter()
                            .any(|h| h.can_override);
                        if can_override {
                            info!("Hook overriding verdict: {} → {}", verdict, override_verdict);
                            verdict = override_verdict.to_string();
                        }
                    }
                }
            }

            if verdict == "revise" && attempt < max_revise {
                warn!(
                    "PM verdict: REVISE (attempt {}/{}) — re-running stage '{}'",
                    attempt + 1,
                    max_revise,
                    stage
                );
                let issues_text = issues
                    .iter()
                    .map(|i| format!("- {}", i))
                    .collect::<Vec<_>>()
                    .join("\n");
                context.push_str(&format!(
                    "\n\nThe previous {} attempt was rejected. Issues found:\n{}\n\nGuidance for retry:\n{}",
                    stage, issues_text, pm_result.handoff
                ));
                context = self::context::cap_context(&context, 12000);
                continue;
            }

            if verdict == "revise" {
                warn!("PM verdict: REVISE but max attempts reached — proceeding");
                if garbage::is_garbage_output(&team_outputs) {
                    warn!("Stage '{}' output is garbage after all retries — dropping", stage);
                }
            }

            if *stage == "test" {
                test_passed = verdict != "revise";
            }
            if *stage == "code" {
                code_made_changes = git::has_uncommitted_changes(config).await;
            }

            stage_results.insert(stage.to_string(), pm_result.full_response.clone());
            output::save_stage_output(
                &config.pipeline_output_dir,
                &tid,
                stage,
                &pm_result.full_response,
                &format!("Verdict: {} | Issues: {}", verdict, issues.len()),
            );
            let clean_handoff = garbage::clean_stage_output(&pm_result.handoff);
            context.push_str(&format!(
                "\n\n=== {} stage output ===\n{}\n=== End {} ===",
                capitalize(stage),
                clean_handoff,
                stage
            ));
            context = self::context::cap_context(&context, 12000);

            let elapsed = stage_start.elapsed().as_secs_f64();
            info!("Stage '{}' done in {:.0}s — PM: {}", stage, elapsed, verdict.to_uppercase());
            stage_log.push(StageLogEntry {
                stage: stage.to_string(),
                elapsed,
                verdict: verdict.clone(),
                issues,
                team: team_names.clone(),
                note: pm_result.handoff.chars().take(200).collect(),
                output: pm_result.synthesis.chars().take(2000).collect(),
                output_file: Some(
                    output::stage_output_path(&config.pipeline_output_dir, &tid, stage)
                        .to_string_lossy()
                        .to_string(),
                ),
            });
            let mut status = agent_status.write().await;
            status.stage_log = stage_log.clone();
            break;
        }
    }

    // ── Publish ──────────────────────────────────────────────────────
    let mut published = false;
    if !test_passed {
        warn!("Skipping publish — test stage indicated failures");
    } else if publish_cfg.enabled && publish_cfg.auto_push {
        let title = if task_id.is_some() {
            tid.clone()
        } else {
            prompt.chars().take(80).collect()
        };
        info!("Publishing: {}", title);
        published = git::git_commit_and_push(&tid, &title, "pipeline", config).await;
    }

    {
        let mut status = agent_status.write().await;
        status.state = "idle".into();
        status.stage_log.clear();
        status.pipeline_started = None;
    }

    // Stats summary
    let stats = stats_tracker.get_summary();
    let total_cli: usize = stats.values().map(|s| s.cli_calls).sum();
    let total_sub: usize = stats.values().map(|s| s.subagents).sum();
    info!("Pipeline stats: {} CLI calls, {} subagents spawned", total_cli, total_sub);

    info!("Pipeline complete for: {} (published={})", tid, published);
    PipelineResult {
        success: true,
        stage_results,
        stage_log,
        pipeline_elapsed: pipeline_start.elapsed().as_secs_f64(),
        published,
        error: None,
        stats,
    }
}

fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        None => String::new(),
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
    }
}

/// Load pipeline config from file.
pub fn load_pipeline_config(config: &AppConfig) -> PipelineConfig {
    crate::state::load_json_file::<PipelineConfig>(&config.pipeline_file, "pipeline.json")
        .unwrap_or_default()
}
