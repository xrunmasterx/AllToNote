# CLI 保真图文精读验收（2026-09-06）

## 范围与实现

在已有 `faithful-edition` 上接入图文精读，不改变 `knowledge-note` 或 CLI 默认输出。
源 CLI：`local-data/dev-envs/screenshot-fix/Scripts/alltonote.exe`；保留的 V20 二进制未重建。

- 规划仅排除明确音乐／噪声标记，保留相邻重复的实际发言；按输入预算及约 150 秒分段。
- 编辑逐段保留正文来源顺序，Runtime 每个段落最多 12 个来源 ID；数字按原表达、原顺序保留。
- 修复旧数值正则的中文边界漏检，并从排序比较改为有序比较，防止买入／卖出数量互换未被发现。
- 正文不接收图片；独立 `faithful-review` 调用接收正文、原始转录及该段真实 WebP，
  审查语义遗漏与无依据改写，并选图、描述可见内容、标记局部疑点。仍用原配置绑定的模型，非独立模型评审。
- 程序将选中图片绑定到包含其来源 ID 的正文段落。没有合适图片时允许空选择，拒绝未知／重复帧。
- 确定性校验仍只有一次修复机会；正文语义复核失败或返回格式不合法时停止，不发布 Bundle。
  辅助摘要独立复核，失败时省略其内容并标注警告，不阻塞已通过复核的正文。
- 正文连续呈现，AI 摘要、关键点在单独的辅助区域。审计稿内保存逐段原文／编辑后文本、时间及来源 ID。
  `draft show --presentation reading` 隐藏系统脚注和 `alltonote-edit-log-v1` 对照，不修改原始 Bundle。
- 本地元数据语言 `und` 不再阻止已识别语言的保真转录：保真正文及其输出元数据使用转录语言，
  原视频元数据保持不变。

实现主要位置：

- `backend/app/core/application/faithful_edition_compiler.py`
- `backend/app/core/recipes/video/faithful_edition/{contracts,pipeline,prompts,quality,review}.py`
- `backend/app/core/application/video_service.py`
- `backend/app/core/application/artifact_query_service.py`
- `backend/app/runtime.py`

## 验证方法

回归覆盖数值改动、相邻重复保留、原文到正文映射、实际图像请求、复核拒绝、空选图、
编辑日志隐藏／字面代码保留、持久结果恢复不重复调用、固定 FFmpeg Pack、后台快照、
原文件消失、未知元数据语言、双输出原子提交及既有摘要流程。

真实输入沿用用户提供视频的已下载本地文件，不重新下载：

- 原链接：`https://www.youtube.com/watch?v=FOaPOP1GO_Q`
- 文件：`local-data/workspaces/youtube-test-FOaPOP1GO_Q-20260820/source/FOaPOP1GO_Q.mp4`
- SHA256：`f66de5d1a733dad899f4ad4e99a995d45a70abd454aa5fbcbd30211d079cfe9d`
- 时长 821.847 秒，原始画面 640×360。
- 使用 `composer` profile、`gpt-5.6-terra`、现有 CPU small 转录包。
- 参数：`--recipe-version 2 --quality balanced --output faithful-edition --style illustrated-reading --screenshot-policy on_demand --output-language zh-CN`。

## 实跑记录

1. `job_01a0766b-2c42-722b-93a0-35dc78bee9f0`：旧 preflight 限制保真稿不能截图，调用模型前失败；已调整为允许图文精读组合。
2. `job_01a0766b-97d2-7a9c-902a-f0525e12b9b6`：转录和抽帧完成，因元数据 `und` 与转录 `zh` 不同而被保真契约拒绝，未调用模型；已修复并补测。
3. `job_01a07670-b01b-722d-8ee8-e0efcc6215d6`：六段初稿部分使用从 1 开始的编号，且部分段落超过 12 条来源；
   有限修复后仍不符合契约，未发布。发现发送给模型的 schema 缺少段落引用上限，提示也没有明确编号起点；
   已补充 schema `maxItems`、各数组零起始编号和超限分段规则，未放宽解析校验。Prompt version 升为 3。
4. `job_01a07677-dd5d-736c-acea-11b000514b63`：格式正确；初稿将 `355360` 拆开，被数字校验拦截并修复。
   修复后仅余 `355,366` → `355，366` 的全角标点变化被误判，未进入模型复核、未发布。
   已增加 NFKC 宽度归一化；测试确认仅改变标点宽度可通过，凭空增加数字边界仍失败。Quality version 升为 3。
5. `job_01a0767f-22c4-7b97-af9b-435228531f4d`：进入模型复核后被拦截。一类为摘要弱化了讲者确信程度，
   一类为复核把有上下文支持的同音纠错也当作无依据改写。编辑提示明确保留讲者立场和确信程度，
   复核明确允许不改变含义的上下文纠错，不要求正确拼写已在原转录逐字出现；未添加视频专用纠错词典。
6. `job_01a07686-6e8d-7b30-9ef4-7d1f25597b21`：正文复核未报告问题，但辅助关键点省略了“看涨”限定，
   当时的耦合复核阻塞了整篇。已改为正文与辅助摘要独立判定，不展示未通过复核的摘要；正文失败仍停止。
7. `job_01a0768f-0751-7807-980e-4b4807790eb2`：成功，`pass_with_warnings`，允许发布。
   6 个正文分段、13 张插图、12 次模型调用（6 次编辑、6 次复核），无需修复调用。
   正文来源引用覆盖率 1.0；02:29–04:59 的辅助摘要未通过复核，已省略，不影响正文。

最终回归：相关 18 个测试模块 **488 项通过（129.25 秒）**。`git diff --check` 通过。
期间一次 Windows 后台引擎端点文件读取出现 `PermissionError`，该测试组单独重跑 5 项通过，
随后 487 项全量相关回归通过；未扩大修改引擎。

## 最终产物与检查

- Bundle：`bnd_01a0768f-0751-7359-b4e6-e233a51be2ea`
- Run：`run_01a0768f-0751-71ee-b1a3-5f0fc4d802ce`
- Manifest SHA256：`498e136cf51a7ea1554bb03c19b0128886ac1baf2ee2cc3965d28e5b1f18aa1e`
- Commit SHA256：`3b09f33ebf4c79e26af48c71f455dcce9d1e3a1b980b8f063333253f277066c3`
- 阅读版：`local-data/outputs/youtube-faithful-FOaPOP1GO_Q-20260906-reading.md`
- 审计 Bundle 副本：`local-data/outputs/youtube-faithful-FOaPOP1GO_Q-20260906/`

阅读版由 CLI `draft show --presentation reading` 完整输出生成，仅调整导出图片路径并增加审计稿链接。
21 个 Bundle 文件逐一 SHA256 比对原件，无差异；13 个图片链接全部存在；阅读版不显示系统脚注及编辑日志。
审计稿含 43 条实际改动的前后对照，带来源 ID、时间范围及“未核验音频”的依据标签。

逐张查看 13 张实际图片，均为对应时间附近的图表、订单、持仓或课程页面；课程图中可见 Vacuum Effect，
期权策略段落配有期权链。图片仍为原视频的 640×360 清晰度，订单小字不宜作为自动数值纠错依据。
通读阅读版，确认正文先连续呈现，再展示独立 AI 摘要；`197.6768`、`355360`、`118×181`、`2223美金`
均未被擅自替换成模型推算结果。后面三项有局部疑点提示，`197.6768` 本次仍漏掉了局部提示。

人工式内容检查仍发现限制：编辑与复核给出的疑点提示存在语义重复；“波头皮”“正当区间”等术语仍有漏纠，
课件可见的 18E 未自动用于正文纠错。按时间分段也会切开跨段句子。
因此本轮验收结论是：**保真图文流程及回归通过，但输出仍需人工校对，不能判为免校对成品。**
未重建 V20、未验证浏览器界面，未执行全仓库测试或其他真实视频泛化测试。

## 验收边界

来源 ID 全覆盖不能证明语义没有遗漏；模型复核也不能证明原音频事实正确。
本次没有音频复听／音频 LLM 核对流程，因此 `197.6768`、期权价格、数量等可疑 ASR 应保留并标注，
不能凭推算或模糊图像自动“修正”。画面仍按时间采样，可能错过短暂关键帧。
编辑日志记录可复查的前后文本，不把模型推断包装为音频验证依据。
