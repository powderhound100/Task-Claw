
use crate::config::AppConfig;
use crate::error::{TaskClawError, Result};
use crate::state::load_json_file;
use crate::types::{Provider, ProvidersConfig};

/// Load provider definitions from providers.json.
pub fn load_providers(config: &AppConfig) -> ProvidersConfig {
    load_json_file::<ProvidersConfig>(&config.providers_file, "providers.json")
        .unwrap_or_default()
}

/// Get a provider config by name, falling back to default.
pub fn get_provider(config: &AppConfig, name: Option<&str>) -> Result<Provider> {
    let cfg = load_providers(config);
    let providers = &cfg.providers;
    let name = match name {
        Some(n) if !n.trim().is_empty() => n.trim().to_lowercase(),
        _ => cfg.default_provider.clone(),
    };

    if let Some(p) = providers.get(&name) {
        return Ok(p.clone());
    }

    // Fuzzy match
    for (key, val) in providers {
        if name.contains(key.as_str()) || key.contains(name.as_str()) {
            return Ok(val.clone());
        }
    }

    Err(TaskClawError::Provider(format!(
        "Unknown CLI provider: {}. Available: {}",
        name,
        providers.keys().cloned().collect::<Vec<_>>().join(", ")
    )))
}

/// Resolve provider for a given phase.
/// Priority: task-level override → phase env var → global env var → providers.json default.
pub fn get_provider_for_phase(
    config: &AppConfig,
    phase: &str,
    task_override: Option<&str>,
) -> Result<Provider> {
    if let Some(ovr) = task_override {
        if !ovr.is_empty() {
            return get_provider(config, Some(ovr));
        }
    }

    let env_key = match phase {
        "plan" => "CLI_PLAN_PROVIDER",
        "implement" => "CLI_IMPLEMENT_PROVIDER",
        "simplify" => "CLI_IMPLEMENT_PROVIDER",
        "security" => "CLI_SECURITY_PROVIDER",
        "test" => "CLI_TEST_PROVIDER",
        "review" => "CLI_REVIEW_PROVIDER",
        "rewrite" => "CLI_PLAN_PROVIDER",
        _ => "CLI_PROVIDER",
    };

    let provider_name = std::env::var(env_key)
        .ok()
        .or_else(|| std::env::var("CLI_PROVIDER").ok());

    get_provider(config, provider_name.as_deref())
}

/// Get timeout for a phase, checking env overrides then provider config.
/// Returns None when the resolved value is 0.
pub fn get_timeout(provider: &Provider, phase: &str) -> Option<u64> {
    let env_keys: &[&str] = match phase {
        "plan" => &["PIPELINE_PLAN_TIMEOUT", "COPILOT_PLAN_TIMEOUT"],
        "implement" => &["PIPELINE_CODE_TIMEOUT", "COPILOT_TIMEOUT"],
        "simplify" => &["PIPELINE_SIMPLIFY_TIMEOUT", "COPILOT_TIMEOUT"],
        "security" => &["PIPELINE_REVIEW_TIMEOUT", "COPILOT_SECURITY_TIMEOUT"],
        "test" => &["PIPELINE_TEST_TIMEOUT", "COPILOT_SECURITY_TIMEOUT"],
        "review" => &["PIPELINE_REVIEW_TIMEOUT", "COPILOT_SECURITY_TIMEOUT"],
        _ => &["COPILOT_TIMEOUT"],
    };

    for key in env_keys {
        if let Ok(val) = std::env::var(key) {
            if let Ok(v) = val.parse::<u64>() {
                return if v == 0 { None } else { Some(v) };
            }
        }
    }

    let v = match phase {
        "plan" => provider.plan_timeout,
        "implement" => provider.implement_timeout,
        "security" => provider.security_timeout,
        "test" => provider.test_timeout,
        "review" => provider.review_timeout,
        _ => provider.implement_timeout,
    };

    if v == 0 { None } else { Some(v) }
}

/// Return dict of provider_key → provider_name for UI/status.
pub fn list_available_providers(config: &AppConfig) -> std::collections::HashMap<String, String> {
    let cfg = load_providers(config);
    cfg.providers
        .iter()
        .map(|(k, v)| (k.clone(), if v.name.is_empty() { k.clone() } else { v.name.clone() }))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_get_timeout_defaults() {
        let provider = Provider {
            name: "test".into(),
            binary: "test".into(),
            subcommand: vec![],
            plan_args: vec![],
            implement_args: vec![],
            simplify_args: None,
            security_args: vec![],
            test_args: vec![],
            review_args: vec![],
            plan_timeout: 900,
            implement_timeout: 600,
            security_timeout: 300,
            test_timeout: 300,
            review_timeout: 300,
            env: Default::default(),
            notes: String::new(),
        };

        assert_eq!(get_timeout(&provider, "plan"), Some(900));
        assert_eq!(get_timeout(&provider, "implement"), Some(600));
        assert_eq!(get_timeout(&provider, "security"), Some(300));
    }
}
