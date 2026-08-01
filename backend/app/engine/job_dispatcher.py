from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.adapters.jobs.machine_resource_lease import MachineResourceLeaseStore
from app.adapters.jobs.workspace_instance_registry import (
    WorkspaceInstance,
    WorkspaceInstanceRegistry,
)
from app.adapters.worker_process import (
    minimal_worker_environment,
    run_worker_process,
)
from app.core.errors import DomainError, ErrorCategory, ErrorDetail
from app.core.jobs.model import (
    LOCAL_USER_PRINCIPAL,
    AttemptState,
    JobExecutionOwner,
    JobState,
)
from app.core.jobs.resource_lease import (
    HEAVY_PRODUCTION_RESOURCE_NAME,
    HEAVY_PRODUCTION_RESOURCE_NAMES,
    JobExecutionAuthority,
    ResourceLease,
    ResourceLeaseHandoff,
    ResourceOwner,
)
from app.engine.contracts import EngineJobReference, EngineWorkerLaunchV1
from app.engine.job_store import open_engine_job_store
from app.runtime_paths import RuntimePaths


MAXIMUM_PENDING_JOBS = 256
DISCOVERY_PAGE_SIZE = 100
DEFAULT_RECONCILE_INTERVAL_SECONDS = 30.0
WORKER_TIMEOUT_SECONDS = 24 * 60 * 60
WORKER_AUTHORITY_TTL_SECONDS = 300
WORKER_HEARTBEAT_INTERVAL_SECONDS = 30.0
WORKER_CANCELLATION_POLL_SECONDS = 0.25
WORKER_CANCELLATION_GRACE_SECONDS = 5.0
MAXIMUM_AUTOMATIC_WORKER_LAUNCHES = 3
DEFAULT_MAXIMUM_ACTIVE_WORKERS = 1
MAXIMUM_ACTIVE_WORKERS = len(HEAVY_PRODUCTION_RESOURCE_NAMES)
_SCHEDULER_WAITING_EVENT = "scheduler.waiting.v1"
_SCHEDULER_ADMITTED_EVENT = "scheduler.admitted.v1"
_SCHEDULER_WAITING_PAYLOAD = json.dumps(
    {
        "schema_version": 1,
        "reason": "resource_capacity",
        "resource_class": "produce:heavy",
    },
    sort_keys=True,
    separators=(",", ":"),
)
_SCHEDULER_ADMITTED_PAYLOAD = json.dumps(
    {"schema_version": 1, "resource_class": "produce:heavy"},
    sort_keys=True,
    separators=(",", ":"),
)

WorkerRunner = Callable[[EngineWorkerLaunchV1, Callable[[], None]], int]


class _DispatcherStopping(RuntimeError):
    pass


class _WorkerCancellationRequested(RuntimeError):
    pass


@dataclass
class _WorkerAdmission:
    repository: object
    resource_store: MachineResourceLeaseStore
    source_lease: ResourceLease
    launch: EngineWorkerLaunchV1
    next_heartbeat: float = 0.0
    next_cancel_poll: float = 0.0
    cancellation_started: float | None = None

    def check_running(self, check_engine: Callable[[], None]) -> None:
        check_engine()
        now = time.monotonic()
        if now >= self.next_cancel_poll:
            job = self.repository.get_job(self.launch.reference.job_id)
            if job.state is JobState.CANCELLED:
                raise _WorkerCancellationRequested
            if job.state not in {JobState.QUEUED, JobState.RUNNING}:
                return
            if job.cancellation_requested:
                if self.cancellation_started is None:
                    self.cancellation_started = now
                elif now - self.cancellation_started >= WORKER_CANCELLATION_GRACE_SECONDS:
                    raise _WorkerCancellationRequested
            else:
                self.cancellation_started = None
            self.next_cancel_poll = now + WORKER_CANCELLATION_POLL_SECONDS
        if now < self.next_heartbeat:
            return
        self.repository.heartbeat_job_claim(
            self.launch.job_authority,
            ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS,
        )
        try:
            self.source_lease = self.source_lease.heartbeat(
                ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS
            )
        except DomainError as error:
            if error.code != "resource_lease_lost":
                raise
            self.resource_store.heartbeat_adopted(
                self.launch.resource_handoff,
                ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS,
            )
        self.next_heartbeat = now + WORKER_HEARTBEAT_INTERVAL_SECONDS

    def settle_cancellation(self) -> None:
        try:
            if not self.repository.is_cancellation_requested(
                self.launch.reference.job_id
            ):
                return
            claim = self.repository.claim_job(
                self.launch.reference.job_id,
                self.launch.job_authority.owner_id,
                ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS,
            )
            if claim.authority != self.launch.job_authority:
                self.repository.release_job_claim(claim.authority)
                return
        except (DomainError, OSError, RuntimeError, ValueError):
            return

    def release(self) -> None:
        try:
            self.repository.release_job_claim(self.launch.job_authority)
        except Exception:
            pass
        try:
            self.resource_store.release_adopted(self.launch.resource_handoff)
        except Exception:
            pass
        try:
            self.source_lease.release()
        except Exception:
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
        maximum_active_workers: int = DEFAULT_MAXIMUM_ACTIVE_WORKERS,
        reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        if (
            type(maximum_pending_jobs) is not int
            or maximum_pending_jobs < 1
            or maximum_pending_jobs > 1_000
            or type(maximum_active_workers) is not int
            or not 1 <= maximum_active_workers <= MAXIMUM_ACTIVE_WORKERS
            or maximum_pending_jobs < maximum_active_workers
            or type(reconcile_interval_seconds) not in {int, float}
            or isinstance(reconcile_interval_seconds, bool)
            or not math.isfinite(float(reconcile_interval_seconds))
            or reconcile_interval_seconds <= 0
        ):
            raise ValueError("engine_dispatcher_configuration_invalid")
        self._paths = paths
        self._worker_runner = worker_runner or self._run_worker
        self._maximum_pending_jobs = maximum_pending_jobs
        self._maximum_active_workers = maximum_active_workers
        self._reconcile_interval = float(reconcile_interval_seconds)
        self._condition = threading.Condition()
        self._pending: deque[EngineJobReference] = deque()
        self._known: set[EngineJobReference] = set()
        self._active: set[EngineJobReference] = set()
        self._worker_threads: set[threading.Thread] = set()
        self._reconcile_requested = False
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
                and not self._active
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
        try:
            while not self._stop.is_set():
                with self._condition:
                    if self._reconcile_requested:
                        next_reconcile = 0.0
                        self._reconcile_requested = False
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
                admission: _WorkerAdmission | None = None
                try:
                    admission = self._admit_worker(reference)
                    if admission is None:
                        self._finish_reference(reference, reconcile=True)
                        continue
                    self._record_resource_admitted(
                        admission.repository,
                        reference.job_id,
                    )
                    self._start_worker(reference, admission)
                except DomainError as error:
                    if error.code == "resource_busy":
                        try:
                            self._record_resource_waiting(reference)
                        except (DomainError, OSError, RuntimeError, ValueError):
                            pass
                    if admission is not None:
                        admission.release()
                    self._finish_reference(reference)
                except Exception:
                    if admission is not None:
                        admission.release()
                    self._finish_reference(reference)
        finally:
            self._join_worker_threads()

    def _start_worker(
        self,
        reference: EngineJobReference,
        admission: _WorkerAdmission,
    ) -> None:
        worker = threading.Thread(
            target=self._supervise_worker,
            args=(reference, admission),
            name=f"alltonote-engine-worker-{reference.job_id}",
            daemon=False,
        )
        with self._condition:
            self._worker_threads.add(worker)
        try:
            worker.start()
        except BaseException:
            with self._condition:
                self._worker_threads.discard(worker)
            raise

    def _supervise_worker(
        self,
        reference: EngineJobReference,
        admission: _WorkerAdmission,
    ) -> None:
        exit_code: int | None = None
        launch_count: int | None = None
        controlled_stop = False
        try:
            launch_count = admission.repository.record_engine_worker_launch(
                reference.job_id,
                admission.launch.job_authority,
            )
            exit_code = self._worker_runner(
                admission.launch,
                lambda: admission.check_running(self._check_running),
            )
        except (_DispatcherStopping, _WorkerCancellationRequested):
            controlled_stop = True
        except Exception:
            pass
        finally:
            try:
                try:
                    admission.settle_cancellation()
                    job = admission.repository.get_job(reference.job_id)
                    if job.state not in {JobState.QUEUED, JobState.RUNNING}:
                        admission.repository.clear_engine_worker_launches(
                            reference.job_id,
                            admission.launch.job_authority,
                        )
                    elif (
                        not controlled_stop
                        and launch_count is not None
                        and launch_count >= MAXIMUM_AUTOMATIC_WORKER_LAUNCHES
                    ):
                        self._settle_worker_exhaustion(
                            admission,
                            launch_count,
                        )
                except Exception:
                    with self._condition:
                        self._reconcile_failed = True
            finally:
                admission.release()
                self._finish_reference(reference, reconcile=exit_code == 0)
                with self._condition:
                    self._worker_threads.discard(threading.current_thread())
                    self._condition.notify_all()

    def _finish_reference(
        self,
        reference: EngineJobReference,
        *,
        reconcile: bool = False,
    ) -> None:
        with self._condition:
            self._active.discard(reference)
            self._known.discard(reference)
            if reconcile:
                self._reconcile_requested = True
            self._condition.notify_all()

    def _join_worker_threads(self) -> None:
        while True:
            with self._condition:
                workers = tuple(self._worker_threads)
            if not workers:
                return
            for worker in workers:
                worker.join()
                with self._condition:
                    if not worker.is_alive():
                        self._worker_threads.discard(worker)

    @staticmethod
    def _latest_scheduler_event(repository, job_id: str) -> str | None:
        for event in reversed(repository.list_events(job_id)):
            if event.event_type in {
                _SCHEDULER_WAITING_EVENT,
                _SCHEDULER_ADMITTED_EVENT,
            }:
                return event.event_type
        return None

    def _record_resource_waiting(self, reference: EngineJobReference) -> None:
        _instance, repository = self._resolve_repository(reference)
        if (
            self._latest_scheduler_event(repository, reference.job_id)
            == _SCHEDULER_WAITING_EVENT
        ):
            return
        repository.append_event(
            reference.job_id,
            _SCHEDULER_WAITING_EVENT,
            _SCHEDULER_WAITING_PAYLOAD,
        )

    def _record_resource_admitted(self, repository, job_id: str) -> None:
        if (
            self._latest_scheduler_event(repository, job_id)
            != _SCHEDULER_WAITING_EVENT
        ):
            return
        repository.append_event(
            job_id,
            _SCHEDULER_ADMITTED_EVENT,
            _SCHEDULER_ADMITTED_PAYLOAD,
        )

    def _settle_worker_exhaustion(
        self,
        admission: _WorkerAdmission,
        launch_count: int,
    ) -> None:
        repository = admission.repository
        job_id = admission.launch.reference.job_id
        claim = repository.claim_job(
            job_id,
            admission.launch.job_authority.owner_id,
            ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS,
        )
        try:
            self._settle_exhausted_worker_failure(
                repository,
                claim,
                launch_count,
            )
        finally:
            repository.release_job_claim(claim.authority)

    @staticmethod
    def _settle_exhausted_worker_failure(
        repository,
        claim,
        launch_count: int,
    ) -> None:
        if claim.job.state not in {JobState.QUEUED, JobState.RUNNING}:
            return
        attempt = claim.active_attempt
        if (
            attempt is not None
            and attempt.state is AttemptState.RUNNING
            and attempt.fencing_token != claim.authority.fencing_token
        ):
            attempt = repository.take_over_running_attempt(
                claim.job.job_id,
                attempt.attempt_id,
                claim.authority,
            )
        if attempt is not None:
            unknown = repository.reconcile_external_operations_after_process_loss(
                claim.job.job_id,
                claim.authority,
            )
            if unknown:
                repository.pause_for_external_outcome_atomic(
                    claim.job.job_id,
                    attempt.attempt_id,
                    claim.authority,
                )
                repository.clear_engine_worker_launches(
                    claim.job.job_id,
                    claim.authority,
                )
                return
        repository.fail_job_atomic(
            claim.job.job_id,
            ErrorDetail(
                "engine_worker_retry_exhausted",
                ErrorCategory.RETRYABLE_RUNTIME,
                "Engine Worker repeatedly failed",
                {"automatic_launch_count": launch_count},
            ),
            attempt_id=(attempt.attempt_id if attempt is not None else None),
            authority=claim.authority,
        )

    def _admit_worker(
        self,
        reference: EngineJobReference,
    ) -> _WorkerAdmission | None:
        instance, repository = self._resolve_repository(reference)
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
            return None
        launch_count = repository.get_engine_worker_launch_count(
            reference.job_id
        )
        if launch_count >= MAXIMUM_AUTOMATIC_WORKER_LAUNCHES:
            claim = repository.claim_job(
                reference.job_id,
                f"engine-recovery-{uuid4().hex}",
                ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS,
            )
            try:
                self._settle_exhausted_worker_failure(
                    repository,
                    claim,
                    launch_count,
                )
            finally:
                repository.release_job_claim(claim.authority)
            return None
        resource_store = MachineResourceLeaseStore.open(
            self._paths.data_dir / "machine"
        )
        resource_owner = ResourceOwner(
            instance.workspace_identity,
            f"engine-supervisor-{uuid4().hex}",
            process_id=os.getpid(),
        )
        last_busy: DomainError | None = None
        source_lease: ResourceLease | None = None
        resource_names = (
            (HEAVY_PRODUCTION_RESOURCE_NAME,)
            if self._maximum_active_workers == 1
            else HEAVY_PRODUCTION_RESOURCE_NAMES
        )
        for resource_name in resource_names:
            try:
                source_lease = resource_store.acquire(
                    resource_name,
                    resource_owner,
                    ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS,
                )
                break
            except DomainError as error:
                if error.code != "resource_busy":
                    raise
                last_busy = error
        if source_lease is None:
            assert last_busy is not None
            raise last_busy
        handoff: ResourceLeaseHandoff | None = None
        authority: JobExecutionAuthority | None = None
        try:
            worker_owner = ResourceOwner(
                instance.workspace_identity,
                f"engine-worker-{uuid4().hex}",
            )
            handoff = resource_store.handoff(
                source_lease,
                worker_owner,
                ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS,
            )
            claim = repository.claim_job(
                reference.job_id,
                worker_owner.process_instance_id,
                ttl_seconds=WORKER_AUTHORITY_TTL_SECONDS,
            )
            authority = claim.authority
            if claim.job.state not in {JobState.QUEUED, JobState.RUNNING}:
                repository.release_job_claim(authority)
                source_lease.release()
                return None
            launch = EngineWorkerLaunchV1(1, reference, handoff, authority)
            return _WorkerAdmission(
                repository=repository,
                resource_store=resource_store,
                source_lease=source_lease,
                launch=launch,
            )
        except BaseException:
            if authority is not None:
                try:
                    repository.release_job_claim(authority)
                except Exception:
                    pass
            if handoff is not None:
                try:
                    resource_store.release_adopted(handoff)
                except Exception:
                    pass
            try:
                source_lease.release()
            except Exception:
                pass
            raise

    def _next_reference(self, next_reconcile: float) -> EngineJobReference | None:
        while not self._stop.is_set():
            waiting_reference: EngineJobReference | None = None
            with self._condition:
                if self._reconcile_requested:
                    return None
                if (
                    self._pending
                    and len(self._active) < self._maximum_active_workers
                ):
                    reference = self._pending.popleft()
                    self._active.add(reference)
                    return reference
                if self._pending:
                    waiting_reference = self._pending[0]
                remaining = max(0.0, next_reconcile - time.monotonic())
                if remaining == 0:
                    return None
                if waiting_reference is None:
                    self._condition.wait(timeout=remaining)
                    continue
            try:
                self._record_resource_waiting(waiting_reference)
            except (DomainError, OSError, RuntimeError, ValueError):
                pass
            with self._condition:
                if (
                    self._stop.is_set()
                    or self._reconcile_requested
                    or not self._pending
                    or len(self._active) < self._maximum_active_workers
                ):
                    continue
                remaining = max(0.0, next_reconcile - time.monotonic())
                if remaining == 0:
                    return None
                self._condition.wait(timeout=remaining)
        return None

    def _check_running(self) -> None:
        if self._stop.is_set():
            raise _DispatcherStopping

    def _run_worker(
        self,
        launch: EngineWorkerLaunchV1,
        check_running: Callable[[], None],
    ) -> int:
        reference = launch.reference
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
            environment=minimal_worker_environment(
                overrides={
                    key: value
                    for key in (
                        "APPDATA",
                        "CODEX_HOME",
                        "HOME",
                        "LOCALAPPDATA",
                        "USERPROFILE",
                    )
                    if type(value := os.environ.get(key)) is str and value
                }
            ),
            timeout_seconds=WORKER_TIMEOUT_SECONDS,
            stdin_payload=launch.to_bytes(),
            check_running=check_running,
        )


__all__ = [
    "DEFAULT_MAXIMUM_ACTIVE_WORKERS",
    "DEFAULT_RECONCILE_INTERVAL_SECONDS",
    "EngineJobDispatchReceipt",
    "EngineJobDispatcher",
    "MAXIMUM_PENDING_JOBS",
    "MAXIMUM_ACTIVE_WORKERS",
]
