use std::collections::HashMap;
use std::sync::RwLock;
use crate::types::StageStats;

/// Thread-safe pipeline stats tracker.
pub struct PipelineStatsTracker {
    stats: RwLock<HashMap<String, StageStats>>,
}

impl PipelineStatsTracker {
    pub fn new() -> Self {
        Self {
            stats: RwLock::new(HashMap::new()),
        }
    }

    pub fn reset(&self) {
        let mut stats = self.stats.write().unwrap();
        stats.clear();
    }

    /// Record a CLI invocation and any subagent/tool usage.
    #[allow(dead_code)]
    pub fn record_cli_call(
        &self,
        phase: &str,
        subagent_count: usize,
        tool_counts: Option<&HashMap<String, usize>>,
    ) {
        // Map CLI phase back to pipeline stage name
        let stage = match phase {
            "plan" => "plan",
            "implement" => "code",
            "simplify" => "simplify",
            "security" => "review",
            "test" => "test",
            "review" => "review",
            other => other,
        };

        let mut stats = self.stats.write().unwrap();
        let entry = stats.entry(stage.to_string()).or_insert_with(StageStats::default);
        entry.cli_calls += 1;
        entry.subagents += subagent_count;
        if let Some(tc) = tool_counts {
            for (tool, count) in tc {
                *entry.tool_calls.entry(tool.clone()).or_insert(0) += count;
            }
        }
    }

    pub fn get_summary(&self) -> HashMap<String, StageStats> {
        self.stats.read().unwrap().clone()
    }

    #[allow(dead_code)]
    pub fn get_stage(&self, stage: &str) -> StageStats {
        self.stats
            .read()
            .unwrap()
            .get(stage)
            .cloned()
            .unwrap_or_default()
    }
}
