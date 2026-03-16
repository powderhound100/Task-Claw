use std::time::Duration;
use once_cell::sync::Lazy;
use regex::Regex;
use tracing::{info, warn};

use crate::config::AppConfig;
use crate::prompts::get_prompt;
use crate::types::{OverseerResult, PmConfig};

/// Low-level PM API call with retry on 429/5xx.
pub async fn pm_api_call(
    system_msg: &str,
    user_msg: &str,
    pm_cfg: &PmConfig,
    config: &AppConfig,
    http_client: &reqwest::Client,
) -> Result<String, String> {
    let backend = &pm_cfg.backend;
    let model = &pm_cfg.model;
    let max_tokens = pm_cfg.max_tokens;
    let temperature = pm_cfg.temperature;
    let pm_timeout = config.pipeline_manager_timeout;

    let retries = 3;
    let backoff = 2.0f64;
    let mut last_err = String::new();

    for attempt in 0..=retries {
        let result = match backend.as_str() {
            "github_models" => {
                if config.github_token.is_empty() {
                    return Err("GITHUB_TOKEN not set".into());
                }
                let body = serde_json::json!({
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg}
                    ]
                });
                let resp = http_client
                    .post(&config.github_models_url)
                    .header("Authorization", format!("Bearer {}", config.github_token))
                    .header("Content-Type", "application/json")
                    .timeout(Duration::from_secs(pm_timeout))
                    .json(&body)
                    .send()
                    .await;
                extract_openai_response(resp).await
            }
            "anthropic" => {
                if config.anthropic_api_key.is_empty() {
                    return Err("ANTHROPIC_API_KEY not set".into());
                }
                let body = serde_json::json!({
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system_msg,
                    "messages": [{"role": "user", "content": user_msg}]
                });
                let resp = http_client
                    .post("https://api.anthropic.com/v1/messages")
                    .header("x-api-key", &config.anthropic_api_key)
                    .header("anthropic-version", "2023-06-01")
                    .header("Content-Type", "application/json")
                    .timeout(Duration::from_secs(pm_timeout))
                    .json(&body)
                    .send()
                    .await;
                extract_anthropic_response(resp).await
            }
            "openai_compatible" => {
                let key = if !config.pipeline_pm_key.is_empty() {
                    &config.pipeline_pm_key
                } else {
                    &std::env::var("OPENAI_API_KEY").unwrap_or_default()
                };
                let body = serde_json::json!({
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg}
                    ]
                });
                let resp = http_client
                    .post(&config.pipeline_pm_url)
                    .header("Authorization", format!("Bearer {}", key))
                    .header("Content-Type", "application/json")
                    .timeout(Duration::from_secs(pm_timeout))
                    .json(&body)
                    .send()
                    .await;
                extract_openai_response(resp).await
            }
            other => return Err(format!("Unknown PM backend: {}", other)),
        };

        match result {
            Ok(text) => return Ok(text),
            Err(e) => {
                let is_retryable = e.contains("429")
                    || e.contains("500")
                    || e.contains("502")
                    || e.contains("503")
                    || e.contains("504")
                    || e.contains("timeout");

                if is_retryable && attempt < retries {
                    let wait = backoff * (2.0f64).powi(attempt as i32)
                        + rand_float();
                    warn!(
                        "PM API error — retrying in {:.1}s (attempt {}/{}): {}",
                        wait, attempt + 1, retries, e
                    );
                    tokio::time::sleep(Duration::from_secs_f64(wait)).await;
                    last_err = e;
                    continue;
                }
                return Err(e);
            }
        }
    }

    Err(last_err)
}

fn rand_float() -> f64 {
    use std::time::SystemTime;
    let nanos = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    (nanos % 1000) as f64 / 1000.0
}

async fn extract_openai_response(
    resp: Result<reqwest::Response, reqwest::Error>,
) -> Result<String, String> {
    let resp = resp.map_err(|e| e.to_string())?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("HTTP {}: {}", status.as_u16(), body));
    }
    let json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    json["choices"][0]["message"]["content"]
        .as_str()
        .map(|s| s.to_string())
        .ok_or_else(|| "No content in response".into())
}

async fn extract_anthropic_response(
    resp: Result<reqwest::Response, reqwest::Error>,
) -> Result<String, String> {
    let resp = resp.map_err(|e| e.to_string())?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("HTTP {}: {}", status.as_u16(), body));
    }
    let json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    json["content"][0]["text"]
        .as_str()
        .map(|s| s.to_string())
        .ok_or_else(|| "No content in response".into())
}

/// Extract a section's content between `## SectionName` and the next `## ` or end of string.
fn extract_section(text: &str, section_name: &str) -> Option<String> {
    let header = format!("## {}", section_name);
    let lower_text = text.to_lowercase();
    let lower_header = header.to_lowercase();
    let start = lower_text.find(&lower_header)?;
    let after_header = start + header.len();
    // Skip to end of the header line
    let content_start = text[after_header..].find('\n').map(|i| after_header + i + 1)?;
    // Find the next ## header or end of string
    let rest = &text[content_start..];
    let content_end = rest.find("\n## ")
        .map(|i| content_start + i)
        .unwrap_or(text.len());
    let content = text[content_start..content_end].trim().to_string();
    if content.is_empty() { None } else { Some(content) }
}

/// Extract a section with fuzzy header matching (e.g. "Handoff to next stage" matches "Handoff").
fn extract_section_fuzzy(text: &str, keyword: &str) -> Option<String> {
    // First try exact match
    if let Some(result) = extract_section(text, keyword) {
        return Some(result);
    }
    // Fuzzy: find any ## line containing the keyword
    let lower_keyword = keyword.to_lowercase();
    let lines: Vec<&str> = text.lines().collect();
    for (i, line) in lines.iter().enumerate() {
        if line.starts_with("## ") && line.to_lowercase().contains(&lower_keyword) {
            let content_start_line = i + 1;
            let mut content_end_line = lines.len();
            for j in content_start_line..lines.len() {
                if lines[j].starts_with("## ") {
                    content_end_line = j;
                    break;
                }
            }
            let content = lines[content_start_line..content_end_line]
                .join("\n")
                .trim()
                .to_string();
            if !content.is_empty() {
                return Some(content);
            }
        }
    }
    None
}

/// Parse the PM overseer's structured response into an OverseerResult.
pub fn parse_overseer_response(text: &str) -> OverseerResult {
    static VERDICT_RE: Lazy<Regex> =
        Lazy::new(|| Regex::new(r"(?i)##\s*Verdict\s*\n\s*(APPROVE|REVISE)").unwrap());

    let mut result = OverseerResult {
        verdict: "approve".into(),
        synthesis: String::new(),
        handoff: String::new(),
        issues: vec![],
        full_response: text.to_string(),
        pm_succeeded: false,
        team_outputs: None,
        cross_reviews: None,
        comparison_summary: None,
    };

    // Extract verdict
    if let Some(caps) = VERDICT_RE.captures(text) {
        result.verdict = caps[1].trim().to_lowercase();
    } else {
        let lower = text.to_lowercase();
        let approval_signals = [
            "approved", "looks good", "meets requirements",
            "lgtm", "well done", "production-ready",
        ];
        if approval_signals.iter().any(|sig| lower.contains(sig)) {
            warn!("PM response missing ## Verdict — inferred APPROVE from text signals");
            result.verdict = "approve".into();
        } else {
            warn!("PM response missing ## Verdict and no approval signals — defaulting to REVISE");
            result.verdict = "revise".into();
            result.issues.push("PM response was unstructured — flagged for review".into());
        }
    }

    // Extract sections between ## headers
    let issues_text = extract_section(text, "Issues");
    if let Some(issues_text) = issues_text {
        if issues_text.to_lowercase() != "none" {
            result.issues.extend(
                issues_text
                    .lines()
                    .filter(|l| {
                        let trimmed = l.trim();
                        !trimmed.is_empty() && trimmed != "-" && trimmed != "*" && trimmed != "•"
                    })
                    .map(|l| {
                        l.trim()
                            .trim_start_matches(|c: char| c == '-' || c == '*' || c == '•' || c == ' ')
                            .trim()
                            .to_string()
                    }),
            );
        }
    }

    if let Some(synthesis) = extract_section(text, "Synthesis") {
        result.synthesis = synthesis;
    }

    if let Some(handoff) = extract_section_fuzzy(text, "Handoff") {
        result.handoff = handoff;
    }

    // Fallbacks
    if result.synthesis.is_empty() {
        result.synthesis = text.to_string();
    }
    if result.handoff.is_empty() {
        result.handoff = result.synthesis.clone();
    }

    result
}

/// Quick check if PM backend config looks valid (no API call wasted).
pub fn pm_health_check(pm_cfg: &PmConfig, config: &AppConfig) -> bool {
    if pm_cfg.backend == "github_models" && config.github_token.is_empty() {
        warn!("PM health check: no GITHUB_TOKEN set");
        return false;
    }
    true
}

/// Have the PM rewrite the raw prompt for clarity.
pub async fn rewrite_prompt(
    raw_prompt: &str,
    pm_cfg: &PmConfig,
    config: &AppConfig,
    http_client: &reqwest::Client,
) -> String {
    let system_msg = get_prompt("pm_system", Some("rewriter"), "");
    let rewrite_fmt = get_prompt("rewrite_format", None, "");
    let user_msg = rewrite_fmt.replace("{prompt}", raw_prompt);

    info!("PM [rewrite]: clarifying prompt ({} chars)…", raw_prompt.len());
    match pm_api_call(&system_msg, &user_msg, pm_cfg, config, http_client).await {
        Ok(result) => {
            let rewritten = result.trim().to_string();
            if rewritten.is_empty() {
                raw_prompt.to_string()
            } else {
                info!("Rewritten prompt ({} chars)", rewritten.len());
                rewritten
            }
        }
        Err(e) => {
            warn!("PM rewrite failed ({}) — using original prompt", e);
            raw_prompt.to_string()
        }
    }
}

/// Extract discrete, testable requirements from the prompt.
pub async fn pm_extract_requirements(
    prompt: &str,
    pm_cfg: &PmConfig,
    config: &AppConfig,
    http_client: &reqwest::Client,
) -> Vec<String> {
    static NUM_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\d+\.\s+(.+)").unwrap());

    let template = get_prompt("pm_extract_requirements", None, "");
    let user_msg = template.replace("{prompt}", prompt);
    let system_msg = get_prompt("pm_system", Some("overseer"), "");

    match pm_api_call(&system_msg, &user_msg, pm_cfg, config, http_client).await {
        Ok(result) => {
            let reqs: Vec<String> = result
                .lines()
                .filter_map(|line| {
                    NUM_RE
                        .captures(line.trim())
                        .map(|c| c[1].trim().to_string())
                })
                .collect();
            if reqs.is_empty() {
                warn!("PM returned no parseable requirements");
            } else {
                info!("PM extracted {} requirements from prompt", reqs.len());
            }
            reqs
        }
        Err(e) => {
            warn!("PM requirements extraction failed ({}) — continuing without", e);
            vec![]
        }
    }
}

/// PM acts as director BEFORE the team runs.
pub async fn pm_direct_team(
    stage_name: &str,
    original_prompt: &str,
    context: &str,
    team: &[String],
    pm_cfg: &PmConfig,
    config: &AppConfig,
    http_client: &reqwest::Client,
) -> (String, bool) {
    let system_msg = get_prompt("pm_system", Some("director"), "");
    let guidance = get_prompt("pm_stage_guidance", Some(stage_name), "");
    let team_str = team.join(", ");

    let user_msg = format!(
        "Stage: {}\nTeam members: {}\nOriginal user request: {}\n\n\
         Prior pipeline context:\n{}\n\n{}\n\n\
         Write a clear, specific task brief for the team. \
         Include: what to do, which files/components to focus on, constraints from prior stages, \
         and expected output format. Be concise and actionable. \
         Return only the task brief — no preamble.",
        stage_name, team_str, original_prompt, context, guidance
    );

    info!("PM [direct/{}]: generating task brief for team {}…", stage_name, team_str);

    match pm_api_call(&system_msg, &user_msg, pm_cfg, config, http_client).await {
        Ok(brief) => {
            info!("PM brief ({} chars)", brief.len());
            (brief, true)
        }
        Err(e) => {
            warn!("PM direction failed ({}) — building direct prompt", e);
            let direct = super::stages::build_direct_prompt(stage_name, original_prompt, context);
            (direct, false)
        }
    }
}

/// PM acts as overseer AFTER the team runs.
pub async fn pm_oversee_stage(
    stage_name: &str,
    original_prompt: &str,
    context: &str,
    team_outputs: &[(String, String)],
    pm_cfg: &PmConfig,
    config: &AppConfig,
    http_client: &reqwest::Client,
    requirements: Option<&[String]>,
) -> OverseerResult {
    // Check for garbage first
    if super::garbage::is_garbage_output(team_outputs) {
        let total_len: usize = team_outputs.iter().map(|(_, out)| out.len()).sum();
        warn!("Team output is garbage ({} chars) — REVISE", total_len);
        let fallback = team_outputs
            .iter()
            .map(|(name, output)| format!("## {}\n{}", name, output))
            .collect::<Vec<_>>()
            .join("\n\n");
        return OverseerResult {
            verdict: "revise".into(),
            synthesis: fallback.clone(),
            handoff: "Team output was garbage — agent asked questions or gave no content.".into(),
            issues: vec!["Team output is a clarification question, not actual work.".into()],
            full_response: fallback,
            pm_succeeded: false,
            team_outputs: None,
            cross_reviews: None,
            comparison_summary: None,
        };
    }

    let agent_blocks = team_outputs
        .iter()
        .map(|(name, output)| format!("--- Agent: {} ---\n{}", name, output))
        .collect::<Vec<_>>()
        .join("\n\n");

    let system_msg = get_prompt("pm_system", Some("overseer"), "");
    let criteria = get_prompt("pm_stage_criteria", Some(stage_name), "");

    // Build enhanced validation prompts
    let mut enhanced_section = String::new();
    if stage_name == "plan" {
        if let Some(reqs) = requirements {
            if !reqs.is_empty() {
                let checklist = reqs
                    .iter()
                    .map(|r| format!("- [ ] {}", r))
                    .collect::<Vec<_>>()
                    .join("\n");
                let template = get_prompt("pm_plan_checklist", None, "");
                enhanced_section = format!("\n\n{}", template.replace("{checklist}", &checklist));
            }
        }
    } else if stage_name == "code" {
        let plan_text = super::context::extract_plan_context(context);
        if !plan_text.is_empty() {
            let template = get_prompt("pm_code_traceability", None, "");
            enhanced_section = format!("\n\n{}", template.replace("{plan}", &plan_text));
        }
    }

    let user_msg = format!(
        "Stage: {}\nOriginal user request: {}\n\n\
         Prior pipeline context:\n{}\n\n\
         Agent outputs:\n\n{}\n\n{}\n\n\
         Evaluate rigorously:\n\
         1. **Requirements check**: missed requirements or gaps?\n\
         2. **Quality check**: bugs, incomplete work?\n\
         3. **Drift check**: deviated from plan?\n\
         4. **Verdict**: APPROVE only if ALL standards met. REVISE if in doubt.\
         {}\n\n\
         ## Verdict\nAPPROVE or REVISE\n\n\
         ## Issues\n[problems, or 'None']\n\n\
         ## Synthesis\n[best combined output]\n\n\
         ## Handoff to next stage\n[context for next team]",
        stage_name, original_prompt, context, agent_blocks, criteria, enhanced_section
    );

    for (name, output) in team_outputs {
        info!("Agent output [{}]: {} chars", name, output.len());
    }
    info!(
        "PM [oversee/{}]: evaluating {} agent output(s)…",
        stage_name,
        team_outputs.len()
    );

    match pm_api_call(&system_msg, &user_msg, pm_cfg, config, http_client).await {
        Ok(result) => {
            let mut parsed = parse_overseer_response(&result);
            parsed.pm_succeeded = true;
            info!(
                "PM verdict: {} | issues: {}",
                parsed.verdict.to_uppercase(),
                parsed.issues.len()
            );
            parsed
        }
        Err(e) => {
            warn!("PM oversight failed ({}) — auto-approving (output passed garbage check)", e);
            let fallback = team_outputs
                .iter()
                .map(|(name, output)| format!("## {}\n{}", name, output))
                .collect::<Vec<_>>()
                .join("\n\n");
            OverseerResult {
                verdict: "approve".into(),
                synthesis: fallback.clone(),
                handoff: fallback.clone(),
                issues: vec![],
                full_response: fallback,
                pm_succeeded: false,
                team_outputs: None,
                cross_reviews: None,
                comparison_summary: None,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_overseer_approve() {
        let text = "## Verdict\nAPPROVE\n\n## Issues\nNone\n\n## Synthesis\nGood work\n\n## Handoff to next stage\nProceed";
        let r = parse_overseer_response(text);
        assert_eq!(r.verdict, "approve");
        assert!(r.issues.is_empty());
        assert_eq!(r.synthesis, "Good work");
        assert_eq!(r.handoff, "Proceed");
    }

    #[test]
    fn test_parse_overseer_revise() {
        let text = "## Verdict\nREVISE\n\n## Issues\n- Missing error handling\n- No tests\n\n## Synthesis\nIncomplete\n\n## Handoff\nFix issues";
        let r = parse_overseer_response(text);
        assert_eq!(r.verdict, "revise");
        assert_eq!(r.issues.len(), 2);
        assert!(r.issues[0].contains("error handling"));
    }

    #[test]
    fn test_parse_overseer_no_verdict_approve_signal() {
        let text = "The code looks good and meets requirements. LGTM!";
        let r = parse_overseer_response(text);
        assert_eq!(r.verdict, "approve");
    }

    #[test]
    fn test_parse_overseer_no_verdict_default_revise() {
        let text = "I have some concerns about the implementation.";
        let r = parse_overseer_response(text);
        assert_eq!(r.verdict, "revise");
    }

    #[test]
    fn test_parse_overseer_case_insensitive() {
        let text = "## Verdict\napprove\n\n## Issues\nnone\n\n## Synthesis\nOK";
        let r = parse_overseer_response(text);
        assert_eq!(r.verdict, "approve");
    }
}
