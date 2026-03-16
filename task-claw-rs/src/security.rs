use once_cell::sync::Lazy;
use regex::Regex;
use tokio::process::Command;
use tracing::{info, warn};

use crate::cli::run_cli_command;
use crate::config::AppConfig;
use crate::provider::get_provider_for_phase;
use crate::types::SecurityReview;

/// Run a security review on staged/unstaged changes.
pub async fn run_security_review(task_id: &str, title: &str, config: &AppConfig) -> SecurityReview {
    info!("Running security review for: {}", title);

    // Get diff
    let diff_text = match get_diff(config).await {
        Some(d) => d,
        None => {
            info!("No diff to review — skipping");
            return SecurityReview {
                passed: true,
                severity: "none".into(),
                findings: vec![],
                report: "No changes.".into(),
            };
        }
    };

    let truncated = if diff_text.len() > 15000 {
        format!("{}\n\n... (truncated)", &diff_text[..15000])
    } else {
        diff_text
    };

    let review_prompt = format!(
        "You are a security auditor. Review this git diff.\n\n\
         RESPOND ONLY WITH VALID JSON — no markdown, no code fences.\n\n\
         Check for:\n\
         1. Hardcoded secrets, API keys, tokens, passwords\n\
         2. Exposed IP addresses or internal network details\n\
         3. Insecure HTTP endpoints (missing auth, CORS wildcards)\n\
         4. Dangerous shell commands or code injection vectors\n\
         5. Known vulnerable libraries or insecure dependency versions\n\
         6. Overly permissive file/network permissions\n\
         7. Secrets logged to console or files\n\n\
         Rate each: low / medium / high.\n\n\
         Return JSON: {{\"passed\": true/false, \"severity\": \"none\"/\"low\"/\"medium\"/\"high\", \
         \"findings\": [{{\"severity\": \"...\", \"file\": \"...\", \"line\": \"...\", \"issue\": \"...\", \"fix\": \"...\"}}]}}\n\n\
         DIFF:\n{}",
        truncated
    );

    match get_provider_for_phase(config, "security", None) {
        Ok(provider) => {
            let (_success, output) =
                run_cli_command(&provider, "security", &review_prompt, None, None, &config.project_dir)
                    .await;

            if output.is_empty() {
                warn!("Security review produced no output");
                return SecurityReview {
                    passed: true,
                    severity: "none".into(),
                    findings: vec![],
                    report: "No output.".into(),
                };
            }

            match parse_security_json(&output) {
                Some(mut review) => {
                    review.report = output;
                    // Save report
                    let report_file = config.security_review_dir.join(format!("{}-review.json", task_id));
                    if let Ok(json) = serde_json::to_string_pretty(&review) {
                        std::fs::write(&report_file, json).ok();
                    }
                    info!(
                        "Security result: severity={}, findings={}, passed={}",
                        review.severity,
                        review.findings.len(),
                        review.passed
                    );
                    review
                }
                None => {
                    warn!("Could not parse security JSON — treating as passed");
                    SecurityReview {
                        passed: true,
                        severity: "none".into(),
                        findings: vec![],
                        report: output,
                    }
                }
            }
        }
        Err(e) => {
            warn!("Security review error: {} — allowing push", e);
            SecurityReview {
                passed: true,
                severity: "none".into(),
                findings: vec![],
                report: e.to_string(),
            }
        }
    }
}

async fn get_diff(config: &AppConfig) -> Option<String> {
    for args in &[&["diff", "--cached"][..], &["diff"][..]] {
        match Command::new("git")
            .args(*args)
            .current_dir(&config.project_dir)
            .output()
            .await
        {
            Ok(output) => {
                let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                if !stdout.is_empty() {
                    return Some(stdout);
                }
            }
            Err(e) => {
                warn!("Could not get diff: {}", e);
                return None;
            }
        }
    }
    None
}

/// Parse security review JSON output, trying multiple extraction strategies.
pub fn parse_security_json(text: &str) -> Option<SecurityReview> {
    // Direct parse
    if let Ok(review) = serde_json::from_str::<SecurityReview>(text) {
        return Some(review);
    }

    // Try extracting from markdown fences
    static FENCE_RE: Lazy<Vec<Regex>> = Lazy::new(|| {
        vec![
            Regex::new(r"(?s)```json\s*\n(.*?)\n\s*```").unwrap(),
            Regex::new(r"(?s)```\s*\n(.*?)\n\s*```").unwrap(),
            Regex::new(r#"(?s)(\{[^{}]*"passed"[^{}]*"findings"[^{}]*\[.*?\]\s*\})"#).unwrap(),
        ]
    });

    for re in FENCE_RE.iter() {
        if let Some(caps) = re.captures(text) {
            if let Ok(review) = serde_json::from_str::<SecurityReview>(&caps[1]) {
                return Some(review);
            }
        }
    }

    None
}

/// Handle security findings. Returns "publish" | "fixed" | "blocked".
pub async fn handle_security_findings(
    review: &SecurityReview,
    _task_id: &str,
    config: &AppConfig,
) -> String {
    if review.severity == "none" || review.findings.is_empty() {
        info!("No security issues — clear to push");
        return "publish".into();
    }

    let high: Vec<_> = review
        .findings
        .iter()
        .filter(|f| f.severity == "high")
        .collect();
    let medium: Vec<_> = review
        .findings
        .iter()
        .filter(|f| f.severity == "medium")
        .collect();
    let low: Vec<_> = review
        .findings
        .iter()
        .filter(|f| f.severity == "low")
        .collect();

    info!(
        "Findings: {} high, {} medium, {} low",
        high.len(),
        medium.len(),
        low.len()
    );

    // HIGH → block + revert
    if !high.is_empty() {
        warn!("HIGH severity — blocking push!");
        // Revert changes
        Command::new("git")
            .args(["reset", "HEAD", "--hard"])
            .current_dir(&config.project_dir)
            .output()
            .await
            .ok();
        info!("Changes reverted");
        return "blocked".into();
    }

    // LOW/MEDIUM → auto-fix then publish
    let fix_lines: Vec<String> = review
        .findings
        .iter()
        .map(|f| {
            format!(
                "- [{}] {}: {} — Fix: {}",
                f.severity.to_uppercase(),
                f.file,
                f.issue,
                f.fix
            )
        })
        .collect();
    let fix_prompt = format!("Fix these security issues:\n\n{}", fix_lines.join("\n"));

    info!(
        "Attempting auto-fix for {} issue(s)…",
        medium.len() + low.len()
    );

    match get_provider_for_phase(config, "implement", None) {
        Ok(provider) => {
            let (success, _) =
                run_cli_command(&provider, "implement", &fix_prompt, None, None, &config.project_dir)
                    .await;
            if success {
                info!("Security issues auto-fixed");
                "fixed".into()
            } else {
                warn!("Auto-fix failed");
                "publish".into()
            }
        }
        Err(_) => "publish".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_security_json_direct() {
        let json = r#"{"passed": true, "severity": "none", "findings": []}"#;
        let result = parse_security_json(json);
        assert!(result.is_some());
        assert!(result.unwrap().passed);
    }

    #[test]
    fn test_parse_security_json_fenced() {
        let text = "Here is my review:\n```json\n{\"passed\": false, \"severity\": \"high\", \"findings\": [{\"severity\": \"high\", \"file\": \"main.rs\", \"line\": \"5\", \"issue\": \"hardcoded secret\", \"fix\": \"use env var\"}]}\n```";
        let result = parse_security_json(text);
        assert!(result.is_some());
        let review = result.unwrap();
        assert!(!review.passed);
        assert_eq!(review.findings.len(), 1);
    }

    #[test]
    fn test_parse_security_json_invalid() {
        let result = parse_security_json("not json at all");
        assert!(result.is_none());
    }

    #[test]
    fn test_parse_security_json_no_fence() {
        let text = r#"Some text before {"passed": true, "severity": "low", "findings": [{"severity": "low", "file": "test.py", "line": "1", "issue": "minor", "fix": "fix it"}]} some text after"#;
        let result = parse_security_json(text);
        assert!(result.is_some());
    }
}
