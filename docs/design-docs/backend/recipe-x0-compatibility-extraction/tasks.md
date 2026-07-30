# Recipe X0-A 最小兼容接缝任务清单

> 来源：本目录 spec.md 修订版；替代旧 Recipe Extension 实施计划中的 RX-01、RX-03、RX-05、RX-06 和旧 X0 Gate
> 可执行任务：8
> 执行规则：一次只完成一个 Task；验证并汇报后再进入下一 Task
> 目标：入口解耦与 Video 零语义变化，不把 X0-A 扩成持久化重写或并发 Engine

## 0. 复杂度与工作量

| 维度 | 评估 | 说明 |
|---|---|---|
| 算法难度 | 低 | 不修改下载、转写、编译、质量算法 |
| 架构难度 | 中高 | SDK、Runtime、CLI 的依赖方向变化 |
| 兼容难度 | 高 | request JSON、两套 hash、config snapshot、CLI golden 必须不变 |
| 持久化风险 | 中 | 只做 characterization，不重构 result_json/atomic commit |
| 预计变更 | 900 至 1,600 行，含测试 | 原计划为 1,900 至 3,300 行 |
| 总体评级 | 中等规模、高兼容敏感度 | 不是小改名，也不是中大型数据面重构 |

## 1. 全局不变量

所有 Task 必须遵守：

1. 不修改 SQLite schema、user_version 或 legacy request/result wire；
2. 不重写 Video compiler、Checkpoint、Portable/iwiki 或 atomic commit；
3. 不改变无版本 produce video 的 v1 默认；
4. 不新增线程池、daemon、Engine、worker protocol、DAG 或动态插件；
5. 不实现 add 或独立 run 动词；
6. 不冻结 PreflightReport、RecipePlan、RecipeOutput、ProduceResult；
7. 不覆盖、重建、清理当前工作树中的既有修改；
8. 每个生产代码 Task 尽量控制在约 200 行改动；超过时先缩小边界；
9. 所有兼容 facade 必须委托同一路由，不保留第二套业务语义；
10. X0-A 不宣称完成 Job/Repository/Bundle 去 Video 化或多 Job 高并发。

## Task 1: [x] 冻结兼容与当前执行能力基线

- 风险：中
- 预计生产代码：0 行
- 目标：先锁定现有行为，避免后续把兼容回归误判为“通用化差异”。
- 主要文件：
  - backend/tests/core/test_video_request_persistence.py
  - backend/tests/adapters/test_sqlite_job_repository.py
  - backend/tests/contracts/test_cli_envelope_golden.py
  - backend/tests/integration/test_fake_video_producer.py
  - 必要时新增聚焦的 X0 compatibility test

验收：

- [x] 固定 legacy SDK submit；
- [x] 固定 Video v1/v2 canonical request；
- [x] 明确区分 Job request hash 与 Video/checkpoint hash；
- [x] 固定 config snapshot；
- [x] 固定 legacy raw result round-trip；
- [x] 固定 crash/reopen、historical candidate 和 zero replay；
- [x] 固定 Portable manifest/bundle identity 与 source conflict rollback；
- [x] 固定 CLI JSON/Human envelope、warning、exit code、wait 和 Ctrl+C/cancel；
- [x] characterization 明确同一 Runtime 当前完整执行串行；
- [x] characterization 明确同 Workspace 第二 scheduler owner 返回 scheduler_busy；
- [x] characterization 明确 submit 不 wait 只创建 queued Job，后续 job wait 可接管；
- [x] 不修改生产代码。

Stop condition：

- 无法在锁定依赖环境重现已知 baseline；
- 测试暴露当前数据已不满足已有 request/result wire；
- 必须修改生产代码才能“让旧测试通过”。

## Task 2: [x] 只归位 JobState 所有权

- 风险：中
- 预计生产代码：40 至 100 行
- 目标：把无争议的 Job 状态从 Video domain 移到通用 Job domain，同时保持对象身份。
- 主要文件：
  - backend/app/core/jobs/model.py
  - backend/app/core/domain/video.py
  - backend/app/core/jobs/state_machine.py
  - 仅确实使用 JobState 的通用模块
  - 对应 Job state tests

验收：

- [x] 只有一份 JobState Enum 定义；
- [x] domain.video re-export 的 JobState 与新通用 import 是同一类型对象；
- [x] 所有 is JobState.* 判断保持正确；
- [x] 状态转换集合和持久化字符串不变；
- [x] 不迁移携带 VideoProduceResult 的 JobSnapshot；
- [x] 不引入通用 result DTO；
- [x] Job submit/get/wait/cancel/retry 的相关测试通过。

Stop condition：

- 需要复制第二份 Enum；
- 必须同时重构 result_json、JobSnapshot 或 Repository 才能完成；
- 迁移造成状态 wire 变化。

## Task 3: [x] 建立最小 Recipe submission 合同

- 风险：中
- 预计生产代码：100 至 180 行
- 目标：只定义 X0-A 真正使用的调用 envelope。
- 主要文件：
  - 新增 backend/app/core/recipes/contracts.py
  - 新增 backend/tests/core/test_recipe_contracts.py

只允许实现：

- [x] RecipeKey；
- [x] RecipeDescriptor；
- [x] InputDescriptor；
- [x] ProduceRequest；
- [x] ProduceSubmission；
- [x] RecipeEndpoint.submit Protocol；
- [x] 最小 JSON-safe、不可变和版本校验。

验收：

- [x] 合同模块不导入 Video、Document、Web 或 Codebase 类型；
- [x] 不包含 Transcript、timeline、page、bbox、provider、transcriber 等单 Recipe 必填字段；
- [x] parameters 由 Recipe 拥有并以不可变 JSON-safe mapping 表达；
- [x] durable request 只接受 secret reference，不以字符串启发式猜测 Secret；
- [x] 未知 contract version、空 Recipe key 和非法 input fail closed；
- [x] 没有 PreflightReport、RecipePlan、RecipeOutput 或 ProduceResult；
- [x] 合同明确标记 internal、可演进，不承诺第三方 ABI。

Stop condition：

- 为了 Fake Recipe 增加未被 Video 调用路径使用的字段；
- 开始设计 capability/resource/publisher/detach 大而全 descriptor；
- 开始设计通用 stage、DAG 或完整 Agent plan。

## Task 4: [x] 实现静态 Registry 与薄 ProduceService

- 风险：中
- 预计生产代码：150 至 220 行
- 目标：建立唯一、轻量、进程内的 Recipe resolve 与 submit 路由。
- 主要文件：
  - 新增 backend/app/core/recipes/registry.py
  - 新增 backend/app/core/application/produce_service.py
  - 新增对应单元测试

Registry 验收：

- [x] 组合根显式注册；
- [x] 构建后只读；
- [x] list 顺序稳定；
- [x] describe/resolve 对非法 selector、未知版本、重复 key 返回稳定错误；
- [x] 不扫描目录或 entry point；
- [x] 不动态下载或导入远端代码；
- [x] descriptor 查询不实例化 VideoService 或重型 Pack；
- [x] 不为几个官方 Recipe 编写无价值微基准。

ProduceService 验收：

- [x] 只做通用 envelope 校验、Recipe 固定、resolve、submit 和 submission 返回；
- [x] 不导入 Video；
- [x] 不保存当前活动 Job、Workspace 或 Recipe 的全局可变状态；
- [x] submit 不执行下载、模型、转写、Agent 或 wait；
- [x] submit 不创建后台线程；
- [x] 不实现 preflight-plan-output-result 编排；
- [x] 不包装第二套 Video-specific 错误层。

Stop condition：

- 需要 Service Locator 或全局可变 Registry；
- ProduceService 开始拥有 Recipe 领域 Pipeline；
- 为未来并发加入 worker、queue 或 ResourceRequirement DSL。

## Task 5: [x] 用兼容 Adapter 注册 Video Recipe

- 风险：高
- 预计生产代码：120 至 200 行
- 目标：把通用 ProduceRequest 翻译为现有 VideoProduceRequest，并继续委托现有 VideoService。
- 主要文件：
  - 新增 backend/app/core/recipes/video/adapter.py
  - 必要时新增轻量 Video descriptor 模块
  - 新增 backend/tests/core/test_video_recipe_adapter.py
  - 原 VideoService 只允许极小接线改动

验收：

- [x] Video descriptor 可在冷路径读取；
- [x] v1/v2 所有现有字段确定性转换；
- [x] 同一 client request identity 下 generic 与 legacy submit 得到相同 Job ID；
- [x] request JSON、Job hash、Video/checkpoint hash 和 config snapshot 不变；
- [x] requested outputs、output bindings、language policy 和 principal 不变；
- [x] Adapter 不绕过 VideoService 直接调用 JobService；
- [x] Adapter 不重写 acquisition、transcription、compiler、quality、Checkpoint 或 Portable；
- [x] 无版本 legacy produce video 仍映射 v1；
- [x] 显式 v2 generic 与显式 v2 legacy 路径等价。

Stop condition：

- 需要修改 legacy request schema；
- 需要改变两套 hash；
- 需要移动 Job 创建或 config snapshot 所有权；
- 需要修改 SQLite schema、result wire 或 atomic commit。

## Task 6: [x] 改造 SDK 与 Runtime 组合根

- 风险：高
- 预计生产代码：100 至 180 行
- 目标：公共生产入口依赖 ProduceService，保留 legacy facade。
- 主要文件：
  - backend/app/core/sdk.py
  - backend/app/runtime.py
  - backend/tests/cli/test_runtime_bootstrap.py
  - backend/tests/runtime/test_runtime_paths.py

验收：

- [x] 组合方向为 VideoService -> VideoRecipeAdapter -> Registry -> ProduceService -> SDK -> Runtime；
- [x] SDK/Runtime 暴露通用 submit；
- [x] submit_video 保留原签名和行为，但内部委托 ProduceService；
- [x] 不保留并行的旧 Video 业务路径；
- [x] 所有现有 factory 名称和参数不变；
- [x] workspace/machine root 解析不变；
- [x] reopen、Job query/wait/cancel 的兼容测试通过；
- [x] Runtime 基础 import 不 eager load 未使用重型 Pack。

Stop condition：

- 为通用入口新建第二个 JobStore 或 Runtime；
- factory 兼容必须通过静默改默认路径实现；
- 组合根开始拥有 Recipe 业务判断。

## Task 7: [x] 收敛为单一 produce CLI 心智模型

- 风险：高
- 预计生产代码：160 至 260 行
- 目标：让 legacy 与 generic CLI 只做请求适配并进入同一 ProduceService。
- 主要文件：
  - backend/app/cli/main.py，或新增聚焦的 production command 模块
  - backend/tests/cli/test_produce_video_cli.py
  - backend/tests/cli/test_recipe_cli.py
  - backend/tests/contracts/test_cli_envelope_golden.py
  - backend/tests/helpers/report_cli_imports.py

实现范围：

- [x] 保留 alltonote produce video；
- [x] 增加 alltonote produce <input> --recipe <id>@<version>；
- [x] 增加 alltonote produce --request <request.json>；
- [x] 增加 recipe list/describe 作为高级发现入口；
- [x] 不新增独立 run；
- [x] 不新增 add。

验收：

- [x] legacy produce video 参数、warning、JSON/Human output、exit code 和 Ctrl+C/cancel 不变；
- [x] 无版本 produce video 仍为 v1；
- [x] 显式 v2 legacy 与 generic produce 生成相同 Video request、Job identity 和结果；
- [x] request 文件未知 contract/字段、非法 selector 和明文 Secret fail closed；
- [x] recipe list/describe stdout 保持稳定 envelope；
- [x] recipe list/describe 不导入 Runtime、Downloader、Whisper、Torch、FFmpeg、Model client 或 FastAPI；
- [x] 普通 Video 用户不需要理解 Recipe ID 或 request JSON；
- [x] CLI handler 不复制 wait、Job query 或结果渲染 Pipeline。

Stop condition：

- 为 add 自动识别引入 MIME/网络/认证探测；
- 为 run 新建第二套语义相同的命令；
- 为保持 golden 而复制一整条旧业务路径。

## Task 8: [x] 执行 X0-A 架构、兼容与冷路径 Gate

- 风险：高
- 预计生产代码：0 至 80 行，仅修复本阶段暴露的缺陷
- 目标：证明最小接缝完成，同时避免虚假宣称数据面或高并发已完成。
- 主要文件：
  - architecture/import tests
  - X0-A 直接相关测试
  - 本 tasks.md 完成状态

架构验收：

- [x] contracts、registry、produce_service 不导入 Video；
- [x] ProduceService/Recipe 路径不导入旧 task_serial_executor；
- [x] X0-A 未新增线程池、daemon、Engine、DAG、动态插件或 DB schema；
- [x] Registry/ProduceService 无新的全局锁或活动 Job 状态；
- [x] Job/Repository/Bundle 剩余 Video 耦合已登记为 X0-B，不被隐藏。

兼容验收：

- [x] Task 1 全部 characterization 通过；
- [x] generic 与 legacy Video submit 等价；
- [x] CLI golden 通过；
- [x] Job、SQLite、恢复、Portable/iwiki 定向回归通过；
- [x] 全量 backend pytest 通过，或只剩实施前登记且可复现的环境基线；
- [x] git diff --check 通过；
- [x] 每个改动文件都能直接追溯到 X0-A；
- [x] 没有覆盖当前工作树中的用户修改。

声明验收：

- [x] 文档和发布说明明确 X0-A 不提供同 Workspace 多 Job 并发；
- [x] 文档和发布说明明确 X0-A 不提供 detach/Engine/AgentExecutor；
- [x] 文档和发布说明明确 X0-B 才完成 Job/Repository 数据面去 Video 化；
- [x] 文档和发布说明明确第一个真实非 Video Recipe 才能验证完整扩展合同。

## 2. X0-B 延后项，不得从本任务清单直接开工

以下工作最终必要，但在没有真实 Document/PPT 消费者 fixture 前不得凭 Video 猜测实现：

1. 通用 JobSnapshot 和 result projection；
2. result discriminator、opaque record 或 codec 边界；
3. legacy Video result dual-read；
4. atomic commit port 去 Video 化；
5. 多 SourceRevision、partial success、no-op 和多 primary Artifact；
6. Video + Document 共享 Artifact manifest；
7. Bundle assembler 真正通用部分；
8. 通用 Job/Repository 的完整 Recipe import Gate。

开启 X0-B 的必要前置：

- [ ] 已选择第一个真实非 Video Recipe；
- [ ] 有真实输入、失败和结果 fixture；
- [ ] 已证明不能只用现有 Artifact/Portable port 完成；
- [ ] 所有新增公共字段至少有 Video 与该 Recipe 两个真实消费者；
- [ ] migration、legacy dual-read 和 rollback oracle 已写入独立 spec/tasks。

## 3. Parallel Production 延后项

以下工作属于独立运行时阶段，不得夹带进 X0-A：

1. Codex client 并发重入正确性；
2. patched SQLite 打包版本；
3. on-demand Engine；
4. scheduler leadership 与 per-job claim 分离；
5. 多进程 worker 和 process tree supervision；
6. CPU、内存、GPU、Provider、Agent slot、workspace commit 资源准入；
7. detach、batch、max-parallel 和重连；
8. AgentExecutor；
9. 4、8、16 worker 负载与故障注入。

具体任务见 docs/design-docs/backend/local-parallel-production。

## 4. 全局 Stop Conditions

任一 Task 出现以下情况，停止并重新评估：

- 必须修改 SQLite schema、legacy request/result JSON 或 Bundle schema；
- 必须重写 Video compiler、Checkpoint 或 Portable/iwiki；
- 无法保持无版本 produce video 的 v1 行为；
- legacy Job 无法读取、恢复或由旧 Runtime 回读；
- 为通过抽取而引入动态加载、全局 Service Locator、DAG 或公共插件 SDK；
- 单 Task 明显超过约 200 行生产代码且不是机械兼容迁移；
- X0-A 开始承担 Engine、并发调度或 AgentExecutor；
- 需要覆盖、重建或丢弃当前工作树中的既有修改。
