# AllToNote Runtime/CLI Wave 1A 验收与基线审计

```yaml
doc_type: acceptance
status: completed
authority: evidence
upstream:
  - ../superpowers/specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - ../superpowers/plans/2026-07-18-alltonote-runtime-cli-feature-pack-implementation-plan.md
  - ../superpowers/specs/2026-07-14-alltonote-video-producer-design.md
  - ../superpowers/plans/2026-07-18-alltonote-video-release-implementation-plan.md
implementation_status: wave-1a-rcp-00-through-07-vrel-00-01-accepted
last_verified_at: 2026-07-18
```

## 1. 工作树与 Gate 0

- 文档工作树：`G:\AllToNote`，分支 `master`，存在用户未提交和未跟踪文档成果；本 Goal 不清理或覆盖。
- 实现工作树：`G:\AllToNote-video-producer`，分支 `codex/alltonote-video-producer`，存在用户未提交和未跟踪 Video/Compiler 代码与测试；本 Goal 在其上做局部兼容接线。
- 完整基线：`1731 passed, 2 skipped, 3 warnings, 3 subtests passed in 69.11s`。
- Gate 0 命令墙钟：`70.716s`。
- `git diff --check`：退出码 0；只有现有 LF 到 CRLF 的工作副本提示，无 whitespace error。
- pytest warning：两个 smoke marker warning 和 `ctranslate2/pkg_resources` deprecation；均未导致失败。

## 2. 当前 CLI 行为

当前公开命令只有：

```text
alltonote version [--json]
alltonote produce video <input> --workspace <path> [--wait] [--json]
```

当前事实：

- `version` 人类模式输出裸版本号；JSON 模式输出一行 JSON。
- `produce video` 不论是否传 `--json` 都输出 JSON；尚无独立人类 renderer。
- `--wait` 等待终态；不传时返回 durable queued Job 投影。
- v1 默认仍是 `alltonote.video-course-note@1`；显式 v2、多输出或翻译型高保真请求冻结为 `alltonote.video-producer@2`。
- 成功结果保留 primary Draft 投影，并可附加 `documents`；现有 v1/v2、`requested_outputs` 和 Faithful language policy 已有测试。
- stdout 当前是一行协议结果；stderr 当前为空。Argparse 自身的 usage error 仍使用 argparse stderr/exit 2，尚未进入统一 Application Result。
- CLI 直接调用 Runtime/Application，不启动 FastAPI，也不调用旧 `NoteGenerator`。

## 3. 已发布 Automation Protocol 与设计漂移

当前实现、已有 CLI golden 和 `REC-VIDEO-001` 一致使用：

```text
alltonote_cli_protocol_version = 1
exit codes = 0 / 2 / 10 / 20 / 30 / 40 / 50 / 60 / 70 / 130
```

`RUNTIME-001` 的示例和 Runtime plan 另写为 `api_version` 与 `0/2/3...10`。覆盖矩阵没有为两组字段/退出码建立替代关系，ADR-0001 与该问题无关。

Wave 1A 的兼容策略：

- 不删除、不改名或改变现有 `alltonote_cli_protocol_version`、既有 error category 或退出码语义；
- 新 Runtime/Job/inspect 命令复用现有 Automation Protocol；
- 新字段只做向后兼容的可选扩展；
- Runtime 设计中的另一组示例记录为设计漂移，不用当前代码静默覆盖文档，也不以破坏已发布 Video CLI 的方式“统一”；
- 若后续必须删除字段或改变退出码语义，需单独主协议升级和用户决策，不属于本 Goal。

## 4. 已有能力

- Durable Job submit/get、状态机、Attempt、checkpoint、scheduler lease、fencing、cancel、PendingChallenge/respond、retry-as-new-Job 和 ExternalOperation unknown 语义已经存在于 Application/Repository。
- SQLite Repository 已保存 Job request/result/error/event、Portable commit result、lineage 和 external operation。
- Video v1/v2、Knowledge Note、Faithful Edition、requested outputs、primary projection、Portable Bundle v1/v2、iwiki validate/commit 与 restart zero replay 已有回归。
- Runtime config 已有 typed model、严格字段、固定 allowlist 环境变量、原子写和普通配置 Secret 拒绝。
- Credential 已有 keyring broker、ephemeral 环境变量和仅含 metadata 的 profile catalog。
- Workspace instance registry 已将 Video JobStore 放到机器本地 workspace instance root。
- CLI version 冷路径已有“不导入 FastAPI/Whisper/Torch”测试。

## 5. 重复或分散实现

- CLI success/error/version envelope 在 `app/cli/main.py` 多处手工构造。
- config path、credential catalog path 和 Video machine root 分别解析平台目录，没有唯一 Runtime path service。
- Job read projection 存在于 `JobService.get`，但 Runtime/SDK 只暴露单个 get，CLI 尚无 list/events/wait/cancel/respond/retry 公共命令。
- Video result 的 CLI 投影写在 handler 内，不能供 Job get/wait 或未来 Desktop/MCP 复用同一 Application Result。
- ErrorCategory 只有 Video-era 八类；不能完整解释 capability、credential、external platform、outcome unknown 和 artifact/JobStore failure。

## 6. 缺失能力

- `runtime info`、`runtime capabilities` 及稳定 version/capability projection；
- 唯一 config/data/cache/state/log path service 和 Vault-outside invariant check；
- user/machine/Recipe/Job snapshot 的明确配置分域与恢复漂移解释；
- Credential missing/expired/denied/unsupported 的完整产品状态和 CLI status；
- `job get/list/wait/events/cancel/respond/retry`；
- `artifact inspect`、`draft inspect`；
- 人类模式与 JSON 模式共享同一 Application Result；
- 全局 stdout/stderr policy、统一 error mapping、路径/Prompt/provider raw/Secret redaction；
- Runtime/CLI benchmark 与本 Goal 的安全验收摘要。

## 7. 兼容约束

- 不修改 iwiki published Workspace/Schema/Validator/commit/publish/index 合同。
- MachineState、JobStore、Attempt、checkpoint、lease、log 和运行缓存不得进入 Vault。
- 不建立第二套 CLI/Runtime/Video Pipeline。
- 不改变 Video request schema v1/v2、默认 Recipe、规范化 `requested_outputs`、faithful language policy、request hash 或 compiler/checkpoint identity。
- 不复活终态 Job；retry 必须创建新 Job并保留 lineage。
- outcome unknown 不自动重复可能付费或有副作用的调用。
- inspect 只读、无发布/写入副作用，不默认返回私人正文或绝对路径。
- 现有大量未提交长视频实现和测试必须原样保留并继续通过。

## 8. Wave 1A 计划写集

实现中只计划触碰以下现有文件；若发现新增目标，先在本文记录原因：

- `backend/app/cli/main.py`
- `backend/app/core/errors.py`
- `backend/app/core/sdk.py`
- `backend/app/runtime.py`
- `backend/app/core/config/model.py`
- `backend/app/core/config/loader.py`
- `backend/app/adapters/credentials/keyring_broker.py`
- `backend/app/adapters/credentials/profile_catalog.py`
- `backend/app/core/ports/jobs.py`
- `backend/app/adapters/jobs/sqlite_repository.py`
- `backend/pyproject.toml`（仅在必要依赖/测试配置确有变化时）

计划新增的最小模块：

- `backend/app/cli/contracts.py`
- `backend/app/cli/errors.py`
- `backend/app/cli/render.py`
- `backend/app/cli/commands/runtime.py`
- `backend/app/cli/commands/jobs.py`
- `backend/app/cli/commands/inspect.py`
- `backend/app/runtime_paths.py`
- `backend/app/runtime_config.py`
- `backend/app/runtime_info.py`
- `backend/app/runtime_capabilities.py`
- `backend/app/job_runtime.py`
- `backend/app/core/application/job_query_service.py`
- `backend/app/core/application/artifact_query_service.py`
- `backend/app/core/config/events.py`
- `backend/app/core/ports/job_queries.py`

测试写集：

- `backend/tests/contracts/`
- `backend/tests/cli/test_produce_video_cli.py`
- `backend/tests/cli/test_runtime_bootstrap.py`
- `backend/tests/cli/test_job_cli.py`
- `backend/tests/cli/test_artifact_cli.py`
- `backend/tests/runtime/test_runtime_paths.py`
- `backend/tests/runtime/test_runtime_config.py`
- `backend/tests/runtime/test_runtime_info.py`
- `backend/tests/runtime/test_job_query_service.py`
- 受影响的现有 Job/Video/Portable contract tests（只增加必要回归，不重写已有覆盖）。

文档写集：

- 本文；
- `docs/tasks/alltonote-master-tasks.md`；
- `docs/tasks/alltonote-design-coverage-matrix.md`；
- `docs/superpowers/plans/2026-07-18-alltonote-runtime-cli-feature-pack-implementation-plan.md` 的实施证据；
- 最终 acceptance/CLI 示例所需的同一份证据文档，不建立第二份总进度。

`backend/app/runtime.py` 已是大型 composition root，因此本 Goal 不把它批量移动为 `app/runtime/` package；平台路径等新职责使用小型平级模块，避免覆盖现有未提交 Video 编译成果。

RCP-06 增加 `app/job_runtime.py`，原因是 Job get/list/events/cancel/respond/retry 不应为了只读/状态控制导入完整 Video 执行 composition root；`core/ports/job_queries.py` 是独立只读 Port，避免扩大已由架构测试冻结的执行型 `JobRepositoryPort`。

最终实现没有新增单用途 `commands/runtime.py`；轻量 Runtime command projection 保留在现有 CLI composition 中。计划中的 `commands/inspect.py` 按公共命令名落为 `commands/artifacts.py`，并新增只读边界 `core/ports/portable_queries.py`。RCP-07 因必须复用已发布 Validator 而局部扩展现有 `adapters/iwiki/portable_gateway.py`；VREL-01 为前台取消接缝局部扩展 `video_service.py`、`core/sdk.py` 和 `runtime.py`。这些新增写点均直接对应本 Goal，没有改动 iwiki schema/validator/commit 合同。

## 9. RCP-00 Golden

新增 contract golden 冻结：

- `version --json`；
- durable Job queued 投影；
- committed Portable Job result、primary projection 与 additive `documents`；
- capability failure 的安全 error envelope 和退出码。

现有 Portable/iwiki schema、Bundle、commit 和 Video integration contract tests继续作为磁盘合同 Golden，不复制第二套私有 Bundle fixture。

## 10. RCP-01 / RCP-02 实施证据

- RCP-01：统一 `ApplicationResult`、JSON/human renderer、稳定退出码与错误映射；现有 Video Automation Protocol v1 保持不变，仅增加向后兼容字段。
- RCP-01 定向 Gate：`653 passed in 26.57s`；stdout 严格单 JSON、human stdout/stderr 分流和 Secret/Prompt/Provider raw/绝对路径 canary 均有回归。
- RCP-02：新增唯一 `RuntimePaths`，配置、credential catalog、Workspace Registry 与 Runtime composition root 共用；默认 Windows/macOS 路径继续由 `platformdirs` 决定，不移动现有用户数据。
- RCP-02 Gate：`142 passed in 4.18s`。覆盖中文与空格、只读兼容、复制 Vault 不携带 live Job、删除机器 cache 不改变 Job/Bundle、同 Vault 的两个机器状态根不共享 lease，以及路径重叠时首次写入前 fail closed。
- `runtime paths --json` 默认只返回逻辑角色与脱敏路径；只有显式 `--show-paths` 才返回本机绝对路径。

## 11. RCP-03 实施证据

- 有效配置优先级固定为 built-in `<` user TOML `<` explicit profile `<` allowlisted env `<` CLI flags；任意 `ALLTONOTE_*` 和 ephemeral Credential 环境变量不进入普通配置合并。
- `config get|set|validate|profiles` 复用同一版本化 loader/service；写入使用 lock、临时文件、fsync 和 atomic replace，权限失败返回稳定 `config_write_failed`，不覆盖原文件。
- 有效配置生成无 Secret 的完整 digest 与 semantic digest；drift 分类为 `none`、`non-semantic`、`semantic`。
- Job 配置快照不修改已冻结 Video request JSON/hash，而是作为 `configuration.snapshot.v1` 首事件与 Job 在同一 SQLite 事务创建；幂等重放若快照不同则冲突，敏感字段在落盘前拒绝。
- 恢复允许 log/UI 等 non-semantic drift；结果相关 semantic drift 在 Attempt、lease 与外部调用前返回 `effective_config_drift`，原 Job 保持 queued，要求创建新 Job。
- 受影响 Gate：`1615 passed in 58.22s`；包括 Core、Adapter、CLI、contract、Runtime、Fake Video、Platform Subtitle 和 Portable Bundle 回归。`git diff --check` 退出码 0，仅有既有 LF/CRLF 提示。

## 12. RCP-04 实施证据

- Credential CLI 提供 `credential set PROFILE --stdin`、`credential status PROFILE` 与 `credential delete PROFILE`；Secret 不允许作为位置参数，非交互环境必须使用 stdin，交互输入使用无回显读取。
- ephemeral 环境 Credential 继续优先于 keyring 且永不持久化；keyring 不可用时不降级为明文文件。
- 状态投影只返回 `present`、`validated`（未知时为 null）和 `last_checked_at`，不返回 Secret、provider raw response、后端异常文本或本机路径。
- 稳定错误细分为既有兼容的 `credential_missing`，以及 `credential_backend_unavailable`、`credential_backend_locked`、`credential_invalid`、`credential_input_required`；错误映射与退出码沿用 Automation Protocol v1。
- 定向 Gate：`87 passed`；计划筛选 Gate：`98 passed, 1674 deselected, 1 warning in 7.07s`。`git diff --check` 退出码 0，仅有既有 LF/CRLF 提示。

## 13. RCP-05 实施证据

- `runtime info` 返回固定版本、协议、schema、平台与静态能力投影；该路径不发网络请求、不启子进程，也不导入 FastAPI、Whisper、Torch 等重模块。
- `runtime capabilities` 区分静态安装能力与动态健康状态；`runtime doctor` 默认只做静态本地检查，只有显式 `--dynamic` 才执行有界的 Codex/FFmpeg 健康探测。
- `runtime-lock.json` 由唯一 loader 校验；Portable Gateway 复用同一校验逻辑，并保留既有测试注入点。版本或资源不匹配时在副作用前返回 `portable_contract_incompatible`。
- `desktop_api_versions` 当前诚实返回空列表；Wave 1A 不提前宣称未实现的 RCP-10 Desktop API。
- 冷启动基准以 9 个独立进程测量：`version` 中位数 `97.222 ms`、观测最大值 `113.528 ms`；`runtime info` 中位数 `154.341 ms`、观测最大值 `163.425 ms`，分别满足 `<150 ms` 与 `<300 ms` 目标。
- RCP-05 定向 Gate：`75 passed in 3.86s`；Portable Gateway/runtime-lock 定向 Gate：`63 passed in 3.38s`。
- RCP-05 广域 Gate：`1632 passed in 58.35s`；覆盖 Core、Adapter、CLI、contract、Runtime、Fake Video、Platform Subtitle 与 Portable Bundle。`git diff --check` 退出码 0，仅有既有 LF/CRLF 提示。

## 14. RCP-06 实施证据

- 新增 principal-scoped `JobQueryService` 与独立 read Port；CLI 不直接读取或写入 SQLite。`get/status`、`list`、`events`、`wait`、`cancel`、`respond`、`retry` 全部经 Application/Runtime facade 进入现有状态机。
- 保留既有 `job status` 名称，同时按 Runtime feature pack 增加兼容别名 `job get`；查询到 failed Job 时命令退出 0，failure 作为数据返回；`job wait` 观察到 failed/cancelled 终态时才映射相应非零退出码。
- `list` 默认 50、最大 200，cursor 绑定 state filter；10,000 Job 以 200 条/页完整遍历，无重复、无遗漏。`events` 默认 100、最大 1,000，支持 `after-sequence`，JSONL 继续使用既有 `event_schema_version=1`、单 Job 单调 sequence 合同。
- `job wait` 无 timeout 时继续承担 P0 无 daemon 的恢复执行；显式 timeout 只观察 durable 状态，不启动后台执行线程或隐式取消 Job。timeout 返回可重试 `job_wait_timeout`，Ctrl+C 返回 130，二者均保持 durable Job 的 cancellation flag 不变。
- `cancel`、`respond`、`retry` 均先做 Workspace/principal 授权。respond 只消费 state/challenge ID 匹配且声明 `kind` 的 waiting-for-input schema；`external_outcome_unknown` 不允许用任意响应绕过，必须人工对账、显式取消并在 retry request 中精确确认全部 operation ID。
- retry 始终原子创建新 Job 并保留 `retry_of_job_id`；终态原 Job 不复活。新 Job 的当前配置快照在同一事务写为首事件；同 client request ID 用不同配置快照重放时返回 `idempotency_conflict`。
- 成功 Job 缺 result、失败 Job 缺 error 或 result/error 与状态不一致时统一 fail closed 为 `job_projection_invalid`，不再把损坏终态误报成功。
- 默认查询路径仅凭 Workspace grant 定位机器本地 JobStore；终态 get/list/events/control 不加载 Codex 执行 Runtime。实测 `job get` 暖路径 30 次：中位数 `11.904 ms`、p95 `13.221 ms`、最大 `19.146 ms`，满足 `<100 ms` 目标。
- RCP-06 定向 Gate：Job CLI `18 passed`，10k pagination/event boundedness `2 passed`，CLI/contracts/runtime `78 passed`。广域 Gate：`1655 passed in 58.65s`；`git diff --check` 退出码 0，仅有既有 LF/CRLF 提示。

## 15. RCP-07 实施证据

- `artifact inspect` 只接受 `art_*` 或 `bnd_*`，`draft inspect` 只接受作为 Draft 的 `art_*`；任意路径、文件名和非 typed ID 在 Workspace 文件访问前拒绝。为兼容既有 Video Automation 设计，`artifact show` 与 `draft show` 作为同一实现的 additive alias 保留。
- CLI/Application 不读取 iwiki 私有文件。`ArtifactQueryService` 只调用 `PortableInspectionPort`；`IWikiPortableGateway` 以 `writable=False` 打开显式 Workspace grant，先调用已发布 `validate_bundle(..., SEMANTIC)`，再从已验证 committed Bundle 投影公开 manifest/receipt/payload。
- 默认返回 Bundle manifest hash、Artifact kind/hash/size、Recipe、compiler capability、父 Artifact/SourceRevision/Quality lineage、Source 的 opaque ID/kind/connector、Evidence set/transcript refs，以及 per-Draft 或 Bundle Quality/publish eligibility；不返回 canonical identity、URL、display metadata、executor/model identity、Prompt、Provider raw 或 payload path。
- Draft inspection 内部最多读取 8 MiB 已验证 UTF-8 Markdown，最多投影前 100 个 heading 和 100 个 Evidence ID，同时返回实际总数/截断标记。Bundle inventory 最多投影 200 个 Artifact；按 Artifact ID 查询最多扫描 10,000 个 committed Bundle。
- 正文默认不存在于结果中。只有显式 `--body-bytes N` 才返回 UTF-8-safe preview，`N` 必须为 `1..262144`；跨多字节字符截断不会生成无效 JSON。非文本 Artifact、过大 Draft、缺失、tamper/stale、contract mismatch 均使用稳定 fail-closed error。
- 真实 committed v1/v2 fixture 覆盖 Knowledge Note、Faithful Edition 双输出、hash/size/quality/source/evidence、正文边界、中文空格 Workspace、只读无副作用、tamper、missing、contract mismatch、usage identity 和兼容 alias。`test_artifact_cli.py` 最终为 `14 passed`；CLI/Portable/Bundle 定向组合曾为 `342 passed`，最终相关 Gate 见第 18 节。
- 暖进程 `artifact inspect` 30 次：中位数 `15.357 ms`、p95 `18.175 ms`、最大 `23.441 ms`。

## 16. VREL-00 / VREL-01 兼容收敛

### 16.1 VREL-00 冻结事实

- 实现分支保持 `codex/alltonote-video-producer`，审计时 HEAD 为 `32891d3 fix: allow repeated evidence references`；未清理、覆盖、stage 或提交既有 dirty worktree。
- 当前真实命令链只有一条：`CLI main -> create_codex_app_server_runtime_for_workspace -> create_platform_video_runtime -> VideoService -> checkpoint/compiler/PortableGateway`，不启动 FastAPI，也不经过旧 `NoteGenerator -> GPTFactory -> RequestChunker`。
- URL Source 的过渡调用链是 `LegacyVideoSourceAdapter -> lazy legacy Downloader`；平台字幕由 Adapter 规范化为 Core `TranscriptDocument`。本地媒体链使用 `_LocalVideoOperations -> TranscriptPort`，当前默认过渡实现为 `LegacyTranscriberAdapter -> lazy legacy transcriber`。这些 legacy 实现只位于 Adapter 边界，没有重新取得 Recipe/Job/Portable 所有权。
- `pyproject.toml`、`runtime-lock.json` 和 RuntimeInfo 均固定 `llm-iwiki==0.1.2`、Portable API 1、`iwiki-portable-contract-v1`、schema set `2026-07-portable-v1`、schema hash `sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9`。本地 editable metadata 的旧 0.1.0 requirement 已用无依赖、无 build isolation 的本地注册刷新；随后 `pip check` 为 `No broken requirements found.`
- 65 分钟真实 Transcript 的既有验收只引用公开输入身份和统计，私人 Transcript 正文/缓存路径未进入仓库。本 Goal 没有读取、复制或重新导入该缓存，也不把既有缓存编译冒充实时 YouTube acquisition。

### 16.2 VREL-01 CLI seam

- 规范命令收敛为 `alltonote produce video --input VALUE --workspace PATH`。原位置参数继续走同一 parser/handler/service，JSON `warnings` 明确返回 `Positional video input is deprecated; use --input`；同时提供两种输入会在创建 Job 前返回 usage error。
- canonical 与 legacy alias 规范化为相同 `VideoProduceRequest`，测试证明请求对象及 `VideoService._request_hash` 相同。v1 默认 Recipe/request schema、v2 `requested_outputs` 规范化、Faithful language policy、model/provider/transcriber profile refs 和配置快照保持不变。
- `--json` 继续使用 Automation Protocol v1 的同一 `ApplicationResult`，返回 Job/Bundle/manifest/commit、primary Draft、additive `documents`、per-document Quality 和 Artifact refs；没有第二条 Video projection。
- 前台 `produce --wait` 遇到 Ctrl+C 时，通过现有 `VideoService -> SDK -> Runtime` facade 请求结构化 Job cancellation，然后当前命令返回 130。`job wait` 的 Ctrl+C 仍只中断观察、不隐式取消，两个入口语义没有混淆。
- validation/capability/acquisition/transcription/model/quality/commit 继续使用统一 DomainError/exit mapping；终态 Job 仍不可复活，`job retry` 原子创建带 lineage 的新 Job；outcome unknown 仍要求精确人工确认。
- VREL 定向 Gate与 RCP-07 合并运行：Produce/Artifact CLI `40 passed`；Video CLI/contract、v1/v2 request persistence、Fake Video、Platform Subtitle、Portable Bundle 组合 `372 passed`；最终 Gate 见第 18 节。

## 17. VREL-00 发布矩阵

| 发布项 | 状态 | 本次可复核结论 |
|---|---|---|
| RCP-00..07 | `pass` | 本文第 9–15 节和最终 Gate |
| VREL-00 | `pass` | branch/status/log、调用链、runtime lock、缓存边界均已审计 |
| VREL-01 | `pass` | canonical CLI、alias、request hash、profile、Ctrl+C、错误/结果接缝均有测试 |
| VREL-02 Bilibili 真实字幕路径 | `not-run` | 明确不属于 Wave 1A；不得由平台 fixture 冒充真实发布验收 |
| VREL-03 本地视频 + faster-whisper | `not-run` | 明确不属于 Wave 1A；未安装系统工具或模型 |
| VREL-04 字幕/转写完整决策矩阵 | `not-run` | 后续 Video Goal |
| VREL-05 YouTube 实时 acquisition | `blocked` | 既有 anti-bot 外部阻塞；本 Goal 未读取 Cookie、未重试、未报成功 |
| VREL-06 短/中/长/Profile 发布矩阵 | `not-run` | 编译器回归通过，但完整发布矩阵属于后续 Goal |
| VREL-07 截图/多模态发布矩阵 | `not-run` | 后续 Video Goal |
| VREL-08 恢复/取消/故障注入 | `not-run` | Wave 1A 的取消、恢复、unknown、零重放子集已通过；完整发布任务后续执行 |
| VREL-09 Runtime/Pack 集成 | `not-run` | RCP-08+ Pack 明确不属于本 Goal |
| VREL-10 ReviewCandidate 接口 | `not-run` | Review/Publisher 明确不属于本 Goal |
| VREL-11 全回归与真实验收摘要 | `not-run` | Wave 1A 全回归通过，但 Bilibili/local/clean-machine 等完整真实发布前置未执行 |

## 18. Goal 最终 Gate

- Runtime/CLI/contract/runtime tests：`97 passed in 10.76s`。
- Job/Checkpoint/ExternalOperation/SQLite tests：`337 passed in 10.21s`。
- PortableGateway/Bundle/Video request/v1/v2/Platform Subtitle tests：`358 passed in 28.04s`。
- 完整 pytest：`1820 passed, 2 skipped, 3 warnings, 3 subtests passed in 65.97s`；命令墙钟 `66.95s`。两个 skip 是显式平台/真实 smoke；三个 warning 是既有的 Windows/macOS smoke marker 未注册提示，以及 `ctranslate2` 对 `pkg_resources` 的 deprecation。
- 全量 Gate 后仅做三处等价折行；随后 compiler/plan/quality/parser 定向回归为 `92 passed in 5.76s`。
- `git diff --check`：退出码 0，仅有既有 LF/CRLF 工作副本提示；没有 whitespace error。
- 本地依赖一致性：`pip check` 为 `No broken requirements found.`。
- CLI help 已实际检查 `produce video`、`artifact inspect`、`draft inspect`；canonical `--input`、deprecated positional alias、Workspace、JSON 和 bounded body option 均可见。
- 使用仓库安全 fixture 和产品 Runtime composition 完成 deterministic CLI smoke：canonical `produce video --wait --json` 成功提交并 semantic validate/commit；重建 Runtime 后 `job get`/`job wait`、`artifact inspect`、`draft inspect` 均成功；重启后的 download/transcribe/model replay count 为 0；human Artifact 与 RuntimeInfo 输出正常。该 smoke 明确不是付费 Provider 或真实平台发布验收。
- smoke 使用中文+空格 Workspace 和独立 MachineState root；JSON 未出现绝对 Workspace 路径，Draft 默认未返回正文，inspect 前后无 Workspace 文件写入。

## 19. 稳定 CLI 示例

规范提交：

```text
alltonote produce video --input fixture://course --workspace <WORKSPACE> --wait --json
alltonote job get <JOB_ID> --workspace <WORKSPACE> --json
alltonote artifact inspect <ARTIFACT_ID> --workspace <WORKSPACE> --json
alltonote draft inspect <DRAFT_ID> --workspace <WORKSPACE> --json
alltonote draft inspect <DRAFT_ID> --workspace <WORKSPACE> --body-bytes 4096 --json
```

所有 JSON 命令使用同一顶层合同；以下只展示脱敏 shape：

```json
{
  "alltonote_cli_protocol_version": 1,
  "ok": true,
  "command": "artifact inspect",
  "correlation_id": "corr_<typed-id>",
  "data": {
    "target_kind": "artifact",
    "bundle": {"bundle_id": "bnd_<typed-id>", "manifest_sha256": "sha256:<digest>"},
    "artifact": {
      "artifact_id": "art_<typed-id>",
      "kind": "knowledge.draft.markdown.v1",
      "size_bytes": 192,
      "sha256": "sha256:<digest>",
      "compiler_identity": "alltonote.video-source-bundle@1.0.0"
    },
    "quality": {"overall": "pass", "publish_eligible": true, "repair_attempts": 0}
  },
  "error": null,
  "warnings": [],
  "job": null,
  "artifacts": [{"artifact_id": "art_<typed-id>", "kind": "knowledge.draft.markdown.v1"}],
  "capabilities": [],
  "versions": {"runtime_version": "0.1.0", "cli_protocol_version": 1}
}
```

## 20. 性能与安全结论

- 最终 9 个独立进程复测：`version --json` 中位数 `134.259 ms`、最大 `142.600 ms`；`runtime info --json` 中位数 `175.405 ms`、最大 `206.055 ms`，仍满足 `<150 ms` / `<300 ms` 目标。RCP-06 的 `job get` 暖路径数据见第 14 节；RCP-07 的 Artifact 数据见第 15 节。
- JSON/human 共用一个 Application Result；Automation Protocol 仍为 `alltonote_cli_protocol_version=1`，退出码仍为 `0/2/10/20/30/40/50/60/70/130`。Runtime plan 中不同 envelope/exit 示例被视为未发布设计漂移，本 Goal 未破坏既有脚本。
- Secret 只存在于 OS keyring/ephemeral boundary；config、Job snapshot/event、Bundle/Receipt 和 inspect projection 均不返回 Secret。canary tests覆盖 API key/Cookie/Token/Prompt/Provider raw/credential path/绝对路径及异常对象。
- MachineState、JobStore、Attempt、checkpoint、lease、日志、external result 和 Workspace registry继续位于唯一 Runtime platform path service 下并与 Vault 分离；inspect 使用 read-only Workspace handle，不发布、不写 Wiki。

## 21. 已知限制、阻塞与 Git 状态

- RCP-08..12（Feature Pack、Desktop Resolver/API 等）和 VREL-02..11 明确未实现，不能从本次 Wave 1A 推导为完整 Video/Desktop/Pack release。
- YouTube 实时 acquisition 保持独立 `blocked`；Bilibili、本地 Whisper、截图、clean-machine 和 macOS 均未在本 Goal 冒充通过。
- 没有真实付费调用、Cookie/API Key 读取、系统级安装、PATH/服务/安全设置变更、Vault 数据迁移或外部发布。
- 文档工作树仍是 `master`，实现工作树仍是 `codex/alltonote-video-producer`；两者保留开始时的用户 dirty/untracked 成果以及本 Goal 的未提交成果。未执行 stage、commit、merge、rebase、push 或 Release。

## 22. 本 Goal 修改面

主要现有实现文件：

- `backend/app/cli/main.py`
- `backend/app/core/sdk.py`
- `backend/app/core/application/video_service.py`
- `backend/app/core/application/job_service.py`
- `backend/app/core/config/model.py`
- `backend/app/core/config/loader.py`
- `backend/app/core/ports/jobs.py`
- `backend/app/adapters/credentials/keyring_broker.py`
- `backend/app/adapters/credentials/profile_catalog.py`
- `backend/app/adapters/jobs/sqlite_repository.py`
- `backend/app/adapters/iwiki/portable_gateway.py`
- `backend/app/runtime.py`
- `backend/app/runtime-lock.json`
- `backend/pyproject.toml`

新增 Runtime/CLI/Core 模块：

- `backend/app/cli/contracts.py`、`errors.py`、`render.py`
- `backend/app/cli/commands/jobs.py`、`artifacts.py`
- `backend/app/runtime_paths.py`、`runtime_config.py`、`runtime_info.py`、`runtime_capabilities.py`、`runtime_lock.py`
- `backend/app/job_runtime.py`
- `backend/app/core/config/events.py`
- `backend/app/core/application/job_query_service.py`、`artifact_query_service.py`
- `backend/app/core/ports/job_queries.py`、`portable_queries.py`

测试：

- 新增 `backend/tests/contracts/`、`backend/tests/cli/test_credential_cli.py`、`test_job_cli.py`、`test_artifact_cli.py` 和 `backend/tests/runtime/`。
- 局部扩展现有 Produce CLI、Runtime bootstrap、Credential、SQLite Job、JobService、Runtime config、Video request/recovery/Portable integration tests；没有删除或重写既有长视频测试。

文档：

- `docs/acceptance/2026-07-18-runtime-cli-wave-1a-baseline.md`
- `docs/tasks/alltonote-master-tasks.md`
- `docs/tasks/alltonote-design-coverage-matrix.md`
- Runtime/CLI implementation plan 与 Video release plan 的 implementation status/验收引用。
