use std::time::Duration;
use chrono::Utc;
use tracing::warn;

use crate::types::HooksConfig;

/// Fire webhook hooks for a pipeline event.
/// Events: on_stage_start, on_stage_end, on_verdict.
/// Failures are logged, never block the pipeline.
pub async fn fire_hooks(
    event: &str,
    mut data: serde_json::Value,
    hooks_cfg: &HooksConfig,
    http_client: &reqwest::Client,
) -> Vec<serde_json::Value> {
    let hook_list = match event {
        "on_stage_start" => &hooks_cfg.on_stage_start,
        "on_stage_end" => &hooks_cfg.on_stage_end,
        "on_verdict" => &hooks_cfg.on_verdict,
        _ => return vec![],
    };

    if hook_list.is_empty() {
        return vec![];
    }

    data["event"] = serde_json::Value::String(event.to_string());
    data["timestamp"] = serde_json::Value::String(Utc::now().to_rfc3339());

    let mut handles = Vec::new();
    for hook in hook_list {
        if hook.r#type != "webhook" || hook.url.is_empty() {
            continue;
        }
        let client = http_client.clone();
        let url = hook.url.clone();
        let timeout = hook.timeout;
        let body = data.clone();

        handles.push(tokio::spawn(async move {
            match client
                .post(&url)
                .json(&body)
                .timeout(Duration::from_secs(timeout))
                .send()
                .await
            {
                Ok(resp) => {
                    if resp.status().is_success() {
                        resp.json::<serde_json::Value>().await.ok()
                    } else {
                        warn!("Hook {} failed: HTTP {}", url, resp.status());
                        None
                    }
                }
                Err(e) => {
                    warn!("Hook {} failed: {}", url, e);
                    None
                }
            }
        }));
    }

    let mut responses = Vec::new();
    for handle in handles {
        if let Ok(Some(val)) = handle.await {
            responses.push(val);
        }
    }
    responses
}
