# AllToNote Recipe 最小扩展合同实施计划

```yaml
doc_type: plan
status: partially-superseded
authority: historical-execution-context
upstream:
  - ../specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - ../specs/2026-07-18-alltonote-recipe-extension-contract-design.md
  - ../specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
implementation_status: partially-superseded-by-implementation-worktree-x0-a
last_verified_at: 2026-07-20
```

## 0. 执行状态与权威边界

本计划已被**部分取代**。它仍保留总体目标、后续消费者顺序和长期 Gate 的历史上下文，但不再是当前 X0-A 的可执行任务来源。

当前 X0-A 的可执行分解位于实现工作树中的：

- `G:/AllToNote-video-producer/docs/design-docs/backend/recipe-x0-compatibility-extraction/spec.md`；
- `G:/AllToNote-video-producer/docs/design-docs/backend/recipe-x0-compatibility-extraction/tasks.md`。

这两份下位文档只负责执行已由 ARCH、REC-CONTRACT 和 RUNTIME 冻结的 X0-A 边界，不能覆盖上位架构。发生冲突时必须停止，先修订上位文档并重新确认，再同步下位任务；不得让 implementation plan、tasks 或当前代码反向成为领域真相。下文 RX-01、RX-03、RX-05、RX-06 及旧 Gate X0 中超出最小 submission 接缝的内容仅作历史记录，不得直接开工；特别是 `add`、独立 `run`、完整 Preflight/Plan/Output/Result DTO、Job/Repository 泛化和 atomic commit 抽取均不属于 X0-A。

修订后的阶段边界是：

```text
X0-A
  = 最小 submission 合同 + 静态 Registry + 薄 ProduceService
    + Video Adapter + SDK/Runtime/单一 produce CLI 兼容路由

X0-B
  = 由一个最小真实 PDF 或 PPTX 纵切共同驱动
    Job result / Artifact / Repository / atomic commit / generic reconnect 去 Video 化
```

不得以 Fake Recipe 作为 X0-B 或多 Recipe 数据面的完成证据，也不得在首个纵切中扩展到 PDF+PPTX 全格式、OCR、Vision 或长文档矩阵。

## 1. 目标、原则与成功标准

目标不是重写 Video，也不是现在发布插件 SDK，而是把已经验证的 Video 垂直切片包裹在一个足够小的多 Recipe 接缝后面，使下一类知识转化无需复制 Runtime、CLI、Job、Bundle、Review 或 Publisher。

实施原则：

- 只提取 Video 与至少一个非 Video 消费者确实共同需要的合同；
- 先用 characterization/golden tests 锁定现有行为，再移动所有权；
- Video compiler、长视频 Knowledge/Faithful、Portable/iwiki、checkpoint 和零重放语义不重写；
- 首版只使用显式组装的官方静态 Registry，不做动态 import、entry point、第三方 SDK、DAG 或 Prompt DSL；
- 单消费者字段留在 Recipe extension/Artifact，不因“以后也许需要”进入 Core；
- X0-A 可与 Vault/Review/Video 发布收敛并行；第一个非 Video Recipe 只能在 X0-A 完成后与 X0-B 联合落地。

成功标准：

1. X0-A 的 generic `produce`、legacy `produce video`、SDK 与 Runtime 进入同一薄 ProduceService；`add`、独立 `run`、Desktop 和 MCP 扩面后移；
2. X0-A 的最小控制面模块不导入 Video-specific 类型；通用 Job/Repository 数据面去 Video 化由 X0-B 验收；
3. Video 现有公共行为和产物零语义退化；
4. Video + Document 通过同一 Registry/ProduceService/Job/Bundle 形成两个真实消费者；
5. Article/Wiki 再验证一类不同来源后才冻结 internal v1；
6. 公共插件 SDK 继续由更高的生态、安全和兼容 Gate 控制。

## 2. 已确认的当前漂移

截至 2026-07-19，实现工作树 `G:\AllToNote-video-producer` 的接缝事实是：

| 位置 | 当前事实 | 目标边界 |
|---|---|---|
| `backend/app/core/sdk.py` | `AllToNoteSDK` 直接依赖 `VideoService`，只有 `submit_video` | SDK/官方入口依赖 ProduceService；`submit_video` 仅兼容 facade |
| `backend/app/runtime.py` | Runtime 暴露 `submit_video` | Runtime 组装通用 ProduceService/Registry |
| `backend/app/cli/main.py` | 只路由 `produce video` | X0-A 增加 generic `produce` 与 `recipe list/describe`；`add`/独立 `run` 后移 |
| `backend/app/core/ports/jobs.py` | 导入 Video 类型并含 Video-specific result/commit 合同 | Job Repository 只依赖通用 Job/Artifact 引用 |
| 通用 Job model/state machine | 仍从 Video 领域模块导入状态 | Job 生命周期归通用 Job domain 所有 |
| `backend/app/core/recipes/` | 只有 `video` 实现，尚无统一合同/Registry | 具体 Recipe 实现通用内部合同并由组合根显式注册 |

这张表只描述迁移起点，不授权修改目标设计来迁就现状。

## 3. 历史 Task RX-00：冻结 Video 行为与建立接缝矩阵

先记录并测试：

- Video request serialization/hash、requested outputs 和 language policy；
- `produce video` 人类/JSON envelope、退出码和错误；
- Job submit/get/list/wait/cancel/retry、checkpoint 和 ExternalOperation；
- VideoResultPlan/VideoProduceResult/JobState 的全部生产者与消费者；
- metadata/subtitle/model/commit 重启零重放；
- Portable/iwiki semantic validation 与 Bundle identity；
- 基础命令 import 集和无重 Pack启动。

输出 contract matrix：每个字段标记 `common`、`video-owned`、`compatibility-projection` 或 `unproven`。本任务不先改生产代码。

Gate：完整基线可解释；任何已有失败先分类；没有字段仅因名字“看起来通用”而进入合同。

## 4. 历史 Task RX-01：最小通用 DTO（X0-A 中已收缩）

本节原范围已被 implementation-worktree X0-A spec/tasks 取代。X0-A 只允许 `RecipeKey`、`RecipeDescriptor`、`InputDescriptor`、`ProduceRequest`、`ProduceSubmission` 和 `RecipeEndpoint.submit`；不得实现或冻结 `PreflightReport`、`RecipePlan`、`RecipeOutput`、`ProduceResult`。原有完整 DTO、result projection 与 plan digest 要求已从当前执行面移除，等待真实第二消费者在 X0-B 中验证。

## 5. Task RX-02：静态官方 RecipeRegistry

建议：

- `backend/app/core/recipes/registry.py`；
- `backend/tests/core/test_recipe_registry.py`。

实现显式组合根注册、list/describe/resolve、重复 ID、未知版本和不兼容 contract fail closed。Registry 保存 descriptor/factory，不在基础命令中 eagerly 实例化或导入重型 Pack。

Gate：空、重复、未知、不兼容、缺 Pack descriptor 和 lazy-load 合同测试通过；不存在目录扫描、动态 Python entry point 或远端代码下载。

## 6. 历史 Task RX-03：唯一 ProduceService（X0-A 中已收缩）

本节原生命周期不属于 X0-A。X0-A 的 ProduceService 只校验最小 envelope、固定并解析 Recipe、委托 `RecipeEndpoint.submit`、返回 `ProduceSubmission`；preflight、deterministic plan、durable Job 所有权和 generic result projection 等待真实第二消费者在 X0-B 中确定。原 preflight→plan→result 流程不得作为 X0-A 实施清单。

## 7. 历史 Task RX-04：RecipeContext 与最小执行接口（延后 X0-B/Agent Wave）

以下内容不属于 X0-A；只有真实 Document/PPT 在 X0-B 证明共同需要，或后续 AgentExecutor Wave 需要相应 grant/process 合同时，才重新评审：

向 Recipe 注入：checkpoint、ExternalOperation、cancel、events、临时 workspace、Artifact staging、credential/grant、ModelExecutor/AgentExecutor、固定 Runtime/Pack identity。

Recipe 不获得 JobStore 原始连接、Publisher、更新管理器或任意文件系统。执行接口不强制所有 Recipe 使用相同 stage 数、分块、Prompt 或恢复粒度。

Gate：fake Recipe 权限/lifecycle/crash tests 通过；Fake 只证明编排，不作为产品级多 Recipe验收。

## 8. 历史 Task RX-05：以 facade 迁移 Video，不重写 compiler（X0-A 以 implementation-worktree tasks 为准）

1. 为现有 VideoService 提供最小 Descriptor/RecipeEndpoint submit facade；
2. `AllToNoteSDK.submit_video` 与 Runtime `submit_video` 暂时保留兼容签名，但内部只构造 ProduceRequest 并委托 ProduceService；
3. CLI 增加 Registry 驱动的 `recipe list/describe` 与 generic `produce --recipe`/`produce --request`；`produce video` 只做参数翻译，不新增独立 `run` 或 `add`；
4. 对等的 legacy 与 generic Video submit 生成相同 canonical Video request、hash 和 Job identity；
5. 现有 Job ID、checkpoint、Draft/Bundle、JSON/exit code 兼容行为不变；
6. 长视频/多 Draft/faithful/Portable/iwiki/零重放全回归；
7. 基础 Runtime 命令不加载 FFmpeg/Whisper/yt-dlp 或 Video heavy adapter。

若迁移要求重写长视频 Pipeline、重新定义 Portable 数据或改变已提交 Job 语义，立即缩小抽象并回退本任务改动。

## 9. 历史 Task RX-06：清除通用层的 Video 反向依赖（除 JobState 外后移 X0-B）

X0-A 只归位唯一 `JobState` 定义并保持 legacy re-export 的类型身份。JobSnapshot、result query/codec、Repository port、atomic commit、Bundle 真实共享边界及 generic reconnect 均由真实 Document/PPT 纵切与 X0-B 联合实现；不得从本节直接开工。原 RX-06 的 Repository/commit 改造要求已从 X0-A 活跃范围移除。

## 10. 历史 Task RX-07：Artifact/Evidence、错误与事件扩展（延后 X0-B/Artifact Wave）

以下扩展不属于 X0-A；由真实第二消费者和 Artifact/Review/Publisher Gate 重新证明后才可实施：

- namespaced Artifact kind/locator validator 与 opaque forward compatibility；
- 先保留现有 Video locator，Document/Web locator 随其真实实现加入；
- Recipe error code 投影到通用 category，入口共享同一 error envelope；
- 事件只携带 Job/stage/progress/artifact/quality 元数据，不携带正文、Prompt、Secret 或 Provider raw；
- 未知扩展可验证、复制，不被 Core 删除或改写。

Gate：forward compatibility、redaction、CLI/Desktop/MCP 投影和恶意 extension tests 通过。

## 11. X0-B：Document/PPT 真实第二消费者验证

X0-B 必须在 X0-A 通过后，由 Document/PPT 的一个最小真实纵切共同驱动。首个纵切只选择**一个**真实本地 born-digital PDF 或原生 PPTX，不同时实现两种格式，不扩展 OCR、Vision、旧格式、URL、多文件或长文档全矩阵：

- 注册真实 Document/PPT endpoint，并经同一 ProduceService 提交；
- 提取并规范化真实文件，产出可用 Markdown Draft 与 page 或 slide Evidence；
- 测试可使用 deterministic fake model，但 parser、文件身份、Artifact、Job/result、commit 与恢复链路必须是真实实现；Fake Recipe 或 fake-only fixture 不能作为验收；
- 只提升 Video 与该真实消费者共同需要的 result envelope、Artifact manifest/reference、Recipe/result discriminator、durable result query 和 source/evidence identity；
- page/slide/bbox/Notes 等领域字段留在 namespaced Artifact/Evidence，不进入 Core 必填字段；
- 同步完成 generic Job execution runtime factory：reconnect/wait 根据已持久化 Recipe/Pack/Runtime identity resolve，不能硬编码 Video factory，也不能重新 submit 创建第二个 Job。

X0-B Gate：

1. legacy Video result **dual-read**；旧成功与旧未完成 Job 都可查询，后者可恢复；
2. schema migration 有真实旧库 fixture，可重复执行、可重开；旧 Runtime 回读或明确回滚路径被验证；
3. source identity、Artifact/portable commit、result 与 Job terminal transition 保持单一可证明的**原子提交协议**，故障注入不得暴露半提交；
4. generic reconnect 按 persisted identity 恢复 exact Recipe/Pack/Runtime，缺失版本 fail closed，不存在 Video-only job wait；
5. Video 与真实 Document/PPT 在提交前后 crash/reopen 均不重复有效提取、模型或 commit 副作用，outcome unknown 可 reconcile；
6. 通用 Job/Repository 无 Recipe-specific import，每个公共字段均有两个真实消费者；
7. migration、Repository/atomic commit 与 Engine migration 不并行编辑；首个纵切不宣称支持全部 PDF/PPT/OCR。

Gate 通过前不得冻结 internal v1，更不得以 Fake Recipe 通过代替真实消费者证据。

## 12. Task RX-09：Article/Wiki 第三消费者与 internal v1

实现 `REC-WEB-001` W0/W1 最小纵切，验证 canonical URL、snapshot、freshness、段落 Evidence 和 capture/credential preflight。记录 Video/Document 塑造但 Web 不需要的字段，并只做兼容收窄或 extension 化。

Video、Document、Article/Wiki 全部通过同一 Registry/ProduceService 后，才将内部合同标为 v1 frozen，并记录字段消费者、兼容策略和弃用流程。

Gate：三类真实 Recipe 回归、恢复、Portable/iwiki、CLI/JSON、安全和性能基线通过。

## 13. Task RX-10：公共 SDK Go/No-Go

internal v1 不等于公共插件 SDK。按设计升级门单独评估：Video/Web/Document 真实生产、至少一个 Codebase/UE5 Agent Recipe 原型、外部实现者、安全沙箱/签名/供应链、兼容承诺、卸载与真实生态需求。

默认结论为 `continue internal`。不得因 Registry 和三个官方 Recipe 完成就开放任意第三方 Python 加载。

## 14. 分层 Gate

### Gate X0-A：最小控制面兼容接缝

- 以 implementation-worktree `recipe-x0-compatibility-extraction/spec.md` 与 `tasks.md` 为唯一执行依据；
- Video 全回归零语义 diff；
- 薄 ProduceService、静态 Registry 和最小 submission DTO 已建立；
- legacy 与 generic Video `produce` 等价；
- `submit_video` 仅为兼容 facade；
- 不含 `add`、独立 `run`、完整 Plan/Result、Repository/atomic commit 泛化、schema migration、Engine 或公共 SDK。

### Gate X0-B：真实 Document/PPT 合入

- Video + 一个最小真实 PDF 或 PPTX 纵切共享 JobStore、Checkpoint、Artifact/portable commit；
- page/slide/OCR 不泄漏为通用必填字段；
- legacy result dual-read、可重复/可重开 migration、旧 Job 查询/恢复与回滚 oracle 通过；
- source identity、Artifact commit、result 与 terminal transition 原子；
- generic reconnect 按 persisted Recipe/Pack/Runtime identity resolve，不再存在 Video-only job wait；
- 两个 Recipe crash/reopen 不重复昂贵副作用；
- Fake 不作为第二消费者验收，且本 Gate 不扩展到全格式或 OCR。

### Gate X2：冻结 internal v1

- Video + Document + Article/Wiki 真实纵切通过；
- 通用字段有至少两个真实消费者；
- 入口归一化、错误、事件、版本、恢复和 portable contracts 有 golden；
- 破坏性变化、兼容窗口和 migration 已记录。

### Gate X3：公共 SDK

必须另行通过生态、安全、供应链和长期兼容 Gate；未通过即保持官方 internal Registry。

## 15. 验证与回退

每个任务均执行：定向测试、受影响 Video 回归、architecture/import tests、redaction checks 和 `git diff --check`。X0-A 结束及 X0-B/X2 结束时执行完整测试与真实可复核 fixture；Fake 不冒充生产验收。

回退单位是 facade/Registry 接线，不回滚或改写已提交 Bundle/Job。兼容入口可以临时继续委托旧 VideoService，但不得在失败时复制一条新 Pipeline。任何需要修改 iwiki 已发布合同、迁移用户 Job/Vault、读取 Secret、执行 Git 发布动作或扩大到公共插件生态的工作必须单独获得授权。
