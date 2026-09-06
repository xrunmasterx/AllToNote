# Bilibili BV1ntt66WEzy 实测（2026-09-06）

后续更新：已实现定向修复并成功生成，详见[语义修复闭环复核](2026-09-06-faithful-semantic-repair.md)。以下保留首次失败的历史记录。

结论：视频下载、转录、候选截图和模型调用可用；两轮保真正文复核失败，未发布笔记 Bundle。
本次没有修改源码、放宽质量门槛或重建 V20。

## 输入与环境

- 用户来源：`https://www.bilibili.com/video/BV1ntt66WEzy/`
- 下载到：`local-data/inputs/BV1ntt66WEzy/BV1ntt66WEzy.mp4`
- 时长：319.437551 秒；分辨率：1728×1080；视频及音频流均存在。
- 文件大小：27,886,566 字节。
- SHA256：`e9d4916c569c56c7f5a4ff9d5a330279368f392ae131133f125d6fc55cdf0542`
- CLI：`local-data/dev-envs/screenshot-fix/Scripts/alltonote.exe`
- Workspace：`local-data/workspaces/bilibili-faithful-BV1ntt66WEzy-20260906`
- 参数：`--recipe-version 2 --provider-profile composer --transcriber-profile default --model gpt-5.6-terra --quality balanced --style illustrated-reading --screenshot-policy on_demand --output faithful-edition --faithful-language preserve-source --output-language zh-CN`

## 执行结果

1. 直接 URL 任务 `job_01a076c0-219b-729f-b419-fc0fd871d96e` 被 `screenshot_source_unsupported` 拒绝：当前截图流程要求本地视频。
2. 使用已安装的 yt-dlp 匿名下载成功，无需 Cookie。直接调用包内 Python 时未带 `-B`，导致新增 135 个缓存文件、改写 177 个已有 `.pyc`，触发 Pack 完整性检查失败。
   已移除仅本次新增的缓存，并从原签名包逐项校验 SHA256 后恢复被改写的缓存。媒体包 doctor 最终恢复 `installed: true, healthy: true`。
   后续不得以可写字节码方式直接运行签名包内 Python；本次没有改动源视频或用户笔记。
3. 本地首轮 `job_01a076c4-7a81-714e-b512-42df2082d77c`：完成转录、抽帧及三段编译/复核，`faithful_review_failed`。
   模型指出含糊转录“有整整古单都在做多的”被改为“有整整多单都在做多的”，无法确定该纠错是否保留原意。
4. 相同配置第二轮 `job_01a076c7-bf7c-79eb-8a2d-ca6a5d24c0d5`：再次 `faithful_review_failed`。
   正文复核指出“支屁者”被猜改为“支持者”，且正文“看见久转”与画面字幕“看跌9转”不符。
   辅助摘要另有确定程度弱化、回调条件遗漏、ASTS 与 MARS 交易混同等问题；这些是存储的模型复核发现，不是人工音频核验结论。

## 边界

没有第三轮随机重试，也没有将失败中间稿标为合格笔记。
当前已支持 B 站视频作为本地输入进入图文流水线，但这条样本尚未通过最终质量验收。
瓶颈是含糊转录的纠错和多模态复核结果不能反馈修复正文，而非下载或截图能力。
