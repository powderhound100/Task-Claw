use tokio::process::Command;
use tracing::{info, warn};

use crate::config::AppConfig;

/// Pull latest changes (fast-forward only).
pub async fn git_pull(config: &AppConfig) -> bool {
    match Command::new("git")
        .args(["pull", "--ff-only"])
        .current_dir(&config.project_dir)
        .output()
        .await
    {
        Ok(output) => {
            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if stdout.contains("Already up to date") {
                    info!("Git: already up to date");
                } else {
                    let last_line = stdout.lines().last().unwrap_or("done");
                    info!("Git pull: {}", last_line);
                }
                true
            } else {
                let stderr = String::from_utf8_lossy(&output.stderr);
                warn!("Git pull failed: {}", stderr.trim());
                false
            }
        }
        Err(e) => {
            warn!("Git pull error: {}", e);
            false
        }
    }
}

/// Check if the PROJECT_DIR working tree has any uncommitted changes.
pub async fn has_uncommitted_changes(config: &AppConfig) -> bool {
    match Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(&config.project_dir)
        .output()
        .await
    {
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout);
            !stdout.trim().is_empty()
        }
        Err(_) => false,
    }
}

/// Commit all changes and push.
pub async fn git_commit_and_push(
    task_id: &str,
    title: &str,
    label: &str,
    config: &AppConfig,
) -> bool {
    let project_dir = &config.project_dir;

    // git add -A
    if let Err(e) = Command::new("git")
        .args(["add", "-A"])
        .current_dir(project_dir)
        .output()
        .await
    {
        warn!("git add failed: {}", e);
        return false;
    }

    // Check if anything staged
    match Command::new("git")
        .args(["diff", "--cached", "--quiet"])
        .current_dir(project_dir)
        .output()
        .await
    {
        Ok(output) if output.status.success() => {
            info!("No staged changes to commit");
            return true;
        }
        _ => {}
    }

    let safe_title = title.replace('\n', " ").replace('\r', "");
    let safe_tid = task_id.replace('\n', " ").replace('\r', "");
    let msg = format!(
        "Agent [{}]: {}\n\nTask: {}\nAutomatically {} by Task-Claw agent.\n\n\
         Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
        label, safe_title, safe_tid, label
    );

    match Command::new("git")
        .args(["commit", "-m", &msg])
        .current_dir(project_dir)
        .output()
        .await
    {
        Ok(output) if !output.status.success() => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            warn!("Git commit failed: {}", stderr);
            return false;
        }
        Err(e) => {
            warn!("Git commit error: {}", e);
            return false;
        }
        _ => {}
    }

    git_pull(config).await;

    match Command::new("git")
        .args(["push"])
        .current_dir(project_dir)
        .output()
        .await
    {
        Ok(output) if !output.status.success() => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            warn!("Git push failed: {}", stderr);
            false
        }
        Err(e) => {
            warn!("Git push error: {}", e);
            false
        }
        Ok(_) => {
            info!("Changes committed and pushed");
            true
        }
    }
}
