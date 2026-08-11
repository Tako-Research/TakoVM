# Security Policy

Tako VM exists to run untrusted, often AI-generated, Python. Execution safety is the product, so we treat vulnerabilities in the isolation boundary as our highest-priority class of bug. This document explains what we consider in and out of scope, how to report an issue, and how to deploy Tako VM so its security controls are actually in effect.

## Reporting a Vulnerability

Report vulnerabilities privately through GitHub's coordinated disclosure flow:

1. Go to the [Security tab](https://github.com/Tako-Research/TakoVM/security) of this repository.
2. Click **Report a vulnerability** to open a private advisory.

Please do not open public issues, pull requests, or discussion threads for suspected vulnerabilities until a fix has been released and we have agreed on a disclosure date.

A useful report includes:

- The version, commit SHA, or PyPI release you tested.
- Your configuration relevant to the finding, in particular `container_runtime`, `security_mode`, `enable_seccomp`, and any job types with `network_enabled: true`.
- A minimal reproduction: the submitted code or request, and the observed versus expected behavior.
- The impact you believe it has (container escape, host access, data exfiltration, denial of service, information disclosure).

Reports that demonstrate a sandbox escape (host filesystem access, code execution outside the executor container, reaching the Docker daemon, or bypassing network isolation) are the most valuable and will be prioritized accordingly.

### What to expect

- Acknowledgement of your report within 3 business days.
- An initial assessment, including a severity judgment and whether we can reproduce it, within 10 business days.
- Progress updates as we work toward a fix.
- Credit in the release notes and advisory if you want it; tell us how you would like to be named, or if you prefer to remain anonymous.

We support coordinated disclosure and will agree on a public disclosure date with you. We ask for a 90-day disclosure window as a default, shortened if a fix ships sooner or extended by mutual agreement for complex issues.

### Safe harbor

We will not pursue or support legal action against researchers who, in good faith, find and report vulnerabilities in accordance with this policy, provided they avoid privacy violations, data destruction, and service degradation against systems they do not own. Test against your own deployment, not against other people's hosted instances.

## Supported Versions

Tako VM is pre-1.0 and under active development. Security fixes are applied to the latest released version on the PyPI `tako-vm` package and the `main` branch. We do not backport fixes to older tags. If you are pinning a version, track the latest release and review the changelog before upgrading, since security-relevant defaults may change between releases.

| Version | Supported |
| --- | --- |
| Latest release / `main` | Yes |
| Older tagged releases | No |

## Security Model

Tako VM uses layered isolation. No single layer is treated as sufficient on its own; each is intended to contain failures in the layers above it.

**Process isolation (gVisor).** When `container_runtime: runsc` is set, the executor runs under gVisor, a userspace kernel that intercepts and services guest syscalls instead of passing them to the host kernel. This is the primary boundary against kernel-level container escapes and is the recommended runtime for any untrusted workload.

**Container isolation (Docker).** Each job runs in its own single-use container (removed after every run, on every exit path) with a read-only root filesystem and a non-root user (uid 1000) enforced at runtime. All Linux capabilities are dropped except `SETUID`/`SETGID`, which `gosu` needs to perform the privilege drop, and `KILL`, which the root-side `timeout` supervisor needs to signal the dropped process when its budget expires. `gosu` clears all capabilities on the uid switch, so none of the three reach user code. Writable space is limited to `/output/` and a `/tmp/` that is `noexec` except when runtime dependency installation is enabled (uv needs to execute from its unpack target). Containers carry no persistent state between executions.

Both execution paths -- the API server and the library-mode `Sandbox` -- assemble this posture from the same shared builder and are covered by a test that fails if they ever diverge.

**Syscall filtering (seccomp).** A default-deny seccomp profile (`SCMP_ACT_ERRNO`) allows only a whitelist of syscalls and blocks dangerous ones including `ptrace`, `mount`, `reboot`, `sethostname`, and `init_module`. Controlled by `enable_seccomp`.

**Network isolation.** Containers run with `--network=none` by default, which blocks data exfiltration, command-and-control traffic, and access to internal services. Network access is opt-in per job type via `network_enabled: true`, and when enabled it permits access to any external host: egress filtering is your responsibility (host firewall or Kubernetes NetworkPolicy). Runtime dependency installation and the shared dependency cache are disabled by default to prevent untrusted jobs from fetching and executing package setup code.

**Resource limits.** Memory, CPU, PID, file-descriptor, file-size, and `/tmp` size limits, plus enforced execution timeouts, bound the damage from runaway or deliberately abusive code (fork bombs, memory exhaustion, disk filling).

**Input validation.** Submitted code and inputs are size-capped (100KB code, 1MB input, 300s max timeout by default). Container build inputs (base image, Python version, pip requirements, environment variables, and shared code paths) are validated to reject shell metacharacters, newlines, URL and path specifiers, and directory traversal. Output artifact filenames are checked to block path separators, parent-directory references, and hidden files.

**Output handling.** stdout, stderr, and artifacts are size-capped, and stack traces are rewritten to strip internal host paths before they are returned.

**API front door.** The server enforces a maximum request body size and per-client-IP rate limiting by default. Run it behind TLS in production.

A fuller description lives in [docs/deployment/security.md](https://github.com/Tako-Research/TakoVM/blob/main/docs/deployment/security.md).

## Threat Model

### In scope

The following are vulnerabilities we want reported and will treat as security issues:

| Threat | Expected mitigation |
| --- | --- |
| Code execution escaping the executor container to the host | gVisor, container isolation, seccomp |
| Bypassing network isolation on a `--network=none` job | Docker network namespace |
| Reaching the Docker daemon or socket from executed code | Socket is not mounted into the executor; proxied access only |
| Injection through build inputs (image, version, pip, env, paths) | Input validation |
| Path traversal via artifact filenames or shared code paths | Filename and path validation |
| Resource-limit bypass leading to host denial of service | Memory, CPU, PID, time, and file-size limits |
| Data exfiltration from a no-network job | Network isolation |
| Leakage of host paths or internal state through output or errors | Output and stack-trace sanitization |
| Authentication or rate-limit bypass on the API | API payload and rate-limit controls |

### Out of scope

These are known limitations of the architecture rather than bugs. They will not generally be treated as vulnerabilities, though we welcome discussion of hardening:

| Item | Reason |
| --- | --- |
| Host kernel exploits when running under `runc` instead of gVisor | Standard containers share the host kernel by design. Use gVisor for untrusted code. |
| Compromise of the Docker daemon by an operator with Docker access | Requires privileges Tako VM assumes are trusted. |
| CPU cache, timing, and other side-channel attacks | Shared hardware; requires dedicated hosts or VM-level isolation to address. |
| Observability of execution timing | Job timing is intentionally returned to callers. |
| Vulnerabilities in user-supplied job-type images or dependencies | The operator owns the contents of their images. |
| Misconfiguration that disables a control (see below) | Security depends on deploying with the documented settings. |

For stronger isolation than gVisor provides, run Tako VM on dedicated hosts or pair it with VM-level isolation such as Kata Containers or Firecracker.

## Secure Deployment

Several defaults in the shipped example configuration favor ease of first-run over maximum hardening. If you are exposing Tako VM to untrusted code in production, do not run with the example defaults unchanged. At minimum:

- Keep `security_mode: strict` (the default). It fails closed if gVisor is unavailable. `permissive` silently falls back to standard `runc`, meaning untrusted code can end up running without the userspace-kernel boundary you expect.
- Install and verify gVisor (`docker run --runtime=runsc --rm hello-world`) and keep `container_runtime: runsc`.
- Keep `enable_seccomp: true`.
- Terminate TLS in front of the API and keep rate limiting enabled.
- Keep `network_enabled` off for every job type that does not strictly require egress, and apply external egress filtering to those that do.
- Keep runtime dependency installation and the shared dependency cache disabled unless you fully trust the jobs and the install path.
- Keep the host kernel, Docker, and gVisor patched.
- Review execution records and logs, and re-test your isolation controls after upgrades, since security-relevant defaults can change.

A deployment that sets `security_mode: permissive` on a host without gVisor is running untrusted code with only standard container isolation. Treat that as an unsafe configuration for AI-generated or otherwise untrusted input.

The server binds `127.0.0.1` by default. Binding a non-loopback interface while `api_auth_enabled` is false is refused at startup, because that combination is an unauthenticated remote code-execution endpoint; `allow_unauthenticated_network_access: true` overrides it for deployments that authenticate in front of Tako VM.
