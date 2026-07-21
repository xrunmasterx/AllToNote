# AllToNote Video Producer Implementation Plan

> **Status:** Partially superseded. 已完成的 Video 实现与验收记录继续有效；其中 generic `run`/`add` 接口任务已由单一 `produce`、X0-A/X0-B 和当前 handoff 取代，不得重新执行。当前平台接缝以 Recipe X0-A spec/tasks 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一个不依赖 Desktop/Web 的 AllToNote Video Producer，使 Bilibili、YouTube 和本地视频可以通过 CLI 生成带 Transcript、Evidence、Markdown Draft、QualityReport 与 Receipt 的 Portable Source Bundle，并由 iwiki 原子提交。

**Architecture:** 在现有 `backend/` 中新增独立 Core/Application/Ports，用 Legacy Adapter 包装当前 Downloader、Transcriber 和 GPT，实现前台 CLI 加持久 Job/Step Attempt。AllToNote 只组装候选 Bundle，最终语义校验和原子提交由固定版本的 iwiki 公共 SDK 完成；FastAPI 最后通过兼容 Adapter 接入相同 Application Facade。

**Tech Stack:** Python 3.11、stdlib `argparse/dataclasses/enum/hashlib/json/sqlite3/tomllib/pathlib/subprocess/threading`、`filelock`、`keyring`、`platformdirs`、`tomli-w`、现有 FastAPI/Pydantic/yt-dlp/Faster Whisper/OpenAI-compatible 实现、llm-iwiki SDK 0.1.0 Portable API v1、pytest。

## Global Constraints

- 规范来源：`docs/superpowers/specs/2026-07-14-alltonote-video-producer-design.md`，确认提交 `4ba05b4`。
- iwiki 实现锁：提交 `8701ace4f65ffd7ee46fbcf3edcc2ce2bcfc47e1`、package `llm-iwiki==0.1.0`、SDK API `1`、contract `iwiki-portable-contract-v1`、schema set `2026-07-portable-v1`、schema SHA-256 `sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9`。
- 实现开始时先使用 `superpowers:using-git-worktrees` 创建 `codex/alltonote-video-producer` 隔离 worktree；不得在当前含 `.superpowers/` 和 `AGENTS.md` 的工作树直接实现。
- 用户已经选择 Subagent-Driven Development；每个任务使用全新实现子代理，并依次通过规格符合性审查和代码质量审查。
- 当前规划环境运行 `backend/python -m pytest -q` 因缺少 FastAPI/Pydantic Core 出现 7 个收集错误；任务 1 必须在隔离 venv 安装完整依赖并取得真实 exit 0 基线，不能把环境错误当产品回归。
- Task 1 之后的每条 Python 命令都必须显式调用隔离 worktree 的 `.venv\Scripts\python.exe`（macOS 为 `.venv/bin/python`），不能依赖上一个终端的 activation。计划中同一 `Run:` 行以分号展示的命令必须作为独立 shell 调用依次执行，并在任一非零退出时停止；PowerShell 的 `;` 不是 fail-fast 机制。
- 所有任务严格 TDD：先写精确失败测试并观察正确红态，再写最小实现；每个任务单独 commit。
- 每次 commit 前运行本任务聚焦测试、`python -m pytest -q` 和 `git diff --check`；若完整回归耗时超过 5 分钟，仍需在每个里程碑 V1–V7 末执行。
- Core Domain/Application 不导入 FastAPI、SQLAlchemy、CLI parser、旧 DAO 或 iwiki 私有模块。
- CLI 不调用 `NoteGenerator`，新 Core 不读取旧 `bili_note.db`。
- 机器协议统一使用 `workspace_root` / `--workspace`；不在内部 DTO 中把 Workspace Root 命名为 Vault。
- Job 终态不可复活；`failed/cancelled` 的再次生产创建带 `retry_of_job_id` 的新 Job。
- Attempt 是 Step Attempt；旧 Attempt 不回到 running。
- P0 只有前台 `--wait`，不实现 daemon、`--detach`、Desktop Producer、MCP、Publisher 或新来源 Recipe。
- Screenshot 默认关闭；平台字幕可用且截图关闭时严禁下载媒体、启动 FFmpeg 或加载 Whisper。
- 当前 iwiki `supported_required_contracts` 为空，P0 视频元数据 Artifact 使用核心 `source.metadata.v1`，并以 `source_kind=video` 和 namespaced `extensions.alltonote` 表达视频字段；不写未注册的 `source.metadata.video.v1`。
- Transcript 使用 `evidence.transcript.v1` NDJSON，整数毫秒半开区间，无第二份权威 `full_text`。
- Draft 使用 `knowledge.draft.markdown.v1`，引用最终 Evidence ID；QualityReport 精确绑定 Draft hash。
- Candidate 不含 `commit.json`；最终文件变更只能通过公开 `prepare_bundle_commit` / `commit_prepared_bundle`。
- Quality `fail` 可以 commit，但返回 `publish_eligible=false`；Bundle invalid 禁止 commit。
- API Key、Cookie、Authorization、完整 Prompt、provider raw、绝对本机路径不得进入命令行、日志、事件、Receipt 或 Bundle。
- Windows 11 x64 是发布阻断 Gate；macOS Apple Silicon 是独立 Tier 2 Gate。

---

## File Responsibility Map

### Runtime 与公共入口

- `backend/pyproject.toml` — Runtime package、console script、基础/Video/Web/Dev extras。
- `backend/app/runtime-lock.json` — 作为 package data 固定 iwiki package/API/contract/schema fingerprint，由 `importlib.resources` 读取。
- `backend/app/__init__.py` — 延迟导入 FastAPI，确保基础 CLI 不加载 Web。
- `backend/app/cli/main.py` — argparse、stdout/stderr、exit code；不含业务分支。
- `backend/app/runtime.py` — 官方 composition root，组装 Facade 与 Adapter。

### Core

- `backend/app/core/errors.py` — stable category/code/retryability/next action。
- `backend/app/core/domain/ids.py` — UUIDv7 typed ID、RFC3339 UTC、digest。
- `backend/app/core/domain/video.py` — Video Request、Source、Transcript、Draft、Evidence、Quality、Result DTO。
- `backend/app/core/jobs/model.py` — Job、Step、Attempt、Challenge、ExternalOperation、Event。
- `backend/app/core/jobs/state_machine.py` — 合法状态转换。
- `backend/app/core/jobs/recovery.py` — checkpoint 验证与剩余 Step 计划。
- `backend/app/core/application/job_service.py` — submit/get/list/wait/cancel/respond/retry。
- `backend/app/core/application/video_service.py` — Recipe orchestration 与 commit finalizer。
- `backend/app/core/sdk.py` — 官方进程内 Facade。
- `backend/app/core/ports/*.py` — Source、Transcript、Model、Screenshot、Job、Storage、Credential、Portable、Clock/ID/Event/Cancellation。
- `backend/app/core/config/*.py` — 严格 TOML schema、合并和 effective config。
- `backend/app/core/portable/*.py` — 精确 JSON/JSONL、Artifact/Evidence/Quality、Bundle assembly。
- `backend/app/core/recipes/video/*.py` — Recipe contract、prompt、citation parser、quality profile。

### Adapter

- `backend/app/adapters/jobs/sqlite_repository.py` — per-local-workspace JobStore、事务、lease、commit guard。
- `backend/app/adapters/jobs/file_attempt_storage.py` — Attempt workspace、checkpoint、event journal。
- `backend/app/adapters/credentials/keyring_broker.py` — OS Credential Store 与 env override。
- `backend/app/adapters/iwiki/portable_gateway.py` — 公开 SDK Adapter。
- `backend/app/adapters/sources/legacy_video.py` — Legacy Downloader wrapper/registry。
- `backend/app/adapters/transcription/legacy_transcriber.py` — Legacy Transcript wrapper/normalizer。
- `backend/app/adapters/models/legacy_gpt.py` — Legacy GPT wrapper、segment citation protocol、ExternalOperation。
- `backend/app/adapters/screenshots/ffmpeg.py` — 安全 FFmpeg WebP 提取。
- `backend/app/adapters/legacy/fastapi_bridge.py` — 旧 request/task/result projection。

### 测试

- `backend/tests/core/` — Domain、Job、Checkpoint、Portable、Quality 单元测试。
- `backend/tests/adapters/` — Port contract、Credential、iwiki、Source、Transcriber、Model、FFmpeg。
- `backend/tests/integration/` — Fake vertical slice、Bilibili/YouTube、本地视频、crash/cancel/idempotency。
- `backend/tests/cli/` — JSON/JSONL、stdout/stderr、exit code、lazy import、doctor/job commands。
- `backend/tests/fixtures/` — Workspace v2、自制短视频、字幕/metadata 响应、黄金 Draft。
- `backend/tests/conftest.py` — deterministic clock/ID、temp machine state/workspace fixtures。

## Interfaces Frozen by This Plan

```python
@dataclass(frozen=True)
class VideoProduceRequest:
    request_schema_version: int
    workspace_root: Path
    input_value: str
    recipe_id: str = "alltonote.video-course-note"
    recipe_version: int = 1
    provider_profile: str = "default"
    model_override: str | None = None
    transcriber_profile: str = "default"
    output_language: str = "zh-CN"
    quality_preset: str = "balanced"
    style: str = "structured"
    screenshot_policy: ScreenshotPolicy = ScreenshotPolicy.OFF
    client_request_id: str | None = None
    principal: str = "local-user"
    provided_transcript: TranscriptDocument | None = None

@dataclass(frozen=True)
class TranscriptSegment:
    segment_id: str
    start_ms: int
    end_ms: int
    text: str

@dataclass(frozen=True)
class TranscriptDocument:
    language: str
    segments: tuple[TranscriptSegment, ...]

@dataclass(frozen=True)
class ScreenshotRequest:
    segment_id: str
    offset_ms: int = 0

@dataclass(frozen=True)
class GeneratedVideoDraft:
    markdown: str
    cited_segment_ids: tuple[str, ...]
    screenshot_requests: tuple[ScreenshotRequest, ...]
    model_identity: str
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]

@dataclass(frozen=True)
class VideoProduceResult:
    job_id: str
    run_id: str
    bundle_id: str
    manifest_sha256: str
    commit_sha256: str
    workspace_relative_bundle_path: str
    source_id: str
    source_revision_id: str
    primary_draft_artifact_id: str
    transcript_artifact_id: str
    evidence_set_artifact_id: str
    quality_report_artifact_id: str
    display_asset_ids: tuple[str, ...]
    quality_overall: QualityOverall
    publish_eligible: bool
    usage: Mapping[str, int | float | str]
    warnings: tuple[str, ...]
    idempotent: bool

@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    state: JobState
    active_attempt_id: str | None
    challenge_id: str | None
    retry_of_job_id: str | None
    result: VideoProduceResult | None
    error: ErrorDetail | None

@dataclass(frozen=True)
class RetryJobRequest:
    retry_request_schema_version: int
    client_request_id: str
    expected_original_job_state: JobState
    confirmed_unknown_operation_ids: tuple[str, ...] = ()
```

`ScreenshotPolicy` has only `OFF` and `ON_DEMAND` in P0; CLI `--screenshots` maps to `ON_DEMAND`. `EventSink` is `Callable[[JobEvent], None]`. `AllToNoteRuntime` exposes these fixed signatures: `submit_video(VideoProduceRequest) -> JobSnapshot`, `wait_job(job_id, event_sink=None) -> JobSnapshot`, `get_job(job_id) -> JobSnapshot`, `list_jobs(query) -> tuple[JobSnapshot, ...]`, `stream_job_events(job_id, after_sequence, follow, event_sink) -> JobSnapshot`, `cancel_job(job_id) -> JobSnapshot`, `respond_job(job_id, challenge_id, response) -> JobSnapshot`, `retry_job(job_id, RetryJobRequest) -> JobSnapshot`, `doctor(request) -> DoctorReport`, `list_recipes() -> tuple[RecipeDescriptor, ...]`, and `describe_recipe(recipe_id, recipe_version) -> RecipeDescriptor`. Typed runtime IDs use `job_`, `run_`, `att_`, `evt_`, `chl_`, `corr_`, and `op_`; Portable IDs retain the iwiki prefixes such as `bnd_`, `src_`, `rev_`, `art_`, and `ev_`.

---

## Task 1: Establish the Isolated Baseline and Lazy Runtime Package

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/runtime-lock.json`
- Create: `backend/app/cli/__init__.py`
- Create: `backend/app/cli/main.py`
- Modify: `backend/app/__init__.py`
- Create: `backend/tests/helpers/report_cli_imports.py`
- Create: `backend/tests/cli/test_runtime_bootstrap.py`

**Interfaces:** Produces console script `alltonote`, `main(argv: Sequence[str] | None = None) -> int`, runtime version `0.1.0`.

- [ ] **Step 1: Create worktree, venv, install legacy dependencies and pinned iwiki wheel**

```powershell
git worktree add ..\AllToNote-video-producer -b codex/alltonote-video-producer
if ($LASTEXITCODE -ne 0) { throw 'git worktree add failed' }
py -3.11 -m venv ..\AllToNote-video-producer\.venv
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
$python = Resolve-Path ..\AllToNote-video-producer\.venv\Scripts\python.exe
$iwikiWheelhouse = Join-Path $env:TEMP 'alltonote-wheelhouse\iwiki'
New-Item -ItemType Directory -Force -Path $iwikiWheelhouse | Out-Null
& $python -m pip install --upgrade pip build
if ($LASTEXITCODE -ne 0) { throw 'bootstrap dependency install failed' }
& $python -m build --wheel --outdir $iwikiWheelhouse E:\Agent_Learning\.worktrees\llm-iwiki\iwiki-portable-contract-v1
if ($LASTEXITCODE -ne 0) { throw 'iwiki wheel build failed' }
& $python -m pip install -r ..\AllToNote-video-producer\backend\requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'legacy dependency install failed' }
& $python -m pip install (Join-Path $iwikiWheelhouse 'llm_iwiki-0.1.0-py3-none-any.whl') pytest filelock keyring platformdirs tomli-w
if ($LASTEXITCODE -ne 0) { throw 'runtime dependency install failed' }
Push-Location ..\AllToNote-video-producer\backend
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'isolated baseline failed' }
Pop-Location
```

Expected: all commands exit 0; the isolated baseline has zero collection errors and exits 0 before production edits. If the baseline still fails, stop Task 1 and diagnose that failure before writing the first production line.

- [ ] **Step 2: Write failing lazy-import/CLI tests**

```python
def test_cli_version_does_not_import_web_or_video_modules(monkeypatch):
    result = subprocess.run(
        [sys.executable, str(HELPER), "version", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert report["exit_code"] == 0
    assert not {"fastapi", "torch", "faster_whisper", "app.services.note"} & set(report["imported_modules"])
```

Run from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/cli/test_runtime_bootstrap.py -q`
Expected: FAIL because `app.cli.main` does not exist.

- [ ] **Step 3: Add package metadata, lock and lazy app import**

```toml
[project]
name = "alltonote-runtime"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["filelock>=3.18,<4", "keyring>=25,<26", "llm-iwiki==0.1.0", "platformdirs>=4.3,<5", "tomli-w>=1.2,<2"]

[project.scripts]
alltonote = "app.cli.main:entrypoint"

[build-system]
requires = ["setuptools>=75", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["."]
include = ["app*"]

[tool.setuptools.package-data]
app = ["runtime-lock.json"]
```

Write `app/runtime-lock.json` exactly as follows and add a wheel-install test that reads it with `importlib.resources.files("app").joinpath("runtime-lock.json")`. In `app/__init__.py`, add `from __future__ import annotations`, import `FastAPI` only under `TYPE_CHECKING`, and import the runtime class inside `create_app`; this preserves the `-> FastAPI` annotation without importing FastAPI or raising `NameError` at module load. `version --json` must emit only `{"alltonote_cli_protocol_version":1,"ok":true,"data":{"runtime_version":"0.1.0"}}` to stdout.

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(lifespan) -> FastAPI:
    from fastapi import FastAPI
    # Keep the existing router-registration body unchanged below this point.
```

```json
{
  "iwiki_package": "llm-iwiki==0.1.0",
  "portable_api_version": 1,
  "portable_contract_id": "iwiki-portable-contract-v1",
  "schema_set_id": "2026-07-portable-v1",
  "schema_sha256": "sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9",
  "source_commit": "8701ace4f65ffd7ee46fbcf3edcc2ce2bcfc47e1"
}
```

After these files exist, install the package from the worktree root before Step 4:

```powershell
& .\.venv\Scripts\python.exe -m pip install --no-deps -e .\backend
if ($LASTEXITCODE -ne 0) { throw 'editable runtime install failed' }
```

- [ ] **Step 4: Verify focused and full baseline**

Run from the worktree root as three separate shell invocations: `.\.venv\Scripts\python.exe -m pytest backend/tests/cli/test_runtime_bootstrap.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, and `git diff --check`.
Expected: all exit 0.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/app/runtime-lock.json backend/app/__init__.py backend/app/cli backend/tests/helpers/report_cli_imports.py backend/tests/cli
git commit -m "build: add lazy alltonote runtime entrypoint"
```

## Task 2: Freeze Core IDs, Errors, Video DTOs, and Ports

**Files:** Create `backend/app/core/errors.py`, `backend/app/core/domain/{ids,video}.py`, `backend/app/core/ports/{source,transcript,model,screenshot,portable,credentials,jobs,events}.py`; test `backend/tests/core/test_domain_contracts.py`.

**Interfaces:** Produces typed UUIDv7 IDs, `VideoProduceRequest`, `TranscriptDocument`, `GeneratedVideoDraft`, `DomainError`, all Port Protocols used below.

- [ ] **Step 1: Write failing contract tests**

```python
def test_typed_id_is_uuid7_and_stable_prefix():
    value = new_typed_id("bnd", now_ms=1_721_000_000_000, randomness=b"\x00" * 10)
    assert value.startswith("bnd_")
    assert UUID(value[4:]).version == 7

def test_transcript_rejects_invalid_half_open_range():
    with pytest.raises(DomainError, match="transcript_segment_invalid"):
        TranscriptSegment("seg_000001", 100, 100, "text")
```

Run from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/core/test_domain_contracts.py -q`; expected missing-module FAIL.

- [ ] **Step 2: Implement immutable DTOs and stable errors**

```python
class ErrorCategory(StrEnum):
    INVALID_REQUEST = "invalid_request"
    WORKSPACE_INCOMPATIBLE = "workspace_incompatible"
    CONFLICT = "conflict"
    RETRYABLE_RUNTIME = "retryable_runtime"
    POLICY_DENIED = "policy_denied"
    RECIPE_FAILED = "recipe_failed"
    CANCELLED = "cancelled"
    INTERNAL = "internal"
```

Implement RFC 9562 UUIDv7 bit layout, lowercase `sha256:` digest, UTC millisecond timestamps, frozen dataclasses and Protocols. No Pydantic imports.

- [ ] **Step 3: Verify**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/core/test_domain_contracts.py -q`, then `.\.venv\Scripts\python.exe -m pytest backend -q`; expected both pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/core backend/tests/core/test_domain_contracts.py
git commit -m "feat: define video producer core contracts"
```

## Task 3: Add Strict Runtime Config and CredentialBroker

**Files:** Create `backend/app/core/config/{model,loader}.py`, `backend/app/adapters/credentials/{profile_catalog,keyring_broker}.py`, tests `backend/tests/core/test_runtime_config.py`, `backend/tests/adapters/test_credential_broker.py`.

**Interfaces:** `load_runtime_config(path, environ) -> RuntimeConfig`; `CredentialBroker.resolve(profile) -> SecretValue`; `CredentialProfileCatalog.list_profiles() -> tuple[CredentialProfileMetadata, ...]`; Secrets never appear in repr or the catalog.

- [ ] **Step 1: Write failing tests**

```python
def test_unknown_config_key_fails_closed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('config_version=1\nunknown=true\n', encoding="utf-8")
    with pytest.raises(DomainError, match="config_unknown_key"):
        load_runtime_config(path, {})

def test_environment_secret_wins_and_repr_is_redacted(monkeypatch):
    monkeypatch.setenv("ALLTONOTE_CREDENTIAL_OPENAI_MAIN", "secret-value")
    value = broker.resolve("providers/openai-main")
    assert value.reveal() == "secret-value"
    assert "secret-value" not in repr(value)
```

Run from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/core/test_runtime_config.py backend/tests/adapters/test_credential_broker.py -q`
Expected: FAIL because the config and credential modules do not exist.

- [ ] **Step 2: Implement config schema and keyring adapter**

Use `tomllib` for read, `tomli_w` for locked atomic write and `platformdirs.user_config_path("AllToNote")`. Resolve non-Secrets in the exact order CLI override → environment → Runtime TOML → Recipe v1 default. Resolve Secrets in the exact order normalized process environment name → OS keyring → explicitly imported Legacy credential. Because Python keyring cannot enumerate credentials portably, maintain a separate locked/atomic non-Secret profile catalog containing only profile ID, provider/service kind and timestamps; `credentials set/delete` updates it only after keyring succeeds, and `credentials list` never attempts to enumerate Secret values. Reject Secret fields and unknown keys in TOML; reject a higher config major version.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/core/test_runtime_config.py backend/tests/adapters/test_credential_broker.py -q`, then `.\.venv\Scripts\python.exe -m pytest backend -q`.

```bash
git add backend/app/core/config backend/app/adapters/credentials/profile_catalog.py backend/app/adapters/credentials/keyring_broker.py backend/tests/core/test_runtime_config.py backend/tests/adapters/test_credential_broker.py
git commit -m "feat: add runtime config and credential broker"
```

## Task 4: Implement Per-Workspace SQLite JobStore

**Files:** Create `backend/app/core/jobs/{model,state_machine}.py`, `backend/app/adapters/jobs/{workspace_instance_registry,sqlite_repository}.py`, tests `backend/tests/adapters/test_workspace_instance_registry.py`, `test_sqlite_job_repository.py`.

**Interfaces:** `WorkspaceInstanceRegistry.resolve(workspace_root) -> WorkspaceInstance`, `SqliteJobRepository.open(machine_root)`, transactional `create_job/get_job/create_attempt/transition_job/transition_attempt`, unique `(principal, client_request_id)` and commit guard. The registry is a locked, atomically replaced local mapping under `%LOCALAPPDATA%\AllToNote\workspace-instances.json`; it keys an iwiki-inspected Workspace identity plus normalized canonical root to a generated local instance ID. The same root resolves to the same ID across processes. A copied or moved root is deliberately a new local instance and rebuilds caches from Portable truth; P0 never guesses that two paths are one writable instance. `machine_root` is then `%LOCALAPPDATA%\AllToNote\workspaces\<local-workspace-instance-id>` and `jobs.sqlite` lives directly below it; the instance ID is never written into the portable knowledge tree.

- [ ] **Step 1: Write failing state/transaction tests**

```python
def test_terminal_job_cannot_return_to_running(repo):
    job = repo.create_job(request_hash="sha256:" + "0" * 64, principal="local", client_request_id=None)
    repo.transition_job(job.job_id, JobState.RUNNING)
    repo.transition_job(job.job_id, JobState.FAILED)
    with pytest.raises(DomainError, match="job_terminal"):
        repo.transition_job(job.job_id, JobState.RUNNING)

def test_idempotency_key_cannot_bind_different_request(repo):
    repo.create_job(request_hash=HASH_A, principal="agent", client_request_id="req-1")
    with pytest.raises(DomainError, match="idempotency_conflict"):
        repo.create_job(request_hash=HASH_B, principal="agent", client_request_id="req-1")

@pytest.mark.parametrize("start,end", LEGAL_ATTEMPT_TRANSITIONS)
def test_attempt_state_machine_accepts_only_legal_edges(start, end):
    assert transition_attempt(start, end) is end

def test_job_cannot_be_terminal_while_an_attempt_is_pending_or_running(repo, running_attempt):
    with pytest.raises(DomainError, match="attempt_not_settled"):
        repo.transition_job(running_attempt.job_id, JobState.FAILED)

def test_same_root_is_stable_but_copied_root_gets_new_local_instance(instance_registry, workspace_copy):
    first = instance_registry.resolve(WORKSPACE_ROOT)
    assert instance_registry.resolve(WORKSPACE_ROOT).instance_id == first.instance_id
    assert instance_registry.resolve(workspace_copy).instance_id != first.instance_id
```

- [ ] **Step 2: Implement schema version 1**

Create tables `jobs`, `steps`, `attempts`, `events`, `challenges`, `external_operations`, `checkpoints`, `leases`, `source_identities`, with foreign keys, WAL, busy timeout and explicit transactions. Attempt edges are exactly `pending -> running`, `running -> succeeded/failed/cancelled/interrupted/needs_input`, and `pending -> skipped/cancelled`; terminal Attempts never return to running. A terminal Job transition is rejected until every Attempt is terminal. Store normalized JSON as UTF-8 text; no Secret columns.

- [ ] **Step 3: Run and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/adapters/test_workspace_instance_registry.py backend/tests/adapters/test_sqlite_job_repository.py -q`, then `.\.venv\Scripts\python.exe -m pytest backend -q`.

```bash
git add backend/app/core/jobs backend/app/adapters/jobs/workspace_instance_registry.py backend/app/adapters/jobs/sqlite_repository.py backend/tests/adapters/test_workspace_instance_registry.py backend/tests/adapters/test_sqlite_job_repository.py
git commit -m "feat: add durable workspace-scoped job store"
```

## Task 5: Implement JobService, Idempotent Submit, Challenge, and Retry-New-Job

**Files:** Create `backend/app/core/application/job_service.py`, test `backend/tests/core/test_job_service.py`.

**Interfaces:** `submit(request)`, `cancel(job_id)`, `respond(job_id, challenge_id, response)`, `retry(original_id, RetryJobRequest)`; retry returns a distinct Job ID.

- [ ] **Step 1: Write failing tests**

```python
def test_retry_creates_new_job_and_preserves_lineage(service, failed_job):
    retried = service.retry(failed_job.job_id, RetryJobRequest(1, "retry-1", failed_job.state, ()))
    assert retried.job_id != failed_job.job_id
    assert retried.retry_of_job_id == failed_job.job_id
    assert service.get(failed_job.job_id).state is JobState.FAILED

def test_challenge_response_is_hash_idempotent(service, waiting_job):
    first = service.respond(waiting_job.job_id, waiting_job.challenge_id, {"credential_profile":"p"})
    second = service.respond(waiting_job.job_id, waiting_job.challenge_id, {"credential_profile":"p"})
    assert second.active_attempt_id == first.active_attempt_id
```

- [ ] **Step 2: Implement minimal service and stable conflicts**

Hash canonical requests with sorted UTF-8 JSON. Respond consumes challenge and creates Attempt in one transaction. Retry schema version `1` requires a new `client_request_id`, exact expected original state and all unknown operation confirmations; reject unknown higher versions and any omitted unknown operation ID before creating the new Job. Replaying the same versioned retry request returns the already-created retry Job.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/core/test_job_service.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass. Then commit:

```bash
git add backend/app/core/application/job_service.py backend/tests/core/test_job_service.py
git commit -m "feat: add durable job lifecycle service"
```

## Task 6: Add Attempt Storage, Checkpoints, Event Journal, and Recovery Planner

**Files:** Create `backend/app/adapters/jobs/file_attempt_storage.py`, `backend/app/core/jobs/recovery.py`, test `backend/tests/core/test_checkpoint_recovery.py`.

**Interfaces:** `save_checkpoint(CheckpointRecord)`, `validate_checkpoint`, `plan_remaining_steps`; event sequence monotonic per Job.

- [ ] **Step 1: Write failing corruption/recovery tests**

```python
def test_corrupt_draft_checkpoint_rewinds_only_to_generate_draft(storage, planner):
    storage.write_valid("transcript", b'{"record_type":"transcript_header"}\n')
    storage.write_valid("draft", b"# Draft\n")
    storage.path("draft").write_bytes(b"changed")
    plan = planner.plan(STEPS)
    assert plan.pending[0] == "generate_draft"
    assert "normalize_transcript" not in plan.pending
```

- [ ] **Step 2: Implement atomic file write/hash validation and append-only JSONL**

SQLite is the sole truth for Job/Attempt/Event sequence and checkpoint metadata. Files contain only immutable payload bytes; SQLite stores their relative path, schema, input hash and output hash. Write payload temp → fsync → atomic replace → SQLite metadata transaction; an orphan payload is ignored and later garbage-collected. For events, commit the SQLite event first, then append `events.jsonl` as a rebuildable observer projection; startup backfills missing projection records from SQLite, and records that exist only in JSONL are never accepted as truth. Ignore only a truncated final JSONL line; reject malformed middle records. Check schema/input/output hash before checkpoint reuse.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/core/test_checkpoint_recovery.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass. Then commit:

```bash
git add backend/app/adapters/jobs/file_attempt_storage.py backend/app/core/jobs/recovery.py backend/tests/core/test_checkpoint_recovery.py
git commit -m "feat: add checkpointed job recovery"
```

## Task 7: Add Cancellation, Fencing, Resource Lease, and ExternalOperation Safety

**Files:** Create `backend/app/core/jobs/{cancellation,external_operation,resource_lease}.py`, `backend/app/adapters/jobs/machine_resource_lease.py`, tests `backend/tests/core/test_execution_safety.py`, `backend/tests/adapters/test_machine_resource_lease.py`.

**Interfaces:** `CancellationToken.raise_if_cancelled`, `ExternalOperationGuard`, `ResourceLease("transcriber:faster-whisper:gpu")`.

- [ ] **Step 1: Write failing tests**

```python
def test_unknown_paid_call_stops_without_retry(operation_guard):
    operation_guard.mark_started("op_1")
    operation_guard.reconcile_after_process_loss()
    assert operation_guard.get("op_1").outcome is ExternalOutcome.UNKNOWN

def test_stale_fencing_token_cannot_checkpoint(repo, old_attempt, new_attempt):
    with pytest.raises(DomainError, match="attempt_fenced"):
        repo.authorize_checkpoint(old_attempt.attempt_id, old_attempt.fencing_token)

def test_gpu_lease_is_exclusive_across_two_workspaces(machine_lease_store, workspace_a, workspace_b):
    first = machine_lease_store.acquire("transcriber:faster-whisper:gpu", owner=workspace_a)
    with pytest.raises(DomainError, match="resource_busy"):
        machine_lease_store.acquire("transcriber:faster-whisper:gpu", owner=workspace_b)
    first.release()
```

- [ ] **Step 2: Implement bounded leases and unknown outcome transition**

Use per-workspace DB compare-and-swap for Job scheduler ownership and fencing. Use a separate machine-level lease store at `%LOCALAPPDATA%\AllToNote\machine\leases.sqlite` on Windows (platformdirs equivalent on macOS) for resources such as `transcriber:faster-whisper:gpu`, so two Workspaces cannot acquire the same GPU concurrently. Never identify a process only by PID. Cancellation is cooperative outside commit; commit guard uses conditional Job update.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/core/test_execution_safety.py backend/tests/adapters/test_machine_resource_lease.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass. Then commit:

```bash
git add backend/app/core/jobs/cancellation.py backend/app/core/jobs/external_operation.py backend/app/core/jobs/resource_lease.py backend/app/adapters/jobs/machine_resource_lease.py backend/tests/core/test_execution_safety.py backend/tests/adapters/test_machine_resource_lease.py
git commit -m "feat: guard video jobs against duplicate side effects"
```

## Task 8: Integrate the Public iwiki Portable SDK

**Files:** Create `backend/app/adapters/iwiki/portable_gateway.py`, `backend/tests/fixtures/workspace-v2/**`, `backend/tests/adapters/test_iwiki_portable_gateway.py`.

**Interfaces:** `inspect`, `validate_candidate`, `prepare_candidate`, `commit_prepared`; no private iwiki imports.

- [ ] **Step 1: Write failing contract-lock tests**

```python
def test_gateway_rejects_wrong_schema_fingerprint(gateway, workspace_root, monkeypatch):
    monkeypatch.setattr(gateway, "EXPECTED_SCHEMA_SHA256", "sha256:" + "0" * 64)
    with pytest.raises(DomainError, match="portable_contract_incompatible"):
        gateway.inspect(workspace_root)

def test_gateway_imports_only_public_sdk():
    tree = ast.parse(inspect.getsource(sys.modules[IWikiPortableGateway.__module__]))
    imports = imported_module_names(tree)
    assert imports & {name for name in imports if name.startswith("iwiki.")} <= {
        "iwiki.errors",
        "iwiki.portable",
        "iwiki.workspace",
    }
```

- [ ] **Step 2: Implement adapter using `open_workspace(workspace_root, writable=True)` and public exports**

Map `IWikiError` codes to AllToNote categories without leaking absolute paths. Preserve `PreparedBundle` as process-local opaque object.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/adapters/test_iwiki_portable_gateway.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass against the installed pinned wheel. Then commit:

```bash
git add backend/app/adapters/iwiki backend/tests/adapters/test_iwiki_portable_gateway.py backend/tests/fixtures/workspace-v2
git commit -m "feat: integrate iwiki portable sdk"
```

## Task 9: Implement Portable JSON, Transcript, Evidence, Draft, and Quality Builders

**Files:** Create `backend/app/core/portable/{jsonio,artifacts,evidence,quality,markdown_safety}.py`, tests `backend/tests/core/test_portable_artifacts.py`.

**Interfaces:** exact UTF-8 LF encoders; `build_transcript`, `build_evidence_set`, `rewrite_segment_citations`, `evaluate_video_draft`.

- [ ] **Step 1: Write failing byte/semantic tests**

```python
def test_transcript_has_one_header_no_full_text_and_millisecond_segments():
    raw = build_transcript(REV_ID, "zh-CN", SEGMENTS)
    lines = [json.loads(line) for line in raw.splitlines()]
    assert lines[0]["record_type"] == "transcript_header"
    assert "full_text" not in lines[0]
    assert lines[1]["start_ms"] == 0
    assert raw.endswith(b"\n") and b"\r" not in raw

def test_segment_citations_are_rewritten_to_evidence_ids():
    result = rewrite_segment_citations("结论[^seg_000001]", {"seg_000001": EV_ID})
    assert result == f"结论[^{EV_ID}]"

def test_quality_repair_is_bounded_and_report_binds_final_draft(quality_runner):
    outcome = quality_runner.run(DRAFT_WITH_FIXABLE_FAILURE)
    assert quality_runner.repair_calls == 1
    assert outcome.report.subject_sha256 == sha256_digest(outcome.final_draft)

def test_unrepairable_quality_failure_is_publish_ineligible_not_execution_error(quality_runner):
    outcome = quality_runner.run(DRAFT_WITH_SEVERE_QUALITY_FAILURE)
    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert outcome.execution_error is None
```

- [ ] **Step 2: Implement deterministic builders and safety checks**

Reject empty text, invalid ranges, missing citations, `<script>`, active iframe, dangerous SVG/HTML, `javascript:`, unapproved `data:`, file/UNC/absolute links and Bundle escape. Every substantive H2 needs Evidence. Deterministic quality repair runs at most once and is counted separately from network retry; QualityReport binds the exact final Draft digest.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/core/test_portable_artifacts.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass. Then commit:

```bash
git add backend/app/core/portable backend/tests/core/test_portable_artifacts.py
git commit -m "feat: build portable video artifacts"
```

## Task 10: Assemble a Semantically Valid Video Bundle

**Files:** Create `backend/app/core/portable/bundle_assembler.py`, `backend/tests/integration/test_video_bundle_assembly.py`.

**Interfaces:** `BundleAssembler.assemble(VideoBundleInput) -> CandidateBundle`; P0 metadata type `source.metadata.v1`.

- [ ] **Step 1: Write failing real-validator test**

```python
def test_assembled_video_bundle_passes_iwiki_semantic_validation(assembler, gateway, workspace_root):
    candidate = assembler.assemble(valid_video_bundle_input(workspace_root))
    report = gateway.validate_candidate(workspace_root, candidate.staging_relative_path)
    assert report.valid, [(issue.code, issue.path) for issue in report.issues]
    assert not (candidate.absolute_path / "commit.json").exists()
    assert candidate.target_area == "raw_personal"
```

- [ ] **Step 2: Implement exact manifest/receipt/artifact inventory**

Use core Source/Revision schemas, `source.metadata.v1`, Transcript/Evidence/Draft/Quality outputs, deterministic sorted JSON, exact byte lengths/hashes, UUIDv7 IDs and namespaced video extensions. Add golden schema/byte tests for connector/platform/capability version, stable video identity, canonical URI, title/author/duration/published/observed time, subtitle acquisition mode, materialization/license/privacy/freshness, and the fixed output roles `primary_draft`, `transcript`, `evidence_set`, `quality_reports`, `source_snapshots`, and `display_assets`. Receipt contains `run_id`, bounded Job/Step Attempt summary, Recipe/Capability/Runtime/Portable contract versions, hashes of effective non-Secret policies, model/transcriber non-Secret identity, usage, quality, retry/parent run, redaction summary and start/completion timestamps; it never contains Secrets, full Prompt, provider raw, PID/lease/fencing values or absolute paths. No unknown `required_contracts`; obtain `raw_personal` from iwiki inspect and never hardcode or target `wiki`/`common`. After commit, assert iwiki generated `commit.json`, its digest matches `VideoProduceResult.commit_sha256`, and Candidate still never contained that file.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_video_bundle_assembly.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: both the real iwiki semantic validator test and the full suite pass. Then commit:

```bash
git add backend/app/core/portable/bundle_assembler.py backend/tests/integration/test_video_bundle_assembly.py
git commit -m "feat: assemble portable video source bundles"
```

## Task 11: Complete the Fake Vertical Slice and CLI Produce Contract

**Files:** Create `backend/app/core/application/video_service.py`, `backend/app/core/sdk.py`, `backend/app/runtime.py`, modify `backend/app/cli/main.py`, create `backend/tests/integration/test_fake_video_producer.py`, `backend/tests/cli/test_produce_video_cli.py`.

**Interfaces:** `submit_video`, `wait_job`, CLI `produce video <input> --workspace <path> --wait --json`.

- [ ] **Step 1: Write failing fake E2E and stdout tests**

```python
def test_fake_recipe_commits_once_and_returns_bundle(runtime, capsys, workspace_root):
    code = main(["produce", "video", "fixture://course", "--workspace", str(workspace_root), "--wait", "--json"], runtime=runtime)
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert code == 0
    assert envelope["data"]["state"] == "succeeded"
    assert envelope["data"]["bundle_id"].startswith("bnd_")
    assert envelope["data"]["workspace_relative_bundle_path"].startswith("raw/personal/bundles/")
    assert envelope["data"]["primary_draft_artifact_id"].startswith("art_")
    assert captured.err == ""
    assert captured.out.count("\n") == 1

def test_quality_fail_still_commits_and_returns_success(runtime_with_quality_fail, workspace_root):
    code, snapshot = run_fake_cli(runtime_with_quality_fail, workspace_root)
    assert code == 0
    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result.quality_overall is QualityOverall.FAIL
    assert snapshot.result.publish_eligible is False

@pytest.mark.parametrize("failure", PREFLIGHT_FAILURE_CASES)
def test_preflight_failure_starts_no_external_work(runtime_factory, failure, workspace_root):
    runtime, calls = runtime_factory(preflight_failure=failure)
    snapshot = runtime.wait_job(runtime.submit_video(valid_request(workspace_root)).job_id)
    assert snapshot.state is JobState.FAILED
    assert calls.download == calls.transcribe == calls.model == calls.ffmpeg == 0
```

- [ ] **Step 2: Implement a side-effect-free Preflight, then the sequential recipe runner and short commit guard**

Preflight validates normalized request schema, iwiki Workspace capability/fingerprint, Recipe version, Runtime/Video Feature Pack, JobStore/work directory, conservative disk space, selected Source/Transcript/Model/Screenshot capability combination, FFmpeg/model/transcriber loadability, effective non-Secret config and required credential references before any download, transcriber, model or paid call. Parameterize the failing-Preflight test over every check, including screenshot/model incompatibility. Persist the safe Preflight policy hash for Receipt provenance. After Preflight, the service executes fixed step names, saves checkpoint after each, prepares outside commit guard, then guard → fencing/cancel check → commit → JobStore success. After success, update the Source Identity Registry in the same local durability boundary as CommitResult. CLI renders one envelope; business logic stays in service.

- [ ] **Step 3: Add crash-after-rename reconciliation test**

Inject failure between SDK commit and JobStore success; `wait_job` must reconcile existing Bundle and make zero new model calls.

- [ ] **Step 4: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_fake_video_producer.py backend/tests/cli/test_produce_video_cli.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass and the Bundle validates through the real iwiki SDK. Then commit:

```bash
git add backend/app/core/application/video_service.py backend/app/core/sdk.py backend/app/runtime.py backend/app/cli/main.py backend/tests/integration/test_fake_video_producer.py backend/tests/cli/test_produce_video_cli.py
git commit -m "feat: deliver cli-only fake video producer"
```

## Task 12: Wrap Legacy Video Source Connectors

**Files:** Create `backend/app/adapters/sources/legacy_video.py`, tests `backend/tests/adapters/test_video_source_contract.py`, characterization tests `backend/tests/adapters/test_legacy_downloaders.py`.

**Interfaces:** `resolve(input) -> ResolvedVideoSource`; `acquire(source, need_media, output_dir, token) -> AcquiredVideoSource`.

- [ ] **Step 1: Write shared contract tests for Bilibili, YouTube, Douyin/TikTok, Kuaishou, Local**

```python
@pytest.mark.parametrize("input_value,connector_id", CASES)
def test_resolve_returns_stable_canonical_identity(registry, input_value, connector_id):
    resolved = registry.resolve(input_value)
    assert resolved.connector_id == connector_id
    assert "token=" not in resolved.canonical_identity

def test_subtitle_path_does_not_request_media(adapter, legacy_downloader):
    adapter.acquire(RESOLVED, need_media=False, output_dir=WORK, token=TOKEN)
    legacy_downloader.download.assert_called_once_with(ANY, output_dir=ANY, skip_download=True, need_video=False, quality=ANY)

def test_source_identity_hit_is_verified_before_reuse(registry, committed_bundle):
    registry.bind(CANONICAL_IDENTITY, committed_bundle.source_binding)
    assert registry.resolve_verified(CANONICAL_IDENTITY).source_id == committed_bundle.source_id
    committed_bundle.corrupt_manifest()
    assert registry.resolve_verified(CANONICAL_IDENTITY) is None

def test_source_identity_is_workspace_local_and_rebuildable(registry_factory, workspace_a, workspace_b, committed_bundle):
    registry_a = registry_factory(workspace_a)
    registry_a.rebuild_from_portable_truth()
    assert registry_a.resolve_verified(CANONICAL_IDENTITY).source_id == committed_bundle.source_id
    assert registry_factory(workspace_b).resolve_verified(CANONICAL_IDENTITY) is None
```

- [ ] **Step 2: Implement registry and wrapper without moving legacy classes**

Map legacy exceptions to stable Source errors; distinguish no-subtitle from transient/auth failure. Local paths become machine bindings, not portable absolute paths. Canonical identity excludes signed query tokens, short-lived redirects and credentials; a Registry hit is reused only after verifying the committed Bundle and manifest hash. When the local Registry is missing, rebuild from the iwiki index/Portable Bundles; when association cannot be proven, allocate a new Source ID instead of guessing. Classify `xiaoyuzhoufm_download.py` in a test/report but do not register it unless current product support is proven.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/adapters/test_video_source_contract.py backend/tests/adapters/test_legacy_downloaders.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass. Then commit:

```bash
git add backend/app/adapters/sources/legacy_video.py backend/tests/adapters/test_video_source_contract.py backend/tests/adapters/test_legacy_downloaders.py
git commit -m "feat: adapt legacy video sources to core"
```

## Task 13: Normalize Platform and Legacy Transcriber Outputs

**Files:** Create `backend/app/adapters/transcription/legacy_transcriber.py`, tests `backend/tests/adapters/test_transcript_contract.py`.

**Interfaces:** `normalize_legacy_transcript`, `LegacyTranscriberAdapter.transcribe(MediaInput, token) -> TranscriptDocument`.

- [ ] **Step 1: Write failing conversion/contract tests**

```python
def test_seconds_convert_without_truncating_evidence():
    result = normalize_legacy_transcript(TranscriptResult("zh", "ignored", [LegacySegment(1.001, 2.009, " text ")]))
    assert result.segments[0].start_ms == 1001
    assert result.segments[0].end_ms == 2009
    assert result.segments[0].segment_id == "seg_000001"

def test_unknown_remote_transcription_is_not_automatically_reissued(adapter, operation_guard):
    adapter.transport.lose_process_after_send = True
    with pytest.raises(DomainError, match="external_outcome_unknown"):
        adapter.transcribe(MEDIA, TOKEN)
    assert adapter.transport.calls == 1
    assert operation_guard.current.outcome is ExternalOutcome.UNKNOWN
```

Convert legacy numeric seconds through `Decimal(str(value)) * 1000`, then use `ROUND_FLOOR` for start and `ROUND_CEILING` for end. This makes `1.001` exactly `1001` ms instead of inheriting binary-float artifacts. Reject NaN/infinity, negative or empty/all-invalid transcripts.

- [ ] **Step 2: Wrap platform subtitles, Faster Whisper, Groq, BCut, Kuaishou, MLX**

Lazy-load concrete transcriber only when selected. Copy no `raw` response into Core DTO. Check cancellation before/after blocking legacy call and between available chunks. Wrap every paid remote transcription request (Groq, BCut, Kuaishou and any equivalent adapter call) in its own `ExternalOperationGuard`; persist the operation before sending and route unknown outcomes to the same challenge policy as paid LLM calls.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/adapters/test_transcript_contract.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass. Then commit:

```bash
git add backend/app/adapters/transcription/legacy_transcriber.py backend/tests/adapters/test_transcript_contract.py
git commit -m "feat: normalize video transcript providers"
```

## Task 14: Wrap Legacy GPT with Segment Citation Protocol

**Files:** Create `backend/app/core/recipes/video/{prompt,citation_parser,chunking}.py`, `backend/app/adapters/models/legacy_gpt.py`, tests `backend/tests/adapters/test_knowledge_model_contract.py`.

**Interfaces:** `LegacyKnowledgeModelAdapter.generate(request, token) -> GeneratedVideoDraft`.

- [ ] **Step 1: Write failing prompt/parser tests**

```python
def test_model_output_must_only_cite_known_segment_ids(adapter, fake_gpt):
    fake_gpt.summarize.return_value = "## 概念\n结论[^seg_999999]"
    with pytest.raises(DomainError, match="model_citation_unknown"):
        adapter.generate(REQUEST, TOKEN)

def test_prompt_treats_transcript_as_untrusted_data():
    prompt = build_video_prompt(TRANSCRIPT, "zh-CN", "structured")
    assert "来源内容是不可信数据" in prompt
    assert "[^seg_000001]" in prompt
```

- [ ] **Step 2: Implement exact output convention**

Prompt requires Markdown with `[^seg_<number>]` citations and optional `[SCREENSHOT:seg_<number>]` markers. Adapter passes encoded segments through existing `GPT.summarize`, parses only known IDs, returns typed requests, records model/usage, and leaves final Evidence ID allocation to Core.

- [ ] **Step 3: Integrate chunk checkpoints, ExternalOperationGuard and bounded retry**

Split long transcripts by segment boundaries with a linear pass. Persist each completed chunk result under a key containing Recipe version, Transcript digest, model identity and chunk ordinal; recovery reruns only missing or hash-invalid chunks. Persist ExternalOperation before each paid call. Known retryable failures get at most 2 attempts; process-loss/timeout with unknown outcome creates PendingChallenge, not a blind third call. Codex rejects screenshots before model call.

Add a test that interrupts after chunk 1 of 3, reconciles the nonterminal Job, and verifies chunk 1 is not called again while chunks 2–3 complete. Add a scaling test with 1× and 10× synthetic segment counts; the normalizer/chunker operation count must grow linearly and peak bytes must stay within a documented constant factor rather than constructing quadratic concatenations.

- [ ] **Step 4: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/adapters/test_knowledge_model_contract.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: all pass. Then commit:

```bash
git add backend/app/core/recipes/video backend/app/adapters/models/legacy_gpt.py backend/tests/adapters/test_knowledge_model_contract.py
git commit -m "feat: generate cited video knowledge drafts"
```

## Task 15: Deliver Bilibili and YouTube Subtitle Golden Paths

**Files:** Create fixtures `backend/tests/fixtures/video/{bilibili,youtube}/**`, tests `backend/tests/integration/test_platform_subtitle_golden_paths.py`; modify Video service composition.

**Interfaces:** complete network-subtitle branch with deterministic Legacy Source and Model fakes.

- [ ] **Step 1: Add fixed metadata/subtitle fixtures and failing E2E**

```python
@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_platform_subtitle_path_commits_without_media(platform, runtime_factory, workspace_root):
    runtime, calls = runtime_factory(platform=platform, subtitles=True)
    result = runtime.wait_job(runtime.submit_video(request(platform, workspace_root)).job_id)
    assert result.state is JobState.SUCCEEDED
    assert calls.media_download == 0
    assert calls.transcriber == 0
    assert result.result.bundle_id.startswith("bnd_")
```

- [ ] **Step 2: Wire resolve → subtitle → normalize → model → evidence → quality → commit**

Provided Transcript from Legacy FastAPI, when present, takes precedence and passes the same normalizer; it is part of request hash and is never trusted without schema checks.

- [ ] **Step 3: Add resume test after process interruption before the model checkpoint**

The harness terminates execution immediately before the first model call is checkpointed, leaving the Job nonterminal. After stale-lease reconciliation, the second `job wait` reuses Source/Transcript checkpoints and creates a new model Step Attempt. A normal exhausted model error must instead transition the Job to terminal `failed`; retrying that case is covered only by Task 5's new-Job path.

- [ ] **Step 4: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_platform_subtitle_golden_paths.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: both golden paths, recovery case and full suite pass. Then commit:

```bash
git add backend/tests/fixtures/video/bilibili backend/tests/fixtures/video/youtube backend/tests/integration/test_platform_subtitle_golden_paths.py backend/app/core/application/video_service.py backend/app/runtime.py
git commit -m "feat: add platform subtitle video recipe"
```

## Task 16: Deliver Local Video, Faster Whisper, and Optional Screenshot Path

**Files:** Create `backend/app/adapters/screenshots/ffmpeg.py`, fixture `backend/tests/fixtures/video/local-course.mp4`, metadata `backend/tests/fixtures/video/local-course.json`; modify `backend/pyproject.toml` to register smoke markers; create tests `backend/tests/integration/test_local_video_golden_path.py`, `backend/tests/adapters/test_ffmpeg_screenshot.py`.

**Interfaces:** local `external_local` SourceRevision, real/contract Transcriber, validated WebP screenshot asset.

- [ ] **Step 1: Write failing local/resume tests**

```python
def test_local_video_transcribes_once_across_model_resume(runtime, local_video, workspace_root):
    runtime.faults.interrupt_once("before_model_checkpoint")
    job = runtime.submit_video(local_request(local_video, workspace_root))
    with pytest.raises(SimulatedProcessLoss):
        runtime.wait_job(job.job_id)
    runtime.reconcile_interrupted_jobs()
    runtime.wait_job(job.job_id)
    assert runtime.transcriber.calls == 1
    assert runtime.get_job(job.job_id).state is JobState.SUCCEEDED

def test_on_demand_screenshot_is_a_declared_bundle_asset(runtime, local_video, workspace_root):
    runtime.model.output = DRAFT_WITH_VALID_SCREENSHOT_REQUEST
    snapshot = runtime.wait_job(runtime.submit_video(screenshot_request(local_video, workspace_root)).job_id)
    assert len(snapshot.result.display_asset_ids) == 1
    assert_bundle_declares_webp_asset(snapshot.result.bundle_id, snapshot.result.display_asset_ids[0])
    assert_bundle_draft_uses_relative_asset_link(snapshot.result.bundle_id)

def test_screenshot_policy_does_not_fetch_media_without_a_valid_model_request(runtime, workspace_root):
    runtime.model.output = DRAFT_WITHOUT_SCREENSHOT_REQUEST
    runtime.wait_job(runtime.submit_video(network_screenshot_request(workspace_root)).job_id)
    assert runtime.source.media_download_calls == 0
```

- [ ] **Step 2: Implement safe FFmpeg WebP extraction**

Use argument list, no shell, `-ss <seconds> -i <path> -frames:v 1 -c:v libwebp`; output path allocated inside Attempt assets. Nonzero exit raises a redacted stable error. Validate the requested segment and timestamp before subprocess; reject offsets outside the segment. On cancellation/timeout, terminate the owned FFmpeg process tree and never expose a partial asset. Convert valid WebP to declared `evidence.asset.v1`, use only Bundle-relative links, and reject old `/static/screenshots` paths.

- [ ] **Step 3: Add Windows real smoke marker**

Create a 10–15 second project-owned fixture containing the clearly spoken phrase “AllToNote turns video into cited knowledge” over a static generated frame. Record the creation tool/voice, expected phrase, duration, license=`project_test_fixture` and SHA-256 in `local-course.json`; fail tests if bytes do not match. Register pytest markers `windows_smoke` and `macos_smoke` in `backend/pyproject.toml`. The Windows command `.\.venv\Scripts\python.exe -m pytest -m windows_smoke backend/tests/integration/test_local_video_golden_path.py -q` must use real FFmpeg and a small Faster Whisper model/cache and assert the normalized transcript contains `alltonote`, `video`, and `knowledge`. Ordinary CI runs the deterministic transcriber fake.

- [ ] **Step 4: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_local_video_golden_path.py backend/tests/adapters/test_ffmpeg_screenshot.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
On the Windows release machine also run from the worktree root: `.\.venv\Scripts\python.exe -m pytest -m windows_smoke backend/tests/integration/test_local_video_golden_path.py -q`.
Expected: deterministic gates pass everywhere; Windows smoke exits 0 with real FFmpeg and Faster Whisper. Then commit:

```bash
git add backend/pyproject.toml backend/app/adapters/screenshots/ffmpeg.py backend/tests/fixtures/video/local-course.mp4 backend/tests/fixtures/video/local-course.json backend/tests/integration/test_local_video_golden_path.py backend/tests/adapters/test_ffmpeg_screenshot.py
git commit -m "feat: add local whisper video recipe"
```

## Task 17: Complete Remaining Adapter Contract Coverage

**Files:** Modify adapter registries; create `backend/tests/adapters/test_all_source_adapters.py`, `test_all_transcriber_adapters.py`, `test_all_model_adapters.py`; create `docs/adapter-support-matrix.md`.

**Interfaces:** every current production registry entry has capability metadata and contract result.

- [ ] **Step 1: Add inventory test**

```python
def test_every_registered_legacy_capability_has_adapter_contract_case():
    assert set(SUPPORT_PLATFORM_MAP) == {"youtube", "bilibili", "tiktok", "kuaishou", "douyin", "local"}
    assert {item.value for item in TranscriberType} == set(CONTRACT_TESTED_TRANSCRIBERS)
    assert set(PRODUCTION_MODEL_TYPES) <= set(CONTRACT_TESTED_MODEL_TYPES)
```

- [ ] **Step 2: Add characterization/contract cases**

Cover Douyin/TikTok, Kuaishou, Groq, BCut, Kuaishou transcript, MLX, OpenAI, Universal/OpenAI-compatible, DeepSeek, Qwen and Codex, plus every other model still present in the production registry. Tests mock remote service boundaries; opt-in live smoke is documented separately. The support matrix lists every registry key explicitly, so an accidentally incomplete `PRODUCTION_MODEL_TYPES` constant cannot make the inventory test self-consistently pass.

- [ ] **Step 3: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/adapters/test_all_source_adapters.py backend/tests/adapters/test_all_transcriber_adapters.py backend/tests/adapters/test_all_model_adapters.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: the inventory and all mocked contract cases pass. Then commit:

```bash
git add backend/app/adapters backend/tests/adapters/test_all_source_adapters.py backend/tests/adapters/test_all_transcriber_adapters.py backend/tests/adapters/test_all_model_adapters.py docs/adapter-support-matrix.md
git commit -m "test: lock video adapter compatibility matrix"
```

## Task 18: Finish Generic Run, Job, Config, Credential, Doctor, Recipe, and JSONL Commands

**Files:** Modify `backend/app/cli/main.py`, `backend/app/core/sdk.py`, `backend/app/core/application/job_service.py`, `backend/app/core/config/loader.py`, `backend/app/adapters/credentials/profile_catalog.py`, `keyring_broker.py`, `backend/app/adapters/jobs/sqlite_repository.py`; create `backend/app/core/application/doctor.py`, `backend/app/adapters/legacy/config_import.py`; tests `backend/tests/cli/test_run_command.py`, `test_job_commands.py`, `test_config_commands.py`, `test_doctor_command.py`, `test_event_stream.py`.

**Interfaces:** exact commands from design, including `run <recipe>@<version> --request <json>`; exit codes `0/2/10/20/30/40/50/60/70/130`.

- [ ] **Step 1: Write failing stdout/error/exit tests**

```python
def test_json_failure_is_single_safe_envelope(cli, capsys):
    code = cli(["produce", "video", "bad", "--workspace", "missing", "--wait", "--json"])
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert code == 10
    assert payload["ok"] is False
    assert "Traceback" not in output.out
    assert "api_key" not in json.dumps(payload).lower()

def test_generic_run_and_produce_video_build_the_same_normalized_request(cli, runtime, request_file):
    cli(["run", "alltonote.video-course-note@1", "--request", str(request_file), "--workspace", str(WORKSPACE), "--wait", "--json"])
    cli(["produce", "video", INPUT, "--workspace", str(WORKSPACE), "--wait", "--json"])
    assert runtime.submitted_requests[0] == runtime.submitted_requests[1]
```

Add table-driven cases for every P0 Video flag; `--json`/`--jsonl` mutual exclusion; unsupported Recipe/version and higher request schema; request-file/CLI override conflicts; no Workspace and no configured default (never current-directory guessing); platform subtitle precedence over `--transcriber`; stdout-only protocol with stderr-only progress; redacted `--debug`; `job events --follow` monotonic sequence and terminal-last; `job status` on failed Job exits 0; `produce --wait` on failed Job exits nonzero; committed Quality fail exits 0; and Ctrl+C exits 130 while the durable Job converges to the correct cancelled/interrupted semantics.

- [ ] **Step 2: Implement parsers/renderers only**

Commands call SDK: generic `run`; `job status/wait/events/cancel/retry/respond`; `config path/show/validate/set-default-workspace/import-legacy`; `credentials set/list/delete`; `doctor`; `recipe list/describe`. `produce video` only translates flags into the same versioned Recipe request used by `run`. JSONL event sequences are monotonic and the terminal event is last. Do not add optional `job list` or `job clean` in this plan.

- [ ] **Step 3: Add legacy import dry-run and no-overwrite tests**

Read old DAO only inside `app/adapters/legacy/config_import.py`; dry-run produces a redacted report, apply leaves the old DB untouched, atomically writes non-Secrets through the config loader and stores Secrets through `KeyringCredentialBroker`. Conflict requires explicit user resolution and never overwrites silently.

- [ ] **Step 4: Verify and commit**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/cli/test_run_command.py backend/tests/cli/test_job_commands.py backend/tests/cli/test_config_commands.py backend/tests/cli/test_doctor_command.py backend/tests/cli/test_event_stream.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, then `git diff --check`.
Expected: CLI protocol, exit-code and redaction tests pass. Then commit:

```bash
git add backend/app/cli/main.py backend/app/core/sdk.py backend/app/core/application/job_service.py backend/app/core/application/doctor.py backend/app/core/config/loader.py backend/app/adapters/credentials/profile_catalog.py backend/app/adapters/credentials/keyring_broker.py backend/app/adapters/jobs/sqlite_repository.py backend/app/adapters/legacy/config_import.py backend/tests/cli/test_run_command.py backend/tests/cli/test_job_commands.py backend/tests/cli/test_config_commands.py backend/tests/cli/test_doctor_command.py backend/tests/cli/test_event_stream.py
git commit -m "feat: complete video runtime automation cli"
```

## Task 19: Switch Legacy FastAPI to the Shared Application Facade

**Files:** Create `backend/app/adapters/legacy/fastapi_bridge.py`; modify `backend/app/routers/note.py`, `backend/app/runtime.py`, `backend/app/core/config/{model,loader}.py`; create `backend/tests/integration/test_legacy_fastapi_bridge.py`.

**Interfaces:** old `/generate_note` and `/task_status/{task_id}` shapes preserved as projections; Core Bundle remains truth.

- [ ] **Step 1: Write failing compatibility tests**

```python
def test_generate_note_uses_runtime_not_note_generator(client, monkeypatch):
    monkeypatch.setattr("app.services.note.NoteGenerator.generate", Mock(side_effect=AssertionError("legacy pipeline called")))
    response = client.post("/api/generate_note", json=LEGACY_REQUEST)
    assert response.status_code == 200
    assert response.json()["data"]["task_id"].startswith("job_")

def test_generate_note_submits_without_waiting_in_request_thread(client, runtime):
    response = client.post("/api/generate_note", json=LEGACY_REQUEST)
    assert response.status_code == 200
    runtime.wait_job.assert_not_called()

def test_task_status_projects_committed_bundle(client, succeeded_job):
    payload = client.get(f"/api/task_status/{succeeded_job.job_id}").json()["data"]
    assert payload["status"] == "SUCCESS"
    assert payload["result"]["markdown"].startswith("#")
    assert set(payload["result"]) >= {"markdown", "transcript", "audio_meta"}
    assert payload["result"]["transcript"]["segments"][0]["start"] == 1.001
    assert payload["result"]["transcript"]["segments"][0]["end"] == 2.009
    assert payload["result"]["audio_meta"]["duration"] >= 0

def test_enabled_core_route_never_falls_back_after_new_pipeline_failure(client, failing_runtime, legacy_generate):
    response = client.post("/api/generate_note", json=LEGACY_REQUEST)
    assert response.json()["code"] != 0
    legacy_generate.assert_not_called()
```

- [ ] **Step 2: Implement request/result bridge**

Add a single startup-time route cutover setting `legacy_video_pipeline = "producer_core" | "note_generator"`; characterize both modes before changing the production default. The setting is fixed for the process and there is no per-request fallback. Map old provider/model IDs only in the Legacy boundary; use configured default Workspace; map prefetched transcript through the normalizer; return Job ID as task ID. Preserve the complete frontend runtime shape consumed by `useTaskPolling.ts`: `markdown`, `transcript` with legacy floating-point seconds and `audio_meta`, plus any currently documented image fields. For old RAG/UI, write an explicitly disposable projection JSON from committed outputs, never treat it as success truth.

- [ ] **Step 3: Add safe asset projection**

Expose only manifest-declared asset by Bundle/Artifact ID after integrity check; rewrite legacy Markdown image links to this read-only endpoint. No arbitrary file path parameter.

- [ ] **Step 4: Cut over without silent fallback and verify**

Run separately from the worktree root: `.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_legacy_fastapi_bridge.py -q`, `.\.venv\Scripts\python.exe -m pytest backend -q`, `rg -n "NoteGenerator\(\)\.generate" backend/app/routers backend/app/adapters`, then `git diff --check`.
Expected: pytest and `git diff --check` exit 0; `rg` exits 1 with no output because there are zero active route calls. Then run `pnpm --dir BillNote_frontend build` from the repository root; expected exit 0. Commit:

```bash
git add backend/app/adapters/legacy/fastapi_bridge.py backend/app/routers/note.py backend/app/runtime.py backend/app/core/config/model.py backend/app/core/config/loader.py backend/tests/integration/test_legacy_fastapi_bridge.py
git commit -m "feat: route legacy video api through producer core"
```

## Task 20: Run Release Gates, Package Feature Boundaries, and Publish Operator Docs

**Files:** Modify `backend/pyproject.toml`, `backend/build.bat`, `backend/build.sh`, `backend/tests/helpers/report_cli_imports.py`; create `backend/tests/integration/test_crash_matrix.py`, `test_security_gates.py`, `test_performance_gates.py`, `docs/video-producer-cli.md`, `docs/video-producer-release-checklist.md`.

**Interfaces:** reproducible Base Runtime + Video extra, documented Windows release evidence.

- [ ] **Step 1: Add crash/security/performance gate tests**

```python
@pytest.mark.parametrize("fault", RECIPE_FAULT_BOUNDARIES)
def test_fault_recovery_never_duplicates_bundle_or_model_call(fault, harness):
    result = harness.run_with_fault_then_recover(fault)
    assert result.final_bundle_count == 1
    assert result.unexpected_model_replays == 0

@pytest.mark.parametrize("winner", ["cancel", "atomic_rename"])
def test_cancel_commit_race_has_one_legal_winner(winner, harness):
    result = harness.run_cancel_commit_race(winner)
    assert result.job_state is (JobState.CANCELLED if winner == "cancel" else JobState.SUCCEEDED)
    assert result.final_bundle_count == (0 if winner == "cancel" else 1)

def test_bundle_id_hash_conflict_never_overwrites_existing_bundle(harness):
    with pytest.raises(DomainError, match="bundle_id_conflict"):
        harness.commit_same_bundle_id_with_different_manifest()

def test_help_path_does_not_import_heavy_modules(subprocess_json):
    report = subprocess_json([sys.executable, "tests/helpers/report_cli_imports.py", "version", "--json"])
    assert not {"fastapi", "torch", "faster_whisper"} & set(report["imported_modules"])

def test_measured_cli_budgets(benchmark_cli):
    assert benchmark_cli.p95(["version", "--json"]) <= 1.0
    assert benchmark_cli.p95(["job", "status", JOB_ID, "--json"]) <= 0.5
    assert benchmark_cli.peak_rss(["version", "--json"]) <= 150 * 1024 * 1024
```

`RECIPE_FAULT_BOUNDARIES` contains before/after every fixed stage: Preflight, Resolve, Acquire, Normalize Transcript, Create SourceRevision, every Model chunk, Optional Screenshots, Assemble Candidate, Quality/repair, Validate, Prepare, atomic rename, and JobStore success persistence. The crash matrix also corrupts each checkpoint class (Source metadata/revision, Transcript, Draft/chunk, screenshot asset, Candidate) and verifies the earliest safe rewind. Add explicit cases for Source/network transient retry ≤3 with `Retry-After`, model retry ≤2, quality repair ≤1, no retry for auth/policy/input/contract failures, two concurrent `job wait` owners, stale lease/fencing, unknown-operation confirmation on retry, and all Attempts settled before terminal Job.

The performance test first writes the measured machine/OS/Python baseline to the release report. The published Windows gate uses the confirmed budgets above; if the target hardware cannot meet one, the task must stop for an explicit spec amendment rather than silently weakening the assertion. It also asserts video/media is streamed rather than wholly read, Transcript is streamed as NDJSON, local transcription records audio duration/transcription duration/RTF/device/model/compute type, and 1×/10× transcript scaling stays linear; a stable-baseline regression over 15% requires investigation. Add security cases for dangerous Markdown/active content, undeclared/dangling assets, path traversal, symlink/junction/reparse points, Windows reserved names, Unicode and case collisions, `file://`/FTP/custom URL schemes and unsafe redirects, subprocess argument injection/timeout/cancel, debug redaction, default-no-telemetry and Bundle hash conflict. Add reliability cases for every Recipe step boundary, stale owner/fencing, cancel-versus-commit in both winner orders, unknown external outcomes, and rename-before-JobStore recovery.

- [ ] **Step 2: Split package extras without deleting legacy requirements**

Base contains Core/CLI/Job/Config/Credential/iwiki; `[video]` contains source/transcription/model/screenshot dependencies; `[web]` contains FastAPI/uvicorn/SQLAlchemy; `[dev]` contains pytest/build. Existing `requirements.txt` remains compatibility lock until installer migration has its own plan.

- [ ] **Step 3: Build once and verify clean Base and Video installations on Windows 11 x64**

```powershell
cd backend
$python = Resolve-Path ..\.venv\Scripts\python.exe
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'full deterministic suite failed' }
& $python -m pytest -m windows_smoke -q
if ($LASTEXITCODE -ne 0) { throw 'Windows smoke suite failed' }
& $python -m build
if ($LASTEXITCODE -ne 0) { throw 'AllToNote wheel build failed' }
$iwikiSource = 'E:\Agent_Learning\.worktrees\llm-iwiki\iwiki-portable-contract-v1'
$lockedIwikiCommit = '8701ace4f65ffd7ee46fbcf3edcc2ce2bcfc47e1'
if ((git -C $iwikiSource rev-parse HEAD).Trim() -ne $lockedIwikiCommit) { throw 'iwiki source commit does not match runtime lock' }
$releaseRoot = Join-Path $env:TEMP ('alltonote-release-0.1.0-' + [guid]::NewGuid().ToString('N'))
$iwikiWheelhouse = Join-Path $releaseRoot 'wheelhouse\iwiki'
New-Item -ItemType Directory -Force -Path $iwikiWheelhouse | Out-Null
& $python -m build --wheel --outdir $iwikiWheelhouse $iwikiSource
if ($LASTEXITCODE -ne 0) { throw 'locked iwiki wheel build failed' }
py -3.11 -m venv (Join-Path $releaseRoot 'base')
if ($LASTEXITCODE -ne 0) { throw 'Base release venv creation failed' }
& (Join-Path $releaseRoot 'base\Scripts\python.exe') -m pip install --find-links $iwikiWheelhouse dist\alltonote_runtime-0.1.0-py3-none-any.whl
if ($LASTEXITCODE -ne 0) { throw 'Base release install failed' }
& (Join-Path $releaseRoot 'base\Scripts\alltonote.exe') version --json
if ($LASTEXITCODE -ne 0) { throw 'Base release version command failed' }
py -3.11 -m venv (Join-Path $releaseRoot 'video')
if ($LASTEXITCODE -ne 0) { throw 'Video release venv creation failed' }
& (Join-Path $releaseRoot 'video\Scripts\python.exe') -m pip install --find-links $iwikiWheelhouse "dist\alltonote_runtime-0.1.0-py3-none-any.whl[video,dev]"
if ($LASTEXITCODE -ne 0) { throw 'Video release install failed' }
& (Join-Path $releaseRoot 'video\Scripts\alltonote.exe') doctor --workspace tests\fixtures\workspace-v2 --recipe video --json
if ($LASTEXITCODE -ne 0) { throw 'Video release doctor failed' }
git diff --check
if ($LASTEXITCODE -ne 0) { throw 'git diff check failed' }
```

Expected: every command exits 0; the iwiki source commit and built wheel match `runtime-lock.json`; no failed/error tests; the subprocess import report test proves Base does not import FastAPI, Torch, Faster Whisper, MLX, downloader packages, LLM SDK aggregates or legacy SQLAlchemy models; Video Doctor reports the Portable contract compatible. Record exact test counts, P95/RSS values and smoke Bundle ID in the release checklist. The temporary release root lives outside the repository and is never committed.

- [ ] **Step 4: Run the independent macOS Apple Silicon Tier 2 gate**

On a macOS Apple Silicon runner, create a clean Python 3.11 environment, install the same wheel with `[video,dev]`, run `python -m pytest -q`, then run `python -m pytest -m macos_smoke tests/integration/test_local_video_golden_path.py -q`. Expected: deterministic suite and local video smoke exit 0. Failure blocks only the macOS release label, never the Windows artifact; record the result separately.

- [ ] **Step 5: Run manual quality corpus and real platform smoke**

Run one public Bilibili, one public YouTube and one owned local video with a real configured Provider. Review facts, coverage, hallucination, structure, citations, timestamps, terminology and Obsidian rendering. No severe hallucination or invalid citation is permitted.

- [ ] **Step 6: Verify repository boundaries and commit**

```powershell
rg -n "from fastapi|import fastapi|sqlalchemy|NoteGenerator|provider_dao|video_task_dao" backend/app/core backend/app/cli
rg -n "commit_prepared_bundle|prepare_bundle_commit" backend/app
git status --short
git diff --check
```

Expected: first command has no Core/CLI violations; iwiki calls occur only in `adapters/iwiki`; only intended files changed.

```bash
git add backend/pyproject.toml backend/build.bat backend/build.sh backend/tests/helpers/report_cli_imports.py backend/tests/integration/test_crash_matrix.py backend/tests/integration/test_security_gates.py backend/tests/integration/test_performance_gates.py docs/video-producer-cli.md docs/video-producer-release-checklist.md
git commit -m "release: gate headless video producer"
```

---

## Delivery Slicing

This is the full implementation plan for the already-confirmed Video Producer P0 definition; it is not one undifferentiated release push.

- **Essential Producer slice (Tasks 1–16, V1–V3):** deliver the real architectural spine, fake Bundle walking skeleton, Bilibili/YouTube subtitle paths, and local Faster Whisper path. Stop after each milestone for user acceptance. V3 is the first practically useful headless Producer, but it must not be advertised as the complete P0 while Adapter/automation/Web/release gates remain open.
- **Contract-complete P0 slice (Tasks 17–20, V4–V7):** close the confirmed existing-Adapter inventory, full automation commands, explicit FastAPI cutover and platform/release evidence. Start it only after V3 is accepted; do not mix its changes into earlier commits.

This split deliberately avoids a throwaway “simple pipeline”: both slices use the same Core, Job model, Portable contract and iwiki commit path. It changes delivery checkpoints, not the confirmed architecture or Definition of Done.

---

## Specification Coverage Audit

| Confirmed design concern | Implemented and verified by |
|---|---|
| Headless shared Core, independent CLI, thin integration boundaries | Tasks 1, 2, 11, 18, 19 |
| Stable SDK/CLI contracts and exit codes | Tasks 2, 11, 18 |
| Per-workspace durable Job/Step Attempt model, idempotency, recovery and cancellation | Tasks 4–7, 11, 20 |
| Runtime config, OS credentials and secret redaction | Tasks 3, 18, 20 |
| Portable Source/Revision/Artifact/Evidence/Draft/Quality/Receipt contract | Tasks 8–10 |
| Public iwiki validation, prepare and atomic commit only | Tasks 8, 10, 11, 20 |
| Bilibili/YouTube subtitle fast path without unnecessary media work | Tasks 12, 13, 15 |
| Local video, Faster Whisper and opt-in screenshots | Tasks 13, 16 |
| Existing downloader/transcriber/model compatibility inventory | Tasks 12–17 |
| Prompt injection boundary, evidence citations and unknown paid-call outcomes | Tasks 7, 9, 14, 20 |
| FastAPI compatibility without making Web the source of truth | Task 19 |
| Windows release gate and separate macOS Tier 2 evidence | Tasks 16, 20 |
| Explicitly deferred daemon, MCP, Desktop Producer, Publisher and non-video Recipes | Global Constraints; no implementation task intentionally covers them |

Audit result: every in-scope section of the confirmed Video Producer design has a task, a named file boundary and a verification gate. Deferred scope is excluded explicitly rather than represented by empty interfaces or speculative modules.

---

## Subagent-Driven Execution Order

Tasks are sequential because each freezes interfaces consumed by the next. Dispatch exactly one implementation subagent at a time. After its commit:

1. Dispatch a specification reviewer with only the task text, design spec and diff; require explicit missing/extra behavior findings.
2. If findings exist, return them to the same implementer for correction and repeat specification review.
3. Dispatch a code-quality reviewer; require correctness, test quality, security, performance and maintainability findings.
4. Correct and re-review until both reviewers approve.
5. Run the task verification commands independently in the orchestrator.
6. Mark the task complete and dispatch the next fresh implementation subagent.

Milestone gates:

- V1 after Task 11: fake CLI → real iwiki Bundle.
- V2 after Task 15: Bilibili/YouTube subtitle paths.
- V3 after Task 16: local Faster Whisper path.
- V4 after Task 17: all current Adapter contracts.
- V5 after Task 18: reliable automation CLI.
- V6 after Task 19: FastAPI shares Core.
- V7 after Task 20: Windows release evidence and docs.

Do not parallelize tasks that modify `video_service.py`, `runtime.py`, `main.py`, JobStore schema or shared Domain DTOs. Fixture-only work may be delegated by an implementation subagent only after the owning task's public interfaces are frozen.
