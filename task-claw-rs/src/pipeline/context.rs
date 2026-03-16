use once_cell::sync::Lazy;
use regex::Regex;

/// Split a string on a delimiter pattern, keeping the delimiter at the start of each piece.
fn split_keeping_delimiter(text: &str, delimiter: &str) -> Vec<String> {
    let mut sections = Vec::new();
    let mut last = 0;
    for (idx, _) in text.match_indices(delimiter) {
        if idx > last {
            sections.push(text[last..idx].to_string());
        }
        last = idx;
    }
    if last < text.len() {
        sections.push(text[last..].to_string());
    }
    if sections.is_empty() {
        sections.push(text.to_string());
    }
    sections
}

/// Cap context to max_chars by dropping oldest non-plan sections first.
pub fn cap_context(context: &str, max_chars: usize) -> String {
    if context.len() <= max_chars {
        return context.to_string();
    }

    // Split on === ... === delimiters
    let mut sections = split_keeping_delimiter(context, "=== ");

    if sections.len() <= 1 {
        // Fallback: try ## headers
        sections = split_keeping_delimiter(context, "## ");
    }

    // Never drop the Plan section
    while total_len(&sections) > max_chars && sections.len() > 1 {
        let mut dropped = false;
        for i in 0..sections.len() {
            if !sections[i].to_lowercase().contains("plan") {
                sections.remove(i);
                dropped = true;
                break;
            }
        }
        if !dropped {
            sections.remove(0); // all plan sections, drop oldest
        }
    }

    sections.join("")
}

fn total_len(sections: &[String]) -> usize {
    sections.iter().map(|s| s.len()).sum()
}

/// Extract just the plan section from pipeline context.
pub fn extract_plan_context(context: &str) -> String {
    static PLAN_RE: Lazy<Regex> = Lazy::new(|| {
        Regex::new(r"(?is)(=== Plan[^\n]*===.*?=== End plan ===)").unwrap()
    });
    if let Some(m) = PLAN_RE.captures(context) {
        return m[1].to_string();
    }

    // Fallback: try ## headers — find "## Plan" and capture until next "## " or end
    let lower = context.to_lowercase();
    if let Some(start) = lower.find("## plan") {
        // Find the next ## header after the plan header line
        let after_header = start + 7; // len("## plan")
        let rest = &context[after_header..];
        let end = rest.find("\n## ")
            .map(|i| after_header + i)
            .unwrap_or(context.len());
        return context[start..end].trim().to_string();
    }
    String::new()
}

/// Check if test stage output indicates failures that need code fixes.
pub fn test_found_failures(test_output: &str) -> bool {
    let lower = test_output.to_lowercase();

    let pass_patterns = [
        "no fail", "no error", "all pass", "tests pass", "0 fail",
        "0 errors", "no issues", "success", "everything passed",
        "all tests pass",
    ];
    if pass_patterns.iter().any(|pp| lower.contains(pp)) {
        return false;
    }

    let long_failure_patterns = [
        "exception", "traceback", "not working", "does not work", "undefined",
        "typeerror", "syntaxerror", "referenceerror", "attributeerror",
        "fix needed", "needs fix", "issue found", "issues found",
    ];
    if long_failure_patterns.iter().any(|fp| lower.contains(fp)) {
        return true;
    }

    // Short keywords with word boundary
    static SHORT_KW_RE: Lazy<Vec<Regex>> = Lazy::new(|| {
        ["fail", "error", "broken", "bug", "crash", "assert"]
            .iter()
            .map(|kw| Regex::new(&format!(r"\b{}\b", kw)).unwrap())
            .collect()
    });

    SHORT_KW_RE.iter().any(|re| re.is_match(&lower))
}

/// Extract relevant test failure lines with context. Falls back to tail truncation.
pub fn extract_test_failures(output: &str, max_chars: usize) -> String {
    static FAILURE_RE: Lazy<Regex> = Lazy::new(|| {
        Regex::new(
            r"(?i)(fail|error|traceback|assert|exception|syntaxerror|typeerror\
            |referenceerror|attributeerror|not working|does not work)",
        )
        .unwrap()
    });

    let lines: Vec<&str> = output.lines().collect();
    let mut relevant_indices: Vec<usize> = Vec::new();

    for (i, line) in lines.iter().enumerate() {
        if FAILURE_RE.is_match(line) {
            let start = i.saturating_sub(2);
            let end = (i + 3).min(lines.len());
            for j in start..end {
                if !relevant_indices.contains(&j) {
                    relevant_indices.push(j);
                }
            }
        }
    }

    if !relevant_indices.is_empty() {
        relevant_indices.sort();
        let result: String = relevant_indices
            .iter()
            .map(|&i| lines[i])
            .collect::<Vec<_>>()
            .join("\n");
        if result.len() > max_chars {
            result[..max_chars].to_string()
        } else {
            result
        }
    } else {
        // Fallback: last max_chars of output
        if output.len() > max_chars {
            output[output.len() - max_chars..].to_string()
        } else {
            output.to_string()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cap_context_short() {
        let ctx = "short context";
        assert_eq!(cap_context(ctx, 12000), ctx);
    }

    #[test]
    fn test_cap_context_preserves_plan() {
        let ctx = format!(
            "=== Old stage output ===\n{}\n=== End old ===\n\
             === Plan stage output ===\nImportant plan\n=== End plan ===\n\
             === Another stage ===\n{}\n=== End another ===",
            "x".repeat(5000),
            "y".repeat(5000)
        );
        let capped = cap_context(&ctx, 8000);
        assert!(capped.contains("Important plan"));
    }

    #[test]
    fn test_test_found_failures_pass() {
        assert!(!test_found_failures("All tests passed successfully"));
        assert!(!test_found_failures("0 failures, 10 tests passed"));
        assert!(!test_found_failures("No errors found"));
    }

    #[test]
    fn test_test_found_failures_fail() {
        assert!(test_found_failures("FAIL: test_something - assertion error"));
        assert!(test_found_failures("TypeError: undefined is not a function"));
        assert!(test_found_failures("Traceback (most recent call last):"));
    }

    #[test]
    fn test_extract_plan_context() {
        let ctx = "some stuff\n=== Plan stage output ===\nThe plan\n=== End plan ===\nmore";
        let plan = extract_plan_context(ctx);
        assert!(plan.contains("The plan"));
    }

    #[test]
    fn test_extract_test_failures() {
        let output = "line 1\nline 2\nFAIL: test_foo\nline 4\nline 5\nline 6";
        let result = extract_test_failures(output, 4000);
        assert!(result.contains("FAIL: test_foo"));
    }
}
