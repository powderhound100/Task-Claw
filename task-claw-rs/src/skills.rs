use std::collections::HashMap;
use regex::Regex;
use tracing::{info, warn};

use crate::cli::run_cli_command;
use crate::config::AppConfig;
use crate::provider::get_provider_for_phase;
use crate::state::load_json_file;
use crate::types::{Skill, SkillsConfig};

/// Load user-defined skills from skills.json.
pub fn load_skills(config: &AppConfig) -> SkillsConfig {
    load_json_file::<SkillsConfig>(&config.skills_file, "skills.json")
        .unwrap_or_default()
}

/// Save skills config.
pub fn save_skills(config: &AppConfig, data: &SkillsConfig) -> std::io::Result<()> {
    let json = serde_json::to_string_pretty(data)?;
    std::fs::write(&config.skills_file, json)?;
    Ok(())
}

/// Auto-discover skills from .claude/skills/ directories.
pub fn discover_env_skills(config: &AppConfig) -> HashMap<String, Skill> {
    let mut discovered = HashMap::new();
    let skills_root = config.project_dir.join(".claude").join("skills");

    if !skills_root.is_dir() {
        return discovered;
    }

    let entries = match std::fs::read_dir(&skills_root) {
        Ok(e) => e,
        Err(_) => return discovered,
    };

    for entry in entries.flatten() {
        let skill_dir = entry.path();
        if !skill_dir.is_dir() {
            continue;
        }

        let skill_file = skill_dir.join("SKILL.md");
        if !skill_file.exists() {
            continue;
        }

        match std::fs::read_to_string(&skill_file) {
            Ok(text) => {
                let name_re = Regex::new(r"(?m)^#\s+(.+)").unwrap();
                let name = name_re
                    .captures(&text)
                    .map(|c| c[1].trim().to_string())
                    .unwrap_or_else(|| {
                        skill_dir
                            .file_name()
                            .map(|n| n.to_string_lossy().to_string())
                            .unwrap_or_default()
                    });

                let desc_re = Regex::new(r"(?s)## Description\n+(.+?)(?:\n##|\z)").unwrap();
                let description = desc_re
                    .captures(&text)
                    .map(|c| c[1].trim().lines().next().unwrap_or("").to_string())
                    .unwrap_or_default();

                let mut triggers = Vec::new();
                let trig_re = Regex::new(r"(?s)## Triggers\n+(.+?)(?:\n##|\z)").unwrap();
                if let Some(caps) = trig_re.captures(&text) {
                    for line in caps[1].trim().lines() {
                        let line = line.trim().trim_start_matches("- ").trim_matches('"');
                        if !line.is_empty() {
                            triggers.push(line.to_string());
                        }
                    }
                }

                let dir_name = skill_dir
                    .file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_default();
                let skill_id = format!("env:{}", dir_name);

                discovered.insert(
                    skill_id,
                    Skill {
                        name,
                        description,
                        prompt: format!(
                            "Follow the instructions in {} to complete this task. {{input}}",
                            skill_file.display()
                        ),
                        provider: None,
                        timeout: 300,
                        phase: "implement".into(),
                        tags: vec!["environment".into()],
                        source: Some("environment".into()),
                        triggers: Some(triggers),
                        skill_file: Some(skill_file.to_string_lossy().to_string()),
                    },
                );
            }
            Err(e) => {
                warn!("Could not parse skill {:?}: {}", skill_dir.file_name(), e);
            }
        }
    }

    discovered
}

/// Get merged dict of user-defined + environment-discovered skills.
pub fn get_all_skills(config: &AppConfig) -> HashMap<String, Skill> {
    let user_skills = load_skills(config).skills;
    let env_skills = discover_env_skills(config);
    let mut merged = HashMap::new();
    merged.extend(env_skills);
    merged.extend(user_skills);
    merged
}

/// Execute a skill by running its prompt through a CLI provider.
pub async fn run_skill(
    skill_id: &str,
    input_text: &str,
    provider_override: Option<&str>,
    config: &AppConfig,
) -> SkillRunResult {
    let all_skills = get_all_skills(config);
    let skill = match all_skills.get(skill_id) {
        Some(s) => s,
        None => {
            return SkillRunResult {
                success: false,
                output: format!("Skill '{}' not found", skill_id),
                run_id: String::new(),
                elapsed: 0.0,
            };
        }
    };

    let run_id = format!(
        "skill-{}-{}",
        chrono::Utc::now().timestamp(),
        &uuid::Uuid::new_v4().to_string()[..6]
    );

    let prompt_template = &skill.prompt;
    let prompt = prompt_template.replace("{input}", input_text).trim().to_string();
    if prompt.is_empty() {
        return SkillRunResult {
            success: false,
            output: "Skill has no prompt template".into(),
            run_id,
            elapsed: 0.0,
        };
    }

    let phase = &skill.phase;
    let provider_name = provider_override
        .map(|s| s.to_string())
        .or_else(|| skill.provider.clone());

    let provider = match get_provider_for_phase(config, phase, provider_name.as_deref()) {
        Ok(p) => p,
        Err(e) => {
            return SkillRunResult {
                success: false,
                output: e.to_string(),
                run_id,
                elapsed: 0.0,
            };
        }
    };

    info!("Skill '{}' started (run_id={})", skill.name, run_id);
    let start = std::time::Instant::now();

    let (success, output) =
        run_cli_command(&provider, phase, &prompt, None, None, &config.project_dir).await;
    let elapsed = start.elapsed().as_secs_f64();

    // Save output
    let out_dir = config.skills_output_dir.join(&run_id);
    std::fs::create_dir_all(&out_dir).ok();
    std::fs::write(out_dir.join("output.md"), &output).ok();

    info!(
        "Skill '{}' {} in {:.1}s (run_id={})",
        skill.name,
        if success { "succeeded" } else { "failed" },
        elapsed,
        run_id
    );

    SkillRunResult {
        success,
        output,
        run_id,
        elapsed,
    }
}

#[allow(dead_code)]
pub struct SkillRunResult {
    pub success: bool,
    pub output: String,
    pub run_id: String,
    pub elapsed: f64,
}
