use once_cell::sync::Lazy;
use regex::Regex;

use crate::pipeline::garbage;
use crate::prompts::{get_prompt, warn_cli_prompt_size};

/// Build a clean prompt for a CLI agent.
pub fn build_direct_prompt(stage: &str, original_prompt: &str, context: &str) -> String {
    match stage {
        "plan" => {
            let tmpl = get_prompt("cli_prompts", Some("plan"), "");
            let result = tmpl.replace("{prompt}", original_prompt);
            warn_cli_prompt_size(stage, &result, original_prompt.len());
            result
        }
        "code" => {
            let mut prompt = original_prompt.to_string();
            if !context.is_empty() {
                static HANDOFF_RE: Lazy<Regex> =
                    Lazy::new(|| Regex::new(r"## [A-Z]+ HANDOFF\n(?:## \w+\n)?").unwrap());

                let clean = HANDOFF_RE.replace_all(context, "").to_string();
                let clean: String = clean
                    .lines()
                    .filter(|l| {
                        let lower = l.to_lowercase();
                        !garbage::GARBAGE_LINE_PATTERNS
                            .iter()
                            .any(|p| lower.contains(p))
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                let clean = clean.trim();
                if !clean.is_empty() && clean.len() > 50 {
                    let tail = if clean.len() > 3000 {
                        &clean[clean.len() - 3000..]
                    } else {
                        clean
                    };
                    prompt.push_str(&format!("\n\nPlan:\n{}", tail));
                }
            }
            let suffix = get_prompt("cli_prompts", Some("code_suffix"), "");
            let result = format!("{}{}", prompt, suffix);
            warn_cli_prompt_size(stage, &result, prompt.len());
            result
        }
        "simplify" | "test" | "review" => {
            let tmpl = get_prompt("cli_prompts", Some(stage), "");
            let result = tmpl.replace("{prompt}", original_prompt);
            warn_cli_prompt_size(stage, &result, original_prompt.len());
            result
        }
        _ => original_prompt.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_direct_prompt_plan() {
        let result = build_direct_prompt("plan", "add logging", "");
        assert!(result.contains("add logging"));
        assert!(result.contains("implementation plan"));
    }

    #[test]
    fn test_build_direct_prompt_code() {
        let result = build_direct_prompt("code", "fix bug", "");
        assert!(result.contains("fix bug"));
        assert!(result.contains("conventions"));
    }

    #[test]
    fn test_build_direct_prompt_code_with_context() {
        let context = "Step 1: Modify foo.rs\nStep 2: Update bar.rs\n".repeat(5);
        let result = build_direct_prompt("code", "fix bug", &context);
        assert!(result.contains("Plan:"));
    }

    #[test]
    fn test_build_direct_prompt_test() {
        let result = build_direct_prompt("test", "test changes", "");
        assert!(result.contains("git diff"));
    }
}
