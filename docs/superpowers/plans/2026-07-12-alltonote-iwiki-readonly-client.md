# AllToNote iwiki Read-Only Client Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the AllToNote backend a tested, read-only client for `iwiki` protocol v1 so later desktop workspace UI can inspect, validate, and query a local Markdown knowledge base without importing llm-iwiki internals.

**Architecture:** A small backend adapter discovers an `iwiki` executable, invokes it without a shell, parses exactly one JSON protocol envelope, negotiates schema/capabilities, and exposes typed service methods only inside Python. This plan deliberately adds no FastAPI route, Tauri command, workspace persistence, file watcher, or write capability; those belong to the separately reviewable Phase 2 desktop-workspace plan after the client boundary is proven.

**Tech Stack:** Python 3.11, `dataclasses`, `enum`, `json`, `pathlib`, `shutil`, `subprocess`, existing pytest suite.

## Global Constraints

- Work in `G:\AllToNote` on a new `codex/iwiki-readonly-client` branch or isolated worktree.
- Begin only after llm-iwiki Phase 1 exposes schema `2`, CLI protocol `1`, and capabilities through `iwiki inspect`.
- AllToNote must invoke the CLI; it must not import `E:\Agent_Learning\llm-iwiki\tools\*.py` or copy llm-iwiki path rules.
- The client is read-only and supports only `inspect`, `validate`, `query`, and `index status`.
- Never expose the workspace path through an unauthenticated HTTP route in this plan.
- Never use `shell=True`; arguments are an immutable list and the executable is discovered from `IWIKI_BIN` or `PATH`.
- stdout must contain exactly one JSON envelope; stderr is diagnostic text and must not be parsed as data.
- Supported values are CLI protocol `1` and schema `2`. A newer schema is returned as read-only when inspect succeeds; unsupported protocol is a hard compatibility error.
- A subprocess timeout or malformed output is reported as a typed client error, not as a successful empty result.
- Existing video generation, Codex app-server, provider, frontend, and Tauri behavior are untouched.

---

## File Responsibility Map

- Create `backend/app/services/iwiki_client.py` — executable discovery, subprocess transport, envelope parsing, typed errors, capability negotiation, and read-only commands.
- Create `backend/tests/fixtures/iwiki/inspect-success.json` — protocol v1 golden envelope.
- Create `backend/tests/fixtures/iwiki/query-success.json` — query result golden envelope.
- Create `backend/tests/test_iwiki_client.py` — unit tests with mocked subprocess boundaries.
- Create `backend/tests/test_iwiki_contract_e2e.py` — opt-in real CLI compatibility test controlled by `IWIKI_TEST_WORKSPACE`.
- Modify `backend/requirements.txt` only if a new dependency becomes necessary; the planned implementation uses only the standard library, so no dependency change is expected.
- Modify `README.md` — developer instructions for installing/discovering iwiki and running the cross-repository contract test.

## Public Interfaces Frozen by This Plan

| Symbol | Exact signature |
|---|---|
| `IWikiClient.discover` | `() -> IWikiClient` |
| `IWikiClient.inspect` | `(workspace: Path) -> IWikiInspectResult` |
| `IWikiClient.validate` | `(workspace: Path) -> dict[str, object]` |
| `IWikiClient.query` | `(workspace: Path, *, scope: str, text: str, limit: int = 20) -> dict[str, object]` |
| `IWikiClient.index_status` | `(workspace: Path) -> dict[str, object]` |

`IWikiClientError` carries `code: IWikiClientErrorCode`, `message: str`, and `details: dict[str, object]`. `IWikiInspectResult` carries schema/protocol versions, workspace identity, read-only state, a frozen capability set, relative contract paths, and index state.

## Task 1: Define and Test the Protocol Parser

**Files:**

- Create: `backend/app/services/iwiki_client.py`
- Create: `backend/tests/fixtures/iwiki/inspect-success.json`
- Create: `backend/tests/fixtures/iwiki/query-success.json`
- Create: `backend/tests/test_iwiki_client.py`

**Interfaces:**

- Consumes: iwiki CLI protocol v1 envelope.
- Produces: `IWikiEnvelope`, `IWikiInspectResult`, `IWikiClientErrorCode`, `IWikiClientError`, and `parse_envelope()`.

- [ ] **Step 1: Create golden protocol fixtures**

Create `backend/tests/fixtures/iwiki/inspect-success.json`:

```json
{
  "cli_protocol_version": 1,
  "ok": true,
  "command": "inspect",
  "data": {
    "schema_version": 2,
    "cli_protocol_version": 1,
    "workspace_id": "llm-iwiki-main",
    "name": "LLM Wiki",
    "read_only": false,
    "capabilities": ["inspect", "validate", "query_native", "plan_publish", "atomic_publish", "qmd_index"],
    "paths": {
      "raw_common": "raw/common",
      "raw_personal": "raw/personal",
      "wiki_common": "wiki/common",
      "wiki_personal": "wiki/personal",
      "cache": ".cache"
    },
    "index": {"state": "missing", "backend": "qmd"}
  },
  "error": null
}
```

Create `backend/tests/fixtures/iwiki/query-success.json`:

```json
{
  "cli_protocol_version": 1,
  "ok": true,
  "command": "query",
  "data": {
    "scope": "common",
    "query": "RHI",
    "items": [
      {
        "path": "wiki/common/ue5/rhi/index.md",
        "scope": "common",
        "title": "RHI",
        "score": 12,
        "snippet": "RHI is the rendering hardware interface.",
        "updated_at": "2026-07-12T00:00:00+00:00"
      }
    ],
    "index_backend": "native"
  },
  "error": null
}
```

- [ ] **Step 2: Write failing parser tests**

Create `backend/tests/test_iwiki_client.py`:

```python
from pathlib import Path
import json
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.iwiki_client import (
    IWikiClientError,
    IWikiClientErrorCode,
    parse_envelope,
    parse_inspect_result,
)


FIXTURES = Path(__file__).parent / "fixtures" / "iwiki"


def test_parse_inspect_success_fixture():
    envelope = parse_envelope((FIXTURES / "inspect-success.json").read_text(encoding="utf-8"), "inspect")
    result = parse_inspect_result(envelope)
    assert result.schema_version == 2
    assert result.cli_protocol_version == 1
    assert result.capabilities >= {"inspect", "validate", "query_native", "qmd_index"}


@pytest.mark.parametrize("stdout", ["", "not-json", "{}", "[]", '{"cli_protocol_version": 2}'])
def test_parse_envelope_rejects_malformed_or_incompatible_output(stdout: str):
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(stdout, "inspect")
    assert raised.value.code in {
        IWikiClientErrorCode.MALFORMED_RESPONSE,
        IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL,
    }


def test_parse_envelope_converts_remote_error():
    stdout = json.dumps({
        "cli_protocol_version": 1,
        "ok": False,
        "command": "inspect",
        "data": None,
        "error": {"code": "invalid_workspace", "message": "cannot read manifest", "details": {}},
    })
    with pytest.raises(IWikiClientError) as raised:
        parse_envelope(stdout, "inspect")
    assert raised.value.code == IWikiClientErrorCode.REMOTE_ERROR
    assert raised.value.details["remote_code"] == "invalid_workspace"
```

- [ ] **Step 3: Run the focused tests to verify red state**

Run:

```powershell
cd backend
pytest tests/test_iwiki_client.py -v
```

Expected: FAIL with `ModuleNotFoundError: app.services.iwiki_client`.

- [ ] **Step 4: Implement typed protocol parsing**

Create `backend/app/services/iwiki_client.py` with these complete parser types and functions:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any


SUPPORTED_CLI_PROTOCOL = 1
SUPPORTED_SCHEMA_VERSION = 2


class IWikiClientErrorCode(str, Enum):
    NOT_INSTALLED = "not_installed"
    TIMEOUT = "timeout"
    PROCESS_FAILED = "process_failed"
    MALFORMED_RESPONSE = "malformed_response"
    INCOMPATIBLE_PROTOCOL = "incompatible_protocol"
    MISSING_CAPABILITY = "missing_capability"
    REMOTE_ERROR = "remote_error"


class IWikiClientError(Exception):
    def __init__(self, code: IWikiClientErrorCode, message: str, details: dict[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class IWikiEnvelope:
    cli_protocol_version: int
    command: str
    data: dict[str, Any]


@dataclass(frozen=True)
class IWikiInspectResult:
    schema_version: int
    cli_protocol_version: int
    workspace_id: str
    name: str
    read_only: bool
    capabilities: frozenset[str]
    paths: dict[str, str]
    index: dict[str, object]


def parse_envelope(stdout: str, expected_command: str) -> IWikiEnvelope:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, "iwiki stdout is not JSON") from error
    if not isinstance(payload, dict):
        raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, "iwiki response must be an object")
    protocol = payload.get("cli_protocol_version")
    if protocol != SUPPORTED_CLI_PROTOCOL:
        raise IWikiClientError(
            IWikiClientErrorCode.INCOMPATIBLE_PROTOCOL,
            "unsupported iwiki CLI protocol",
            {"expected": SUPPORTED_CLI_PROTOCOL, "actual": protocol},
        )
    if payload.get("command") != expected_command:
        raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, "iwiki command does not match request")
    if payload.get("ok") is not True:
        remote = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        raise IWikiClientError(
            IWikiClientErrorCode.REMOTE_ERROR,
            str(remote.get("message", "iwiki command failed")),
            {"remote_code": remote.get("code"), "remote_details": remote.get("details", {})},
        )
    data = payload.get("data")
    if not isinstance(data, dict) or payload.get("error") is not None:
        raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, "iwiki success response has invalid data")
    return IWikiEnvelope(protocol, expected_command, data)


def parse_inspect_result(envelope: IWikiEnvelope) -> IWikiInspectResult:
    data = envelope.data
    capabilities = data.get("capabilities")
    paths = data.get("paths")
    index = data.get("index")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, "inspect capabilities are invalid")
    if not isinstance(paths, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in paths.items()):
        raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, "inspect paths are invalid")
    if not isinstance(index, dict):
        raise IWikiClientError(IWikiClientErrorCode.MALFORMED_RESPONSE, "inspect index is invalid")
    return IWikiInspectResult(
        schema_version=int(data["schema_version"]),
        cli_protocol_version=int(data["cli_protocol_version"]),
        workspace_id=str(data["workspace_id"]),
        name=str(data["name"]),
        read_only=bool(data["read_only"]),
        capabilities=frozenset(capabilities),
        paths=dict(paths),
        index=dict(index),
    )
```

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
cd backend
pytest tests/test_iwiki_client.py -v
cd ..
git add backend/app/services/iwiki_client.py backend/tests/fixtures/iwiki backend/tests/test_iwiki_client.py
git commit -m "feat: parse iwiki protocol responses"
```

Expected: parser tests pass.

## Task 2: Add Safe Executable Discovery and Subprocess Transport

**Files:**

- Modify: `backend/app/services/iwiki_client.py`
- Modify: `backend/tests/test_iwiki_client.py`

**Interfaces:**

- Consumes: an explicit executable path and immutable argument list.
- Produces: `IWikiTransport.run(command, args, timeout_seconds)` and `discover_iwiki_bin()`.

- [ ] **Step 1: Add failing discovery/transport tests**

Append:

```python
from subprocess import CompletedProcess, TimeoutExpired

from app.services.iwiki_client import IWikiTransport, discover_iwiki_bin


def test_discovery_prefers_explicit_environment(monkeypatch, tmp_path: Path):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    monkeypatch.setenv("IWIKI_BIN", str(binary))
    monkeypatch.setattr("app.services.iwiki_client.shutil.which", lambda _name: None)
    assert discover_iwiki_bin() == binary.resolve()


def test_transport_uses_argument_list_without_shell(monkeypatch, tmp_path: Path):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    fixture = (FIXTURES / "inspect-success.json").read_text(encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return CompletedProcess(args, 0, fixture, "")

    monkeypatch.setattr("app.services.iwiki_client.subprocess.run", fake_run)
    envelope = IWikiTransport(binary).run("inspect", ["--workspace", "C:/Wiki", "--json"], 10)
    assert envelope.command == "inspect"
    assert calls[0][0] == [str(binary), "inspect", "--workspace", "C:/Wiki", "--json"]
    assert "shell" not in calls[0][1] or calls[0][1]["shell"] is False


def test_transport_timeout_is_typed(monkeypatch, tmp_path: Path):
    binary = tmp_path / "iwiki.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.iwiki_client.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutExpired(args[0], 10)),
    )
    with pytest.raises(IWikiClientError) as raised:
        IWikiTransport(binary).run("inspect", ["--workspace", "C:/Wiki", "--json"], 10)
    assert raised.value.code == IWikiClientErrorCode.TIMEOUT
```

- [ ] **Step 2: Run tests to verify red state**

Run:

```powershell
cd backend
pytest tests/test_iwiki_client.py -v
```

Expected: FAIL because transport/discovery symbols are missing.

- [ ] **Step 3: Implement discovery and transport**

Append to `iwiki_client.py`:

```python
import os
import shutil
import subprocess


def discover_iwiki_bin() -> Path:
    configured = os.environ.get("IWIKI_BIN")
    if configured:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            return path
        raise IWikiClientError(IWikiClientErrorCode.NOT_INSTALLED, "IWIKI_BIN does not point to a file")
    discovered = shutil.which("iwiki") or shutil.which("iwiki.exe")
    if not discovered:
        raise IWikiClientError(IWikiClientErrorCode.NOT_INSTALLED, "iwiki executable was not found")
    return Path(discovered).resolve()


class IWikiTransport:
    def __init__(self, executable: Path):
        self.executable = executable.resolve()

    def run(self, command: str, args: list[str], timeout_seconds: float) -> IWikiEnvelope:
        process_args = [str(self.executable), command, *args]
        try:
            result = subprocess.run(
                process_args,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            raise IWikiClientError(IWikiClientErrorCode.TIMEOUT, f"iwiki {command} timed out") from error
        except (OSError, UnicodeError) as error:
            raise IWikiClientError(IWikiClientErrorCode.PROCESS_FAILED, f"cannot run iwiki {command}") from error
        try:
            return parse_envelope(result.stdout, command)
        except IWikiClientError as error:
            if result.returncode != 0:
                error.details.update({"exit_code": result.returncode, "stderr": result.stderr[-4000:]})
            raise
```

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
cd backend
pytest tests/test_iwiki_client.py -v
cd ..
git add backend/app/services/iwiki_client.py backend/tests/test_iwiki_client.py
git commit -m "feat: add safe iwiki subprocess transport"
```

Expected: discovery, no-shell argument, timeout, and parser tests pass.

## Task 3: Implement Read-Only Capability-Negotiated Client Methods

**Files:**

- Modify: `backend/app/services/iwiki_client.py`
- Modify: `backend/tests/test_iwiki_client.py`

**Interfaces:**

- Consumes: `IWikiTransport` and inspect capabilities.
- Produces: `IWikiClient.discover()`, `inspect()`, `validate()`, `query()`, and `index_status()`.

- [ ] **Step 1: Add failing method and capability tests**

Append tests with a fake transport:

```python
from app.services.iwiki_client import IWikiClient, IWikiEnvelope


class FakeTransport:
    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.calls = []

    def run(self, command: str, args: list[str], timeout_seconds: float) -> IWikiEnvelope:
        self.calls.append((command, args, timeout_seconds))
        return IWikiEnvelope(1, command, self.responses[command])


def inspect_data(capabilities: list[str]) -> dict:
    return {
        "schema_version": 2,
        "cli_protocol_version": 1,
        "workspace_id": "wiki",
        "name": "Wiki",
        "read_only": False,
        "capabilities": capabilities,
        "paths": {"wiki_common": "wiki/common", "wiki_personal": "wiki/personal"},
        "index": {"state": "ready"},
    }


def test_query_checks_capability_and_scope_before_invocation(tmp_path: Path):
    transport = FakeTransport({"inspect": inspect_data(["inspect", "query_native"]), "query": {"items": []}})
    client = IWikiClient(transport)
    client.query(tmp_path, scope="personal", text="daily", limit=5)
    assert transport.calls[-1][1] == [
        "--workspace", str(tmp_path.resolve()), "--scope", "personal", "--text", "daily", "--limit", "5", "--json"
    ]


def test_missing_capability_stops_before_command(tmp_path: Path):
    transport = FakeTransport({"inspect": inspect_data(["inspect"])})
    client = IWikiClient(transport)
    with pytest.raises(IWikiClientError) as raised:
        client.query(tmp_path, scope="common", text="rhi")
    assert raised.value.code == IWikiClientErrorCode.MISSING_CAPABILITY
    assert [call[0] for call in transport.calls] == ["inspect"]


def test_query_rejects_invalid_scope_and_limit(tmp_path: Path):
    client = IWikiClient(FakeTransport({"inspect": inspect_data(["inspect", "query_native"])}))
    for scope, limit in (("raw", 10), ("common", 0), ("common", 101)):
        with pytest.raises(ValueError):
            client.query(tmp_path, scope=scope, text="x", limit=limit)
```

- [ ] **Step 2: Run tests to verify red state**

Run:

```powershell
cd backend
pytest tests/test_iwiki_client.py -v
```

Expected: FAIL because `IWikiClient` is missing.

- [ ] **Step 3: Implement the read-only client**

Append:

```python
class IWikiClient:
    def __init__(self, transport: IWikiTransport):
        self.transport = transport

    @classmethod
    def discover(cls) -> "IWikiClient":
        return cls(IWikiTransport(discover_iwiki_bin()))

    def inspect(self, workspace: Path) -> IWikiInspectResult:
        envelope = self.transport.run(
            "inspect", ["--workspace", str(workspace.expanduser().resolve()), "--json"], 10
        )
        return parse_inspect_result(envelope)

    def _require(self, workspace: Path, capability: str) -> IWikiInspectResult:
        inspected = self.inspect(workspace)
        if capability not in inspected.capabilities:
            raise IWikiClientError(
                IWikiClientErrorCode.MISSING_CAPABILITY,
                f"iwiki capability is missing: {capability}",
                {"capability": capability},
            )
        return inspected

    def validate(self, workspace: Path) -> dict[str, object]:
        self._require(workspace, "validate")
        return self.transport.run(
            "validate", ["--workspace", str(workspace.expanduser().resolve()), "--json"], 30
        ).data

    def query(self, workspace: Path, *, scope: str, text: str, limit: int = 20) -> dict[str, object]:
        if scope not in {"personal", "common", "combined"}:
            raise ValueError("scope must be personal, common, or combined")
        if not text.strip():
            raise ValueError("text must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self._require(workspace, "query_native")
        return self.transport.run(
            "query",
            [
                "--workspace", str(workspace.expanduser().resolve()),
                "--scope", scope,
                "--text", text,
                "--limit", str(limit),
                "--json",
            ],
            30,
        ).data

    def index_status(self, workspace: Path) -> dict[str, object]:
        self._require(workspace, "qmd_index")
        return self.transport.run(
            "index", ["status", "--workspace", str(workspace.expanduser().resolve()), "--json"], 10
        ).data
```

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
cd backend
pytest tests/test_iwiki_client.py -v
cd ..
git add backend/app/services/iwiki_client.py backend/tests/test_iwiki_client.py
git commit -m "feat: add read-only iwiki client"
```

Expected: all unit tests pass.

## Task 4: Add an Opt-In Cross-Repository Contract Test

**Files:**

- Create: `backend/tests/test_iwiki_contract_e2e.py`
- Modify: `README.md`

**Interfaces:**

- Consumes: installed `iwiki` executable and `IWIKI_TEST_WORKSPACE`.
- Produces: a real consumer/provider compatibility test without hardcoding the developer's E: drive in normal CI.

- [ ] **Step 1: Write the opt-in E2E test**

Create `backend/tests/test_iwiki_contract_e2e.py`:

```python
from pathlib import Path
import os
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.iwiki_client import IWikiClient


WORKSPACE = os.environ.get("IWIKI_TEST_WORKSPACE")


@pytest.mark.skipif(not WORKSPACE, reason="set IWIKI_TEST_WORKSPACE for cross-repository contract test")
def test_real_iwiki_cli_contract():
    client = IWikiClient.discover()
    workspace = Path(WORKSPACE)
    inspected = client.inspect(workspace)
    assert inspected.schema_version == 2
    assert inspected.cli_protocol_version == 1
    assert {"inspect", "validate", "query_native", "qmd_index"} <= inspected.capabilities
    report = client.validate(workspace)
    assert report["valid"] is True
    result = client.query(workspace, scope="common", text="RHI", limit=3)
    assert all(item["path"].startswith("wiki/common/") for item in result["items"])
```

- [ ] **Step 2: Verify normal test runs skip the external dependency**

Run:

```powershell
cd backend
Remove-Item Env:IWIKI_TEST_WORKSPACE -ErrorAction SilentlyContinue
pytest tests/test_iwiki_contract_e2e.py -v
```

Expected: 1 skipped, 0 failed.

- [ ] **Step 3: Verify against the real llm-iwiki Phase 1 workspace**

Run after installing the Phase 1 worktree with `python -m pip install -e <phase-1-worktree>`:

```powershell
$env:IWIKI_TEST_WORKSPACE='E:\Agent_Learning\llm-iwiki'
pytest tests/test_iwiki_contract_e2e.py -v
```

Expected: 1 passed. If the implementation is in a separate Phase 1 worktree, set the variable to that worktree instead of the dirty migration path.

- [ ] **Step 4: Document local setup**

Add to `README.md`:

```markdown
### llm-iwiki development contract

AllToNote talks to llm-iwiki through the `iwiki` CLI protocol; it does not import llm-iwiki Python internals.

1. Install the compatible runtime: `python -m pip install -e <llm-iwiki-worktree>`.
2. Optionally set `IWIKI_BIN` to the absolute `iwiki` executable path.
3. Set `IWIKI_TEST_WORKSPACE` to a schema-v2 workspace.
4. Run `cd backend && pytest tests/test_iwiki_contract_e2e.py -v`.

The client currently exposes only inspect, validate, query, and index status. No HTTP API or write operation is enabled at this stage.
```

- [ ] **Step 5: Commit**

Run:

```powershell
git add backend/tests/test_iwiki_contract_e2e.py README.md
git commit -m "test: add iwiki consumer contract gate"
```

## Task 5: Final Consumer Foundation Gate

**Files:**

- Verify: `backend/app/services/iwiki_client.py`
- Verify: all backend tests and unchanged frontend build

**Interfaces:**

- Consumes: completed client and contract test.
- Produces: a clean AllToNote commit suitable as the base for the later desktop workspace/API plan.

- [ ] **Step 1: Run focused unit and E2E tests**

Run:

```powershell
cd backend
pytest tests/test_iwiki_client.py -v
$env:IWIKI_TEST_WORKSPACE='E:\Agent_Learning\llm-iwiki'
pytest tests/test_iwiki_contract_e2e.py -v
```

Expected: all client unit tests pass and the real contract test passes against the completed Phase 1 workspace.

- [ ] **Step 2: Run the existing backend suite**

Run:

```powershell
pytest
```

Expected: all backend tests pass; no provider, Codex, video, transcriber, or task regression.

- [ ] **Step 3: Verify frontend remains unaffected**

Run:

```powershell
cd ..\BillNote_frontend
pnpm build
pnpm lint
```

Expected: production build and lint pass with no frontend source changes from this plan.

- [ ] **Step 4: Verify forbidden integration surfaces were not added**

Run from `G:\AllToNote`:

```powershell
git diff --name-only HEAD~4..HEAD
rg -n "iwiki" backend/app/routers BillNote_frontend/src BillNote_frontend/src-tauri/src
```

Expected: the diff contains only the client, its tests/fixtures, and README; the `rg` command returns no FastAPI route, frontend call, or Tauri command.

- [ ] **Step 5: Verify clean status and record the base commit**

Run:

```powershell
git diff --check HEAD~4..HEAD
git status --short
git log -4 --oneline
```

Expected: diff check exits 0; only the user's pre-existing untracked `AGENTS.md` may remain; four focused commits describe parser, transport, client, and contract gate.
