# AllToNote Video Producer 发布收敛计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-14-alltonote-video-producer-design.md
  - ../specs/2026-07-16-alltonote-long-video-knowledge-compilation-design.md
  - ../specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - ../specs/2026-07-14-alltonote-portable-artifact-source-bundle-design.md
implementation_status: vrel-00-03-evidence-recorded-vrel-04-through-11-open
last_verified_at: 2026-07-31
```

## 0. 已完成、不得重做

当前实现工作树已有：

- Video Source/Transcriber/Model/Portable ports/adapters；
- durable Job/Checkpoint/ExternalOperation；
- Knowledge Note v2：Transcript Quality、deterministic chunk plan、Knowledge Map、Global Compose、Citation Freeze、Quality/Repair；
- Faithful Edition 独立 Draft/Quality；
- 同 Bundle 多 Draft 原子提交；
- 真实 65 分钟缓存 transcript 编译、iwiki commit、restart zero replay；
- 1731 passed/2 skipped 的历史全回归基线和专项 review 记录。

本计划不重新发明长视频 compiler、分块、Prompt 或 Bundle。目标是把现有核心变成可正式交付的 Video 产品面。

## 1. 当前唯一外部阻塞

指定 YouTube URL 的实时 acquisition 被平台 anti-bot 拒绝，即使使用用户导出 Cookie 和 JS runtime 仍返回登录/机器人确认。除非 Cookie/IP/平台 Adapter 条件变化，不反复重试；缓存 transcript E2E 不能冒充实时 URL 获取成功。

YouTube 阻塞独立记录，不应阻止 Bilibili、本地视频和 acquisition 之后的编译器发布证据。

## 2. 成功标准

- Bilibili 平台字幕真实路径通过；
- 本地视频 + faster-whisper 真实路径通过；
- 字幕优先不下载媒体/不加载 Whisper；
- 无字幕按策略回退；
- CLI/JSON/job/cancel/restart 正式稳定；
- Bundle semantic validate/iwiki commit/ReviewCandidate 通过；
- Windows clean machine/Pack 组合可执行；
- YouTube 若仍阻塞，错误准确且无假成功；
- 发布验收摘要脱敏、可复核。

## 3. Task VREL-00：现状冻结与差异审计

步骤：

1. 运行 `git status/branch/log`，不清理工作树；
2. 保存当前全量测试结果和时间；
3. 对照 Video/Long Video/Portable/Runtime 设计建立 release matrix；
4. 列出 legacy Downloader/Transcriber 与新 Adapter 的实际调用链；
5. 确认 runtime-lock/iwiki schema/hash；
6. 确认 65 分钟验收缓存位置只作为受控 fixture，不纳入仓库私密正文；
7. 将差异记录到任务清单，不先重构。

Gate：每个发布项明确 `pass/fail/blocked/not-run`，没有“应该可以”。

## 4. Task VREL-01：CLI 与 ProduceRequest 收敛

依赖 Runtime CLI RCP-00/01，目标文件以当前 `backend/app/cli/main.py`、`video_service.py` 为基线。

步骤：

1. 固定命令：`alltonote produce video --input ... --workspace ...`；
2. 保留必要兼容 alias，输出弃用 warning，不维护两条业务链；
3. 明确 profile、draft kinds、language policy、credential/model/transcriber profile refs；
4. `--json` 返回 Job/Bundle/Draft/Quality refs；
5. 前台 Ctrl+C/cancel；
6. validation/capability/acquisition/transcription/model/quality/commit 错误映射；
7. idempotency/request persistence；
8. 终态 retry 新建 Job。

测试：扩展 `backend/tests/cli/test_produce_video_cli.py`，golden 参数/JSON/exit codes。

## 5. Task VREL-02：Bilibili 字幕黄金路径

目标：真实公开、合法可访问视频，优先平台字幕。

步骤：

1. 选择稳定短/中/长公开测试样本并记录 ID，不保存 Cookie；
2. metadata inspect；
3. 选择人工/平台字幕；
4. 证明未下载视频/音频、未加载 FFmpeg/Whisper；
5. normalize Transcript，保存 source language/segment/timing；
6. balanced Knowledge Note + 可选 Faithful Edition；
7. Bundle validate/commit；
8. restart 证明 acquisition/model zero replay；
9. 对无字幕视频验证受控音频回退或明确 capability missing；
10. Cookie 过期/地区/风控失败分类。

目标测试：现有 legacy adapter contract + 新 `backend/tests/integration/test_bilibili_video_golden_path.py`，真实 smoke 使用 opt-in marker，不让普通 CI 依赖网络。

## 6. Task VREL-03：本地视频 + faster-whisper 黄金路径

步骤：

1. 使用仓库内小型许可 fixture 和一个受控较长真实文件；
2. Local Root Grant/canonical path/reparse point；
3. FFmpeg probe/audio extraction；
4. CPU faster-whisper 模型能力/preflight；
5. transcript segment/timing/language/quality；
6. 取消、磁盘不足、模型缺失、FFmpeg crash；
7. checkpoint 后 restart 不重做已完成转写；
8. Draft/Bundle/iwiki commit；
9. 原视频 hash 前后不变；
10. Pack/tool/model version 进入 Receipt。

Gate：clean Windows CPU 环境可完成；GPU 不是基础发布前置。

## 7. Task VREL-04：字幕/转写决策矩阵

为每个平台测试：

```text
高质量人工字幕
自动字幕
多语言字幕
字幕为空/损坏
无字幕有音频
字幕和音频都不可用
```

步骤：

1. 固定 selection policy；
2. 用户显式语言优先级；
3. 自动字幕质量标记；
4. 无字幕只在 Pack/预算允许时回退；
5. 不因字幕质量低就静默捏造内容；
6. Transcript Quality 决定 fast/balanced/thorough 路径和 warning；
7. selection 进入 Source/Receipt/Quality；
8. fixture/golden contract。

## 8. Task VREL-05：YouTube preflight 与阻塞语义

除非外部条件变化，本任务不继续穷举 Cookie。实现/验证：

1. metadata/subtitle/audio acquisition 错误保留平台分类；
2. `AUTH_REQUIRED`、`ANTI_BOT`、`GEO_RESTRICTED`、`VIDEO_UNAVAILABLE`、`NETWORK` 区分；
3. Cookie 只经 secret/file ref，内容不进 argv/log/Job/Bundle；
4. yt-dlp/JS runtime version 进入 doctor；
5. 用户动作明确：刷新认证、换网络/合法来源、本地文件；
6. 失败不创建无 Source Draft；
7. 若未来条件变化，执行一次无缓存完整 URL E2E；
8. 结果独立写 acceptance，不能改写历史缓存验收。

## 9. Task VREL-06：短/中/长与 Profile 发布矩阵

样本至少：3–5 分钟、20–40 分钟、65 分钟、2–3 小时 synthetic/真实许可 transcript。

验证：

- 短内容不走复杂 map/compose；
- balanced 中长内容调用数/时间适中；
- thorough 提升 coverage 而不是重复重写；
- Knowledge Note 与 Faithful Edition 独立；
- 默认来源语言、显式中文翻译；
- 1 H1、章节不重复、全局连贯；
- Evidence/coverage/quality；
- targeted repair 上限；
- 模型失败/重启/结果复用；
- 成本/调用/耗时记录。

2–3 小时真实模型验收可昂贵，先用 deterministic/fake 验证规划，再用用户批准的预算跑一次真实样本；Fake 不能作为最终质量证据。

## 10. Task VREL-07：截图与多模态边界

只验证现有设计要求，不扩张新功能：

1. `include_screenshots=false` 时不调用 FFmpeg；
2. 截图计划与 Evidence time range 对齐；
3. 截图失败是否允许 Draft 继续由 profile/policy 明确；
4. WebP/MIME/dimension/hash/size 安全；
5. 不把追踪/临时绝对路径写 Markdown；
6. 多 Draft 引用一致；
7. Bundle asset validation。

## 11. Task VREL-08：恢复、取消与故障注入

在以下边界 kill/exception：

- metadata 前后；
- subtitle/audio download；
- transcribe chunk；
- knowledge map 每次调用；
- compose；
- faithful compile；
- Quality/repair；
- Bundle staging/validate/iwiki commit；
- 成功结果持久化前后。

Gate：

- 已验证 Artifact/Model result 不重复；
- outcome unknown 不自动付费重试；
- terminal Job 不复活；
- cancel 不留下半 Bundle；
- iwiki commit reconcile；
- restart 可由 CLI 查询并继续/解释。

## 12. Task VREL-09：Runtime/Pack 集成

依赖 RCP-08/09。

步骤：

1. 字幕纯路径只需 downloader capability；
2. media-basic Pack 包含/定位 FFmpeg/yt-dlp/JS；
3. transcribe-cpu Pack 独立；
4. model profiles/credentials 独立；
5. preflight 显示下载大小/磁盘/能力；
6. running Job pin versions；
7. Pack 缺失/损坏/更新 rollback；
8. SBOM/license，特别核对 bundled yt-dlp/FFmpeg/Whisper 的分发许可。

## 13. Task VREL-10：ReviewCandidate 接口

不在 Video 内实现 Publisher。只保证：

1. Bundle 中每个 Draft/Quality/Source/Evidence 可被 Review service 投影；
2. Knowledge/Faithful 独立 candidate/hash；
3. publish eligibility 准确；
4. 低置信自动字幕/翻译 warning 可见；
5. Candidate 默认目标 personal；
6. 不直接写 wiki。

## 14. Task VREL-11：全回归与真实验收摘要

运行：

```powershell
pytest backend/tests -q
pytest backend/tests/integration -q
pytest backend/tests/cli -q
```

另行运行标记的 Windows/网络/真实模型 smoke，并记录：

- input identity（不含私密 URL query/Cookie）；
- transcript segments/duration；
- model calls/waves/repair；
- elapsed/resource/cost category；
- Draft headings/evidence/quality；
- Bundle/iwiki receipt refs；
- restart replay counts；
- Runtime/Pack versions；
- blocked external platform。

输出：`docs/acceptance/video-producer-v1.md`。不复制完整 transcript、Draft、Prompt、Cookie 或 provider raw。

## 15. 发布判定

可以宣称：

- “Bilibili 支持”只有 Bilibili真实 smoke通过；
- “本地视频支持”只有 clean-machine CPU path通过；
- “YouTube 支持”只有实时无缓存 URL path通过，否则标 beta/blocked；
- “2–3 小时长视频”只有至少一次对应规模真实质量/恢复验收；
- “Windows 可用”还需平台发布计划安装/签名 Gate。

不得用单元测试、Fake、缓存 transcript 或旧 BiliNote UI 成功替代这些声明。

## 16. 建议执行顺序

```text
VREL-00
 -> VREL-01
 -> VREL-02 + VREL-03
 -> VREL-04/05
 -> VREL-06/07
 -> VREL-08
 -> VREL-09
 -> VREL-10
 -> VREL-11
```

YouTube 保持外部阻塞时，跳过实时成功 Gate并继续其余任务；一旦外部条件变化，只补跑 VREL-05 的真实链路和相应回归。

## 17. Wave 1A 接缝记录（2026-07-18）

- `VREL-00` 和 `VREL-01` 已完成；逐项 release matrix、调用链、runtime-lock、CLI compatibility、request hash、profile refs、Ctrl+C/cancel 和测试证据见 [`Runtime/CLI Wave 1A acceptance`](../../acceptance/2026-07-18-runtime-cli-wave-1a-baseline.md)。
- canonical 命令为 `alltonote produce video --input ... --workspace ...`；位置参数作为带 deprecation warning 的同链 alias 保留。
- Video v1/v2、normalized `requested_outputs`、Faithful language policy、Job request persistence/hash、checkpoint/recovery、ModelExecutor 和 Portable/iwiki 合同均无退化。
- 截至 Wave 1A，`VREL-02` 至 `VREL-11` 尚未执行。后续进展见下一节；YouTube 实时 acquisition 继续明确 `blocked`，不得用缓存或其他平台结果冒充成功。

## 18. 2026-07-31 发布候选接缝记录

- `VREL-02`：三条用户提供的真实 Bilibili 输入已通过签名 `media-basic` / `transcribe-cpu` 组合、Portable commit 和用户结果 Gate，证据见 [`Official Video Packs 与真实视频验收`](../../acceptance/2026-07-31-official-video-packs.md)。
- `VREL-03`：项目自建 12 秒英文语音 MP4 已在提交 `34cbf9550edae6fabae1fd13c04cc623bd5c401b` 导出的 Runtime wheel 中，通过签名 Pack 的真实 `ffprobe`、`faster-whisper small/cpu-int8`、Codex、semantic validate、commit 与新进程零重放；源文件哈希保持不变。
- `VREL-03` 尚未关闭完整发布 Gate：机器状态卷现在会在媒体快照写入前按已打开源文件长度执行容量快照检查，源文件复制增长也被该长度硬上界阻断；这不是空间预留，真实发布环境仍需补独立非管理员 Windows 用户或 VM、受控较长本地文件，以及取消、预检后磁盘耗尽、模型缺失和 FFmpeg crash 矩阵。
- `VREL-04` 的核心安全决策已冻结：用户提供稿件优先；有效平台字幕不下载媒体；只有已确认无字幕或平台明确不支持字幕时才允许在转写能力存在的前提下回退；字幕状态未知返回可重试失败且不启动媒体下载；损坏字幕直接失败且不伪装成“无字幕”。Bilibili 当前默认选择顺序已冻结为人工中文、AI 中文、其他首个可用字幕；用户显式语言偏好、自动字幕质量标记和各平台真实矩阵仍未完成。
- `VREL-09` 的当前版本签名 Pack 安装、解析、动态 doctor、Job 精确 digest 冻结与恢复已通过；通用更新、rollback、卸载、Desktop Resolver 和公开分发仍在后续 Runtime/平台发布范围。
- `VREL-11` 当前完整 backend 为 `2240 passed, 2 skipped, 3 warnings, 3 subtests passed`；机器状态卷容量切片的独立 Gate Review 为 0 P0 / 0 P1。这只证明当前代码回归和上述有限真实输入，不等于 VREL-04 至 VREL-10 全矩阵完成。
- 新 Workspace 的 strict validate 仍会报告缺少 `wiki/common/index.md`；本次提交的 Portable Bundle semantic validate 为 `valid=true, issues=[]`。Workspace 严格初始化属于 Workspace-init 后续合同，不在 Video Produce 中隐式修补。
