# Changelog

All notable changes to Tako VM are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **`--host` can no longer bypass the fail-closed bind guard.** The config
  validator refused `api_auth_enabled: false` together with a non-loopback
  `server_host`, but `tako-vm server --host 0.0.0.0` resolved the host after
  validation and handed it straight to uvicorn unchecked. The shipped
  `docker/Dockerfile.server` `CMD` does exactly that, over a baked-in config
  with authentication disabled, so `docker compose up` published an
  unauthenticated `POST /execute` code-execution endpoint on every interface.
  The CLI now re-applies the config's own rule (`ensure_bind_host_allowed`,
  one rule shared by the validator and the CLI) to the host it actually binds,
  and exits with a message naming all three ways out.
  `TAKO_VM_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS` is a new environment override
  for the existing escape hatch, needed because container images bake in their
  config file.
- **`docker-compose.yaml` no longer publishes PostgreSQL on the host.** The
  `postgres` service mapped `5432:5432` with `postgres`/`postgres`
  credentials; the API reaches it by service name over the compose network, so
  the mapping only exposed the database to anything that could route to the
  host. Credentials are now interpolated from `TAKO_VM_POSTGRES_USER` /
  `TAKO_VM_POSTGRES_PASSWORD` / `TAKO_VM_POSTGRES_DB` with a placeholder
  default. The API's own port is now published on `127.0.0.1:8000` only.
- **Rate limiting now runs before the authentication rejection.** Failed-auth
  requests returned 401 before reaching the limiter, so API-key brute force was
  unmetered and the DoS control was inert against unauthenticated callers.
  Identity is still resolved first, so authenticated clients keep their
  per-key buckets.
- **A non-ASCII API key header no longer returns HTTP 500.**
  `hmac.compare_digest` raises `TypeError` on non-ASCII `str` operands; that
  reached the middleware's catch-all, which logged a full traceback at ERROR
  and returned 500 — an unauthenticated log-flood amplifier. Keys are now
  compared as UTF-8 bytes.

### Changed

- **BREAKING: a configured job type may no longer declare a `timeout` above
  `max_timeout`** (#146). `max_timeout` is documented as the maximum allowed
  execution timeout, but it only bounded explicit per-request overrides: a
  request could name a job type whose default exceeded it and the worker would
  run the larger value. The ceiling is now enforced on the *effective* timeout
  at three layers — config validation, the API boundary, and the worker — so a
  deployment whose `job_types` declare a timeout above `max_timeout` is refused
  at startup instead of quietly exceeding its own limit. Raise `max_timeout` to
  cover the largest job type you run.
- **`default_timeout` now applies to requests that name no job type.** Such
  requests previously resolved the built-in default job type's hardcoded 30s and
  ignored `default_timeout` entirely; they now use the configured value. Both
  are 30 out of the box, so this changes nothing unless the deployment sets
  `default_timeout`.
- **The `timeout` request field accepts values up to 86,400 seconds** rather
  than being capped at 300 by the request schema (#146, fixes #143). This is an
  absolute schema ceiling only — the deployment's `max_timeout` (default 300)
  still applies and rejects anything above it with HTTP 422, so the accepted
  range is unchanged unless an operator raises `max_timeout`.

## [0.2.0] - 2026-08-12

A security release. The shipped defaults now fail closed, and the default
isolation posture is exercised for the first time: it could not previously start
a container, and CI had switched the two controls off to work around that.

Upgrading is not a drop-in swap. Three defaults changed in ways that will stop a
deployment that relied on the old behavior; each has an explicit opt-out, listed
under Changed.

### Changed

- **BREAKING: `security_mode` now defaults to `strict`** (#156). It was
  `permissive`, so on a host without gVisor every job silently ran on runc while
  the docs called gVisor the sole isolation boundary. Strict refuses to run
  instead. Set `security_mode: permissive` explicitly for local development and
  for CI on hosts without gVisor.
- **BREAKING: `server_host` now defaults to `127.0.0.1`**, and binding a
  non-loopback interface while `api_auth_enabled` is false is refused at startup
  rather than warned about (#156). That combination is an unauthenticated remote
  code-execution endpoint. Deployments that authenticate in front of Tako VM set
  `allow_unauthenticated_network_access: true` to opt out.
- **BREAKING: the shared uv dependency cache is now scoped per job type** rather
  than one host-wide volume (#156). This narrows the blast radius of a poisoned
  cache entry to a group the operator defines. It does not eliminate the channel:
  jobs sharing a job type still share a cache, and Tako VM has no tenant identity
  to key on.
- The effective isolation runtime is recorded and surfaced per job, so a result
  says whether it actually ran under gVisor (#117).
- Dependency installation runs as the unprivileged sandbox user (#116).
- Worker ulimits are mirrored onto the library `Sandbox` path (#113).

### Fixed

- **The default posture now works and now enforces** (#153). Four defects, all
  hidden by CI running a configuration nobody ships:
  - The default-deny seccomp profile blocked container init. It allowed `prctl`
    only for `PR_SET_NAME` and `PR_SET_PDEATHSIG`, but the OCI runtime needs
    `PR_CAPBSET_DROP`, `PR_SET_KEEPCAPS` and `PR_CAP_AMBIENT`; every `docker run`
    failed with "unable to apply bounding set" on native-Linux Docker.
  - The entrypoint aborted before user code: `chown` on the cache dirs needs
    `CAP_CHOWN`, which `--cap-drop=ALL` strips. The dirs are created as the
    sandbox user instead.
  - The in-container timeout never fired. A uid-0 supervisor signalling the
    uid-1000 child needs `CAP_KILL`; without it the SIGTERM was dropped and the
    limit was only enforced by the SIGKILL 10s later, past the host-side backstop.
  - The library path enforced a weaker posture than the server: it never passed
    `--security-opt=seccomp` at all and always mounted `/tmp` exec. Both paths now
    assemble an identical argv, asserted by test.
- `build_session_run_command` validates `workspace_dir` before it becomes a
  read-write host bind mount: absolute, normalized, no `..`, not a sensitive
  system directory, and symlink-resolved (#156).
- Leaked containers are reaped by the periodic cleanup loop (#114).
- Executor library cache directories are redirected to a writable `/tmp` (#135).
- `?view=full` responses no longer drop timing and runtime (#127).
- Correctness and reliability fixes across the durable execution path (#132).
- Container/image removal and build failures are logged instead of swallowed
  (#133).

### Security

- Base images are pinned by digest and the `docker` binary is verified by
  checksum (#118).
- Pillow bumped to 12.2.0 and the dependency/security backlog cleared
  (#115, #148).

### Added

- Executable invariants for the gVisor hardening posture, asserting the full
  assembled argv from both execution paths and that the shipped defaults really
  do run code with ptrace and `/tmp` exec blocked (#136, #153).
- Session-persistence schema and session-container command builders, both
  additive and not yet reachable (#138, #139).

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
