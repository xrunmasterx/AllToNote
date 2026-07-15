from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from iwiki.portable import PortableBundleRef, ValidationLevel, validate_bundle
from iwiki.workspace import open_workspace

from app.adapters.models.legacy_gpt import (
    LegacyModelBinding,
    LegacyModelCapabilities,
    LegacyModelResponse,
)
from app.adapters.screenshots.ffmpeg import FFmpegScreenshotAdapter
from app.adapters.sources.legacy_video import LegacyVideoSourceAdapter
from app.adapters.transcription.legacy_transcriber import LegacyTranscriberAdapter
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState, ScreenshotPolicy, VideoProduceRequest
from app.core.portable.webp import is_valid_webp
from app.core.ports.transcript import MediaInput
from app.runtime import create_local_video_runtime


BACKEND_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "video"
WORKSPACE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace-v2"
VIDEO_FIXTURE = FIXTURE_ROOT / "local-course.mp4"
VIDEO_PROVENANCE = FIXTURE_ROOT / "local-course.json"
EXPECTED_PHRASE = "AllToNote turns video into cited knowledge."
FIXTURE_SCHEMA = "urn:alltonote:test-fixture:local-video:v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
ALLTONOTE_WORDMARK = re.compile(
    r"(?<![A-Za-z0-9])all\s*to\s*note(?![A-Za-z0-9])",
    re.IGNORECASE,
)
WINDOWS_SKIP_REASON = "windows_smoke requires Windows"
MACOS_SKIP_REASON = "macos_smoke requires Apple Silicon macOS"
FFMPEG_ENV = "ALLTONOTE_SMOKE_FFMPEG"
MODEL_CACHE_ENV = "ALLTONOTE_SMOKE_MODEL_CACHE"


@dataclass(frozen=True)
class _SmokePrerequisites:
    ffmpeg: Path
    ffmpeg_version: str
    model_cache: Path


class _TimedTranscriber:
    def __init__(self, delegate: LegacyTranscriberAdapter) -> None:
        self._delegate = delegate
        self.elapsed_seconds: float | None = None
        self.transcript = None

    def transcribe(self, media: MediaInput, token: object) -> object:
        started = time.perf_counter()
        self.transcript = self._delegate.transcribe(media, token)
        self.elapsed_seconds = time.perf_counter() - started
        return self.transcript


class _SmokeCompletion:
    def complete_once(self, prompt: str) -> LegacyModelResponse:
        assert "seg_000001" in prompt
        return LegacyModelResponse(
            markdown=(
                "# Local video note\n\n"
                "AllToNote turns video into cited knowledge.[^seg_000001]\n\n"
                "[SCREENSHOT:seg_000001]\n"
            ),
            provider_request_id="local-smoke-request",
            input_tokens=20,
            output_tokens=12,
            actual_model="fixture/model-v1",
        )


def _validated_provenance() -> dict[str, object]:
    assert VIDEO_FIXTURE.is_file(), "project-controlled video fixture is missing"
    assert VIDEO_PROVENANCE.is_file(), "video fixture provenance is missing"

    payload = VIDEO_FIXTURE.read_bytes()
    provenance = json.loads(VIDEO_PROVENANCE.read_bytes())

    assert provenance["$schema"] == FIXTURE_SCHEMA
    assert provenance["fixture_schema_version"] == 1
    assert provenance["creation_tool"]
    assert provenance["voice"]
    assert provenance["source"]
    assert provenance["expected_phrase"] == EXPECTED_PHRASE
    assert 10_000 <= provenance["duration_ms"] <= 15_000
    assert provenance["license"] == "project_test_fixture"
    assert provenance["byte_length"] == len(payload)
    assert SHA256_PATTERN.fullmatch(provenance["sha256"])
    assert provenance["sha256"] == hashlib.sha256(payload).hexdigest()
    return provenance


def _explicit_marker_selected(pytestconfig: pytest.Config, marker: str) -> None:
    if pytestconfig.getoption("markexpr").strip() != marker:
        pytest.skip(f"{marker} must be explicitly selected with -m {marker}")


def _safe_existing_path(raw: str, *, executable: bool = False) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        pytest.fail("smoke prerequisite paths must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        pytest.fail("smoke prerequisite path is unavailable")
    if executable and not resolved.is_file():
        pytest.fail("smoke prerequisite path is not a direct regular file")
    if not executable and not resolved.is_dir():
        pytest.fail("smoke model cache must be a directory")
    return resolved


def _smoke_prerequisites() -> _SmokePrerequisites:
    raw_cache = os.environ.get(MODEL_CACHE_ENV)
    if not raw_cache:
        pytest.skip(f"real host smoke requires {MODEL_CACHE_ENV}")
    model_cache = _safe_existing_path(raw_cache)

    raw_ffmpeg = os.environ.get(FFMPEG_ENV)
    if raw_ffmpeg:
        ffmpeg = _safe_existing_path(raw_ffmpeg, executable=True)
    else:
        discovered = shutil.which("ffmpeg")
        if discovered is None:
            pytest.skip(f"real host smoke requires {FFMPEG_ENV} or ffmpeg on PATH")
        ffmpeg = _safe_existing_path(str(Path(discovered).resolve()), executable=True)

    completed = subprocess.run(
        [str(ffmpeg), "-version"],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail("configured FFmpeg is not loadable")
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    match = re.fullmatch(r"ffmpeg version (\S+).*", first_line)
    if match is None:
        pytest.fail("configured FFmpeg returned an unrecognized version")
    return _SmokePrerequisites(ffmpeg, match.group(1), model_cache)


def _model_cache_snapshot(root: Path) -> tuple[tuple[str, int, str], ...]:
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    snapshot = []
    for path in files:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        snapshot.append(
            (path.relative_to(root).as_posix(), path.stat().st_size, digest)
        )
    return tuple(snapshot)


def _private_model_cache(source: Path, private_root: Path) -> Path:
    private = private_root / "model-cache"
    shutil.copytree(source, private)
    return private


def _contains_alltonote_wordmark(transcript: str) -> bool:
    return ALLTONOTE_WORDMARK.search(transcript) is not None


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    shutil.copytree(WORKSPACE_FIXTURE, workspace)
    shutil.rmtree(workspace / "raw" / "personal" / ".staging")
    for relative in (
        "raw/common",
        "raw/personal/.staging",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    return workspace


def _run_real_local_video_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: dict[str, object],
    prerequisites: _SmokePrerequisites,
) -> None:
    source_cache_snapshot = _model_cache_snapshot(prerequisites.model_cache)
    private_model_cache = _private_model_cache(
        prerequisites.model_cache,
        tmp_path / "private-model",
    )
    whisper_module = importlib.import_module("app.transcriber.whisper")
    monkeypatch.setattr(
        whisper_module,
        "get_model_dir",
        lambda _subdir="whisper": str(private_model_cache),
    )
    transcriber = _TimedTranscriber(
        LegacyTranscriberAdapter(
            "fast-whisper",
            factories={
                "fast-whisper": lambda: whisper_module.WhisperTranscriber(
                    model_size="tiny",
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=1,
                )
            },
        )
    )
    model = LegacyModelBinding(
        provider_kind="fixture/provider-v1",
        model_identity="fixture/model-v1",
        bridge=_SmokeCompletion(),
        capabilities=LegacyModelCapabilities(screenshot_requests=True),
    )
    runtime = create_local_video_runtime(
        tmp_path / "machine",
        source=LegacyVideoSourceAdapter(local_machine_id="smoke-machine"),
        source_metadata={
            "local": {
                "title": "Local course smoke fixture",
                "author": "AllToNote",
                "channel": "Project test fixtures",
                "duration_ms": provenance["duration_ms"],
                "published_at": None,
                "observed_at": "2026-07-15T00:00:00.000Z",
                "language": "en",
            }
        },
        transcriber=transcriber,
        model=model,
        screenshot_adapter_factory=lambda storage, repository: FFmpegScreenshotAdapter(
            storage,
            repository,
            ffmpeg_executable=str(prerequisites.ffmpeg),
        ),
    )
    workspace_root = _workspace(tmp_path)
    submitted = runtime.submit_video(
        VideoProduceRequest(
            request_schema_version=1,
            workspace_root=workspace_root,
            input_value=str(VIDEO_FIXTURE),
            client_request_id="local-real-smoke",
            screenshot_policy=ScreenshotPolicy.ON_DEMAND,
        )
    )

    snapshot = runtime.wait_job(submitted.job_id)

    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result is not None
    transcript = transcriber.transcript
    assert transcript is not None
    assert transcript.segments
    assert [segment.start_ms for segment in transcript.segments] == sorted(
        segment.start_ms for segment in transcript.segments
    )
    transcript_text = " ".join(segment.text for segment in transcript.segments).casefold()
    assert _contains_alltonote_wordmark(transcript_text)
    assert "video" in transcript_text
    assert "knowledge" in transcript_text
    assert transcriber.elapsed_seconds is not None

    result = snapshot.result
    assert len(result.display_asset_ids) == 1
    bundle = workspace_root / result.workspace_relative_bundle_path
    committed_manifests = tuple(workspace_root.rglob("bundle.json"))
    assert committed_manifests == (bundle / "bundle.json",)
    manifest = json.loads((bundle / "bundle.json").read_bytes())
    display_assets = [
        artifact
        for artifact in manifest["artifacts"]
        if artifact["artifact_id"] == result.display_asset_ids[0]
    ]
    assert len(display_assets) == 1
    asset_relative_path = display_assets[0]["payload"]["path"]
    asset_bytes = (bundle / asset_relative_path).read_bytes()
    assert is_valid_webp(asset_bytes)
    draft_bytes = (
        bundle / "drafts" / f"{result.primary_draft_artifact_id}.md"
    ).read_bytes()
    expected_link = (
        f"![Video screenshot 1 at 00:00.000](../{asset_relative_path})\n".encode()
    )
    assert draft_bytes.count(expected_link) == 1
    quality = json.loads(
        (
            bundle
            / "quality"
            / f"{result.quality_report_artifact_id}.json"
        ).read_bytes()
    )
    assert quality["subject"]["sha256"] == sha256_digest(draft_bytes)
    workspace = open_workspace(workspace_root, writable=True)
    validation = validate_bundle(
        workspace,
        PortableBundleRef.committed(result.bundle_id),
        ValidationLevel.SEMANTIC,
    )
    assert validation.valid
    assert validation.bundle_id == result.bundle_id
    assert validation.issues == ()
    assert _model_cache_snapshot(prerequisites.model_cache) == source_cache_snapshot
    print(
        json.dumps(
            {
                "bundle_id": result.bundle_id,
                "compute_type": "int8",
                "device": "cpu",
                "ffmpeg_version": prerequisites.ffmpeg_version,
                "fixture_sha256": provenance["sha256"],
                "model": "tiny",
                "source_cache_unchanged": True,
                "transcription_elapsed_seconds": round(
                    transcriber.elapsed_seconds, 3
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def test_host_smoke_markers_are_registered() -> None:
    project = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text("utf-8"))
    markers = project["tool"]["pytest"]["ini_options"]["markers"]

    assert any(value.startswith("windows_smoke:") for value in markers)
    assert any(value.startswith("macos_smoke:") for value in markers)


def test_local_course_fixture_matches_committed_provenance() -> None:
    _validated_provenance()


def test_private_model_cache_is_detached_from_caller_source(tmp_path: Path) -> None:
    source = tmp_path / "source-cache"
    (source / "models--fixture" / "snapshots" / "one").mkdir(parents=True)
    source_file = source / "models--fixture" / "snapshots" / "one" / "model.bin"
    source_file.write_bytes(b"caller-owned-model")
    helper = globals().get("_private_model_cache")
    assert callable(helper), "private model cache helper is missing"

    private = helper(source, tmp_path / "smoke")
    private_file = private / source_file.relative_to(source)
    private_file.write_bytes(b"modified-private-copy")
    shutil.rmtree(private)

    assert source_file.read_bytes() == b"caller-owned-model"


@pytest.mark.parametrize(
    ("expression", "marker", "selected"),
    (
        ("windows_smoke", "windows_smoke", True),
        ("macos_smoke", "macos_smoke", True),
        ("windows_smoke and not slow", "windows_smoke", False),
        ("windows_smoke_disabled", "windows_smoke", False),
        ("macos_smoke_backup", "macos_smoke", False),
        ("not guard(label='windows_smoke')", "windows_smoke", False),
    ),
)
def test_explicit_marker_selection_requires_complete_identifier(
    expression: str,
    marker: str,
    selected: bool,
) -> None:
    class _Config:
        def getoption(self, name: str) -> str:
            assert name == "markexpr"
            return expression

    if selected:
        _explicit_marker_selected(_Config(), marker)  # type: ignore[arg-type]
    else:
        with pytest.raises(pytest.skip.Exception):
            _explicit_marker_selected(_Config(), marker)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("transcript", "contains_wordmark"),
    (
        ("AllToNote turns video into knowledge.", True),
        ("all to note turns video into knowledge.", True),
        ("small to note turns video into knowledge.", False),
    ),
)
def test_alltonote_wordmark_match_has_narrow_boundaries(
    transcript: str,
    contains_wordmark: bool,
) -> None:
    helper = globals().get("_contains_alltonote_wordmark")
    assert callable(helper), "AllToNote wordmark helper is missing"

    assert helper(transcript) is contains_wordmark


def test_safe_existing_path_resolves_absolute_directory_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "cache"
    target.mkdir()
    link = tmp_path / "cache-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")

    assert _safe_existing_path(str(link)) == target.resolve(strict=True)


@pytest.mark.windows_smoke
def test_windows_real_local_video_smoke(
    pytestconfig: pytest.Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance = _validated_provenance()
    if platform.system() != "Windows":
        pytest.skip(WINDOWS_SKIP_REASON)
    _explicit_marker_selected(pytestconfig, "windows_smoke")
    prerequisites = _smoke_prerequisites()
    _run_real_local_video_smoke(tmp_path, monkeypatch, provenance, prerequisites)


@pytest.mark.macos_smoke
def test_macos_real_local_video_smoke(
    pytestconfig: pytest.Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance = _validated_provenance()
    if platform.system() != "Darwin" or platform.machine().lower() not in {
        "arm64",
        "aarch64",
    }:
        pytest.skip(MACOS_SKIP_REASON)
    _explicit_marker_selected(pytestconfig, "macos_smoke")
    prerequisites = _smoke_prerequisites()
    _run_real_local_video_smoke(tmp_path, monkeypatch, provenance, prerequisites)
