from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from iwiki.workspace import open_workspace

from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.adapters.worker_process import minimal_worker_environment, run_worker_process
from app.core.jobs.model import JobExecutionOwner
from app.engine.host import run_engine_host
from app.engine.job_dispatcher import EngineJobDispatcher
from app.engine.job_worker import execute_engine_job
from app.job_runtime import JobRuntime
from app.runtime_paths import RuntimePaths


_PROCESS_TIMEOUT_SECONDS = 20.0


def _runtime_paths(args: argparse.Namespace) -> RuntimePaths:
    return RuntimePaths(
        config_dir=args.config_dir,
        data_dir=args.data_dir,
        cache_dir=args.cache_dir,
        state_dir=args.state_dir,
        log_dir=args.log_dir,
    )


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise RuntimeError("worker_release_timeout")


def _run_worker(args: argparse.Namespace) -> int:
    paths = _runtime_paths(args)
    args.worker_started.write_text(str(os.getpid()), encoding="ascii")
    _wait_for_file(args.worker_release)

    def runtime_factory(
        workspace_root: Path,
        *,
        runtime_paths: RuntimePaths,
        current_config_snapshot,
        execution_owner: JobExecutionOwner,
        require_existing_job_store: bool,
    ) -> JobRuntime:
        if not require_existing_job_store:
            raise RuntimeError("existing_job_store_required")
        if execution_owner is not JobExecutionOwner.ENGINE:
            raise RuntimeError("engine_execution_owner_required")
        registry = WorkspaceInstanceRegistry(
            runtime_paths.workspace_registry_parent,
            inspect_workspace=lambda root: open_workspace(
                root, writable=False
            ).manifest.workspace_id,
        )
        instance = registry.get(args.workspace_instance_id)
        if instance is None or instance.canonical_root != workspace_root:
            raise RuntimeError("workspace_instance_mismatch")
        if args.recipe_kind == "document":
            from tests.helpers.engine_fake_document_runtime import (
                create_fake_document_runtime_for_workspace,
            )

            runtime = create_fake_document_runtime_for_workspace(
                workspace_root,
                paths=runtime_paths,
                call_log=args.call_log,
                require_existing_job_store=True,
            )
        elif args.recipe_kind == "video-local":
            from tests.integration.test_local_media_golden_path import _create_runtime

            runtime = _create_runtime(
                instance.machine_root,
                call_log=args.call_log,
                owner_id="engine-local-video-worker",
                workspace_instance_id=instance.instance_id,
                current_config_snapshot=current_config_snapshot,
            )
        else:
            from app.runtime import create_fake_runtime

            runtime = create_fake_runtime(
                instance.machine_root,
                workspace_instance_id=instance.instance_id,
                current_config_snapshot=current_config_snapshot,
                call_log_path=args.call_log,
            )
        return JobRuntime(
            runtime.job_repository,
            wait_job=lambda requested_job_id: runtime.wait_job(requested_job_id),
            current_config_snapshot=current_config_snapshot,
            execution_owner=execution_owner,
        )

    try:
        execute_engine_job(
            paths,
            workspace_instance_id=args.workspace_instance_id,
            job_id=args.job_id,
            runtime_factory=runtime_factory,
        )
    finally:
        args.worker_finished.write_text(str(os.getpid()), encoding="ascii")
    return 0


def _run_host(args: argparse.Namespace) -> int:
    paths = _runtime_paths(args)

    if os.name != "nt":
        def terminate_host(_signum, _frame) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate_host)

    def worker_runner(reference, check_running) -> int:
        command = (
            str(Path(sys.executable).resolve()),
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            "worker",
            *_runtime_path_arguments(paths),
            "--workspace-instance-id",
            reference.workspace_instance_id,
            "--job-id",
            reference.job_id,
            "--recipe-kind",
            args.recipe_kind,
            "--worker-started",
            str(args.worker_started),
            "--worker-release",
            str(args.worker_release),
            "--worker-finished",
            str(args.worker_finished),
            "--call-log",
            str(args.call_log),
        )
        return run_worker_process(
            command,
            cwd=BACKEND_ROOT,
            environment=minimal_worker_environment(),
            timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
            check_running=check_running,
        )

    import app.engine.host as host_module

    host_module.EngineJobDispatcher = lambda runtime_paths: EngineJobDispatcher(
        runtime_paths,
        worker_runner=worker_runner,
        reconcile_interval_seconds=0.1,
    )
    return run_engine_host(paths=paths, idle_seconds=60.0)


def _runtime_path_arguments(paths: RuntimePaths) -> tuple[str, ...]:
    return (
        "--config-dir",
        str(paths.config_dir),
        "--data-dir",
        str(paths.data_dir),
        "--cache-dir",
        str(paths.cache_dir),
        "--state-dir",
        str(paths.state_dir),
        "--log-dir",
        str(paths.log_dir),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("host", "worker"):
        child = subparsers.add_parser(mode)
        child.add_argument("--config-dir", required=True, type=Path)
        child.add_argument("--data-dir", required=True, type=Path)
        child.add_argument("--cache-dir", required=True, type=Path)
        child.add_argument("--state-dir", required=True, type=Path)
        child.add_argument("--log-dir", required=True, type=Path)
        child.add_argument("--worker-started", required=True, type=Path)
        child.add_argument("--worker-release", required=True, type=Path)
        child.add_argument("--worker-finished", required=True, type=Path)
        child.add_argument("--call-log", required=True, type=Path)
        child.add_argument(
            "--recipe-kind",
            choices=("video", "video-local", "document"),
            required=True,
        )
        if mode == "worker":
            child.add_argument("--workspace-instance-id", required=True)
            child.add_argument("--job-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return _run_host(args) if args.mode == "host" else _run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
