use once_cell::sync::Lazy;

static GARBAGE_STRONG: Lazy<Vec<&'static str>> = Lazy::new(|| {
    vec![
        "what would you like", "could you share", "could you describe",
        "could you provide", "could you grant", "could you tell",
        "could you give", "could you let", "could you help",
        "could you approve", "could you confirm", "could you try",
        "please describe", "please provide", "please share",
        "please approve", "i don't see", "i don't have",
        "what bug are you", "message might be incomplete",
        "message got cut off", "message may have been cut off",
        "message was cut off", "message appears to be cut off",
        "got cut off", "was cut off", "appears incomplete",
        "no content after it", "but no content", "but no details",
        "plan handoff", "no plan content",
        "what do you want", "how can i help", "can you clarify",
        "what specific", "what exactly", "can you tell me",
        "need more context", "i'd be happy to help", "i'll need to know",
        "grant permission", "i need access", "need write permission",
        "need permission", "approve the edit", "can you confirm",
        "i need write", "write permission",
    ]
});

static GARBAGE_WEAK: Lazy<Vec<&'static str>> = Lazy::new(|| {
    vec![
        "more information", "more details",
        "permission was denied", "permission denied",
    ]
});

/// Check if team output is garbage (questions, too short, permission asks, etc.).
/// Uses tiered scoring: strong signals = 2pts, weak signals = 1pt.
/// Weak signals only count if output is short (<500 chars) or has question marks.
/// Threshold = 2 points.
pub fn is_garbage_output(team_outputs: &[(String, String)]) -> bool {
    let total_len: usize = team_outputs.iter().map(|(_, out)| out.len()).sum();

    // Trivially short — almost always a permission denial or empty run
    if total_len < 100 {
        return true;
    }

    let all_output: String = team_outputs
        .iter()
        .map(|(_, out)| out.to_lowercase())
        .collect::<Vec<_>>()
        .join(" ");

    let mut score: i32 = 0;

    for pat in GARBAGE_STRONG.iter() {
        if all_output.contains(pat) {
            score += 2;
        }
    }
    if score >= 2 {
        return true;
    }

    // Weak signals only count if output is short or contains question marks
    let is_short_or_questioning = total_len < 500 || all_output.contains('?');
    if is_short_or_questioning {
        for pat in GARBAGE_WEAK.iter() {
            if all_output.contains(pat) {
                score += 1;
            }
        }
    }

    score >= 2
}

/// Combined patterns for line-level filtering (public for use in stages.rs).
pub static GARBAGE_LINE_PATTERNS: Lazy<Vec<&'static str>> = Lazy::new(|| {
    let mut all: Vec<&str> = GARBAGE_STRONG.clone();
    all.extend(GARBAGE_WEAK.iter());
    all
});

/// Strip garbage lines from stage output before appending to context.
pub fn clean_stage_output(output: &str) -> String {
    let patterns = &*GARBAGE_LINE_PATTERNS;
    output
        .lines()
        .filter(|line| {
            let lower = line.to_lowercase();
            !patterns.iter().any(|p| lower.contains(p))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_garbage_short_output() {
        let outputs = vec![("agent".into(), "ok".into())];
        assert!(is_garbage_output(&outputs));
    }

    #[test]
    fn test_garbage_permission_request() {
        let outputs = vec![(
            "claude".into(),
            "I'd be happy to help! Could you share more details about what you'd like me to do?".into(),
        )];
        assert!(is_garbage_output(&outputs));
    }

    #[test]
    fn test_not_garbage_real_output() {
        let code = "fn main() {\n    println!(\"Hello, world!\");\n}\n\
                     // Added error handling for file operations\n\
                     // Updated the configuration parser to handle edge cases\n\
                     // This implementation follows the existing patterns in the codebase\n\
                     // We need to ensure backwards compatibility with the old format\n\
                     let result = process_data(&input).expect(\"Failed to process\");\n\
                     // Additional validation logic added here to catch malformed input\n\
                     // The test suite has been updated to cover these new paths";
        let outputs = vec![("claude".into(), code.into())];
        assert!(!is_garbage_output(&outputs));
    }

    #[test]
    fn test_garbage_plan_handoff() {
        let outputs = vec![(
            "claude".into(),
            "plan handoff — no plan content was provided".into(),
        )];
        assert!(is_garbage_output(&outputs));
    }

    #[test]
    fn test_garbage_weak_only_long() {
        // Weak patterns alone shouldn't trigger on long output
        let long = format!("more information about the changes that were made. {}", "x".repeat(600));
        let outputs = vec![("agent".into(), long)];
        assert!(!is_garbage_output(&outputs));
    }

    #[test]
    fn test_garbage_weak_short_with_question() {
        // Weak patterns + short + question mark → garbage
        let outputs = vec![(
            "agent".into(),
            "permission denied? more information more details".into(),
        )];
        assert!(is_garbage_output(&outputs));
    }

    #[test]
    fn test_clean_stage_output() {
        let input = "good line\ncould you share the details\nanother good line";
        let cleaned = clean_stage_output(input);
        assert_eq!(cleaned, "good line\nanother good line");
    }
}
