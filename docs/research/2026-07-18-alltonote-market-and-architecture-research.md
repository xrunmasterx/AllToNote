# AllToNote 市场与架构一手资料调研

```yaml
doc_type: research
status: completed
authority: evidence
upstream:
  - ../superpowers/specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md
downstream:
  - ../superpowers/specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - ../superpowers/specs/2026-07-18-alltonote-review-publisher-design.md
  - ../superpowers/specs/2026-07-18-alltonote-knowledge-access-mcp-design.md
  - ../superpowers/specs/2026-07-18-alltonote-engine-production-mcp-design.md
  - ../superpowers/specs/2026-07-18-alltonote-recipe-extension-contract-design.md
  - ../superpowers/specs/2026-07-18-alltonote-article-wiki-recipe-design.md
  - ../superpowers/specs/2026-07-18-alltonote-document-recipe-design.md
  - ../superpowers/specs/2026-07-18-alltonote-codebase-ue5-recipe-design.md
  - ../superpowers/specs/2026-07-18-alltonote-personal-work-digest-design.md
  - ../superpowers/specs/2026-07-18-alltonote-site-control-plane-design.md
  - ../superpowers/specs/2026-07-18-alltonote-platform-release-design.md
last_verified_at: 2026-07-18
```

## 1. 调研问题

本调研不试图寻找一个可以直接复制的竞品，而是回答以下架构问题：

1. 用户知识的最终事实源应当是开放文件、应用私有数据库，还是云端服务？
2. “把任意内容变成高质量知识”应由一个巨型流水线完成，还是由稳定内核与独立 Recipe 组合完成？
3. CLI、桌面端、MCP、网站分别应拥有哪一部分职责？
4. 长任务、模型调用、浏览器登录、第三方下载器、OCR、代码 Agent 如何隔离失败与升级？
5. 如何让 Windows 中国大陆用户容易安装，同时不把全部依赖和业务能力锁进 EXE？
6. 哪些成熟产品或协议已经证明了某些边界，哪些产品的限制恰好说明 AllToNote 不应采用什么？

只使用官方文档、官方仓库和协议规范作为技术事实来源。产品评价属于基于这些事实的推论，不把营销描述当作性能或可靠性证明。

## 2. 第一性原理

### 2.1 产品真正交付的不是“一个笔记应用”

AllToNote 的长期产品价值是：

```text
不稳定、异构、难复用的外部内容
  -> 可追溯的来源事实
  -> 可审阅的知识草稿
  -> 开放、稳定、可被任意工具读取的 Markdown 知识
```

因此必须区分：

- 知识资产：属于用户，寿命应长于任何一个应用；
- 编译能力：AllToNote 提供，可被 CLI、Desktop、Agent、MCP 调用；
- 运行状态：机器级、可重建或可恢复，不应污染 Vault；
- 产品入口：Desktop 是方便的 UI，不是业务能力的唯一宿主；
- 云端控制面：负责账号、授权、分发、公共知识和商业化，不默认托管个人正文。

### 2.2 最小稳定内核

真正需要长期稳定的只有六类语义：

1. Source/Revision：输入到底是什么；
2. Artifact/Evidence：产物如何回指来源；
3. Job/Checkpoint：长任务如何恢复且不重复付费；
4. Recipe：某类输入如何编译；
5. Review/Publisher：草稿如何安全进入正式知识；
6. Workspace Grant：谁可以读写哪个范围。

下载器、OCR、Whisper、浏览器、LLM Provider、Agent、向量索引和 UI 都是可替换实现，不应成为知识格式或核心业务真相。

### 2.3 高质量不能只等同于“大模型重写”

高质量知识至少同时满足：

- 完整性：没有静默丢失高价值来源内容；
- 真实性：可定位到时间、页、段、幻灯片、文件/行或 Git commit；
- 可读性：结构、术语、重复、节奏适合人吸收；
- 可维护性：新来源版本到来时可识别差异，不覆盖用户编辑；
- 可移植性：脱离 AllToNote 仍可阅读、搜索和版本管理；
- 成本可控：短内容走短路径，长内容才使用分块、全局规划和修复。

这也是为什么 Knowledge Note 与 Faithful Edition 应并存：前者优化理解效率，后者优化来源内容保全，不能用一个模式同时假装解决两个目标。

## 3. 市面产品与协议观察

### 3.1 本地知识与开放文件

| 产品/方案 | 官方事实 | 对 AllToNote 的启示 | 不应照搬之处 |
|---|---|---|---|
| Obsidian | Vault 是本地文件夹，笔记为 Markdown；可由其他编辑器和文件管理器直接修改，Obsidian 会刷新变化。[官方说明](https://obsidian.md/help/Files%2Band%2Bfolders/How%2BObsidian%2Bstores%2Bdata) | “开放磁盘合同”可让知识寿命独立于应用；缓存可以重建 | 不把 Obsidian 专有配置、插件或索引格式变成 AllToNote 合同 |
| Logseq | Graph 可基于 Markdown/Org 文件工作。[官方文档](https://docs.logseq.com/) | 同一批开放文件可被多个知识工具消费 | 不假设所有 Markdown 方言、块引用和属性都天然兼容 |
| Joplin | Offline-first，支持 Markdown 与多种同步目标；同步层有独立抽象。[官方帮助](https://joplinapp.org/help/) / [同步设计](https://joplinapp.org/help/dev/spec/sync/) | 离线优先和同步可以分层；本地生产不应依赖云在线 | Joplin 内部项目/数据库模型不适合作为跨工具开放合同 |
| Anytype | Offline-first，但本地内容以应用可读的加密片段存储，不能像普通 Markdown 那样被任意应用直接读取。[官方说明](https://doc.anytype.io/anytype-docs/advanced/data-and-security/data-storage-and-deletion) | “本地优先”不自动等于“开放可互操作”；AllToNote 必须明确选择后者 | 不采用只有本应用能解释的正文存储 |

结论：AllToNote 应坚持 Markdown 为正式知识事实源，SQLite/FTS/向量库只作可删除重建的派生索引；用户可以用 Obsidian、编辑器、Git 或未来 Agent 读取同一 Vault。

### 3.2 AI 资料摄取与阅读产品

| 产品 | 官方事实 | 借鉴点 | AllToNote 的差异 |
|---|---|---|---|
| NotebookLM | 支持 PDF、网页、YouTube、音频、Docs、PPTX 等来源；多数导入是静态副本；YouTube 主要依赖公开字幕；网页只导入文本。[来源类型说明](https://support.google.com/notebooklm/answer/16215270) | 多来源统一到来源层；回答应受来源约束 | AllToNote 还要产出永久开放的 Markdown、Evidence 和可发布 Bundle，而不只是会话型问答 |
| Readwise Reader | 可保存网页、PDF、视频；YouTube 提供时间同步 transcript；浏览器扩展收到浏览器已渲染页面，通常比仅传 URL 更可靠。[产品文档](https://docs.readwise.io/reader/docs) / [网页解析说明](https://docs.readwise.io/reader/docs/faqs/parsing) / [视频说明](https://docs.readwise.io/reader/docs/faqs/videos) | URL 直抓和浏览器捕获必须是两条采集路径；网页解析无法承诺 100% 成功 | AllToNote 需要保留原始快照、Evidence、编译 Receipt 和发布门，不只保存高亮 |
| Readwise 导出 | 支持 Markdown/Obsidian 导出和模板。[官方说明](https://docs.readwise.io/reader/docs/faqs/exporting) | 开放导出显著降低锁定风险 | AllToNote 的 Markdown 不是导出副本，而是首要正式资产 |

结论：文章/Wiki Recipe 必须同时支持无登录 HTTP 路径和用户明确授权的浏览器捕获路径；登录态是敏感输入，只能保存引用或系统凭据标识，不能进入 Bundle。

### 3.3 网页与文档解析工具

| 工具 | 官方事实 | 设计含义 |
|---|---|---|
| Mozilla Readability | 从 DOM 提取标题、正文、作者、语言、发布时间等；官方明确要求对不可信输出再做 sanitizer/CSP。[官方仓库](https://github.com/mozilla/readability) | 它适合做文章候选提取器，不是安全边界，也不能独自证明完整性 |
| Playwright | 可复用浏览器认证状态，但状态文件可能包含可冒充用户的 Cookie/Headers，官方明确警告不得提交仓库。[认证文档](https://playwright.dev/docs/auth) | 浏览器登录态必须进入系统级 Secret/受控 session，不能进 Vault、日志、Receipt 或测试 fixture |
| Apache Tika | 以统一接口从一千多种格式提取文本与元数据，包括 PPT/XLS/PDF。[官网](https://tika.apache.org/) | 可作为广覆盖 fallback 或隔离 sidecar；不能成为 Core 的强制 JVM 依赖 |
| Pandoc | 支持多种输入/输出格式并可列举；对不可信输入提供 sandbox 相关警告。[用户手册](https://pandoc.org/MANUAL.html) | 适合可选转换 Pack；转换结果仍需保留结构损失和警告，不可假装无损 |
| PDF.js | 是解析和渲染 PDF 的通用 Web 标准平台。[官网](https://mozilla.github.io/pdf.js/) | Desktop 预览可独立使用；渲染器不应拥有知识提取语义 |
| OCRmyPDF | 为扫描 PDF 增加可搜索文字层，兼容 born-digital 与扫描页；官方列出阅读顺序、手写和低质量扫描限制。[官方文档](https://ocrmypdf.readthedocs.io/en/latest/introduction.html) | 先探测是否已有文本，再按页局部 OCR；OCR 结果必须标注置信和来源页，不能覆盖原始文件 |
| python-pptx | 可在无 PowerPoint 环境读取和分析 PPTX 文本、图片、Notes 等，但并不支持 PowerPoint 的全部丰富特性。[官方文档](https://python-pptx.readthedocs.io/en/latest/) | PPTX 结构提取与视觉渲染应是两条证据通道；复杂对象需显式降级 |
| LibreOffice headless | 官方支持 `--headless` 无 UI 运行和命令行转换。[官方帮助](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html) | 可作为可选渲染/转换 sidecar，必须隔离用户 profile、超时和崩溃 |

结论：Document Recipe 应采用“原生结构优先、视觉/OCR 补充、统一 Evidence”而不是“把每页截图全部扔给视觉模型”。后者成本高、慢、难复核，并且会丢失已有机器可读结构。

### 3.4 视频采集与转写

| 工具 | 官方事实 | 设计含义 |
|---|---|---|
| yt-dlp | 支持大量网站、字幕、认证、插件和独立二进制；官方说明站点变化会使稳定版本过时，YouTube 完整支持还依赖 JS runtime/ejs，且二进制分发包含不同许可证。[官方仓库](https://github.com/yt-dlp/yt-dlp) | 平台采集器必须可独立升级和诊断；不能把一次 anti-bot 失败归类为编译器失败；依赖和许可证要进入 Pack manifest |
| FFmpeg | 提供媒体探测、转换、过滤和命令行工具文档。[官方文档](https://ffmpeg.org/documentation.html) | 作为媒体 Pack 的外部工具；Core 只拥有受控调用、超时、能力探测和产物校验 |
| OpenAI Whisper | 开源通用语音识别模型和推理代码。[官方仓库](https://github.com/openai/whisper) | 本地转写是可选能力，不应强塞进最小 Runtime |
| faster-whisper | 基于 CTranslate2 的 Whisper 实现，提供不同设备/精度配置。[官方仓库](https://github.com/SYSTRAN/faster-whisper) | CPU/GPU/模型是能力矩阵，不应写死为单一安装包 |

结论：Video Recipe 现有“字幕优先、音频/Whisper 回退、平台错误与编译错误分离”是正确方向。YouTube 风控属于可观测的 acquisition 阻塞，不能用 Fake Runtime 或缓存 transcript 冒充实时获取成功。

### 3.5 CLI、桌面和进程隔离

| 技术/产品 | 官方事实 | 设计含义 |
|---|---|---|
| Language Server Protocol | 通过稳定协议让同一语言服务器被多个编辑器复用。[官网](https://microsoft.github.io/language-server-protocol/) | 独立 Runtime + 多客户端是成熟模式；UI 不必拥有业务实现 |
| VS Code Extension Host | 扩展运行在与 UI 隔离的 host 中，并可按需激活。[官方文档](https://code.visualstudio.com/api/advanced-topics/extension-host) | Feature Pack 应延迟加载、进程隔离；坏 Pack 不应拖垮 Desktop/Core |
| Tauri Sidecar | 支持随应用调用外部二进制，并按平台/架构准备 target-specific 文件；命令需能力授权。[官方文档](https://v2.tauri.app/develop/sidecar/) | Desktop 可以发现或托管独立 Runtime；sidecar 只解决安装便利，不改变组件所有权 |
| Tauri 权限/Runtime Authority | Tauri 通过 capability 限制 WebView 可调用的命令。[权限文档](https://v2.tauri.app/security/permissions/) / [Runtime Authority](https://v2.tauri.app/security/runtime-authority/) | Desktop WebView 不直接获得任意磁盘或进程权限，所有访问经最小能力 API |

结论：采用“托管式独立 Runtime”合理：用户获得一体化安装体验，但 Runtime、CLI 和 Core 在架构与版本上仍独立；Desktop 只负责发现、握手、启动临时 Desktop API 和展示状态。

### 3.6 Job、持久化与长任务

| 技术 | 官方事实 | 设计含义 |
|---|---|---|
| SQLite WAL | WAL 提升读写并发，但要求同一主机，仍只有一个 writer，并需要 checkpoint 管理。[官方文档](https://www.sqlite.org/wal.html) | JobStore 可以是每台机器本地 SQLite；不得放入同步 Vault，也不能被当成跨机队列 |
| Temporal | 通过 Event History、重放和 Retry Policy 提供 Durable Execution。[概念文档](https://docs.temporal.io/evaluate/understanding-temporal) | 可借鉴幂等、事件历史、活动重试概念；当前个人桌面产品不应引入服务端 Temporal 集群和全量 replay 复杂度 |
| MCP Tasks | 当前规范中的 durable task wrapper 仍标为 experimental。[规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks) | AllToNote 内部 Job ID/状态机必须保持稳定；未来可以映射为 MCP Task，但不能反向依赖实验协议 |

结论：当前 Job/Checkpoint/ExternalOperation 方案应保留。常驻 Engine 只有在出现“调用者必须退出而任务继续、并需要多个 worker/资源调度”的真实需求后再启用；不要为了未来可能性先引入 daemon。

### 3.7 MCP 与 Agent 接入

MCP 官方架构把 Host、Client、Server 分离，Server 提供 Resources/Tools/Prompts，并在初始化时进行 capability negotiation；本地 stdio 无网络开销，远端使用 Streamable HTTP。[架构规范](https://modelcontextprotocol.io/specification/2025-06-18/architecture) / [传输与原语说明](https://modelcontextprotocol.io/docs/learn/architecture)

由此得到：

- “读取知识”和“生产知识”必须拆成两个 MCP Server/能力面；
- 本地只读知识 MCP 默认 stdio，进程继承 Workspace Grant；
- 生产 MCP 的工具具有成本、外部副作用和长任务，必须显式授权、预算和 Job 化；
- 公共远端知识使用 Streamable HTTP 与标准认证；
- stdio 不应套用远端 OAuth 模型；远端授权需遵循 MCP Authorization 约束。[授权规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- MCP Server 只能看到完成调用所需的最小上下文，不接收整个对话历史。

### 3.8 代码库与增量知识

| 技术 | 官方事实 | 设计含义 |
|---|---|---|
| Git | commit 记录 index 快照并形成父子历史；log/diff 能稳定选择 revision 范围。[commit](https://git-scm.com/docs/git-commit) / [log](https://git-scm.com/docs/git-log) | Code SourceRevision 应以 repo identity + commit/tree + dirty-state digest 为基线，而不是“当前文件夹” |
| Tree-sitter | 提供增量解析和具体语法树。[官方文档](https://tree-sitter.github.io/tree-sitter/) | 可用于多语言结构定位，但不替代编译数据库和语义索引 |
| clangd index | 区分动态索引和静态后台索引，并基于编译命令理解 C/C++。[官方设计](https://clangd.llvm.org/design/indexing) | UE5/C++ 需要编译数据库、模块边界和符号索引；不能仅靠全文切块和向量检索声称理解代码 |

结论：Codebase/UE5 Recipe 必须固定 revision、记录 file/line/symbol Evidence，并把 Agent 的每个事实结论与实际源码证据绑定。UE5 能力是独立 Pack，通用 Core 不依赖引擎工程环境。

### 3.9 Windows/macOS 分发

| 平台 | 官方事实 | 设计含义 |
|---|---|---|
| Windows MSIX | MSIX 提供可靠安装/卸载、差分更新和包身份；包必须签名。[概览](https://learn.microsoft.com/en-us/windows/msix/overview) / [签名](https://learn.microsoft.com/en-us/windows/msix/package/signing-package-overview) | 可作为正式渠道之一，但需验证文件系统虚拟化、CLI PATH、Runtime/Pack 外置更新是否适配；不应只因“现代”就唯一选 MSIX |
| WinGet | 通过 YAML manifest 分发受支持安装器。[官方说明](https://learn.microsoft.com/en-us/windows/package-manager/package/) | 适合中国大陆用户之外的辅助渠道；主下载必须有可达镜像和校验 |
| Apple notarization | Developer ID 签名、Hardened Runtime、时间戳和 notarization 是直接分发的重要要求。[Apple 官方说明](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution) | macOS 不是“重新编译一下”即可；必须独立做 Keychain、FSEvents/APFS、签名、公证和回归 Gate |
| Tauri Updater | 支持签名更新产物和不同平台更新。[官方文档](https://v2.tauri.app/plugin/updater/) | Desktop 更新签名与 Runtime/Pack manifest 签名可共用发布原则，但各自版本和回滚必须独立 |

结论：Windows 是 Tier 1；macOS 是共享 Core 稳定后的 Tier 2。中国大陆首发必须提供离线组合安装包、独立 Runtime 包、校验文件和至少两个可切换下载源；运行时不能依赖安装过程中临时访问 GitHub。

## 4. 方案比较

### 4.1 单体桌面应用

优点：首次安装最直观，进程和版本少。

致命问题：

- CLI/Agent 必须绕过或启动 UI；
- Whisper、FFmpeg、浏览器、OCR、模型等使安装包急剧膨胀；
- 任一平台适配器升级都迫使整桌面端更新；
- 用户知识容易被 UI 的私有数据库和生命周期绑死；
- 无法满足“任何 Agent 都能直接使用”的核心目标。

结论：拒绝作为架构，只允许把多个独立组件组合进便利安装器。

### 4.2 网页 + 本地 Agent

优点：UI 更新快、安装界面轻。

问题：

- 浏览器权限、文件选择、长任务、进程生命周期和离线体验更复杂；
- 网站可用性会成为本地知识生产入口的依赖；
- 很容易演化为云端托管正文并弱化 CLI；
- 账号、网页与本地 Runtime 的认证边界比纯桌面更难解释。

结论：网站适合作为账号、邀请、下载、设备、公共知识和 Pack 控制面，不作为个人 Vault 的主 UI 或业务宿主。

### 4.3 独立 Runtime/CLI + 薄 Desktop + 可选网站

优点：

- CLI 和 Agent 是一等公民；
- Desktop 可轻量且随时关闭；
- Core/SDK 保持单一业务实现；
- 重依赖按 Pack 安装；
- 个人数据留在开放 Vault；
- 网站故障不影响本地工作；
- 未来可加 Engine，但不要求常驻 daemon。

成本：需要版本握手、安装发现、更新协调和更严格的协议设计。

结论：这是满足长期目标的最小正确架构，也是本轮所有下位设计的共同基线。

## 5. 推荐总体设计

```text
                         网站控制面（可选）
          账号 / 邀请 / 设备 / 下载 / Pack / 公共知识 MCP
                              │
                              │ 不托管个人 Markdown 正文
                              ▼
外部 Agent ── CLI / Knowledge MCP / Production MCP ─┐
                                                    │
薄 Desktop ── 临时 loopback Desktop API ───────────┤
                                                    ▼
                    独立 AllToNote Runtime
       Core/SDK + Job/Checkpoint + Review/Publisher + Grants
                  │              │
        Recipe / Adapter Packs   │
  Video / Web / Docs / Code / Personal
                  │              │
                  ▼              ▼
           raw/personal       wiki/personal
                  └──── 开放 Markdown Vault ────┘
                              │
             Obsidian / Git / 编辑器 / 其他 Agent
```

### 5.1 应保持在 Core 的能力

- 领域 ID、Source/Artifact/Evidence/Bundle；
- Job/Checkpoint/ExternalOperation；
- Recipe 调度合同；
- ModelExecutor/AgentExecutor 端口；
- Review/PublishPlan/Publisher；
- Workspace Grant 与安全路径解析；
- 稳定 CLI/JSON envelope；
- 质量 Gate 的组合框架。

### 5.2 应保持在 Pack/Adapter 的能力

- yt-dlp/平台下载器；
- FFmpeg/Whisper/GPU 模型；
- Playwright/浏览器捕获；
- Readability/Tika/Pandoc/LibreOffice/OCR；
- 各 LLM/Agent Provider；
- Tree-sitter/clangd/UE5 专用分析器；
- 远端公共知识连接器。

### 5.3 应保持在客户端的能力

- Desktop：选择 Vault、浏览/阅读/搜索、任务和审阅 UI；
- CLI：完整自动化面与人类可读输出；
- MCP：受控暴露 read 或 production 能力；
- 网站：账号、分发、设备、公共资产，不实现本地 Pipeline。

## 6. 关键非目标

在出现真实证据前，不设计或实现：

- 通用可视化 Workflow/DAG 编辑器；
- 任意第三方公开插件市场；
- 常驻 daemon 作为所有命令的前置条件；
- 云端个人 Markdown 正文同步服务；
- 用向量库替代 Markdown；
- 一个 MCP Server 同时拥有无限制读取和生产/发布权限；
- 自动发布到 `wiki/common`；
- 让 Agent 在没有固定 revision 和 Evidence 的情况下生成正式代码知识；
- 把 Desktop UI 状态、JobStore 或索引写进同步 Vault。

## 7. 研发顺序推论

合理顺序不是按“最炫功能”排序，而是按风险闭合：

1. 收敛 Runtime/CLI、Video 发布和真实验收；
2. 完成 Vault Core/CLI/薄 Desktop，只读闭环；
3. 完成 Review/Publisher，形成第一条从生产到正式知识的闭环；
4. 提供只读 Knowledge MCP，让 Agent 安全消费已发布知识；
5. 用 Article/Wiki、Document 两类非视频 Recipe 验证扩展合同；
6. 只有出现真实跨进程长任务需求后，启用 on-demand Engine 和 Production MCP；
7. 再做 Codebase/UE5 与 Personal Digest；
8. 本地产品稳定后建设网站控制面；
9. Windows Tier 1 通过发布 Gate 后，再完成 macOS Tier 2。

其中第 5 步可先以同步/前台 CLI + durable Job 工作，不必等待 Engine。这样既验证多 Recipe 架构，又避免过早建设 daemon。

## 8. 调研结论

当前上位方向是合理的，但必须做三项修正才能成为真正可执行的最佳方案：

1. 把“独立 Runtime”具体化为稳定 CLI、机器级状态、系统凭据、Feature Pack、版本握手和组合安装器，而不是一句部署口号；
2. 把消费知识的 MCP 与生产知识的 MCP 完全拆开，前者可先做且默认只读，后者延后到 Engine/授权/预算成熟；
3. 不在 Video 之后立刻抽象公开插件系统，先用 Web 和 Document 两种 Recipe 验证最小内部扩展合同，再冻结第三方 SDK。

完成这些下位设计后，AllToNote 可以同时做到：桌面轻、CLI 一等、数据开放、长任务健壮、重依赖可选、未来 Agent 可接入，并且不会为了远期想象把当前产品过度复杂化。
