# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Task-Claw, please report it responsibly.

**Do NOT open a public issue for security vulnerabilities.**

Instead, please email the maintainers or use [GitHub's private vulnerability reporting](https://github.com/powderhound100/Task-Claw/security/advisories/new).

Include:
- A description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Suggested fix (if any)

We will acknowledge receipt within 48 hours and aim to provide a fix or mitigation within 7 days for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest on `master` | Yes |
| Older commits | No |

## Security Architecture

Task-Claw includes several built-in security measures:

- **Security review stage** — Every pipeline run includes a structured security audit. HIGH severity findings block code from being pushed to production.
- **API key authentication** — Mutating endpoints require authentication.
- **CORS origin restriction** — Configurable allowed origins.
- **Path traversal protection** — All file-serving routes validate paths.
- **Request body limits** — 2 MB cap on incoming requests.
- **No secrets in commits** — The agent never commits `.env`, credentials, or API keys.

## Scope

This policy covers the Task-Claw agent (`task-claw.py`), web UI (`web/`), and configuration files. It does not cover third-party CLI providers (Claude Code, Copilot, etc.) which have their own security policies.
