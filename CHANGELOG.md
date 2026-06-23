# Changelog

All notable changes to Tako VM are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.5] - 2026-06-22

Housekeeping release. Tako VM now lives under the Tako Research organization.
There are no functional or API changes.

### Changed

- **Moved to the Tako Research org.** The repository is now
  [github.com/Tako-Research/TakoVM](https://github.com/Tako-Research/TakoVM),
  the documentation is served at
  [tako-research.github.io/TakoVM](https://tako-research.github.io/TakoVM/), and
  the prebuilt executor and server images are published to
  `ghcr.io/tako-research/takovm`. The previous `github.com/las7` URLs redirect,
  and the existing `ghcr.io/las7/takovm` images remain available.
- Copyright attribution updated to Tako Research.

## [0.1.4] - 2026-06-10

The largest release since Tako VM's first PyPI publish. It graduates the Python
SDK to a complete, production-grade client and hardens the execution engine
end-to-end for durability, traceability, and security.

### Added

- **Python SDK, full API parity** (#62): asynchronous submission
  (`submit`/`submit_code`), the complete job lifecycle (`get_status`,
  `get_result`, `cancel`, `rerun`, `fork`), artifact download, paginated
  execution history, and job-type metadata.
- **SDK reliability layer** (#72): pooled sessions with idempotent-GET retries,
  auto-generated idempotency keys for retry-safe submission, an
  `X-Correlation-ID` on every request (exposed on results and exceptions), and a
  structured exception taxonomy (`TransportError`, `ServerError`/`ClientError`
  with `retryable`, `MalformedResponseError`).
- **Module-level SDK parity** (#89): `configure()` and the flat `tako_vm.*`
  helpers now expose the full client surface, so `import tako_vm;
  tako_vm.submit(...)` works without manually instantiating `TakoVM`.
- **Correlation-ID traceability** (#81): correlation IDs are persisted on
  execution records for end-to-end tracing.
- **Opt-in API-key authentication** for the server (#54).
- **Pre-built job-type images** execute directly; contract-less base images are
  refused (#85).
- **Security policy, threat model, and vulnerability reporting process.**

### Changed

- Synchronous `/execute` now runs off the event loop and persists an
  `ExecutionRecord` (#71).
- Default `security_mode` is now `permissive` (falls back to runc when gVisor is
  unavailable); use `strict` to require gVisor.
- Dead-letter-queue TTL is configurable, and DLQ payloads are redacted (#83).
- Failure modes across the server, workers, sandbox, and SDK are now captured
  and verbosely surfaced instead of silently swallowed (#88).
- README and docs overhauled for the PyPI-published package, server-mode-first
  onboarding, and a Japanese translation.

### Fixed

- Idempotent retries use a unique container per attempt with clean output
  isolation (#82); the idempotency key is no longer burned when the queue is
  full (#75).
- Execution watchdog honors per-job-type budgets, kills the container, and
  records the timeout (#73); job futures are shielded from wait timeouts (#64).
- In-container timeout enforcement with a host-side SIGKILL backstop that
  preserves partial output (#68, #63).
- Stale job records are reconciled on startup; shutdown/running transitions are
  persisted (#66).
- Hardened storage: robust record hydration, protected submission/terminal
  fields on upsert, and retries on transient save failures (#74, #78).
- Executor containers are labeled and reliably reclaimed by orphan cleanup
  (#76); stranded workspaces and artifacts past TTL are reclaimed.
- Symlink rejection and replay-read containment in artifact collection;
  container-ID sanitizer hardening and error-classification ordering (#79).
- Docker infrastructure failures are treated as failures, with host timeouts
  classified correctly (#70).
- CLI `--workers`, `--reload`, and explicit host/port handling (#65).
- Postgres-backed tests now actually run in CI and fail loudly when the database
  is unreachable (#80).

### Security

- Runtime dependency installs are disabled by default (#51); Docker access is
  routed through a socket proxy (#52).
- A secrets-looking test fixture was removed and legacy DLQ rows scrubbed (#87).
- Secrets are redacted from configuration validation errors, with
  insecure-default warnings surfaced (#67).

### Known limitations

- The async worker pool is per-process and in-memory; multi-worker deployments
  are warned about explicitly at startup (#87) and remain an architectural
  constraint rather than a supported topology.

## [0.1.3] - 2026-03-17

Earlier releases (0.1.0–0.1.3) established the core sandbox, REST API, async job
queue, PostgreSQL persistence, and the initial library-mode `Sandbox`. See the
[git history](https://github.com/Tako-Research/TakoVM/commits/v0.1.3) for details.

[0.1.5]: https://github.com/Tako-Research/TakoVM/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/Tako-Research/TakoVM/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Tako-Research/TakoVM/releases/tag/v0.1.3
