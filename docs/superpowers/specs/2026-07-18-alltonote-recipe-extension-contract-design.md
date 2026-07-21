# AllToNote Recipe 最小扩展合同设计

```yaml
doc_type: contract-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
  - 2026-07-14-alltonote-video-producer-design.md
  - 2026-07-18-alltonote-runtime-cli-feature-pack-design.md
downstream:
  - ../plans/2026-07-18-alltonote-recipe-extension-contract-implementation-plan.md
  - 2026-07-18-alltonote-article-wiki-recipe-design.md
  - 2026-07-18-alltonote-document-recipe-design.md
  - 2026-07-18-alltonote-codebase-ue5-recipe-design.md
  - 2026-07-18-alltonote-personal-work-digest-design.md
implementation_status: not-started-current-video-specific-seam-audited
last_verified_at: 2026-07-19
```

## 1. 决策摘要

AllToNote 需要让 Video、Article/Wiki、PDF/PPT、Codebase/UE5 和 Personal Work 等能力共享稳定底座，但不应现在就建设通用插件平台或 Workflow 引擎。

这里的层级关系是：AllToNote 是上层知识编译与积累平台，Production 是用户发起知识转化的用例，Recipe 是平台内部的版本化扩展单位。Video、Article/Wiki、Document、Codebase 和 Personal 是并列官方 Recipe；任何一个 Recipe 都不是 Runtime、CLI、Job、Bundle、Review 或 Publisher 的宿主。

本合同按第 19 节的真实消费者 Gate 分阶段冻结。当前唯一生效的 X0-A 表面是：

```text
ProduceRequest -> RecipeEndpoint.submit -> ProduceSubmission
                         │
                  static Registry
                         │
                  thin ProduceService
                         │
                  Video Adapter -> existing VideoService
```

Preflight、Plan、Output、Result、Repository、commit 与 schema 均不属于 X0-A 的公开合同；其跨 Recipe 形状只能由 Video 与真实 Document/PPT 纵切在 X0-B 共同证明。

每个 Recipe 仍拥有自己的领域流水线、质量策略和错误。Core 不要求所有 Recipe 使用相同 stage 数、相同分块方式、相同 LLM Prompt 或相同解析器。

## 2. 为什么现在需要合同，但不需要插件 SDK

Video 已证明了 Source/Transcript/Evidence/Draft/Quality/Job 的纵向闭环。未来多个 Recipe 会共同需要：

- CLI/MCP 能力发现；
- 输入身份与 revision；
- 配置/Pack preflight；
- Job/Checkpoint/取消；
- Portable Bundle；
- Review/Publisher；
- 统一错误和事件投影。

如果没有最小合同，后续每个 Recipe 会复制独立 FastAPI/CLI/Pipeline；如果过早设计公共插件 SDK，又会把尚未验证的抽象冻结为兼容负担。

因此：

- 当前合同是 AllToNote 内部候选接口，在 X3 前允许受控、带迁移记录地演进；
- 官方 Recipe 可在同一仓库或官方 Pack 中实现；
- 至少 Article/Wiki 和 Document 两类 Recipe 生产验收后，才允许进入公开第三方 SDK 评估，并仍须满足第 17 节全部升级门；
- 未经评估，Pack 只部署官方受控能力，不执行任意第三方 Python 代码。

## 3. 目标

- X0-A 只建立最小 submission/control-plane 接缝：RecipeKey、RecipeDescriptor、InputDescriptor、ProduceRequest、ProduceSubmission、`RecipeEndpoint.submit`、静态 Registry 与薄 ProduceService；
- PreflightReport、RecipePlan、RecipeOutput、ProduceResult 的通用形状不是 X0-A 前置合同，只能由 Video 与第一个真实 Document/PPT 消费者在 X0-B 中共同证明；
- 输入、输出、Job、错误、事件和权限最终可被 CLI/Desktop/MCP 统一处理；
- Recipe 可声明能力和重依赖；
- Recipe-specific 逻辑不泄漏进 Core；
- 产物遵守 Portable/iwiki 合同；
- 恢复时固定 Recipe/compiler/Pack identity；
- 能测试每个 Recipe 的来源真实性和故障矩阵；
- 新 Recipe 不需要复制 Runtime/Job/Publisher。

## 4. 非目标

- 任意 YAML DAG；
- 用户可视化拖拽 Workflow；
- 在运行时下载并执行未签名代码；
- 统一所有 Recipe 的内部 stage；
- 通用 Prompt 模板语言；
- 让 Recipe 直接写 `wiki`；
- 让 Recipe 自己管理 Secret、JobStore 或更新；
- 让一个 Adapter 同时负责采集、生成、发布和索引；
- 用“所有输入都是文本”抹掉时间、页面、布局、代码 revision 等证据语义。

## 5. 所有权边界

### 5.1 Core 在 X0-A 拥有

- `ProduceRequest`、`ProduceSubmission` 与薄 `ProduceService`；
- `RecipeEndpoint.submit`；
- 静态 Recipe Registry 与 descriptor 验证；
- 对现有 Job/Checkpoint、Portable/iwiki 和 Video 数据面的委托，不新增公开 Repository、commit 或 schema。

通用 Preflight、Plan、Output、Result、Artifact/Repository 与 atomic commit 只能在 X0-B 由 Video 和真实 Document/PPT 共同抽取。本节后续关于 Job、Artifact、Review、Publisher 的职责描述是长期平台边界，不把对应类型提升为 X0-A 公共表面。

### 5.2 Recipe 拥有

- 该类输入的识别与 canonical identity；
- SourceRevision 计算；
- acquisition/extraction/normalization 计划；
- 领域 Evidence locator；
- 分块/结构分析/编译策略；
- Draft kind、质量规则和降级策略；
- Recipe-specific 错误；
- 所需 Pack/capability；
- 真实验收 fixture。

### 5.3 Adapter/Pack 拥有

- 某平台/格式/Provider 的外部交互；
- 工具版本探测；
- 受控子进程；
- 结构化结果与明确错误；
- 不拥有 Job/重试/Bundle/Publisher。

### 5.4 依赖方向与禁止边界

```text
CLI / Desktop / MCP
  -> ProduceService
      -> RecipeRegistry -> Recipe contract <- Video / Document / Web / Code / Personal
      -> shared Job / Checkpoint / Artifact / Quality / Review / Publisher
          <- infrastructure adapters implement common ports
```

依赖只能由具体 Recipe 指向通用合同。Core/Application/Job Repository 不得导入 `recipes.video`、`recipes.document` 等实现模块，也不得在通用 DTO 中直接返回 `VideoProduceResult`、`PdfResultPlan` 等专用类型。Recipe-specific 数据通过有命名空间的 extension、Artifact kind、Evidence locator 或 Draft kind 表达。

新增 Recipe 的默认改动范围是：新增 Recipe 包、领域 Adapter/Pack、descriptor/plan/quality 和组合根注册，再补合同与真实 fixture。若必须复制 ProduceService、JobStore、Checkpoint、Bundle commit、Review、Publisher 或 CLI handler Pipeline，说明接入违反合同；若必须修改通用合同，则新增字段必须有至少两个真实消费者或保留为 Recipe extension。

## 6. RecipeDescriptor

```json
{
  "recipe_id": "alltonote.video-producer",
  "recipe_version": 2,
  "display_name": "Video to Knowledge",
  "input_kinds": ["url", "local-file"],
  "output_kinds": ["knowledge-note", "faithful-edition"]
}
```

X0-A 只冻结用于静态发现和确定性 resolve 的字段。capability、Pack、profile、detach/foreground/resume、publisher target、config schema 和 implementation build identity 均属于 X0-B 或后续 Pack 候选，不是 X0-A 公共合同。

## 7. InputDescriptor

```json
{
  "kind": "url",
  "value": "https://example.com/..."
}
```

X0-A 的 InputDescriptor 只保留 kind/value（或 opaque ref）及已证明的不可变轻量属性；credential、capture、repo grant、collection、activity-window、hints 等扩展字段留在 Recipe-owned parameters 或 X0-B 候选。

## 8. X0-B 候选：Preflight（X0-A 不公开）

以下形状仅保存为历史设计候选，不是当前可实现或可依赖的合同。只有真实 Document/PPT 与 Video 在 X0-B 共同需要时，才重新评审并冻结。

Recipe 在创建昂贵外部操作前可能需要表达：

```json
{
  "recipe_id": "alltonote.document-note",
  "accepted": true,
  "resolved_input_kind": "local-file",
  "required_capabilities": ["recipe.document.pdf.native"],
  "missing_capabilities": [],
  "credentials": [{"ref": "...", "status": "not-required"}],
  "resource_estimate": {"disk_mb": 500, "memory_mb": 1000, "duration_class": "medium"},
  "network_destinations": [],
  "warnings": [],
  "user_actions": []
}
```

Preflight 分两层：

- static：配置、Pack、格式、权限、空间；
- dynamic：可选的网络/认证/Provider 探测。

dynamic probe 本身可能有外部副作用/风控，必须由 Recipe 明确且有限执行；不能用无限重试“验证 URL”。

## 9. X0-B 候选：RecipePlan（X0-A 不公开）

Plan 是确定性、可摘要、可 digest 的执行计划，不是通用 DAG：

```json
{
  "plan_version": 1,
  "recipe_id": "alltonote.web-note",
  "recipe_version": 1,
  "recipe_implementation": "...",
  "input_fingerprint": "sha256:...",
  "source_revision_strategy": "http-snapshot-v1",
  "selected_adapters": ["http-fetch", "readability"],
  "selected_profile": "balanced",
  "requested_drafts": ["knowledge-note"],
  "model_policy_ref": "balanced-default",
  "pack_versions": {},
  "expected_artifact_kinds": [],
  "estimated_external_operations": [],
  "plan_digest": "sha256:..."
}
```

Plan 可包含 Recipe 自己的 stage 摘要和有命名空间的扩展，但 Core 只依赖稳定字段、规范化扩展和 digest。Video v2 的多个输出 compiler/Recipe Binding 先保留在 Video-owned plan extension 与 Artifact provenance；没有第二个真实消费者前不提升为所有 Recipe 的通用必填字段。恢复时会影响结果的字段变化必须使旧 Job 不兼容。

## 10. 通用生产用例与最小执行接口

### 10.1 X0-A 的 ProduceRequest 与 ProduceSubmission

所有入口先构造同一版本化请求，最小通用字段为：

```json
{
  "produce_request_version": 1,
  "client_request_id": "...",
  "recipe_selector": "alltonote.document-note@1",
  "input": {"kind": "local-file", "value": "opaque-ref"},
  "workspace_ref": "...",
  "principal": "workspace-grant-or-stable-caller-ref",
  "requested_outputs": ["knowledge-note"],
  "recipe_parameters": {}
}
```

约束：

- `recipe_selector` 由单一 `produce` 命令族显式给出或从其用户友好别名解析，进入 ProduceService 时必须明确并固定；
- `input`、Workspace、Secret 和本地路径继续遵守 ref/grant 合同；
- `recipe_parameters` 在 X0-A 只由被选中的 Endpoint/Video Adapter 使用现有规则校验，不新增公开参数 schema；
- request digest 沿用现有 Video 语义；X0-A 不定义新的通用结果、输出或完成结果形状。

X0-A 的提交响应只使用 `ProduceSubmission`，至少携带既有 Job 引用和提交状态。下述通用完成结果曾是早期候选，现已被本段取代；它不是 X0-A 合同，是否形成 `ProduceResult` 由 X0-B 的真实 Document/PPT 纵切决定：

```json
{
  "produce_result_version": 1,
  "recipe": {"id": "alltonote.document-note", "version": "1"},
  "job_id": "job_...",
  "state": "succeeded",
  "source_revision_refs": ["sr_..."],
  "artifact_refs": ["artifact_..."],
  "draft_refs": ["draft_..."],
  "quality_refs": ["quality_..."],
  "bundle_ref": "bundle_...",
  "warnings": []
}
```

### 10.2 ProduceService 与 RecipeRegistry

应用层只提供一条生产用例：

```python
class ProduceService:
    def submit(self, request: ProduceRequest) -> ProduceSubmission: ...

class RecipeEndpoint(Protocol):
    descriptor: RecipeDescriptor

    def submit(self, request: ProduceRequest) -> ProduceSubmission: ...

class RecipeRegistry:
    def list(self) -> list[RecipeDescriptor]: ...
    def describe(self, selector: str) -> RecipeDescriptor: ...
    def resolve(self, selector: str) -> RecipeEndpoint: ...
```

`ProduceService` 在 X0-A 只负责通用请求校验、通过静态 Registry 解析并固定 Endpoint，然后委托 `RecipeEndpoint.submit` 返回 `ProduceSubmission`；Video Adapter 再委托既有 VideoService。它不拥有 preflight、plan、执行、结果投影、Repository、commit 或 schema，也不实现任一来源的采集、分块、Prompt 或质量阈值。`RecipeRegistry` 首版是组合根显式注册的官方静态 registry，不使用动态 import、entry point 扫描或远端代码下载。

`ProduceService` 默认是模块化单体中的普通进程内应用对象，不是 HTTP 服务、独立进程或微服务；CLI 前台可直接调用。Engine 的业务触发条件已经成立，但仍受 Wave 0-4 阻塞；解除阻塞后也只能托管同一对象。该接口用于减少入口和 Recipe 耦合，不扩大部署拓扑。

Registry 在 X0-A 只发现顶层可提交 Endpoint。输出 binding、preflight、plan 与执行接口继续由现有 Video 实现内部拥有；除非真实 Document/PPT 在 X0-B 证明共同需要，否则不提升为公共合同，也不暴露另一个生产入口。

CLI 的 `--wait`、Desktop 的进度页和 MCP 的长任务映射是入口/传输策略：它们消费 `ProduceSubmission.job_id` 并调用统一 Job API，不改变 submit 语义，也不要求 ProduceService 同时承担终端渲染或 IPC。

### 10.3 X0-B 候选执行接口（X0-A 不公开）

以下伪接口仅保留为已被取代的设计讨论，不是 X0-A 实施目标。X0-A 的 Recipe 公共表面只有 `RecipeEndpoint.submit`；Preflight、Plan、Output 及其 Context 必须等真实 Document/PPT 驱动 X0-B 后重新定义。

历史候选：

```python
class Recipe(Protocol):
    descriptor: RecipeDescriptor

    def preflight(self, request: ProduceRequest, context: PreflightContext) -> PreflightReport: ...
    def plan(self, request: ProduceRequest, preflight: PreflightReport) -> RecipePlan: ...
    def execute(self, job: JobContext, plan: RecipePlan) -> RecipeOutput: ...
```

`RecipeOutput`、通用 `ProduceResult` 与 commit receipt 均为已被 X0-A 范围决策取代的历史候选；只有 X0-B 可以基于 Video 与真实 Document/PPT 的共同证据重新引入。

`JobContext` 提供：

- checkpoint read/write；
- external operation coordinator；
- cancellation；
- event/progress；
- resource/temporary workspace；
- credential resolver；
- grant-bound input access；
- ModelExecutor/AgentExecutor；
- Artifact staging；
- fixed runtime/pack identity。

Recipe 不获得 JobStore 原始连接、任意文件系统、Publisher 或更新管理器。

### 10.4 入口归一化

```text
alltonote produce video|web|document|codebase|work-digest ...
alltonote produce <input> --recipe <id>@<version>
alltonote produce --request <request.json>
alltonote recipe list|describe ...
Desktop Produce form
Production MCP tool
    -> ProduceRequest v1 -> ProduceService
```

`produce <kind>` 是明确类型的用户友好别名；`produce --recipe` 与 `produce --request` 是显式自动化形式；`recipe list/describe` 只负责发现。所有等价形式必须生成相同 canonical request 与 Job identity，并进入同一 ProduceService；不得直接调用 VideoService/DocumentService 或建立第二条错误、Job、Bundle 路径。当前不发布独立 `add` 或 `run` 主命令。

## 11. Artifact 与 Evidence 扩展

### 11.1 Artifact kind

通用稳定 kind：

```text
source-metadata
source-snapshot
normalized-content
transcript
evidence-set
draft
quality-report
receipt
```

Recipe-specific kind 使用命名空间，例如：

```text
video/storyboard
web/dom-snapshot
document/page-layout
document/slide-render
code/symbol-index
personal/activity-ledger
```

未知 kind 必须仍可作为 opaque portable artifact 被验证/复制；消费者不能因不认识扩展就破坏 Bundle。

### 11.2 Evidence locator

EvidenceRef 基础字段稳定，locator 为判别联合：

```json
{
  "evidence_id": "ev_...",
  "source_revision_id": "sr_...",
  "locator_kind": "video-time-range",
  "locator": {},
  "content_hash": "sha256:...",
  "confidence": {"level": "high", "basis": "platform-caption"}
}
```

允许 locator：

- video-time-range；
- web-snapshot-range/dom-locator；
- document-page-bbox；
- presentation-slide-shape；
- code-file-line/symbol-at-revision；
- activity-event-range。

不得把不同 locator 压成无语义的“字符偏移”。

### 11.3 Provenance completeness

每个 Draft 质量报告声明：

- 哪些事实声明有 Evidence；
- 来源覆盖率；
- 低置信/缺口；
- 生成/翻译/推论边界；
- 是否 publish eligible。

具体阈值归 Recipe/模式所有，Core 只执行“存在对应 report 且状态允许进入 Review”的门。

## 12. Profile 与模式

`fast/balanced/thorough` 是质量-时间策略，不应改变输出语义种类；`knowledge-note/faithful-edition` 是产物目标。其他 Recipe 可以增加其特定 draft kind，但要区分：

- profile：花多少成本和步骤；
- draft kind：为用户生产什么文档；
- language policy：保留来源语言或显式翻译；
- execution owner：foreground/detach；
- model provider：实现选择。

不得用一个 `quality=high` 参数同时控制这些相互独立的维度。

## 13. 错误分类

通用 category：

```text
input-validation
capability-missing
credential-required
acquisition
extraction
normalization
model
agent
quality
portable-validation
commit
conflict
canceled
internal
```

Recipe 增加稳定 code，例如 `WEB_PAYWALL_CAPTURE_REQUIRED`、`PDF_OCR_LANGUAGE_MISSING`，但 category 保持可供 CLI/MCP/桌面统一处理。

错误必须提供：stage、retryable、是否需要新输入/凭据/Pack、是否已有可复用 checkpoint、用户动作。不返回 Secret、Provider raw 或大正文。

## 14. 版本与恢复

Job 固定：

- recipe_id/contract/implementation；
- RecipePlan digest；
- config snapshot digest；
- compiler/prompt/policy identity；
- adapter/Pack versions；
- Portable/iwiki contract；
- source revision strategy。

升级后恢复规则：

- identity 完全兼容：继续；
- 只改变 UI/日志：继续；
- 已验证 Artifact 可做显式 migration/import：新 Job 消费；
- 影响结果语义：旧 Job 留在旧 Runtime/Pack 或要求新 Job；
- 不静默用新 Prompt/Adapter 接着旧 checkpoint。

## 15. 权限与安全

- Recipe 使用 ProduceGrant/Workspace Grant，而不是任意 OS 权限；
- 外部输入作为不可信数据；
- 网络 destination、redirect、DNS/private range 按 Recipe 策略；
- Local file/repo 通过 root grant 和 canonical containment；
- Browser capture/Secret 使用引用；
- Pack 子进程最小权限；
- Recipe 只写自己的 staging 和 `raw/personal` 提交目标；
- `wiki` 必须由 Publisher；
- common 永不由 Recipe 直接选择；
- 模型/Agent 输出必须经过结构、Evidence 和 Quality 验证。

## 16. 测试合同

每个正式 Recipe 必须提供：

1. descriptor/preflight/plan golden tests；
2. input identity/revision tests；
3. 至少一个无模型 deterministic fixture；
4. 至少一个真实 acquisition/extraction smoke；
5. Model/Agent fake contract test 与真实 provider smoke 分开；
6. 每个外部操作 crash/recovery test；
7. cancellation；
8. no duplicate paid operation；
9. Artifact/Evidence/Quality/Bundle semantic validation；
10. iwiki commit + restart zero replay；
11. malicious input/path/content；
12. CLI JSON/exit/error contract；
13. ReviewCandidate 可读；
14. 性能/成本基线；
15. 第三方工具许可证/版本清单；
16. 单一 `produce` 命令族的等价请求与同一 ProduceService 路由；
17. X0-A contracts/registry/produce_service 无 Recipe-specific import；
18. X0-A 不公开 Preflight、Plan、Output、Result、Repository、commit 或 schema；
19. X0-B 由真实 Document/PPT 与 Video 共同证明新增公共字段和数据面。

Fake Runtime 只能证明编排逻辑，不能替代真实平台、工具、Provider 和 iwiki 的正式验收。

## 17. 抽象升级门

只有满足以下条件才设计公开插件 SDK：

- Video、Web、Document 进入真实用户生产，且至少一个 Codebase/UE5 Agent Recipe 原型通过端到端验证；
- 两个非核心团队/实现者成功新增 Recipe；
- 现有内部合同一年内的破坏性变化已可评估；
- 安全沙箱、签名、供应链、许可证、版本和卸载策略完成；
- 用户确实需要第三方生态，而非官方 Recipe 已够用；
- 能承诺兼容周期和弃用流程。

在此之前，新增官方 Recipe 可以复制少量局部 orchestration，优先保持语义清晰，不为消除几行重复扩大公共抽象。

## 18. 当前实现漂移与 X0 迁移 Gate

截至 2026-07-19，目标合同尚未实现，当前公共接缝仍是 Video-first 的过渡形态：

- `core/sdk.py` 的 `AllToNoteSDK` 直接依赖 `VideoService`，只有 `submit_video`；
- `runtime.py` 暴露 `submit_video`，没有通用 ProduceService；
- `cli/main.py` 只路由 `produce video`，没有 Registry 驱动的 generic `produce` 或 `recipe list/describe`；
- `core/ports/jobs.py` 导入 Video 领域类型并暴露 Video-specific result/commit 方法；
- 通用 Job 状态仍从 Video 领域模块导入；
- 只有 `core/recipes/video/` 实现目录，统一合同与 Registry 未落地。

这些事实不能被解释为“Video Production 就是 AllToNote”。修订后的实施顺序固定为：

1. X0-A characterization 锁定 Video request digest、Job、checkpoint、Portable/iwiki 和零重放语义；
2. X0-A 只建立 RecipeKey/Descriptor/Input/Request/Submission、`RecipeEndpoint.submit`、静态 Registry、薄 ProduceService 与 Video Adapter；
3. `produce video` 与 generic `produce --recipe/--request` 通过同一 `Runtime/SDK -> ProduceService -> Registry -> VideoRecipeAdapter -> existing VideoService` 调用方向；
4. 第一个真实 Document/PPT 纵切共同驱动 X0-B，再抽取通用 Result/Artifact/Repository/atomic commit、legacy dual-read 和 generic reconnect；
5. X0-B 通过后再由 Article/Wiki 验证 URL snapshot/freshness，之后才评估冻结 internal v1。

第一个非 Video Recipe 合入 Gate：X0-A 已通过；Video 零语义 diff；真实 Document/PPT 与 Video 共同证明 X0-B 字段；通用层无 Recipe-specific import；legacy dual-read、migration、atomic commit 和恢复 Gate 通过；没有公共插件 SDK、动态加载或通用 DAG。

## 19. 分期

### Phase X0-A：最小 submission/control-plane 接缝

只建立 RecipeKey、RecipeDescriptor、InputDescriptor、ProduceRequest、ProduceSubmission、`RecipeEndpoint.submit`、静态 Registry、薄 ProduceService 和 Video compatibility adapter；不修改 SQLite schema、legacy result wire、两套 hash、Checkpoint、Portable 或 atomic commit，不公开或冻结 PreflightReport、RecipePlan、RecipeOutput、ProduceResult、Repository、commit 或 schema。

### Phase X0-B：真实 Document/PPT 驱动数据面抽取

在最小真实 Document/PPT 纵切中，只提升 Video 与第二消费者共同需要的 Result、Artifact、Repository、atomic commit、durable query、source/evidence identity 和 generic reconnect。第二消费者提供 X0-B 的设计证据，但在 legacy dual-read、migration、atomicity、恢复和 import Gate 通过前不得合入。

实现 PDF 原生文本最小纵切，用 page/bbox Evidence、多 Artifact 和可选 Pack 验证合同。此时合同可用于内部扩展，但仍不冻结 internal v1。

### Phase X2：Article/Wiki 第三消费者验证

实现非媒体 URL/snapshot/freshness 纵切，记录合同中不适用、缺失或被 Video/Document 偶然塑形的字段。

### Phase X3：冻结 internal v1

只在 Video、Document、Article/Wiki 三类真实验证后冻结跨 Recipe internal v1；三者必须位于同一 Registry/ProduceService。

### Phase X4：Code/Personal 扩展

验证 repo revision/AgentExecutor 和 activity window/scheduler；必要时以兼容扩展增加 locator/trigger，而不是破坏 v1。

## 20. 完成定义

1. CLI/Runtime 可统一发现和提交多个 Recipe；
2. Core 无 Video/Web/PDF/UE5 特定业务分支；
3. 每个 Recipe 保留自己的领域流水线和质量规则；
4. Job/Checkpoint/ExternalOperation/Bundle/Review 只实现一份；
5. Artifact/Evidence 保留各来源真实 locator；
6. Recipe 无权直接写正式 Wiki；
7. Runtime/Pack/Recipe identity 可固定和恢复；
8. Web 与 Document 两类真实验收完成后再冻结 internal v1；
9. 未提前建设通用 DAG 或公共插件市场；
10. 每个 Recipe 满足统一真实验收合同；
11. `produce <kind>`、generic `produce --recipe/--request`、Desktop 和 MCP 都只调用同一 ProduceService；
12. 通用 Core/Application/Repository 不导入 Recipe-specific 类型；
13. Video compiler 未被重写，现有 request/Job/checkpoint/Bundle/质量语义零退化；
14. Video + Document 只建立 internal candidate，Article/Wiki 通过后才冻结 internal v1；公共插件 SDK 仍受 Codebase 原型、外部实现者和生态安全 Gate 控制。
