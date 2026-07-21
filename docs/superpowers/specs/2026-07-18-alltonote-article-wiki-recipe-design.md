# AllToNote Article / Wiki Recipe 设计

```yaml
doc_type: subsystem-design
status: active
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
  - 2026-07-14-alltonote-portable-artifact-source-bundle-design.md
  - 2026-07-18-alltonote-recipe-extension-contract-design.md
downstream:
  - ../plans/2026-07-18-alltonote-article-wiki-recipe-implementation-plan.md
implementation_status: not-started
last_verified_at: 2026-07-19
```

## 1. 决策摘要

Article/Wiki Recipe 把网页或 Wiki 内容编译为可追溯的知识草稿。它不是“把 URL 文本交给 LLM”，而是：

```text
URL / Browser Capture / Saved HTML
  -> 确定 Source identity 与 immutable snapshot
  -> 结构化提取正文、元数据、列表、表格、代码、链接、图片说明
  -> 建立 snapshot block Evidence
  -> 按长度选择短路径或长内容 map/compose
  -> Knowledge Note Draft + Quality
  -> Portable Bundle -> Review
```

采集分为两条一等路径：

- 无登录 HTTP 路径：快速、可自动化；
- 用户授权 Browser Capture：处理登录、动态渲染和网页直抓失败。

Recipe 不尝试绕过登录、付费墙、验证码、反自动化或 DRM；无法合法、稳定获取时明确要求用户通过浏览器捕获或提供本地文件。

Article/Wiki 是 AllToNote 的并列官方 Recipe，不拥有独立 Runtime、ProduceService、Job、Bundle、Review、Publisher 或 CLI Pipeline。它在 Video + Document 已验证最小接缝后，作为第三类真实消费者验证 URL snapshot/freshness/browser capture 语义；三类通过后才冻结 Recipe internal v1。网页 block、DOM、canonical URL 等字段不得成为所有 Recipe 的通用必填字段。

## 2. 用户目标

- 粘贴文章链接，快速生成高质量中文或来源语言知识笔记；
- 登录网站可在浏览器中明确授权捕获当前已渲染页面；
- Wiki 页面能保留 revision、章节和内部链接；
- 长文章仍是一篇连贯文档，而非多个分块拼接；
- 表格、代码、列表、引用和图片语义不被简单纯文本化丢失；
- 每个重要结论可回到网页快照中的具体段落；
- 网页更新时生成新 revision 并显示差异，不覆盖旧来源或用户已发布内容；
- Agent 可用 CLI 生产，但不能获得整个浏览器 Cookie 或任意浏览权限。

## 3. 非目标

- 通用全站爬虫；
- 搜索引擎；
- 绕过 robots、付费墙、验证码、账号限制或访问控制；
- 保存浏览器完整 profile/Cookie 到 Vault；
- 保证任何网页都能从 URL 直接解析；
- 执行页面脚本作为知识编译的一部分；
- 默认递归抓取所有 Wiki 链接；
- 把实时网页当成可重复 Evidence 而不保存快照；
- 自动发布到正式 Wiki；
- 复刻网页视觉版式或建立离线站点镜像。

## 4. 输入类型

### 4.1 URL

```text
alltonote produce web --url <https-url> --workspace <ref>
```

只允许 HTTP(S)。Recipe 负责 redirect、canonical、MIME、大小和网络策略。

### 4.2 Browser Capture

用户在扩展/Desktop 浏览器控制中对当前 tab 执行“发送到 AllToNote”，生成本地 Capture Artifact：

```json
{
  "capture_id": "capture_...",
  "captured_at": "...",
  "original_url": "...",
  "final_url": "...",
  "title": "...",
  "rendered_dom_ref": "...",
  "readable_text_ref": "...",
  "selected_assets": [],
  "capture_hash": "sha256:...",
  "browser_session_ref": null
}
```

Capture 不包含 Cookie、localStorage、authorization header、密码字段或完整浏览器 profile。扩展在传输前移除 form values、隐藏安全字段和脚本；Runtime 再校验与 sanitize。

### 4.3 本地 HTML/Markdown

用户可提供 `.html/.htm/.mhtml/.md/.txt` 文件，通过 Local Root Grant 读取。MHTML 需专用 Pack；不支持时明确报 capability missing。

### 4.4 Wiki collection

MVP 先做单页面。后续集合输入必须显式：

- seed pages；
- allowed origin/path；
- maximum pages/depth/bytes；
- 是否跟随内部链接；
- revision/freshness 策略；
- cancel 和预算。

不得因页面含链接就自动递归抓取。

## 5. Source identity 与 revision

### 5.1 Identity

优先使用：

1. 平台稳定 page ID（如 Wiki API page ID）；
2. 经验证的 canonical URL；
3. final normalized URL；
4. 本地文件 identity。

`<link rel=canonical>` 是信号，不是绝对真相。若 canonical 指向不同 origin、异常聚合页或与内容冲突，保留 original/final 并发出 warning。

URL normalization 只做安全、语义明确的处理：scheme/host 大小写、默认端口、fragment 规则、已知 tracking 参数策略。不得随意删除可能决定内容的 query。

### 5.2 Revision

Revision 必须绑定 immutable snapshot：

```json
{
  "source_id": "src_...",
  "revision_id": "sr_...",
  "identity": {"kind": "url", "canonical_url": "...", "platform_page_id": null},
  "revision": {
    "platform_revision_id": null,
    "etag": null,
    "last_modified": null,
    "captured_at": "...",
    "snapshot_hash": "sha256:..."
  }
}
```

ETag/Last-Modified 可辅助 freshness，但不替代 snapshot hash。Wiki 有稳定 revision ID 时同时记录。

### 5.3 再次导入

- snapshot hash 相同：可 no-op 或生成引用同 revision 的新 Draft；
- identity 相同、hash 不同：新 SourceRevision；
- identity 不确定：不得自动把两页合并；
- 新 revision 不覆盖旧 raw Bundle；
- Publisher 更新正式文档仍需独立 Review/PublishPlan。

## 6. 网络与采集策略

### 6.1 HTTP 路径

1. URL validation；
2. DNS/redirect/SSRF policy；
3. HEAD 或有限 GET 探测（不依赖 HEAD 必然可用）；
4. 流式下载到大小受限 staging；
5. 校验 MIME/charset/compression；
6. 保存原始响应主体或安全快照及选定 headers；
7. 解析 DOM；
8. sanitize 后提取。

不记录认证 header、Set-Cookie、完整 header dump 或服务器返回的敏感调试信息。

### 6.2 Browser Capture 路径

适用：

- 登录页面；
- client-rendered 正文；
- HTTP 抓取只有 shell/错误；
- 用户选择局部文章；
- 网站要求交互确认。

原则：

- 捕获动作由用户明确触发；
- 只捕获当前 tab/选择范围；
- 不后台遍历标签页；
- Runtime 只拿 sanitized capture，不读取浏览器 Cookie 数据库；
- capture 在本机 loopback/Native Messaging 等受控通道传输；
- 浏览器扩展和 Runtime 版本握手；
- 大资源按 manifest/hash 分块传输；
- 失败后不把不完整 DOM 当成功来源。

### 6.3 不可访问

稳定错误：

```text
WEB_AUTH_REQUIRED
WEB_BROWSER_CAPTURE_REQUIRED
WEB_PAYWALL_OR_ACCESS_RESTRICTED
WEB_ROBOTS_OR_POLICY_RESTRICTED
WEB_CAPTCHA_REQUIRED
WEB_DYNAMIC_CONTENT_EMPTY
WEB_UNSUPPORTED_MIME
WEB_TOO_LARGE
WEB_REDIRECT_BLOCKED
```

错误只描述用户可采取的合法动作，不提供绕过访问控制方法。

## 7. 快照与 Artifact

推荐 Artifact：

| Kind | 内容 | 是否必需 |
|---|---|---|
| `source-metadata` | URL、title、author、published/modified、language、capture method | 是 |
| `web/http-snapshot` 或 `web/dom-snapshot` | immutable 原始/渲染快照 | 是 |
| `web/extraction-report` | 候选提取器、覆盖和 warning | 是 |
| `normalized-content` | block tree | 是 |
| `evidence-set` | block Evidence | 是 |
| `web/asset-manifest` | 选定图片/附件引用与 hash | 可选 |
| `draft` | Knowledge Note | 是 |
| `quality-report` | 质量与来源覆盖 | 是 |
| `receipt` | acquisition/compiler/tool identity | 是 |

原始 HTML 可能含敏感数据和脚本，必须：

- 作为不执行的二进制/文本 Artifact；
- 读取/预览时 sanitize；
- 默认不把 session-specific 页面提交到 common；
- 通过 privacy classification 标记；
- 遵守大小和保留策略。

## 8. 结构提取

### 8.1 多信号提取

不依赖单一 Readability 结果。组合：

- DOM title/headings/semantic tags；
- JSON-LD/OpenGraph/metadata；
- Mozilla Readability 候选正文；
- main/article 容器；
- paragraph/list/table/pre/code/blockquote/figure；
- Wiki API/HTML 专用结构；
- 用户选择范围；
- 文本密度、导航/广告重复等确定性信号。

输出统一 block tree：

```json
{
  "block_id": "wb_...",
  "kind": "paragraph|heading|list|table|code|quote|figure|callout",
  "text": "...",
  "level": null,
  "children": [],
  "source_locator": {
    "snapshot_hash": "...",
    "dom_path_hint": "...",
    "text_anchor_before": "...",
    "text_anchor_after": "...",
    "ordinal": 42
  }
}
```

DOM path 是辅助定位；Evidence 的稳定性依赖 immutable snapshot hash + block content hash + ordinal/anchors。

### 8.2 提取完整性检查

至少检查：

- extracted text 与页面可见文本/选择范围的覆盖比；
- 标题/章节数量；
- 是否只得到登录提示/导航/脚本 shell；
- 表格/代码/列表是否丢失；
- 阅读顺序异常；
- 语言检测；
- 重复页眉/页脚；
- 内容长度与 metadata 预期。

低于阈值不直接调用 LLM“补全”；返回 Browser Capture 建议或降级 warning。

### 8.3 表格与代码

- 表格保留 cell/row/header 结构和原始文本；
- 复杂嵌套表格可同时保存 HTML fragment 与线性化版本；
- code 保留语言 hint、空白和行范围；
- LLM 不能把代码改写当作来源事实；
- Draft 中引用代码时 Evidence 指向原 block/行。

### 8.4 图片

MVP 不默认下载所有图片。只考虑：

- figure 内且与正文高度相关；
- 用户显式要求；
- 同源/允许 origin、大小/MIME 安全；
- 可建立 hash 和 alt/caption；
- 许可/隐私 warning。

远程 tracking pixel、data URI 超限、脚本 SVG、未知附件不进入安全预览。

## 9. Evidence

```json
{
  "locator_kind": "web-snapshot-range",
  "locator": {
    "snapshot_artifact_id": "art_...",
    "block_ids": ["wb_41", "wb_42"],
    "start_offset": 0,
    "end_offset": 318,
    "heading_path": ["Architecture", "Runtime"]
  },
  "content_hash": "sha256:...",
  "confidence": {"level": "high", "basis": "captured-visible-dom"}
}
```

Evidence 必须可在离线快照中复核，不依赖网页仍在线。链接到原 URL 是便利，不是唯一证据。

## 10. 编译策略

### 10.1 长度路由

| 内容规模 | 路径 |
|---|---|
| 短且结构清晰 | 单次 grounded compile + deterministic quality |
| 中等 | 章节级 knowledge extraction -> global outline/compose |
| 超长/Wiki 长页 | deterministic block chunk -> knowledge map -> global compose -> targeted repair |

复用长视频已验证的原则，而不是复用“按时间分块”实现：

- 分块按 heading/block/token 边界；
- 每块提取结构化知识和 Evidence，不各写一篇文章；
- 全局去重/术语/大纲；
- 一次完整 compose；
- 只修复不合格部分；
- 最终唯一 H1、章节连贯、引用完整。

### 10.2 Profile

#### fast

- 单一成功采集路径；
- Readability/结构提取；
- 短内容单次 compile；
- 基础 deterministic quality；
- 不做视觉理解和深度链接展开。

#### balanced（默认）

- 多信号 extraction；
- 中长内容 map/compose；
- 表格/代码保护；
- 覆盖/引用/重复/结构 Gate；
- 最多一次 targeted repair。

#### thorough

- 更严格完整性比较；
- 可选重要图片视觉解释；
- 可选外链只作为用户显式追加 Source，不静默抓取；
- 更高 coverage/evidence Gate；
- 时间/成本预估和确认。

### 10.3 Language

- 默认保留来源语言；
- 中国用户可显式 `--output-language zh-CN`；
- 翻译 Draft 仍保留原文 Evidence；
- 专有名词、代码、引用不应无依据本地化；
- Quality 区分事实错误与翻译风格问题。

## 11. Wiki 专项

检测到支持 API 的 Wiki 时优先使用官方 API 获取：

- stable page ID；
- revision ID/timestamp；
- wikitext/HTML；
- section tree；
- categories/templates/内部链接（按能力）；
- redirect/disambiguation。

但平台 API Adapter 仍是可替换实现。若 API 不可用，可退到 HTTP/Browser Capture并降低 revision confidence。

Wiki collection 未来必须：

- 每页独立 SourceRevision/Bundle；
- collection manifest 只聚合引用；
- 增量只抓变化 revision；
- 避免循环/重复/重定向爆炸；
- 允许用户先预览页面清单和预算；
- 不把数百页面硬合成一篇不可维护 Markdown。

## 12. Quality Gate

确定性：

- 唯一 H1、标题层级；
- 不重复章节；
- 链接/图片/代码 fence/表格语法；
- Evidence ID 均存在且 hash/range 有效；
- 每个核心 H2 至少有 Evidence；
- 引用不越过 grant/snapshot；
- 无页面导航/登录提示污染；
- Draft metadata 完整；
- language policy 一致。

Recipe-specific：

- extraction coverage；
- table/code/list preservation；
- claim/evidence coverage；
- low-confidence block 披露；
- Wiki revision 完整性；
- 不把外部链接内容写成已验证事实；
- 翻译忠实度（若适用）。

失败只 repair 有问题章节/引用；不能让模型凭空补齐未采集正文。

## 13. CLI

规范自动化入口：

```text
alltonote produce --recipe alltonote.web-note@1 --request <request.json>
```

用户友好别名：

```text
alltonote produce web --url <url> --workspace <ref> [--profile balanced]
alltonote produce web --capture <capture-id> --workspace <ref>
alltonote produce web --file <path-token> --workspace <ref>
  [--draft knowledge-note]
  [--output-language source|zh-CN]
  [--title <override>]
  [--json]
```

`produce web` 和 generic `produce --recipe/--request` 必须构造同一 ProduceRequest，经同一 ProduceService/Registry/Job/Bundle 路径执行；CLI 不直接调用 Web compiler。Article/Wiki 位于真实 Document/PPT 驱动的 X0-B 之后，当前不重新开放独立 `add` 或 `run`。

后续 Wiki collection 使用单独显式命令/参数，不让 `--url` 默认递归。

## 14. 安全与隐私

- SSRF：阻止 loopback、link-local、私网/metadata endpoint，redirect 每跳检查；本地受信内网站点需独立 grant；
- 下载流式大小/压缩比/MIME/超时限制；
- HTML sanitize，脚本不执行；
- Browser Capture 移除 form/Secret/session data；
- 页面 Prompt injection 只作内容；
- 登录/付费内容默认 privacy=personal；
- 不将完整 URL query、正文或 cookie 写普通日志；
- 远程模型调用遵守用户 provider/privacy policy；
- common 发布仍由 Publisher 独立拦截；
- 遵守用户有权访问和处理来源内容的前提，产品不提供绕过手段。

## 15. 性能预算

- 普通公开文章 HTTP + extraction p95 < 5 s（不含站点慢响应）；
- 无需浏览器时不启动 Browser Pack；
- 短文章 fast/balanced 目标模型调用 1 次；
- 中长内容按 token/结构控制 map 数，不以固定小块制造调用爆炸；
- snapshot/asset 流式写，内存不随页面总字节线性翻倍；
- 相同 snapshot 重启不重复网络和模型；
- Browser Capture 的浏览器只在用户动作/任务期间存在；
- collection 在执行前给出页面/字节/调用估算。

## 16. 测试矩阵

### 16.1 网页类型

- 静态文章；
- client-rendered SPA；
- 登录页 browser capture；
- Wiki 页面/revision；
- 长文章；
- 多栏/目录/脚注；
- 表格/代码/图片；
- 中文/英文/混合；
- redirect/canonical/AMP；
- 页面更新；
- 空 shell/错误页/验证码/付费限制。

### 16.2 安全

- SSRF/private IP/DNS rebinding/redirect；
- zip/compression bomb/超大 DOM；
- 恶意 HTML/SVG/URL scheme；
- Cookie/form/storage 泄漏；
- Prompt injection；
- 伪造 capture/version/hash；
- local file symlink/reparse point；
- 日志脱敏。

### 16.3 真实 E2E

至少维护：

1. 公开中文文章 URL；
2. 公开英文长文章 URL；
3. 一个官方 Wiki revision；
4. 一个用户授权 browser capture fixture（脱敏）；
5. 表格/代码结构页；
6. 同页面更新 revision；
7. URL -> Bundle -> iwiki commit -> restart zero replay；
8. Draft -> ReviewCandidate -> personal Publisher；
9. CLI JSON contract；
10. Browser Pack 未安装时 URL 快路径仍工作。

平台接缝还必须验证：`produce web` 与 generic `produce --recipe/--request` canonical request/plan digest 等价；与 Video/Document 共用 ProduceService、Registry、Job/Checkpoint、Bundle、Review/Publisher；通用层无 Web-specific import；Web 接入不改变前两类 Recipe 的 golden 和恢复语义。

## 17. 分期

### Phase W0：静态公开文章

前置完成 Recipe X0 和 Document 第二消费者 Gate；然后通过同一 ProduceService/Registry 实现 HTTP snapshot、Readability + block tree、Evidence、短文章 Knowledge Note、Bundle，并作为第三类消费者反馈 internal v1。

### Phase W1：长内容与质量

heading-aware map/compose、表格/代码、coverage/repair、真实长文章验收。

### Phase W2：Browser Capture

受控扩展/通道、sanitization、登录/动态页面，不读取 Cookie 数据库。

### Phase W3：Wiki 专项

单页面 API/revision/section/redirect；先不递归。

### Phase W4：受限 Wiki collection

页面清单预览、预算、增量 revision、collection manifest。

## 18. 完成定义

1. 公开文章 URL 可生成带离线 Evidence 的高质量 Draft；
2. 登录/动态网页通过用户触发 Browser Capture，而非读取或泄露 Cookie；
3. 原始/渲染 snapshot 不可变并绑定 SourceRevision；
4. 表格、代码、列表和章节结构可追溯；
5. 长文章使用全局 compose，不是分块文章拼接；
6. 网页变化创建新 revision，不覆盖旧来源；
7. 无法采集时 fail closed，不让模型补写未知正文；
8. 默认不递归爬站；
9. Bundle/iwiki/restart/Review E2E 通过；
10. SSRF、恶意 HTML、Prompt injection、Secret 泄漏 Gate 通过；
11. 未复制 ProduceService、JobStore、Checkpoint、Bundle、Review、Publisher 或 CLI Pipeline；
12. `produce web` 与 generic `produce --recipe/--request` 请求和结果语义等价；
13. Video + Document + Article/Wiki 三类真实消费者通过后才冻结 internal v1，公共插件 SDK 仍由独立高阶 Gate 控制。
