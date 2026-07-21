# AllToNote Runtime、CLI 与 Feature Pack 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - ../specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md
downstream:
  - 2026-07-18-alltonote-video-release-implementation-plan.md
  - 2026-07-18-alltonote-vault-desktop-implementation-plan.md
implementation_status: rcp-00-through-07-completed-rcp-08-through-12-pending
last_verified_at: 2026-07-18
```

## 0. 执行规则

实施工作树优先使用 `G:\AllToNote-video-producer`，但开始前必须按总任务清单检查 branch/status，保留所有现有未提交成果。未经用户授权不 stage/commit/push/merge。

每个任务遵循：先写失败测试 -> 最小实现 -> 小范围测试 -> 相关全量测试 -> 更新任务状态/证据。不要借机重构旧 FastAPI/React 代码；新 CLI/Core 只在必要适配点接旧实现。

完成本计划不代表发布完成；真实 Video、Vault、Desktop 和安装器由各自计划验收。

## 1. 成功标准

- `alltonote runtime info|doctor|paths` 有稳定 JSON；
- job/artifact/draft 命令可供无 Desktop Agent 自动化；
- config 与 Secret 分离；
- machine state 不在 Vault；
- Pack manifest/probe/pin/rollback 基础完成；
- Desktop 可通过稳定 resolver/handshake 找到 Runtime；
- 现有 Video/Portable 全回归不退化；
- CLI 冷启动、脱敏、错误码达到设计 Gate。

## 2. Task RCP-00：冻结现状与合同 Golden

目标：避免一边实现一边继续发明 CLI。

读取：Runtime 设计、Video/Portable 设计、当前 `backend/app/cli/main.py`、`backend/app/core/sdk.py`、`backend/app/runtime-lock.json`、`backend/pyproject.toml`。

新增/修改建议：

- `backend/tests/contracts/test_cli_envelope_golden.py`
- `backend/tests/contracts/test_runtime_info_golden.py`
- `backend/tests/contracts/fixtures/*.json`
- `backend/app/cli/contracts.py`

步骤：

1. 列出现有命令、参数、stdout/stderr、退出码；
2. 固定 `api_version=1` envelope、error category/code、日期和 path redaction；
3. 为 version、runtime info、一个成功 job get、一个失败 produce 写 golden；
4. 确认旧 `produce video` 人类输出兼容策略；
5. 只提交合同/测试，不先实现所有命令。

验证：

```powershell
pytest backend/tests/contracts/test_cli_envelope_golden.py -q
pytest backend/tests/cli -q
```

Gate：golden 能明确失败，字段没有 Secret/绝对路径/Prompt/provider raw。

## 3. Task RCP-01：统一 CLI Envelope 与错误映射

目标：所有命令不再各自 print/捕获异常。

目标文件：

- `backend/app/cli/contracts.py`
- `backend/app/cli/errors.py`
- `backend/app/cli/render.py`
- `backend/app/cli/main.py`
- `backend/app/core/errors.py`

步骤：

1. 建立 typed success/error envelope；
2. 建立 Core error -> CLI error code/category/exit code 的单向映射；
3. `--json` stdout 只输出 envelope；
4. 人类进度走 stderr；
5. 未知异常映射 `RUNTIME_INTERNAL_ERROR`，只在 debug log 保留受控 traceback；
6. 加入通用 `--show-paths`/debug policy，但默认脱敏；
7. 迁移现有 version/produce video，不改变业务服务。

测试：参数错误、配置错误、能力缺失、外部平台、模型、质量、取消、冲突、内部错误；验证退出码。

Gate：CLI test + golden 全绿，stdout 可被严格 JSON parser 读取。

## 4. Task RCP-02：平台目录与 MachineState

目标：统一 config/data/cache/state/log，显式排除 Vault。

目标文件：

- `backend/app/runtime/paths.py`
- `backend/app/runtime/state.py`
- `backend/app/core/config/loader.py`
- `backend/tests/runtime/test_runtime_paths.py`

步骤：

1. 用 `platformdirs` 建立不可变 `RuntimePaths`；
2. 支持测试 override state root；
3. 迁移 JobStore、logs、credential refs、Pack registry 的默认解析；
4. 检测 state root 位于已知 Workspace 内时 fail closed；
5. `runtime paths --json` 返回逻辑角色，默认隐藏完整用户名；
6. 不移动现有用户数据，先提供显式 migration/diagnostic；
7. 添加 Windows/macOS 路径测试。

Gate：临时 Vault fixture 中没有 `jobs.sqlite`/runtime cache；删除 cache 不影响 Job/Bundle。

## 5. Task RCP-03：配置分域与快照

目标：实现 default < user < profile < env allowlist < flags，Secret 不参与普通合并。

目标文件：

- `backend/app/core/config/model.py`
- `backend/app/core/config/loader.py`
- `backend/app/runtime/config_service.py`
- `backend/tests/core/test_runtime_config.py`

步骤：

1. 按设计分区 typed config；
2. 对未知/弃用字段给出稳定 warning/error；
3. 定义允许的环境变量，不读取任意 `ALLTONOTE_*`；
4. 生成 redacted effective config 与 digest；
5. Job 创建时持久化影响结果的快照/digest；
6. 恢复时区分 semantic vs non-semantic drift；
7. 实现 `config get|set|validate|profiles`；
8. config 写入使用 atomic replace + 权限检查。

Gate：Secret 值无法通过 config get/Job snapshot/log 看到；drift tests 全绿。

## 6. Task RCP-04：Credential Service 产品化

现有基础：`backend/app/adapters/credentials/keyring_broker.py`、`profile_catalog.py`。

目标：把凭据变为 `secret_ref`，明确 headless/ephemeral 失败语义。

步骤：

1. 固定 CredentialPort 错误：not-found/backend-unavailable/locked/invalid；
2. `credential set` 从安全 stdin/交互读取，不接受明文 positional arg；
3. `status` 只返回 present/validated/last checked；
4. CI 环境变量注入不持久化；
5. 无安全 keyring 时禁止静默写明文；
6. child process Secret 传递不使用 argv；
7. 添加日志/异常/Job/Receipt 全局泄漏回归。

验证：

```powershell
pytest backend/tests/adapters/test_credential_broker.py -q
pytest backend/tests -q -k "credential or sensitive or redact"
```

## 7. Task RCP-05：RuntimeInfo 与 Capability Registry

目标文件：

- `backend/app/runtime/info.py`
- `backend/app/runtime/capabilities.py`
- `backend/app/runtime_lock.py`（如现有位置不同，复用现有 loader）
- `backend/tests/runtime/test_runtime_info.py`

步骤：

1. 从 package/runtime-lock/iwiki inspection 生成 RuntimeInfo；
2. capability 分 static installed 和 dynamic health；
3. 注册现有 Video platform/transcriber/model/portable 能力；
4. `runtime info` 无网络、无副作用、快速；
5. `runtime doctor` 可选 `--dynamic`，每项 bounded timeout；
6. Runtime/package/contract/version 不匹配时稳定错误；
7. 为后续 Desktop API handshake 复用同一对象。

性能 Gate：version < 150 ms、info < 300 ms 的基线测试/记录；CI 不把抖动当硬单测，使用专门 benchmark。

## 8. Task RCP-06：Job 查询/等待/取消/重试 CLI

现有基础：`job_service.py`、jobs ports/state machine/recovery、SQLite repository。

目标文件：

- `backend/app/cli/commands/jobs.py`
- `backend/app/core/application/job_query_service.py`
- `backend/tests/cli/test_job_cli.py`

步骤：

1. 不让 CLI 直接查 SQLite；增加 read projection service；
2. `get/list` 支持 cursor/filter，默认有界；
3. `events` 支持 after sequence/JSONL；
4. `wait` 有 timeout、Ctrl+C 不隐式取消；
5. `cancel` 走 JobService；
6. `respond` 只处理明确 waiting-for-input schema；
7. `retry` 创建新 Job/retry_of，终态不复活；
8. 输出 action/retryability/result refs；
9. 并发和不存在/权限错误不泄漏敏感信息。

Gate：CLI-only 可 submit -> query -> wait -> cancel/retry；SQLite 无绕过状态机写入。

## 9. Task RCP-07：Artifact/Draft inspect CLI

目标：给 Agent/用户稳定检查结果，不暴露任意文件读取。

目标文件：

- `backend/app/core/application/artifact_query_service.py`
- `backend/app/cli/commands/artifacts.py`
- `backend/tests/cli/test_artifact_cli.py`

步骤：

1. 只接受 artifact/draft/bundle ID + Workspace grant；
2. 调用 PortableGateway/validator，不自行解析私有 iwiki 文件；
3. 返回 kind/hash/size/quality/source/evidence 摘要；
4. 正文只在明确 bounded option 下返回；
5. Draft inspect 支持 heading/evidence summary；
6. 路径/Secret/Provider raw 脱敏；
7. stale/missing/contract mismatch 错误稳定。

## 10. Task RCP-08：Pack Manifest 与 Registry v1

目标文件：

- `backend/app/packs/contracts.py`
- `backend/app/packs/registry.py`
- `backend/app/packs/verification.py`
- `backend/app/packs/probe.py`
- `backend/tests/packs/`

步骤：

1. 先实现纯数据 manifest/schema/hash/compatibility；
2. 测试 path traversal、duplicate file、wrong platform/hash/signature；
3. 以 test signing key 验证签名流程，production key 不入仓库；
4. installed registry atomic update；
5. immutable version directory + active pointer；
6. static probe contract；
7. Job pin pack identity；
8. running Job 引用旧 Pack 时禁止删除；
9. 暂不实现互联网 catalog/第三方代码加载。

Gate：损坏/不兼容/伪造 Pack 无法激活，旧 active 保持可用。

## 11. Task RCP-09：先迁移 Media/Transcribe 为受控 Pack 投影

目标：用真实现有依赖验证 Pack，不重写 Downloader/Transcriber。

步骤：

1. 为当前 FFmpeg/yt-dlp/JS/Whisper 生成开发 manifest；
2. Adapter 通过 capability/entrypoint resolver 找工具，不硬编码 PATH；
3. 保留开发环境 fallback，但 production profile 必须 pinned；
4. subprocess 使用最小 env、timeout、kill tree、output bound；
5. 记录实际 tool/version/pack 到 Receipt；
6. 测试未装 media Pack 时字幕不需要 FFmpeg 的快路径仍可用；
7. Pack 更新不影响 running Job。

Gate：本地 Video golden path 继续通过，且 runtime info 能解释依赖来自何处。

## 12. Task RCP-10：Desktop Runtime Resolver 合同

本任务只做 Runtime 侧和可独立测试的 resolver library，不做 UI。

目标文件：

- `BillNote_frontend/src-tauri/src/runtime_resolver.rs`
- `BillNote_frontend/src-tauri/src/runtime_process.rs`
- Rust tests / integration fixture
- Runtime `desktop-api --handshake-only` 基础

步骤：

1. 实现 explicit/managed/standard/PATH 候选顺序；
2. 验证候选 publisher/路径/`runtime info --json`；
3. timeout、malformed JSON、version incompatible fail closed；
4. 不执行当前目录下同名未知 exe；
5. 返回 missing/repair/update/incompatible 状态；
6. 不在此任务实现 Vault API/React UI；
7. 测试中文路径、空格、多个版本、损坏 runtime。

## 13. Task RCP-11：Runtime Doctor 与修复体验

步骤：

1. Doctor 分层：runtime/state/iwiki/pack/credential ref/Vault optional/external dynamic；
2. 每项返回 code/status/action，不能只返回日志文本；
3. 修复动作默认只是建议；`--fix` 只执行明确幂等、安全操作；
4. 不自动更新/删除/重装 Pack；
5. 输出可复制的脱敏诊断包；
6. 测试磁盘满、权限、锁、schema、Pack、keyring、network failure。

## 14. Task RCP-12：兼容、性能与安全 Gate

执行：

```powershell
pytest backend/tests/cli backend/tests/core backend/tests/adapters backend/tests/packs -q
pytest backend/tests -q
pnpm --dir BillNote_frontend test
pnpm --dir BillNote_frontend tauri build   # 仅在发布环境/依赖具备时
```

另外运行：

- CLI golden compatibility；
- Secret canary 全仓输出扫描；
- malicious Pack/path；
- 10k job list pagination；
- cold-start benchmark；
- Windows clean-user state root；
- macOS smoke 仅在对应主机，不用 Windows 模拟冒充。

Gate：无回归、无 Secret、无 Vault machine state、所有失败可行动。

## 15. 建议提交边界

如果用户授权提交，按任务形成小提交：

1. CLI contracts；
2. paths/config/credentials；
3. runtime info/doctor；
4. job/artifact CLI；
5. pack contracts/registry；
6. media Pack integration；
7. resolver；
8. final gates/docs。

不要把现有大批未提交 Video 代码与新 Runtime 重构混成一个不可审查提交；先按 Git 历史和用户要求安全整理。

## 16. 交接证据

完成后更新：

- `docs/tasks/alltonote-master-tasks.md` 的 `RELEASE-CLI-01`；
- 设计登记表 implementation status；
- `docs/acceptance/runtime-cli-v1.md`；
- 命令/JSON golden 版本；
- benchmark 与 clean-machine 结果；
- 已知兼容/外部阻塞。

不得在验收摘要中保存 Secret、Cookie、私人路径、Prompt 或完整模型正文。

## 17. Wave 1A 实施记录（2026-07-18）

- `RCP-00` 至 `RCP-07` 已按 [`Runtime/CLI Wave 1A acceptance`](../../acceptance/2026-07-18-runtime-cli-wave-1a-baseline.md) 完成并通过最终全回归。
- 已交付统一 Automation Protocol v1 envelope/exit/redaction、唯一平台路径、配置快照、Credential CLI、RuntimeInfo/capabilities、完整 Job CLI、read-only Artifact/Draft inspect。
- `RCP-08` 至 `RCP-12` 未开始，继续保持本计划的后续范围；不得把 Wave 1A 状态解释为 Pack/Desktop/完整分发已完成。
- 最终 pytest 为 `1820 passed, 2 skipped, 3 warnings, 3 subtests passed`；`git diff --check` 退出码 0。未执行 Git 集成或发布操作。
