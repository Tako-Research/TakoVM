"""Session manager for long-running container workloads."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tako_vm.config import TakoVMConfig
from tako_vm.constants import WORKSPACE_DIR
from tako_vm.execution.docker import generate_container_name, is_native_linux
from tako_vm.execution.worker import CodeExecutor, RuntimeUnavailableError
from tako_vm.job_types import JobType, JobTypeRegistry
from tako_vm.models import SessionEvent, SessionRecord
from tako_vm.security import (
    sanitize_error,
    validate_docker_image,
    validate_env_key,
    validate_env_value,
)
from tako_vm.storage import ExecutionStorage

logger = logging.getLogger(__name__)


class SessionManagerError(Exception):
    """Raised when session operations fail."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class SessionManager:
    """Manage lifecycle and mailbox I/O for long-running sessions."""

    def __init__(
        self,
        config: TakoVMConfig,
        storage: ExecutionStorage,
        registry: JobTypeRegistry,
        executor: CodeExecutor,
    ):
        self.config = config
        self.storage = storage
        self.registry = registry
        self.executor = executor

        self._reaper_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Start background maintenance tasks."""
        if not self.config.sessions_enabled:
            return
        if self._reaper_task is not None:
            return
        self._reaper_task = asyncio.create_task(self._reaper_loop(), name="session-reaper")

    async def stop(self) -> None:
        """Stop background maintenance tasks."""
        if self._reaper_task is None:
            return

        self._reaper_task.cancel()
        try:
            await self._reaper_task
        except asyncio.CancelledError:
            pass
        self._reaper_task = None

    async def create_session(
        self,
        job_type_name: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
        idle_timeout_seconds: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
    ) -> SessionRecord:
        """Create and start a new long-running session container."""
        if not self.config.sessions_enabled:
            raise SessionManagerError("Sessions are disabled by configuration", status_code=403)

        job_type = self._get_job_type(job_type_name)
        if not job_type.session_enabled:
            raise SessionManagerError(
                f"Job type '{job_type.name}' is not enabled for sessions", status_code=400
            )

        image_name = job_type.base_image or self.config.docker_image
        if not validate_docker_image(image_name):
            raise SessionManagerError(f"Invalid image configured for job type '{job_type.name}'")

        idle_timeout = idle_timeout_seconds or self.config.session_idle_timeout_seconds
        ttl = ttl_seconds or self.config.session_max_ttl_seconds
        self._validate_session_timeouts(idle_timeout, ttl)

        session_id = str(uuid.uuid4())
        container_name = generate_container_name("tako-session", session_id)
        created_at = datetime.now(timezone.utc)

        try:
            runtime = self.executor.resolve_runtime_for_job_type(job_type, workload="session")
            gpu_flags = self.executor.build_gpu_flags(job_type)
            gpu_env = self.executor.build_gpu_env_vars(job_type)
        except RuntimeUnavailableError as e:
            raise SessionManagerError(str(e), status_code=400) from e

        workspace_dir, inbox_dir, outbox_dir = self._prepare_workspace(session_id)

        session = SessionRecord(
            session_id=session_id,
            status="creating",
            job_type=job_type.name,
            created_at=created_at,
            last_activity_at=created_at,
            expires_at=created_at + timedelta(seconds=ttl),
            idle_timeout_seconds=idle_timeout,
            ttl_seconds=ttl,
            container_name=container_name,
            image_name=image_name,
            runtime=runtime,
            gpu_enabled=job_type.gpu_enabled,
            gpu_vendor=job_type.gpu_vendor,
            workspace_dir=str(workspace_dir),
            metadata=metadata or {},
        )
        await self.storage.save_session(session)

        command = self._build_create_command(
            session=session,
            job_type=job_type,
            runtime=runtime,
            inbox_dir=inbox_dir,
            outbox_dir=outbox_dir,
            gpu_flags=gpu_flags,
            gpu_env=gpu_env,
        )

        result = await self._run_subprocess(command, timeout=self.config.default_startup_timeout)
        if result.returncode != 0:
            session.status = "failed"
            session.ended_at = datetime.now(timezone.utc)
            session.error_message = sanitize_error((result.stderr or result.stdout).strip())
            await self.storage.save_session(session)
            self._cleanup_workspace(workspace_dir)
            raise SessionManagerError(
                f"Failed to start session container: {session.error_message}",
                status_code=500,
            )

        session.status = "running"
        session.started_at = datetime.now(timezone.utc)
        session.container_id = result.stdout.strip() or None
        await self.storage.save_session(session)

        await self.storage.save_session_event(
            SessionEvent(
                session_id=session.session_id,
                direction="system",
                event_type="session_started",
                payload={
                    "job_type": session.job_type,
                    "runtime": session.runtime,
                    "gpu_enabled": session.gpu_enabled,
                    "gpu_vendor": session.gpu_vendor,
                },
            )
        )

        return session

    async def get_session(self, session_id: str, refresh: bool = True) -> Optional[SessionRecord]:
        """Get a session by ID, optionally refreshing container state."""
        session = await self.storage.get_session(session_id)
        if not session:
            return None

        if refresh:
            session = await self._refresh_session_status(session)
        return session

    async def list_sessions(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[SessionRecord]:
        """List sessions with optional status filter."""
        return await self.storage.list_sessions(status=status, limit=limit, offset=offset)

    async def send_event(
        self,
        session_id: str,
        payload: Any,
        event_type: str = "input",
    ) -> SessionEvent:
        """Write an input message to a session inbox and persist event metadata."""
        session = await self.get_session(session_id, refresh=True)
        if not session:
            raise SessionManagerError("Session not found", status_code=404)
        if session.status != "running":
            raise SessionManagerError(
                f"Session is not running (status={session.status})",
                status_code=409,
            )

        payload_json = json.dumps(payload, ensure_ascii=False)
        payload_size = len(payload_json.encode("utf-8"))
        if payload_size > self.config.session_max_message_bytes:
            raise SessionManagerError(
                "Message payload exceeds session_max_message_bytes",
                status_code=413,
            )

        file_name = f"msg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}.json"
        event = SessionEvent(
            session_id=session.session_id,
            direction="in",
            event_type=event_type,
            payload=payload,
            file_name=file_name,
            created_at=datetime.now(timezone.utc),
        )

        inbox_file = Path(session.workspace_dir) / "inbox" / file_name
        envelope = {
            "event_type": event_type,
            "payload": payload,
            "created_at": event.created_at.isoformat(),
        }
        self._write_json_atomically(inbox_file, envelope)

        saved = await self.storage.save_session_event(event)
        await self.storage.touch_session(session.session_id)
        return saved

    async def poll_events(
        self,
        session_id: str,
        after_id: int = 0,
        limit: int = 100,
    ) -> Tuple[SessionRecord, List[SessionEvent]]:
        """Poll outbound session events after a cursor."""
        session = await self.get_session(session_id, refresh=True)
        if not session:
            raise SessionManagerError("Session not found", status_code=404)

        await self._ingest_outbox(session)

        bounded_limit = max(1, min(limit, self.config.session_max_events_per_poll))
        events = await self.storage.list_session_events(
            session_id=session.session_id,
            after_id=after_id,
            limit=bounded_limit,
        )

        if events:
            await self.storage.touch_session(session.session_id)
            session = await self.get_session(session.session_id, refresh=False) or session

        return session, events

    async def terminate_session(self, session_id: str, reason: str = "terminated") -> SessionRecord:
        """Stop and mark a session as terminated/expired."""
        session = await self.storage.get_session(session_id)
        if not session:
            raise SessionManagerError("Session not found", status_code=404)

        if session.status in {"terminated", "expired", "failed"}:
            return session

        result = await self._run_subprocess(
            ["docker", "rm", "-f", session.container_name],
            timeout=20,
        )

        if result.returncode != 0 and "No such container" not in (result.stderr or ""):
            logger.warning(
                "Failed to remove session container %s: %s",
                session.container_name,
                sanitize_error((result.stderr or result.stdout).strip()),
            )

        session.status = "expired" if reason.startswith("expired") else "terminated"
        session.ended_at = datetime.now(timezone.utc)
        session.last_activity_at = session.ended_at
        session.terminated_reason = reason
        await self.storage.save_session(session)

        await self.storage.save_session_event(
            SessionEvent(
                session_id=session.session_id,
                direction="system",
                event_type="session_ended",
                payload={"reason": reason, "status": session.status},
                created_at=session.ended_at,
            )
        )

        self._cleanup_workspace(Path(session.workspace_dir))
        return session

    def _get_job_type(self, job_type_name: Optional[str]) -> JobType:
        """Resolve session job type and require an explicit registry match."""
        if not job_type_name:
            raise SessionManagerError("job_type is required for session creation")

        lookup_name = job_type_name.split("@", 1)[0]
        job_type = self.registry.get(lookup_name)
        if not job_type:
            raise SessionManagerError(f"Job type '{lookup_name}' not found", status_code=404)
        return job_type

    def _validate_session_timeouts(self, idle_timeout_seconds: int, ttl_seconds: int) -> None:
        """Validate per-session timeout overrides against global bounds."""
        if idle_timeout_seconds < 30:
            raise SessionManagerError("idle_timeout_seconds must be at least 30")
        if ttl_seconds < 60:
            raise SessionManagerError("ttl_seconds must be at least 60")
        if idle_timeout_seconds > ttl_seconds:
            raise SessionManagerError("idle_timeout_seconds must be <= ttl_seconds")
        if idle_timeout_seconds > self.config.session_max_ttl_seconds:
            raise SessionManagerError("idle_timeout_seconds exceeds server session_max_ttl_seconds")
        if ttl_seconds > self.config.session_max_ttl_seconds:
            raise SessionManagerError("ttl_seconds exceeds server session_max_ttl_seconds")

    def _prepare_workspace(self, session_id: str) -> Tuple[Path, Path, Path]:
        """Create and return workspace/inbox/outbox directories."""
        workspace_dir = Path(WORKSPACE_DIR) / f"tako-session-{session_id}"
        inbox_dir = workspace_dir / "inbox"
        outbox_dir = workspace_dir / "outbox"

        inbox_dir.mkdir(parents=True, exist_ok=True)
        outbox_dir.mkdir(parents=True, exist_ok=True)

        inbox_dir.chmod(0o777)
        outbox_dir.chmod(0o777)

        return workspace_dir, inbox_dir, outbox_dir

    def _build_create_command(
        self,
        session: SessionRecord,
        job_type: JobType,
        runtime: str,
        inbox_dir: Path,
        outbox_dir: Path,
        gpu_flags: List[str],
        gpu_env: Dict[str, str],
    ) -> List[str]:
        """Build docker run command for a detached session container."""
        limits = self.config.container_limits

        cmd = [
            "docker",
            "run",
            "-d",
            f"--name={session.container_name}",
            "--label=tako-vm-session=true",
            f"--label=tako-vm-session-id={session.session_id}",
            "--init",
            "--read-only",
        ]

        if self.config.enable_cap_restrictions:
            cmd.extend(["--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID"])

        if runtime == "runsc":
            cmd.append("--runtime=runsc")

        cmd.extend(gpu_flags)

        if job_type.network_enabled:
            cmd.append("--network=bridge")
        else:
            cmd.append("--network=none")

        if self.config.enable_userns:
            cmd.append("--user=1000:1000")

        cmd.extend(
            [
                f"--memory={job_type.memory_limit}",
                f"--memory-swap={job_type.memory_limit}",
                f"--cpus={job_type.cpu_limit}",
                f"--pids-limit={limits.pids_limit}",
                f"--ulimit=nofile={limits.nofile_soft}:{limits.nofile_hard}",
                f"--ulimit=nproc={limits.nproc_soft}:{limits.nproc_hard}",
                f"--ulimit=fsize={limits.fsize}",
                f"--mount=type=bind,source={inbox_dir.absolute()},target=/session/inbox",
                f"--mount=type=bind,source={outbox_dir.absolute()},target=/session/outbox",
                f"--tmpfs=/tmp:rw,noexec,nosuid,size={limits.tmpfs_size}",
            ]
        )

        if self.config.enable_seccomp and self.config.seccomp_profile_path:
            if is_native_linux() and self.config.seccomp_profile_path.exists():
                cmd.append(f"--security-opt=seccomp={self.config.seccomp_profile_path}")

        for key, value in job_type.environment.items():
            if not validate_env_key(key):
                logger.warning("Skipping invalid environment variable key: %s", key)
                continue
            if not validate_env_value(value):
                logger.warning("Skipping unsafe environment variable value for key: %s", key)
                continue
            cmd.append(f"--env={key}={value}")

        cmd.extend(
            [
                f"--env=TAKO_SESSION_ID={session.session_id}",
                "--env=TAKO_SESSION_INBOX=/session/inbox",
                "--env=TAKO_SESSION_OUTBOX=/session/outbox",
            ]
        )

        for key, value in gpu_env.items():
            cmd.append(f"--env={key}={value}")

        cmd.append(session.image_name)
        return cmd

    async def _run_subprocess(
        self, cmd: List[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        """Execute a subprocess command in a thread to avoid blocking the loop."""
        return await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    async def _refresh_session_status(self, session: SessionRecord) -> SessionRecord:
        """Refresh session status from Docker container state."""
        if session.status not in {"creating", "running"}:
            return session

        inspect = await self._run_subprocess(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}\t{{.State.ExitCode}}",
                session.container_name,
            ],
            timeout=10,
        )

        if inspect.returncode != 0:
            stderr = inspect.stderr or ""
            if "No such object" in stderr or "No such container" in stderr:
                session.status = "failed"
                session.ended_at = datetime.now(timezone.utc)
                session.error_message = "Session container is not running"
                await self.storage.save_session(session)
                return session
            return session

        output = inspect.stdout.strip()
        if not output:
            return session

        state_text, _, exit_code_text = output.partition("\t")

        if state_text == "running":
            if session.status == "creating":
                session.status = "running"
                session.started_at = datetime.now(timezone.utc)
                await self.storage.save_session(session)
            return session

        if state_text in {"exited", "dead"}:
            session.status = "failed"
            session.ended_at = datetime.now(timezone.utc)
            session.error_message = f"Container exited with code {exit_code_text or '?'}"
            await self.storage.save_session(session)
            return session

        return session

    async def _ingest_outbox(self, session: SessionRecord) -> None:
        """Ingest JSON message files from outbox into session_events table."""
        outbox_dir = Path(session.workspace_dir) / "outbox"
        if not outbox_dir.exists():
            return

        files = sorted(outbox_dir.glob("*.json"))
        for path in files:
            try:
                content = path.read_text(encoding="utf-8")
                try:
                    payload = json.loads(content)
                except json.JSONDecodeError:
                    payload = {
                        "raw": content,
                        "parse_error": "invalid_json",
                        "file_name": path.name,
                    }

                event_type = "message"
                if isinstance(payload, dict):
                    maybe_event_type = payload.get("event_type")
                    if isinstance(maybe_event_type, str) and maybe_event_type.strip():
                        event_type = maybe_event_type.strip()[:64]

                await self.storage.save_session_event(
                    SessionEvent(
                        session_id=session.session_id,
                        direction="out",
                        event_type=event_type,
                        payload=payload,
                        file_name=path.name,
                        created_at=datetime.now(timezone.utc),
                    )
                )

                try:
                    path.unlink()
                except OSError:
                    logger.debug("Failed to remove processed outbox file: %s", path)
            except Exception as e:
                logger.warning("Failed to ingest outbox file %s: %s", path, sanitize_error(str(e)))

    async def _reaper_loop(self) -> None:
        """Expire idle or over-TTL sessions."""
        interval = 30
        while True:
            try:
                await asyncio.sleep(interval)

                running_sessions = await self.storage.list_sessions(status="running", limit=1000)
                now = datetime.now(timezone.utc)

                for session in running_sessions:
                    ttl_exceeded = (now - session.created_at).total_seconds() >= session.ttl_seconds
                    idle_exceeded = (
                        now - session.last_activity_at
                    ).total_seconds() >= session.idle_timeout_seconds

                    if ttl_exceeded:
                        await self.terminate_session(
                            session.session_id,
                            reason="expired_ttl",
                        )
                    elif idle_exceeded:
                        await self.terminate_session(
                            session.session_id,
                            reason="expired_idle",
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Session reaper error: %s", sanitize_error(str(e)), exc_info=True)

    def _write_json_atomically(self, path: Path, payload: Dict[str, Any]) -> None:
        """Atomically write JSON data to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)

    def _cleanup_workspace(self, workspace: Path) -> None:
        """Remove session workspace directory."""
        try:
            shutil.rmtree(workspace)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(
                "Failed to cleanup session workspace %s: %s", workspace, sanitize_error(str(e))
            )
