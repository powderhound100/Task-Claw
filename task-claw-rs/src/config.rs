use std::path::PathBuf;
use tracing::warn;

/// Parse an env var as an integer with a default fallback.
fn env_int(key: &str, default: i64) -> i64 {
    match std::env::var(key) {
        Ok(val) => val.parse().unwrap_or_else(|_| {
            warn!("Invalid int for env var {}={} — using default {}", key, val, default);
            default
        }),
        Err(_) => default,
    }
}

fn env_str(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn env_bool(key: &str, default: bool) -> bool {
    match std::env::var(key) {
        Ok(val) => val.to_lowercase() == "true",
        Err(_) => default,
    }
}

#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct AppConfig {
    pub agent_version: String,
    pub agent_dir: PathBuf,
    pub project_dir: PathBuf,
    pub data_dir: PathBuf,
    pub photos_dir: PathBuf,
    pub tasks_file: PathBuf,
    pub ideas_file: PathBuf,
    pub state_file: PathBuf,
    pub log_file: PathBuf,
    pub web_dir: PathBuf,
    pub research_dir: PathBuf,
    pub security_review_dir: PathBuf,
    pub pipeline_output_dir: PathBuf,
    pub skills_output_dir: PathBuf,
    pub providers_file: PathBuf,
    pub skills_file: PathBuf,
    pub pipeline_file: PathBuf,

    pub poll_interval: u64,
    pub github_token: String,
    pub trigger_port: u16,
    pub github_models_url: String,
    pub github_models_model: String,
    pub max_api_calls_per_day: i64,
    pub auto_implement_default: bool,

    pub api_key: String,
    pub cors_origin: String,
    pub max_request_body: usize,

    pub pipeline_manager_timeout: u64,
    pub pipeline_max_revise: usize,
    pub pipeline_wallclock_timeout: u64,

    pub restart_service_map: String,

    pub anthropic_api_key: String,
    pub pipeline_pm_url: String,
    pub pipeline_pm_key: String,
}

impl AppConfig {
    pub fn from_env() -> Self {
        let agent_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));

        // For development, use CWD if it contains Cargo.toml (running via cargo run)
        let agent_dir = if agent_dir.join("Cargo.toml").exists() {
            agent_dir
        } else {
            let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            if cwd.join("Cargo.toml").exists() || cwd.join("task-claw.py").exists() {
                cwd
            } else {
                agent_dir
            }
        };

        // Try to find the parent Task-Claw directory (we're in task-claw-rs/)
        let base_dir = if agent_dir.join("task-claw.py").exists() {
            agent_dir.clone()
        } else if agent_dir.parent().map_or(false, |p| p.join("task-claw.py").exists()) {
            agent_dir.parent().unwrap().to_path_buf()
        } else {
            agent_dir.clone()
        };

        let project_dir_setting = env_str("PROJECT_DIR", base_dir.to_str().unwrap_or("."));
        let project_dir = {
            let p = PathBuf::from(&project_dir_setting);
            if p.is_dir() { p } else { base_dir.clone() }
        };

        let data_dir = base_dir.join("data");
        let photos_dir = data_dir.join("photos");

        let tasks_file = match std::env::var("TASKS_FILE") {
            Ok(f) => PathBuf::from(f),
            Err(_) => data_dir.join("tasks.json"),
        };
        let ideas_file = match std::env::var("IDEAS_FILE") {
            Ok(f) => PathBuf::from(f),
            Err(_) => data_dir.join("ideas.json"),
        };

        let pipeline_file_str = env_str(
            "PIPELINE_FILE",
            base_dir.join("pipeline.json").to_str().unwrap_or("pipeline.json"),
        );

        Self {
            agent_version: "2026.03.15-rs-v1".to_string(),
            project_dir,
            data_dir: data_dir.clone(),
            photos_dir,
            tasks_file,
            ideas_file,
            state_file: base_dir.join("agent-state.json"),
            log_file: base_dir.join("agent.log"),
            web_dir: base_dir.join("web"),
            research_dir: base_dir.join("research-output"),
            security_review_dir: base_dir.join("security-reviews"),
            pipeline_output_dir: base_dir.join("pipeline-output"),
            skills_output_dir: base_dir.join("skill-output"),
            providers_file: base_dir.join("providers.json"),
            skills_file: base_dir.join("skills.json"),
            pipeline_file: PathBuf::from(pipeline_file_str),

            poll_interval: env_int("AGENT_POLL_INTERVAL", 3600) as u64,
            github_token: env_str("GITHUB_TOKEN", ""),
            trigger_port: env_int("AGENT_TRIGGER_PORT", 8099) as u16,
            github_models_url: env_str(
                "GITHUB_MODELS_URL",
                "https://models.inference.ai.azure.com/chat/completions",
            ),
            github_models_model: env_str("GITHUB_MODELS_MODEL", "gpt-4o"),
            max_api_calls_per_day: env_int("AGENT_MAX_CALLS", 10),
            auto_implement_default: env_bool("AGENT_AUTO_IMPLEMENT_DEFAULT", true),

            api_key: env_str("API_KEY", ""),
            cors_origin: env_str("CORS_ORIGIN", ""),
            max_request_body: 2 * 1024 * 1024,

            pipeline_manager_timeout: env_int("PIPELINE_MANAGER_TIMEOUT", 300) as u64,
            pipeline_max_revise: env_int("PIPELINE_MAX_REVISE", 1) as usize,
            pipeline_wallclock_timeout: env_int("PIPELINE_WALLCLOCK_TIMEOUT", 3600) as u64,

            restart_service_map: env_str("RESTART_SERVICE_MAP", ""),

            anthropic_api_key: env_str("ANTHROPIC_API_KEY", ""),
            pipeline_pm_url: env_str(
                "PIPELINE_PM_URL",
                "http://localhost:11434/v1/chat/completions",
            ),
            pipeline_pm_key: env_str("PIPELINE_PM_KEY", ""),

            agent_dir: base_dir,
        }
    }

    /// Ensure all runtime directories exist.
    pub fn ensure_dirs(&self) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.data_dir)?;
        std::fs::create_dir_all(&self.photos_dir)?;
        std::fs::create_dir_all(&self.research_dir)?;
        std::fs::create_dir_all(&self.security_review_dir)?;
        std::fs::create_dir_all(&self.pipeline_output_dir)?;
        std::fs::create_dir_all(&self.skills_output_dir)?;
        Ok(())
    }

    pub fn allowed_provider_binaries() -> &'static [&'static str] {
        &["claude", "gh", "copilot", "aider", "codex", "gemini", "q"]
    }

    pub fn allowed_photo_extensions() -> &'static [&'static str] {
        &[".jpg", ".jpeg", ".png", ".gif", ".webp"]
    }
}
