# llm-iwiki Portable Contract Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 llm-iwiki 中发布 Portable Contract v1、Reference Validator、稳定 SDK 和 `iwiki portable validate/commit`，使任意合规 Producer 能把 staging Source Bundle 安全、原子地提交到 Workspace。

**Architecture:** llm-iwiki 新增独立 `iwiki.portable` 包，公开不可变合同信息、严格文件读取、路径/树校验、四级 Bundle Validator 和 PreparedBundle 提交接口。AllToNote 不在本计划中修改；未来只消费稳定 SDK。CLI 是相同 Contract Engine 的适配器，不复制验证或提交逻辑。

**Tech Stack:** Python 3.11+、stdlib `argparse` / `ctypes` / `dataclasses` / `enum` / `hashlib` / `importlib.resources` / `json` / `os` / `pathlib` / `stat` / `uuid`，现有 `filelock>=3.18,<4`、`PyYAML>=6.0,<7`、`unittest`。本计划不新增运行时依赖。

## Global Constraints

- 规范来源：`G:\AllToNote\docs\superpowers\specs\2026-07-14-alltonote-portable-artifact-source-bundle-design.md`，提交 `91818f7`。
- 目标仓库：`E:\Agent_Learning\llm-iwiki`；实现基线：`codex/iwiki-stable-cli` 的提交 `2b6db85`。
- 执行时使用 `superpowers:using-git-worktrees`，创建 `codex/iwiki-portable-contract-v1` 和新的隔离 worktree；不得修改有用户改动的主工作树，也不得直接复用规划时的 `iwiki-stable-cli` 工作树。
- 规划时已验证基线：`python -m unittest discover -s tests -p "test_*.py" -v` 为 332 tests OK、6 skipped；实现开始后必须在新 worktree 再跑一次。
- Workspace Schema 保持 `2`，CLI Protocol 保持 `1`；本计划只做 additive capability，不改变既有 envelope 语义。
- Portable SDK API 初始版本为 `1`；Bundle、Source、SourceRevision、Artifact、EvidenceRef、Receipt、Transcript、EvidenceSet、QualityReport 和 Commit Protocol 初始版本均为 `1`。
- 所有控制文件为 UTF-8、无 BOM、LF、文件末尾 LF；哈希格式为 `sha256:<64 lowercase hex>`。
- v1 payload representation 只允许 `bundle_file`；不实现 CAS、`workspace_blob`、签名、加密、导入导出、迁移、trash、Publisher 附件、Engine、MCP 或 AllToNote Producer。
- Bundle candidate 必须位于 Workspace 返回的 `<raw_personal>/.staging/<local-instance>/<job>.<nonce>/bundle.partial`；final 位于 `<raw_personal>/bundles/<bundle_id>`。SDK/CLI 中 staging reference 使用完整 Workspace-relative path，不接受绝对路径或省略前缀的别名。
- staging candidate 必须没有 `commit.json`；committed Bundle 必须有有效 `commit.json`。
- final 不覆盖、不 merge；相同 Bundle ID + 相同 manifest hash 为幂等成功，不同 hash 为冲突。
- commit 必须同卷、no-replace atomic directory rename；失败时禁止逐文件 copy fallback。
- Windows 是 P0 必须通过的发布平台；macOS adapter 同期实现，但只有在真实 APFS runner 通过相同 fault suite 后才标记为正式支持。Linux adapter 用于开发/CI，不扩大当前产品承诺。
- SDK 与 CLI 调用同一 Contract Engine；CLI handler 不包含验证或文件 mutation 业务。
- `iwiki inspect` 不加载或哈希整个 Workspace，只读取小型 packaged contract catalog。
- 正常 `inspect/query/index` 冷路径不得导入 Portable Validator 的重型扫描模块。
- 所有错误继续使用现有 `IWikiError` / `ErrorCode`；Bundle 细节通过稳定 validation issue code 表达，不新增未经设计的顶层 exit-code 分类。
- Validation issue message 只描述规则，不回显正文、Transcript、Source URL 查询串、参数摘要或绝对磁盘路径；机器调用以稳定 code/path 为准。
- 每个任务遵循 TDD：先写失败测试、确认正确红态、最小实现、聚焦测试、完整回归、单独 commit。
- 每次 commit 前运行 `git diff --check`；不得带入 raw/wiki 内容变化、用户文件或无关重构。

### Assumptions and explicit trade-offs

- **Why manual Reference Validator:** public JSON Schema remains the interchange declaration, but exact bytes, duplicate JSON keys, filesystem links, dependency closure and atomic rename are outside ordinary JSON-Schema scope. P0 therefore keeps one explicit validator with no new runtime library and locks schema/validator agreement through golden and mutation tests.
- **Why no SQLite/CAS/index in P0:** Bundle truth is ordinary files plus hashes. Any cache/index remains rebuildable Machine State; adding storage indirection now would weaken openness and enlarge the first compatibility surface.
- **Why PreparedBundle is process-local:** authorization after validation depends on live file identities/handles. Serializing it would falsely imply that a validation decision survives process restart or file mutation.
- **Why Windows and POSIX differ:** Windows can deny write sharing while preserving directory rename; macOS/Linux advisory locks require a compliant Writer contract plus identity recheck. The documented guarantee matches what each kernel primitive can actually enforce.
- **Why AllToNote is untouched:** this phase first makes llm-iwiki a stable contract authority. AllToNote Producer integration starts only after a fake/manual Producer passes the same public SDK and CLI, preventing private coupling from becoming the de facto format.

---

## File Responsibility Map

以下路径均相对于新的 llm-iwiki 实现 worktree。

### 新增运行时代码

- `iwiki/portable/__init__.py` — 稳定 SDK 唯一导出面。
- `iwiki/portable/contract.py` — capability、版本、locator、packaged schema catalog 和 contract fingerprint。
- `iwiki/portable/types.py` — `PortableContractInfo`、`PortableBundleRef`、validation DTO。
- `iwiki/portable/jsonio.py` — 严格 UTF-8 JSON/JSONL、duplicate-key、LF、hash 和原子小控制文件写入。
- `iwiki/portable/path_policy.py` — typed ID、digest、POSIX 相对路径、Windows alias、树扫描和文件身份。
- `iwiki/portable/validator.py` — Structure、Integrity、Closure、Semantic 编排和 manifest/reference 校验。
- `iwiki/portable/content_validation.py` — Transcript、EvidenceSet、QualityReport、Draft citation/resource 语义。
- `iwiki/portable/commit.py` — PreparedBundle、文件句柄/identity seal、Workspace mutation lock、`commit.json`、fsync 和 no-replace rename。
- `iwiki/portable/contracts/v1/*.schema.json` — 公开 JSON Schema 2020-12 文件。
- `iwiki/portable/contracts/v1/schema-set.json` — schema set ID 和有序文件清单。

### 修改运行时代码

- `pyproject.toml` — 把 contract JSON 作为 wheel package data。
- `iwiki/cli.py` — additive inspect contract、`portable validate`、`portable commit` parser/dispatch。
- `README.md` — 安装、能力协商和 CLI 用法。
- `docs/wiki-architecture-v2.md` — Workspace raw personal Bundle 合同入口，不复制完整下位规范。

### 新增/修改测试

- `tests/portable_fixture_factory.py` — 确定性最小 Workspace/Bundle fixture builder；Artifact 直接位于 `drafts/`、`sources/`、`evidence/` 等合同目录，不发明额外 `payload/` 层。
- `tests/test_portable_contract.py` — schema catalog、fingerprint、SDK DTO、wheel resource。
- `tests/test_portable_jsonio.py` — BOM/CRLF/final LF/duplicate key/JSONL/hash。
- `tests/test_portable_path_policy.py` — ID/path/tree/link/hardlink/collision/containment。
- `tests/test_portable_validation.py` — Structure/Integrity/Closure。
- `tests/test_portable_semantics.py` — Source/Revision/Artifact/Transcript/Evidence/Draft/Quality/Receipt。
- `tests/test_portable_commit.py` — prepare、identity、cancel-authorizer hook、lock、fsync、atomic rename、crash points、idempotency/conflict。
- `tests/test_portable_cli.py` — subprocess JSON、exit code、sanitization、installed CLI。
- `tests/test_iwiki_cli.py` — inspect golden capability 更新和旧命令回归。
- `tests/test_iwiki_packaging.py` — wheel 必须携带 contract JSON，且 installed SDK/CLI 可离线运行。
- `tests/golden/inspect-v1.json` — additive Portable contract response。
- `tests/fixtures/portable/v1/valid-minimal/**` — committed 最小 golden Bundle。
- `tests/fixtures/portable/v1/invalid/**` — 每类稳定 issue code 的最小非法 fixture。
- `docs/portable-contract-v1.md` — 第三方 Producer 可独立实现的公开说明。

## Public Interfaces Frozen by This Plan

```python
from iwiki.portable import (
    BundleState,
    CommitResult,
    PortableBundleRef,
    PortableContractInfo,
    PortableValidationIssue,
    PortableValidationReport,
    PreparedBundle,
    ValidationLevel,
    commit_prepared_bundle,
    inspect_portable_contract,
    prepare_bundle_commit,
    validate_bundle,
)
```

Exact signatures：

```python
def inspect_portable_contract(workspace: Workspace) -> PortableContractInfo: ...

def validate_bundle(
    workspace: Workspace,
    bundle_ref: PortableBundleRef,
    level: ValidationLevel = ValidationLevel.SEMANTIC,
) -> PortableValidationReport: ...

def prepare_bundle_commit(
    workspace: Workspace,
    staging_ref: PortableBundleRef,
    *,
    expected_bundle_id: str,
    expected_manifest_sha256: str,
) -> PreparedBundle: ...

def commit_prepared_bundle(prepared: PreparedBundle) -> CommitResult: ...
```

DTO 字段：

```python
class ValidationLevel(StrEnum):
    STRUCTURE = "structure"
    INTEGRITY = "integrity"
    CLOSURE = "closure"
    SEMANTIC = "semantic"

class BundleState(StrEnum):
    STAGING = "staging"
    COMMITTED = "committed"

@dataclass(frozen=True)
class PortableBundleRef:
    state: BundleState
    value: str

@dataclass(frozen=True)
class PortableValidationIssue:
    level: ValidationLevel
    code: str
    path: str
    message: str

@dataclass(frozen=True)
class PortableValidationReport:
    valid: bool
    level: ValidationLevel
    state: BundleState
    bundle_id: str | None
    manifest_sha256: str | None
    issues: tuple[PortableValidationIssue, ...]

@dataclass(frozen=True)
class CommitResult:
    bundle_id: str
    manifest_sha256: str
    commit_sha256: str
    relative_path: str
    idempotent: bool
```

`PreparedBundle` 是 process-local、不可序列化、single-use、context-manager 对象。公开只读属性为 `bundle_id`、`manifest_sha256`、`staging_relative_path`；内部句柄和 inventory 不属于稳定 SDK。

## Stable Validation Issue Codes

本计划冻结以下 issue code；测试按 code 断言，不解析 message：

```text
missing_control_file
unexpected_commit_file
invalid_utf8
invalid_line_endings
missing_final_lf
duplicate_json_key
invalid_json
unsupported_schema
invalid_bundle_id
bundle_directory_mismatch
invalid_typed_id
duplicate_typed_id
invalid_digest
invalid_timestamp
invalid_relative_path
path_collision
linked_or_reparse_entry
hardlinked_payload
non_regular_payload
undeclared_file
declared_file_missing
byte_length_mismatch
hash_mismatch
receipt_binding_invalid
commit_binding_invalid
dependency_missing
dependency_manifest_mismatch
dependency_cycle
reference_target_missing
reference_hash_mismatch
required_contract_unsupported
source_invalid
source_revision_invalid
artifact_invalid
output_invalid
transcript_invalid
evidence_set_invalid
evidence_locator_invalid
draft_citation_missing
draft_resource_invalid
quality_report_invalid
receipt_invalid
```

---

## Task 1: Publish the Contract Catalog, DTOs, and Packaged Schema Set

**Files:**

- Create: `iwiki/portable/__init__.py`
- Create: `iwiki/portable/types.py`
- Create: `iwiki/portable/contract.py`
- Create: `iwiki/portable/contracts/v1/schema-set.json`
- Create: `iwiki/portable/contracts/v1/core.schema.json`
- Create: `iwiki/portable/contracts/v1/source.schema.json`
- Create: `iwiki/portable/contracts/v1/source-revision.schema.json`
- Create: `iwiki/portable/contracts/v1/artifact.schema.json`
- Create: `iwiki/portable/contracts/v1/evidence-ref.schema.json`
- Create: `iwiki/portable/contracts/v1/receipt.schema.json`
- Create: `iwiki/portable/contracts/v1/commit.schema.json`
- Create: `iwiki/portable/contracts/v1/transcript.schema.json`
- Create: `iwiki/portable/contracts/v1/evidence-set.schema.json`
- Create: `iwiki/portable/contracts/v1/quality-report.schema.json`
- Create: `iwiki/portable/contracts/v1/bundle.schema.json`
- Modify: `pyproject.toml`
- Create: `tests/test_portable_contract.py`

**Interfaces:**

- Consumes: existing `Workspace`; Python package resources.
- Produces: all frozen DTOs, version constants, ordered schema bytes, contract fingerprint and `inspect_portable_contract()` for Tasks 3–9.

- [ ] **Step 1: Create the isolated implementation worktree and verify the baseline**

Run from `E:\Agent_Learning\llm-iwiki` without modifying its files:

```powershell
git worktree add `
  'E:\Agent_Learning\.worktrees\llm-iwiki\iwiki-portable-contract-v1' `
  -b codex/iwiki-portable-contract-v1 2b6db85
Set-Location 'E:\Agent_Learning\.worktrees\llm-iwiki\iwiki-portable-contract-v1'
python -m unittest discover -s tests -p "test_*.py" -v
git status --short
```

Expected: 332 tests OK、6 skipped；`git status --short` 为空。若基线不同，停止执行并记录差异，不在脏基线上继续。

- [ ] **Step 2: Write the failing contract catalog tests**

Create `tests/test_portable_contract.py` with these tests:

```python
import hashlib
import json
from pathlib import Path
import unittest

from iwiki.portable import inspect_portable_contract
from iwiki.portable.contract import (
    PORTABLE_CAPABILITIES,
    PORTABLE_CONTRACT_ID,
    PORTABLE_SCHEMA_SET_ID,
    PORTABLE_VALIDATION_ISSUE_CODES,
    schema_entries,
    schema_set_sha256,
)
from iwiki.workspace import open_workspace


FIXTURE = Path(__file__).parent / "fixtures" / "workspaces" / "valid-v2"


class PortableContractTests(unittest.TestCase):
    def test_contract_identity_and_capabilities_are_frozen(self):
        self.assertEqual(PORTABLE_CONTRACT_ID, "iwiki-portable-contract-v1")
        self.assertEqual(PORTABLE_SCHEMA_SET_ID, "2026-07-portable-v1")
        self.assertEqual(
            PORTABLE_CAPABILITIES,
            ("portable_bundle_validate_v1", "portable_bundle_commit_v1"),
        )
        self.assertEqual(len(PORTABLE_VALIDATION_ISSUE_CODES), 42)
        self.assertEqual(
            len(PORTABLE_VALIDATION_ISSUE_CODES),
            len(set(PORTABLE_VALIDATION_ISSUE_CODES)),
        )

    def test_schema_catalog_is_sorted_parseable_and_fingerprinted(self):
        entries = schema_entries()
        names = [name for name, _ in entries]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], "artifact.schema.json")
        self.assertEqual(names[-1], "transcript.schema.json")
        for name, content in entries:
            self.assertTrue(content.endswith(b"\n"), name)
            self.assertNotIn(b"\r", content, name)
            json.loads(content.decode("utf-8"))

        digest = hashlib.sha256()
        for name, content in entries:
            encoded_name = name.encode("utf-8")
            digest.update(len(encoded_name).to_bytes(4, "big"))
            digest.update(encoded_name)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        self.assertEqual(schema_set_sha256(), f"sha256:{digest.hexdigest()}")
        self.assertEqual(
            schema_set_sha256(),
            "sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9",
        )

    def test_every_packaged_urn_reference_resolves_inside_the_schema_set(self):
        documents = [json.loads(content) for _, content in schema_entries()]
        known_ids = {document["$id"] for document in documents}

        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value and value["$ref"].startswith("urn:"):
                    self.assertIn(value["$ref"].split("#", 1)[0], known_ids)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for document in documents:
            walk(document)

    def test_inspect_contract_returns_stable_ranges(self):
        info = inspect_portable_contract(open_workspace(FIXTURE))
        payload = info.to_dict()
        self.assertEqual(payload["iwiki_sdk_api_version"], 1)
        self.assertEqual(payload["contract_id"], PORTABLE_CONTRACT_ID)
        self.assertEqual(payload["schema_set_id"], PORTABLE_SCHEMA_SET_ID)
        self.assertEqual(payload["bundle_schema_versions"], [1])
        self.assertEqual(payload["transcript_schema_versions"], [1])
        self.assertEqual(payload["evidence_set_schema_versions"], [1])
        self.assertEqual(payload["quality_report_schema_versions"], [1])
        self.assertEqual(payload["commit_protocol_versions"], [1])
        self.assertIn("video-time-range.v1", payload["locator_schemes"])
        self.assertIn("git-file-lines.v1", payload["locator_schemes"])
        self.assertEqual(payload["supported_required_contracts"], [])
        self.assertEqual(
            payload["validation_levels"],
            ["structure", "integrity", "closure", "semantic"],
        )
        self.assertEqual(payload["schema_set_sha256"], schema_set_sha256())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests and verify the intended red state**

Run:

```powershell
python -m unittest tests.test_portable_contract -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'iwiki.portable'`.

- [ ] **Step 4: Implement the frozen DTOs**

Create `iwiki/portable/types.py`:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class ValidationLevel(StrEnum):
    STRUCTURE = "structure"
    INTEGRITY = "integrity"
    CLOSURE = "closure"
    SEMANTIC = "semantic"


class BundleState(StrEnum):
    STAGING = "staging"
    COMMITTED = "committed"


@dataclass(frozen=True)
class PortableBundleRef:
    state: BundleState
    value: str

    @classmethod
    def staging(cls, relative_path: str) -> "PortableBundleRef":
        return cls(BundleState.STAGING, relative_path)

    @classmethod
    def committed(cls, bundle_id: str) -> "PortableBundleRef":
        return cls(BundleState.COMMITTED, bundle_id)


@dataclass(frozen=True)
class PortableValidationIssue:
    level: ValidationLevel
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level.value,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class PortableValidationReport:
    valid: bool
    level: ValidationLevel
    state: BundleState
    bundle_id: str | None
    manifest_sha256: str | None
    issues: tuple[PortableValidationIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "level": self.level.value,
            "state": self.state.value,
            "bundle_id": self.bundle_id,
            "manifest_sha256": self.manifest_sha256,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class PortableContractInfo:
    iwiki_sdk_api_version: int
    contract_id: str
    schema_set_id: str
    schema_set_sha256: str
    bundle_schema_versions: tuple[int, ...]
    source_schema_versions: tuple[int, ...]
    source_revision_schema_versions: tuple[int, ...]
    artifact_schema_versions: tuple[int, ...]
    evidence_ref_schema_versions: tuple[int, ...]
    receipt_schema_versions: tuple[int, ...]
    transcript_schema_versions: tuple[int, ...]
    evidence_set_schema_versions: tuple[int, ...]
    quality_report_schema_versions: tuple[int, ...]
    commit_protocol_versions: tuple[int, ...]
    locator_schemes: tuple[str, ...]
    supported_required_contracts: tuple[str, ...]
    validation_levels: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for field in (
            "bundle_schema_versions",
            "source_schema_versions",
            "source_revision_schema_versions",
            "artifact_schema_versions",
            "evidence_ref_schema_versions",
            "receipt_schema_versions",
            "transcript_schema_versions",
            "evidence_set_schema_versions",
            "quality_report_schema_versions",
            "commit_protocol_versions",
            "locator_schemes",
            "supported_required_contracts",
            "validation_levels",
        ):
            payload[field] = list(payload[field])
        return payload


@dataclass(frozen=True)
class CommitResult:
    bundle_id: str
    manifest_sha256: str
    commit_sha256: str
    relative_path: str
    idempotent: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 5: Implement the contract catalog and deterministic fingerprint**

Create `iwiki/portable/contract.py`:

```python
from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from typing import Final

from iwiki.portable.types import PortableContractInfo
from iwiki.workspace import Workspace
from iwiki.errors import ErrorCode, IWikiError


PORTABLE_SDK_API_VERSION: Final = 1
PORTABLE_CONTRACT_ID: Final = "iwiki-portable-contract-v1"
PORTABLE_SCHEMA_SET_ID: Final = "2026-07-portable-v1"
PORTABLE_CAPABILITIES: Final = (
    "portable_bundle_validate_v1",
    "portable_bundle_commit_v1",
)
PORTABLE_VALIDATION_ISSUE_CODES: Final = (
    "missing_control_file",
    "unexpected_commit_file",
    "invalid_utf8",
    "invalid_line_endings",
    "missing_final_lf",
    "duplicate_json_key",
    "invalid_json",
    "unsupported_schema",
    "invalid_bundle_id",
    "bundle_directory_mismatch",
    "invalid_typed_id",
    "duplicate_typed_id",
    "invalid_digest",
    "invalid_timestamp",
    "invalid_relative_path",
    "path_collision",
    "linked_or_reparse_entry",
    "hardlinked_payload",
    "non_regular_payload",
    "undeclared_file",
    "declared_file_missing",
    "byte_length_mismatch",
    "hash_mismatch",
    "receipt_binding_invalid",
    "commit_binding_invalid",
    "dependency_missing",
    "dependency_manifest_mismatch",
    "dependency_cycle",
    "reference_target_missing",
    "reference_hash_mismatch",
    "required_contract_unsupported",
    "source_invalid",
    "source_revision_invalid",
    "artifact_invalid",
    "output_invalid",
    "transcript_invalid",
    "evidence_set_invalid",
    "evidence_locator_invalid",
    "draft_citation_missing",
    "draft_resource_invalid",
    "quality_report_invalid",
    "receipt_invalid",
)
LOCATOR_SCHEMES: Final = (
    "video-time-range.v1",
    "audio-time-range.v1",
    "text-span.v1",
    "document-page.v1",
    "presentation-slide.v1",
    "web-section.v1",
    "wiki-revision-section.v1",
    "git-file-lines.v1",
    "code-symbol.v1",
    "git-commit.v1",
    "record-id.v1",
)
SUPPORTED_REQUIRED_CONTRACTS: Final[tuple[str, ...]] = ()
VALIDATION_LEVELS: Final = ("structure", "integrity", "closure", "semantic")


def schema_entries() -> tuple[tuple[str, bytes], ...]:
    root = files("iwiki.portable").joinpath("contracts", "v1")
    catalog_bytes = root.joinpath("schema-set.json").read_bytes()
    if (
        catalog_bytes.startswith(b"\xef\xbb\xbf")
        or b"\r" in catalog_bytes
        or not catalog_bytes.endswith(b"\n")
        or catalog_bytes.endswith(b"\n\n")
    ):
        raise RuntimeError("portable schema catalog bytes are not canonical")
    catalog = json.loads(catalog_bytes.decode("utf-8"))
    if catalog.get("contract_id") != PORTABLE_CONTRACT_ID:
        raise RuntimeError("portable contract id does not match the catalog")
    if catalog.get("schema_set_id") != PORTABLE_SCHEMA_SET_ID:
        raise RuntimeError("portable schema set id does not match the catalog")
    names = tuple(catalog["schemas"])
    if names != tuple(sorted(names)) or len(names) != len(set(names)):
        raise RuntimeError("portable schema catalog is not canonical")
    entries = tuple((name, root.joinpath(name).read_bytes()) for name in names)
    if any(
        content.startswith(b"\xef\xbb\xbf")
        or b"\r" in content
        or not content.endswith(b"\n")
        or content.endswith(b"\n\n")
        for _, content in entries
    ):
        raise RuntimeError("portable schema bytes are not canonical")
    return entries


def schema_set_sha256() -> str:
    digest = hashlib.sha256()
    for name, content in schema_entries():
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def inspect_portable_contract(workspace: Workspace) -> PortableContractInfo:
    if not isinstance(workspace, Workspace):
        raise IWikiError(ErrorCode.INVALID_ARGUMENT, "workspace must be a Workspace")
    return PortableContractInfo(
        iwiki_sdk_api_version=PORTABLE_SDK_API_VERSION,
        contract_id=PORTABLE_CONTRACT_ID,
        schema_set_id=PORTABLE_SCHEMA_SET_ID,
        schema_set_sha256=schema_set_sha256(),
        bundle_schema_versions=(1,),
        source_schema_versions=(1,),
        source_revision_schema_versions=(1,),
        artifact_schema_versions=(1,),
        evidence_ref_schema_versions=(1,),
        receipt_schema_versions=(1,),
        transcript_schema_versions=(1,),
        evidence_set_schema_versions=(1,),
        quality_report_schema_versions=(1,),
        commit_protocol_versions=(1,),
        locator_schemes=LOCATOR_SCHEMES,
        supported_required_contracts=SUPPORTED_REQUIRED_CONTRACTS,
        validation_levels=VALIDATION_LEVELS,
    )
```

The fingerprint intentionally frames each cataloged schema filename and exact byte body in sorted catalog order. `schema-set.json` itself is not included, avoiding a self-referential digest; its `contract_id`, `schema_set_id`, sorted unique filenames and final-LF JSON bytes are validated separately. The frozen v1 digest for the exact schema blocks in this task is `sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9`.

- [ ] **Step 6: Add the schema-set catalog and core schema**

Create `iwiki/portable/contracts/v1/schema-set.json` exactly as:

```json
{
  "contract_id": "iwiki-portable-contract-v1",
  "schema_set_id": "2026-07-portable-v1",
  "schemas": [
    "artifact.schema.json",
    "bundle.schema.json",
    "commit.schema.json",
    "core.schema.json",
    "evidence-ref.schema.json",
    "evidence-set.schema.json",
    "quality-report.schema.json",
    "receipt.schema.json",
    "source-revision.schema.json",
    "source.schema.json",
    "transcript.schema.json"
  ]
}
```

Create `iwiki/portable/contracts/v1/core.schema.json` with exact reusable definitions:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:core:v1",
  "$defs": {
    "timestamp": {
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}Z$"
    },
    "digest": {
      "type": "string",
      "pattern": "^sha256:[0-9a-f]{64}$"
    },
    "relativePath": {
      "type": "string",
      "minLength": 1,
      "pattern": "^(?!/)(?!.*(?:^|/)\\.\\.?/)(?!.*\\\\)(?!.*:)[^\\u0000]+$"
    },
    "bundleId": {"type": "string", "pattern": "^bnd_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "sourceId": {"type": "string", "pattern": "^src_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "revisionId": {"type": "string", "pattern": "^rev_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "artifactId": {"type": "string", "pattern": "^art_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "evidenceId": {"type": "string", "pattern": "^ev_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "runId": {"type": "string", "pattern": "^run_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "externalRefId": {"type": "string", "pattern": "^ext_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "artifactRef": {
      "type": "object",
      "additionalProperties": false,
      "required": ["bundle_id", "artifact_id", "sha256"],
      "properties": {
        "bundle_id": {"$ref": "#/$defs/bundleId"},
        "artifact_id": {"$ref": "#/$defs/artifactId"},
        "sha256": {"$ref": "#/$defs/digest"}
      }
    },
    "sourceRevisionRef": {
      "type": "object",
      "additionalProperties": false,
      "required": ["bundle_id", "source_revision_id"],
      "properties": {
        "bundle_id": {"$ref": "#/$defs/bundleId"},
        "source_revision_id": {"$ref": "#/$defs/revisionId"}
      }
    },
    "extensions": {
      "type": "object",
      "propertyNames": {"pattern": "^[a-z0-9][a-z0-9.-]*:[A-Za-z0-9_.-]+$"}
    }
  }
}
```

- [ ] **Step 7: Add the object schemas**

Create the remaining schema files with the exact required fields below. Keep every file UTF-8/LF/final-LF and `additionalProperties: false` at the core object level.

`source.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:source:v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["source_schema_version", "source_id", "source_kind", "canonical_identity", "display", "extensions"],
  "properties": {
    "source_schema_version": {"const": 1},
    "source_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/sourceId"},
    "source_kind": {"type": "string", "minLength": 1},
    "canonical_identity": {
      "type": "object",
      "additionalProperties": false,
      "required": ["scheme", "value"],
      "properties": {"scheme": {"type": "string", "minLength": 1}, "value": {"type": "string", "minLength": 1}}
    },
    "display": {"type": "object"},
    "extensions": {"type": "object"}
  }
}
```

`source-revision.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:source-revision:v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["source_revision_schema_version", "source_revision_id", "source_ref", "captured_at", "observed_revision", "content_digest", "materialization", "license", "privacy", "freshness", "extensions"],
  "properties": {
    "source_revision_schema_version": {"const": 1},
    "source_revision_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/revisionId"},
    "source_ref": {"type": "object", "required": ["bundle_id", "source_id"], "additionalProperties": false, "properties": {"bundle_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/bundleId"}, "source_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/sourceId"}}},
    "captured_at": {"type": "string"},
    "observed_revision": {"type": "object"},
    "content_digest": {
      "oneOf": [
        {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        {"type": "object", "additionalProperties": false, "required": ["unavailable_reason", "reason_version"], "properties": {"unavailable_reason": {"type": "string", "minLength": 1}, "reason_version": {"type": "integer", "minimum": 1}}}
      ]
    },
    "materialization": {
      "oneOf": [
        {"type": "object", "additionalProperties": false, "required": ["kind", "artifact_ref"], "properties": {"kind": {"const": "archived"}, "artifact_ref": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactRef"}}},
        {"type": "object", "additionalProperties": false, "required": ["kind", "reason_code"], "properties": {"kind": {"const": "reference_only"}, "reason_code": {"type": "string", "minLength": 1}}},
        {"type": "object", "additionalProperties": false, "required": ["kind", "external_ref_id"], "properties": {"kind": {"const": "external_local"}, "external_ref_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/externalRefId"}}}
      ]
    },
    "license": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status", "archive_permission"],
      "properties": {
        "status": {"enum": ["known", "unknown", "restricted"]},
        "archive_permission": {"enum": ["allowed", "disallowed", "unknown"]},
        "identifier": {"type": "string"},
        "source_note": {"type": "string"},
        "user_confirmation_ref": {"type": "string"}
      }
    },
    "privacy": {"enum": ["public", "personal", "sensitive", "confidential", "unknown"]},
    "freshness": {"type": "object"},
    "extensions": {"type": "object"}
  }
}
```

`artifact.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:artifact:v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["artifact_schema_version", "artifact_id", "artifact_type", "payload", "created_at", "parents", "source_revision_refs", "generated_by", "generation", "quality_report_refs", "extensions"],
  "properties": {
    "artifact_schema_version": {"const": 1},
    "artifact_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactId"},
    "artifact_type": {"type": "string", "minLength": 1},
    "payload": {"type": "object", "additionalProperties": false, "required": ["representation", "path", "media_type", "byte_length", "sha256"], "properties": {"representation": {"const": "bundle_file"}, "path": {"type": "string"}, "media_type": {"type": "string"}, "charset": {"type": "string"}, "byte_length": {"type": "integer", "minimum": 0}, "sha256": {"type": "string"}}},
    "created_at": {"type": "string"},
    "parents": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactRef"}},
    "source_revision_refs": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/sourceRevisionRef"}},
    "generated_by": {"type": "object"},
    "generation": {"type": "object"},
    "quality_report_refs": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactRef"}},
    "extensions": {"type": "object"}
  }
}
```

`evidence-ref.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:evidence-ref:v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["evidence_ref_schema_version", "evidence_id", "source_revision_ref", "target_artifact_ref", "locator", "excerpt_sha256", "extensions"],
  "properties": {
    "evidence_ref_schema_version": {"const": 1},
    "evidence_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/evidenceId"},
    "source_revision_ref": {"$ref": "urn:iwiki:portable:core:v1#/$defs/sourceRevisionRef"},
    "target_artifact_ref": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactRef"},
    "locator": {"type": "object", "required": ["scheme"]},
    "excerpt_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    "extensions": {"type": "object"}
  }
}
```

`receipt.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:receipt:v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["receipt_schema_version", "run_id", "state", "started_at", "completed_at", "recipe", "parameters", "inputs", "outputs", "capabilities", "executors", "usage", "quality", "redactions"],
  "properties": {
    "receipt_schema_version": {"const": 1},
    "run_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/runId"},
    "state": {"const": "succeeded"},
    "started_at": {"type": "string"},
    "completed_at": {"type": "string"},
    "recipe": {"type": "object", "required": ["id", "version"]},
    "parameters": {"type": "object", "required": ["sha256", "summary"]},
    "inputs": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/sourceRevisionRef"}},
    "outputs": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactRef"}},
    "capabilities": {"type": "array"},
    "executors": {"type": "array"},
    "usage": {"type": "object"},
    "quality": {"type": "object"},
    "redactions": {"type": "object"}
  }
}
```

`commit.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:commit:v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["commit_protocol_version", "bundle_id", "manifest", "committed_at"],
  "properties": {
    "commit_protocol_version": {"const": 1},
    "bundle_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/bundleId"},
    "manifest": {"type": "object", "additionalProperties": false, "required": ["path", "byte_length", "sha256"], "properties": {"path": {"const": "bundle.json"}, "byte_length": {"type": "integer", "minimum": 1}, "sha256": {"type": "string"}}},
    "committed_at": {"type": "string"}
  }
}
```

`transcript.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:transcript:v1",
  "oneOf": [
    {"type": "object", "additionalProperties": false, "required": ["record_type", "transcript_schema_version", "source_revision_id", "time_base"], "properties": {"record_type": {"const": "transcript_header"}, "transcript_schema_version": {"const": 1}, "source_revision_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/revisionId"}, "time_base": {"const": "millisecond"}, "language": {"type": "string"}}},
    {"type": "object", "additionalProperties": false, "required": ["record_type", "segment_id", "start_ms", "end_ms", "text"], "properties": {"record_type": {"const": "segment"}, "segment_id": {"type": "string", "pattern": "^seg_[0-9]{6,}$"}, "start_ms": {"type": "integer", "minimum": 0}, "end_ms": {"type": "integer", "minimum": 1}, "text": {"type": "string", "minLength": 1}, "speaker": {"type": "string"}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "language": {"type": "string"}, "source_cue": {"type": "string"}}}
  ]
}
```

`evidence-set.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:evidence-set:v1",
  "oneOf": [
    {"type": "object", "additionalProperties": false, "required": ["record_type", "evidence_set_schema_version", "bundle_id", "record_count"], "properties": {"record_type": {"const": "evidence_set_header"}, "evidence_set_schema_version": {"const": 1}, "bundle_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/bundleId"}, "record_count": {"type": "integer", "minimum": 0}}},
    {"$ref": "urn:iwiki:portable:evidence-ref:v1"}
  ]
}
```

`quality-report.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:quality-report:v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["quality_report_schema_version", "subject", "profile", "overall", "checks", "method", "metrics", "messages", "evidence_ids"],
  "properties": {
    "quality_report_schema_version": {"const": 1},
    "subject": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactRef"},
    "profile": {"type": "object", "required": ["id", "version"]},
    "overall": {"enum": ["pass", "pass_with_warnings", "fail"]},
    "checks": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "status"],
        "properties": {
          "id": {"type": "string", "minLength": 1},
          "status": {"enum": ["pass", "warn", "fail", "skipped"]},
          "reason": {"type": "string", "minLength": 1}
        },
        "allOf": [{"if": {"properties": {"status": {"const": "skipped"}}}, "then": {"required": ["reason"]}}]
      }
    },
    "method": {"type": "object", "required": ["kind"], "properties": {"kind": {"enum": ["deterministic", "model", "agent", "human"]}}},
    "metrics": {"type": "object"},
    "messages": {"type": "array"},
    "evidence_ids": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/evidenceId"}, "uniqueItems": true}
  }
}
```

`bundle.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:iwiki:portable:bundle:v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["$schema", "bundle_schema_version", "bundle_id", "created_at", "producer", "sources", "source_revisions", "dependencies", "artifacts", "outputs", "receipt", "required_contracts", "extensions"],
  "properties": {
    "$schema": {"const": "urn:iwiki:portable:bundle:v1"},
    "bundle_schema_version": {"const": 1},
    "bundle_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/bundleId"},
    "created_at": {"type": "string"},
    "producer": {"type": "object", "required": ["product", "runtime_version", "recipe", "capability", "portable_contract_id"]},
    "sources": {"type": "array", "items": {"$ref": "urn:iwiki:portable:source:v1"}},
    "source_revisions": {"type": "array", "items": {"$ref": "urn:iwiki:portable:source-revision:v1"}},
    "dependencies": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["bundle_id", "bundle_manifest_sha256", "used_source_ids", "used_source_revision_ids", "used_artifact_ids"],
        "properties": {
          "bundle_id": {"$ref": "urn:iwiki:portable:core:v1#/$defs/bundleId"},
          "bundle_manifest_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
          "used_source_ids": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/sourceId"}, "uniqueItems": true},
          "used_source_revision_ids": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/revisionId"}, "uniqueItems": true},
          "used_artifact_ids": {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactId"}, "uniqueItems": true}
        }
      }
    },
    "artifacts": {"type": "array", "items": {"$ref": "urn:iwiki:portable:artifact:v1"}},
    "outputs": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": {
        "oneOf": [
          {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactId"},
          {"type": "array", "items": {"$ref": "urn:iwiki:portable:core:v1#/$defs/artifactId"}, "uniqueItems": true}
        ]
      }
    },
    "receipt": {"type": "object", "additionalProperties": false, "required": ["path", "byte_length", "sha256"], "properties": {"path": {"const": "receipt.json"}, "byte_length": {"type": "integer", "minimum": 1}, "sha256": {"type": "string"}}},
    "required_contracts": {"type": "array", "items": {"type": "string"}, "uniqueItems": true},
    "extensions": {"type": "object"}
  }
}
```

- [ ] **Step 8: Package the JSON contract resources**

Append to `pyproject.toml`:

```toml
[tool.setuptools.package-data]
"iwiki.portable" = ["contracts/v1/*.json"]
```

Create `iwiki/portable/__init__.py` initially as:

```python
from iwiki.portable.contract import inspect_portable_contract
from iwiki.portable.types import (
    BundleState,
    CommitResult,
    PortableBundleRef,
    PortableContractInfo,
    PortableValidationIssue,
    PortableValidationReport,
    ValidationLevel,
)

__all__ = [
    "BundleState",
    "CommitResult",
    "PortableBundleRef",
    "PortableContractInfo",
    "PortableValidationIssue",
    "PortableValidationReport",
    "ValidationLevel",
    "inspect_portable_contract",
]
```

- [ ] **Step 9: Run focused tests and the existing packaging suite**

Run:

```powershell
python -m unittest tests.test_portable_contract -v
python -m unittest tests.test_iwiki_packaging -v
```

Expected: contract tests pass; existing packaging tests remain green before installed-resource assertions are added in Task 9.

- [ ] **Step 10: Commit the contract catalog**

Run:

```powershell
git add pyproject.toml iwiki/portable tests/test_portable_contract.py
git diff --cached --check
git commit -m "feat(iwiki): publish portable contract v1"
```

Expected: commit contains only packaged contract files, DTOs and contract tests.

---

## Task 2: Add Strict Control-File I/O and the Secure Bundle Tree Policy

**Files:**

- Create: `iwiki/portable/jsonio.py`
- Create: `iwiki/portable/path_policy.py`
- Create: `tests/test_portable_jsonio.py`
- Create: `tests/test_portable_path_policy.py`

- [ ] **Step 1: Write failing tests for exact control-file bytes**

Create `tests/test_portable_jsonio.py`. Cover these observable rules individually:

```python
class StrictJsonTests(unittest.TestCase):
    def test_accepts_canonical_utf8_json_with_one_final_lf(self): ...
    def test_rejects_utf8_bom(self): ...
    def test_rejects_crlf_and_bare_cr(self): ...
    def test_rejects_missing_final_lf(self): ...
    def test_rejects_multiple_final_lf(self): ...
    def test_rejects_duplicate_object_key_at_any_depth(self): ...
    def test_rejects_invalid_utf8(self): ...
    def test_iter_jsonl_reports_one_based_line_number(self): ...
    def test_encoder_sorts_keys_uses_compact_separators_and_appends_lf(self): ...
    def test_sha256_is_over_the_exact_file_bytes(self): ...
```

The accepted byte form is exactly:

```python
b'{"a":1,"nested":{"b":2}}\n'
```

Run:

```powershell
python -m unittest tests.test_portable_jsonio -v
```

Expected: FAIL because `iwiki.portable.jsonio` does not exist.

- [ ] **Step 2: Implement strict JSON/JSONL primitives**

Create `iwiki/portable/jsonio.py` with this public-to-the-package surface:

```python
@dataclass(frozen=True)
class StrictJsonDocument:
    data: object
    raw: bytes
    byte_length: int
    sha256: str

@dataclass(frozen=True)
class JsonLine:
    number: int
    data: object
    raw: bytes

class StrictJsonError(ValueError):
    def __init__(self, code: str, message: str, *, line: int | None = None): ...

def sha256_bytes(data: bytes) -> str: ...
def encode_control_json(payload: object) -> bytes: ...
def read_control_json(path: Path) -> StrictJsonDocument: ...
def iter_jsonl(path: Path) -> Iterator[JsonLine]: ...
def write_small_control_file(path: Path, payload: object) -> StrictJsonDocument: ...
```

Implementation rules:

```python
def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError("duplicate_json_key", f"duplicate key: {key}")
        result[key] = value
    return result

def encode_control_json(payload: object) -> bytes:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8") + b"\n"
```

Before decoding, reject BOM, any `b"\r"`, missing final `b"\n"`, and more than one terminal LF. Decode with strict UTF-8. Parse with `object_pairs_hook=_reject_duplicate_pairs` and reject NaN/Infinity through `parse_constant`. `iter_jsonl` reads binary lines incrementally, checks BOM only at byte zero, rejects CR/blank lines, requires exactly one LF terminator per record including the last record, and parses/yields one line at a time; it never calls `read_bytes()` on an unbounded Transcript. `write_small_control_file` uses `open(..., "xb")`, flushes, and `os.fsync`; it never replaces an existing file.

- [ ] **Step 3: Write failing path and tree-policy tests**

Create `tests/test_portable_path_policy.py` with table-driven cases for:

```python
VALID_IDS = {
    "bundle": "bnd_01900000-0000-7000-8000-000000000001",
    "source": "src_01900000-0000-7000-8000-000000000002",
    "revision": "rev_01900000-0000-7000-8000-000000000003",
    "artifact": "art_01900000-0000-7000-8000-000000000004",
    "run": "run_01900000-0000-7000-8000-000000000005",
    "evidence": "ev_01900000-0000-7000-8000-000000000006",
}
```

Test:

- only lowercase prefix + canonical RFC 9562 UUIDv7 is accepted;
- digest is exactly `sha256:` plus 64 lowercase hex characters;
- portable paths use `/`, are relative, Unicode NFC, contain no empty/`.`/`..` segment, colon, backslash or NUL;
- reject Windows device aliases case-insensitively, including an extension (`CON`, `con.txt`, `LPT1.md`), and segments ending with dot/space;
- reject NFC and Unicode-casefold collisions across the full relative tree;
- reject symlinks/junctions/reparse points, hard-linked files, sockets/FIFOs/devices and escapes outside the candidate root;
- replace a validated parent directory with a symlink/junction between scan and open and require fail-closed behavior rather than reading the replacement;
- resolve staging references only below `raw_personal/.staging`, committed references only as one typed bundle ID below `raw_personal/bundles`.

Run:

```powershell
python -m unittest tests.test_portable_path_policy -v
```

Expected: FAIL because the path-policy module does not exist.

- [ ] **Step 4: Implement typed IDs, paths, secure resolution and tree scanning**

Create `iwiki/portable/path_policy.py`:

```python
@dataclass(frozen=True)
class TreeEntry:
    relative_path: str
    absolute_path: Path
    byte_length: int
    device: int
    inode: int
    link_count: int
    modified_ns: int
    changed_ns: int

class PathPolicyError(ValueError):
    def __init__(self, code: str, path: str, message: str): ...

def validate_typed_id(prefix: str, value: str) -> str: ...
def validate_digest(value: str) -> str: ...
def validate_relative_path(value: str) -> str: ...
def resolve_bundle_ref(workspace: Workspace, bundle_ref: PortableBundleRef) -> Path: ...
@contextmanager
def open_bundle_tree(root: Path) -> Iterator[BundleTreeAnchor]: ...
def scan_bundle_tree(root: Path) -> tuple[TreeEntry, ...]: ...
```

Use `uuid.UUID`, require `parsed.version == 7`, `parsed.variant == uuid.RFC_4122`, and require `str(parsed) == suffix`. Do not accept “UUID-shaped” values with the wrong version/variant.

`scan_bundle_tree` must use `os.scandir` without following links, inspect `st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT` on Windows when present, and use `entry.stat(follow_symlinks=False)`. A regular payload file must have `st_nlink == 1`; directories may have normal platform link counts. Normalize every relative segment with `unicodedata.normalize("NFC", segment).casefold()` and reject a duplicate normalized full path. Sort output by UTF-8 encoded relative path so validation reports are deterministic.

`BundleTreeAnchor` is package-private and exposes only `scan()` plus `open_read(TreeEntry)` as context-managed operations. On POSIX, it holds a root directory FD and descends with `openat`/`dir_fd` plus `O_NOFOLLOW`, comparing `fstat` identities. On Windows, it opens each directory component with `CreateFileW(..., FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)`, rejects reparse attributes, and compares the final handle identity to the scanned entry. `scan_bundle_tree` is a short-lived convenience wrapper; Validator and Committer keep one anchor for their operation. Never validate by `Path.resolve()` and then later perform an ordinary unanchored `open()` as though the pathname could not change.

`resolve_bundle_ref` first validates the relative reference syntactically, resolves the fixed Workspace root and its parent, and confirms containment with `os.path.commonpath`. It must not use a user-provided absolute path. A staging `value` is the exact Workspace-relative `<raw_personal>/.staging/<local-instance>/<job>.<nonce>/bundle.partial` path and must match `workspace.paths.raw_personal`; a committed `value` is exactly a `bnd_*` ID and resolves below `<raw_personal>/bundles`.

- [ ] **Step 5: Run the focused policy suite**

Run:

```powershell
python -m unittest tests.test_portable_jsonio tests.test_portable_path_policy -v
```

Expected: all strict-byte, ID, digest, path, collision and filesystem-entry tests pass on Windows. POSIX-only link/device cases use `@unittest.skipIf` only when the host cannot construct that fixture; the production check is never skipped.

- [ ] **Step 6: Commit the control-file and path boundary**

Run:

```powershell
git add iwiki/portable/jsonio.py iwiki/portable/path_policy.py tests/test_portable_jsonio.py tests/test_portable_path_policy.py
git diff --cached --check
git commit -m "feat(iwiki): enforce portable bundle file policy"
```

Expected: one commit containing only reusable byte/path primitives and their tests.

---

## Task 3: Build Deterministic Fixtures and Structure/Integrity Validation

**Files:**

- Create: `tests/portable_fixture_factory.py`
- Create: `iwiki/portable/validator.py`
- Create: `tests/test_portable_validation.py`

- [ ] **Step 1: Build a deterministic in-test Bundle factory**

Create `tests/portable_fixture_factory.py`. It copies `tests/fixtures/workspaces/valid-v2` into a temporary directory, then creates:

```text
raw/personal/.staging/test-instance/job-0001.fixed/bundle.partial/
  bundle.json
  receipt.json
  drafts/
    art_01900000-0000-7000-8000-000000000004.md
```

Use the frozen IDs from Task 2, timestamp `2026-07-14T00:00:00.000Z`, and deterministic Markdown `# Minimal note\n`. The factory must calculate every `byte_length` and `sha256` from bytes actually written, then write `receipt.json`, then write `bundle.json` last. Provide only these mutation helpers:

```python
class PortableFixture:
    workspace_root: Path
    staging_value: str
    bundle_root: Path
    bundle_id: str

    def rewrite_json(self, relative_path: str, mutate: Callable[[dict], None]) -> None: ...
    def rewrite_bytes(self, relative_path: str, data: bytes) -> None: ...
    def delete(self, relative_path: str) -> None: ...
    def add_undeclared_file(self, relative_path: str, data: bytes) -> None: ...
    def refresh_receipt_and_manifest(self) -> None: ...
```

The factory is test infrastructure only. It must not import private validator functions or recreate validation decisions.

- [ ] **Step 2: Write failing Structure and Integrity tests**

Create `tests/test_portable_validation.py` with one test per stable code:

```python
class PortableStructureValidationTests(unittest.TestCase):
    def test_valid_minimal_staging_bundle_passes_structure(self): ...
    def test_missing_bundle_manifest_reports_missing_control_file(self): ...
    def test_staging_commit_reports_unexpected_commit_file(self): ...
    def test_unknown_schema_reports_unsupported_schema(self): ...
    def test_directory_and_manifest_id_mismatch_is_reported_for_committed_bundle(self): ...
    def test_duplicate_typed_id_across_manifest_sections_is_reported(self): ...
    def test_undeclared_file_is_reported(self): ...

class PortableIntegrityValidationTests(unittest.TestCase):
    def test_valid_minimal_staging_bundle_passes_integrity(self): ...
    def test_missing_declared_file_is_reported(self): ...
    def test_byte_length_mismatch_is_reported(self): ...
    def test_hash_mismatch_is_reported(self): ...
    def test_receipt_binding_is_verified(self): ...
    def test_committed_bundle_requires_and_verifies_commit_binding(self): ...
    def test_large_payload_hashing_does_not_use_path_read_bytes(self): ...
```

Tests assert `report.valid`, ordered `issue.code`, `issue.level`, and portable relative `issue.path`. They never assert English message prose.

Run:

```powershell
python -m unittest tests.test_portable_validation -v
```

Expected: FAIL because `validate_bundle` is not implemented/exported.

- [ ] **Step 3: Implement the deterministic validation pipeline shell**

Create `iwiki/portable/validator.py` with these private stages:

```python
_LEVEL_ORDER = {
    ValidationLevel.STRUCTURE: 0,
    ValidationLevel.INTEGRITY: 1,
    ValidationLevel.CLOSURE: 2,
    ValidationLevel.SEMANTIC: 3,
}

@dataclass
class _ValidationContext:
    workspace: Workspace
    bundle_ref: PortableBundleRef
    root: Path
    anchor: BundleTreeAnchor
    requested_level: ValidationLevel
    tree: tuple[TreeEntry, ...]
    manifest: StrictJsonDocument | None = None
    receipt: StrictJsonDocument | None = None
    commit: StrictJsonDocument | None = None
    issues: list[PortableValidationIssue] = field(default_factory=list)

def validate_bundle(
    workspace: Workspace,
    bundle_ref: PortableBundleRef,
    level: ValidationLevel = ValidationLevel.SEMANTIC,
) -> PortableValidationReport: ...

def _validate_structure(context: _ValidationContext) -> None: ...
def _validate_integrity(context: _ValidationContext) -> None: ...
def _validate_closure(context: _ValidationContext) -> None: ...
def _validate_semantics(context: _ValidationContext) -> None: ...
```

The orchestration contract is:

1. Resolve, open one anchored tree, scan once, and close the anchor in `finally` after all requested stages.
2. Run all stages up to the requested level in order.
3. A stage may add multiple independent issues, but does not dereference data known invalid from an earlier stage.
4. Sort issues by `(level order, path encoded as UTF-8, code)` before returning.
5. Invalid Bundle data returns a report. Invalid SDK arguments, unreadable Workspace configuration, permission errors, and filesystem races raise existing `IWikiError` categories.

- [ ] **Step 4: Implement Structure validation**

Structure validation must:

- require `bundle.json` and `receipt.json`; `sources/`, `evidence/`, `drafts/`, `assets/`, `quality/`, and `refs/` are optional logical groups and empty groups may be absent;
- reject `commit.json` in staging and require it in committed state;
- parse every control file through `jsonio` and map `StrictJsonError.code` to the same issue code;
- require exact `$schema` URNs and schema versions from the catalog;
- reject unexpected top-level control-file fields through a small explicit allowed/required-field table matching the packaged schemas;
- validate every typed ID, digest, timestamp (`YYYY-MM-DDTHH:MM:SS.mmmZ` only) and declared relative path;
- require extension keys to use the namespaced `vendor-or-domain:field` form; unknown optional extension payloads are preserved/ignored, while any extension needed for interpretation or safety must be named in `required_contracts` and therefore fails closed in P0’s empty supported-required-contract set;
- build a single global definition index for Bundle, Source, Revision, Artifact and Receipt Run IDs available in `bundle.json`/`receipt.json`, and report duplicates; Evidence IDs inside streaming EvidenceSet content are added to the same duplicate policy in Task 4 without loading the whole file;
- form the declared inventory from Artifact envelopes plus the manifest-bound receipt and optional commit controls; Receipt inputs/outputs are provenance references, not a second competing file manifest;
- report every scanned regular file that is neither declared nor one of those controls as `undeclared_file`.

Directory entries are allowed only when they are ancestors of declared files or one of the reserved top-level logical groups `sources`, `evidence`, `drafts`, `assets`, `quality`, and `refs`. An arbitrary empty directory is reported as `undeclared_file` with its trailing-slash relative path; the contract does not let unhashed directory names become hidden metadata.

Do not add a generic schema library. The packaged schemas are the public declaration; the Reference Validator uses explicit Python checks so duplicate-key, exact-byte and filesystem rules remain authoritative and testable without another runtime dependency.

- [ ] **Step 5: Implement Integrity validation**

Integrity validation must:

- for every declared file, verify existence, regular-file status, `byte_length`, and SHA-256 over exact bytes;
- hash payloads from the anchored handle in fixed-size chunks with O(1) memory; never call `read_bytes()` for arbitrary media or Transcript Artifacts;
- verify the manifest’s receipt descriptor equals the actual `receipt.json` byte length/hash;
- for committed state, verify `commit.json` binds `bundle_id` plus the exact `bundle.json` path, byte length and SHA-256; the manifest then binds `receipt.json`, so there is one integrity-root chain rather than duplicate competing fields;
- require the Committer timestamp to be valid UTC milliseconds and not earlier than `bundle.created_at`;
- never trust manifest-supplied sizes or hashes to decide which path is safe to open;
- read through the held `BundleTreeAnchor` and compare file identity before every read; no validation stage performs an ordinary pathname reopen;
- report all deterministic integrity mismatches in one report.

The manifest hash is always the exact `bundle.json` bytes, including its final LF. `commit.json` is deliberately absent from the Bundle manifest inventory because it is written by the Committer after validation.

- [ ] **Step 6: Export validation and run focused tests**

Update `iwiki/portable/__init__.py`:

```python
from iwiki.portable.validator import validate_bundle
```

Add `"validate_bundle"` to `__all__`, then run:

```powershell
python -m unittest tests.test_portable_validation tests.test_portable_contract -v
```

Expected: valid fixtures pass, every mutation produces its specific stable issue code, and contract tests remain green.

- [ ] **Step 7: Commit the first Reference Validator slice**

Run:

```powershell
git add iwiki/portable/__init__.py iwiki/portable/validator.py tests/portable_fixture_factory.py tests/test_portable_validation.py
git diff --cached --check
git commit -m "feat(iwiki): validate portable bundle structure and integrity"
```

Expected: no CLI or commit mutation code in this commit.

---

## Task 4: Add Source, Artifact and Knowledge-Content Semantics

**Files:**

- Create: `iwiki/portable/content_validation.py`
- Create: `tests/test_portable_semantics.py`
- Modify: `iwiki/portable/validator.py`
- Modify: `tests/portable_fixture_factory.py`

- [ ] **Step 1: Extend the fixture with each normative content type**

Add opt-in factory methods so the minimal fixture stays minimal:

```python
def add_materialized_source(self) -> None: ...
def add_transcript(self) -> None: ...
def add_evidence_set(self) -> None: ...
def add_quality_report(self) -> None: ...
def add_local_resource(self) -> None: ...
```

Each method writes exact bytes first and refreshes the receipt/manifest descriptors. Do not create one giant fixture that makes failures ambiguous.

- [ ] **Step 2: Write failing semantic tests as rule tables**

Create `tests/test_portable_semantics.py`. Required cases:

```text
source_invalid
source_revision_invalid
artifact_invalid
output_invalid
transcript_invalid
evidence_set_invalid
evidence_locator_invalid
draft_citation_missing
draft_resource_invalid
quality_report_invalid
receipt_invalid
```

For each code, include one valid control test and the smallest mutation that produces only that code where practical. Privacy/license findings use `source_revision_invalid`; materialization findings use `source_revision_invalid`; representation findings use `artifact_invalid`. Do not multiply public codes for subrules that callers handle identically.

Run:

```powershell
python -m unittest tests.test_portable_semantics -v
```

Expected: FAIL because semantic validators do not exist.

- [ ] **Step 3: Implement Source/Revision, privacy and license semantics**

Create `iwiki/portable/content_validation.py` with pure validators that append issues and never mutate files:

```python
def validate_content_structure(context: ContentValidationContext) -> None: ...
def validate_source_records(context: ContentValidationContext) -> None: ...
def validate_artifacts_and_outputs(context: ContentValidationContext) -> None: ...
def validate_transcript(path: Path, descriptor: dict[str, object], sink: IssueSink) -> None: ...
def validate_evidence_set(path: Path, descriptor: dict[str, object], sink: IssueSink) -> None: ...
def validate_draft_markdown(path: Path, context: ContentValidationContext) -> None: ...
def validate_quality_report(path: Path, context: ContentValidationContext) -> None: ...
def validate_receipt_semantics(context: ContentValidationContext) -> None: ...
```

Source rules:

- `source_id` and `revision_id` use their correct prefixes and are unique;
- each Revision names an existing Source;
- `materialization.kind == "archived"` requires an ArtifactRef in the current Bundle to a declared Artifact whose exact hash is bound by its envelope; a concrete `content_digest` must agree with that archived target hash;
- `reference_only` requires its versioned reason code and never pretends that remote bytes are part of Portable integrity;
- `external_local` requires an `ext_*` ID and must not contain an absolute path or machine-local URI;
- privacy class is exactly one of `public`, `personal`, `sensitive`, `confidential`, `unknown`; no silent default;
- license status is `known`, `unknown`, or `restricted`; archive permission is independently `allowed`, `disallowed`, or `unknown`; unknown never implies permission.

- [ ] **Step 4: Implement Artifact/output and media semantics**

Artifact rules:

- v1 representation is exactly `bundle_file`;
- every parent ArtifactRef and SourceRevisionRef, when present, resolves through the current Bundle or declared dependency; a generic Artifact is not forced to invent a SourceRevision when its Recipe legitimately derives only from parent Artifacts;
- media type and path extension agree for `.md`, `.json`, `.jsonl`, `.txt`, and binary resources;
- each Artifact envelope is the single file descriptor for its path, and its byte length/hash match the actual anchored file bytes;
- `generated_by.run_id`, when present, equals the Bundle Receipt run; generation metadata stays structured and does not embed provider secrets or raw private responses;
- two Artifact IDs cannot claim the same path;
- every `outputs` value is an explicit Artifact ID or ordered list of Artifact IDs; aliases cannot reference an absent artifact;
- the minimum note-producing Bundle exposes `outputs.primary_draft`; other Recipe Contracts may define different explicit output roles.

P0 validates output binding shape and existence, not the business meaning of arbitrary Recipe-specific role names. A role whose meaning is required for safe interpretation must arrive through a later supported required Recipe Contract.

- [ ] **Step 5: Implement Transcript and EvidenceSet streaming validation**

Read JSONL with `iter_jsonl`; never load an unbounded transcript into one JSON object.

`validate_content_structure` runs even when the caller requests only Structure. It streams declared JSON/JSONL Artifacts, rejects malformed record shapes/unknown content schema versions, and adds Evidence IDs to duplicate-ID checks. Locator resolution, hash-bound references, Markdown links and quality meaning remain in later levels. This preserves the published level definitions instead of hiding basic JSONL validity behind Semantic validation.

Transcript rules:

- line 1 is one header carrying transcript schema version, SourceRevision ID and time base `millisecond`;
- following lines are segments with stable segment ID, integer `start_ms >= 0`, `end_ms > start_ms`, non-empty text and optional speaker/language;
- segment IDs are unique; ordering is nondecreasing by start time; overlap is valid and preserved;
- the header contains no second authoritative full-text field and no count field absent from the v1 schema.

EvidenceSet rules:

- line 1 is a header that binds EvidenceSet schema version, current Bundle ID and record count;
- each following record has a unique `ev_*` ID and exactly one locator family;
- every EvidenceRef binds a SourceRevisionRef, immutable target ArtifactRef plus exact target hash, versioned locator, and excerpt hash;
- when a locator deterministically selects target bytes/text, recompute and compare `excerpt_sha256`; schemes that select non-text media require their versioned equivalent verification fields rather than pretending a timestamp itself is an excerpt hash;
- `video-time-range.v1` and `audio-time-range.v1` use integer half-open milliseconds and lie inside declared duration when known;
- `text-span.v1` uses half-open UTF-8 byte offsets on codepoint boundaries, not Unicode character or UTF-16 indices;
- page/slide locators are one-based and optional bounding boxes are normalized to `[0,1]` with one coordinate convention;
- `web-section.v1` and `wiki-revision-section.v1` bind archived normalized text, with Wiki revision fixed; CSS/XPath is auxiliary rather than the integrity root;
- `git-file-lines.v1`, `code-symbol.v1`, and `git-commit.v1` pin commit/tree/blob identity; line ranges are one-based inclusive and a symbol locator carries a verifiable file/range fallback;
- `record-id.v1` requires versioned dataset identity plus a stable non-empty record ID;
- header evidence count equals observed count.

- [ ] **Step 6: Implement Markdown citation/resource and QualityReport semantics**

Scan Markdown line-by-line outside fenced code blocks and inline code, keeping only citation/link sets rather than the whole Draft in memory. The Reference Validator recognizes:

```markdown
Claim text.[^ev_01900000-0000-7000-8000-000000000006]
![frame](resources/frame-001.png)
```

Rules:

- every `[^ev_*]` use resolves to an Evidence record declared by the Bundle or dependency closure;
- every used evidence ID is also present in one Markdown footnote definition; unused Evidence and unused definitions are permitted and may be reported by a Quality Profile rather than invalidating the Bundle;
- local Markdown links/resources may contain `..`, but after resolution from the Draft’s directory they must remain inside the Bundle and resolve to a declared, hash-bound Artifact;
- same-document `#fragment` links are allowed; external URL schemes are limited to `https`, `http`, and `mailto`; reject `file`, `javascript`, `data` in P0, absolute/UNC paths, opaque drive-letter paths and protocol-relative local aliases;
- QualityReport binds a subject Artifact ID and its exact hash, has a profile ID/version, and every `skipped` check carries a reason;
- QualityReport check values are exactly `pass`, `warn`, `fail`, or `skipped`, and overall is `pass`, `pass_with_warnings`, or `fail`;
- `overall: fail` is still a semantically valid Portable Bundle. Quality, review approval and publish eligibility are separate states; later Publisher policy decides whether a failing report blocks publication.

Use a dedicated package-private Markdown-link resolver for the `..` case; do not weaken `validate_relative_path`, because manifest paths themselves must continue rejecting dot segments.

- [ ] **Step 7: Implement Receipt time/run/input/output semantics and wire the stage**

Receipt rules:

- one `run_*` ID; producer/capability/recipe agree with `bundle.json`;
- `started_at <= completed_at <= bundle.created_at` after strict UTC millisecond parsing;
- every input is a resolvable SourceRevisionRef and every output is a resolvable ArtifactRef; exact payload path/size/hash remain authoritative in the Artifact envelope rather than being duplicated into a second file manifest;
- redaction data is a structured, non-secret summary; when it claims redaction was performed, rule IDs and counts are internally consistent, but P0 does not invent a mandatory redaction policy contract;
- warnings and model/tool invocations are arrays with stable explicit fields, never free-form top-level extensions.

Wire `_validate_structure` to the streaming content-structure pass and `_validate_semantics` to the remaining pure validators only after required structure/integrity/closure inputs are available. Then run:

```powershell
python -m unittest tests.test_portable_semantics tests.test_portable_validation -v
```

Expected: all semantic issue-code cases pass, and lower validation levels do not report semantic-only issues.

- [ ] **Step 8: Commit semantic validation**

Run:

```powershell
git add iwiki/portable/content_validation.py iwiki/portable/validator.py tests/portable_fixture_factory.py tests/test_portable_semantics.py
git diff --cached --check
git commit -m "feat(iwiki): validate portable knowledge semantics"
```

Expected: content rules are isolated from CLI and filesystem mutation.

---

## Task 5: Validate Dependency Closure and Committed Bundle References

**Files:**

- Modify: `iwiki/portable/validator.py`
- Modify: `tests/test_portable_validation.py`
- Modify: `tests/portable_fixture_factory.py`

- [ ] **Step 1: Write failing dependency-closure tests**

Add tests for:

```python
class PortableClosureValidationTests(unittest.TestCase):
    def test_local_only_bundle_passes_closure(self): ...
    def test_dependency_must_exist_as_committed_bundle(self): ...
    def test_dependency_must_pin_exact_manifest_hash(self): ...
    def test_cross_bundle_reference_must_name_declared_dependency(self): ...
    def test_cross_bundle_reference_target_id_must_exist(self): ...
    def test_cross_bundle_reference_hash_must_match(self): ...
    def test_required_contract_must_be_supported(self): ...
    def test_cycle_is_terminated_and_reported_deterministically(self): ...
    def test_transitive_dependency_is_not_implicitly_visible(self): ...
    def test_deep_dependency_chain_uses_iterative_traversal(self): ...
```

The last test freezes an important boundary: a Bundle may use only its own IDs and IDs explicitly imported from a direct dependency entry. Transitive visibility is not implicit.

Run:

```powershell
python -m unittest tests.test_portable_validation.PortableClosureValidationTests -v
```

Expected: FAIL because `_validate_closure` is still empty.

- [ ] **Step 2: Build the closure index without trusting dependency content**

Add private immutable records:

```python
@dataclass(frozen=True)
class _BundleIndex:
    bundle_id: str
    manifest_sha256: str
    ids: Mapping[str, _IndexedObject]
    dependencies: tuple[_Dependency, ...]

@dataclass(frozen=True)
class _Dependency:
    bundle_id: str
    bundle_manifest_sha256: str
    used_source_ids: tuple[str, ...]
    used_source_revision_ids: tuple[str, ...]
    used_artifact_ids: tuple[str, ...]
```

For each direct dependency:

1. Resolve only `raw_personal/bundles/<validated bnd_id>`.
2. Validate its committed Structure + Integrity before indexing it.
3. Confirm the dependency entry’s pinned manifest hash equals exact dependency `bundle.json` bytes.
4. Index only IDs listed by that entry’s `used_source_ids`, `used_source_revision_ids`, and `used_artifact_ids`; do not expose every dependency object.
5. Iteratively verify that dependency’s own declared closure at Structure + Integrity + Closure, while keeping visibility for the current Bundle limited to its direct `used_*` lists.
6. Memoize by `(bundle_id, manifest_sha256)` for the current validation call.
7. Use an explicit DFS work stack plus active/finished sets, and report a deterministic dependency error on a cycle; do not rely on Python call-stack depth for a large closure.

Do not cache this index globally: committed Bundles are immutable by contract, but a process may observe an external invalid mutation and must not return a stale “valid” result.

When a dependency’s own validation fails, project its issue paths under `dependencies/<bundle_id>/` in the caller’s report so no absolute Workspace path leaks and two dependency failures remain distinguishable.

- [ ] **Step 3: Resolve cross-Bundle references and required contracts**

Every reference includes its owning Bundle ID. The two important forms are:

```json
{"bundle_id":"bnd_01900000-0000-7000-8000-000000000001","source_revision_id":"rev_01900000-0000-7000-8000-000000000003"}
{"bundle_id":"bnd_01900000-0000-7000-8000-000000000001","artifact_id":"art_01900000-0000-7000-8000-000000000004","sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}
```

Validate:

- a reference whose `bundle_id` equals the current Bundle resolves locally; ArtifactRef hash must match the exact target payload;
- another Bundle ID must be directly declared, the ID must appear in the matching typed `used_*_ids` list, the target must exist, and ArtifactRef hash must match;
- reference type matches the field contract (for example an Evidence locator cannot resolve to a Source);
- every `required_contracts` string is one of `inspect_portable_contract(workspace).supported_required_contracts`;
- unused declared dependencies are allowed but preserved as provenance; they do not make IDs visible.

Issue codes are exactly `dependency_missing`, `dependency_manifest_mismatch`, `dependency_cycle`, `reference_target_missing`, `reference_hash_mismatch`, and `required_contract_unsupported`. A cycle is not valid because a legitimately committed immutable Bundle can depend only on already committed Bundles; report `dependency_cycle` and terminate traversal deterministically.

- [ ] **Step 4: Run all validator levels and commit**

Run:

```powershell
python -m unittest tests.test_portable_validation tests.test_portable_semantics -v
python -m unittest tests.test_portable_contract tests.test_portable_jsonio tests.test_portable_path_policy -v
git add iwiki/portable/validator.py tests/portable_fixture_factory.py tests/test_portable_validation.py
git diff --cached --check
git commit -m "feat(iwiki): verify portable dependency closure"
```

Expected: Structure, Integrity, Closure and Semantic tests all pass; no Workspace mutation occurs during validation.

---

## Task 6: Implement PreparedBundle and Crash-Safe Atomic Commit

**Files:**

- Create: `iwiki/portable/commit.py`
- Create: `tests/test_portable_commit.py`
- Modify: `iwiki/portable/__init__.py`
- Modify: `iwiki/portable/validator.py`

### Commit invariants frozen before implementation

- `validate_bundle` is read-only and gives no later mutation authority.
- `prepare_bundle_commit` is the authorization boundary. A Producer must have closed all writable handles before calling it and must not touch the candidate afterward.
- `PreparedBundle` is process-local, single-use, and owns exact hashes plus sealed file identities until committed or closed. It deliberately does not hold the Workspace mutation lock while the caller obtains its JobStore Commit Guard.
- A commit is complete only when `<raw_personal>/bundles/<bundle_id>` exists with a valid `commit.json` binding the exact manifest bytes, which in turn bind the Receipt and Artifact bytes.
- No-replace rename is mandatory. There is no copy fallback and no delete-then-rename path.
- On Windows, kernel share modes prevent new writers during the prepared interval. On macOS/Linux, descriptor locks are advisory; the implementation rechecks descriptor/path identity, size and high-resolution change timestamps immediately before rename. Complete hashing stays in prepare so the Commit Guard remains short. The same-user adversarial case is outside this local Workspace trust model and is documented rather than falsely claimed to be kernel-enforced.

- [ ] **Step 1: Write failing PreparedBundle lifecycle tests**

Create `tests/test_portable_commit.py` with:

```python
class PreparedBundleTests(unittest.TestCase):
    def test_prepare_requires_staging_ref(self): ...
    def test_prepare_requires_matching_expected_bundle_id(self): ...
    def test_prepare_requires_matching_expected_manifest_hash(self): ...
    def test_prepare_runs_semantic_validation(self): ...
    def test_prepare_is_context_manager_and_close_is_idempotent(self): ...
    def test_prepared_bundle_can_be_consumed_only_once(self): ...
    def test_prepare_does_not_hold_workspace_mutation_lock(self): ...
    def test_commit_serializes_on_workspace_mutation_lock(self): ...
    def test_prepare_rejects_cross_volume_candidate(self): ...
    def test_commit_securely_creates_missing_bundles_parent(self): ...
    def test_mutation_after_prepare_is_detected_before_rename(self): ...
    def test_new_file_after_prepare_is_detected_before_rename(self): ...
```

Run:

```powershell
python -m unittest tests.test_portable_commit.PreparedBundleTests -v
```

Expected: FAIL because the commit module does not exist.

- [ ] **Step 2: Define the private seal and stable PreparedBundle shell**

Create `iwiki/portable/commit.py`:

```python
@dataclass(frozen=True)
class _SealedFile:
    relative_path: str
    byte_length: int
    sha256: str
    device: int
    inode: int
    modified_ns: int
    changed_ns: int
    handle: object

class PreparedBundle:
    @property
    def bundle_id(self) -> str: ...

    @property
    def manifest_sha256(self) -> str: ...

    @property
    def staging_relative_path(self) -> str: ...

    def close(self) -> None: ...
    def __enter__(self) -> "PreparedBundle": ...
    def __exit__(self, exc_type, exc, traceback) -> None: ...

def prepare_bundle_commit(
    workspace: Workspace,
    staging_ref: PortableBundleRef,
    *,
    expected_bundle_id: str,
    expected_manifest_sha256: str,
) -> PreparedBundle: ...

def commit_prepared_bundle(prepared: PreparedBundle) -> CommitResult: ...
```

`PreparedBundle` constructor and mutable internals are private. It cannot be copied or pickled. `close()` releases every file handle; `commit_prepared_bundle` marks it consumed in a `finally` block whether the attempt succeeds or raises. It is not thread-safe and must be consumed by the process that created it.

- [ ] **Step 3: Implement the prepare boundary and idempotent replay lookup**

Prepare is read-mostly and follows this order:

1. Validate SDK arguments and expected typed ID/digest; reject `workspace.read_only` for a commit preparation.
2. Check final `bundles/<expected_bundle_id>` first. If it is a valid committed Bundle with the same manifest hash, return an internal idempotent `PreparedBundle` that needs no staging directory. If it is valid with another hash, raise `IWikiError(ErrorCode.CONFLICT, ...)`; if it exists but is invalid, raise `IWikiError(ErrorCode.VALIDATION_FAILED, ...)` and never write around it.
3. Resolve the full Workspace-relative staging candidate and require its volume identity to match `<raw_personal>`; this remains valid when the `bundles/` child does not exist yet.
4. Run `validate_bundle(workspace, staging_ref, ValidationLevel.SEMANTIC)`; invalid returns are promoted to `IWikiError(ErrorCode.VALIDATION_FAILED, details={"report": report.to_dict()})`.
5. Require report bundle ID and manifest hash to equal both caller expectations.
6. Rescan, require the same normalized inventory seen by validation, then open and seal every regular file in deterministic relative-path order; creation/deletion/type change between validation and sealing is `RETRYABLE_RUNTIME`.
7. Re-read `bundle.json` and `receipt.json` through the sealed descriptors, then verify their exact hashes one more time.

Prepare may take and release the Workspace mutation lock briefly for a consistent final preflight, but the returned object must not retain it. `commit_prepared_bundle` obtains that lock only after the caller has obtained any AllToNote JobStore Commit Guard, preserving the global lock order. Bound acquisition with the existing CLI timeout convention; a timeout maps to `ErrorCode.RETRYABLE_RUNTIME`, not `CONFLICT`.

The operational lock directory `<cache>/iwiki` is created by a small package-private helper in `commit.py` using the same anchored no-link checks as `path_policy.py`; do not refactor the pre-existing Publisher lock code in this P0 change. Reject any link/reparse/non-directory component. The directory is Machine State, never part of Bundle inventory or portability.

- [ ] **Step 4: Implement platform file seals**

Windows implementation:

- open each file through the anchored path-policy helper and `CreateFileW` with `GENERIC_READ`, share flags `FILE_SHARE_READ | FILE_SHARE_DELETE` (intentionally excluding `FILE_SHARE_WRITE`), `OPEN_EXISTING`, and `FILE_FLAG_OPEN_REPARSE_POINT`;
- query `BY_HANDLE_FILE_INFORMATION`; reject reparse entries and capture volume serial/file index/size/write time;
- hash by the held handle, not by reopening the path;
- stream hashing in fixed-size chunks so large source media does not scale Runtime memory;
- hold and later verify the candidate root identity plus its staging parent anchor so a swapped `bundle.partial` path cannot authorize a different directory;
- any sharing violation while sealing means the Producer still has a writer and maps to `RETRYABLE_RUNTIME` after a short bounded retry budget.

macOS/Linux implementation:

- open with `O_RDONLY | O_CLOEXEC | O_NOFOLLOW` when available;
- descend from held directory FDs and keep the candidate root/staging-parent identities for the rename precheck;
- take non-blocking shared `fcntl.flock` locks on regular-file descriptors; these coordinate compliant Writers but are not described as protection from a malicious same-user process;
- capture `fstat` device/inode/size/mtime-ns/ctime-ns and hash from each descriptor;
- immediately before rename, repeat `fstat` from the same descriptor and compare device/inode/size/mtime-ns/ctime-ns with the sealed record. Do not rehash large payloads inside the short Commit Guard.

The implementation is split only at private helpers `_seal_file`, `_verify_seal`, and `_close_seal`; the public API and issue/error semantics are platform-independent.

- [ ] **Step 5: Write failing commit, crash-point and recovery tests**

Add:

```python
class AtomicCommitTests(unittest.TestCase):
    def test_commit_writes_bound_commit_file_then_atomically_renames(self): ...
    def test_commit_result_contains_final_relative_path_and_hashes(self): ...
    def test_same_id_and_manifest_is_idempotent(self): ...
    def test_same_id_and_different_manifest_is_conflict(self): ...
    def test_final_is_never_replaced(self): ...
    def test_rename_failure_leaves_no_partial_final(self): ...
    def test_failure_before_commit_file_keeps_retryable_staging(self): ...
    def test_failure_after_commit_file_is_recoverable(self): ...
    def test_partial_commit_file_fails_closed_and_preserves_staging(self): ...
    def test_replay_after_success_without_staging_is_idempotent(self): ...
    def test_fsync_happens_before_rename_and_on_final_parent_after_rename(self): ...
```

Patch private adapter functions to inject failure at exactly these boundaries:

```text
after final preflight
after every seal is verified
after commit.json fsync
before no-replace rename
after no-replace rename but before final-parent fsync
```

Do not expose fault-injection hooks in the SDK.

- [ ] **Step 6: Write and bind `commit.json` deterministically**

Use `write_small_control_file` to create, never replace, this exact shape inside the candidate:

```json
{
  "commit_protocol_version": 1,
  "bundle_id": "bnd_...",
  "manifest": {
    "path": "bundle.json",
    "byte_length": 12345,
    "sha256": "sha256:..."
  },
  "committed_at": "2026-07-14T00:00:00.000Z"
}
```

Production uses current UTC rounded/formatted to exact milliseconds. Tests inject time by patching private `_utc_now_millis`, not by adding a public clock parameter.

After creating or accepting a recovery `commit.json`, seal that file too and compute `CommitResult.commit_sha256` from its exact held bytes. A commit result never hashes a reserialized object.

Crash recovery rule: if a previous attempt left a complete `commit.json` in staging, `prepare_bundle_commit` may accept it only through a private recovery path that verifies every field and exact manifest binding. It validates the candidate as staging with that one known control excluded from the unexpected-file check, seals the existing `commit.json`, and resumes the same no-replace rename. A partial, malformed or differently bound file is `VALIDATION_FAILED`; it is never overwritten or deleted automatically, preserving forensic staging data.

- [ ] **Step 7: Implement no-replace directory rename and durability**

Private `_rename_directory_no_replace(source, destination)` uses:

- Windows: `MoveFileExW(source, destination, MOVEFILE_WRITE_THROUGH)` without `MOVEFILE_REPLACE_EXISTING`;
- macOS: `renameatx_np(source_parent_fd, source_name, destination_parent_fd, destination_name, RENAME_EXCL)` so the held parent anchors participate in the rename;
- Linux: `renameat2(AT_FDCWD, source, AT_FDCWD, destination, RENAME_NOREPLACE)` through `ctypes`;
- another platform or unavailable primitive: fail closed with `RETRYABLE_RUNTIME`; never fall back to overwriting `os.rename`.

Before rename:

1. verify all seals;
2. write/recover, seal and fsync `commit.json`;
3. rescan through the held tree anchor and require the exact prepared inventory plus that one Committer-owned file—no added, removed or type-changed entry—and perform the fast identity/size/change-time seal check;
4. fsync the candidate directory;
5. acquire `workspace.paths.cache / "iwiki" / "portable-mutation.lock"`;
6. securely create `<raw_personal>/bundles` if absent using one anchored, non-link directory creation and fsync `<raw_personal>`; reject a non-directory/reparse/link entry;
7. confirm final still does not exist or resolve same-hash idempotency/different-hash conflict.

After rename, fsync the final directory and its parent where the platform supports directory handles. On Windows, `MOVEFILE_WRITE_THROUGH` is the required rename durability primitive; inability to open a directory for an additional flush must not trigger an unsafe second rename. If the rename reports “destination exists”, validate final: same manifest is idempotent, different manifest is `CONFLICT`.

- [ ] **Step 8: Export the stable commit API and run focused tests**

Update `iwiki/portable/__init__.py` to export `PreparedBundle`, `prepare_bundle_commit`, and `commit_prepared_bundle`, then run:

```powershell
python -m unittest tests.test_portable_commit -v
python -m unittest tests.test_portable_validation tests.test_portable_semantics -v
```

Expected: lifecycle, concurrent-writer, mutation, crash, replay, idempotency and conflict tests pass; the read-only validator suite remains green.

- [ ] **Step 9: Commit the atomic commit boundary**

Run:

```powershell
git add iwiki/portable/__init__.py iwiki/portable/commit.py iwiki/portable/validator.py tests/test_portable_commit.py
git diff --cached --check
git commit -m "feat(iwiki): atomically commit prepared bundles"
```

Expected: the commit contains no CLI parser and no Producer implementation.

---

## Task 7: Publish the Stable SDK and Additive Capability Negotiation

**Files:**

- Modify: `iwiki/portable/__init__.py`
- Modify: `iwiki/portable/contract.py`
- Modify: `iwiki/cli.py`
- Modify: `tests/test_portable_contract.py`
- Modify: `tests/test_iwiki_cli.py`
- Modify: `tests/golden/inspect-v1.json`

- [ ] **Step 1: Write failing public-import and inspect tests**

Add a public-surface assertion that imports only from `iwiki.portable` and checks exact `__all__`. Add CLI inspect assertions for this additive object:

```json
"portable_contract": {
  "iwiki_sdk_api_version": 1,
  "contract_id": "iwiki-portable-contract-v1",
  "schema_set_id": "2026-07-portable-v1",
  "schema_set_sha256": "sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9",
  "bundle_schema_versions": [1],
  "source_schema_versions": [1],
  "source_revision_schema_versions": [1],
  "artifact_schema_versions": [1],
  "evidence_ref_schema_versions": [1],
  "receipt_schema_versions": [1],
  "transcript_schema_versions": [1],
  "evidence_set_schema_versions": [1],
  "quality_report_schema_versions": [1],
  "commit_protocol_versions": [1],
  "supported_required_contracts": [],
  "locator_schemes": ["video-time-range.v1", "audio-time-range.v1", "text-span.v1", "document-page.v1", "presentation-slide.v1", "web-section.v1", "wiki-revision-section.v1", "git-file-lines.v1", "code-symbol.v1", "git-commit.v1", "record-id.v1"],
  "validation_levels": ["structure", "integrity", "closure", "semantic"]
}
```

And additive capability strings:

```json
"portable_bundle_validate_v1",
"portable_bundle_commit_v1"
```

Run:

```powershell
python -m unittest tests.test_portable_contract tests.test_iwiki_cli -v
```

Expected: FAIL because inspect does not expose the new capability.

- [ ] **Step 2: Freeze exact SDK exports and argument failures**

`iwiki/portable/__init__.py` must export exactly the interfaces listed under “Public Interfaces Frozen by This Plan”; package-private helpers stay inaccessible by convention.

Update public functions so bad enum/type/ref/value arguments raise `IWikiError(ErrorCode.INVALID_ARGUMENT, ...)`, never raw `TypeError`, `ValueError`, `KeyError`, `JSONDecodeError` or `OSError`. Validation findings inside a syntactically valid request still return `PortableValidationReport`.

Add DTO serialization helpers only where the existing CLI envelope needs them. Do not introduce a general serialization framework.

- [ ] **Step 3: Extend `iwiki inspect` without scanning Workspace content**

Add the two frozen capability strings to the existing constant in `iwiki/cli.py`. In the inspect handler, call `inspect_portable_contract(workspace)` and serialize the small packaged catalog.

The inspect path may read:

- Workspace config already read by the existing command;
- `schema-set.json` and the packaged schema bytes needed for the deterministic fingerprint.

It must not import `validator.py`, call `scan_bundle_tree`, enumerate `raw/personal`, or hash user Bundle content. Add a mock assertion proving those calls do not occur.

- [ ] **Step 4: Update the golden response and run compatibility tests**

Regenerate only the additive golden fields deliberately, then run:

```powershell
python -m unittest tests.test_portable_contract tests.test_iwiki_cli -v
python -m unittest tests.test_manifest_contract tests.test_iwiki_query tests.test_iwiki_index -v
```

Expected: CLI protocol remains `1`, Workspace schema remains `2`, old fields are byte-for-byte unchanged after normal JSON normalization, and new capability fields are present.

- [ ] **Step 5: Commit the SDK/capability surface**

Run:

```powershell
git add iwiki/portable/__init__.py iwiki/portable/contract.py iwiki/cli.py tests/test_portable_contract.py tests/test_iwiki_cli.py tests/golden/inspect-v1.json
git diff --cached --check
git commit -m "feat(iwiki): expose portable sdk capabilities"
```

Expected: only additive public surface changes.

---

## Task 8: Add Thin `iwiki portable validate` and `iwiki portable commit` Commands

**Files:**

- Modify: `iwiki/cli.py`
- Create: `tests/test_portable_cli.py`

- [ ] **Step 1: Write subprocess-first CLI tests**

Create `tests/test_portable_cli.py` and invoke the real module entry point in a subprocess. Required cases:

```python
class PortableValidateCliTests(unittest.TestCase):
    def test_validate_staging_emits_one_success_json_document(self): ...
    def test_validate_committed_bundle_emits_one_success_json_document(self): ...
    def test_validate_requires_exactly_one_reference_kind(self): ...
    def test_validate_accepts_all_four_levels(self): ...
    def test_invalid_bundle_uses_validation_failed_exit_code(self): ...
    def test_json_flag_is_required_like_existing_iwiki_commands(self): ...

class PortableCommitCliTests(unittest.TestCase):
    def test_commit_requires_expected_id_and_manifest_hash(self): ...
    def test_commit_returns_machine_readable_result(self): ...
    def test_idempotent_replay_is_success(self): ...
    def test_conflict_uses_existing_conflict_exit_code(self): ...
    def test_error_payload_does_not_leak_absolute_workspace_path_or_secret(self): ...
```

Run:

```powershell
python -m unittest tests.test_portable_cli -v
```

Expected: FAIL because the parser has no `portable` command.

- [ ] **Step 2: Add the parser grammar**

Extend the existing parser with:

```text
iwiki portable validate --workspace <path> (--staging <workspace-relative-bundle.partial> | --bundle <bnd_id>)
                        [--level structure|integrity|closure|semantic] --json

iwiki portable commit --workspace <path> --staging <workspace-relative-bundle.partial>
                      --expected-bundle-id <bnd_id>
                      --expected-manifest-sha256 <sha256:...> --json
```

Use an argparse mutually-exclusive required group for `--staging`/`--bundle`. Default validation level is `semantic`. `--staging` accepts only the full Workspace-relative path returned/constructed under `<raw_personal>/.staging/.../bundle.partial`; it never accepts an absolute filesystem path. Do not add flags for overwrite, merge, copy fallback, disabling checks, or arbitrary commit destination.

Add the parent command `portable` to the existing supported-command gate and use required subparsers for `validate` and `commit`; an unknown/missing portable subcommand remains an `INVALID_ARGUMENT` parser failure rather than falling through to another command.

- [ ] **Step 3: Keep handlers as SDK adapters**

The validate handler performs only:

1. parse arguments into `Workspace`, `PortableBundleRef`, and `ValidationLevel`;
2. call `validate_bundle`;
3. serialize the DTO through the existing response envelope;
4. if invalid, raise `IWikiError(ErrorCode.VALIDATION_FAILED, details={"report": ...})` so the existing top-level error/exit machinery remains authoritative.

The commit handler performs only:

```python
with prepare_bundle_commit(
    workspace,
    ref,
    expected_bundle_id=args.expected_bundle_id,
    expected_manifest_sha256=args.expected_manifest_sha256,
) as prepared:
    result = commit_prepared_bundle(prepared)
```

No validator rule, filesystem mutation or idempotency decision may be duplicated in `cli.py`.

- [ ] **Step 4: Freeze JSON output and sanitation**

Validate success data:

```json
{
  "valid": true,
  "level": "semantic",
  "state": "staging",
  "bundle_id": "bnd_...",
  "manifest_sha256": "sha256:...",
  "issues": []
}
```

Commit success data:

```json
{
  "bundle_id": "bnd_...",
  "manifest_sha256": "sha256:...",
  "commit_sha256": "sha256:...",
  "relative_path": "raw/personal/bundles/bnd_...",
  "idempotent": false
}
```

The required JSON mode writes exactly one JSON document to stdout through the existing envelope policy; unexpected diagnostics go to stderr. Issue paths are Bundle-relative. Error details must not include API keys, URL query strings, prompt bodies, transcript contents, absolute Workspace paths, or Python tracebacks.

- [ ] **Step 5: Run CLI and legacy protocol tests**

Run:

```powershell
python -m unittest tests.test_portable_cli tests.test_iwiki_cli -v
python -m unittest tests.test_iwiki_packaging -v
```

Expected: new commands pass, all old commands keep their prior exit codes/output contracts, and `python -m iwiki.cli --help` shows the additive `portable` group.

- [ ] **Step 6: Commit the thin CLI adapter**

Run:

```powershell
git add iwiki/cli.py tests/test_portable_cli.py
git diff --cached --check
git commit -m "feat(iwiki): add portable validate and commit cli"
```

Expected: CLI business logic remains a direct SDK call.

---

## Task 9: Check In Golden Bundles, Package Resources, and Publish Producer Documentation

**Files:**

- Create: `tests/fixtures/portable/v1/valid-minimal/**`
- Create: `tests/fixtures/portable/v1/invalid/**`
- Modify: `tests/test_portable_contract.py`
- Modify: `tests/test_portable_validation.py`
- Modify: `tests/test_iwiki_packaging.py`
- Create: `docs/portable-contract-v1.md`
- Modify: `docs/wiki-architecture-v2.md`
- Modify: `README.md`

- [ ] **Step 1: Materialize a deterministic committed golden Bundle**

Use the test factory once to generate `tests/fixtures/portable/v1/valid-minimal`, then inspect and check in the exact bytes. It must include a valid `commit.json` and no machine-specific path, random ID, current timestamp, cache file or temporary artifact.

Add a test that copies this fixture into a fresh Workspace and validates it at all four levels. Add a reproducibility test that rebuilds it with the frozen factory clock/IDs and compares every relative filename and byte exactly.

Add a schema/Reference-Validator coverage test: for each core object schema, derive its top-level `required` field list from the packaged JSON, remove one field at a time from the matching factory object, and require Structure failure. For every `additionalProperties: false` core object, inject one unknown field and require failure. Content schemas with `oneOf` use one valid factory record per branch. This is a test-only conformance matrix, not a runtime JSON-Schema dependency.

- [ ] **Step 2: Add minimal invalid golden fixtures**

Under `tests/fixtures/portable/v1/invalid`, check in one minimal fixture for each external interoperability boundary that a third-party Producer is likely to get wrong:

```text
bad-line-endings/
duplicate-key/
path-alias-collision/
receipt-hash-mismatch/
dependency-pin-mismatch/
missing-evidence/
quality-subject-hash-mismatch/
```

Each directory contains `expected.json` with only:

```json
{"level":"integrity","codes":["hash_mismatch"]}
```

Tests discover these directories, validate, and compare ordered codes. Do not add dozens of full copied Bundles: use the smallest standalone bytes needed for cross-implementation fixtures; keep combinatorial mutations in the factory-based unit tests.

- [ ] **Step 3: Prove packaged/offline contract availability**

Extend `tests/test_iwiki_packaging.py` to:

1. build a wheel with the project’s existing packaging command;
2. inspect the wheel ZIP and require `iwiki/portable/contracts/v1/schema-set.json` plus every cataloged schema;
3. install the wheel into an isolated temporary virtual environment;
4. run from a working directory outside the repository with network disabled by test convention;
5. import `iwiki.portable`, inspect the contract fingerprint, validate the copied golden Bundle, and run both CLI commands.

The installed fingerprint must equal the source-tree fingerprint and the golden inspect value.

- [ ] **Step 4: Write third-party Producer documentation**

Create `docs/portable-contract-v1.md` with these sections:

1. Contract/version negotiation through `iwiki inspect`.
2. Physical staging/final tree and ownership handoff.
3. Exact control-file byte rules and schema download-from-package location.
4. Typed IDs, hashes, timestamps and portable paths.
5. Minimal Bundle example linked to the golden fixture.
6. Validation levels and stable issue codes.
7. `validate_bundle` / PreparedBundle SDK example.
8. `iwiki portable validate/commit` examples.
9. Idempotency, conflict, crash recovery and retry rules.
10. Security/trust boundary, including Windows enforced sharing versus POSIX protocol cooperation.
11. Explicit P0 exclusions: CAS, signing, encryption, import/export, migration, trash, Publisher, Desktop, MCP and AllToNote Producer.

Update `docs/wiki-architecture-v2.md` with a short link and the fact that `raw/personal/bundles` is immutable committed input to later Publisher workflows. Update `README.md` only with installation-neutral SDK/CLI discovery commands; do not reposition llm-iwiki as the AllToNote application.

- [ ] **Step 5: Verify docs, fixtures and package together**

Run:

```powershell
python -m unittest tests.test_portable_contract tests.test_portable_validation tests.test_iwiki_packaging -v
python -m iwiki.cli inspect --workspace tests/fixtures/workspaces/valid-v2 --json
$smoke = Join-Path ([System.IO.Path]::GetTempPath()) ("iwiki-portable-smoke-" + [guid]::NewGuid().ToString("N"))
Copy-Item -LiteralPath 'tests/fixtures/workspaces/valid-v2' -Destination $smoke -Recurse
$bundleParent = Join-Path $smoke 'raw/personal/bundles'
New-Item -ItemType Directory -Path $bundleParent -Force | Out-Null
Copy-Item -LiteralPath 'tests/fixtures/portable/v1/valid-minimal' -Destination (Join-Path $bundleParent 'bnd_01900000-0000-7000-8000-000000000001') -Recurse
python -m iwiki.cli portable validate --workspace $smoke --bundle bnd_01900000-0000-7000-8000-000000000001 --level semantic --json
```

Expected: schema fingerprint agrees in source, wheel and inspect; golden valid/invalid expectations pass; public examples match the actual parser. The GUID-named temporary directory avoids deleting or overwriting any pre-existing path; the test process may clean up only that directory in its own teardown.

- [ ] **Step 6: Commit interoperability artifacts and docs**

Run:

```powershell
git add README.md docs/portable-contract-v1.md docs/wiki-architecture-v2.md tests/fixtures/portable tests/test_portable_contract.py tests/test_portable_validation.py tests/test_iwiki_packaging.py
git diff --cached --check
git commit -m "docs(iwiki): publish portable producer contract"
```

Expected: public documentation, package assertions and golden fixtures land together so they cannot drift silently.

---

## Task 10: Run the P0 Fault Matrix and Release Gate

**Files:**

- Modify only if a test exposes a defect in files already owned by Tasks 1–9.

- [ ] **Step 1: Run static repository hygiene checks**

Run from the implementation worktree:

```powershell
git status --short
git diff --check 2b6db85...HEAD
git diff --name-only 2b6db85...HEAD
```

Expected:

- no `.superpowers/`, Workspace `raw/`/`wiki/` content, SQLite database, cache, model, transcript or generated build artifact is tracked;
- every changed path appears in this plan’s File Responsibility Map;
- no unrelated refactor is present.

- [ ] **Step 2: Run the complete test suite**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: the 332-test/6-skip planning baseline plus all new Portable tests pass. Any additional skip must name an unavailable platform fixture; core validation/commit behavior may not be skipped on the current host.

- [ ] **Step 3: Run the commit fault matrix on the current platform**

Run:

```powershell
python -m unittest tests.test_portable_commit.AtomicCommitTests -v
python -m unittest tests.test_portable_commit.PreparedBundleTests -v
```

For each injected crash point, assert the filesystem is in exactly one recoverable state:

| Crash boundary | Allowed staging state | Allowed final state | Retry result |
|---|---|---|---|
| before `commit.json` | original valid candidate | absent | normal commit |
| after `commit.json` fsync | bound recovery candidate | absent | resume commit |
| before rename | bound recovery candidate | absent | resume commit |
| rename reports destination exists | candidate may exist | one valid immutable final | idempotent or conflict by hash |
| after successful rename | absent | one valid immutable final | idempotent from expected ID/hash |

No row allows a partially copied final directory, overwritten prior Bundle, or automatic deletion of forensic staging data.

Windows is the required P0 release platform. Run the same focused commit suite on a real macOS/APFS CI runner before documenting macOS as supported. If no macOS runner is available in this phase, keep the `renamex_np` adapter and unit tests but label macOS “implemented, not release-verified”; do not infer support from mocked Windows tests.

- [ ] **Step 4: Build and test the installed wheel outside the repository**

Use the repository’s packaging test plus a manual smoke run in a temporary venv:

```powershell
$wheelOut = Join-Path ([System.IO.Path]::GetTempPath()) ("iwiki-wheel-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $wheelOut | Out-Null
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir $wheelOut
python -m unittest tests.test_iwiki_packaging -v
```

Expected: wheel builds, contains all schema resources, installed inspect works outside the source tree, and no network access is required. Remove only build artifacts created by this step if they are untracked; do not touch user files.

- [ ] **Step 5: Verify public contract consistency mechanically**

Run a short read-only script from PowerShell that imports `iwiki.portable` and asserts:

- `__all__` equals the frozen interface list;
- catalog entries equal packaged schema filenames;
- catalog/inspect/source-wheel fingerprints agree;
- all documented stable issue codes are emitted by or explicitly reserved in one central tuple;
- CLI protocol remains `1`, Workspace schema remains `2`;
- `iwiki inspect` does not import `iwiki.portable.validator` in a clean subprocess.

Promote these assertions into existing tests if they are not already covered; do not leave an ad-hoc verification script in the repository.

- [ ] **Step 6: Perform a spec-to-diff audit**

Compare the implementation to:

```text
G:\AllToNote\docs\superpowers\specs\2026-07-14-alltonote-portable-artifact-source-bundle-design.md
```

Record the audit in the implementing agent’s final response, not a new status document:

- implemented P0 clauses;
- deliberately deferred clauses and their reason;
- test command/result;
- platform actually exercised;
- known trust-model limitation on POSIX;
- commit list.

Any P0 requirement without code plus a passing test returns to its owning task before completion.

- [ ] **Step 7: Commit only release-gate fixes, if any**

If verification required fixes:

```powershell
git add -u -- iwiki tests docs README.md pyproject.toml
git diff --cached --check
git commit -m "fix(iwiki): close portable contract release gaps"
```

Before `git add -u`, inspect `git diff --name-only` and require every path to be owned by Tasks 1–9; the isolated worktree makes this tracked-file staging command deterministic. If a release fix creates a new file, add that one reviewed path explicitly. If no fix was required, make no empty commit.

---

## Definition of Done

P0 is complete only when all statements below are true:

- A third-party Producer can discover exact contract/version support without scanning user content.
- The packaged schema set, checked-in golden Bundle and Reference Validator agree byte-for-byte.
- `validate_bundle` reports deterministic Structure, Integrity, Closure and Semantic findings without mutating the Workspace.
- `prepare_bundle_commit` binds explicit caller expectations to a single sealed candidate.
- `commit_prepared_bundle` is same-volume, no-replace, atomic, recoverable and idempotent by Bundle ID + manifest hash.
- SDK and CLI use one implementation; CLI contains no duplicated validation or commit policy.
- Existing CLI protocol, Workspace schema and 332-test baseline remain compatible.
- The installed wheel works offline and outside the repository.
- All public errors use existing top-level error categories plus stable validation issue codes.
- AllToNote, Desktop, Publisher, Engine, MCP, daemon, CAS, signing, encryption, import/export, migration and trash remain intentionally unimplemented in this phase.

## Execution Handoff

After this plan is approved, create the isolated llm-iwiki worktree from `2b6db85` and execute one task at a time. Do not implement in the dirty primary llm-iwiki tree or in the planning/reference worktree.

Two execution modes are supported:

1. **Subagent-Driven (recommended):** execute each task with a fresh implementation subagent, then run requirements review and code-quality review before advancing.
2. **Inline Execution:** execute in the current task with `superpowers:executing-plans`, preserving the same TDD, per-task commit and verification gates.
