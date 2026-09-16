# Repository Guidelines

## Project Structure & Module Organization

`autovid/` contains the Python application. Workflow orchestration lives in `graph.py` and `pipeline.py`; provider adapters and capability metadata live in `providers.py`, `providers_registry.py`, and `provider_caps.py`; media processing is in `media.py`; the local workbench is implemented by `web/server.py` and `web/static/index.html`. Runtime settings belong in `config/`. Put executable checks and deployment helpers in `scripts/`, documentation in `docs/`, and sample scripts in `examples/`. Generated runs, uploaded voices, portraits, and temporary files stay in `runs/`, `assets/`, and `.tmp/` and must not be committed.

## Build, Test, and Development Commands

- `python -m autovid web --open` starts the local workbench.
- `python scripts/check_env.py` checks Python, FFmpeg, and configured runtime dependencies.
- `python -m autovid graph run --script-file "examples\自然口播样片.txt"` runs the LangGraph workflow with a known script.
- `python scripts/smoke_web.py` starts a real local server and exercises the Web API and UI flow.
- `python scripts/smoke_flow.py` verifies workflow state, gates, and checkpoint behavior.
- `python scripts/smoke_provider_caps.py` validates provider capabilities and failure classification.

## Coding Style & Naming Conventions

Use four-space indentation, UTF-8, type hints, and `pathlib.Path` for filesystem work. Follow existing Python naming: `snake_case` for functions and modules, `PascalCase` for classes, and uppercase names for constants. Keep provider-specific behavior inside adapters or declarative presets rather than branching in workflow nodes. The frontend is intentionally dependency-free; preserve its small vanilla HTML/CSS/JavaScript structure. No repository-wide formatter is configured, so match surrounding code and run `python -m compileall -q autovid` before submitting.

## Testing Guidelines

Tests are executable `scripts/smoke_*.py` programs rather than a pytest suite. Add or extend the nearest smoke script for every regression. Prefer local mock providers; do not invoke paid cloud generation during routine tests. Provider changes should cover success, authentication failure, quota failure, and malformed responses. Workflow changes must prove that completed nodes are not rerun after resume.

## Commit & Pull Request Guidelines

Recent commits use Conventional Commit prefixes such as `feat:`, `fix:`, and `chore:` followed by a concise description. Keep commits focused. Pull requests should explain user-visible behavior, list verification commands, and include screenshots for workbench changes. Call out provider/API assumptions and any untested cloud path.

## Security & Configuration

Store API keys only in `config/secrets.json`. Never commit secrets, cloned voice data, portrait photos, generated media, or provider responses containing credentials. API failures must remain explicit; do not add silent fallback to a default voice, local model, or static video.
