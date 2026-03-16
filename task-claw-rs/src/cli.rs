use std::path::{Path, PathBuf};
use std::collections::HashMap;
use tokio::process::Command;
use tracing::{info, warn, error};

use crate::provider::get_timeout;
use crate::types::Provider;

const PROMPT_FILE_THRESHOLD: usize = 6000;

/// Build the full CLI command list from a provider config.
pub fn build_cli_command(
    provider: &Provider,
    phase: &str,
    prompt: &str,
    prompt_file: Option<&Path>,
) -> Vec<String> {
    let binary = &provider.binary;
    // Resolve .cmd/.bat/.exe on Windows
    let resolved = which::which(binary).ok();
    let binary = resolved
        .as_ref()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|| binary.clone());

    let sub = &provider.subcommand;

    let arg_key = match phase {
        "plan" => "plan",
        "implement" => "implement",
        "simplify" => "simplify",
        "security" => "security",
        "test" => "test",
        "review" => "review",
        _ => "implement",
    };

    let args = get_phase_args(provider, arg_key);

    // Auto-switch: when prompt is long and a file exists, replace inline "-p {prompt}"
    let use_file = prompt_file.is_some() && prompt.len() > PROMPT_FILE_THRESHOLD;

    let final_args = if use_file {
        let prompt_file = prompt_file.unwrap();
        let bin_name = Path::new(&binary)
            .file_stem()
            .map(|s| s.to_string_lossy().to_lowercase())
            .unwrap_or_default();

        let mut new_args = Vec::new();
        let mut i = 0;
        let mut swapped = false;

        while i < args.len() {
            if (args[i] == "-p" || args[i] == "--prompt" || args[i] == "--message")
                && i + 1 < args.len()
                && args[i + 1].contains("{prompt}")
            {
                if bin_name == "claude" {
                    new_args.push("--prompt-file".to_string());
                    new_args.push(prompt_file.to_string_lossy().to_string());
                } else if bin_name == "aider" {
                    new_args.push("--message-file".to_string());
                    new_args.push(prompt_file.to_string_lossy().to_string());
                } else {
                    new_args.push(args[i].clone());
                    new_args.push(prompt.to_string());
                    i += 2;
                    continue;
                }
                swapped = true;
                i += 2;
                continue;
            }
            let pf_str = prompt_file.to_string_lossy().to_string();
            new_args.push(
                args[i]
                    .replace("{prompt_file}", &pf_str)
                    .replace("{prompt}", prompt),
            );
            i += 1;
        }

        if swapped {
            info!(
                "Auto-switched to prompt file ({} chars > {} threshold): {}",
                prompt.len(),
                PROMPT_FILE_THRESHOLD,
                prompt_file.display()
            );
            new_args
        } else {
            let pf_str = prompt_file.to_string_lossy().to_string();
            args.iter()
                .map(|a| a.replace("{prompt_file}", &pf_str).replace("{prompt}", prompt))
                .collect()
        }
    } else {
        let pf_str = prompt_file
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_default();
        args.iter()
            .map(|a| a.replace("{prompt_file}", &pf_str).replace("{prompt}", prompt))
            .collect()
    };

    let mut cmd = vec![binary];
    cmd.extend(sub.iter().cloned());
    cmd.extend(final_args);
    cmd
}

fn get_phase_args(provider: &Provider, phase: &str) -> Vec<String> {
    match phase {
        "plan" => provider.plan_args.clone(),
        "implement" => provider.implement_args.clone(),
        "simplify" => provider
            .simplify_args
            .clone()
            .unwrap_or_else(|| provider.implement_args.clone()),
        "security" => provider.security_args.clone(),
        "test" => {
            if provider.test_args.is_empty() {
                provider.plan_args.clone()
            } else {
                provider.test_args.clone()
            }
        }
        "review" => {
            if provider.review_args.is_empty() {
                provider.plan_args.clone()
            } else {
                provider.review_args.clone()
            }
        }
        _ => provider.implement_args.clone(),
    }
}

/// Return a copy of env without vars that block nested CLI sessions.
fn clean_env() -> HashMap<String, String> {
    let mut env: HashMap<String, String> = std::env::vars().collect();
    env.remove("CLAUDECODE");
    env.remove("CLAUDE_CODE_SESSION");
    env
}

/// Parse Claude Code --output-format json output.
/// Returns (text_output, subagent_count, tool_counts).
pub fn parse_claude_json_output(raw: &str) -> (String, usize, HashMap<String, usize>) {
    let messages: Vec<serde_json::Value> = match serde_json::from_str(raw) {
        Ok(v) => v,
        Err(_) => return (raw.to_string(), 0, HashMap::new()),
    };

    let mut text_parts = Vec::new();
    let mut subagent_count = 0;
    let mut tool_counts: HashMap<String, usize> = HashMap::new();

    for msg in &messages {
        let role = msg
            .get("role")
            .or_else(|| msg.get("type"))
            .and_then(|v| v.as_str())
            .unwrap_or("");

        if role != "assistant" {
            continue;
        }

        let content = match msg.get("content") {
            Some(serde_json::Value::String(s)) => {
                text_parts.push(s.clone());
                continue;
            }
            Some(serde_json::Value::Array(arr)) => arr,
            _ => continue,
        };

        for block in content {
            let block_type = block.get("type").and_then(|v| v.as_str()).unwrap_or("");
            match block_type {
                "text" => {
                    if let Some(text) = block.get("text").and_then(|v| v.as_str()) {
                        text_parts.push(text.to_string());
                    }
                }
                "tool_use" => {
                    let tool_name = block
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown");
                    *tool_counts.entry(tool_name.to_string()).or_insert(0) += 1;
                    if tool_name == "Agent" {
                        subagent_count += 1;
                    }
                }
                _ => {}
            }
        }
    }

    let text_output = text_parts.join("\n").trim().to_string();
    if text_output.is_empty() {
        return (raw.to_string(), subagent_count, tool_counts);
    }
    (text_output, subagent_count, tool_counts)
}

/// Run a CLI provider command. Returns (success, output_text).
pub async fn run_cli_command(
    provider: &Provider,
    phase: &str,
    prompt: &str,
    cwd: Option<&Path>,
    prompt_file: Option<&Path>,
    project_dir: &Path,
) -> (bool, String) {
    let cmd = build_cli_command(provider, phase, prompt, prompt_file);
    let timeout = get_timeout(provider, phase);

    let work_dir = cwd.unwrap_or(project_dir);
    let work_dir = if work_dir.is_dir() {
        work_dir.to_path_buf()
    } else {
        warn!("cwd '{}' does not exist — falling back", work_dir.display());
        project_dir.to_path_buf()
    };

    let timeout_label = timeout
        .map(|t| format!("{}s", t))
        .unwrap_or_else(|| "no timeout".to_string());

    let cmd_display: Vec<String> = cmd
        .iter()
        .map(|c| {
            if c.len() > 100 {
                format!("{}…", &c[..100])
            } else {
                c.clone()
            }
        })
        .collect();
    info!(
        "CLI [{}/{}]: {} ({})",
        provider.name.as_str(),
        phase,
        cmd_display.join(" "),
        timeout_label
    );

    let env = clean_env();

    let mut command = Command::new(&cmd[0]);
    command.args(&cmd[1..]).current_dir(&work_dir).envs(&env);

    // Kill on drop ensures cleanup on timeout
    command.kill_on_drop(true);

    let result = if let Some(timeout_secs) = timeout {
        let duration = std::time::Duration::from_secs(timeout_secs);
        match tokio::time::timeout(duration, command.output()).await {
            Ok(Ok(output)) => Ok(output),
            Ok(Err(e)) => Err(e),
            Err(_) => {
                error!("Timed out after {}", timeout_label);
                return (false, format!("Timed out after {}", timeout_label));
            }
        }
    } else {
        command.output().await
    };

    match result {
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();

            info!("Exit code: {} | output: {} chars",
                output.status.code().unwrap_or(-1),
                stdout.len()
            );

            if !stdout.is_empty() {
                let snippet = if stdout.len() > 500 {
                    format!("{}…[truncated]", &stdout[..500])
                } else {
                    stdout.clone()
                };
                info!("Output preview:\n{}", snippet);
            }

            if !stderr.is_empty() {
                let tail = if stderr.len() > 500 {
                    &stderr[stderr.len() - 500..]
                } else {
                    &stderr
                };
                warn!("Stderr: {}", tail);
            }

            let mut output_text = if !stdout.is_empty() {
                stdout
            } else {
                stderr
            };

            // Parse Claude JSON output for subagent/tool tracking
            let is_claude = provider.binary == "claude";
            if is_claude && !output_text.is_empty() && output_text.starts_with('[') {
                let (text, subagent_count, tool_counts) =
                    parse_claude_json_output(&output_text);
                if text != output_text {
                    output_text = text;
                    info!(
                        "Parsed Claude JSON: {} subagents, tools: {:?}",
                        subagent_count, tool_counts
                    );
                }
            }

            (output.status.success(), output_text)
        }
        Err(e) => {
            error!("CLI error: {}", e);
            (false, e.to_string())
        }
    }
}

/// Write the prompt to a temp file and return its path.
pub fn write_prompt_file(
    prompt: &str,
    task_id: &str,
    stage: &str,
    phase: &str,
    pipeline_output_dir: &Path,
) -> PathBuf {
    let task_dir = pipeline_output_dir.join(if task_id.is_empty() {
        "scratch"
    } else {
        task_id
    });
    std::fs::create_dir_all(&task_dir).ok();
    let prompt_file = task_dir.join(format!(".prompt-{}-{}.md", stage, phase));
    std::fs::write(&prompt_file, prompt).ok();
    prompt_file
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Provider;

    fn test_provider() -> Provider {
        Provider {
            name: "Claude Code".into(),
            binary: "claude".into(),
            subcommand: vec![],
            plan_args: vec!["-p".into(), "{prompt}".into(), "--output-format".into(), "json".into()],
            implement_args: vec!["-p".into(), "{prompt}".into(), "--dangerously-skip-permissions".into()],
            simplify_args: None,
            security_args: vec!["-p".into(), "{prompt}".into()],
            test_args: vec!["-p".into(), "{prompt}".into()],
            review_args: vec!["-p".into(), "{prompt}".into()],
            plan_timeout: 900,
            implement_timeout: 600,
            security_timeout: 300,
            test_timeout: 300,
            review_timeout: 300,
            env: Default::default(),
            notes: String::new(),
        }
    }

    #[test]
    fn test_build_cli_command_basic() {
        let provider = test_provider();
        let cmd = build_cli_command(&provider, "plan", "hello world", None);
        // Should contain the prompt substituted
        assert!(cmd.iter().any(|a| a == "hello world"));
        assert!(cmd.iter().any(|a| a == "--output-format"));
    }

    #[test]
    fn test_parse_claude_json_output_plain() {
        let (text, subs, tools) = parse_claude_json_output("just plain text");
        assert_eq!(text, "just plain text");
        assert_eq!(subs, 0);
        assert!(tools.is_empty());
    }

    #[test]
    fn test_parse_claude_json_output_structured() {
        let json = r#"[
            {"role": "assistant", "content": [
                {"type": "text", "text": "Hello world"},
                {"type": "tool_use", "name": "Read"},
                {"type": "tool_use", "name": "Agent"}
            ]}
        ]"#;
        let (text, subs, tools) = parse_claude_json_output(json);
        assert_eq!(text, "Hello world");
        assert_eq!(subs, 1);
        assert_eq!(tools.get("Read"), Some(&1));
        assert_eq!(tools.get("Agent"), Some(&1));
    }
}
