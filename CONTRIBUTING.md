# Contributing to Tako VM

Thanks for your interest in contributing! This guide covers everything you need to get a development environment running and a pull request merged.

## Where to start

- Issues labeled [`good first issue`](https://github.com/Tako-Research/TakoVM/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) are well-scoped entry points.
- Issues labeled [`help wanted`](https://github.com/Tako-Research/TakoVM/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22) are larger pieces where design input is welcome; comment on the issue before starting.
- Questions and ideas: [GitHub Discussions](https://github.com/Tako-Research/TakoVM/discussions).

**Security issues are the exception: never open a public issue.** Report privately via the [Security tab](https://github.com/Tako-Research/TakoVM/security/advisories/new). See [SECURITY.md](SECURITY.md).

## Development setup

Requirements: Python 3.10+, Docker, and `git`.

```bash
git clone https://github.com/Tako-Research/TakoVM.git
cd TakoVM

# Install in editable mode with dev + server extras
pip install -e ".[dev,server]"        # or: uv pip install -e ".[dev,server]"

# Build the executor image (one-time)
docker build -t code-executor:latest -f docker/Dockerfile.executor .

# Start local PostgreSQL for development
tako-vm dev up
```

### macOS notes

- [colima](https://github.com/abiosoft/colima) works fine as the Docker runtime for development (no gVisor, so tests requiring it auto-skip).
- Set `TAKO_VM_WORKSPACE` to a directory under `$HOME`. The default macOS temp dir (`/var/folders/...`) cannot be bind-mounted into the colima VM, and executions fail with `bind source path does not exist`.
- To test against real gVisor on macOS, use the Lima VM config in [`lima-gvisor.yaml`](lima-gvisor.yaml).

## Running tests

```bash
TAKO_VM_SECURITY_MODE=permissive pytest tests/ -v
```

- Tests need Docker running and local PostgreSQL (`tako-vm dev up`).
- Markers `requires_gvisor` and `requires_host_mounts` auto-skip when the environment doesn't support them; `slow` marks long-running tests.
- CI runs the suite on Python 3.10, 3.11, and 3.12 with a coverage gate, plus `TAKO_VM_ENABLE_SECCOMP=false` (GitHub runners don't support the seccomp profile).

## Lint and format

`pre-commit` and `pyright` live in the `tools` dependency group (PEP 735), which the `[dev]` extra does **not** install. Install them once, then enable the hooks:

```bash
pip install --group tools      # pip >= 25.1; or: uv sync --group tools
pre-commit install
```

This runs `ruff` (lint + format) and `pyright`. To run manually:

```bash
ruff check tako_vm/ tests/
ruff format tako_vm/ tests/
```

> **Note:** The ruff version is pinned (`0.15.16`) and must stay in sync across three files: `pyproject.toml`, `.github/workflows/lint.yml`, and `.pre-commit-config.yaml`. If you bump it, bump all three.

## Pull requests

- Use conventional-commit style titles: `feat:`, `fix:`, `docs:`, `chore:`, with an optional scope like `feat(sdk):`.
- PRs are squash-merged, so the PR title becomes the commit message, so make it descriptive.
- CI must pass: ruff, the test matrix (3.10–3.12), and CodeQL.
- Update docs (`docs/`, README) when behavior changes.
- One of the failure-handling principles of this project: **failures are logged and surfaced verbosely, never silently swallowed**. New code paths should follow suit.

## Project architecture

See [docs/architecture.md](docs/architecture.md) for the full picture, and the module map in [CLAUDE.md](CLAUDE.md) for a quick orientation.
