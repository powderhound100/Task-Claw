use tokio::process::Command;
use tracing::{info, warn};

use crate::config::AppConfig;

/// Auto-restart docker-compose services whose files changed.
pub async fn restart_changed_services(config: &AppConfig) {
    if config.restart_service_map.is_empty() {
        return;
    }

    let mut service_map = std::collections::HashMap::new();
    for entry in config.restart_service_map.split(',') {
        let entry = entry.trim();
        if let Some((prefix, svc)) = entry.split_once('=') {
            service_map.insert(prefix.trim().to_string(), svc.trim().to_string());
        }
    }

    if service_map.is_empty() {
        return;
    }

    let diff = match Command::new("git")
        .args(["diff", "--name-only"])
        .current_dir(&config.project_dir)
        .output()
        .await
    {
        Ok(output) => String::from_utf8_lossy(&output.stdout).trim().to_string(),
        Err(e) => {
            warn!("Could not auto-restart services: {}", e);
            return;
        }
    };

    let changed: Vec<&str> = diff.lines().collect();
    let mut restarted = std::collections::HashSet::new();

    for path in &changed {
        for (prefix, svc) in &service_map {
            if path.starts_with(prefix.as_str()) && !restarted.contains(svc) {
                info!("Restarting {} (files changed in {})", svc, prefix);
                Command::new("docker")
                    .args(["compose", "restart", svc])
                    .current_dir(&config.project_dir)
                    .output()
                    .await
                    .ok();
                restarted.insert(svc.clone());
            }
        }
    }

    if restarted.is_empty() {
        info!("No Docker restarts needed");
    }
}
