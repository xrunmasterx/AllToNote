# CLI 视频截图接线修复与验收

日期：2026-09-06。范围：源码 CLI、本地视频、Recipe v2 知识笔记。

## 修复

- 生产 Runtime 接入已有 `FFmpegScreenshotAdapter`，使用签名媒体 Pack 的明确 FFmpeg 路径，不回退到系统 PATH。
- 任务恢复或重试时，从任务冻结的 Pack manifest 解析相同的 FFmpeg，不读取新 active generation 作为替代。
- 截图预检不再无条件报告可用；未接适配器、缺失 FFmpeg 文件、不兼容的 Recipe v1 模型，以及当前不支持截图的远程来源，在转写和模型调用前失败。
- 后台任务预检检查已保存的输入快照，仍按原始请求计算策略哈希，避免原文件移动或删除破坏已提交任务。
- 保留已有 WebP 校验、任务取消、超时、输入快照和 Bundle 图片绑定机制；不改变章节布局。

远程 Bilibili 获取仍是音频流程，未修改下载协议。YouTube 仍需先下载完整视频。缺少视频流的音频文件不在本轮截图成功验收范围内。

## 自动测试

修复前，相关三个既有测试文件共 125 项通过。新增用例先得到失败结果，再实施接线修复。

最终执行：

```powershell
cd G:\AllToNote\backend
& 'G:\AllToNote\local-data\dev-envs\screenshot-fix\Scripts\python.exe' -m pytest `
  tests/integration/test_local_media_golden_path.py `
  tests/integration/test_platform_subtitle_golden_paths.py `
  tests/cli/test_produce_video_cli.py `
  tests/adapters/test_ffmpeg_screenshots.py `
  tests/core/test_screenshot_plan.py `
  tests/integration/test_fake_video_producer.py `
  tests/integration/test_video_detach_engine_e2e.py `
  tests/core/test_video_request_persistence.py -q
```

结果：264 passed，96.67 秒。新增 9 个参数化场景覆盖普通/冻结 Pack 接线、前台/后台快照、缺适配器、缺执行文件、远程来源、缺输入及模型不兼容。没有运行全仓库测试或 Web UI 测试。

## 真实视频

- 来源：`https://www.youtube.com/watch?v=FOaPOP1GO_Q`，复用此前已下载的 MP4，本次不验证网络下载或 URL 直传。
- 输入 SHA-256：`f66de5d1a733dad899f4ad4e99a995d45a70abd454aa5fbcbd30211d079cfe9d`。
- CLI：`local-data/dev-envs/screenshot-fix/Scripts/alltonote.exe`，安装本仓库工作树源码（基于 `c923558`），不是旧 V20 二进制。
- 机器环境：`local-data/releases/runtime-v20-video-e2e/machine`，通过进程环境变量指定；未改默认机器配置。
- 模型：`gpt-5.6-terra`；Recipe v2；`knowledge-note`；`on_demand`。
- Job：`job_01a075c8-d04f-7705-84db-70de4f3a6b10`。
- Bundle：`bnd_01a075c8-d04f-7068-9e0b-1366a64b8f6e`。
- Draft：`art_01a075c8-d04f-7696-a5a8-97415199fbbf`。
- 结果：`succeeded`、`quality.overall=pass`、`publish_eligible=true`。

实际生成 3 张 WebP：02:45.240、10:30.360、11:51.920。逐张查看，分别为视频内的图表/期权页面，不是空白占位。源视频为低分辨率画面，截图不提高原视频中文字的清晰度。

8 个 Bundle 文件型 artifact 的长度与 SHA-256 校验通过；`draft show --presentation reading` 保留 3 个图片引用。整体复制 Bundle 到 `local-data/outputs/youtube-screenshots-FOaPOP1GO_Q-20260906` 后，3 个相对图片链接与全部 artifact 哈希再次通过。

可打开的本地导出笔记：`local-data/outputs/youtube-screenshots-FOaPOP1GO_Q-20260906/drafts/art_01a075c8-d04f-7696-a5a8-97415199fbbf.md`。这里的导出是完整目录复制，不是新增 CLI 导出命令。

复现命令（已初始化的本次工作区）：

```powershell
$env:ALLTONOTE_MACHINE_STATE_ROOT = 'G:\AllToNote\local-data\releases\runtime-v20-video-e2e\machine'
& 'G:\AllToNote\local-data\dev-envs\screenshot-fix\Scripts\alltonote.exe' produce video `
  --input 'G:\AllToNote\local-data\workspaces\youtube-test-FOaPOP1GO_Q-20260820\source\FOaPOP1GO_Q.mp4' `
  --workspace 'G:\AllToNote\local-data\workspaces\youtube-screenshots-fixed-FOaPOP1GO_Q-20260906' `
  --wait --json --recipe-version 2 --provider-profile composer `
  --transcriber-profile default --model gpt-5.6-terra --quality balanced `
  --style 'Structured illustrated notes. Request exactly 3 screenshots from supplied transcript segments at distinct key moments where the speaker discusses an on-screen chart or trade. Use the supported [SCREENSHOT:seg_NNNNNN] controls with real supplied segment IDs. Do not invent visual details.' `
  --screenshot-policy on_demand --output knowledge-note --output-language zh-CN
```

`on_demand` 不保证模型一定要求截图。图片目前集中在文末 `Screenshots` 章节，不代表逐字稿旁配图功能已经实现。旧 V20 制品和签名 Pack 未被覆盖；本地视频、笔记、截图和开发环境均留在 Git 忽略的 `local-data/`。
