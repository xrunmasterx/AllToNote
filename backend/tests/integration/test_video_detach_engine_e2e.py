from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from multiprocessing import get_context
from pathlib import Path

import pytest
from iwiki.workspace import open_workspace

from app.adapters.jobs.workspace_instance_registry import WorkspaceInstanceRegistry
from app.adapters.worker_process import minimal_worker_environment
from app.cli.main import main
from app.core.jobs.model import JobExecutionOwner
from app.engine.client import LocalEngineClient
from app.engine.job_store import open_engine_job_store
from app.runtime_config import RuntimeConfigService
from app.runtime_paths import resolve_runtime_paths


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
PROCESS_HELPER = Path(__file__).parents[1] / "helpers" / "engine_fake_video_process.py"
_PROCESS_TIMEOUT_SECONDS = 20.0


def _wait_for_file(path: Path, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path.name}")


def _wait_for_control_files(
    directory: Path,
    *,
    expected_count: int,
    timeout_seconds: float,
) -> dict[str, int]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        values = {
            path.name: int(path.read_text(encoding="ascii"))
            for path in directory.iterdir()
            if path.is_file()
        }
        if len(values) >= expected_count:
            return values
        time.sleep(0.02)
    raise AssertionError(
        f"Timed out waiting for {expected_count} files in {directory.name}"
    )


def _run_detached_submit(
    workspace_root: Path,
    local_data_parent: Path,
    recipe_kind: str,
    input_value: str,
    call_log: Path,
    results,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=local_data_parent)
    if recipe_kind == "document":
        from tests.helpers.engine_fake_document_runtime import (
            create_fake_document_runtime_for_workspace,
        )

        runtime = create_fake_document_runtime_for_workspace(
            workspace_root,
            paths=paths,
            call_log=call_log,
            require_existing_job_store=False,
        )
        arguments = [
            "produce",
            input_value,
            "--recipe",
            "alltonote.document-note@1",
            "--workspace",
            str(workspace_root),
            "--detach",
            "--json",
        ]
    elif recipe_kind == "video-local":
        from tests.integration.test_local_media_golden_path import _create_runtime

        registry = WorkspaceInstanceRegistry(
            paths.workspace_registry_parent,
            inspect_workspace=lambda root: open_workspace(
                root, writable=False
            ).manifest.workspace_id,
        )
        instance = registry.resolve(workspace_root)
        runtime = _create_runtime(
            instance.machine_root,
            workspace_instance_id=instance.instance_id,
        )
        arguments = [
            "produce",
            "video",
            "--input",
            input_value,
            "--workspace",
            str(workspace_root),
            "--detach",
            "--json",
        ]
    else:
        from app.runtime import create_fake_runtime_for_workspace

        runtime = create_fake_runtime_for_workspace(
            workspace_root,
            local_app_data=local_data_parent,
        )
        arguments = [
            "produce",
            "video",
            "--input",
            input_value,
            "--workspace",
            str(workspace_root),
            "--detach",
            "--json",
        ]
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(
            arguments,
            runtime=runtime,
            config_service=RuntimeConfigService(paths=paths, environ={}),
            engine_client=LocalEngineClient(paths),
        )
    results.put((exit_code, stdout.getvalue(), stderr.getvalue(), os.getpid()))


def _run_job_wait(
    workspace_root: Path,
    local_data_parent: Path,
    job_id: str,
    timeout_seconds: float,
    results,
) -> None:
    paths = resolve_runtime_paths(local_data_parent=local_data_parent)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(
            [
                "job",
                "wait",
                job_id,
                "--workspace",
                str(workspace_root),
                "--timeout",
                str(timeout_seconds),
                "--json",
            ],
            config_service=RuntimeConfigService(paths=paths, environ={}),
        )
    results.put((exit_code, stdout.getvalue(), stderr.getvalue(), os.getpid()))


@pytest.mark.parametrize(
    ("recipe_kind", "expected_operations"),
    (
        ("video", ("download", "transcribe", "model", "portable_commit")),
        (
            "video-local",
            (
                "source_resolve",
                "source_acquire",
                "transcriber",
                "model",
                "portable_commit",
            ),
        ),
        (
            "document",
            (
                "parse_document",
                "document-knowledge-compose",
                "document-knowledge-verify",
            ),
        ),
    ),
)
def test_detached_recipe_survives_submitter_exit_and_finishes_in_engine_worker(
    tmp_path: Path,
    recipe_kind: str,
    expected_operations: tuple[str, ...],
) -> None:
    workspace_root = tmp_path / "Workspace 有空格"
    shutil.copytree(FIXTURE_ROOT, workspace_root)
    local_data_parent = tmp_path / "machine state"
    local_data_parent.mkdir()
    paths = resolve_runtime_paths(local_data_parent=local_data_parent)
    worker_started = tmp_path / "worker-started"
    worker_release = tmp_path / "worker-release"
    worker_finished = tmp_path / "worker-finished"
    call_log = tmp_path / "worker-calls.jsonl"
    if recipe_kind == "document":
        source = tmp_path / "detached document.pdf"
        source.write_bytes(b"%PDF-1.7\nfixture\n")
        input_value = str(source)
    elif recipe_kind == "video-local":
        source = tmp_path / "detached local course.mp4"
        source.write_bytes(b"detached local video fixture")
        input_value = str(source)
    else:
        input_value = "fixture://detached-cross-process"
    context = get_context("spawn")
    host = subprocess.Popen(
        (
            str(Path(sys.executable).resolve()),
            "-I",
            "-B",
            str(PROCESS_HELPER.resolve()),
            "host",
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
            "--worker-started",
            str(worker_started),
            "--worker-release",
            str(worker_release),
            "--worker-finished",
            str(worker_finished),
            "--call-log",
            str(call_log),
            "--recipe-kind",
            recipe_kind,
        ),
        cwd=Path(__file__).parents[2],
        env=minimal_worker_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    submit_results = context.Queue()
    probe_results = context.Queue()
    wait_results = context.Queue()
    client = LocalEngineClient(
        paths,
        startup_timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
        shutdown_timeout_seconds=10.0,
    )
    submitter = None
    probe = None
    observer = None
    host_stopped = False

    try:
        deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not client.status().running:
            time.sleep(0.02)
        status = client.status()
        assert status.running is True

        submitter = context.Process(
            target=_run_detached_submit,
            args=(
                workspace_root,
                local_data_parent,
                recipe_kind,
                input_value,
                call_log,
                submit_results,
            ),
        )
        submitter.start()
        submitter.join(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert submitter.exitcode == 0
        submit_code, submit_stdout, submit_stderr, submit_pid = submit_results.get(
            timeout=2
        )
        submission = json.loads(submit_stdout)
        job_id = submission["data"]["job_id"]
        assert submit_code == 0
        assert submit_stderr == ""
        assert submit_stdout.count("\n") == 1
        assert submission["data"]["state"] == "queued"

        _wait_for_file(
            worker_started,
            timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
        )
        worker_pid = int(worker_started.read_text(encoding="ascii"))
        assert len({host.pid, submit_pid, worker_pid}) == 3
        assert submitter.is_alive() is False
        if recipe_kind in {"document", "video-local"}:
            source.unlink()

        probe = context.Process(
            target=_run_job_wait,
            args=(workspace_root, local_data_parent, job_id, 0.1, probe_results),
        )
        probe.start()
        probe.join(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert probe.exitcode == 0
        probe_code, probe_stdout, probe_stderr, probe_pid = probe_results.get(
            timeout=5
        )
        probed = json.loads(probe_stdout)
        assert probe_code == 30
        assert probe_stderr == ""
        assert probe_stdout.count("\n") == 1
        assert probed["error"]["code"] == "job_wait_timeout"
        assert probe_pid not in {host.pid, submit_pid, worker_pid}
        assert not call_log.exists()

        registry = WorkspaceInstanceRegistry(
            paths.workspace_registry_parent,
            inspect_workspace=lambda root: open_workspace(
                root, writable=False
            ).manifest.workspace_id,
        )
        instance = registry.resolve(workspace_root)
        repository = open_engine_job_store(paths, instance)
        assert repository.get_job(job_id).state.value == "running"
        assert repository.list_attempts(job_id) == ()

        worker_release.write_text("continue", encoding="ascii")
        observer = context.Process(
            target=_run_job_wait,
            args=(
                workspace_root,
                local_data_parent,
                job_id,
                _PROCESS_TIMEOUT_SECONDS,
                wait_results,
            ),
        )
        observer.start()
        observer.join(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert observer.exitcode == 0
        wait_code, wait_stdout, wait_stderr, observer_pid = wait_results.get(
            timeout=2
        )
        observed = json.loads(wait_stdout)
        assert wait_code == 0, json.dumps(observed, ensure_ascii=False)
        assert wait_stderr == ""
        assert wait_stdout.count("\n") == 1
        assert observed["data"]["job"]["job_id"] == job_id
        assert observed["data"]["job"]["state"] == "succeeded"
        assert observer_pid not in {host.pid, submit_pid, worker_pid, probe_pid}

        persisted = repository.get_job(job_id)
        assert persisted.execution_owner is JobExecutionOwner.ENGINE
        assert persisted.state.value == "succeeded"
        attempts = repository.list_attempts(job_id)
        assert len({attempt.step_id for attempt in attempts}) == len(attempts)
        stage_payloads = tuple(
            json.loads(event.payload_json)
            for event in repository.list_events(job_id)
            if event.event_type == "stage.changed.v1"
        )
        assert len(stage_payloads) == len(attempts) * 3
        for attempt in attempts:
            payloads = tuple(
                payload
                for payload in stage_payloads
                if payload["attempt_id"] == attempt.attempt_id
            )
            assert [payload["state"] for payload in payloads] == [
                "pending",
                "running",
                "succeeded",
            ]
            assert {payload["stage"] for payload in payloads} == {
                attempt.step_id
            }
            assert [payload["generation"] for payload in payloads] == [
                0,
                attempt.fencing_token,
                attempt.fencing_token,
            ]
        assert repository.list_events(job_id)[-1].payload_json == (
            '{"state":"succeeded"}'
        )

        operations = tuple(
            json.loads(line)["operation"]
            for line in call_log.read_text(encoding="utf-8").splitlines()
        )
        assert tuple(operations) == expected_operations
        assert len(tuple(workspace_root.rglob("commit.json"))) == 1

        _wait_for_file(worker_finished, timeout_seconds=_PROCESS_TIMEOUT_SECONDS)
        assert int(worker_finished.read_text(encoding="ascii")) == worker_pid
        stopped = client.stop()
        assert stopped.stopped is True
        assert host.wait(timeout=_PROCESS_TIMEOUT_SECONDS) == 0
        host_stopped = True
    finally:
        if worker_started.exists() and not worker_release.exists():
            worker_release.write_text("continue", encoding="ascii")
        for process in (submitter, probe, observer):
            if process is None:
                continue
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        if not host_stopped:
            if worker_started.exists() and not worker_finished.exists():
                try:
                    _wait_for_file(worker_finished, timeout_seconds=5)
                except AssertionError:
                    pass
            try:
                client.stop()
            except Exception:
                pass
            try:
                host.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host.terminate()
                host.wait(timeout=5)
        for results in (submit_results, probe_results, wait_results):
            results.close()
            results.join_thread()


@pytest.mark.parametrize(
    ("recipe_kind", "expected_operations"),
    (
        (
            "video-local",
            (
                "source_resolve",
                "source_acquire",
                "transcriber",
                "model",
                "portable_commit",
            ),
        ),
        (
            "document",
            (
                "parse_document",
                "document-knowledge-compose",
                "document-knowledge-verify",
            ),
        ),
    ),
)
def test_capacity_two_runs_real_workers_and_admits_third_after_release(
    tmp_path: Path,
    recipe_kind: str,
    expected_operations: tuple[str, ...],
) -> None:
    workspace_root = tmp_path / "Concurrent Workspace 有空格"
    shutil.copytree(FIXTURE_ROOT, workspace_root)
    local_data_parent = tmp_path / "machine state"
    local_data_parent.mkdir()
    paths = resolve_runtime_paths(local_data_parent=local_data_parent)
    control_root = tmp_path / "worker-control"
    started_root = control_root / "started"
    release_root = control_root / "release"
    finished_root = control_root / "finished"
    crash_once_root = control_root / "crash-once"
    for directory in (
        started_root,
        release_root,
        finished_root,
        crash_once_root,
    ):
        directory.mkdir(parents=True)
    call_log = tmp_path / "worker-calls.jsonl"
    sources = []
    for index in range(3):
        suffix = ".pdf" if recipe_kind == "document" else ".mp4"
        source = tmp_path / f"concurrent source {index}{suffix}"
        source.write_bytes(
            (b"%PDF-1.7\n" if recipe_kind == "document" else b"video\n")
            + f"fixture-{index}\n".encode("ascii")
        )
        sources.append(source)

    host = subprocess.Popen(
        (
            str(Path(sys.executable).resolve()),
            "-I",
            "-B",
            str(PROCESS_HELPER.resolve()),
            "host",
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
            "--worker-control-dir",
            str(control_root),
            "--call-log",
            str(call_log),
            "--recipe-kind",
            recipe_kind,
            "--maximum-active-workers",
            "2",
        ),
        cwd=Path(__file__).parents[2],
        env=minimal_worker_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    context = get_context("spawn")
    submit_results = context.Queue()
    client = LocalEngineClient(
        paths,
        startup_timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
        shutdown_timeout_seconds=10.0,
    )
    host_stopped = False
    job_ids: set[str] = set()

    try:
        deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not client.status().running:
            if host.poll() is not None:
                pytest.fail("capacity-2 Engine Host exited before startup")
            time.sleep(0.02)
        assert client.status().running is True

        for source in sources:
            _run_detached_submit(
                workspace_root,
                local_data_parent,
                recipe_kind,
                str(source),
                call_log,
                submit_results,
            )
        submitter_pids = {os.getpid()}
        for _ in sources:
            exit_code, stdout, stderr, process_id = submit_results.get(timeout=5)
            submission = json.loads(stdout)
            assert exit_code == 0
            assert stderr == ""
            assert stdout.count("\n") == 1
            assert submission["data"]["state"] in {"queued", "running"}
            assert process_id in submitter_pids
            job_ids.add(submission["data"]["job_id"])
        assert len(job_ids) == 3
        for source in sources:
            source.unlink()

        first_workers = _wait_for_control_files(
            started_root,
            expected_count=2,
            timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
        )
        time.sleep(0.2)
        assert len(tuple(started_root.iterdir())) == 2
        assert len(set(first_workers.values())) == 2
        assert host.pid not in first_workers.values()
        assert not (set(first_workers.values()) & submitter_pids)

        registry = WorkspaceInstanceRegistry(
            paths.workspace_registry_parent,
            inspect_workspace=lambda root: open_workspace(
                root, writable=False
            ).manifest.workspace_id,
        )
        instance = registry.resolve(workspace_root)
        repository = open_engine_job_store(paths, instance)
        waiting_job_id = (job_ids - set(first_workers)).pop()
        deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            waiting_events = tuple(
                event
                for event in repository.list_events(waiting_job_id)
                if event.event_type == "scheduler.waiting.v1"
            )
            if waiting_events:
                break
            time.sleep(0.02)
        assert len(waiting_events) == 1
        assert repository.get_job(waiting_job_id).state.value == "queued"
        assert repository.list_attempts(waiting_job_id) == ()
        assert repository.get_engine_worker_launch_count(waiting_job_id) == 0

        released_job_id = next(iter(first_workers))
        (crash_once_root / released_job_id).write_text("crash", encoding="ascii")
        (release_root / released_job_id).write_text("continue", encoding="ascii")
        _wait_for_file(
            finished_root / released_job_id,
            timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
        )
        all_workers = _wait_for_control_files(
            started_root,
            expected_count=3,
            timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
        )
        assert waiting_job_id in all_workers
        still_running_job_id = (set(first_workers) - {released_job_id}).pop()
        assert not (finished_root / still_running_job_id).exists()
        assert len(set(all_workers.values())) == 3

        for job_id in {still_running_job_id, waiting_job_id}:
            (release_root / job_id).write_text("continue", encoding="ascii")
        for job_id in job_ids:
            _wait_for_file(
                finished_root / job_id,
                timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
            )

        deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if all(
                repository.get_job(job_id).state.value == "succeeded"
                for job_id in job_ids
            ):
                break
            time.sleep(0.02)
        assert {
            repository.get_job(job_id).state.value for job_id in job_ids
        } == {"succeeded"}
        assert int(
            (started_root / released_job_id).read_text(encoding="ascii")
        ) != first_workers[released_job_id]
        admitted_events = tuple(
            event
            for event in repository.list_events(waiting_job_id)
            if event.event_type == "scheduler.admitted.v1"
        )
        assert len(admitted_events) == 1
        operations = tuple(
            json.loads(line)["operation"]
            for line in call_log.read_text(encoding="utf-8").splitlines()
        )
        for operation in expected_operations:
            assert operations.count(operation) == 3
        assert len(tuple(workspace_root.rglob("commit.json"))) == 3

        stopped = client.stop()
        assert stopped.stopped is True
        assert host.wait(timeout=_PROCESS_TIMEOUT_SECONDS) == 0
        host_stopped = True
    finally:
        if started_root.is_dir():
            for started in started_root.iterdir():
                release = release_root / started.name
                if not release.exists():
                    release.write_text("continue", encoding="ascii")
        if not host_stopped:
            try:
                client.stop()
            except Exception:
                pass
            try:
                host.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host.terminate()
                host.wait(timeout=5)
        submit_results.close()
        submit_results.join_thread()
