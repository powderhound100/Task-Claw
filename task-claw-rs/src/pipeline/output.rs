use std::path::{Path, PathBuf};
use chrono::Local;
use tracing::info;

/// Save stage output to pipeline-output/{task_id}/{stage}.md.
pub fn save_stage_output(
    pipeline_output_dir: &Path,
    task_id: &str,
    stage: &str,
    content: &str,
    notes: &str,
) -> PathBuf {
    let task_dir = pipeline_output_dir.join(task_id);
    std::fs::create_dir_all(&task_dir).ok();
    let out_file = task_dir.join(format!("{}.md", stage));

    let mut header = format!(
        "# Pipeline Stage: {}\n# Task: {}\n# Saved: {}\n\n",
        stage,
        task_id,
        Local::now().to_rfc3339()
    );
    if !notes.is_empty() {
        header.push_str(&format!("## Notes\n{}\n\n", notes));
    }
    header.push_str("## Output\n");

    let full = format!("{}{}", header, content);
    std::fs::write(&out_file, &full).ok();
    info!(
        "Stage '{}' output saved → {} ({} chars)",
        stage,
        out_file.display(),
        content.len()
    );
    out_file
}

/// Load a previously saved stage output. Returns None if not found.
#[allow(dead_code)]
pub fn load_stage_output(pipeline_output_dir: &Path, task_id: &str, stage: &str) -> Option<String> {
    let out_file = pipeline_output_dir.join(task_id).join(format!("{}.md", stage));
    if out_file.exists() {
        std::fs::read_to_string(&out_file).ok()
    } else {
        None
    }
}

/// Return the path where a stage's output file would be.
pub fn stage_output_path(pipeline_output_dir: &Path, task_id: &str, stage: &str) -> PathBuf {
    pipeline_output_dir.join(task_id).join(format!("{}.md", stage))
}
