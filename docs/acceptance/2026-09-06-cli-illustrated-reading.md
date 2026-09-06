# CLI 图文精读验收（2026-09-06）

## 范围

源码 CLI 新增 `--style illustrated-reading`，搭配 Recipe v2、`knowledge-note` 和
`--screenshot-policy on_demand`。本次没有修改前端 UI，没有重打包保留的 V20 二进制。
普通 `structured` 摘要模式保持原有行为。

输入是用户指定的 [YouTube 视频](https://www.youtube.com/watch?v=FOaPOP1GO_Q) 的已有本地副本，
不是 BibiGPT 截图中的 ZEC 视频。因此不声称完成了两个产品对同一视频的内容 A/B 测试。

- 文件：`local-data/workspaces/youtube-test-FOaPOP1GO_Q-20260820/source/FOaPOP1GO_Q.mp4`
- SHA-256：`f66de5d1a733dad899f4ad4e99a995d45a70abd454aa5fbcbd30211d079cfe9d`
- 视频时长：821.847 秒；原始画面：640×360。
- 执行器：源码可编辑安装的 `local-data/dev-envs/screenshot-fix/Scripts/alltonote.exe`。
- Provider：`composer` / `codex-app-server`；模型：`gpt-5.6-terra`。
- 环境：`ALLTONOTE_MACHINE_STATE_ROOT=G:\AllToNote\local-data\releases\runtime-v20-video-e2e\machine`。

## 实现与判据

字幕 → 有界时间采样候选帧 → 画面＋邻近字幕选图 → 全文＋所选图片编写 → 字幕边界计算章节时间
→ 结构检查 → 图片＋全文＋笔记复核 → 原位绑定资产与发布。

| 检查项 | 实现/验收要求 |
| --- | --- |
| 真正的多模态 | 图片字节通过内联 `image` 输入发送，不是只传路径或字幕；桥接与协议请求有测试 |
| 请求恢复 | 图片内容及顺序进入请求哈希；成功的选图、编写、复核调用均持久化，不重复支付调用 |
| 图文对应 | 只允许使用已经看过的候选帧，保留原位截图标记；不追加到末尾图库 |
| 时间对应 | 模型给出字幕段 ID 边界，代码计算时间；拒绝跨章图片/引用和倒序、重叠的段落范围 |
| 数字与视觉事实 | 区分讲者陈述与可见画面；不补造看不清的价格、单位、成交与盈亏 |
| 发布门槛 | 图文模型复核有实质性问题时失败，不发布草稿；报告区分确定性检查与模型检查 |

图片接口按照 OpenAI Docs 技能核对的 [App-server 官方文档](https://learn.chatgpt.com/docs/app-server)
接入，并用实际模型调用及协议单元测试验证。模型检查不是独立人工事实认证。

## 实跑与迭代

1. `job_01a0761d-ac38-7061-9d18-dcf3fa979746`：复核发现生成的章节时间范围重叠、部分终点超出本章。
   返回 `visual_review_failed`，没有发布。随后改为字幕 ID 边界，由代码计算时间。
2. `job_01a07624-42ae-7427-85f1-0ea3b6bea18d`：严格范围解析未兼容标题末尾的引用控制，返回
   `illustrated_section_range_invalid`。修复了该解析问题，并将范围顺序检查改为片段序号，
   避免 ASR 相邻片段约 1 毫秒的时间重叠造成误判。保存的第二轮正文经修正后可正确解析全部 10 章。
3. `job_01a0762b-61e0-7f48-9421-564c99237f11`：模型在截止于 `seg_000058` 的章节中引用了
   `seg_000063`，范围校验正确拒绝。增加有上限的一次章节布局修复，要求保留引用、覆盖账本和
   截图选择；修复仍不通过就停止，不放宽校验。布局修复使用原有修复额度，不再叠加一次文本修复。
4. `job_01a07632-3c08-7127-906a-27f8d25b7d64`：成功，9 章、12 张原位配图。
   开始于 10:09:58 UTC，完成生成于 10:14:42 UTC；这次使用 3 次模型调用，没有触发布局修复。
   模型复核原始结果为 `{"issues":[],"pass":true}`；Bundle 的 `overall=pass`、`publish_eligible=true`。
   这些是自动检查结果，不代表下述严格内容验收也已经通过。

## 最终产物与逐图检查

- Bundle：`bnd_01a07632-3c08-789f-998b-4c3af67e4e2c`
- 完整导出：`local-data/outputs/youtube-illustrated-FOaPOP1GO_Q-20260906/`
- 笔记：上述目录内 `drafts/art_01a07632-3c08-7b28-8b46-cf5ec470877e.md`
- 日常阅读版：`local-data/outputs/youtube-illustrated-FOaPOP1GO_Q-20260906-reading.md`。
  使用现有 `draft show --presentation reading` 输出，移除正文证据编号和末尾28条时间脚注；
  保留9章、12张图及图注，仅在末尾提供一个原始笔记链接。相对图片路径已适配阅读版位置，
  不复制图片，也不覆盖已提交 Bundle 或其完整导出副本。
- 笔记 SHA-256：`41e9b259df904a61eafed771de0084392cb73eb4a2661513ad31868423e5f7b0`
- 全部 20 个导出文件与原 Bundle 的 SHA-256 一致；12/12 图片链接指向 Bundle 内实际文件。
- 无末尾 `Screenshots` 图库，无未解析的截图/章节范围控制。
- 编译器质量汇总正确区分了 8 项确定性检查与 1 项模型检查，人工检查计数仍为 0。

代理实际逐张查看了全部 12 张 WebP，并对照完整正文：

| 图号 | 实际画面 | 邻近正文对应性 |
| --- | --- | --- |
| 1–2 | 台积电日线、黄色水平虚线 | 对应盘前图表背景与低开预期 |
| 3–4 | 初始买单、改价后的订单状态页 | 对应初始仓位和害怕错过而追价的复盘 |
| 5 | 日线上的向上箭头 | 对应讲者的后续走势预期；正文没有当成已实现走势 |
| 6–7 | 持续上涨的盘中图、“没有完美的入场”标注 | 对应趋势与交易管理说明 |
| 8–9 | 震荡盘中图、18E 真空效应课件 | 对应假突破与课程讨论 |
| 10–11 | 接近黄色线的日线、订单页 | 对应压力位分析与平仓陈述 |
| 12 | 台积电期权链 Calls/Puts 表格 | 对应两腿价差说明 |

**分层结论：流程、时间顺序和图文对应的基础验收通过；严格的数字与逐项证据验收尚未通过。**

剩余的具体问题不是图片位置，而是内容可信度：

- 正文仍把 ASR 的 `197.6768` 直接当作口述价格保留，未完成音频/画面的交叉核对。
  本次没有证据足以断言正确数值是什么，因此不能把该精度视为已经核实。
- 230/242/360 等期权报价虽有局部说明，但单位和小数点仍未完成一致核验。
- 某些长段引用的是起始片段，而非足以支撑全部细节的片段。例如开盘买入段只引用
  `seg_000016`（01:09.340–01:13.680），该段还包含后续才出现的到期日、行权价、81 张和230等信息。
  章节范围正确、覆盖账本闭合，并不能替代逐条事实的精确引用。

因此本次不宣称已经达到“不用回看视频即可相信所有数字”的质量，也不把模型自评通过当作这一保证。

## 回归测试

测试覆盖：图片实际传递、图片请求哈希与不支持的 provider、成功调用恢复、图文复核失败拦截、
未知选图拒绝、原位插图、代码块中的字面控制不被替换、字幕边界计算、跨章图片/引用与重叠范围拒绝，
以及此前截图生产链路、CLI、脱离终端执行与恢复回归。

最终源码相关回归：**412 passed in 114.14s**。执行的测试模块为：

```text
tests/core/test_model_call_coordinator.py
tests/core/test_video_compiler.py
tests/core/test_screenshot_plan.py
tests/core/test_illustrated_reading.py
tests/core/test_video_compilation_quality.py
tests/core/test_video_compilation_plan.py
tests/adapters/test_codex_app_server_bridge.py
tests/test_codex_app_server_client.py
tests/integration/test_local_media_golden_path.py
tests/integration/test_platform_subtitle_golden_paths.py
tests/cli/test_produce_video_cli.py
tests/adapters/test_ffmpeg_screenshots.py
tests/integration/test_fake_video_producer.py
tests/integration/test_video_detach_engine_e2e.py
tests/core/test_video_request_persistence.py
```

执行方式为在 `backend/` 下使用源码开发环境的 Python 运行 `-m pytest`、上述模块与 `-q`。

## 已知限制

候选帧仍是时间采样（约每 45 秒一个、最多 24 张），不是镜头检测，可能错过短暂画面。
当前要求 `codex-app-server` 和能容纳全文的直接编写预算；超预算明确失败，不静默退化成有损摘要。
360p 订单表的小字不足以可靠核实价格和成交。图文模型复核与编写使用同一模型，不能视作独立事实核验。
没有测试其他视频、其他模型、远程 URL 截图或重新打包后的安装版。
