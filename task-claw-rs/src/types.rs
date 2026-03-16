use serde::{Deserialize, Serialize};
use std::collections::HashMap;

// ── Task / Idea ─────────────────────────────────────────────────────────

/// Task or Idea — uses flatten to preserve unknown fields for round-trip fidelity.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskItem {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub description: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cli_provider: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub plan: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub created: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub updated: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub photos: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ai_analysis: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pipeline_summary: Option<serde_json::Value>,
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

// ── Agent State ──────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AgentState {
    #[serde(default)]
    pub processed: Vec<String>,
    #[serde(default)]
    pub api_calls_today: i64,
    #[serde(default)]
    pub api_date: String,
}

// ── Agent Status (runtime) ──────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentStatus {
    pub state: String,
    pub current_task: Option<String>,
    pub current_stage: Option<String>,
    pub last_run: Option<String>,
    pub last_trigger: Option<String>,
    pub tasks_pending: usize,
    pub ideas_pending: usize,
    pub api_calls_today: i64,
    pub api_limit: i64,
    #[serde(default)]
    pub stage_log: Vec<StageLogEntry>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pipeline_started: Option<String>,
    #[serde(default)]
    pub force_no_age_filter: bool,
}

impl Default for AgentStatus {
    fn default() -> Self {
        Self {
            state: "starting".into(),
            current_task: None,
            current_stage: None,
            last_run: None,
            last_trigger: None,
            tasks_pending: 0,
            ideas_pending: 0,
            api_calls_today: 0,
            api_limit: 10,
            stage_log: vec![],
            pipeline_started: None,
            force_no_age_filter: false,
        }
    }
}

// ── Pipeline Config ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineConfig {
    #[serde(default)]
    pub program_manager: PmConfig,
    #[serde(default)]
    pub stages: HashMap<String, StageConfig>,
    #[serde(default)]
    pub publish: PublishConfig,
    #[serde(default)]
    pub hooks: HooksConfig,
}

impl Default for PipelineConfig {
    fn default() -> Self {
        let mut stages = HashMap::new();
        stages.insert("rewrite".into(), StageConfig {
            enabled: true, team: vec![], timeout: 120,
            description: Some("PM rewrites the raw prompt for clarity".into()),
        });
        stages.insert("plan".into(), StageConfig {
            enabled: true, team: vec!["claude".into()], timeout: 900,
            description: Some("Architect the approach".into()),
        });
        stages.insert("code".into(), StageConfig {
            enabled: true, team: vec!["claude".into()], timeout: 600,
            description: Some("Implement the plan".into()),
        });
        stages.insert("simplify".into(), StageConfig {
            enabled: true, team: vec!["claude".into()], timeout: 300,
            description: Some("Review and simplify".into()),
        });
        stages.insert("test".into(), StageConfig {
            enabled: true, team: vec!["claude".into()], timeout: 300,
            description: Some("Verify correctness".into()),
        });
        stages.insert("review".into(), StageConfig {
            enabled: true, team: vec!["claude".into()], timeout: 300,
            description: Some("Security and quality audit".into()),
        });
        Self {
            program_manager: PmConfig::default(),
            stages,
            publish: PublishConfig::default(),
            hooks: HooksConfig::default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PmConfig {
    #[serde(default = "default_pm_backend")]
    pub backend: String,
    #[serde(default = "default_pm_model")]
    pub model: String,
    #[serde(default = "default_pm_max_tokens")]
    pub max_tokens: u32,
    #[serde(default = "default_pm_temperature")]
    pub temperature: f32,
    #[serde(default)]
    pub extract_requirements: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

fn default_pm_backend() -> String { "github_models".into() }
fn default_pm_model() -> String { "gpt-4o".into() }
fn default_pm_max_tokens() -> u32 { 4096 }
fn default_pm_temperature() -> f32 { 0.3 }

impl Default for PmConfig {
    fn default() -> Self {
        Self {
            backend: default_pm_backend(),
            model: default_pm_model(),
            max_tokens: default_pm_max_tokens(),
            temperature: default_pm_temperature(),
            extract_requirements: false,
            note: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StageConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub team: Vec<String>,
    #[serde(default = "default_timeout")]
    pub timeout: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
}

fn default_true() -> bool { true }
fn default_timeout() -> u64 { 300 }

impl Default for StageConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            team: vec!["claude".into()],
            timeout: 300,
            description: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PublishConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_true")]
    pub auto_push: bool,
    #[serde(default = "default_block_severity")]
    pub block_on_severity: String,
}

fn default_block_severity() -> String { "high".into() }

impl Default for PublishConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            auto_push: true,
            block_on_severity: "high".into(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct HooksConfig {
    #[serde(default)]
    pub on_stage_start: Vec<HookEntry>,
    #[serde(default)]
    pub on_stage_end: Vec<HookEntry>,
    #[serde(default)]
    pub on_verdict: Vec<HookEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HookEntry {
    #[serde(default)]
    pub r#type: String,
    #[serde(default)]
    pub url: String,
    #[serde(default = "default_hook_timeout")]
    pub timeout: u64,
    #[serde(default)]
    pub can_override: bool,
}

fn default_hook_timeout() -> u64 { 5 }

// ── Provider ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProvidersConfig {
    #[serde(default)]
    pub providers: HashMap<String, Provider>,
    #[serde(default = "default_provider_name")]
    pub default_provider: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub default_planning_provider: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub default_implement_provider: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub default_security_provider: Option<String>,
}

fn default_provider_name() -> String { "claude".into() }

impl Default for ProvidersConfig {
    fn default() -> Self {
        Self {
            providers: HashMap::new(),
            default_provider: "claude".into(),
            default_planning_provider: None,
            default_implement_provider: None,
            default_security_provider: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Provider {
    #[serde(default)]
    pub name: String,
    pub binary: String,
    #[serde(default)]
    pub subcommand: Vec<String>,
    #[serde(default)]
    pub plan_args: Vec<String>,
    #[serde(default)]
    pub implement_args: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub simplify_args: Option<Vec<String>>,
    #[serde(default)]
    pub security_args: Vec<String>,
    #[serde(default)]
    pub test_args: Vec<String>,
    #[serde(default)]
    pub review_args: Vec<String>,
    #[serde(default = "default_plan_timeout")]
    pub plan_timeout: u64,
    #[serde(default = "default_impl_timeout")]
    pub implement_timeout: u64,
    #[serde(default = "default_sec_timeout")]
    pub security_timeout: u64,
    #[serde(default = "default_sec_timeout")]
    pub test_timeout: u64,
    #[serde(default = "default_sec_timeout")]
    pub review_timeout: u64,
    #[serde(default)]
    pub env: HashMap<String, String>,
    #[serde(default)]
    pub notes: String,
}

fn default_plan_timeout() -> u64 { 900 }
fn default_impl_timeout() -> u64 { 600 }
fn default_sec_timeout() -> u64 { 300 }

// ── Skill ────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SkillsConfig {
    #[serde(default)]
    pub skills: HashMap<String, Skill>,
}

impl Default for SkillsConfig {
    fn default() -> Self {
        Self { skills: HashMap::new() }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Skill {
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub provider: Option<String>,
    #[serde(default = "default_timeout")]
    pub timeout: u64,
    #[serde(default = "default_phase")]
    pub phase: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub triggers: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skill_file: Option<String>,
}

fn default_phase() -> String { "implement".into() }

// ── Skill Run ────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SkillRun {
    pub status: String,
    pub skill_id: String,
    pub skill_name: String,
    pub input: String,
    pub started: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub finished: Option<String>,
    #[serde(default)]
    pub output: String,
    #[serde(default)]
    pub elapsed: f64,
    #[serde(default)]
    pub success: bool,
}

// ── Research Job ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResearchJob {
    pub status: String,
    pub result: Option<String>,
}

// ── Prompts Config ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PromptsConfig {
    #[serde(default)]
    pub pm_system: HashMap<String, String>,
    #[serde(default)]
    pub pm_stage_guidance: HashMap<String, String>,
    #[serde(default)]
    pub pm_stage_criteria: HashMap<String, String>,
    #[serde(default)]
    pub cli_prompts: HashMap<String, String>,
    #[serde(default)]
    pub rewrite_format: Option<String>,
    #[serde(default)]
    pub pm_extract_requirements: Option<String>,
    #[serde(default)]
    pub pm_plan_checklist: Option<String>,
    #[serde(default)]
    pub pm_code_traceability: Option<String>,
}

// ── Overseer Result ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OverseerResult {
    pub verdict: String,
    pub synthesis: String,
    pub handoff: String,
    pub issues: Vec<String>,
    pub full_response: String,
    #[serde(default)]
    pub pm_succeeded: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub team_outputs: Option<Vec<(String, String)>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cross_reviews: Option<Vec<(String, String)>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comparison_summary: Option<String>,
}

// ── Security Review ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SecurityReview {
    #[serde(default = "default_true")]
    pub passed: bool,
    #[serde(default = "default_severity_none")]
    pub severity: String,
    #[serde(default)]
    pub findings: Vec<SecurityFinding>,
    #[serde(default)]
    pub report: String,
}

fn default_severity_none() -> String { "none".into() }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SecurityFinding {
    #[serde(default)]
    pub severity: String,
    #[serde(default)]
    pub file: String,
    #[serde(default)]
    pub line: String,
    #[serde(default)]
    pub issue: String,
    #[serde(default)]
    pub fix: String,
}

// ── Pipeline Stats ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct StageStats {
    pub cli_calls: usize,
    pub subagents: usize,
    pub tool_calls: HashMap<String, usize>,
}

// ── Stage Log Entry ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StageLogEntry {
    pub stage: String,
    pub elapsed: f64,
    pub verdict: String,
    #[serde(default)]
    pub issues: Vec<String>,
    #[serde(default)]
    pub team: Vec<String>,
    #[serde(default)]
    pub note: String,
    #[serde(default)]
    pub output: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_file: Option<String>,
}

// ── Pipeline Result ──────────────────────────────────────────────────────

#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineResult {
    pub success: bool,
    pub stage_results: HashMap<String, String>,
    pub stage_log: Vec<StageLogEntry>,
    pub pipeline_elapsed: f64,
    pub published: bool,
    pub error: Option<String>,
    #[serde(default)]
    pub stats: HashMap<String, StageStats>,
}
