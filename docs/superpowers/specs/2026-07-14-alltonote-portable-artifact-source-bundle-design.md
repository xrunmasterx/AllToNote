# AllToNote Portable Artifact 与 Source Bundle 设计

```yaml
doc_type: contract-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
downstream:
  - 2026-07-14-alltonote-video-producer-design.md
  - 2026-07-18-alltonote-review-publisher-design.md
implementation_status: portable-v1-foundation-and-real-iwiki-commit-implemented
last_verified_at: 2026-07-18
```

- 状态：已确认，当前有效；iwiki 已发布合同在其协议域内更高
- 日期：2026-07-14
- 上位设计：`2026-07-13-alltonote-knowledge-compiler-architecture-design.md`
- 相关设计：`2026-07-13-alltonote-cli-first-vault-workspace-design.md`
- 产品基线：开放磁盘合同 + 稳定 iwiki CLI/SDK + 托管式独立 AllToNote Runtime
- 首要平台：Windows；macOS 为第二平台 Gate

> 当前解释：本文是 AllToNote 对 Portable Artifact / Source Bundle 的消费和生产设计，不拥有 iwiki 的 Workspace、Schema、Validator、SDK、CLI、commit 或 publish 协议。正文中关于“iwiki 当前尚无某 capability”的描述属于写作时快照；实现前必须以当前 `iwiki inspect/capabilities/schema/validator` 为准。

## 1. 文档目的与范围

本文定义 AllToNote Knowledge Compiler 产出的长期、开放、可验证知识资产格式，以及这些资产在 llm-iwiki Workspace 中的提交、引用、校验、导入导出、迁移和删除边界。

它回答以下问题：

1. 一个来源、一次来源观测、转写、证据、草稿和质量报告如何成为稳定的 Portable Artifact；
2. 多个 Artifact 如何组成不可变 Source Bundle；
3. Source Bundle 如何原子写入 `raw/personal`，且不会产生半提交结果；
4. 其他 Agent、Obsidian、脚本和未来应用如何在不依赖 AllToNote 私有数据库的情况下读取资产；
5. AllToNote Core、iwiki Contract、iwiki SDK/CLI、Desktop、MCP 和 JobStore 分别拥有什么；
6. 当前 AllToNote 松散任务文件如何显式、非破坏地导入新合同；
7. Schema 如何演进，Bundle 如何导出、导入、迁移、删除和恢复；
8. 首个实现阶段必须通过哪些合同测试、故障注入和兼容性 Gate。

本文是下位数据合同设计，不重新讨论上位设计中已经确认的 Recipe、Job Engine、Publisher、Desktop 产品结构和网站边界。本文会说明它们与 Portable Bundle 的接口，但不会把所有后期能力塞入首个实现阶段。

本文不包含正式 JSON Schema 文件或实现代码。正式 Schema、Golden Fixtures、Reference Validator 和稳定 SDK/CLI 将在 Phase P0 中由 llm-iwiki 作为公开合同发布。

## 2. 已确认的产品前提

### 2.1 Knowledge Compiler 定位

AllToNote 的主要职责是把视频、文章、Wiki、PPT/PDF、代码库、UE5 模块资料、个人工作记录等散落内容，编译为可审阅、可追溯、可复用的高质量知识。

AllToNote 不是知识资产的唯一阅读器，也不是必须常驻的知识库服务器。用户应能：

- 用 AllToNote 生产和管理知识；
- 用 Obsidian 阅读普通 Markdown；
- 用任意文本工具读取 Markdown；
- 用本地 Agent 直接读取文件；
- 用 iwiki CLI/SDK/MCP 查询或管理知识；
- 删除 AllToNote Runtime 和 Desktop 后继续保留知识资产。

### 2.2 存储与发布边界

已确认的默认路径是：

1. Producer 只写 `raw/personal` 中的来源、证据、草稿和质量信息；
2. 用户审阅后，由 Publisher 安全发布到 `wiki/personal`；
3. 只有用户针对当前发布计划执行明确确认，才允许发布到 `wiki/common`；
4. `wiki/` 是正式发布结果，`raw/` 是生产资产；
5. Knowledge Read-only MCP 默认不读取 `raw/personal`。

### 2.3 双层契约

长期稳定性由两层合同共同保证：

1. **开放磁盘合同**：普通文件、公开 Schema、稳定目录、不可变 ID、哈希和引用规则；
2. **稳定 iwiki CLI/SDK**：Workspace 检查、语义校验、原子提交、索引、发布、迁移和生命周期管理。

开放磁盘合同保证数据不被某个应用锁死；iwiki CLI/SDK 保证多个 Writer 不需要各自重新实现路径安全、Schema 校验和原子操作。

## 3. 当前实现事实与迁移动机

### 3.1 当前 AllToNote 输出

当前 FastAPI/React/Tauri 视频流程主要在 `backend/note_results` 写入松散任务文件：

- `<task>.json`；
- 状态文件；
- `_audio.json`；
- `_transcript.json`；
- `_markdown.md`；
- GPT checkpoint；
- `/static/screenshots/...` 下的截图。

当前最终 JSON 可包含 Markdown、Transcript 和音频元数据，但没有统一的公开 Schema、Artifact ID、SourceRevision、内容哈希、EvidenceRef、质量报告、Portable Receipt 或原子 Bundle commit。

当前 `_markdown.md` 可能是后处理前的中间内容；最终 Markdown 以 `<task>.json` 为优先。当前状态可能在最终 JSON 完成写入前显示 SUCCESS，因此旧状态不能作为新合同下“已提交”的证据。

SQLite、Chroma、前端 IndexedDB、状态文件和 checkpoint 是机器状态或缓存，不是长期知识真相。

### 3.2 当前 iwiki 能力

llm-iwiki 稳定 CLI 工作位于独立工作树的 `codex/iwiki-stable-cli` 分支，已检查提交为 `2b6db85`。其当前事实是：

- Workspace Schema 为 2；
- CLI Protocol 为 1；
- 能力包括 `inspect`、`validate`、`query_native`、`plan_publish`、`atomic_publish` 和 `qmd_index`；
- 当前只查询 `wiki/`；
- 当前 Publisher 只处理 Markdown；
- 当前没有 Source Bundle、raw Artifact、附件、Portable Import/Export 或 `.trash` 合同。

该工作位尚未合入 llm-iwiki 主工作树，因此不能被视为已发布生产依赖。更重要的是，即使当前 CLI 可用，它也没有声明 Portable Bundle capability，AllToNote 不得把现有 `apply-publish` 偷换成 raw Bundle 提交接口。

### 3.3 迁移原则

旧结果不会被“原地升级”。Legacy Importer 必须：

- 显式执行；
- 支持 dry-run；
- 不修改源文件；
- 为每次成功导入创建新 Bundle；
- 记录旧格式、任务 ID、最终结果哈希和目标 Workspace lineage；
- 允许重复运行但不重复创建同一导入结果；
- 对无法恢复的来源、许可、时间戳和证据强度给出明确 warning。

## 4. 目标、非目标与完成标准

### 4.1 目标

本设计必须实现以下性质：

1. **开放**：任何实现都可依据公开合同读取 Bundle；
2. **不可变**：生产、审阅新版本、重新生成和迁移都创建新 Bundle，不原地覆盖；
3. **可验证**：内容、引用、依赖和控制文件由 SHA-256 和 Schema 绑定；
4. **可追溯**：Draft 可追到 Artifact、Evidence、SourceRevision、Recipe、Executor 和 Receipt；
5. **原子提交**：最终位置只出现完整旧状态或完整新 Bundle；
6. **可恢复**：进程崩溃、取消、磁盘满和 JobStore 更新失败后有确定行为；
7. **可携带**：整个 Workspace 是默认 portable closure，必要时可导出自包含闭包；
8. **跨应用**：Obsidian 和普通 Markdown 工具可阅读 Draft；
9. **低耦合**：AllToNote 不拥有 Workspace Schema，iwiki 不拥有视频或 LLM 编排；
10. **可扩展**：增加新 Recipe 时不重新发明 Job、Bundle、Evidence、CLI 或 MCP；
11. **高性能**：正常启动不扫描或哈希整个 Vault，提交成本与新写入字节相关；
12. **安全**：路径、压缩包、Markdown、远端 URL、隐私和凭据有明确边界。

### 4.2 非目标

本文不设计：

- 全局内容寻址存储或 Blob CAS；
- Bundle 数字签名或作者真实性证明；
- 内容加密协议；
- 云同步服务；
- 分布式锁和分布式 tombstone；
- 自动跨设备删除；
- 常驻 daemon；
- 通用 Workflow 可视化编辑器；
- 任意第三方代码插件；
- Obsidian 级通用 Markdown 编辑器；
- Publisher 附件目录合同；
- 自动 common 发布；
- 稳定公开的 AllToNote Python 类 API；
- 把 SQLite、Chroma、QMD、JobStore 或缓存变为 Portable Asset。

### 4.3 书面规格完成标准

本文必须明确：

- 物理骨架、ID、控制文件和编码规则；
- Source、SourceRevision、Artifact、Evidence、Draft、Quality 和 Receipt 模型；
- 引用方向、闭包、版本和迁移规则；
- 原子提交、取消竞争和崩溃恢复；
- 导入导出、Legacy、删除、GC 和性能边界；
- Core/iwiki/CLI/MCP 接口所有权；
- 错误合同、测试矩阵、故障注入、兼容 Gate 和分阶段范围。

## 5. 方案比较与最终决策

### 5.1 方案 A：Workspace 是默认 Portable Closure（采用）

每次生产、重新生成、审阅新版本或迁移创建一个不可变 Bundle。Bundle 可以通过显式、带哈希的引用依赖其他 Bundle。完整 Workspace 是默认闭包；单独传输时通过 export 计算并复制依赖闭包。

优点：

- 不要求每个 Bundle 重复拷贝所有旧 Evidence 和 Source；
- 本地增量生产成本可控；
- lineage 和跨 Bundle 质量报告自然；
- 保持普通文件结构；
- export 可以按需求生成自包含结果。

代价：

- 单个 Bundle 目录不保证独立离线完整；
- 读取、删除和导出需要解析依赖闭包；
- Workspace 级一致性比“每个 ZIP 都独立”更复杂。

### 5.2 方案 B：每个 Bundle 永远完全自包含（拒绝）

该方案复制所有来源、证据、图片和依赖，使任意 Bundle 可单独移动。

拒绝原因：

- 每次 Draft 新版本都会重复大体积 Transcript、截图和来源快照；
- 视频、PPT、代码库来源会造成明显空间放大；
- 复制不减少引用和迁移复杂度，只把复杂度转成存储成本；
- 本地常态使用并不需要每个 Bundle 都是离线包。

### 5.3 方案 C：全局 CAS + Manifest（拒绝 v1）

该方案以全局哈希对象池去重所有 payload，Bundle 只保存引用。

拒绝 v1 的原因：

- 引入 Blob GC、引用计数、并发写、损坏隔离和恢复协议；
- 普通用户更难理解和手工备份；
- 云盘、杀毒软件和跨平台文件系统复杂度更高；
- 当前没有数据证明去重收益值得这套基础设施。

未来若真实数据表明大文件重复是主要成本，可在新 capability 下增加 `workspace_blob` 表示；v1 只允许 `bundle_file`。

### 5.4 最终决策

采用方案 A，并固定以下原则：

- 整个 Workspace 是默认 portable closure；
- Bundle 永远不可变；
- 跨 Bundle 引用允许但必须显式、带目标 Bundle manifest hash；
- v1 不使用全局 CAS；
- 单 Bundle 分享通过 closure-complete 或 offline-complete export 完成；
- 所有迁移创建新 Bundle，不修改旧 Bundle。

## 6. 术语与不可破坏的不变量

### 6.1 术语

- **Source**：长期来源身份，例如一个视频 URL、Wiki 页面、文档、代码库或本地资料集合；
- **SourceRevision**：某个时间点对 Source 的不可变观测；
- **Artifact**：Bundle 中一个有类型、有哈希、有 provenance 的普通文件；
- **EvidenceRef**：把知识声明定位到特定 SourceRevision 和不可变 Artifact 的证据引用；
- **Draft**：尚未正式发布、可供人工审阅的 Markdown Artifact；
- **QualityReport**：针对精确 Artifact hash 的结构化质量结论；
- **Receipt**：本次生产运行的安全、可携带摘要；
- **Source Bundle**：一次生产、版本创建、导入或迁移的不可变 Artifact 集；
- **Workspace Closure**：从目标 Bundle 出发沿显式依赖可到达的全部 Bundle；
- **Machine State**：JobStore、事件、lease、缓存、索引、凭据和本地映射；
- **Portable Contract**：公开 Schema、路径、哈希、引用、校验和迁移规则；
- **PreparedBundle**：通过完整校验、等待短提交临界区的进程内状态。

### 6.2 不变量

1. 最终 Bundle 目录不覆盖、不追加、不原地编辑；
2. 一个 Artifact 在 v1 中对应一个普通文件；
3. ID 表示语义身份，不等于内容哈希；
4. 内容哈希绑定精确字节，不绑定“看起来相同”的文本；
5. 新对象指向旧对象；旧 Bundle 不反向修改；
6. `bundle.json` 是 Artifact、依赖和 outputs 的权威 manifest；
7. `commit.json` 绑定精确 `bundle.json` 字节；
8. 未经声明的文件不被消费，并在 committed Bundle 中构成完整性错误；
9. staging、checkpoint、JobStore 和缓存不是 Portable Asset；
10. Quality fail 不等于 Job failure；
11. Job succeeded 不等于 Draft approved；
12. Draft approved 不等于已发布；
13. 发布状态不回写旧 Bundle；
14. AllToNote 不绕过 iwiki Contract 写最终 `bundles/`；
15. 当前 iwiki 未声明 Portable capability 时必须拒绝写入；
16. 路径永远由 Workspace 合同发现，不硬编码 `raw/personal`；
17. Credential、Cookie、完整 Prompt、完整 provider 原始响应和绝对本机路径不进入 Portable Bundle；
18. 开放磁盘允许外部软件读取；外部原地修改 committed Bundle 会导致 hash mismatch，而不是合法新版本。

## 7. 物理布局、ID 与控制文件

### 7.1 Workspace 内布局

`raw_personal` 的实际路径由 `iwiki inspect` 返回。下文使用逻辑名表示：

```text
<raw_personal>/
├─ bundles/
│  └─ bnd_<uuidv7>/
│     ├─ commit.json
│     ├─ bundle.json
│     ├─ receipt.json
│     ├─ sources/
│     ├─ evidence/
│     ├─ drafts/
│     ├─ assets/
│     ├─ quality/
│     └─ refs/
├─ .staging/
│  └─ <local-instance>/
│     └─ <job>.<nonce>/
│        ├─ checkpoints/
│        └─ bundle.partial/
├─ .quarantine/
│  └─ <local-instance>/
│     └─ <job>.<nonce>/
└─ .trash/
   └─ <delete-operation-id>/
      ├─ trash.json
      └─ bnd_<uuidv7>/
```

约束：

- 标题、用户名和显示名不参与路径；
- Bundle 最终目录名必须等于 `bundle_id`；
- `.staging` 和 `.quarantine` 按 local instance 隔离；
- 每台机器只清理自己的 staging/quarantine；
- `.trash` 是 Portable 生命周期操作区，不是 Machine Cache；
- final、staging 和 trash 的 rename 必须在同一卷；
- 不使用 symlink、junction、reparse point 或依赖 hardlink 的表示；
- 不把 OneDrive/Dropbox 的远端复制语义当成本地 atomic rename 保证。

`sources/`、`evidence/`、`drafts/`、`assets/`、`quality/` 和 `refs/` 是保留的逻辑分组目录；没有对应 Artifact 时可以省略空目录。最终 Bundle 必须包含 `bundle.json`、`receipt.json` 和 `commit.json`，其余普通文件必须全部由 manifest 声明。

### 7.2 Typed ID

v1 使用带类型前缀的 UUIDv7：

| 对象 | 前缀 | 示例 |
|---|---|---|
| Bundle | `bnd_` | `bnd_019...` |
| Source | `src_` | `src_019...` |
| SourceRevision | `rev_` | `rev_019...` |
| Artifact | `art_` | `art_019...` |
| EvidenceRef | `ev_` | `ev_019...` |
| Portable Run | `run_` | `run_019...` |
| External Local Binding | `ext_` | `ext_019...` |

ID 规则：

- ID 在创建后永不改变；
- ID 不是内容哈希；
- 相同字节的两个 Artifact 可以有不同 Artifact ID；
- 去重判断可以使用 hash，但不能用 hash 代替 lineage；
- UUIDv7 提供近似时间排序，不作为权威时间；
- 时间以显式 RFC3339 字段为准。

### 7.3 控制文件编码

`bundle.json`、`receipt.json`、`commit.json` 和其他 JSON 控制文件使用：

- UTF-8；
- 无 BOM；
- LF；
- 文件末尾一个 LF；
- 时间为 RFC3339 UTC、`Z`、毫秒精度；
- digest 格式为 `sha256:<lowercase-hex>`；
- 大小字段名为 `byte_length`；
- manifest 路径使用 `/`，且必须是 Bundle 相对路径；
- 不允许 NUL、绝对路径、`.`、`..`、空 segment 或平台设备名。

哈希绑定精确文件字节。Reader 不得在验证前重新格式化 JSON、转换换行或 Unicode normalization。

v1 不要求所有 JSON 使用某种 canonical key ordering。Writer 可以稳定排序以便 diff，但 hash 只关心已写入的精确字节。

### 7.4 控制文件角色

- `bundle.json`：Bundle manifest，声明所有 Artifact、依赖、outputs、Receipt 和扩展；
- `receipt.json`：生产运行摘要，被 `bundle.json` 以路径、大小和 hash 绑定；
- `commit.json`：最终提交记录，由权威 Committer 在 rename 前生成，绑定 `bundle.json` 精确字节；
- Transcript、EvidenceSet 和 QualityReport 是 Artifact，不是额外隐式数据库；
- Artifact 不使用 per-file sidecar；其 envelope 集中保存在 `bundle.json`。

`commit.json` 是 Schema 明确声明的特殊控制文件，不属于“未声明文件”，也不需要在 `bundle.json` 中以 Artifact 自引用。

## 8. `bundle.json` 顶层模型

### 8.1 逻辑结构

`bundle.json` 顶层至少包含：

```json
{
  "$schema": "urn:iwiki:portable:bundle:v1",
  "bundle_schema_version": 1,
  "bundle_id": "bnd_...",
  "created_at": "2026-07-14T08:10:20.123Z",
  "producer": {},
  "sources": [],
  "source_revisions": [],
  "dependencies": [],
  "artifacts": [],
  "outputs": {},
  "receipt": {},
  "required_contracts": [],
  "extensions": {}
}
```

顶层不保存：

- Job 当前状态；
- lease、PID、fencing token；
- checkpoint；
- Secret；
- 绝对路径；
- provider 原始请求/响应；
- UI 状态；
- QMD 或 Chroma 数据；
- `latest` 指针；
- 当前 Draft approval；
- 当前 publication 状态。

### 8.2 独立版本

以下版本独立演进：

- Bundle Schema；
- Source Schema；
- SourceRevision Schema；
- Artifact Envelope Schema；
- EvidenceRef Schema；
- Receipt Schema；
- Transcript content schema；
- EvidenceSet content schema；
- QualityReport content schema；
- Locator scheme；
- Commit protocol；
- Workspace Schema；
- iwiki CLI Protocol；
- iwiki SDK API；
- AllToNote Automation Protocol；
- Recipe ID/version。

不得把它们合并为一个“软件版本”。

### 8.3 严格核心与扩展

- 核心字段严格校验，未知核心字段按对应 Schema 规则处理；
- 扩展字段只能进入 namespaced `extensions`；
- 扩展 key 使用可追责命名空间，不允许占用 `iwiki` 或 `alltonote` 保留空间；
- Reader 可以忽略不影响核心语义的未知可选扩展；
- 影响解释、校验或安全的扩展必须出现在 `required_contracts`；
- 未识别 required contract 时状态为 `unsupported_schema`，不能猜测读取或迁移。

### 8.4 Producer

`producer` 记录足以重现解释环境的安全标识：

- 产品 ID；
- Runtime 版本；
- Recipe ID/version；
- Capability ID/version；
- Portable Contract ID；
- 可选 Feature Pack ID/version。

它不记录 Secret、完整环境变量、安装绝对路径或未经脱敏的设备身份。

### 8.5 Outputs

`outputs` 显式声明本次 Recipe 的结果角色，例如：

```json
{
  "primary_draft": "art_...",
  "transcript": "art_...",
  "evidence_set": "art_...",
  "quality_reports": ["art_..."],
  "source_snapshots": ["art_..."],
  "display_assets": ["art_..."]
}
```

UI、CLI、MCP 和 Publisher 不得通过文件扩展名、目录名或“第一个 Markdown”猜测输出角色。

不同 Recipe 可以定义不同 output profile，但所用角色必须由版本化 Recipe Contract 声明。

`receipt` 不是自由文本，而是对 `receipt.json` 的精确绑定：

```json
{
  "path": "receipt.json",
  "byte_length": 2345,
  "sha256": "sha256:..."
}
```

## 9. Source 与 SourceRevision

### 9.1 Source 是长期身份

Source 表示“这是什么来源”，而不是某次下载结果。逻辑字段包括：

```json
{
  "source_schema_version": 1,
  "source_id": "src_...",
  "source_kind": "video",
  "canonical_identity": {
    "scheme": "url",
    "value": "https://example.invalid/video/123"
  },
  "display": {
    "title": "Display title"
  },
  "extensions": {}
}
```

`canonical_identity` 必须是可比较的稳定身份，但不承诺所有网站 URL 永远稳定。不同 Source Adapter 负责定义其 scheme 和规范化规则，并由 Recipe/Capability 版本固定。

显示标题、作者名和文件名不是 Source 身份。它们变化时不创建新 Source；身份语义变化时创建新 Source。

跨 Bundle 引用 Source 使用：

```json
{
  "bundle_id": "bnd_...",
  "source_id": "src_..."
}
```

### 9.2 SourceRevision 是不可变观测

SourceRevision 表示“在这个时间点看到了什么”，逻辑字段包括：

```json
{
  "source_revision_schema_version": 1,
  "source_revision_id": "rev_...",
  "source_ref": {
    "bundle_id": "bnd_...",
    "source_id": "src_..."
  },
  "captured_at": "2026-07-14T08:10:20.123Z",
  "observed_revision": {},
  "content_digest": "sha256:...",
  "materialization": {},
  "license": {},
  "privacy": "personal",
  "freshness": {},
  "extensions": {}
}
```

如果不能计算 `content_digest`，必须给出版本化 unavailable reason，不能省略后假装内容固定。

`observed_revision` 可以记录来源提供的稳定版本，例如：

- 视频平台内容 ID 和更新时间；
- Wiki revision ID；
- Git commit/tree/blob；
- HTTP ETag/Last-Modified；
- 本地文件 hash 和采集时间；
- 文档版本号。

外部 revision 声明不能替代本地内容 hash。它是来源事实，不是 Portable Integrity 根。

### 9.3 Materialization

v1 支持三种 materialization：

#### `archived`

来源快照已经作为当前 Bundle Artifact 保存。materialization 指向当前 Bundle 内的 Artifact ID 和 hash。

适用于：

- Transcript；
- 许可允许保存的网页规范化文本；
- 文档页文本；
- 代码片段或固定 commit 的快照；
- 来源元数据；
- 截图和证据图片。

#### `reference_only`

只保存稳定来源引用和观测信息，不保存完整来源内容。

适用于：

- 许可不允许归档；
- 内容体积或政策不允许复制；
- 只能远端查询；
- 当前采集只获得元数据。

`reference_only` 不自动满足强 Evidence 要求。Draft 若需要可离线验证的强证据，仍需指向允许携带的 evidence Artifact。

#### `external_local`

内容存在用户本机其他位置，但不进入 Bundle。Portable 数据只保存 `external_ref_id`，例如 `ext_...`。绝对路径只保存在 Machine State 的本地映射中。

规则：

- Bundle 不包含绝对路径；
- 换机器后该绑定可以是 unresolved；
- export 不静默把外部文件复制进包；
- 用户若希望携带它，必须显式创建新的 archived SourceRevision 和新 Bundle。

### 9.4 Privacy 与 License

privacy 使用以下核心值：

- `public`；
- `personal`；
- `sensitive`；
- `confidential`；
- `unknown`。

license 至少表达：

- 状态：`known`、`unknown`、`restricted`；
- archive permission：`allowed`、`disallowed`、`unknown`；
- 可选的许可标识、来源说明或用户确认引用。

许可不明时不能自动解释为允许归档或允许发布。Recipe、export 和 Publisher 必须根据 policy 决定允许保存的内容范围。

### 9.5 Freshness

freshness 描述观测的时效事实，例如：

- captured_at；
- source-provided modified time；
- expected refresh policy；
- stale-after 或 unavailable reason；
- 当前观测是否为完整快照。

Freshness 是质量和提示依据，不改变 SourceRevision 的不可变性。重新抓取创建新 SourceRevision 和新 Bundle。

## 10. Artifact Envelope 与跨 Bundle 引用

### 10.1 Artifact Envelope

每个 Artifact 在 `bundle.json` 中有一个 envelope：

```json
{
  "artifact_schema_version": 1,
  "artifact_id": "art_...",
  "artifact_type": "knowledge.draft.markdown.v1",
  "payload": {
    "representation": "bundle_file",
    "path": "drafts/art_....md",
    "media_type": "text/markdown",
    "charset": "utf-8",
    "byte_length": 12345,
    "sha256": "sha256:..."
  },
  "created_at": "2026-07-14T08:10:20.123Z",
  "parents": [],
  "source_revision_refs": [],
  "generated_by": {},
  "generation": {},
  "quality_report_refs": [],
  "extensions": {}
}
```

约束：

- 一个 Artifact 对应一个普通文件；
- envelope 不复制完整 payload；
- `artifact_type` 表示领域角色；
- `media_type` 表示文件编码；
- 二者独立，不能用 `.md` 推断领域角色；
- path 必须在当前 Bundle 内，且规范化后不越界；
- path 大小写冲突按目标平台最严格规则拒绝；
- `charset` 只对文本媒体存在；
- `bundle_file` 是 v1 唯一 payload representation。

未来 `workspace_blob` 必须通过新 capability 和迁移设计引入，不能让 v1 Reader 猜测。

### 10.2 Artifact Type

首批核心 Artifact Type 至少覆盖：

- `source.metadata.v1`；
- `source.snapshot.text.v1`；
- `source.snapshot.document.v1`；
- `evidence.transcript.v1`；
- `evidence.reference-set.v1`；
- `evidence.asset.v1`；
- `knowledge.draft.markdown.v1`；
- `quality.report.v1`。

新增类型必须说明：

- 内容 schema；
- 允许的 media type；
- 是否可作为 Evidence target；
- 是否允许出现在 Recipe outputs；
- 是否需要 required contract；
- Reader 不理解时的降级行为。

### 10.3 Parent Relation

新 Artifact 可以指向旧 Artifact，relation 至少包括：

- `derived_from`；
- `supersedes`；
- `evaluates`；
- `migrates_from`；
- `imports`；
- `publishes`。

引用方向固定为新对象指向旧对象。旧 Bundle 不增加 back-reference；反向关系由 Machine Index 重建。

### 10.4 ArtifactRef

跨 Bundle ArtifactRef 必须包含：

```json
{
  "bundle_id": "bnd_...",
  "artifact_id": "art_...",
  "sha256": "sha256:..."
}
```

Artifact ID 防止语义混淆，hash 防止目标内容被替换。只提供路径或只提供 ID 均不足以构成稳定跨 Bundle 引用。

### 10.5 SourceRevisionRef

SourceRevisionRef 使用：

```json
{
  "bundle_id": "bnd_...",
  "source_revision_id": "rev_..."
}
```

其目标 Bundle 的 manifest hash 必须由 `dependencies` 固定。

### 10.6 Dependencies

每个跨 Bundle 依赖声明：

```json
{
  "bundle_id": "bnd_...",
  "bundle_manifest_sha256": "sha256:...",
  "used_source_ids": ["src_..."],
  "used_source_revision_ids": ["rev_..."],
  "used_artifact_ids": ["art_..."]
}
```

规则：

- 依赖必须最小且显式；
- 不能只声明“依赖整个 Workspace”；
- closure validation 检查目标 Bundle、manifest hash 和所列 ID；
- 目标 Bundle 缺失时状态为 `dependency_missing`；
- manifest hash 不符时为 `integrity_mismatch`；
- export 依赖该图计算闭包；
- 删除 impact analysis 使用反向索引重建 dependents。

## 11. EvidenceRef 与 Locator

### 11.1 EvidenceRef 不变量

EvidenceRef 必须同时绑定：

1. SourceRevision；
2. 不可变 Evidence target Artifact；
3. 目标 Artifact hash；
4. 版本化 locator；
5. excerpt hash、content hash 或能验证定位内容的等价字段。

它不能只保存一个远端 URL、一个 CSS selector 或一段未绑定来源的文本。

逻辑结构示例：

```json
{
  "evidence_ref_schema_version": 1,
  "evidence_id": "ev_...",
  "source_revision_ref": {
    "bundle_id": "bnd_...",
    "source_revision_id": "rev_..."
  },
  "target_artifact_ref": {
    "bundle_id": "bnd_...",
    "artifact_id": "art_...",
    "sha256": "sha256:..."
  },
  "locator": {},
  "excerpt_sha256": "sha256:...",
  "extensions": {}
}
```

EvidenceRef 记录在 `evidence.reference-set.v1` JSONL Artifact 中，不把数千条引用膨胀进 `bundle.json`。

EvidenceSet 使用 `application/x-ndjson`、UTF-8、LF 和文件末尾 LF。第一行是带 schema version、Bundle ID 和 record count 的 header；后续每行一个 EvidenceRef。Evidence ID 在该集合内唯一，header count 与实际记录数必须一致。

### 11.2 Locator Scheme

v1 定义：

| Scheme | 核心定位 |
|---|---|
| `video-time-range.v1` | 视频毫秒范围 |
| `audio-time-range.v1` | 音频毫秒范围 |
| `text-span.v1` | UTF-8 字节范围 |
| `document-page.v1` | 页码和可选 bbox |
| `presentation-slide.v1` | 幻灯片编号和可选 bbox |
| `web-section.v1` | 归档规范化文本中的 section/span |
| `wiki-revision-section.v1` | 固定 revision 和 section |
| `git-file-lines.v1` | 固定 commit/blob 下的文件行范围 |
| `code-symbol.v1` | 固定代码版本中的 symbol identity |
| `git-commit.v1` | 固定 commit |
| `record-id.v1` | 固定数据集/记录 ID |

### 11.3 时间范围

- 使用整数毫秒；
- 半开区间 `[start_ms, end_ms)`；
- `start_ms >= 0`；
- `end_ms > start_ms`；
- 不使用浮点秒作为权威格式；
- overlap 允许，但必须由内容语义解释；
- Legacy 浮点开始时间使用 floor 转毫秒，结束时间使用 ceil 转毫秒，避免证据被截短。

### 11.4 文本范围

- 使用 UTF-8 字节 offset；
- 半开区间 `[start_byte, end_byte)`；
- 两个 offset 必须落在 codepoint 边界；
- offset 针对绑定的精确 Artifact 字节；
- Unicode 字符数、UTF-16 index 或视觉列不能替代 byte offset。

### 11.5 页、幻灯片和 bbox

- page/slide 从 1 开始；
- bbox 坐标归一化到 `[0,1]`；
- 原点为左上角；
- 明确 x/y/width/height 或 left/top/right/bottom schema，不允许混用；
- bbox 可选，但页码/幻灯片号必需；
- OCR 文本和图片快照应作为独立 Artifact。

### 11.6 Web、Wiki 和代码

- Web CSS selector/XPath 只作为辅助信息，不是长期定位根；
- 优先定位已归档的规范化文本；
- Wiki 必须固定 revision；
- Git locator 必须固定 commit/tree/blob 身份；
- 文件行号使用 1-based inclusive；
- Git 原生对象 ID 可以记录，但 Portable Integrity 仍使用 SHA-256；
- code symbol 必须绑定固定代码版本和可验证的文件/范围 fallback。

### 11.7 强证据

强 EvidenceRef 必须指向可携带、可验证的 Evidence Artifact。即使 SourceRevision 是 `reference_only` 或 `external_local`，也不能只凭远端链接宣称强证据。

Quality Profile 和 Publisher 可以：

- 对缺少强证据的声明 warning；
- 对关键声明阻止 approval/publish；
- 允许明确标记为弱证据或来源不可归档的内容保留在 Draft。

## 12. Transcript 合同

### 12.1 格式

Transcript 使用：

- Artifact Type：`evidence.transcript.v1`；
- Media Type：`application/x-ndjson`；
- UTF-8；
- LF；
- 文件末尾 LF；
- 第一行为 header；
- 后续每行一个 segment。

示例：

```jsonl
{"record_type":"transcript_header","transcript_schema_version":1,"source_revision_id":"rev_...","time_base":"millisecond","language":"zh-CN"}
{"record_type":"segment","segment_id":"seg_000001","start_ms":0,"end_ms":2530,"text":"..."}
{"record_type":"segment","segment_id":"seg_000002","start_ms":2400,"end_ms":5100,"text":"..."}
```

### 12.2 Segment

- segment ID 是 Transcript 内局部 ID，例如 `seg_000001`；
- 时间非负；
- segment 按 start time 非递减；
- overlap 允许；
- 空文本 segment 默认无效，除非 content schema 明确允许非语音事件；
- speaker、confidence、language、source subtitle cue 等为可选版本化字段；
- 不把 provider 私有响应原样放进标准 segment。

### 12.3 不重复保存权威全文

Transcript header 不保存另一个权威 `full_text`。如果需要：

- 纯文本；
- 可读 Markdown；
- VTT/SRT；
- 分段摘要；

应生成独立派生 Artifact，并通过 `derived_from` 指向 Transcript。这样不会出现 segment 已更新但 `full_text` 未同步的双真相。

## 13. Draft Markdown 与资源

### 13.1 Markdown

Draft 使用普通 GFM-compatible Markdown：

- UTF-8；
- LF；
- 用户可以用 Obsidian、VS Code 或普通文本工具阅读；
- YAML frontmatter 可以包含 title、tags、aliases 等显示信息；
- frontmatter 不是 provenance 根；
- Artifact Envelope、EvidenceSet 和 Receipt 才是机器可验证来源。

### 13.2 Evidence Citation

Draft 使用 Markdown footnote label 引用 Evidence ID：

```markdown
这个结论来自原始视频中的对应片段。[^ev_019...]

[^ev_019...]: 视频 00:10–00:24，来源标题……
```

机器验证只依赖 footnote label 与 EvidenceSet 中 `evidence_id` 的映射。脚注的人类可读文字是 projection，不是权威 locator。

规则：

- 引用的 Evidence ID 必须存在；
- 未使用 Evidence 可以存在，但 Quality Profile 可 warning；
- 关键声明缺 Evidence 时可 warning 或 fail；
- 用户编辑脚注显示文字不改变 EvidenceRef；
- 修改正文并重新纳入合同必须创建新 Draft Artifact 和新 Bundle。

### 13.3 Bundle 内资源

Draft 引用同一 Bundle 的本地显示资源，例如：

```markdown
![关键帧](../assets/art_019....webp)
```

约束：

- `bundle.json` payload path 永不使用 `..`；
- Markdown 相对链接可以使用 `..`，但规范化后必须仍在当前 Bundle 内；
- 资源必须是当前 Bundle 中已声明、带 hash 的 Artifact；
- Draft 不直接写入其他 Bundle 的物理路径；
- 如新 Draft 需要旧 Bundle 图片，应把相同字节复制为当前 Bundle 的新 Artifact，并以相同 hash 和 parent relation 建立 lineage；
- v1 不使用全局 blob path。

### 13.4 禁止链接

Draft 和附件解析拒绝或隔离：

- `file:` URI；
- 绝对 Windows/POSIX 路径；
- UNC 路径；
- `javascript:`；
- 未经策略允许的 `data:`；
- 规范化后越出 Bundle 的相对路径；
- 通过 symlink/reparse point 越界的路径。

远端 HTTP/HTTPS 链接必须安全渲染，但它们不能替代 SourceRevision。Markdown Viewer 必须清理 HTML、SVG、Mermaid 和其他 active content。

### 13.5 当前截图迁移

当前 `/static/screenshots/...` 截图在新合同中成为 `evidence.asset.v1` Artifact：

- 复制到当前 Bundle `assets/` 或 `evidence/`；
- 计算精确 hash；
- 重写 Draft 链接；
- 与视频 SourceRevision、时间 Evidence 或提取运行建立 lineage；
- 不继续依赖 Runtime HTTP `/static` 路由才能显示。

### 13.6 Publisher 附件限制

当前 iwiki Publisher 没有正式附件合同，也没有已确认的 `wiki/_assets` 布局。因此本设计只保证 raw Bundle 内资源可读，不发明发布附件路径。

包含本地资源的 Draft 在 publish preflight 中必须：

- 若目标 Publisher 不支持附件，明确 warning 或 block；
- 不丢图后伪装发布成功；
- 不把 raw Bundle 相对路径直接写入 `wiki/`；
- 等待独立 Publisher Attachment Contract 设计。

## 14. QualityReport

### 14.1 四种状态必须分开

系统必须区分：

1. Bundle validity；
2. Knowledge quality；
3. Review approval；
4. Publish eligibility/publication state。

关系不是简单等价：

- Bundle 无效：禁止 commit；
- Bundle 有效但 Quality fail：允许 portable commit，默认禁止 approve/publish；
- Quality pass：仍然是 unreviewed；
- approved：尚未发布；
- published：是 Publisher 的独立事务结果。

### 14.2 格式

QualityReport 是 `quality.report.v1` JSON Artifact，至少包含：

```json
{
  "quality_report_schema_version": 1,
  "subject": {
    "bundle_id": "bnd_...",
    "artifact_id": "art_...",
    "sha256": "sha256:..."
  },
  "profile": {
    "id": "video-note-default",
    "version": 1
  },
  "overall": "pass_with_warnings",
  "checks": [],
  "method": {},
  "metrics": {},
  "messages": [],
  "evidence_ids": []
}
```

overall 取值：

- `pass`；
- `pass_with_warnings`；
- `fail`。

check 取值：

- `pass`；
- `warn`；
- `fail`；
- `skipped`。

`skipped` 必须有 reason。method 必须区分：

- deterministic；
- model；
- agent；
- human。

### 14.3 Hash 绑定与后续评估

QualityReport 绑定精确 subject hash。Draft 内容改变后旧报告自动失效。

后续 Bundle 可以产生针对旧 Draft 的新 QualityReport：

- 新报告通过 ArtifactRef 指向旧 Draft；
- 新 Bundle 依赖旧 Bundle；
- 旧 Bundle 不改变；
- 当前有效报告由 review/index projection 计算，不写回旧 manifest。

### 14.4 Quality Profile

Profile 决定：

- 检查集合；
- 阈值；
- warning/fail 聚合；
- 哪些 fail 阻止 approval；
- 哪些 fail 阻止 personal/common publish；
- Recipe 专用质量指标。

“高质量”不能只由一个 Prompt 或单一 LLM 自评决定。首个视频 Profile 至少检查结构、引用完整性、Transcript 覆盖、来源绑定、空内容、明显幻觉风险和 Markdown 可读性。

## 15. Portable Receipt

### 15.1 Receipt 内容

`receipt.json` 至少包含：

- Receipt Schema version；
- `run_id`；
- 可关联的 Job ID 和 Attempt ID；
- 运行状态 `succeeded`；
- started_at/completed_at；
- Recipe ID/version；
- 安全、规范化的参数 hash；
- 安全参数摘要；
- 输入 SourceRevision refs；
- 输出 Artifact refs；
- Capability ID/version；
- Executor 类型和版本；
- 可公开的模型/转写引擎标识；
- usage 摘要；
- Quality 摘要；
- retry/parent run refs；
- redaction summary。

### 15.2 Receipt 不包含

- API key；
- Cookie；
- Credential 内容；
- provider 原始请求/响应；
- 完整 Prompt；
- 完整 Agent event stream；
- provider request ID，除非明确证明安全且有诊断价值；
- PID、lease、fencing token；
- 绝对路径；
- 全部环境变量；
- UI 状态；
- 未脱敏错误堆栈。

Receipt 是 provenance 摘要，不是审计日志或 JobStore 备份。

### 15.3 使用量

usage 可以记录：

- 输入/输出 token；
- 音频/视频时长；
- 转写分钟数；
- Agent step 数；
- 可公开费用估计；
- cache hit 摘要。

usage 必须标注来源和估算性质，不能把供应商不确定数据表示为精确事实。

## 16. `commit.json` 与完整性根

### 16.1 格式

`commit.json` 至少包含：

```json
{
  "commit_protocol_version": 1,
  "bundle_id": "bnd_...",
  "manifest": {
    "path": "bundle.json",
    "byte_length": 12345,
    "sha256": "sha256:..."
  },
  "committed_at": "2026-07-14T08:10:20.123Z"
}
```

`committed_at` 由权威 Committer 在最终 rename 前立即采样。Producer 不预先生成 final `commit.json`。

### 16.2 Hash 根

完整性链为：

```text
commit.json
  -> exact bundle.json bytes
      -> receipt.json
      -> every local Artifact payload
      -> dependency bundle manifest hashes
          -> dependency Artifacts
```

这提供完整性和依赖固定，不证明作者身份，也不抵抗拥有写权限的恶意用户重新生成整套 hash。若未来需要签名，必须设计独立 Signature Contract，不能把 SHA-256 描述成数字签名。

### 16.3 Commit Presence

最终 `bundles/bnd_.../` 只有在以下条件同时满足时才是 committed candidate：

- `commit.json` 存在且可解码；
- commit protocol 受支持；
- Bundle ID 与目录和 manifest 一致；
- `bundle.json` 大小和 hash 匹配；
- 结构校验通过。

“有 `commit.json`”不是完整有效的充分条件。Reader 仍按操作需要执行 Integrity、Closure 或 Semantic validation。

## 17. 校验等级与操作 Gate

### 17.1 Structure

检查：

- 目录和必需控制文件；
- JSON/JSONL 可解析；
- Schema version 形状；
- ID 和路径语法；
- 重复 ID；
- 非法路径、链接和特殊文件；
- 声明文件/未声明文件；
- 基础类型和枚举。

Validator 必须区分两种形态：

- **staging candidate**：要求 `bundle.json`、`receipt.json` 和已声明 payload 存在，且必须没有 `commit.json`；
- **committed Bundle**：额外要求有效 `commit.json`。

两种形态使用同一核心 Schema；`commit.json` 是提交状态边界，不允许 Producer 在 staging 中预造。

### 17.2 Integrity

在 Structure 基础上检查：

- `commit.json -> bundle.json`；
- Artifact byte length/hash；
- Receipt byte length/hash；
- Evidence target hash；
- parent/ref hash；
- manifest 路径边界；
- committed tree 中的全部声明文件。

### 17.3 Closure

在 Integrity 基础上检查：

- 依赖 Bundle 存在；
- 依赖 manifest hash 匹配；
- 引用的 Source/Revision/Artifact 存在；
- required contract 可用；
- export 目标闭包完整；
- strong Evidence target 可携带。

### 17.4 Semantic

在 Closure 基础上检查：

- Source/SourceRevision 不变量；
- materialization 与许可/隐私结构；
- Artifact Type 与 media/content schema；
- Transcript header、segment 和时间；
- Locator 语义；
- Draft Evidence footnote；
- outputs 与 Recipe Contract；
- QualityReport subject hash；
- Receipt 输入/输出一致性；
- 不允许的链接和资源关系。

### 17.5 操作最低等级

| 操作 | 最低要求 |
|---|---|
| 列出 Bundle candidate | Structure，可使用 lazy/cache |
| 打开目标 Artifact | 目标 Bundle Integrity |
| 新 Job 使用输入 | 输入 Integrity + Closure |
| Review/Approval | Semantic |
| Publish plan/apply | Semantic + Publisher preflight |
| Export | Closure |
| Import commit | 当前 Bundle Semantic + dependency Closure |
| Migration | 源 Integrity/Closure + 新 Bundle Semantic |
| Delete plan | 目标 Integrity + 反向依赖扫描 |
| Restore | Integrity + ID/conflict 检查 |

校验 cache 只是优化。Job 输入、Review、Publish、Export、Delete 和 Migration 的权威操作必须根据当前字节重新验证所需等级。

## 18. 原子提交协议

### 18.1 权威 Writer

AllToNote Core 负责构建候选 Bundle；iwiki Portable Contract Engine 负责权威校验和最终原子文件系统变更。

AllToNote 不直接把目录 rename 到最终 `bundles/`。它通过稳定 iwiki SDK 的同进程接口执行 prepare 和 commit。第三方 Producer 可以通过 `iwiki portable commit` 使用同一实现。

### 18.2 为什么采用 SDK + CLI

只使用子进程 CLI 会让 AllToNote 的 JobStore Commit Guard、fencing 和最终 rename 难以形成短且可靠的同进程临界区；直接导入 iwiki 内部模块又会锁死内部实现。

因此：

- 稳定 iwiki SDK 提供窄、版本化的同进程接口；
- iwiki CLI 是该接口的外部适配器；
- 二者调用同一 Contract Engine；
- AllToNote 只依赖自己的 `PortableWorkspacePort` 和公开 SDK DTO；
- AllToNote 不导入 iwiki 私有类或私有数据库。

### 18.3 Step 写入

每个 Step：

1. 写入当前 Job/Attempt 私有 staging；
2. 流式写 payload；
3. 关闭文件；
4. 计算大小和 hash；
5. 校验 content schema；
6. 重新检查 Job、fencing 和 cancel；
7. 将 Artifact 记录为 checkpointed；
8. checkpoint 不对普通 Reader 可见。

旧 fencing token 不能创建 checkpoint 或提交 Artifact。

### 18.4 Bundle Assembly

所有 Step 完成后，Core：

1. 收集已校验 checkpoint；
2. 构建 EvidenceSet、Draft、QualityReport；
3. 构建 Receipt；
4. 构建 `bundle.json`；
5. 写入 `bundle.partial`；
6. fsync payload、控制文件和目录；
7. 确认不存在 `commit.json`；
8. 调用 SDK `prepare_bundle_commit`。

### 18.5 PreparedBundle

prepare 执行昂贵校验并建立进程内 `PreparedBundle`：

- 绑定 Workspace identity；
- 绑定 staging identity；
- 绑定 Bundle ID；
- 绑定精确 manifest hash；
- 绑定已验证文件 inventory；
- 持有或记录阻止/检测受控并发写所需的文件身份；
- 不序列化；
- 不跨进程或 Runtime 重启；
- 文件、Workspace 或合同变化后失效；
- 失败时不产生最终 Bundle。

Windows 上准备阶段应在允许最终目录 rename 的同时拒绝新的写共享；若已有不兼容句柄，执行有界 sharing-violation retry。macOS 使用 Workspace mutation lock、文件身份重检和合作式 Writer 规则。

开放磁盘不能阻止同一用户权限下的恶意程序刻意修改文件；本合同保证受控 Writer 协调、最终 hash 检测和 fail-closed，不承诺本地恶意管理员隔离。

### 18.6 短 Commit Guard

prepare 成功后：

1. AllToNote 获取 JobStore Bundle Commit Guard；
2. 检查 Job 仍非终态；
3. 检查当前 Attempt fencing token；
4. 检查 cancellation 尚未获胜；
5. 调用 `commit_prepared_bundle`；
6. iwiki 快速重检 Prepared identity；
7. 采样 `committed_at`；
8. 生成并写入小型 `commit.json`；
9. fsync `commit.json`、Bundle 和必要父目录；
10. 获取 Workspace Portable Mutation Lock；
11. 检查最终目录；
12. 执行同卷 atomic directory rename；
13. 返回 Bundle ID、manifest hash 和 commit hash；
14. AllToNote 在 JobStore 事务中写 succeeded；
15. 释放 Guard。

Commit Guard 不包含下载、转写、模型、Agent、完整 Artifact hashing、质量评估或完整 semantic validation。

所有需要同时接触 JobStore 和 Workspace mutation lock 的实现必须遵守固定锁顺序：先取得 JobStore Commit Guard，再由 iwiki 取得 Workspace Portable Mutation Lock；任何路径都不得反向获取。Import、Migration、Delete 和 Restore 不访问 AllToNote JobStore，只使用 iwiki 自己的 Workspace mutation lock。

### 18.7 已存在目标

- 目标不存在：正常 rename；
- 目标存在且 manifest hash 相同：幂等成功，返回已有 Bundle；
- 目标存在但 manifest hash 不同：`bundle_id_conflict`；
- 永不覆盖；
- 永不逐文件 merge；
- atomic rename 失败时不降级为 copy-into-final。

### 18.8 Cancel/Commit 竞争

- cancel 以 JobStore 条件更新与 Commit Guard 竞争；
- cancel 先获胜：新 checkpoint/commit 被拒绝，Job 最终 cancelled；
- rename 先完成：Bundle 保留，Job succeeded 获胜；
- 终态 Job 的后续 cancel 返回当前终态，不回写 cancelled；
- cancel 不触发 Publisher；
- cancelled Job 的重新生产创建新 Job 或显式 retry。

### 18.9 Windows 约束

- staging 与 final 必须同卷；
- 所有 Writer 句柄在 prepare 前关闭；
- Prepared handles 允许 rename，但不允许继续写；
- sharing violation 只做有界退避重试；
- 不能用逐文件 copy 模拟 atomic rename；
- 长路径和 Unicode 路径必须由测试验证；
- junction/reparse point 全部拒绝；
- OneDrive placeholder 在需要内容时才 hydrate；
- 云端复制是否完成不影响本机 rename 的原子性判断。

## 19. 崩溃恢复、Replica 与 Quarantine

### 19.1 恢复矩阵

| 观察状态 | 恢复行为 |
|---|---|
| final 有效，JobStore 未成功 | 根据 Receipt/Bundle 对账 Job 为 succeeded |
| final 不存在，有有效 checkpoint | 创建新 Attempt 或按策略恢复 |
| final 不存在，有 commit-ready staging | 重新检查 token/cancel，重新 prepare |
| final 不存在，staging 不完整 | 从 checkpoint 恢复或失败 |
| final 存在但无效 | 不猜测成功，报告 integrity/invalid |
| JobStore 损坏 | 停止调度，保留 portable assets，只读诊断 |

不能把所有旧 `running` 简单重置为 `queued`。

### 19.2 Staging 清理

staging 清理只作用于当前 local instance：

- 确认无活动 lease；
- 确认 fencing token 过期；
- 确认不属于正在 reconcile 的 Job；
- cancelled、无效、冲突或超期 staging 可移入本机 quarantine；
- 不根据另一个设备的时钟直接删除其 staging；
- 不把 quarantine 当作已提交资产。

### 19.3 Replica 状态

云盘、离线设备或外部复制可能产生：

- `replica_incomplete`；
- `source_offline`；
- `valid`；
- `integrity_mismatch`；
- `dependency_missing`；
- `unsupported_schema`；
- `corrupted`。

规则：

- 首次看到不完整同步不立即 quarantine；
- placeholder 未 hydrate 不等于损坏；
- future schema 不等于损坏；
- dependency 缺失不等于当前 Bundle 字节损坏；
- 只有确认本机 Writer 产生的无效/陈旧/取消 staging 才自动 quarantine；
- committed Bundle 只报告问题，不静默移动；
- 多设备并发编辑创建 sibling Bundles，不假设分布式锁。

### 19.4 JobStore 损坏

JobStore 完整性检查失败时：

1. 停止领取和创建 Job；
2. 只读隔离数据库及可用 WAL/sidecar；
3. 失效所有旧 fencing 提交权；
4. containment 可确认的 Worker；
5. 保留 committed raw/wiki；
6. 不从 staging 猜测 succeeded；
7. 明确提示任务历史、幂等绑定和未完成恢复点可能丢失；
8. 用户确认后才创建新 JobStore。

## 20. Schema 演进与合同发布

### 20.1 所有权

公开 Schema、Golden Fixtures、Reference Validator 和合同文档以 llm-iwiki 为规范源。AllToNote 消费已发布合同，不维护私有复制版本，也不在运行时从网络获取 Schema。

AllToNote Core 定义领域意图；路径、序列化、验证和迁移属于共同发布的 iwiki Portable Contract。

### 20.2 Schema URI

- URI 永久指向一个不可变 Schema；
- 已发布 URI 的含义和字节不改变；
- 修订产生新 URI/schema set；
- Runtime 安装包携带受支持 Schema；
- 离线校验不依赖网络；
- `schema_set_sha256` 验证安装内容完整性。

### 20.3 Reader/Writer 兼容

- Reader 声明支持的版本范围；
- Writer 只写明确支持的版本；
- 新增 optional extension 可以兼容旧 Reader；
- 改变核心字段含义、删除字段或放宽安全边界需要新主版本；
- 未知 required contract 必须停止语义操作；
- future schema 可以列出和报告，但不得修改；
- 是否可写由 capability 和 contract range 决定，不由软件版本猜测。

### 20.4 Reader Migration

Reader 可以在内存中把旧 schema 投影为当前领域 DTO，但：

- 不写回旧 Bundle；
- 不改变旧 hash；
- 清楚保留原 schema version；
- projection 不被误认为已经迁移。

### 20.5 Materialized Migration

显式迁移：

1. 支持 dry-run；
2. 校验源 Bundle 和 closure；
3. 创建新 Bundle ID；
4. 创建新 Artifact ID；
5. 对复用相同字节的 Artifact 保留 hash；
6. 使用 `migrates_from` 指向旧 Artifact/Bundle；
7. 写 Migration Receipt；
8. 通过当前 Semantic validation；
9. 原子提交新 Bundle；
10. 保留旧 Bundle。

没有“原地批量改写所有 Bundle”的迁移模式。

### 20.6 发布顺序

跨仓库合同变更必须：

1. 在 llm-iwiki 修改文档/Schema；
2. 增加合法和非法 Fixture；
3. 更新 Reference Validator；
4. SDK/CLI Provider 测试通过；
5. 发布不可变 Contract；
6. AllToNote 更新 pin；
7. Consumer 合同测试通过；
8. 才允许 AllToNote Writer 使用新能力。

禁止先让 AllToNote 写新私有格式，再要求 iwiki 追补支持。

## 21. Export 与 Import

### 21.1 两种完整性

Export 明确区分：

- **closure-complete**：包含目标 Bundle 在 Workspace 中的全部显式 Bundle 依赖；
- **offline-complete**：除 closure 外，还要求所有允许携带且执行目标需要的来源内容已 archived，不依赖 `external_local` 或不可访问远端。

closure-complete 不自动等于 offline-complete。`reference_only` 可能合法存在于 closure-complete export。

### 21.2 规范导出布局

规范导出是普通目录：

```text
iwiki-portable-export-v1/
├─ export.json
└─ bundles/
   ├─ bnd_.../
   └─ bnd_.../
```

`export.json` 至少包含：

- export schema version；
- created_at；
- export mode；
- root Bundle IDs；
- Bundle manifest hashes；
- closure graph；
- completeness summary；
- unresolved external refs；
- required contracts；
- optional license/privacy warnings。

ZIP 只是 transport wrapper，不是规范存储。解压后的普通目录才是权威导入输入。

### 21.3 精确字节

Export 复制 committed Bundle 的精确字节：

- 不格式化 JSON；
- 不转换换行；
- 不改时间；
- 不换 ID；
- 不重写 manifest；
- 不重新生成 `commit.json`；
- 不把 external-local 静默 archived。

Export 不包含：

- JobStore；
- event log；
- SQLite/WAL；
- QMD/Chroma/index；
- credentials；
- staging；
- quarantine；
- trash；
- Runtime/Pack；
- UI state。

### 21.4 ZIP 安全

ZIP/归档导入在解压前和解压时必须限制：

- path traversal；
- 绝对路径；
- UNC/device path；
- NUL；
- symlink、junction、reparse point；
- hardlink/sparse 特殊语义；
- 重复 entry；
- Unicode/case folding 冲突；
- Windows 保留设备名；
- 过多 entry；
- 单文件和总解压大小；
- 压缩比异常；
- 嵌套归档炸弹；
- 声明大小与实际流不符；
- 非普通文件。

解压到私有 import staging，完成全量校验后逐 Bundle 原子提交。

### 21.5 Import 身份与冲突

Import 保留所有 Bundle、Source、Revision、Artifact、Evidence 和 Run ID，且保留精确 hash。

- 相同 Bundle ID + 相同 manifest hash：幂等成功；
- 相同 Bundle ID + 不同 manifest hash：冲突；
- 不自动生成新 ID 规避冲突；
- 不覆盖；
- 不合并两个 manifest；
- 不把冲突 Bundle 的部分 Artifact 放进 final。

### 21.6 多 Bundle 原子性

v1 不宣称跨全部 Bundle 的全局 ACID。Import：

1. 建立 Machine import journal；
2. 解析依赖图；
3. 按依赖顺序处理；
4. 每个 Bundle 独立完整校验和原子 commit；
5. 第 N 个失败时前 N−1 个保持 committed；
6. journal 记录已完成、幂等、冲突、待继续项；
7. 重试从 journal 和 final Bundle 对账继续。

部分导入必须返回 `partial_import`，不能只输出一个模糊 warning。错误详情至少给出 operation ID、计数和安全的 Bundle ID 列表；详细报告可以通过独立 status/read 接口分页读取。

### 21.7 External Local

Export 遇到 `external_local`：

- 报告 unresolved/externally-bound；
- closure-complete 可以继续；
- offline-complete 默认失败；
- 不根据 Machine State 路径静默复制；
- 用户可显式执行 archive operation，产生新 SourceRevision 和新 Bundle，再重新 export。

## 22. Legacy Importer

### 22.1 所有权

Legacy Importer 属于 AllToNote，因为它理解当前 BiliNote/AllToNote 私有历史文件。iwiki 只负责标准 Bundle 的校验和提交。

目标 CLI：

```text
alltonote legacy scan --source <path> --json
alltonote legacy import --request <request.json> --json
```

### 22.2 扫描与 dry-run

scan：

- 只读；
- 识别已知 task 文件集合；
- 计算源文件 hash；
- 报告缺失/冲突/中间态；
- 不根据 status 单独判定成功；
- 给出预计 Source、Artifact 和 warning；
- 不创建 Bundle。

Import request 固定：

- 源根；
- 任务选择；
- 目标 Workspace identity；
- quality profile；
- privacy/license 默认策略；
- 用户对不确定来源的确认；
- client request ID。

### 22.3 内容优先级

对同一旧任务：

1. 最终 `<task>.json` 中的最终 Markdown 是首选；
2. `_transcript.json` 用于补充 Transcript/segment；
3. `_markdown.md` 只在最终 JSON 缺失时作为 fallback，或作为中间 Artifact 保留；
4. `_audio.json` 作为来源/生成元数据；
5. screenshot 文件复制为 `evidence.asset.v1`；
6. GPT checkpoint、状态、Chroma、SQLite 和 provider raw 默认不导入。

如果多个候选内容不一致，Importer 不静默挑选后丢弃信息；它按上述优先级生成结果，并在 Receipt/Quality 中记录差异 warning。

### 22.4 映射

- 视频 URL/平台 ID -> Source；
- 旧采集时间/元数据 -> SourceRevision；
- Transcript -> `evidence.transcript.v1`；
- Markdown -> `knowledge.draft.markdown.v1`；
- Screenshot -> `evidence.asset.v1`；
- 可恢复时间戳 -> EvidenceRef；
- 旧任务参数 -> 脱敏 Receipt parameter summary；
- 旧模型/转写器 -> executor/capability 摘要；
- 无法恢复 provenance -> warning，而不是伪造。

### 22.5 浮点时间

旧浮点秒转换：

```text
start_ms = floor(start_seconds * 1000)
end_ms   = ceil(end_seconds * 1000)
```

然后执行非负、范围和 Transcript duration 校验。该规则避免因舍入而缩短证据区间。

### 22.6 截图与链接

- 读取实际 screenshot 字节；
- 检查媒体类型；
- 计算 SHA-256；
- 复制到新 Bundle；
- 生成 Artifact Envelope；
- 把 `/static/screenshots/...` 重写为 Bundle 相对路径；
- 缺图时保留可读文字并产生 warning；
- 不保留依赖旧 backend HTTP server 的链接。

### 22.7 幂等

Legacy import identity 至少由以下组成：

- legacy format ID/version；
- legacy task ID；
- 最终结果 hash；
- 目标 Workspace lineage；
- import profile version。

重复 request：

- 已有相同导入结果：返回原 Bundle；
- 源字节改变：创建新的 Import Job/Bundle；
- 同 task ID 不同结果：不覆盖旧 Bundle；
- 不依赖可被清理的临时状态才能判定幂等。

### 22.8 Legacy Quality

Legacy Import 使用独立 Quality Profile，因为旧数据可能缺少：

- 完整 SourceRevision；
- 强 Evidence；
- 精确模型参数；
- 可靠 status/完成时间；
- 许可和隐私信息。

Bundle 可以合法提交，但默认携带 warning，是否允许 Publisher 由 profile 决定。

### 22.9 IndexedDB

当前前端 IndexedDB 中可能存在后端目录扫描看不到的任务历史。完整迁移需要单独的 Desktop export：

- 浏览器/Desktop 显式导出历史索引和必要内容；
- 后端 Legacy scan 不宣称覆盖 IndexedDB；
- UI 必须提示“仅扫描后端目录”与“包含前端历史”的区别；
- IndexedDB UI 状态本身不成为 Portable Asset，只有导入的知识内容成为 Bundle。

## 23. 删除、Trash 与 GC

### 23.1 三类清理

#### Machine Cache GC

可以重建的数据：

- QMD/Chroma/index；
- 下载缓存；
- 模型缓存；
- 临时转码；
- validation cache；
- watcher/index state。

按机器策略清理，不改变 Portable Bundle。

#### Operational Cleanup

- stale staging；
- cancelled attempt staging；
- 本机 quarantine；
- 已确认无 lease 的 checkpoint；
- import 临时解压目录。

只能清理当前 local instance 拥有的操作目录。

#### Portable Deletion

删除 committed Bundle 是知识资产操作，必须 plan、impact analysis、trash、grace、restore/purge。

### 23.2 不自动删除

以下状态绝不自动成为删除条件：

- superseded；
- rejected；
- Quality fail；
- unreviewed；
- 未发布；
- 已有新版本；
- 很久未访问。

它们仍可能是 lineage、Evidence、审计或未来重评所需资产。

### 23.3 反向引用

v1 不维护权威引用计数。Delete plan 从 manifests 和 publication/index projection 重建反向引用：

- dependent Bundles；
- published documents；
- active review/plan；
- export/import operation；
- Machine Job refs 作为附加提示。

Machine index 可以加速，但必须可重建；删除正确性不能只依赖缓存计数。

### 23.4 Delete Protocol

1. 用户选择目标 Bundle；
2. iwiki 生成带 hash 的 delete plan；
3. plan 列出 dependents、publications、closure impact、空间估计和 warning；
4. 默认阻止存在 dependents/publications 的删除；
5. cascade 必须显式，且列出完整目标；
6. apply 重新验证 plan 前置条件；
7. 同卷原子 rename 到 `.trash/<operation-id>/`；
8. 写 `trash.json`；
9. grace period 内可 restore；
10. purge 需要独立明确操作。

`trash.json` 至少绑定：

- delete operation ID；
- plan hash；
- Bundle ID/manifest hash；
- original relative path；
- trashed_at；
- grace policy；
- dependents/publication summary；
- initiator type 的安全摘要。

### 23.5 Restore

- 验证 trash 内容完整；
- 验证 final 目标；
- final 不存在时 atomic rename 恢复；
- final 已有相同 hash 时幂等；
- final 已有不同 hash 时冲突；
- 不覆盖；
- restore 不自动恢复已删除 publication；
- 恢复后重建/刷新 Machine Index。

### 23.6 Purge

Purge 前明确提示：

- 云盘历史可能保留；
- SSD/文件系统不保证安全擦除；
- 备份可能保留；
- Git 历史可能保留；
- 离线设备可能重新带回 Bundle；
- v1 没有分布式 tombstone。

离线设备重新出现已 purge Bundle 时，系统报告 `reappeared_bundle`，不静默再次删除。

### 23.7 无 Blob GC

v1 没有全局 CAS，因此没有 Blob 引用计数和 Blob GC。Bundle 目录内部的所有 payload 与 Bundle 一起进入 trash/purge。

## 24. 性能设计

### 24.1 权威数据与索引

权威数据是 committed Bundle 文件。以下仅为可重建优化：

- Machine SQLite index；
- reverse-reference index；
- watcher state；
- validation cache；
- QMD/搜索索引；
- Source identity lookup cache。

允许使用 Machine SQLite，因为它不成为知识真相，也不进入 export。

### 24.2 启动

正常 Runtime/Desktop 启动不得：

- 递归读取全部 Artifact；
- 哈希整个 Vault；
- hydrate 全部 OneDrive placeholder；
- rebuild 全部 index；
- semantic validate 全部 Bundle；
- 加载 Whisper、FFmpeg、模型或 Agent Runtime。

启动只读取 Workspace manifest、iwiki capability、Machine Index 状态和必要的增量 reconcile 信息。

### 24.3 复杂度目标

- Commit：`O(new bundle bytes)`；
- 目标 Bundle Integrity：`O(target bundle bytes)`；
- Closure validation：`O(reachable closure bytes)`；
- 冷 index discovery：`O(bundle count + manifest bytes)`；
- list/tree：优先 `O(result count)`，通过可重建 index；
- reverse dependency query：通过 index，index 丢失时可重建扫描；
- export：`O(export closure bytes)`；
- import：`O(import bytes + dependency graph)`。

### 24.4 流式处理

- 大文件流式读写和 hash；
- Transcript/Evidence 使用 JSONL；
- 不把完整视频、音频、Transcript 或多 Bundle export 读入单个内存对象；
- 设置有界 buffer；
- 大 Artifact 通过文件/Resource 流，不嵌入 CLI JSON；
- provider/Agent 中间结果只在需要时 materialize。

### 24.5 Lazy Validation

- list 只做 candidate/Structure/cache；
- 打开目标时验证目标 Integrity；
- Job 输入、Review、Publish、Export、Delete、Migration 执行权威等级；
- watcher 只使 cache 失效，不作为正确性来源；
- watcher 丢事件后由 reconcile 修复；
- 不为每个 Artifact 启动一个 iwiki 子进程；
- Bundle/closure 使用 batch SDK/CLI 操作。

### 24.6 Commit 临界区

完整 hash 和 semantic validation 在 `prepare_bundle_commit` 中完成。Commit Guard 内只允许快速 identity recheck、小型 `commit.json`、fsync、Workspace lock 和 rename。

### 24.7 OneDrive/云盘

- metadata/list 不主动 hydrate payload；
- 打开、校验或使用具体 Artifact 时按需 hydrate；
- hydration timeout 产生 `source_offline`/`replica_incomplete`，不伪装 corruption；
- 本地 atomic rename 不承诺云端瞬间完整；
- 云端看到部分目录时等同步收敛后再判断。

### 24.8 Benchmark Gate

设计阶段不发明毫秒目标。Phase P0/P1 建立固定基准 corpus，记录：

- CPU、内存、磁盘和操作系统；
- NTFS/APFS；
- 冷/热缓存；
- Bundle 数量；
- manifest 大小；
- payload 总字节；
- dependency 深度；
- placeholder 状态；
- iwiki/Runtime/Contract 版本。

发布后以可复现历史基线判断回归，再确定产品级延迟和内存预算。

## 25. Core、iwiki、CLI、Desktop 与 MCP 边界

### 25.1 最终所有权

| 组件 | 唯一职责 | 不负责 |
|---|---|---|
| AllToNote Core | Recipe、Job、Source、Revision、Artifact、Evidence、Draft、Quality、Receipt 和生产编排 | Workspace 路径合同、最终 Bundle mutation |
| iwiki Contract Engine | Schema、路径、校验、原子 commit、import/export、migration、trash | 下载、转写、Prompt、LLM、Agent、Recipe |
| iwiki SDK | 同进程稳定合同接口 | 暴露私有类、读取 JobStore |
| iwiki CLI | 跨语言/脚本的公开合同入口 | AllToNote 私有生产逻辑 |
| AllToNote Engine | detached Job、调度、取消、恢复、资源租约 | 长期知识存储 |
| `alltonote` CLI | 生产、Job、Artifact、Review、Publish 用例 | 复制通用 iwiki 生命周期命令 |
| Desktop API | 当前 Desktop 会话的 Core Adapter | 解析 CLI stdout、直接写 Bundle |
| MCP | 面向 Agent 的 Core Adapter | 内部 RPC、任意文件/发布/删除 |
| JobStore | 本机操作状态和幂等 | Portable knowledge |
| Vault | 开放长期资产 | 依赖任一应用才能读取 |

### 25.2 依赖方向

```text
Desktop / alltonote CLI / MCP
              |
              v
     AllToNote Application Services
              |
              v
         AllToNote Core
              |
              v
      PortableWorkspacePort
              |
              v
        stable iwiki SDK
              |
              v
       iwiki Contract Engine
              |
              v
         Local Workspace
```

外部第三方 Producer：

```text
Third-party Producer
        |
        v
   iwiki portable CLI
        |
        v
iwiki Contract Engine -> Workspace
```

iwiki 不反向依赖 AllToNote。Vault 不依赖 Core、CLI、Desktop 或 MCP。

### 25.3 Stable iwiki SDK

最小逻辑接口：

```text
inspect_portable_contract(workspace)
validate_bundle(workspace, bundle_ref, validation_level)
prepare_bundle_commit(workspace, staging_ref, expected_bundle_id)
commit_prepared_bundle(prepared_bundle)
resolve_bundle(workspace, bundle_id)
resolve_artifact(workspace, artifact_ref)
```

后续按阶段增加：

```text
export_bundle_set(...)
import_bundle_set(...)
migrate_bundle(...)
plan_bundle_delete(...)
apply_bundle_delete(...)
restore_bundle(...)
purge_bundle(...)
```

这些是稳定行为接口，不要求立即暴露为大量公共 Python 类。SDK：

- 使用独立 `iwiki_sdk_api_version`；
- 返回稳定 DTO 和 error code；
- 说明线程/并发安全；
- 不泄露内部连接、锁对象或异常类型；
- 与 CLI 调用同一个 Contract Engine；
- 官方 Runtime 固定经过验证的 SDK/Contract 组合。

### 25.4 Desktop

Desktop：

- 调用薄 Desktop API；
- 展示 Job、Artifact、Evidence、Draft、Quality 和发布状态；
- 不直接导入 iwiki SDK；
- 不直接写 staging/final；
- 不调用 `alltonote` CLI 并解析 stdout；
- 不把绝对 Workspace 路径暴露给 Web 内容；
- 退出不影响 portable assets；
- 后续 detached Job 由按需 Engine 承载。

## 26. iwiki Portable CLI

### 26.1 Capability

当前 iwiki CLI Protocol 1 没有 Portable capability，因此只允许现有只读/发布能力。未来 `iwiki inspect` 增加：

```json
{
  "capabilities": [
    "portable_bundle_validate_v1",
    "portable_bundle_commit_v1"
  ],
  "portable_contract": {
    "contract_id": "iwiki-portable-contract-v1",
    "schema_set_id": "2026-07-portable-v1",
    "schema_set_sha256": "sha256:...",
    "bundle_schema_versions": [1],
    "artifact_schema_versions": [1],
    "commit_protocol_versions": [1],
    "locator_schemes": []
  }
}
```

AllToNote Writer 同时检查 capability、contract ID、schema set、版本范围和 locator。能力缺失时不 fallback 为直接文件写入。

### 26.2 目标命令

Phase P0：

```text
iwiki portable validate \
  --workspace <path> \
  --bundle <bundle-id> \
  --level semantic \
  --json

iwiki portable validate \
  --workspace <path> \
  --staging <workspace-relative-staging-path> \
  --level semantic \
  --json

iwiki portable commit \
  --workspace <path> \
  --staging <workspace-relative-staging-path> \
  --expected-bundle-id <bundle-id> \
  --expected-manifest-sha256 <sha256> \
  --json
```

后续：

```text
iwiki portable export --workspace <path> --request <request.json> --output <dir> --json
iwiki portable import --workspace <path> --request <request.json> --json
iwiki portable migrate --workspace <path> --request <request.json> --json
iwiki portable delete-plan --workspace <path> --request <request.json> --output <plan.json> --json
iwiki portable delete-apply --workspace <path> --plan <plan.json> --json
iwiki portable restore --workspace <path> --request <request.json> --json
iwiki portable purge --workspace <path> --request <request.json> --json
```

staging 参数必须是 Workspace 相对路径并位于当前 `raw_personal/.staging` 合同内。CLI 不接受 Workspace 外部任意目录直接进入 final。

`--bundle` 与 `--staging` 是 `portable validate` 的互斥输入：前者验证 committed Bundle，后者验证未提交 candidate。

`iwiki portable commit` 在一个命令内调用同一 prepare/commit 实现。它不理解 AllToNote JobStore；外部 Producer 负责自己的任务状态，iwiki 负责文件系统原子性。

## 27. AllToNote Automation CLI

### 27.1 边界

`alltonote` 管知识生产和审阅；`iwiki` 管通用存储合同。两套 CLI 不机械镜像。

下列是目标公共命令面；每条命令的实际可用性服从第 34 节 Phase Gate。列出 `--detach`、Publisher 和 MCP 相关入口不表示 Phase P1 提前实现它们。

目标公共接口延续上位设计：

```text
alltonote runtime info --json
alltonote capability list --json
alltonote capability inspect <id> --json
alltonote capability doctor <id> --json

alltonote recipe list --json
alltonote recipe describe <id>@<version> --json

alltonote produce --request <request.json> --recipe <recipe> --workspace <path> --wait --json
alltonote produce --request <request.json> --recipe <recipe> --workspace <path> --detach --json
alltonote produce video --input <input> --workspace <path> --wait --json

alltonote job status <job-id> --json
alltonote job wait <job-id> --json
alltonote job events <job-id> --jsonl --follow
alltonote job cancel <job-id> --json
alltonote job retry <job-id> --request <retry-request.json> --json
alltonote job respond <job-id> --challenge <challenge-id> --response <response.json> --json

alltonote artifact show <artifact-id> --json
alltonote artifact lineage <artifact-id> --json
alltonote draft show <draft-id> --json
alltonote draft create-version <draft-id> --from <path> --json
alltonote draft check <draft-id> --json
alltonote draft approve <draft-id> --content-hash <sha256> --json
alltonote draft reject <draft-id> --content-hash <sha256> --reason <text> --json

alltonote publish plan --draft <draft-id> --target personal --json
alltonote publish apply --plan <plan-id> --json

alltonote legacy scan --source <path> --json
alltonote legacy import --request <request.json> --json
```

`video` 等用户别名只构造标准 Recipe Request，不拥有独立业务实现。

### 27.2 JSON Envelope

成功：

```json
{
  "alltonote_cli_protocol_version": 1,
  "ok": true,
  "command": "produce",
  "correlation_id": "corr_...",
  "data": {
    "job_id": "job_...",
    "state": "succeeded",
    "bundle_id": "bnd_...",
    "manifest_sha256": "sha256:...",
    "quality": {
      "overall": "pass_with_warnings",
      "publish_eligible": true
    }
  },
  "warnings": []
}
```

失败：

```json
{
  "alltonote_cli_protocol_version": 1,
  "ok": false,
  "command": "produce",
  "correlation_id": "corr_...",
  "data": null,
  "warnings": [],
  "error": {
    "category": "workspace_incompatible",
    "code": "portable_bundle_commit_capability_missing",
    "message": "The selected workspace runtime cannot commit Portable Bundles.",
    "retryable": false,
    "next_actions": ["Install a compatible iwiki Runtime."],
    "details": {
      "required_capability": "portable_bundle_commit_v1"
    },
    "refs": {
      "job_id": "job_...",
      "attempt_id": "attempt_..."
    }
  }
}
```

规则：

- stdout 在 `--json` 模式只输出一个 envelope；
- stdout 不混日志、进度、安装提示或颜色；
- stderr 用于诊断；
- 自动化根据 category/code 分支，不解析 message；
- message 可本地化；
- retryable 由 Core/Contract 明确声明；
- next_actions 安全且可执行；
- details 有大小上限并脱敏；
- warning 有稳定 code；
- Secret 不作为 CLI 参数，只引用 Credential Profile；
- 大型 payload 通过 Artifact/`--output`/流式读取访问。

Quality fail 返回 `ok: true`、Job `succeeded`、`quality.overall: fail` 和 `publish_eligible: false`。它不是执行失败。

### 27.3 Exit Code

| Code | 含义 |
|---:|---|
| 0 | 命令成功 |
| 2 | 参数/请求无效 |
| 10 | Workspace/Schema/Contract 不兼容 |
| 20 | 冲突、前置条件或部分操作失败 |
| 30 | 临时 Runtime/网络/依赖失败 |
| 40 | Policy/Credential/Grant/Capability 不允许 |
| 50 | Job/Recipe 执行失败 |
| 60 | 结构化取消 |
| 70 | 内部错误 |
| 130 | 当前前台命令被 Ctrl+C 中断 |

`job status` 成功查询到 failed Job 时退出 0，失败是返回数据；`produce --wait` 等待的 Job 失败时命令退出非零。

### 27.4 JSONL Event

```json
{
  "event_schema_version": 1,
  "event_id": "evt_...",
  "job_id": "job_...",
  "sequence": 42,
  "recorded_at": "2026-07-14T08:10:20.123Z",
  "type": "artifact.committed",
  "data": {
    "artifact_id": "art_...",
    "bundle_id": "bnd_..."
  }
}
```

- sequence 只在同一 Job 内有序；
- 至少一次投递；
- 用 event ID 或 job/sequence 去重；
- 支持 cursor/after-sequence 重连；
- 事件丢失由 JobStore 查询和 reconcile 修复；
- progress 是提示，不是 Portable Asset；
- Job 终态和 committed Bundle 是权威结果。

## 28. MCP

### 28.1 不作为内部 RPC

MCP Adapter 调用 AllToNote Application Service。它不启动 `alltonote` CLI 后解析 stdout，也不自行实现下载、LLM、Bundle 写入或 Publisher。

### 28.2 Knowledge Read-only Profile

默认提供：

- Published Markdown；
- Workspace metadata；
- index status；
- 来源/provenance 的安全摘要；
- `knowledge_search`；
- `knowledge_related`；
- `knowledge_sources`；
- `knowledge_recent`。

默认不读取：

- `raw/personal`；
- Draft；
- JobStore；
- staging/quarantine/trash；
- Credential；
- sensitive/confidential 内容。

### 28.3 Production Profile

Phase P5 目标 tools：

```text
recipes_list
recipe_describe
job_start
job_get
job_cancel
job_respond
job_events
artifact_get
draft_get
publish_plan
```

首版禁止：

- `publish_apply`；
- common publish；
- Bundle delete/purge；
- migration/import；
- Feature Pack install；
- 任意 shell；
- 任意绝对路径；
- 任意环境变量；
- Credential 明文；
- 直接写 `wiki/` 或 final Bundle。

### 28.4 Workspace Grant

每个 MCP Server 实例/上下文绑定显式 Grant：

- Workspace identity；
- read scope；
- 是否可读 Draft/raw；
- 是否可启动付费 Job；
- 是否可响应 challenge；
- 允许 Recipe；
- 预算；
- 过期时间；
- 允许的本地 source handles。

不使用“Desktop 最近打开的 Workspace”。本地文件输入使用 opaque source handle，不能由 Agent 直接提交任意 `C:\...`、UNC 或 POSIX absolute path。

### 28.5 Artifact Resource

小型 Markdown 可内联；大型 Transcript、音视频和二进制返回 opaque Resource URI，例如：

```text
alltonote://workspace/<workspace-id>/bundle/<bundle-id>/artifact/<artifact-id>
```

URI 不包含绝对路径。每次读取重新检查 Grant、Bundle validity、Artifact hash、privacy、media type 和 payload size。

### 28.6 长任务

稳定内部模型：

```text
job_start -> job_id
job_get(job_id)
job_cancel(job_id)
job_respond(job_id, challenge_id, response)
```

客户端支持稳定 MCP Task 时可以映射；不支持时仍返回 Job ID。MCP stdio 调用不直接承载小时级进程，因此 Production MCP 与 on-demand Engine 一起进入 Phase P5。

## 29. 错误合同

### 29.1 顶层分类

至少包括：

```text
invalid_input
workspace_not_granted
workspace_incompatible
capability_unavailable
policy_denied
credential_unavailable
source_unavailable
external_service_failed
external_outcome_unknown
artifact_invalid
job_conflict
job_store_corrupted
publish_conflict
cancelled
engine_incompatible
internal_error
```

每个错误包含 stable category、specific code、retryable、安全 message、next actions、correlation ID 和可选 job/step/attempt refs。

### 29.2 iwiki 映射

| iwiki code | AllToNote category | 典型 code |
|---|---|---|
| `invalid_argument` | `invalid_input` | `portable_request_invalid` |
| `invalid_workspace` | `workspace_incompatible` | `workspace_invalid` |
| `schema_too_new` | `workspace_incompatible` | `portable_schema_too_new` |
| `validation_failed` | `artifact_invalid` | `portable_bundle_validation_failed` |
| `conflict` | context-specific conflict | `bundle_id_conflict` 等 |
| `permission_denied` | `policy_denied` | `workspace_write_denied` |
| `retryable_runtime` | `external_service_failed` | `iwiki_runtime_retryable` |
| `internal` | `internal_error` | `iwiki_internal_error` |

Gateway 只能按 stable code 映射，不解析 message。可以保留安全的 `upstream=iwiki` 和 `upstream_code`，不能返回 traceback、内部路径或 Secret。

### 29.3 Retry Class

Step/Capability 声明：

- `pure_idempotent`；
- `side_effect_idempotent`；
- `non_idempotent_or_billable`；
- `manual_reconciliation`。

外部服务不承诺 exactly-once。结果未知时停止并返回 `external_outcome_unknown`，不盲目重复付费调用。

## 30. 安全与隐私

### 30.1 路径

- Workspace root 和合同路径来自 iwiki inspect；
- 所有公共请求使用 Workspace identity/relative path/opaque handle；
- final payload 只允许普通文件；
- 拒绝 symlink/junction/reparse/hardlink 依赖；
- normalize 后再次检查 containment；
- Windows case/device/UNC/long-path 专项测试；
- Markdown link 与 manifest path 使用不同规则但都不越界。

### 30.2 网络来源

Source Adapter 必须处理：

- SSRF；
- redirect；
- DNS rebinding；
- 内网/metadata 地址；
- 下载大小和时长；
- MIME 欺骗；
- 压缩炸弹；
- 超时、重试和取消；
- Cookie/Credential 脱敏。

### 30.3 Prompt Injection

来源内容是不可信数据：

- 不得改变 Agent Grant；
- 不得请求额外路径、Credential 或 Tool；
- AgentExecutor 使用最小 ExecutionGrant；
- Source 文本与系统指令分离；
- Quality/Review 显示高风险提示；
- Receipt 不保存完整注入内容作为诊断日志。

### 30.4 渲染

Desktop/Web Viewer 清理：

- HTML；
- SVG；
- Mermaid；
- external image；
- iframe；
- script/event handler；
- dangerous URI。

普通 Markdown 文件可读不等于允许在 WebView 中无清理执行。

### 30.5 Secret Scan

Secret scanner 覆盖：

- Bundle；
- Receipt；
- QualityReport；
- JobStore；
- event；
- log；
- crash report；
- export。

发现疑似 Secret 时按 policy block、redact 或 PendingChallenge；不能把密钥写入 Bundle 后只在 UI 隐藏。

## 31. 测试矩阵

### 31.1 Schema Golden Fixtures

llm-iwiki 发布合法 Fixture：

- 最小 Bundle；
- 一个 Source、多个 SourceRevision；
- archived/reference-only/external-local；
- 跨 Bundle ArtifactRef；
- Transcript JSONL；
- EvidenceSet JSONL；
- 带图片 Draft；
- Quality pass/pass-with-warnings/fail；
- 多依赖 closure；
- closure-complete export；
- offline-complete export。

非法 Fixture：

- commit/manifest hash mismatch；
- Artifact size/hash mismatch；
- 未声明/缺失文件；
- duplicate typed ID；
- Bundle ID/目录不一致；
- absolute/`..`/NUL/device path；
- symlink/junction/reparse/hardlink；
- dependency missing/hash mismatch；
- materialization 缺字段；
- 非法时间范围；
- 非 UTF-8；
- Transcript 缺 header/负时间/乱序；
- text-span 非 codepoint 边界；
- page/slide 为 0；
- unknown required contract；
- future schema；
- 非法 extension namespace；
- strong Evidence 指向不可携带目标。

### 31.2 Core Unit Test

覆盖：

- Source/Revision invariant；
- Artifact Envelope；
- EvidenceRef/Locator；
- Transcript segment；
- Draft footnote mapping；
- outputs；
- Receipt redaction；
- Quality aggregation；
- Job success 与 quality fail；
- retry 新 Job；
- Draft hash 改变后的失效；
- cancel/commit/fencing；
- stable error mapping；
- Legacy precedence/time conversion/idempotency。

遵循 FIRST + AAA；使用 fake filesystem/clock/gateway，不 Sleep，不测试私有方法。

### 31.3 iwiki Provider Test

SDK 与 CLI 对同一 Fixture 验证：

- validity/code 一致；
- manifest hash 一致；
- commit/conflict 一致；
- export/import byte preservation；
- delete impact 一致；
- migration output 合法；
- capability/contract report 一致。

### 31.4 AllToNote Consumer Test

使用固定已发布 Contract 和真实 iwiki SDK/CLI：

1. AllToNote 生成 staging；
2. iwiki semantic validate；
3. prepare；
4. Commit Guard；
5. commit；
6. 重开 Bundle；
7. 对比 Bundle/manifest/Artifact/Receipt hash；
8. 删除 JobStore 和缓存；
9. 仍可由 iwiki/普通文件读取。

不能仅用 AllToNote fake validator 声称兼容。

### 31.5 Cross-surface Conformance

同一输入分别通过 Core、CLI、Desktop API 和后续 MCP，验证：

- Job state；
- Artifact set；
- manifest hash；
- Quality；
- error category/code；
- Grant/Policy；
- cancel/idempotency。

UI 文案可以不同，业务语义不能不同。

### 31.6 Import/Export/Migration/Delete

覆盖：

- export exact bytes；
- ZIP wrapper 等价；
- same ID/same hash 幂等；
- same ID/different hash 冲突；
- 第 N 个 import 失败；
- journal resume；
- dependency order；
- external-local 不静默归档；
- future schema 不迁移；
- migration dry-run；
- new Bundle + migrates_from；
- dependents/publication 阻止 delete；
- trash/restore/conflict/purge；
- reappeared bundle 报告。

### 31.7 Performance

固定 corpus 覆盖：

- 单 Bundle 大 payload；
- 大量小 Bundle；
- 深/宽 dependency graph；
- 10,000 published documents 基线；
- 冷/热 Machine Index；
- OneDrive placeholder；
- Windows Defender/杀毒共享占用；
- CLI/Runtime cold start；
- stream memory；
- SDK 与批量 CLI；
- export/import throughput。

性能报告记录硬件、磁盘、缓存状态、版本和 corpus，不使用不可复现数字。

## 32. 故障注入

### 32.1 Commit 路径

| 注入位置 | Oracle |
|---|---|
| Artifact 写入中磁盘满 | final 不存在；staging 可恢复/隔离 |
| Artifact 完成，manifest 前崩溃 | final 不存在 |
| manifest 完成，prepare 前崩溃 | final 不存在；可重新 prepare |
| prepare 完成，Guard 前崩溃 | Prepared 失效；final 不存在 |
| commit.json 写入中崩溃 | final 不存在 |
| fsync 后、rename 前崩溃 | final 不存在；重做 prepare |
| rename 后、JobStore 前崩溃 | final 有效；reconcile succeeded |
| sharing violation | 有界重试，不 copy fallback |
| stale Worker 提交 | fencing 拒绝 |
| cancel/commit 并发 | 只有一个线性化结果 |
| 同 Bundle ID 并发 | 同 hash 幂等，不同 hash 冲突 |

### 32.2 其他故障

| 故障 | Oracle |
|---|---|
| JobStore corruption | 停止调度，保留 Portable Asset |
| Artifact tamper | integrity mismatch，不作为输入 |
| cloud partial sync | replica_incomplete，不误 quarantine |
| future schema | unsupported，不移动/修改 |
| import 第 N 项 crash | 已完成保留，journal 可续 |
| delete rename 后 journal crash | 按 trash metadata 对账 |
| network timeout/429 | 按 Step policy 退避 |
| paid outcome unknown | 不盲目重试 |
| Runtime/Pack 缺失 | waiting challenge 或 failed，不静默升级 |
| worker/engine/client kill | 进程 containment + fencing |

统一 oracle：

1. final 不存在或完整有效；
2. 不出现部分 final；
3. 不覆盖已有不同内容；
4. 不把 staging 猜成成功；
5. 不重复未知付费副作用；
6. 不因 Machine State 损坏丢 Portable Asset。

### 32.3 安全故障测试

- SSRF/redirect/DNS rebinding；
- ZIP bomb/path traversal；
- MIME 欺骗；
- Markdown/HTML/SVG/Mermaid XSS；
- Prompt Injection；
- Agent tool/path/credential escalation；
- MCP Grant bypass；
- Named Pipe/Unix Socket 跨用户访问；
- symlink/junction/reparse race；
- Secret scanner；
- Tauri command allowlist。

## 33. 兼容性 Gate

### 33.1 Writer Preflight

任何可能提交 Bundle 的 Job 启动前检查：

- Workspace inspect 成功；
- Workspace Schema 支持；
- `raw_personal` 由 iwiki 返回；
- Workspace 可写；
- iwiki SDK API 支持；
- iwiki CLI Protocol 支持；
- `portable_bundle_validate_v1`；
- `portable_bundle_commit_v1`；
- Contract ID 在 Runtime 支持列表；
- schema set hash 正确；
- Bundle/Artifact/Evidence/Receipt 版本支持；
- Recipe Locator 全部支持；
- Commit Protocol 支持；
- roots 不重叠；
- staging/final 同卷；
- 文件系统满足原子提交前提。

失败时：

- 已有 Bundle 仍可只读；
- Writer Job 不启动；
- 返回 capability/workspace error；
- 不直接写 final；
- 不回退旧 loose-file 输出并伪装成功。

### 33.2 Runtime Contract Lock

官方 Runtime 携带经过测试的合同清单：

- 支持的 contract IDs；
- immutable schema set IDs/hashes；
- iwiki SDK API range；
- iwiki CLI Protocol range；
- Workspace Schema range；
- official Pack range；
- Windows/macOS platform status。

兼容 superset 必须作为新合同发布并加入支持列表，不能只因为字段“看起来相似”跳过验证。

### 33.3 当前 iwiki Gate

当前 CLI Protocol 1 不声明 Portable capability，因此预期行为是：

- Phase 2 read-only 继续可用；
- Portable Writer 返回 `portable_bundle_commit_capability_missing`；
- 不调用现有 `apply-publish` 代替；
- 不在 AllToNote 中复制一套 final writer；
- 先完成 llm-iwiki Phase P0，再启用 AllToNote P1 Writer。

### 33.4 Rollback

- 旧 Runtime 读取其支持的旧 Bundle；
- 新 required contract 只读报告 unsupported；
- 不自动降级/重写；
- Runtime 更新不自动切换 Writer schema；
- active Job 固定 Runtime/Recipe/Capability/Contract；
- updater 不删除活动 Job 引用版本；
- 用户可安装旧版本、显式迁移为新 Job 或终止。

## 34. 分阶段落地

### Phase P0：iwiki Portable Contract Foundation

实现：

- 公开 Schema/文档；
- Golden Fixtures；
- Reference Validator；
- inspect Portable capability；
- 稳定 iwiki SDK 最小接口；
- `iwiki portable validate`；
- PreparedBundle；
- atomic commit；
- Windows 文件系统/故障注入。

Gate：fake/manual Producer 能通过 SDK/CLI 创建相同合法 Bundle；当前无 capability iwiki 明确拒绝。

### Phase P1：Knowledge Compiler Contract（总体 Phase 3A）

实现：

- Core 领域对象；
- Bundle assembler；
- Artifact/Evidence/Receipt/Quality；
- JobStore/staging；
- foreground CLI；
- Commit Guard；
- fake Recipe；
- iwiki SDK Adapter；
- 真实合同 E2E。

不实现视频、Desktop Produce、detached Engine、MCP、Publisher 和生命周期 UI。

Gate：CLI-only fake Recipe 产生合法 Bundle；cancel/commit/fencing/crash 全部确定。

### Phase P2：Video Note Headless（总体 Phase 3B）

实现：

- 现有视频路径拆成 Recipe/Capability；
- SourceRevision；
- audio/subtitle/Transcript；
- 时间 Evidence；
- screenshot Artifact；
- Draft/Quality/Receipt；
- `alltonote produce video`。

Gate：不安装/打开 Desktop，从 URL 生成完整可审阅 Bundle；不再给旧 `NoteGenerator` 增加新来源分支。

### Phase P3：Legacy 与 Portable Lifecycle

实现：

- Legacy scan/import；
- Desktop IndexedDB export；
- iwiki export/import；
- migration；
- trash/delete/restore/purge；
- import journal。

Gate：Legacy 非破坏且幂等；export/import 保字节；delete 可恢复且依赖安全。

### Phase P4：Review 与 Publisher（总体 Phase 3C）

实现：

- Source/Evidence/Draft 对照；
- Draft 新版本；
- check/approve/reject/supersede；
- PublishPlan；
- personal publish；
- common 强确认；
- conflict；
- Desktop Produce/Review UI。

附件仍由未来 Attachment Contract Gate；本阶段不发明 `wiki/_assets`。

### Phase P5：Engine 与 Production MCP（总体 Phase 4）

实现：

- `--detach`；
- on-demand Engine；
- scheduler lease；
- durable events；
- cancel/reconcile；
- resource scheduling；
- Production MCP；
- active Runtime/Pack protection。

Gate：kill Desktop/CLI/MCP/Worker/Engine 后不产生半 Bundle，状态确定，不重复副作用。

### 后续 Recipe

依次加入：

- Article/Wiki；
- PPT/PDF；
- Codebase/UE5；
- Personal work/log/Git digest；
- Public/Remote Knowledge Provider。

每个新能力只扩展 Recipe、Capability、Artifact content schema、Locator 和 Quality Profile，不重新发明底层协议。

## 35. 已拒绝的实现捷径

1. AllToNote 直接写最终 `bundles/`；
2. AllToNote 和 iwiki 各有一套 Validator；
3. 每次 Desktop 点击启动 CLI 并解析 stdout；
4. MCP 直接写 Bundle 或 `wiki/`；
5. 使用 current `apply-publish` 写 raw；
6. status=SUCCESS 作为 committed 证据；
7. 原地修改 committed Draft；
8. Quality fail 直接丢弃 Bundle；
9. 通过扩展名猜 outputs；
10. 把 absolute path 写入 Bundle；
11. export 时静默复制 external-local；
12. import 冲突时自动换 ID；
13. atomic rename 失败后逐文件 copy final；
14. 用云盘作为分布式锁；
15. superseded/rejected 自动 GC；
16. 先做全局 CAS；
17. 把 SHA-256 称为作者签名；
18. 让旧 Reader 猜未知 required contract；
19. 把 JobStore、QMD 或 Chroma 放进 Bundle；
20. 在 Source Bundle MVP 中提前实现 daemon、Production MCP 或附件发布。

## 36. 架构决策摘要

1. 采用 Workspace-default closure；
2. Bundle 不可变，所有修订/迁移创建新 Bundle；
3. 使用 typed UUIDv7，ID 与 hash 分离；
4. v1 每个 Artifact 是一个普通 `bundle_file`；
5. manifest 集中保存 Artifact Envelope；
6. Source 与 SourceRevision 分离；
7. Evidence 绑定 SourceRevision、Artifact hash 和 typed locator；
8. Transcript 使用 JSONL，Draft 使用普通 GFM Markdown；
9. Quality、Review、Publish 生命周期分离；
10. Receipt 是安全摘要，不是 JobStore 备份；
11. commit -> manifest -> payload/dependency 构成完整性根；
12. AllToNote 构建候选，iwiki 权威校验和 commit；
13. 官方 Runtime 通过稳定 iwiki SDK，同一能力通过 iwiki CLI 开放给第三方；
14. 当前 iwiki 无 capability 时 fail closed；
15. Export 保持精确字节，ZIP 只是包装；
16. Legacy Import 显式、非破坏、幂等；
17. 删除使用 plan -> trash -> grace -> restore/purge；
18. Machine Index 可重建，不是知识真相；
19. MCP 是外部 Adapter，不是内部总线；
20. Windows 先发布，macOS 独立 Gate；
21. P0/P1 先证明合同和 fake Recipe，再迁移视频；
22. 不在 v1 引入 CAS、签名、云同步、daemon 或分布式删除。

## 37. 实施计划分解约束

本文覆盖 Portable Contract 的完整目标形态，但不能作为一个巨大 implementation plan 一次实施。后续计划必须按 Phase 拆分：

1. 先为 llm-iwiki P0 单独写实施计划；
2. P0 发布并通过 Provider Gate 后，为 AllToNote P1 写独立计划；
3. P1 通过后再规划视频 P2；
4. Lifecycle、Review/Publisher、Engine/MCP 各自独立设计/计划；
5. 任何计划不得把后续 Phase 能力偷渡进当前 Phase。

第一个实施计划的范围上限是 Phase P0。它不修改当前视频流程，也不实现 Desktop 或 MCP。

## 38. 本设计完成定义

当以下条件满足时，Portable Artifact 与 Source Bundle 设计可以进入实施计划阶段：

1. 用户确认本文书面规格；
2. 物理布局、ID、Schema/引用语义无歧义；
3. Core/iwiki 最终写入所有权明确；
4. PreparedBundle 与 Commit Guard 线性化明确；
5. 当前 iwiki capability 缺口明确；
6. Import/Export/Legacy/Delete/Performance 有边界但未误列为 P0；
7. 测试、故障 oracle 和兼容 Gate 可执行；
8. 后续实施按 P0、P1、P2、P3、P4、P5 分解；
9. 在用户审阅确认前不开始代码实现。
