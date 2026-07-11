# llm-iwiki Stable Contract and CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a versioned `.llm-wiki/manifest.yaml` contract and installable `iwiki` CLI that safely inspects, validates, queries, plans publishing, atomically applies publishing, and manages rebuildable indexes for any compatible workspace.

**Architecture:** A new `iwiki` Python package owns the public automation contract and accepts an explicit workspace root everywhere. Existing `tools/*.py` remain implementation/compatibility entry points, but the public CLI never imports AllToNote and never assumes the process current directory is the workspace. JSON envelopes, exit codes, schema support, path containment, publish preconditions, journals, and capabilities are centralized so desktop, MCP, and future SDK consumers share one policy.

**Tech Stack:** Python 3.11+, `PyYAML`, `filelock`, stdlib `argparse`, `dataclasses`, `hashlib`, `json`, `pathlib`, `shutil`, `tempfile`, existing QMD adapter and unittest suite.

## Global Constraints

- Start from the clean Phase 0 commit produced by `2026-07-12-llm-iwiki-v2-migration-closeout.md`.
- Create an isolated worktree with `superpowers:using-git-worktrees`; do not implement Phase 1 in the migration worktree.
- `schema_version` and `cli_protocol_version` are independent integers; initial supported values are schema `2` and CLI protocol `1`.
- The stable CLI writes one JSON document to stdout and diagnostics only to stderr.
- All workspace paths in manifest, requests, plans, and responses are POSIX-style paths relative to the canonical workspace root.
- Absolute paths, `..`, empty segments, `.git`, `.llm-wiki`, and symlink/junction escapes are rejected for publish targets.
- Markdown and attachments are durable; `.cache/`, task state, QMD, graph data, and transaction journals are rebuildable.
- Publishing defaults to `personal`; `common` requires `allow_common: true` in the request and the CLI flag `--confirm-common` during apply.
- Index failure after Markdown commit returns `index_state: stale`; it never rolls back valid Markdown.
- Do not add a network listener or MCP server in this plan.

---

## File Responsibility Map

- Create `pyproject.toml` — package metadata and `iwiki = iwiki.cli:main` console entry point.
- Modify `requirements.txt` — explicit `PyYAML` and `filelock` runtime dependencies.
- Create `.llm-wiki/manifest.yaml` — canonical manifest for the existing repository workspace.
- Create `iwiki/__init__.py` — public version constants.
- Create `iwiki/errors.py` — stable error codes, typed exceptions, and process exit mapping.
- Create `iwiki/protocol.py` — JSON success/error envelope and serialization helpers.
- Create `iwiki/workspace.py` — manifest loading, schema negotiation, canonical path containment, and workspace model.
- Create `iwiki/validation.py` — workspace-level structural validation independent of the legacy CLI renderer.
- Create `iwiki/query_service.py` — read-only scope-first native retrieval and stable result DTOs.
- Create `iwiki/publish.py` — publish request/plan DTOs, hashes, planning, locking, journals, backup, atomic writes, and recovery status.
- Create `iwiki/index_service.py` — stable index status/refresh/rebuild facade around parameterized QMD runtime.
- Create `iwiki/cli.py` — argument parsing, command dispatch, JSON output, and exit codes.
- Modify `tools/qmd_runtime.py` — accept an explicit workspace root through a `QmdPaths` value object while preserving existing defaults.
- Modify `tools/wiki_runtime.py` — expose the stable manifest/workspace model through compatibility helpers without duplicating rules.
- Create `tests/fixtures/workspaces/valid-v2/**` — minimal golden workspace.
- Create `tests/test_manifest_contract.py` — manifest/schema/path tests.
- Create `tests/test_iwiki_cli.py` — subprocess-level JSON and exit-code tests.
- Create `tests/test_iwiki_query.py` — scope-first query tests.
- Create `tests/test_iwiki_publish.py` — plan, conflict, common confirmation, transaction, rollback, and index-stale tests.
- Create `tests/test_iwiki_index.py` — index-state facade tests.
- Modify `README.md` and `docs/wiki-architecture-v2.md` — install and automation contract documentation.

## Public Interfaces Frozen by This Plan

| Symbol | Exact signature |
|---|---|
| `open_workspace` | `(root: Path, *, writable: bool = False) -> Workspace` |
| `validate_workspace` | `(workspace: Workspace) -> ValidationReport` |
| `query_workspace` | `(workspace: Workspace, request: QueryRequest) -> QueryResult` |
| `plan_publish` | `(workspace: Workspace, request: PublishRequest) -> PublishPlan` |
| `apply_publish` | `(workspace: Workspace, plan: PublishPlan, *, confirm_common: bool, refresh_index: Callable[[Workspace], str] | None = None) -> PublishResult` |
| `get_index_status` | `(workspace: Workspace) -> IndexStatus` |

`WorkspaceManifest` contains `schema_version: int`, `workspace_id: str`, `name: str`, `relative_paths: dict[str, str]`, `defaults: WorkspaceDefaults`, and `description: str`. `Workspace` contains `root: Path`, `manifest: WorkspaceManifest`, `paths: WorkspacePaths`, and `read_only: bool`.

Every CLI response uses:

```json
{
  "cli_protocol_version": 1,
  "ok": true,
  "command": "inspect",
  "data": {},
  "error": null
}
```

## Task 1: Package the Stable Protocol and Error Surface

**Files:**

- Create: `pyproject.toml`
- Modify: `requirements.txt`
- Create: `iwiki/__init__.py`
- Create: `iwiki/errors.py`
- Create: `iwiki/protocol.py`
- Create: `tests/test_iwiki_protocol.py`

**Interfaces:**

- Consumes: Python 3.11.
- Produces: `CLI_PROTOCOL_VERSION`, `SCHEMA_VERSION`, `IWikiError`, `success_envelope()`, and `error_envelope()` for every later task.

- [ ] **Step 1: Write failing protocol tests**

Create `tests/test_iwiki_protocol.py`:

```python
import json
import unittest

from iwiki.errors import ErrorCode, IWikiError
from iwiki.protocol import error_envelope, success_envelope


class IWikiProtocolTests(unittest.TestCase):
    def test_success_envelope_is_versioned_and_serializable(self):
        payload = success_envelope("inspect", {"schema_version": 2})
        self.assertEqual(payload["cli_protocol_version"], 1)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])
        json.dumps(payload)

    def test_error_envelope_has_stable_code_and_details(self):
        error = IWikiError(ErrorCode.CONFLICT, "target changed", {"path": "wiki/personal/a.md"})
        payload = error_envelope("apply-publish", error)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "conflict")
        self.assertEqual(payload["error"]["details"]["path"], "wiki/personal/a.md")

    def test_exit_codes_are_stable(self):
        self.assertEqual(ErrorCode.INVALID_WORKSPACE.exit_code, 10)
        self.assertEqual(ErrorCode.SCHEMA_TOO_NEW.exit_code, 11)
        self.assertEqual(ErrorCode.CONFLICT.exit_code, 20)
```

- [ ] **Step 2: Run the tests to verify red state**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_protocol.py" -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'iwiki'`.

- [ ] **Step 3: Add package metadata and dependencies**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "llm-iwiki"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "filelock>=3.18,<4",
  "PyYAML>=6.0,<7",
]

[project.scripts]
iwiki = "iwiki.cli:main"

[tool.setuptools.packages.find]
include = ["iwiki*"]
```

Append to `requirements.txt`:

```text
filelock>=3.18,<4
PyYAML>=6.0,<7
```

- [ ] **Step 4: Implement constants and typed errors**

Create `iwiki/__init__.py`:

```python
CLI_PROTOCOL_VERSION = 1
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (2,)

__all__ = ["CLI_PROTOCOL_VERSION", "SCHEMA_VERSION", "SUPPORTED_SCHEMA_VERSIONS"]
```

Create `iwiki/errors.py`:

```python
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(Enum):
    INVALID_ARGUMENT = ("invalid_argument", 2)
    INVALID_WORKSPACE = ("invalid_workspace", 10)
    SCHEMA_TOO_NEW = ("schema_too_new", 11)
    VALIDATION_FAILED = ("validation_failed", 12)
    CONFLICT = ("conflict", 20)
    PERMISSION_DENIED = ("permission_denied", 21)
    RETRYABLE_RUNTIME = ("retryable_runtime", 30)
    INTERNAL = ("internal", 70)

    def __init__(self, wire_value: str, exit_code: int):
        self.wire_value = wire_value
        self.exit_code = exit_code


class IWikiError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
```

Create `iwiki/protocol.py`:

```python
from __future__ import annotations

from typing import Any

from iwiki import CLI_PROTOCOL_VERSION
from iwiki.errors import IWikiError


def success_envelope(command: str, data: Any) -> dict[str, Any]:
    return {
        "cli_protocol_version": CLI_PROTOCOL_VERSION,
        "ok": True,
        "command": command,
        "data": data,
        "error": None,
    }


def error_envelope(command: str, error: IWikiError) -> dict[str, Any]:
    return {
        "cli_protocol_version": CLI_PROTOCOL_VERSION,
        "ok": False,
        "command": command,
        "data": None,
        "error": {
            "code": error.code.wire_value,
            "message": error.message,
            "details": error.details,
        },
    }
```

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_protocol.py" -v
git add pyproject.toml requirements.txt iwiki/__init__.py iwiki/errors.py iwiki/protocol.py tests/test_iwiki_protocol.py
git commit -m "feat(iwiki): define stable protocol envelope"
```

Expected: 3 tests pass and the commit contains only the protocol/package surface.

## Task 2: Add Manifest Loading and Canonical Workspace Paths

**Files:**

- Create: `.llm-wiki/manifest.yaml`
- Create: `iwiki/workspace.py`
- Create: `tests/test_manifest_contract.py`
- Create: `tests/fixtures/workspaces/valid-v2/.llm-wiki/manifest.yaml`
- Create: `tests/fixtures/workspaces/valid-v2/raw/common/.gitkeep`
- Create: `tests/fixtures/workspaces/valid-v2/wiki/common/index.md`

**Interfaces:**

- Consumes: `SCHEMA_VERSION`, `SUPPORTED_SCHEMA_VERSIONS`, `IWikiError`.
- Produces: `WorkspacePaths`, `WorkspaceDefaults`, `WorkspaceManifest`, `Workspace`, `open_workspace()`, and `Workspace.resolve_contract_path()`.

- [ ] **Step 1: Write failing manifest and containment tests**

Create `tests/test_manifest_contract.py`:

```python
from pathlib import Path
import tempfile
import unittest

from iwiki.errors import ErrorCode, IWikiError
from iwiki.workspace import open_workspace


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "workspaces" / "valid-v2"


class ManifestContractTests(unittest.TestCase):
    def test_valid_manifest_resolves_contract_roots(self):
        workspace = open_workspace(FIXTURE)
        self.assertEqual(workspace.manifest.schema_version, 2)
        self.assertEqual(workspace.paths.wiki_common, (FIXTURE / "wiki/common").resolve())
        self.assertEqual(workspace.manifest.defaults.publish_scope, "personal")

    def test_newer_schema_opens_read_only_but_rejects_writable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".llm-wiki").mkdir()
            (root / ".llm-wiki/manifest.yaml").write_text(
                (FIXTURE / ".llm-wiki/manifest.yaml").read_text(encoding="utf-8").replace(
                    "schema_version: 2", "schema_version: 99"
                ),
                encoding="utf-8",
            )
            read_only = open_workspace(root)
            self.assertTrue(read_only.read_only)
            with self.assertRaises(IWikiError) as raised:
                open_workspace(root, writable=True)
            self.assertEqual(raised.exception.code, ErrorCode.SCHEMA_TOO_NEW)

    def test_manifest_rejects_absolute_and_parent_paths(self):
        for bad in ("C:/outside", "../outside", "/outside"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / ".llm-wiki").mkdir()
                text = (FIXTURE / ".llm-wiki/manifest.yaml").read_text(encoding="utf-8")
                (root / ".llm-wiki/manifest.yaml").write_text(
                    text.replace('wiki_common: "wiki/common"', f'wiki_common: "{bad}"'),
                    encoding="utf-8",
                )
                with self.assertRaises(IWikiError):
                    open_workspace(root)

    def test_publish_target_rejects_reserved_and_escape_paths(self):
        workspace = open_workspace(FIXTURE)
        for bad in ("../x.md", ".git/config", ".llm-wiki/manifest.yaml", "wiki/common/../../x.md"):
            with self.subTest(bad=bad), self.assertRaises(IWikiError):
                workspace.resolve_publish_target(bad)
```

- [ ] **Step 2: Run tests to verify red state**

Run:

```powershell
python -m unittest discover -s tests -p "test_manifest_contract.py" -v
```

Expected: FAIL because `iwiki.workspace` does not exist.

- [ ] **Step 3: Add the canonical repository manifest and fixture manifest**

Create both `.llm-wiki/manifest.yaml` and `tests/fixtures/workspaces/valid-v2/.llm-wiki/manifest.yaml`:

```yaml
schema_version: 2
workspace_id: "llm-iwiki-main"
name: "LLM Wiki"
paths:
  raw_common: "raw/common"
  raw_personal: "raw/personal"
  wiki_common: "wiki/common"
  wiki_personal: "wiki/personal"
  cache: ".cache"
defaults:
  publish_scope: "personal"
  visibility: "private"
  encoding: "utf-8"
  link_style: "wikilink"
description: "Personal and shared Markdown knowledge workspace."
```

Create `tests/fixtures/workspaces/valid-v2/wiki/common/index.md`:

```markdown
# Common Knowledge
```

Create the empty fixture root marker `tests/fixtures/workspaces/valid-v2/raw/common/.gitkeep`.

- [ ] **Step 4: Implement the workspace model and canonical path policy**

Create `iwiki/workspace.py` with these exact public types and rules:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from iwiki import SUPPORTED_SCHEMA_VERSIONS
from iwiki.errors import ErrorCode, IWikiError


@dataclass(frozen=True)
class WorkspacePaths:
    raw_common: Path
    raw_personal: Path
    wiki_common: Path
    wiki_personal: Path
    cache: Path


@dataclass(frozen=True)
class WorkspaceDefaults:
    publish_scope: str
    visibility: str
    encoding: str
    link_style: str


@dataclass(frozen=True)
class WorkspaceManifest:
    schema_version: int
    workspace_id: str
    name: str
    relative_paths: dict[str, str]
    defaults: WorkspaceDefaults
    description: str


@dataclass(frozen=True)
class Workspace:
    root: Path
    manifest: WorkspaceManifest
    paths: WorkspacePaths
    read_only: bool

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def resolve_publish_target(self, relative: str) -> Path:
        parts = _safe_relative_parts(relative)
        if parts[0] != "wiki" or len(parts) < 3:
            raise IWikiError(ErrorCode.PERMISSION_DENIED, "publish target must be under wiki/personal or wiki/common")
        if parts[1] not in {"personal", "common"}:
            raise IWikiError(ErrorCode.PERMISSION_DENIED, "unsupported publish scope")
        if any(part in {".git", ".llm-wiki", ".cache"} for part in parts):
            raise IWikiError(ErrorCode.PERMISSION_DENIED, "reserved path")
        target = (self.root / Path(*parts)).resolve(strict=False)
        _ensure_within(target, self.root)
        _ensure_existing_parents_within(target, self.root)
        return target


def _safe_relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise IWikiError(ErrorCode.INVALID_WORKSPACE, "path must be a non-empty string")
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ":" in pure.parts[0] or any(part in {"", ".", ".."} for part in pure.parts):
        raise IWikiError(ErrorCode.INVALID_WORKSPACE, "path must be canonical and relative", {"path": value})
    return pure.parts


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise IWikiError(ErrorCode.PERMISSION_DENIED, "path escapes workspace", {"path": str(path)}) from error


def _ensure_existing_parents_within(target: Path, root: Path) -> None:
    current = target.parent
    while current != root and not current.exists():
        current = current.parent
    resolved = current.resolve()
    _ensure_within(resolved, root)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IWikiError(ErrorCode.INVALID_WORKSPACE, f"{field} must be a mapping")
    return value


def open_workspace(root: Path, *, writable: bool = False) -> Workspace:
    canonical_root = root.expanduser().resolve()
    manifest_path = canonical_root / ".llm-wiki" / "manifest.yaml"
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise IWikiError(ErrorCode.INVALID_WORKSPACE, "cannot read manifest", {"path": str(manifest_path)}) from error
    data = _mapping(payload, "manifest")
    path_data = _mapping(data.get("paths"), "paths")
    defaults_data = _mapping(data.get("defaults"), "defaults")
    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int):
        raise IWikiError(ErrorCode.INVALID_WORKSPACE, "schema_version must be an integer")
    too_new = schema_version > max(SUPPORTED_SCHEMA_VERSIONS)
    unsupported_old = schema_version not in SUPPORTED_SCHEMA_VERSIONS and not too_new
    if unsupported_old:
        raise IWikiError(ErrorCode.INVALID_WORKSPACE, "unsupported schema", {"schema_version": schema_version})
    if writable and too_new:
        raise IWikiError(ErrorCode.SCHEMA_TOO_NEW, "newer schema is read-only", {"schema_version": schema_version})
    relative_paths = {key: str(path_data[key]) for key in ("raw_common", "raw_personal", "wiki_common", "wiki_personal", "cache")}
    resolved = {key: (canonical_root / Path(*_safe_relative_parts(value))).resolve(strict=False) for key, value in relative_paths.items()}
    for path in resolved.values():
        _ensure_within(path, canonical_root)
        _ensure_existing_parents_within(path, canonical_root)
    manifest = WorkspaceManifest(
        schema_version=schema_version,
        workspace_id=str(data["workspace_id"]),
        name=str(data["name"]),
        relative_paths=relative_paths,
        defaults=WorkspaceDefaults(
            publish_scope=str(defaults_data["publish_scope"]),
            visibility=str(defaults_data["visibility"]),
            encoding=str(defaults_data["encoding"]),
            link_style=str(defaults_data["link_style"]),
        ),
        description=str(data.get("description", "")),
    )
    return Workspace(canonical_root, manifest, WorkspacePaths(**resolved), too_new)
```

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest discover -s tests -p "test_manifest_contract.py" -v
git add .llm-wiki iwiki/workspace.py tests/fixtures tests/test_manifest_contract.py
git commit -m "feat(iwiki): add versioned workspace manifest"
```

Expected: 4 tests pass.

## Task 3: Implement Inspect, Validate, and the JSON CLI Shell

**Files:**

- Create: `iwiki/validation.py`
- Create: `iwiki/cli.py`
- Create: `tests/test_iwiki_cli.py`
- Modify: `tools/wiki_runtime.py`

**Interfaces:**

- Consumes: `open_workspace()`, protocol envelopes, stable errors.
- Produces: `iwiki inspect --workspace PATH --json` and `iwiki validate --workspace PATH --json`.

- [ ] **Step 1: Write subprocess-level failing tests**

Create `tests/test_iwiki_cli.py` with helpers that invoke `python -m iwiki.cli` and parse stdout:

```python
from pathlib import Path
import json
import subprocess
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests/fixtures/workspaces/valid-v2"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "iwiki.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class IWikiCliTests(unittest.TestCase):
    def test_inspect_returns_capabilities_and_versions(self):
        result = run_cli("inspect", "--workspace", str(FIXTURE), "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["data"]["schema_version"], 2)
        self.assertEqual(payload["data"]["cli_protocol_version"], 1)
        self.assertIn("plan_publish", payload["data"]["capabilities"])
        self.assertEqual(result.stderr, "")

    def test_validate_returns_structured_report(self):
        result = run_cli("validate", "--workspace", str(FIXTURE), "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(payload["data"]["valid"])
        self.assertEqual(payload["data"]["issues"], [])

    def test_invalid_workspace_uses_exit_10_and_json_error(self):
        result = run_cli("inspect", "--workspace", str(FIXTURE / "missing"), "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 10)
        self.assertEqual(payload["error"]["code"], "invalid_workspace")
```

- [ ] **Step 2: Run tests to verify red state**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_cli.py" -v
```

Expected: FAIL because `iwiki.cli` does not exist.

- [ ] **Step 3: Implement structural validation**

Create `iwiki/validation.py`:

```python
from dataclasses import asdict, dataclass

from iwiki.workspace import Workspace


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    issues: list[ValidationIssue]

    def to_dict(self) -> dict:
        return {"valid": self.valid, "issues": [asdict(issue) for issue in self.issues]}


def validate_workspace(workspace: Workspace) -> ValidationReport:
    issues: list[ValidationIssue] = []
    required = {
        "raw_common": workspace.paths.raw_common,
        "wiki_common": workspace.paths.wiki_common,
    }
    for name, path in required.items():
        if not path.is_dir():
            issues.append(ValidationIssue("missing_directory", workspace.relative(path), f"{name} directory is missing"))
    common_index = workspace.paths.wiki_common / "index.md"
    if not common_index.is_file():
        issues.append(ValidationIssue("missing_common_index", workspace.relative(common_index), "common index is missing"))
    for legacy in ("topics", "sources", "concepts", "_generated", "modules"):
        path = workspace.root / "wiki" / legacy
        if path.exists():
            issues.append(ValidationIssue("legacy_visible_root", workspace.relative(path), "legacy visible root is not canonical"))
    return ValidationReport(not issues, issues)
```

- [ ] **Step 4: Implement CLI dispatch with strict stdout JSON**

Create `iwiki/cli.py` with `build_parser()`, `dispatch()`, and `main()`; the inspect data must be exactly built from this capability set:

```python
CAPABILITIES = [
    "inspect",
    "validate",
    "query_native",
    "plan_publish",
    "atomic_publish",
    "qmd_index",
]
```

The `main()` exception boundary must follow:

```python
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    try:
        data = dispatch(args)
        payload = success_envelope(command, data)
        exit_code = 0
    except IWikiError as error:
        payload = error_envelope(command, error)
        exit_code = error.code.exit_code
    except Exception as error:
        wrapped = IWikiError(ErrorCode.INTERNAL, "internal iwiki error", {"type": type(error).__name__})
        payload = error_envelope(command, wrapped)
        exit_code = wrapped.code.exit_code
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
```

`inspect` returns manifest fields, `read_only`, supported schema versions, `cli_protocol_version`, capabilities, relative paths, and an initial index status of `missing`. `validate` returns `ValidationReport.to_dict()` and raises `VALIDATION_FAILED` only when invoked with `--strict`.

- [ ] **Step 5: Make the old runtime delegate workspace discovery**

Add to `tools/wiki_runtime.py`:

```python
from iwiki.workspace import Workspace, open_workspace


def current_workspace(*, writable: bool = False) -> Workspace:
    return open_workspace(REPO_ROOT, writable=writable)
```

Do not replace the existing constants in this task; compatibility remains until all old tools accept explicit workspace roots.

- [ ] **Step 6: Run tests, editable-install smoke test, and commit**

Run:

```powershell
python -m pip install -e .
python -m unittest discover -s tests -p "test_iwiki_cli.py" -v
iwiki inspect --workspace . --json | ConvertFrom-Json | Select-Object -ExpandProperty data
git add iwiki/validation.py iwiki/cli.py tools/wiki_runtime.py tests/test_iwiki_cli.py
git commit -m "feat(iwiki): add inspect and validate commands"
```

Expected: 3 CLI tests pass and the installed command returns schema 2/protocol 1.

## Task 4: Add Scope-First Read-Only Query

**Files:**

- Create: `iwiki/query_service.py`
- Create: `tests/test_iwiki_query.py`
- Modify: `iwiki/cli.py`

**Interfaces:**

- Consumes: `Workspace`, Markdown files under configured roots.
- Produces: `QueryRequest(scope, text, limit)` and `QueryResult` containing only documents from the selected roots.

- [ ] **Step 1: Write failing scope-isolation tests**

Create `tests/test_iwiki_query.py`:

```python
from pathlib import Path
import tempfile
import unittest

from iwiki.query_service import QueryRequest, query_workspace
from iwiki.workspace import open_workspace


FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/workspaces/valid-v2"


class IWikiQueryTests(unittest.TestCase):
    def test_personal_scope_never_returns_common_documents(self):
        workspace = open_workspace(FIXTURE)
        result = query_workspace(workspace, QueryRequest("personal", "knowledge", 10))
        self.assertTrue(all(item.scope == "personal" for item in result.items))

    def test_common_scope_never_returns_personal_documents(self):
        workspace = open_workspace(FIXTURE)
        result = query_workspace(workspace, QueryRequest("common", "knowledge", 10))
        self.assertTrue(all(item.scope == "common" for item in result.items))

    def test_invalid_scope_is_rejected_before_scanning(self):
        workspace = open_workspace(FIXTURE)
        with self.assertRaises(ValueError):
            query_workspace(workspace, QueryRequest("all-files", "x", 10))
```

- [ ] **Step 2: Run tests to verify red state**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_query.py" -v
```

Expected: FAIL because `iwiki.query_service` does not exist.

- [ ] **Step 3: Implement native query DTOs and retrieval**

Create `iwiki/query_service.py` with immutable `QueryRequest`, `QueryItem`, and `QueryResult`. Validate `scope` against `personal|common|combined` before selecting roots. Recursively scan only the selected roots, score case-insensitive term occurrences in relative path/title/body, sort by `(-score, path)`, and return at most `limit` items. Each item must serialize:

```python
{
    "path": workspace.relative(path),
    "scope": scope,
    "title": first_h1_or_stem,
    "score": score,
    "snippet": matching_line[:500],
    "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
}
```

Do not scan `raw/`, `.cache/`, `.git/`, or `.obsidian/`.

- [ ] **Step 4: Wire the CLI query command**

Add:

```text
iwiki query --workspace PATH --scope personal|common|combined --text QUERY --limit 20 --json
```

The JSON `data` shape is:

```json
{"scope":"common","query":"rhi","items":[],"index_backend":"native"}
```

- [ ] **Step 5: Test and commit**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_query.py" -v
iwiki query --workspace . --scope common --text RHI --limit 3 --json | ConvertFrom-Json
git add iwiki/query_service.py iwiki/cli.py tests/test_iwiki_query.py
git commit -m "feat(iwiki): add scope-first native query"
```

Expected: all query tests pass; CLI results contain only `wiki/common/**` paths.

## Task 5: Plan Publishing with Hash Preconditions

**Files:**

- Create: `iwiki/publish.py`
- Create: `tests/test_iwiki_publish.py`
- Modify: `iwiki/cli.py`

**Interfaces:**

- Consumes: a JSON `PublishRequest` and writable `Workspace`.
- Produces: a JSON `PublishPlan` with immutable plan ID, target writes, base hashes, content hashes, scope, warnings, and post-actions.

- [ ] **Step 1: Write failing planning tests**

Create `tests/test_iwiki_publish.py` with a helper that copies the valid fixture into a temporary directory, then add:

```python
def test_plan_defaults_to_personal_and_hashes_new_file(self):
    workspace = self.workspace()
    request = PublishRequest(
        writes=[PublishWriteRequest("wiki/personal/video/demo.md", "# Demo\n")],
        allow_common=False,
    )
    plan = plan_publish(workspace, request)
    self.assertEqual(plan.scope, "personal")
    self.assertIsNone(plan.writes[0].base_sha256)
    self.assertEqual(len(plan.writes[0].content_sha256), 64)

def test_plan_rejects_common_without_explicit_request_permission(self):
    workspace = self.workspace()
    request = PublishRequest(
        writes=[PublishWriteRequest("wiki/common/video/demo.md", "# Demo\n")],
        allow_common=False,
    )
    with self.assertRaises(IWikiError) as raised:
        plan_publish(workspace, request)
    self.assertEqual(raised.exception.code, ErrorCode.PERMISSION_DENIED)

def test_plan_rejects_mixed_scope_and_reserved_paths(self):
    workspace = self.workspace()
    for writes in (
        [PublishWriteRequest("wiki/personal/a.md", "a"), PublishWriteRequest("wiki/common/a.md", "a")],
        [PublishWriteRequest(".git/config", "a")],
    ):
        with self.subTest(writes=writes), self.assertRaises(IWikiError):
            plan_publish(workspace, PublishRequest(writes, allow_common=True))

def test_plan_adds_personal_navigation_indexes_idempotently(self):
    workspace = self.workspace()
    request = PublishRequest([PublishWriteRequest("wiki/personal/video/demo.md", "# Demo\n")])
    first = plan_publish(workspace, request)
    paths = [write.path for write in first.writes]
    self.assertEqual(paths, [
        "wiki/personal/index.md",
        "wiki/personal/video/demo.md",
        "wiki/personal/video/index.md",
    ])
    apply_publish(workspace, first, confirm_common=False, refresh_index=lambda _workspace: "ready")
    second = plan_publish(workspace, request)
    domain_index = next(write for write in second.writes if write.path == "wiki/personal/video/index.md")
    self.assertEqual(domain_index.proposed_content.count("[[video/demo|Demo]]"), 1)

def test_common_plan_requires_evidence_and_contains_full_diff(self):
    workspace = self.workspace()
    without_evidence = PublishRequest(
        [PublishWriteRequest("wiki/common/media/video/demo.md", "# Demo\n")],
        allow_common=True,
    )
    with self.assertRaises(IWikiError):
        plan_publish(workspace, without_evidence)
    with_evidence = PublishRequest(
        [PublishWriteRequest("wiki/common/media/video/demo.md", "# Demo\n\n## Sources\n\n- https://example.com\n")],
        allow_common=True,
    )
    plan = plan_publish(workspace, with_evidence)
    topic = next(write for write in plan.writes if write.path.endswith("demo.md"))
    self.assertIsNone(topic.base_content)
    self.assertIn("# Demo", topic.proposed_content)
```

- [ ] **Step 2: Run tests to verify red state**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_publish.py" -v
```

Expected: FAIL because publish DTOs do not exist.

- [ ] **Step 3: Implement plan DTOs and deterministic hashes**

In `iwiki/publish.py`, define frozen dataclasses:

```python
@dataclass(frozen=True)
class PublishWriteRequest:
    path: str
    content: str

@dataclass(frozen=True)
class PublishRequest:
    writes: list[PublishWriteRequest]
    allow_common: bool = False

@dataclass(frozen=True)
class PlannedWrite:
    path: str
    base_sha256: str | None
    content_sha256: str
    base_content: str | None
    proposed_content: str

@dataclass(frozen=True)
class PublishPlan:
    plan_id: str
    workspace_id: str
    schema_version: int
    scope: str
    writes: list[PlannedWrite]
    warnings: list[str]
    post_actions: list[str]
```

Use SHA-256 of UTF-8 bytes. Sort writes by canonical relative path before computing `plan_id`; compute `plan_id` as SHA-256 of the canonical JSON payload excluding `plan_id`. Reject empty writes, duplicate targets, non-`.md` targets in the first version, mixed scopes, and common writes without `allow_common`.

Before hashing, expand topic writes into navigation writes using these exact rules:

- `wiki/personal/<domain>/<topic>.md` creates or updates `wiki/personal/index.md` with `[[<domain>/index|<Domain>]]` and `wiki/personal/<domain>/index.md` with `[[<domain>/<topic>|<Title>]]`.
- `wiki/common/<domain>/<module>/<topic>.md` creates or updates `wiki/common/index.md` with `[[<domain>/<module>|<Module>]]` and `wiki/common/<domain>/<module>/index.md` with `[[<domain>/<module>/<topic>|<Title>]]`.
- A requested `index.md` does not recursively create another navigation write.
- Link insertion is idempotent and preserves all unrelated index content.
- `<Title>` is the first H1 text, falling back to the file stem; `<Domain>` and `<Module>` use `display_name_from_slug` semantics.

For `common` topic writes, require either an `evidence:` frontmatter key or a non-empty `## Evidence` / `## Sources` section. Run structural Markdown validation on all expanded writes before returning a plan. `base_content` and `proposed_content` are both included so the caller can render a full local diff; no caller needs to reread the target between planning and review.

- [ ] **Step 4: Add the plan-publish CLI command**

Support:

```text
iwiki plan-publish --workspace PATH --request request.json --output plan.json --json
```

The request file shape is:

```json
{
  "allow_common": false,
  "writes": [
    {"path": "wiki/personal/video/demo.md", "content": "# Demo\n"}
  ]
}
```

Write the same plan data returned in the envelope to `--output` using UTF-8 and an atomic temporary-file replacement.

- [ ] **Step 5: Test and commit**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_publish.py" -v
git add iwiki/publish.py iwiki/cli.py tests/test_iwiki_publish.py
git commit -m "feat(iwiki): add publish planning"
```

Expected: planning tests pass.

## Task 6: Apply Publishing Atomically with Conflict and Recovery Journals

**Files:**

- Modify: `iwiki/publish.py`
- Modify: `iwiki/cli.py`
- Modify: `tests/test_iwiki_publish.py`

**Interfaces:**

- Consumes: `PublishPlan` and `confirm_common`.
- Produces: `PublishResult(transaction_id, state, committed_paths, index_state)` and a journal under `.cache/alltonote/transactions/<id>/`.

- [ ] **Step 1: Add failing apply/conflict/common/rollback tests**

Add tests that assert:

```python
def test_apply_rejects_changed_target_without_overwrite(self):
    workspace = self.workspace()
    target = workspace.paths.wiki_personal / "demo.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("base", encoding="utf-8")
    plan = plan_publish(workspace, PublishRequest([PublishWriteRequest("wiki/personal/demo.md", "proposed")]))
    target.write_text("changed in obsidian", encoding="utf-8")
    with self.assertRaises(IWikiError) as raised:
        apply_publish(workspace, plan, confirm_common=False)
    self.assertEqual(raised.exception.code, ErrorCode.CONFLICT)
    self.assertEqual(target.read_text(encoding="utf-8"), "changed in obsidian")

def test_apply_requires_second_common_confirmation(self):
    workspace = self.workspace()
    plan = plan_publish(workspace, PublishRequest([PublishWriteRequest("wiki/common/video/demo.md", "# Demo\n")], True))
    with self.assertRaises(IWikiError):
        apply_publish(workspace, plan, confirm_common=False)

def test_apply_commits_markdown_and_writes_committed_journal(self):
    workspace = self.workspace()
    plan = plan_publish(workspace, PublishRequest([PublishWriteRequest("wiki/personal/demo.md", "# Demo\n")]))
    result = apply_publish(workspace, plan, confirm_common=False, refresh_index=lambda _workspace: "ready")
    self.assertEqual(result.state, "committed")
    self.assertEqual((workspace.paths.wiki_personal / "demo.md").read_text(encoding="utf-8"), "# Demo\n")
    state = workspace.paths.cache / "alltonote/transactions" / result.transaction_id / "state.json"
    self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["state"], "committed")

def test_index_failure_keeps_markdown_and_marks_stale(self):
    workspace = self.workspace()
    plan = plan_publish(workspace, PublishRequest([PublishWriteRequest("wiki/personal/demo.md", "# Demo\n")]))
    result = apply_publish(
        workspace,
        plan,
        confirm_common=False,
        refresh_index=lambda _workspace: (_ for _ in ()).throw(RuntimeError("qmd failed")),
    )
    self.assertEqual(result.index_state, "stale")
    self.assertTrue((workspace.paths.wiki_personal / "demo.md").is_file())
```

- [ ] **Step 2: Run the focused tests to verify red state**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_publish.py" -v
```

Expected: FAIL because `apply_publish` and `PublishResult` are missing.

- [ ] **Step 3: Implement the transaction sequence**

Implement `apply_publish()` in this exact order:

1. Reopen the workspace with `writable=True` and verify `workspace_id`/schema match the plan.
2. Acquire `FileLock(<cache>/alltonote/publish.lock, timeout=10)`.
3. Recompute every target hash; mismatch against `base_sha256` raises `CONFLICT` before any write.
4. Create `<cache>/alltonote/transactions/<plan_id>/` and atomically write `plan.json` plus `state.json` with `state: prepared`.
5. Copy existing targets into `backup/<relative-path>` and update state to `backed_up`.
6. Write every proposed body to a sibling `.<name>.<plan-id>.tmp`, flush, `os.fsync()`, and call `os.replace()`.
7. If a Markdown write fails, restore all backups, remove newly created targets, record `rolled_back`, and raise `RETRYABLE_RUNTIME`.
8. Record `committed` before refreshing the index.
9. Call `refresh_index`; convert any exception to `index_state: stale` without undoing Markdown.
10. Atomically write `result.json` and return `PublishResult`.

Use a helper `_atomic_json(path, payload)` that writes UTF-8 JSON to a sibling temporary file, flushes/fsyncs, and replaces the destination.

- [ ] **Step 4: Add apply-publish CLI parsing**

Support:

```text
iwiki apply-publish --workspace PATH --plan plan.json --json
iwiki apply-publish --workspace PATH --plan plan.json --confirm-common --json
```

Deserialize with explicit field validation. Never accept pickle or executable plan content. Common plans without `--confirm-common` return `permission_denied`/exit 21.

- [ ] **Step 5: Run publish tests and fault-injection tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_publish.py" -v
```

Expected: plan, conflict, common confirmation, committed journal, rollback, and index-stale tests pass.

- [ ] **Step 6: Commit**

Run:

```powershell
git add iwiki/publish.py iwiki/cli.py tests/test_iwiki_publish.py
git commit -m "feat(iwiki): apply publish plans atomically"
```

## Task 7: Parameterize QMD and Add Stable Index Commands

**Files:**

- Modify: `tools/qmd_runtime.py`
- Create: `iwiki/index_service.py`
- Create: `tests/test_iwiki_index.py`
- Modify: `iwiki/cli.py`

**Interfaces:**

- Consumes: explicit `Workspace` and existing QMD behavior.
- Produces: `IndexStatus` and `iwiki index status|refresh|rebuild` without a hardcoded repository root.

- [ ] **Step 1: Write failing path and state tests**

Create `tests/test_iwiki_index.py`:

```python
from pathlib import Path
import json
import tempfile
import unittest

from iwiki.index_service import get_index_status
from iwiki.workspace import open_workspace
from tools.qmd_runtime import qmd_paths


class IWikiIndexTests(unittest.TestCase):
    def test_qmd_paths_are_workspace_relative(self):
        root = Path("C:/knowledge")
        paths = qmd_paths(root)
        self.assertEqual(paths.db, root / ".cache/qmd/llm-iwiki.sqlite")

    def test_missing_db_reports_missing(self):
        workspace = open_workspace(Path(__file__).resolve().parents[1] / "tests/fixtures/workspaces/valid-v2")
        self.assertEqual(get_index_status(workspace).state, "missing")
```

- [ ] **Step 2: Run tests to verify red state**

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_index.py" -v
```

Expected: FAIL because `qmd_paths` and `iwiki.index_service` do not exist.

- [ ] **Step 3: Parameterize QMD paths without breaking existing callers**

Add to `tools/qmd_runtime.py`:

```python
@dataclass(frozen=True)
class QmdPaths:
    root: Path
    cache: Path
    runtime: Path
    config_dir: Path
    config_file: Path
    db: Path
    state: Path


def qmd_paths(workspace_root: Path = REPO_ROOT) -> QmdPaths:
    root = workspace_root.resolve()
    cache = root / ".cache" / "qmd"
    return QmdPaths(
        root=root,
        cache=cache,
        runtime=cache / "runtime",
        config_dir=cache / "config",
        config_file=cache / "config" / f"{QMD_INDEX_NAME}.yml",
        db=cache / f"{QMD_INDEX_NAME}.sqlite",
        state=cache / "state.json",
    )
```

Then add optional `workspace_root: Path = REPO_ROOT` parameters to configuration, bootstrap, refresh, search, and URI-resolution entry points. Existing tests must continue passing with defaults.

- [ ] **Step 4: Implement the stable index facade**

Create `iwiki/index_service.py` with the following implementation, and add an explicit keyword-only `workspace_root: Path = REPO_ROOT` parameter to `mark_qmd_state` so state is written under the selected workspace:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil

from iwiki.workspace import Workspace
from tools.qmd_runtime import (
    bootstrap_qmd,
    load_qmd_state,
    mark_qmd_state,
    qmd_paths,
    refresh_qmd_index,
)


@dataclass(frozen=True)
class IndexStatus:
    state: str
    backend: str
    database_path: str
    last_success_at: str | None
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _newest_markdown_mtime(workspace: Workspace) -> float:
    roots = (workspace.paths.wiki_common, workspace.paths.wiki_personal)
    mtimes = [path.stat().st_mtime for root in roots if root.exists() for path in root.rglob("*.md")]
    return max(mtimes, default=0.0)


def get_index_status(workspace: Workspace) -> IndexStatus:
    paths = qmd_paths(workspace.root)
    state = load_qmd_state(workspace_root=workspace.root)
    last_success = state.get("last_success_at")
    error = state.get("error")
    if state.get("building") is True:
        value = "building"
    elif isinstance(error, str) and error:
        value = "failed"
    elif not paths.db.is_file():
        value = "missing"
    elif isinstance(last_success, str):
        indexed_at = datetime.fromisoformat(last_success).timestamp()
        value = "stale" if _newest_markdown_mtime(workspace) > indexed_at else "ready"
    else:
        value = "stale"
    return IndexStatus(value, "qmd", workspace.relative(paths.db), last_success if isinstance(last_success, str) else None, error if isinstance(error, str) else None)


def _run_index_operation(workspace: Workspace, operation) -> IndexStatus:
    mark_qmd_state(workspace_root=workspace.root, building=True, error=None)
    try:
        operation()
    except Exception as error:
        mark_qmd_state(workspace_root=workspace.root, building=False, error=str(error))
        raise
    mark_qmd_state(
        workspace_root=workspace.root,
        building=False,
        error=None,
        last_success_at=datetime.now(timezone.utc).isoformat(),
    )
    return get_index_status(workspace)


def refresh_index(workspace: Workspace) -> IndexStatus:
    return _run_index_operation(
        workspace,
        lambda: refresh_qmd_index(workspace_root=workspace.root, embed_mode="auto"),
    )


def rebuild_index(workspace: Workspace) -> IndexStatus:
    paths = qmd_paths(workspace.root)
    if paths.cache.exists():
        paths.cache.resolve().relative_to(workspace.root)
        shutil.rmtree(paths.cache)
    return _run_index_operation(
        workspace,
        lambda: bootstrap_qmd(workspace_root=workspace.root, skip_embed=False),
    )
```

State mapping is exact: no DB = `missing`; state file says active = `building`; last command success and DB exists = `ready`; Markdown newest mtime exceeds recorded success = `stale`; recorded error = `failed`.

- [ ] **Step 5: Add CLI index commands and run tests**

Support:

```text
iwiki index status --workspace PATH --json
iwiki index refresh --workspace PATH --json
iwiki index rebuild --workspace PATH --json
```

Run:

```powershell
python -m unittest discover -s tests -p "test_iwiki_index.py" -v
python -m unittest discover -s tests -p "test_qmd_integration.py" -v
```

Expected: new index tests and all existing QMD tests pass.

- [ ] **Step 6: Commit**

Run:

```powershell
git add tools/qmd_runtime.py iwiki/index_service.py iwiki/cli.py tests/test_iwiki_index.py tests/test_qmd_integration.py
git commit -m "feat(iwiki): expose stable index commands"
```

## Task 8: Golden Contract, Documentation, and Release Gate

**Files:**

- Create: `tests/golden/inspect-v1.json`
- Create: `tests/golden/validate-v1.json`
- Modify: `tests/test_iwiki_cli.py`
- Modify: `README.md`
- Modify: `docs/wiki-architecture-v2.md`

**Interfaces:**

- Consumes: all public CLI commands.
- Produces: consumer-visible frozen examples and the Phase 1 release gate.

- [ ] **Step 1: Add golden response tests**

Normalize environment-dependent workspace paths to `<WORKSPACE>` and compare inspect/validate responses byte-for-byte with golden JSON. The golden inspect capabilities must be:

```json
["inspect","validate","query_native","plan_publish","atomic_publish","qmd_index"]
```

Also test that stderr is empty on success and stdout contains exactly one JSON document.

- [ ] **Step 2: Document installation and automation commands**

Add to `README.md`:

```markdown
## Stable iwiki automation interface

Install the local runtime with `python -m pip install -e .`.

- `iwiki inspect --workspace <path> --json`
- `iwiki validate --workspace <path> --json`
- `iwiki query --workspace <path> --scope common --text <query> --json`
- `iwiki plan-publish --workspace <path> --request <request.json> --output <plan.json> --json`
- `iwiki apply-publish --workspace <path> --plan <plan.json> --json`
- `iwiki index status --workspace <path> --json`

The disk schema and CLI protocol are versioned independently. Consumers must use `inspect.capabilities` instead of inferring features from a package version.
```

Extend `docs/wiki-architecture-v2.md` with the manifest fields, read-only behavior for newer schema versions, publish preconditions, transaction journal path, and stable error-code table.

- [ ] **Step 3: Run the full release gate**

Run:

```powershell
python -m pip install -e .
python -m unittest discover -s tests -p "test_*.py" -v
iwiki inspect --workspace . --json | ConvertFrom-Json | Out-Null
iwiki validate --workspace . --json | ConvertFrom-Json | Out-Null
git diff --check
```

Expected: full suite passes, both commands emit parseable JSON, and diff check exits 0.

- [ ] **Step 4: Verify publish security invariants**

Run the focused tests by discovery pattern:

```powershell
python -m unittest discover -s tests -p "test_manifest_contract.py" -v
python -m unittest discover -s tests -p "test_iwiki_publish.py" -v
```

Expected: absolute/traversal/reserved paths, common confirmation, hash conflicts, rollback, and index-stale behavior all pass.

- [ ] **Step 5: Commit documentation and goldens**

Run:

```powershell
git add README.md docs/wiki-architecture-v2.md tests/golden tests/test_iwiki_cli.py
git commit -m "docs(iwiki): freeze cli protocol v1"
```

- [ ] **Step 6: Record the consumer base**

Run:

```powershell
git status --short
git log --oneline --decorate -8
iwiki inspect --workspace . --json
```

Expected: clean worktree; inspect reports `schema_version: 2`, `cli_protocol_version: 1`, and all six capabilities. AllToNote implementation must pin or bundle a build from this exact commit or later compatible release.
