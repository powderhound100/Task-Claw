use thiserror::Error;

#[allow(dead_code)]
#[derive(Error, Debug)]
pub enum TaskClawError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("HTTP request error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("Config error: {0}")]
    Config(String),

    #[error("Provider error: {0}")]
    Provider(String),

    #[error("Pipeline error: {0}")]
    Pipeline(String),

    #[error("CLI command error: {0}")]
    Cli(String),

    #[error("Timeout: {0}")]
    Timeout(String),

    #[error("Security blocked: {0}")]
    SecurityBlocked(String),

    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, TaskClawError>;
