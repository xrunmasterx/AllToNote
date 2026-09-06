# AllToNote CLI：YouTube 视频转笔记与本地逐字稿

```yaml
doc_type: guide
status: active
authority: execution
upstream:
  - docs/superpowers/specs/2026-07-18-alltonote-runtime-cli-feature-pack-design.md
  - docs/superpowers/specs/2026-07-14-alltonote-video-producer-design.md
implementation_status: current-master
last_verified_at: 2026-08-20
verified_commit: 913a3a270fe7e81c3add23beae0c992e37454275
```

本文给人和自动化 AI 提供当前 `master` 上可直接执行的 Windows PowerShell 流程：

1. 输入 YouTube 链接，调用 LLM 生成结构化 Markdown 笔记；
2. 输入 YouTube 链接，只在本机转写为带时间戳的文本，不调用 Agent、LLM 或远程模型 API。

这里的“不用 AI”准确指“不使用生成式 LLM”。本地转写仍使用离线
`faster-whisper small / CPU / int8` 语音识别模型。当前 CLI 不能在完全不做语音识别或
语言模型处理的情况下，把无字幕视频变成文本。

## 1. 当前能力边界

| 目标 | 当前是否可用 | 真实入口 |
|---|---:|---|
| YouTube 链接直接生成笔记 | 否 | 先用 `yt-dlp` 下载，再把本地视频传给 `alltonote produce video` |
| 本地视频生成结构化笔记 | 是 | `alltonote produce video --recipe-version 2 ...` |
| 本地视频生成忠实整理稿 | 是，但仍调用 LLM | `--output faithful-edition` |
| 本地视频只生成逐字稿 | 是，内部入口 | 调用已安装 `transcribe-cpu` Pack 的 worker |
| `alltonote transcribe ...` | 否 | 当前没有该公开子命令，不得虚构 |

重要：`alltonote runtime capabilities` 当前可能把
`recipe.video.acquire.youtube` 显示为 `installed: true`，但生产组合中的
`PackedBilibiliVideoSourceAdapter` 仍不接收 YouTube URL。不要用 capability 输出代替真实
输入测试；直接传 YouTube URL 会得到 `source_unsupported`。

## 2. 一次性准备

以下命令从仓库根目录执行。需要 Python 3.11、Node.js、`yt-dlp`，以及已经登录的本机
Codex（仅生成 LLM 笔记时需要）。

### 2.1 安装当前源码 CLI

```powershell
cd G:\AllToNote
python --version
python -m venv .venv
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $Python -m pip install --upgrade pip
& $Python -m pip install --editable .\backend
$AllToNote = (Resolve-Path .\.venv\Scripts\alltonote.exe).Path
& $AllToNote version
```

如果使用 Windows V20 目录候选，把 `$AllToNote` 改为候选目录中的 `alltonote.cmd`。
下载步骤仍使用上面虚拟环境中的 `$Python`，不要向候选自带的 Python 安装下载依赖。
V20 基于 `68d517f` 构建，早于本文核验的源码提交；它不是公开稳定安装包。

### 2.2 检查 Runtime 与 Pack

```powershell
& $AllToNote runtime doctor --dynamic --json
& $AllToNote pack doctor media-basic --json
& $AllToNote pack doctor transcribe-cpu --json
```

继续执行的条件：

- `runtime doctor` 的顶层 `ok` 为 `true`；
- `media-basic` 和 `transcribe-cpu` 均为 `installed: true, healthy: true`；
- 生成 LLM 笔记时，`dynamic.model.codex-app-server` 为 `pass`。

如果 Pack 未安装，只能从可信的 AllToNote 签名 Pack 目录安装：

```powershell
& $AllToNote pack install media-basic --source '<SIGNED_MEDIA_PACK_DIR>' --json
& $AllToNote pack install transcribe-cpu --source '<SIGNED_TRANSCRIBE_PACK_DIR>' --json
```

`<SIGNED_..._PACK_DIR>` 不是仓库源码目录；如果没有发布方提供的签名 Pack，停止并向用户
索取，不要下载来历不明的替代文件。

### 2.3 创建 Workspace

Workspace 保存 Job、逐字稿、Evidence、质量报告和最终 Markdown。它不应位于 AllToNote
机器状态目录内。

本地输入、工作区、模型和发布候选统一放在仓库的 `local-data/` 下；该目录同时由
`.gitignore` 和 `.dockerignore` 排除，不提交浏览器配置、Cookie、模型或个人笔记。

```powershell
$Workspace = 'G:\AllToNote\local-data\workspaces\youtube-notes'
& $AllToNote workspace init $Workspace --name youtube-notes --set-default --json
```

已经初始化时可直接复用，并在后续命令中始终显式传 `--workspace $Workspace`。

### 2.4 准备 `yt-dlp`

```powershell
& $Python -m pip install --upgrade yt-dlp
node --version
& $Python -m yt_dlp --version
```

Node.js 用于解决 YouTube 的 JavaScript challenge。下载视频需要网络；本地转写阶段不需要
网络。

## 3. 公共步骤：把 YouTube 链接下载为本地视频

先设置本次输入和下载目录：

```powershell
$YouTubeUrl = 'https://www.youtube.com/watch?v=FOaPOP1GO_Q'
$DownloadDir = 'G:\AllToNote\local-data\inputs\FOaPOP1GO_Q'
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
```

先尝试匿名下载：

```powershell
$downloadOutput = & $Python -m yt_dlp `
  --js-runtimes node `
  --remote-components ejs:github `
  --format 'b[ext=mp4]/b' `
  --output (Join-Path $DownloadDir '%(id)s.%(ext)s') `
  --print 'after_move:filepath' `
  $YouTubeUrl
$downloadExit = $LASTEXITCODE
$videoPath = [string]($downloadOutput | Select-Object -Last 1)
$videoPath = $videoPath.Trim()

if ($downloadExit -ne 0 -or -not $videoPath -or
    -not (Test-Path -LiteralPath $videoPath)) {
  throw 'YouTube 下载失败'
}
$videoPath = (Resolve-Path -LiteralPath $videoPath).Path
```

若出现 `Sign in to confirm you're not a bot`、年龄限制或账号可见性限制，使用从正常浏览器
导出的 Netscape 格式 Cookie 文件重试：

```powershell
$CookieFile = 'C:\secure\www.youtube.com_cookies.txt'
$downloadOutput = & $Python -m yt_dlp `
  --cookies $CookieFile `
  --js-runtimes node `
  --remote-components ejs:github `
  --format 'b[ext=mp4]/b' `
  --output (Join-Path $DownloadDir '%(id)s.%(ext)s') `
  --print 'after_move:filepath' `
  $YouTubeUrl
$downloadExit = $LASTEXITCODE
$videoPath = [string]($downloadOutput | Select-Object -Last 1)
$videoPath = $videoPath.Trim()

if ($downloadExit -ne 0 -or -not $videoPath -or
    -not (Test-Path -LiteralPath $videoPath)) {
  throw '带 Cookie 的 YouTube 下载仍然失败'
}
$videoPath = (Resolve-Path -LiteralPath $videoPath).Path
```

Cookie 是账号凭据：不要显示内容、不要复制进日志或文档、不要提交到 Git，且只对用户明确
授权的视频使用。`--remote-components ejs:github` 会联网获取 yt-dlp 的 EJS challenge 组件。

## 4. 方式 A：调用 LLM 生成结构化笔记

当前已真实验证的组合是 Video Recipe v2、`knowledge-note`、本地 CPU 转写和
`gpt-5.6-terra`。模型可按当前 Codex 可用模型替换；模型不可用时先运行
`runtime doctor --dynamic --json`，不要盲目重试。

```powershell
$Model = 'gpt-5.6-terra'
$NoteFile = Join-Path $DownloadDir 'note.md'

$produceRaw = & $AllToNote produce video `
  --input $videoPath `
  --workspace $Workspace `
  --wait `
  --json `
  --recipe-version 2 `
  --provider-profile default `
  --transcriber-profile default `
  --model $Model `
  --quality balanced `
  --style structured `
  --screenshot-policy off `
  --output knowledge-note `
  --output-language zh-CN

$produceExit = $LASTEXITCODE
$produce = $produceRaw | ConvertFrom-Json
if ($produceExit -ne 0 -or -not $produce.ok -or $produce.job.state -ne 'succeeded') {
  throw "AllToNote 生成失败：$($produce.error.code) $($produce.error.message)"
}

$draftId = $produce.job.result_refs.primary_draft_artifact_id
$draftRaw = & $AllToNote draft show $draftId `
  --workspace $Workspace `
  --presentation reading `
  --body-bytes 262144 `
  --json

$draftExit = $LASTEXITCODE
$draft = $draftRaw | ConvertFrom-Json
if ($draftExit -ne 0 -or -not $draft.ok -or $draft.data.body_truncated) {
  throw '读取完整 Markdown 笔记失败'
}
[IO.File]::WriteAllText(
  $NoteFile,
  [string]$draft.data.body,
  [Text.UTF8Encoding]::new($false)
)
Write-Output $NoteFile
```

成功判据不是只有进程退出码：

- 顶层 `ok == true`；
- `job.state == "succeeded"`；
- `job.result_refs.primary_draft_artifact_id` 存在；
- 对高质量可发布笔记，还应检查
  `job.result_refs.quality_overall == "pass"` 和
  `job.result_refs.publish_eligible == true`。

如果目标是尽量忠实于原话的整理稿，可把上面命令改为：

```text
--output faithful-edition --faithful-language preserve-source
```

`faithful-edition` 仍会调用 LLM，并不是“无 AI”模式。

### 4.1 带视频截图的知识笔记

2026-09-06 的源码修复将签名 `media-basic` Pack 的 FFmpeg 接入了生产 CLI。
保留的 V20 二进制没有这项修复，需使用安装了修改后源码的 CLI。
真实三张截图及回归测试见[验收记录](../acceptance/2026-09-06-cli-video-screenshots.md)。

对已下载的本地视频，使用 Recipe v2、`--output knowledge-note`，将上面的
`--screenshot-policy off` 改为 `--screenshot-policy on_demand`。若希望模型提出
截帧请求，可同时使用：

```text
--style "Structured illustrated notes. Request 3 screenshots from supplied transcript segments at key on-screen chart or trade explanations. Do not invent visual details."
```

`on_demand` 允许截图但不保证图片数量：模型没有提出请求时仍可能生成纯文字笔记。
截图当前集中追加在文末 `Screenshots` 章节，不是逐段左右配图。远程 URL 截图
尚未接通；应先下载完整视频，不要把只有音频的文件当作视频输入。

带图输出必须保留 Bundle 内 `drafts/` 与 `assets/` 的相对目录结构。上面的
`$NoteFile` 写法只复制 Markdown 文本，不会复制图片，不能作为完整的带图导出。
`workspace_relative_bundle_path` 下的 `drafts/<draft_id>.md` 是带证据脚注的审计原文，
直接用 Markdown 阅读器打开会显示正文编号和文末时间列表。日常阅读应使用
`alltonote draft show <draft_id> --presentation reading`（`draft show` 默认也是阅读版），
它隐藏系统证据脚注，但保留章节时间、图片、图注及用户自己的脚注。
导出阅读版时，应保留图片目录或按导出位置更新相对路径，不要覆盖 Bundle 内的审计原文及哈希。
如需溯源，可在阅读版末尾放置一个指向审计原文的链接，而不是展开全部时间脚注。
成功判据还需增加：实际图片文件存在、Markdown 图片链接可解析。

### 4.2 图文精读（实际多模态输入）

修改后的源码 CLI 支持以下组合，保留原有 `structured` 模式行为：

```text
--recipe-version 2 --output knowledge-note --style illustrated-reading --screenshot-policy on_demand
```

流程：本地视频与字幕 → FFmpeg 候选帧（约每 45 秒一个，最多 24 张）→ 模型结合画面与邻近字幕选图
→ 全文按时间顺序编写、原位配图 → 图片/字幕/正文多模态复核。图片通过 Codex app-server 的
内联图片输入发送，不是仅传文件路径；接口依据 [OpenAI App-server 文档](https://learn.chatgpt.com/docs/app-server)。
模型调用继续使用原有结果持久化和恢复机制，图片内容及顺序参与请求哈希。
章节时间由字幕 ID 边界计算；若章节范围与其引用或图片不一致，只允许一次保留证据的布局修复，
修复失败则停止。实跑与验收记录见[图文精读验收](../acceptance/2026-09-06-cli-illustrated-reading.md)。

目前仅接入 `codex-app-server` provider，并要求全文能放入安全的直接编写预算；超预算会明确失败，
不会悄悄退化为有损摘要。抽帧仍是时间采样，不是镜头检测，可能错过短暂画面。
图文复核失败时返回 `visual_review_failed`，不发布不合格草稿；模型复核不能替代人工事实核验。
原始画面分辨率或 ASR 不足时应在正文局部标注不确定性，不能补造数字或单位。

### 4.3 保真图文精读（推荐用于保留原视频内容）

使用修改后的源码 CLI，将输出切换到保真稿；现有摘要模式及 CLI 默认参数不变：

```text
--recipe-version 2 --output faithful-edition --faithful-language preserve-source --style illustrated-reading --screenshot-policy on_demand
```

流程：时间戳转录 → 逐片段抽帧 → 提出带证据的转录校正与主题边界 → 单独复核校正
→ 按主题保守编辑 → 程序检查来源、顺序和校正后的数值 → 逐段图文复核与定向修复
→ 生成全文概览、章节摘要、配图正文和原始／校正／编辑对照。
图文模式优先按模型核验的主题边界分章，输入字节预算仍是硬上限；无图模式保持原来的分段策略。
每个正文段落最多映射 12 条连续片段，不混写不同交易案例或在半句话中断段。

- 保留原顺序、论证、例子、条件和有意义的重复；只排除明确的音乐／噪声标记。
- 数字校正必须引用附近真实帧及可读字幕，经过单独模型复核后才能成为编辑基准。
  不能用算术、外部知识或不相关行情列表猜改；后续编辑仍严格检查数字及顺序。
  原始 Transcript 不覆盖，校正记录包含原文、新文、片段 ID、画面引用和理由。
- 编辑、纠错和图文复核均使用已有真实图片传输。校正按最多 16 条片段一批执行，带前后文；
  每批独立复核，失败最多定向修复一次并重查，仍失败则停止，不能直接进入正文生成。
  若正文或摘要未通过，最多增加一轮定向修复：仅失败章节接收原转录、旧稿、复核意见及对应真实图片，修复后重新校验来源和数字，并再次独立调用模型复核。
  无证据的含糊指代不能猜补；画面能解决的关键矛盾不能仅以“待核对”绕过正文质量检查。
  所有请求仍使用既有模型绑定和持久化调用机制，最多 9 个逻辑依赖阶段；章节编辑与复核可流水交叠。
  单独生成保真图文稿时，校正候选帧最多 192 张，优先取转录片段中点；长视频超过上限会抽样。
  这些不是强制插图：候选经模型筛选后最多 24 张进入图文编辑，最终可继续去重或不配图。
  校正用帧保留在本地任务检查点，阅读稿只展示最终选中的配图。
  图注没有独立的数值证据契约，因此不得新增所属正文段落未出现的数字；
  引入未知精确刻度的配图会被省略，正文和审计证据不受影响。
- 来源映射全覆盖不是语义完整性的证明。逐段模型复核检查遗漏、反转和无依据新增；
  定向修复后正文仍不合格时 `faithful_review_failed` 会停止发布；修复违反数字等确定性检查则 `faithful_semantic_repair_failed` 停止。
  可选摘要单独复核，修复后仍未通过的摘要省略，
  不让辅助摘要阻塞已通过复核的正文。不将“模型说通过”等同于原视频事实已核验。
- 审计稿附带 `alltonote-edit-log-v1` JSON 对照，记录改动段落的来源、原文、编辑后文本和时间范围。
  依据是转录上下文、对应画面及模型复核，并非逐句原音频核验；来源引用覆盖率不是语义准确率。
- `draft show --presentation reading` 隐藏系统引用脚注、编辑对照、局部“待核对”、辅助“待复核项”和内部时间标记，保留正文图片及章节时间标题。
  AI 摘要与关键点按相同章节标题匹配，插入章节标题之后、对应正文及图片之前，以 `<details open>` 默认展开、可收起的区块呈现，明确标为“不属于原文”。
  块内“AI 章节摘要”和“AI 关键点”使用普通加粗标签，不作为标题进入目录；旧审计稿重新导出阅读版时也会转换这两种标签。
  未通过复核的章节不生成空折叠块，也不把后面的摘要错配到前一章。审计版仍保留原始末尾摘要布局。
  Markdown 阅读器须支持 HTML `details/summary` 才能交互折叠；禁用 HTML 的阅读器请使用 HTML 阅读版。
  校对信息仍保留在 `--presentation audit` 中，不展示给普通读者。
  原始 Bundle 不变；导出时应保留图片相对路径，并可在末尾只保留一个审计入口。

模型接入、鉴权和型号不变；图文模式仍要求已接入图片传输的 `codex-app-server`。
实现采用结构化输出和针对性回归测试，而不只强化提示词；提示设计参考
[OpenAI Prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering)。

### 多视频并发（源码 CLI / Engine）

- 单视频最多 4 路模型调用；同一 Runtime 数据目录下的所有 CLI / Engine 进程共享 8 路模型调用槽。
  等待模型槽不会启动模型请求；等待支持取消和超时，槽位由操作系统锁保护，持有进程退出即释放。
- Engine 默认同时执行最多 4 个任务，超出的后台任务持久化排队。
  批量任务使用 `produce video ... --detach`；`--wait` 是前台执行，4 个任务槽全部占用时返回 `resource_busy`，不会自动转为后台排队。
- 每个视频运行时最多保留 4 条 app-server 连接，避免为每次模型调用重新启动进程。
  每次生成或复核仍创建独立的临时会话，完成后退订；异常连接销毁，取消不会终止其他连接。
  Windows 进程树使用随父进程退出关闭的 Job Object，避免 CLI 崩溃后留下模型进程。
- 每章完成编辑和本地确定性检查后即可独立复核，不再等所有章节编辑完毕。
  发布前仍做全篇覆盖、顺序和数字检查；修改过的章节不能复用修改前的复核结果。
- 模型、推理强度、提示词、图片证据及有界修复预算未降低。
  并发容量不代表账户调用额度或任意视频的成功率；真实内容复核失败仍停止发布。

连接生命周期按 [OpenAI Docs 的 app-server 协议](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
实现；实测与边界见 [性能验收](../acceptance/2026-09-07-video-concurrency.md)。已有后台进程需重启后才会加载源码修改；已打包版本需重新构建。

## 5. 方式 B：不调用 LLM，只生成本地逐字稿

当前没有公开的 `alltonote transcribe` 子命令。以下是当前版本唯一可复用的本地入口：从
Runtime 的 `active.json` 解析固定、签名且已安装的 `transcribe-cpu` Pack，然后调用其
内部 worker。该接口属于内部实现，升级后必须重新核对本文。

从仓库根目录运行：

```powershell
$InputVideo = $videoPath
$TranscriptJson = Join-Path $DownloadDir 'transcript.json'
$TranscriptText = Join-Path $DownloadDir 'transcript.txt'
$CpuThreads = [Math]::Min([Environment]::ProcessorCount, 8)

$pathsEnvelope = & $AllToNote runtime paths --show-paths --json | ConvertFrom-Json
if (-not $pathsEnvelope.ok) { throw '无法读取 AllToNote Runtime 路径' }
$dataDir = ($pathsEnvelope.data.paths | Where-Object role -eq 'data').path

$packVersion = 'faster-whisper-1.1.1-small-536b0662-r1'
$packRoot = Join-Path $dataDir "packs\transcribe-cpu\$packVersion"
$active = Get-Content -Raw -Encoding UTF8 (Join-Path $packRoot 'active.json') |
  ConvertFrom-Json
$generationHash = ([string]$active.manifest_sha256) -replace '^sha256:', ''

$generation = Join-Path $dataDir "pack-store-v1\t\$generationHash"
if (-not (Test-Path -LiteralPath $generation)) {
  $generation = Join-Path $packRoot "installs\$generationHash"
}

$packPython = Join-Path $generation 'python\python.exe'
$modelPath = Join-Path $generation 'models\small'
if (-not (Test-Path -LiteralPath $packPython) -or
    -not (Test-Path -LiteralPath $modelPath)) {
  throw 'transcribe-cpu Pack generation 不完整'
}

$backendPath = (Resolve-Path '.\backend').Path
$previousPythonPath = $env:PYTHONPATH
$previousHfOffline = $env:HF_HUB_OFFLINE
$previousTransformersOffline = $env:TRANSFORMERS_OFFLINE
$env:PYTHONPATH = $backendPath
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$previousConsoleOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$driver = "import json,sys; from app.adapters.video_packs.transcribe_cpu_worker import transcribe_request; request={'schema_version':1,'media_path':sys.argv[1],'model_path':sys.argv[2],'cpu_threads':int(sys.argv[3])}; result=json.dumps(transcribe_request(request),ensure_ascii=False,allow_nan=False).encode('utf-8'); sys.stdout.buffer.write(result)"

try {
  $responseRaw = & $packPython -B -c $driver `
    (Resolve-Path -LiteralPath $InputVideo).Path `
    (Resolve-Path -LiteralPath $modelPath).Path `
    ([string]$CpuThreads)
  if ($LASTEXITCODE -ne 0 -or -not $responseRaw) {
    throw '本地转写 worker 失败'
  }
} finally {
  $env:PYTHONPATH = $previousPythonPath
  $env:HF_HUB_OFFLINE = $previousHfOffline
  $env:TRANSFORMERS_OFFLINE = $previousTransformersOffline
  [Console]::OutputEncoding = $previousConsoleOutputEncoding
}

$response = $responseRaw | ConvertFrom-Json
[IO.File]::WriteAllText(
  $TranscriptJson,
  ($response | ConvertTo-Json -Depth 8),
  [Text.UTF8Encoding]::new($false)
)

$lines = foreach ($segment in $response.segments) {
  $start = [TimeSpan]::FromSeconds([double]$segment.start)
  $hours = [Math]::Floor($start.TotalHours)
  $timestamp = '{0:D2}:{1:D2}:{2:D2}' -f `
    [int]$hours, [int]$start.Minutes, [int]$start.Seconds
  "[$timestamp] $($segment.text)"
}
[IO.File]::WriteAllLines(
  $TranscriptText,
  [string[]]$lines,
  [Text.UTF8Encoding]::new($false)
)
Write-Output $TranscriptText
```

这个流程不会调用 Codex、Agent、LLM 或在线转写 API；Pack 安装完成后，转写阶段可以离线
运行。输出是逐字稿而不是经过总结、重组和语义校验的知识笔记。

## 6. AI 执行约束

后续 AI 使用本文时必须遵守：

1. 先核对当前提交和 `alltonote ... --help`；若实现已变化，以当前代码和帮助为准并更新本文。
2. 不要把 YouTube URL 直接传给当前 `produce video`，除非真实 E2E 已证明适配器支持。
3. 不要声称存在 `alltonote transcribe`；当前无 LLM 流程调用的是内部 Pack worker。
4. 不读取或输出 Cookie 内容；Cookie 文件只作为 `yt-dlp --cookies` 的路径参数。
5. 所有路径使用绝对路径，所有 JSON 命令同时检查退出码、顶层 `ok` 和 Job 最终状态。
6. `runtime capabilities` 是声明性探针，不是 YouTube 下载成功的证据。
7. `knowledge-note` 和 `faithful-edition` 都会调用 LLM；只有第 5 节不调用 LLM。

## 7. 已验证事实

2026-08-20 在提交 `913a3a270fe7e81c3add23beae0c992e37454275` 上使用
`https://www.youtube.com/watch?v=FOaPOP1GO_Q` 验证：

- 直接 URL 任务以 `source_unsupported` 失败；
- `yt-dlp 2026.07.04 + Node + ejs:github + Netscape Cookie` 成功下载 821.847 秒 MP4；
- 本地 `transcribe-cpu` 生成 294 条记录，末段时间为 804.720 秒；
- `gpt-5.6-terra` 的 Recipe v2 `knowledge-note` 成功，质量报告为 `pass`，含 21 个时间戳引用；
- 同一输入使用 `gpt-5.6-sol` 曾在 `generate_draft` 阶段以
  `knowledge_coverage_invalid` 失败，因此模型调用成功不等于整条 Job 必然成功。
