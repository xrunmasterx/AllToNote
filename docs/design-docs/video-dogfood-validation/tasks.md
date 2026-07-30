# Video 三样本 Pilot 任务

> 来源：本目录 `spec.md`
> 状态：Tasks 0–7 completed；修正后系统与用户价值均 `3/3`，保留 reliability gap 分类
> 执行规则：先取得证据，再做最小修复；一次只运行一个真实 Job
> 执行时间盒：最多 3 个工作日；7 天保留另行观察

## Task 0：[x] 落地权威文档与隔离工作树

- [x] 创建 `codex/video-dogfood-validation`；
- [x] 使用 `G:\.worktrees\AllToNote\video-dogfood-validation`；
- [x] 更新 master tasks、coverage matrix、README 和 Local Parallel admission；
- [x] 文档链接、围栏、空白和 `git diff --check` 通过；
- [x] 没有产品代码变更；
- [x] `master` 的 `AGENTS.md`、`config/` 与 iWiki worktree 未被触碰。

## Task 1：[x] 冻结 V01–V03 与 Pilot Oracle

权威输入：[`samples.md`](samples.md)。

- [x] 三个 BVID、canonical URL、标题和时长已通过 Bilibili 官方接口确认；
- [x] 分享追踪参数未进入登记；
- [x] 匿名官方字幕接口成功但三条字幕列表均为空；
- [x] 样本固定为 V01–V03，测试后不得替换；
- [x] Gate 固定为首次系统/用户价值 `3/3`；
- [x] 记录 `3/3` 不能外推为 10 样本、字幕、本地文件、长视频或干净安装验证。

## Task 2：[x] 建立可复现环境并冻结执行配置

当前已知：

- 系统 Python 缺少 `SQLAlchemy`、`yt_dlp` 与 `faster_whisper`；
- `alltonote` console script 尚未安装；
- FFmpeg 8.1.2 缓存与 Codex CLI 0.146.0 存在；
- 本机为 Ryzen 9 9950X + RTX 5080；Pilot 固定 Whisper `small` + CPU/int8。

范围：

1. 在阶段 worktree 创建被忽略的 Python 3.11+ 虚拟环境；
2. 安装锁定的项目/运行依赖；
3. 安装 editable `alltonote-runtime`；
4. 运行 `alltonote runtime info/doctor/capabilities`；
5. 验证 Codex app-server 可启动且冻结 `gpt-5.6-sol`；
6. 冻结 Runtime state、Artifact 输出和本地缓存目录；
7. 执行受影响的聚焦测试，不先改产品代码。

验收：

- [x] CLI 可启动；
- [x] yt-dlp/Bilibili patch 可导入；
- [x] Faster Whisper small CPU/int8 可加载；
- [x] FFmpeg 可执行；
- [x] Codex app-server 握手通过；
- [x] 配置中不出现 Secret/Cookie 明文；
- [x] 记录精确版本和环境缺口。

## Task 3：[x] 逐个运行 V01–V03 首次结果

前置：Task 2 PASS。

对每个样本：

1. 使用 canonical URL 提交一个 Job；
2. 等待终态，不并行；
3. 记录字幕发现、下载、转录、模型调用、Artifact 和耗时；
4. 检查 5 个重要结论的 Evidence/时间戳；
5. 记录首次结果后才进入下一样本；
6. 不因失败替换视频或切换 Provider/模型。

结果：V01 首次失败，V02、V03 首次成功，首次系统结果为 `2/3`，未通过首次 Gate。

## Task 4：[x] 条件式首要根因修复

触发：任一样本首次失败。

规则：

- 每轮只修复排名第一的共同/阻断根因；
- 先固定失败测试或可复现证据；
- 只做直接必要的最小变更；
- 在原样本复测；
- 首次与修复后结果分开；
- 最多两轮。

允许：环境/依赖、Bilibili 获取、固定本地转录、Codex app-server、取消/恢复、幂等、Artifact 原子性和 Evidence 追溯的确定性缺陷。

禁止：换样本、逐视频调参、Document、Publisher、完整 C0、多 Job、Engine、GUI、多 Provider 或公共 SDK。

执行结果：

- 修复循环 1：补齐无平台字幕时的媒体下载、本地转写与 yt-dlp Bilibili patch `fatal` 关键字兼容；
- 修复循环 2：回执记录真实生成式转写器身份，并保留 retry 父 Job ID；
- 两轮均先增加失败测试，再做最小实现；受影响回归 85/85、完整 backend 回归 1908 passed / 2 skipped / 3 subtests passed；
- 全新最终 Workspace 原样顺序重跑达到系统 `3/3`，三个 Bundle 均通过 iwiki semantic validate；
- 没有第三轮修复，也没有修改 Prompt、Recipe 或逐样本参数。

## Task 4A：[x] 修正 Evidence 默认呈现

触发：用户首次审阅三份结果时确认，正文连续脚注编号和文末数百条 `Video 时间段` 定义对普通阅读无用，并显著干扰成品感。

边界：

- canonical Draft、EvidenceSet、Quality Report 和 Bundle 哈希继续作为审计权威，不回写、不迁移；
- 新增确定性 `reading` 投影，只移除可见的系统 `ev_UUID` 脚注引用和对应定义；
- 普通脚注、行内代码、围栏代码、转义字面量保持原样；
- `draft show` 默认输出阅读版，`--presentation audit` 显式输出 canonical 审计稿；
- 这属于用户价值呈现修正，不计作第三轮 acquisition/ASR 技术修复，也不重跑转录或模型。

验收：

- [x] 聚焦测试 `19 passed`；
- [x] 完整 backend 回归 `1913 passed, 2 skipped, 3 subtests passed`；
- [x] V01/V02/V03 阅读版 Evidence 标记分别从 `654/364/122` 降为 `0/0/0`；
- [x] canonical Evidence ID 仍为 `327/182/61`，三个 Draft SHA-256 不变；
- [x] 三份真实 Workspace 回放前后文件快照一致。

## Task 5：[x] 用户人工价值重新评分

第一次评分已识别出默认 Evidence 呈现缺陷，不能记为用户价值 PASS。Task 4A 完成后，每个成功产物改用 `reading` 表示由用户重新确认：

- 是否愿意保留；
- 编辑到可保留质量是否不超过 10 分钟；
- 5 个重要结论是否可追溯；
- 是否存在致命编造或错误归因；
- 接受进入 Vault或拒绝及首要原因。

客观复审已完成并写入 [`report.md`](report.md)：V01 倾向通过，V02 临界，V03 因 15 处/6 类专有名词污染而按 `≤10 分钟` Oracle 倾向不通过。该结论只是代理建议，Task 5 仍必须由用户确认，不能自动代填。

用户最终确认：V01 通过、V02 通过、V03 不通过。用户价值结果为 `2/3`，没有达到 Pilot 价值 Gate；V03 的首要拒绝原因是 ASR 专有名词污染。

7 天后可追加“仍保留/已删除/判定无用”，但不阻塞当前技术结果交付。

## Task 6：[x] 三样本最终报告与后续决策

报告必须包括：

- 首次与修复后 `V01–V03` 逐项结果；
- 环境、版本、命令、耗时、Job/Artifact ID 与脱敏路径；
- 字幕/转录来源、模型调用次数和 Evidence 质量；
- 系统 `3/3` 与用户价值 `3/3` 是否分别成立；
- 失败根因、修复成本和仍未覆盖的范围；
- 明确声明三样本不能证明 10 样本、字幕路径、本地文件、长视频、干净 Windows 或公开发布。

完成后必须由用户显式决定：停止、扩大 Video 验证、进入可信复用，或另行调整路线；不得从 `3/3` 自动启动 X0-B、Publisher、C0 或 Engine。

技术报告已完成：[`report.md`](report.md)。用户已显式决定下一步先修专有名词识别；不扩大 Video 样本，不启动 X0-B、Publisher、C0 或 Engine。

## Task 7：[x] 专有名词保守归一化

已确认的实现边界：

- canonical Transcript 继续保存平台字幕或 ASR 原文，不把模型猜测回写成来源事实；
- Knowledge Map 与所有拓扑共用的 Global Composer 识别疑似 ASR 专有名词；
- 单一 canonical 匹配清楚时统一 spelling；存在多个合理候选时保留原词并显式标注不确定性，不输出“被转写为”等编辑过程元话语；
- 不联网查词，不引入全局产品词典、逐视频替换表、新模型阶段或转写协议扩展；
- 提示词行为必须版本化，现有 Bundle 和 Pilot 原始结论不回写。

验收：

- [x] 先增加 DIRECT 与 MAP_COMPOSE 提示词回归测试，再实现；
- [x] 编译聚焦测试 `92 passed`、Runtime/集成 `77 passed`、完整 backend `1913 passed, 2 skipped, 3 subtests passed`；
- [x] V02 隔离重跑得到 `DeepSeek=1`、`OpenClaw=3`、`Claude Code=2`，`DBC/OpenCrawl/OpenCrew/Cloud Code=0`；
- [x] V03 隔离重跑得到 `Claude Code=9`、`Amp=1`、`Grok=1`、`Issues=1`，`Cloud Code/Omapad/Growk/Asus=0`；
- [x] `隐形` 未被硬猜，语境合法的 `GitLab` 保留；reading 投影完整，两份 Bundle semantic validate 与 Workspace validate 均 valid。

第一次真实 V02 重跑结果：系统与 Portable Quality PASS，但术语验收 FAIL。正文仍保留 `DBC`、`OpenCrawl`、`OpenCrew`、`Cloud Code` 并逐一标注名称不确定，说明“仅凭标题/来源上下文，高置信才改”的规则把可识别的公共产品名也冻结成了 ASR 原词。该结果不启动 V03，避免无信息增益的付费调用。

修正后的最小策略允许模型使用已有的公共名称知识完成拼写归一化，但权限只限 spelling：单一 canonical 匹配清楚时必须改正；存在多个合理候选时才保留原词和不确定性；不得据此新增功能、归属、版本或其他事实。仍不联网、不加逐视频词表、不改 Transcript。

第二版真实验收已通过。V02 使用 DIRECT、1 次模型调用；V03 使用 MAP_COMPOSE、3 次模型调用与 2 个顺序波次；两者 repair 均为 0。新的 canonical Transcript 仍保留 ASR 原词，纠正只发生在 Draft。原用户价值结果继续记录为 V01/V02 PASS、V03 FAIL；用户重新审阅新 V03 reading 后明确判定 PASS，修正后用户价值为 `3/3`。这只关闭当前术语与用户重评任务，不自动扩大样本或启动其他阶段。用户随后显式授权按建议继续，但该授权只进入最小 Video 可信复用验证。
