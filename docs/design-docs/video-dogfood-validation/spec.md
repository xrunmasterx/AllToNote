# Video 三样本 Pilot 验证规格

```yaml
doc_type: design
status: active
authority: stage
upstream:
  - ../../README.md
  - ../../tasks/alltonote-master-tasks.md
  - ../../tasks/alltonote-design-coverage-matrix.md
  - ../../superpowers/specs/2026-07-13-alltonote-knowledge-compiler-architecture-design.md
downstream:
  - tasks.md
  - samples.md
  - report.md
implementation_status: pilot-reliability-gap-user-value-3-of-3-after-terminology-fix
last_verified_at: 2026-07-30
```

## 0. 结论

用户于 2026-07-30 明确把最终输入收缩为 3 个 Bilibili 视频，不再提供独立转录校准集或额外 10 样本。本阶段因此从统计性 Dogfood 改为范围更窄的三样本 Pilot：

> 使用当前 AllToNote Runtime，把 3 个真实中文技术视频逐个转换成可检查、带证据的 Markdown，并报告每个视频的真实结果和失败边界。

Pilot 只有 `3/3` 才算系统链路通过；`2/3` 或更低均视为未通过。因为样本量小且全部是 Bilibili 短视频，结果不能外推为平台字幕、本地文件、长视频、干净安装或广泛用户价值已经验证，也不能仅凭 `3/3` 自动启动 Document、Publisher、完整 C0 或 Engine。

截至 2026-07-30，首次正式运行并非 `3/3`：V01 暴露无字幕媒体回退缺失，V02、V03 首次成功。两轮允许的最小修复完成后，在全新隔离 Workspace 原样顺序重跑达到系统 `3/3`。用户首次审阅又发现 canonical Draft 的 Evidence 脚注被直接作为默认阅读表示，造成正文编号墙和底部长列表；该呈现缺陷已用不改写 canonical Artifact 的阅读投影修复。用户随后首次判定 V01、V02 通过，V03 因 ASR 专有名词污染不通过，当时用户价值为 `2/3`。后续跨样本术语修正已在新的隔离 Workspace 对 V02 DIRECT 与 V03 MAP_COMPOSE 客观验收通过；用户重新审阅新 V03 reading 后明确判定通过。因此当前结论是 **Pilot reliability gap / 修复后技术 `3/3` / 修正后用户价值 `3/3`**。原 Pilot Bundle 和首次用户判定继续作为历史证据，不回写；`3/3` 仍不能自动扩大产品或架构范围。完整证据见 [`report.md`](report.md)。

## 1. 冻结样本

权威登记：[`samples.md`](samples.md)。

样本固定为：

- `V01`：Kimi K3 + Freqtrade 量化策略，约 8 分 55 秒；
- `V02`：从零实现 Agent 第一期，约 4 分 37 秒；
- `V03`：Orca ADE 多 Agent 编程工作流，约 11 分 32 秒。

三条 URL 均已规范化为无分享追踪参数的 canonical Bilibili URL。Bilibili 官方匿名字幕接口返回成功但字幕列表为空；正式 Runtime 仍必须执行自己的字幕发现流程，若带用户 Cookie 能发现可靠字幕则优先使用，否则进入本地转录。

测试开始后不得替换失败样本。获取、字幕、下载、转录、生成、恢复或 Artifact 失败都属于该样本的真实结果。

## 2. Pilot Golden Path

| 维度 | 固定边界 |
|---|---|
| 输入 | 仅 `V01` 至 `V03` 三个 Bilibili canonical URL |
| 交互 | CLI-first |
| 活跃任务 | 同一时间一个 Job，按 V01→V02→V03 顺序 |
| LLM | Codex app-server + `gpt-5.6-sol` |
| 字幕 | Runtime 可靠字幕发现优先 |
| 本地转录回退 | `faster-whisper small`、CPU、int8 |
| 输出 | 本地 Markdown/Artifact；用户之后明确接受或拒绝 |
| 发布 | 不自动写 iWiki/common |
| 环境 | 当前 Windows 开发机源码环境 |

本机具有 Ryzen 9 9950X、32 逻辑处理器和 RTX 5080。Pilot 仍固定 CPU/int8，是为了避免把 CUDA/cuDNN/ctranslate2 兼容性引入三条短视频的变量；总输入时长约 25 分钟，`small` 的 CPU 成本可接受。`tiny` 仅保留为安装 smoke，不作为质量模型。

由于没有独立校准集：

- 不在这 3 个样本之间比较或挑选模型；
- 不根据单条结果切换模型/参数；
- 不调整 Prompt/Recipe 来迎合某个样本；
- 若固定配置无法运行，先报告环境/兼容失败；任何修复后重跑单独标记。

## 3. 数据与隐私

- 三条输入是用户主动提供的公开 Bilibili URL；
- 用户已明确接受本地工作流可把必要的非敏感文本提交给选定的在线 Codex/OpenAI 模型；
- 输入、下载、转录、运行状态、Artifact 与评分保存在本机；
- 不增加远程遥测、分析平台或第二家云端服务；
- 文档只保存 canonical URL、BVID 和必要元数据，不保存分享追踪参数、Cookie、Token、完整字幕或模型原始响应；
- 若 Runtime 必须使用用户 Cookie，只使用现有受控凭据机制，不在命令输出或 Git 中展开内容。

## 4. 运行不变量

1. 先建立可复现环境，再运行真实输入；
2. 每次只运行一个 Job；
3. CLI 明确报告进度、错误、取消、恢复和结果路径；
4. 恢复后不产生不可解释的重复下载、转录或付费模型调用；
5. Artifact 原子完成，不把半提交文件当成功；
6. 重要结论能回溯到 Evidence、字幕/转录片段或时间戳；
7. 第一次运行结果与修复后结果分开记录；
8. 确定性缺陷先固定复现或失败测试，再做最小修复；
9. canonical Draft 保留 Evidence 引用用于质量 Gate 和审计；默认阅读表示隐藏系统 Evidence 脚注及定义，显式审计表示可恢复完整引用；
10. 阅读投影不得改写 committed Bundle，不得删除普通用户脚注、代码或转义字面量；
11. 不借 Pilot 增加 Document、并发 Job、Engine、GUI、多 Provider 或公共 SDK。

## 5. 结果 Oracle

### 5.1 系统结果

每个样本记录：

- canonical URL、BVID、标题、时长；
- 字幕发现结果与实际转录来源；
- 转录模型、device、compute type；
- LLM 模型、Prompt/Recipe 版本；
- 各阶段状态、耗时、重试、恢复和外部调用次数；
- Artifact/Markdown 路径、哈希和证据统计；
- 首次结果、修复后结果和错误分类。

单样本系统通过必须同时满足：

1. 首次正式运行到达成功终态；
2. Markdown/Artifact 可以打开；
3. 5 个重要结论可以回溯到证据或时间戳；
4. 未发现致命编造、错误来源归属或损坏产物；
5. 未出现不可解释的重复付费调用。

### 5.2 用户价值结果

系统生成后由用户完成：

- 是否愿意保留；
- 达到可保留质量所需编辑时间是否不超过 10 分钟；
- 是否发现致命编造或错误归因；
- 接受进入 Vault或拒绝，并说明首要原因。

默认评分对象是 `draft show` 的 `reading` 表示，不是 canonical 审计稿。需要核对来源时可使用 `draft show --presentation audit`；两种表示来自同一 committed Draft，阅读投影不构成新的权威 Artifact，也不改变 EvidenceSet 或质量报告。

7 天保留作为后续观察字段，不阻塞本轮立即交付三份技术结果。

### 5.3 Pilot Gate

| 结果 | 结论 |
|---|---|
| 首次系统通过 `3/3`，用户价值通过 `3/3` | Pilot PASS；仍需用户明确决定是否进入更大样本/可信复用阶段 |
| 首次不足 `3/3`，修复后 `3/3` | Pilot reliability gap；先报告首要缺陷和修复成本，不扩范围 |
| 修复后仍不足 `3/3` | Pilot FAIL；停止架构扩张，报告失败根因 |

`3/3` 不等价于原先的 `8/10` 产品价值证据，也不证明字幕优先、本地文件、长视频或干净 Windows 安装。

## 6. 执行阶梯

```text
冻结 V01–V03
  -> 环境预检与固定转录配置
  -> 逐个运行 V01、V02、V03
  -> 首次结果汇总
  -> 必要时只修复一个首要根因，最多两轮
  -> 原样本复测
  -> 用户人工价值评分
  -> 三样本最终报告
```

环境准备、执行与必要修复最多使用 3 个工作日；7 天保留仅作为被动跟踪，不延长当前技术结果交付。不能以“再加一个功能”为理由扩展范围。

## 7. 成功后的边界

即使 `3/3`，下一阶段也不是自动开工：

```text
三样本 Pilot 3/3
  -> 用户显式决策
  -> 若继续验证产品：Video 可信复用
  -> 可信复用成立后：一个 born-digital PDF + 最小 X0-B
  -> 后续能力由真实瓶颈重新 admission
```

既有 X0-A、X0-B、Video、Document、Review/Publisher、Release、C0 和 Engine 技术规格继续有效；本规格只控制当前实施 admission。

## 8. 工作树与变更边界

- 集成基线：`G:\AllToNote` / `master`；
- 阶段分支：`codex/video-dogfood-validation`；
- 阶段工作树：`G:\.worktrees\AllToNote\video-dogfood-validation`；
- 历史 `G:\AllToNote-video-producer` 不再是活跃路径；
- `codex/iwiki-readonly-client` 保持隔离；
- `master` 的 `AGENTS.md` 与 `config/` 不提交、不移动、不清理；
- 当前允许创建被 `.gitignore` 排除的阶段虚拟环境、Runtime state 和下载/模型缓存；正式结果只记录脱敏路径、哈希和指标；
- 不 stage、commit、push、merge 或发布，除非用户另行授权。
