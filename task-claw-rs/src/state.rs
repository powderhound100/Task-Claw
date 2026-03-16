use std::path::Path;
use tokio::sync::Mutex;
use tracing::warn;
use crate::types::{AgentState, TaskItem};

/// Guards all JSON file I/O to prevent concurrent read-modify-write races.
#[allow(dead_code)]
pub type FileIoLock = Mutex<()>;

/// Load a JSON file, returning the default on failure.
pub fn load_json_file<T: serde::de::DeserializeOwned>(path: &Path, label: &str) -> Option<T> {
    if !path.exists() {
        return None;
    }
    match std::fs::read_to_string(path) {
        Ok(content) => match serde_json::from_str(&content) {
            Ok(val) => Some(val),
            Err(e) => {
                warn!("Could not parse {}: {}", label, e);
                None
            }
        },
        Err(e) => {
            warn!("Could not read {}: {}", label, e);
            None
        }
    }
}

/// Save a value as JSON to a file.
pub fn save_json_file<T: serde::Serialize>(path: &Path, data: &T) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let json = serde_json::to_string_pretty(data)?;
    std::fs::write(path, json)?;
    Ok(())
}

/// Load tasks from the tasks file.
pub fn load_tasks(path: &Path) -> Vec<TaskItem> {
    load_json_file::<Vec<TaskItem>>(path, "tasks.json").unwrap_or_default()
}

/// Save tasks to the tasks file.
pub fn save_tasks(path: &Path, tasks: &[TaskItem]) -> std::io::Result<()> {
    save_json_file(path, &tasks)
}

/// Load ideas from the ideas file.
pub fn load_ideas(path: &Path) -> Vec<TaskItem> {
    load_json_file::<Vec<TaskItem>>(path, "ideas.json").unwrap_or_default()
}

/// Save ideas to the ideas file.
pub fn save_ideas(path: &Path, ideas: &[TaskItem]) -> std::io::Result<()> {
    save_json_file(path, &ideas)
}

/// Load agent state, resetting daily counter if date changed.
pub fn load_state(path: &Path) -> AgentState {
    let mut state = load_json_file::<AgentState>(path, "agent-state.json")
        .unwrap_or_default();
    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    if state.api_date != today {
        state.api_calls_today = 0;
        state.api_date = today;
    }
    state
}

/// Save agent state.
pub fn save_state(path: &Path, state: &AgentState) -> std::io::Result<()> {
    save_json_file(path, state)
}

/// Generate a unique ID for tasks/ideas.
pub fn generate_id() -> String {
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let hex = uuid::Uuid::new_v4().to_string();
    format!("{:x}-{}", ts, &hex[..6])
}

/// Current timestamp in milliseconds.
pub fn ts_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// Update a task's status and optional note.
pub fn update_task_status(tasks: &mut [TaskItem], task_id: &str, status: &str, note: &str) {
    for t in tasks.iter_mut() {
        if t.id == task_id {
            t.status = status.to_string();
            t.updated = Some(ts_ms());
            if !note.is_empty() {
                let existing = t.ai_analysis.clone().unwrap_or_default();
                t.ai_analysis = Some(format!(
                    "{}\n\n---\n**Agent note:** {}", existing, note
                ));
            }
            break;
        }
    }
}

/// Update an idea's status and optional note.
pub fn update_idea_status(ideas: &mut [TaskItem], idea_id: &str, status: &str, note: &str) {
    for i in ideas.iter_mut() {
        if i.id == idea_id {
            i.status = status.to_string();
            i.updated = Some(ts_ms());
            if !note.is_empty() {
                let existing = i.plan.clone().unwrap_or_default();
                if existing.is_empty() {
                    i.plan = Some(format!("**Agent note:** {}", note));
                } else {
                    i.plan = Some(format!("{}\n\n---\n**Agent note:** {}", existing, note));
                }
            }
            break;
        }
    }
}

/// Find an item by ID across tasks and ideas. Returns (item, is_idea).
pub fn find_item<'a>(
    item_id: &str,
    tasks: &'a [TaskItem],
    ideas: &'a [TaskItem],
) -> Option<(&'a TaskItem, bool)> {
    for t in tasks {
        if t.id == item_id {
            return Some((t, false));
        }
    }
    for i in ideas {
        if i.id == item_id {
            return Some((i, true));
        }
    }
    None
}
