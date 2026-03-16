use std::collections::HashMap;
use std::path::Path;
use tracing::{info, warn};

use crate::state::load_json_file;
use crate::types::PromptsConfig;

const CLI_PROMPT_WARN_CHARS: usize = 500;

/// Lazy-loaded prompts config.
static PROMPTS: once_cell::sync::OnceCell<PromptsConfig> = once_cell::sync::OnceCell::new();

fn fallback_prompts() -> &'static FallbackPrompts {
    static INSTANCE: once_cell::sync::Lazy<FallbackPrompts> =
        once_cell::sync::Lazy::new(FallbackPrompts::new);
    &INSTANCE
}

struct FallbackPrompts {
    pm_system: HashMap<String, String>,
    cli_prompts: HashMap<String, String>,
    rewrite_format: String,
    pm_extract_requirements: String,
    pm_plan_checklist: String,
    pm_code_traceability: String,
}

impl FallbackPrompts {
    fn new() -> Self {
        let mut pm_system = HashMap::new();
        pm_system.insert("director".into(),
            "You are a senior Program Manager directing an AI coding team through a \
             multi-stage pipeline (plan → code → simplify → test → review → publish). \
             You write precise, actionable task briefs.".into());
        pm_system.insert("overseer".into(),
            "You are a senior Program Manager overseeing an AI coding pipeline. \
             Prioritize technical accuracy — disagree when output is wrong. \
             You MUST NOT approve work that is incomplete, incorrect, or has drifted.".into());
        pm_system.insert("merger".into(),
            "You are a senior PM overseeing an AI coding pipeline. \
             Produce a merged result from multiple implementations + cross-reviews. \
             Fix ALL identified issues. Do NOT approve code with known bugs.".into());
        pm_system.insert("rewriter".into(),
            "You are a senior PM preparing a user request for an AI coding pipeline.".into());
        pm_system.insert("cross_reviewer".into(),
            "You are a senior code reviewer. Prioritize technical accuracy. \
             Disagree when code is wrong.".into());

        let mut cli_prompts = HashMap::new();
        cli_prompts.insert("plan".into(),
            "I need to understand the codebase before making changes.\n\n\
             Task context: {prompt}\n\n\
             Output a step-by-step implementation plan with:\n\
             - Actionable verb-led steps with specific files\n\
             - Incremental build order\n\
             - Testing strategy".into());
        cli_prompts.insert("code_suffix".into(),
            "\n\nFollow existing conventions. Use descriptive names. \
             Fix root causes. Keep changes minimal.".into());
        cli_prompts.insert("simplify".into(),
            "Run git diff to see recent changes. Review for quality, then fix \
             issues found: duplicate logic, bad naming, empty catch blocks, dead \
             code, deep nesting. Cap fix iterations at 3 per file.\n\n\
             Original task: {prompt}".into());
        cli_prompts.insert("test".into(),
            "Run git diff to see recent changes, then verify they work correctly. \
             If tests fail, fix the code not the tests. Report specific pass/fail \
             results.\n\nOriginal task: {prompt}".into());
        cli_prompts.insert("review".into(),
            "Run git diff to see recent changes. Perform a defensive security \
             audit: check for hardcoded secrets, PII exposure, injection \
             vulnerabilities. Rate findings LOW/MEDIUM/HIGH.\n\n\
             Original task: {prompt}".into());

        Self {
            pm_system,
            cli_prompts,
            rewrite_format: "Rewrite the following user request to be clear and actionable for a coding AI \
                pipeline that WRITES CODE. If the request is vague or exploratory \
                (research/investigate/look into), convert it into a concrete coding task: \
                diagnose the root cause AND fix it. Structure as: WHAT, WHERE, WHY, CONSTRAINTS. \
                Return only the rewritten prompt.\n\nOriginal request:\n{prompt}".into(),
            pm_extract_requirements: "Extract discrete, testable requirements from this user request. \
                Each requirement should be ONE specific thing the code must do or the plan must address. \
                Number each (1., 2., etc.).\n\nUser request:\n{prompt}".into(),
            pm_plan_checklist: "## Requirements Checklist\n\
                For each requirement, state COVERED or MISSING:\n{checklist}\n\n\
                If ANY requirement is MISSING, verdict MUST be REVISE.".into(),
            pm_code_traceability: "## Plan-to-Code Traceability\n\
                For each plan step, state DONE, PARTIAL, or MISSING:\n{plan}\n\n\
                If ANY step is MISSING, verdict MUST be REVISE with unimplemented steps listed.".into(),
        }
    }
}

/// Initialize prompts from a file. Call once at startup.
pub fn init_prompts(prompts_file: &Path) {
    let config = load_json_file::<PromptsConfig>(prompts_file, "prompts.json")
        .unwrap_or_default();
    if PROMPTS.set(config).is_err() {
        warn!("Prompts already initialized");
    } else {
        info!("Loaded prompts from {}", prompts_file.display());
    }
}

/// Get the loaded prompts config (or default).
fn loaded() -> &'static PromptsConfig {
    PROMPTS.get_or_init(PromptsConfig::default)
}

/// Get a prompt template from prompts.json or fallbacks.
///
/// When `key` is None, looks up a top-level field (e.g. "rewrite_format").
/// When `key` is given, looks up section[key] (e.g. "pm_system"/"director").
pub fn get_prompt(section: &str, key: Option<&str>, fallback: &str) -> String {
    let prompts = loaded();
    let fb = fallback_prompts();

    match key {
        None => {
            // Top-level fields
            match section {
                "rewrite_format" => prompts
                    .rewrite_format
                    .clone()
                    .unwrap_or_else(|| fb.rewrite_format.clone()),
                "pm_extract_requirements" => prompts
                    .pm_extract_requirements
                    .clone()
                    .unwrap_or_else(|| fb.pm_extract_requirements.clone()),
                "pm_plan_checklist" => prompts
                    .pm_plan_checklist
                    .clone()
                    .unwrap_or_else(|| fb.pm_plan_checklist.clone()),
                "pm_code_traceability" => prompts
                    .pm_code_traceability
                    .clone()
                    .unwrap_or_else(|| fb.pm_code_traceability.clone()),
                _ => fallback.to_string(),
            }
        }
        Some(key) => {
            let section_map = match section {
                "pm_system" => &prompts.pm_system,
                "pm_stage_guidance" => &prompts.pm_stage_guidance,
                "pm_stage_criteria" => &prompts.pm_stage_criteria,
                "cli_prompts" => &prompts.cli_prompts,
                _ => return fallback.to_string(),
            };

            if let Some(val) = section_map.get(key) {
                return val.clone();
            }

            // Try fallback maps
            let fb_map = match section {
                "pm_system" => Some(&fb.pm_system),
                "cli_prompts" => Some(&fb.cli_prompts),
                _ => None,
            };

            if let Some(map) = fb_map {
                if let Some(val) = map.get(key) {
                    return val.clone();
                }
            }

            fallback.to_string()
        }
    }
}

/// Log a warning if CLI prompt instructions exceed safe threshold.
pub fn warn_cli_prompt_size(stage: &str, prompt: &str, dynamic_len: usize) {
    let instruction_len = prompt.len().saturating_sub(dynamic_len);
    if instruction_len > CLI_PROMPT_WARN_CHARS {
        warn!(
            "CLI prompt for '{}' has {} instruction chars (threshold {}) — risk of permission prompts",
            stage, instruction_len, CLI_PROMPT_WARN_CHARS
        );
    }
}
