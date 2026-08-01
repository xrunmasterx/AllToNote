from __future__ import annotations

import math
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.adapters.jobs.workspace_instance_registry import (
    WorkspaceInstance,
    WorkspaceInstanceRegistry,
)
from app.adapters.worker_process import (
    minimal_worker_environment,
    run_worker_process,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import (
    LOCAL_USER_PRINCIPAL,
    JobExecutionOwner,
    JobState,
)
from app.engine.contracts import EngineJobReference
from app.engine.job_store import open_engine_job_store
from app.runtime_paths import RuntimePaths


MAXIMUM_PENDING_JOBS = 256
DISCOVERY_PAGE_SIZE = 100
DEFAULT_RECONCILE_INTERVAL_SECONDS = 30.0
WORKER_TIMEOUT_SECONDS = 24 * 60 * 60

WorkerRunner = Callable[[EngineJobReference, Callable[[], None]], int]


class _DispatcherStopping(RuntimeError):
    pass


@dataclass(frozen=True)
class EngineJobDispatchReceipt:
    state: JobState
    scheduled: bool


class EngineJobDispatcher:
    """Bounded wake queue over persisted engine-owned JobStore truth."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        worker_runner: WorkerRunner | None = None,
        maximum_pending_jobs: int = MAXIMUM_PENDING_JOBS,
        reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        if (
            type(maximum_pending_jobs) is not int
            or maximum_pending_jobs < 1
            or maximum_pending_jobs > 1_000
            or type(reconcile_interval_seconds) not in {int, float}
            or isinstance(reconcile_interval_seconds, bool)
            or not math.isfinite(float(reconcile_interval_seconds))
            or reconcile_interval_seconds <= 0
        ):
            raise ValueError("engine_dispatcher_configuration_invalid")
        self._paths = paths
        self._worker_runner = worker_runner or self._run_worker
        self._maximum_pending_jobs = maximum_pending_jobs
        self._reconcile_interval = float(reconcile_interval_seconds)
        self._condition = threading.Condition()
        self._pending: deque[EngineJobReference] = deque()
        self._known: set[EngineJobReference] = set()
        self._active: EngineJobReference | None = None
        self._reconciling = False
        self._reconcile_failed = False
        self._scan_cursors: dict[str, tuple[str, str] | None] = {}
        self._next_instance_id: str | None = None
        self._draining = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    @property
    def has_work(self) -> bool:
        with self._condition:
            return bool(
                self._pending
                or self._active
                or self._reconciling
                or self._reconcile_failed
            )

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                raise RuntimeError("engine_dispatcher_already_started")
            if self._draining:
                raise RuntimeError("engine_dispatcher_draining")
            self._thread = threading.Thread(
                target=self._run,
                name="alltonote-engine-job-dispatcher",
                daemon=False,
            )
            self._thread.start()

    def close(self, *, force: bool = False) -> None:
        with self._condition:
            if (
                not force
                and (
                    self._pending
                    or self._active
                    or self._reconciling
                    or (self._reconcile_failed and not self._draining)
                )
            ):
                raise RuntimeError("engine_dispatcher_busy")
            self._draining = True
            self._stop.set()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join()

    def try_begin_draining(self) -> bool:
        with self._condition:
            if (
                self._pending
                or self._active
                or self._reconciling
                or self._reconcile_failed
            ):
                return False
            self._draining = True
            self._condition.notify_all()
            return True

    def begin_graceful_shutdown(self) -> None:
        with self._condition:
            self._draining = True
            pending = tuple(self._pending)
            self._pending.clear()
            for reference in pending:
                self._known.discard(reference)
            self._condition.notify_all()

    @property
    def ready_to_close(self) -> bool:
        with self._condition:
            return bool(
                self._draining
                and not self._pending
                and self._active is None
                and not self._reconciling
            )

    def notify(self, reference: EngineJobReference) -> EngineJobDispatchReceipt:
        with self._condition:
            if self._draining:
                raise DomainError(
                    "engine_draining",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Engine is draining",
                )
        instance, repository = self._resolve_repository(reference)
        del instance
        job = repository.get_job(reference.job_id)
        if (
            job.execution_owner is not JobExecutionOwner.ENGINE
            or job.principal != LOCAL_USER_PRINCIPAL
        ):
            raise DomainError(
                "engine_job_authority_denied",
                ErrorCategory.POLICY_DENIED,
                "Engine is not authorized to execute this Job",
            )
        if job.state not in {JobState.QUEUED, JobState.RUNNING}:
            return EngineJobDispatchReceipt(job.state, False)
        return EngineJobDispatchReceipt(job.state, self._enqueue(reference))

    def reconcile(self) -> int:
        with self._condition:
            while self._reconciling and not self._draining:
                self._condition.wait()
            if self._draining:
                return 0
            self._reconciling = True
            self._reconcile_failed = True
        scheduled = 0
        first_error: DomainError | None = None
        try:
            instances = self._ordered_instances()
            for instance in instances:
                try:
                    repository = open_engine_job_store(self._paths, instance)
                except DomainError as error:
                    if first_error is None:
                        first_error = error
                    continue
                cursor = self._scan_cursors.get(instance.instance_id)
                after_created_at = cursor[0] if cursor is not None else None
                after_job_id = cursor[1] if cursor is not None else None
                while True:
                    jobs = repository.list_engine_execution_candidates(
                        principal=LOCAL_USER_PRINCIPAL,
                        after_created_at=after_created_at,
                        after_job_id=after_job_id,
                        limit=DISCOVERY_PAGE_SIZE,
                    )
                    if not jobs:
                        self._scan_cursors[instance.instance_id] = None
                        break
                    for job in jobs:
                        reference = EngineJobReference(instance.instance_id, job.job_id)
                        try:
                            if self._enqueue(reference):
                                scheduled += 1
                        except DomainError as error:
                            if error.code == "engine_notification_backpressure":
                                self._scan_cursors[instance.instance_id] = (
                                    after_created_at,
                                    after_job_id,
                                ) if after_created_at is not None else None
                                self._next_instance_id = instance.instance_id
                                return scheduled
                            raise
                        after_created_at = job.created_at
                        after_job_id = job.job_id
                    if len(jobs) < DISCOVERY_PAGE_SIZE:
                        self._scan_cursors[instance.instance_id] = None
                        break
                    self._scan_cursors[instance.instance_id] = (
                        after_created_at,
                        after_job_id,
                    )
            if first_error is not None:
                raise first_error
            self._next_instance_id = None
            with self._condition:
                self._reconcile_failed = False
            return scheduled
        finally:
            with self._condition:
                self._reconciling = False
                self._condition.notify_all()

    def _enqueue(self, reference: EngineJobReference) -> bool:
        with self._condition:
            if self._draining:
                raise DomainError(
                    "engine_draining",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Engine is draining",
                )
            if reference in self._known:
                return False
            if len(self._known) >= self._maximum_pending_jobs:
                raise DomainError(
                    "engine_notification_backpressure",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Engine notification queue is full",
                )
            self._known.add(reference)
            self._pending.append(reference)
            self._condition.notify_all()
            return True

    def _resolve_repository(self, reference: EngineJobReference):
        registry = self._registry()
        try:
            instance = registry.get(reference.workspace_instance_id)
        except ValueError as error:
            raise DomainError(
                "workspace_instance_registry_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Workspace instance registry is invalid",
            ) from error
        if instance is None:
            raise DomainError(
                "workspace_instance_not_found",
                ErrorCategory.INVALID_REQUEST,
                "Workspace instance does not exist",
            )
        return instance, open_engine_job_store(self._paths, instance)

    def _registry_instances(self) -> tuple[WorkspaceInstance, ...]:
        if not self._paths.workspace_registry_parent.is_dir():
            return ()
        try:
            return self._registry().list()
        except ValueError as error:
            raise DomainError(
                "workspace_instance_registry_invalid",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Workspace instance registry is invalid",
            ) from error

    def _ordered_instances(self) -> tuple[WorkspaceInstance, ...]:
        instances = self._registry_instances()
        if not instances or self._next_instance_id is None:
            return instances
        for index, instance in enumerate(instances):
            if instance.instance_id == self._next_instance_id:
                return instances[index:] + instances[:index]
        self._next_instance_id = None
        return instances

    def _registry(self) -> WorkspaceInstanceRegistry:
        return WorkspaceInstanceRegistry(
            self._paths.workspace_registry_parent,
            inspect_workspace=lambda _root: "unused-read-only-inspector",
        )

    def _run(self) -> None:
        next_reconcile = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_reconcile:
                try:
                    self.reconcile()
                except (DomainError, OSError, ValueError):
                    pass
                next_reconcile = time.monotonic() + self._reconcile_interval
            reference = self._next_reference(next_reconcile)
            if reference is None:
                continue
            exit_code: int | None = None
            try:
                exit_code = self._worker_runner(reference, self._check_running)
            except (_DispatcherStopping, OSError, RuntimeError, ValueError):
                pass
            finally:
                with self._condition:
                    self._active = None
                    self._known.discard(reference)
                    self._condition.notify_all()
            if exit_code == 0:
                next_reconcile = 0.0

    def _next_reference(self, next_reconcile: float) -> EngineJobReference | None:
        with self._condition:
            while not self._stop.is_set() and not self._pending:
                remaining = max(0.0, next_reconcile - time.monotonic())
                if remaining == 0:
                    return None
                self._condition.wait(timeout=remaining)
            if self._stop.is_set() or not self._pending:
                return None
            reference = self._pending.popleft()
            self._active = reference
            return reference

    def _check_running(self) -> None:
        if self._stop.is_set():
            raise _DispatcherStopping

    def _run_worker(
        self,
        reference: EngineJobReference,
        check_running: Callable[[], None],
    ) -> int:
        command = (
            str(Path(sys.executable).resolve()),
            "-I",
            "-B",
            "-m",
            "app.engine.job_worker",
            "--config-dir",
            str(self._paths.config_dir),
            "--data-dir",
            str(self._paths.data_dir),
            "--cache-dir",
            str(self._paths.cache_dir),
            "--state-dir",
            str(self._paths.state_dir),
            "--log-dir",
            str(self._paths.log_dir),
            "--workspace-instance-id",
            reference.workspace_instance_id,
            "--job-id",
            reference.job_id,
        )
        return run_worker_process(
            command,
            cwd=Path(__file__).resolve().parents[2],
            environment=minimal_worker_environment(),
            timeout_seconds=WORKER_TIMEOUT_SECONDS,
            check_running=check_running,
        )


__all__ = [
    "DEFAULT_RECONCILE_INTERVAL_SECONDS",
    "EngineJobDispatchReceipt",
    "EngineJobDispatcher",
    "MAXIMUM_PENDING_JOBS",
]
