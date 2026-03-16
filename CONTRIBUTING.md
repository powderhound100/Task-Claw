# Contributing to Task-Claw

Thanks for your interest in contributing! Here's how to get started.

## Setup

1. Fork the repository and clone your fork:
   ```bash
   git clone https://github.com/<your-username>/Task-Claw.git
   cd Task-Claw
   ```

2. Install the single dependency:
   ```bash
   pip install requests
   ```

3. Copy and configure your environment:
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

## Development Workflow

1. Create a feature branch from `master`:
   ```bash
   git checkout -b my-feature
   ```

2. Make your changes. The entire agent lives in `task-claw.py` — there are no modules or build steps.

3. Run the tests before submitting:
   ```bash
   python -m unittest test_pipeline test_e2e -v
   ```

4. Commit using [Conventional Commits](https://www.conventionalcommits.org/) format:
   ```
   feat: add new provider for XYZ
   fix: handle empty response in plan stage
   chore: update .env.example with new option
   refactor: simplify garbage detection logic
   ```

5. Push your branch and open a Pull Request against `master`.

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR.
- Ensure all tests pass.
- Do not commit secrets, API keys, or `.env` files.
- Update `CLAUDE.md` if you change the HTTP API, configuration options, or architecture.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold this code.

## Questions?

Open an issue if something is unclear. We're happy to help.
