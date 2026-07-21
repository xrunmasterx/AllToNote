# AllToNote Article / Wiki Recipe 实施计划

```yaml
doc_type: plan
status: active
authority: execution
upstream:
  - ../specs/2026-07-18-alltonote-article-wiki-recipe-design.md
  - ../specs/2026-07-18-alltonote-recipe-extension-contract-design.md
implementation_status: not-started
last_verified_at: 2026-07-19
```

## 1. 成功标准

公开文章 URL、长文章、官方 Wiki revision 和用户触发 Browser Capture 至少各一条真实 E2E；snapshot/Evidence 离线可复核；长文全局 compose；表格/代码保留；SSRF/Secret/HTML/Prompt injection通过；Bundle/iwiki/restart/ReviewCandidate通过。Article/Wiki 作为 Video + Document 后的第三类消费者，必须复用同一 ProduceService/Registry/Job/Bundle，并负责验证 internal v1 是否可冻结。

### 1.1 前置 Gate

fixture、合法样本和工具调研可与 Document 并行；生产代码合入顺序为 X0-A -> 真实 Document/PPT + X0-B -> Article/Wiki 第三消费者。不得在等待期间创建独立 WebService/CLI/JobStore。Article/Wiki 合入后才评估冻结 internal v1，且不因此开放公共插件 SDK。

## 2. Task WEB-00：Fixture 与合法测试样本

建立：静态中英文文章、长文章、表格/代码、动态页脱敏 capture、Wiki revision、失败/登录/验证码/恶意 HTML。记录来源许可/用途；普通 CI 使用快照 fixture，真实 URL 用 opt-in smoke。

## 3. Task WEB-01：URL/Network Policy

新增 `core/recipes/web/` 与 `adapters/web/http_fetcher.py`。

先测 SSRF、private/link-local/loopback、redirect、DNS变化、scheme、MIME、size、compression、timeout、header/cookie脱敏。实现流式 fetch + immutable snapshot，不把网络重试藏进 Adapter。

## 4. Task WEB-02：Source Identity/Revision

实现 original/final/canonical/page ID信号、safe URL normalization、snapshot hash、ETag/Last-Modified辅助、重复导入/no-op/new revision。canonical 异常不自动合并。

## 5. Task WEB-03：DOM Sanitize 与多信号提取

可选 Mozilla Readability + DOM semantic/main/article + metadata/JSON-LD；在隔离/禁脚本环境解析。输出 block tree，保留 heading/list/table/code/quote/figure。提取报告比较 coverage、空 shell、登录提示、重复导航。

## 6. Task WEB-04：Block Evidence

snapshot hash + block hash + ordinal + anchors + DOM hint；实现 range reader/Review navigation。DOM path 不作为唯一定位。测试相同文本重复、Unicode、超长 block、sanitize 后定位。

## 7. Task WEB-05：短文章 Compiler

使用现有 ModelExecutor/Coordinator：grounded structured input -> Knowledge Note；默认来源语言/显式中文。确定性 Markdown/Evidence/coverage quality。没有提取正文时不调用模型。

## 8. Task WEB-06：长文章 Compiler

heading/block aware chunk、knowledge map、global outline/compose、citation freeze、targeted repair。复用长视频原则，不复用时间 chunk 或直接调用 VideoCompiler。测调用数、1 H1、重复章节、引用覆盖。

## 9. Task WEB-07：Asset/Table/Code

结构化表格/code/figure caption；相关图片才下载，origin/MIME/size/SVG安全；Asset manifest/hash。未启用图片时不抓取。模型不能改写代码冒充原文。

## 10. Task WEB-08：Bundle/Job/CLI

Artifact kinds、Receipt（fetch/extractor/model/compiler identity）、checkpoint/restart、cancel；`alltonote produce web --url|--file|--capture` 与 generic `produce --recipe/--request` 归一化为同一 ProduceRequest/plan digest，经 ProduceService/Registry 执行，共用 JSON/exit/errors。commit to raw/personal only；不复制 Application Service/Job/Bundle/Publisher。

## 11. Task WEB-09：Browser Capture Contract

先做 capture 文件/IPC contract 和脱敏测试，再做扩展/UI：

- 当前 tab/用户触发；
- sanitized DOM/readable text/assets；
- 删除 form values/scripts/session data；
- version/hash/size；
- loopback/native channel auth；
- 不读取 Cookie DB；
- dynamic/login真实 fixture。

扩展实现可以复用现有 `BillNote_extension` 作为壳，但不复用旧 backend Pipeline；新增最小“Capture to Runtime”能力。

## 12. Task WEB-10：Wiki Adapter

先支持一个有官方 API 的 Wiki：page/revision/section/redirect。API失败可降级 HTTP并降低 confidence。不递归。

验证同 page 新 revision、redirect/disambiguation、section Evidence。

## 13. Task WEB-11：受限 Collection（后续 Gate）

只有单页稳定后：seed/origin/path/max pages/depth/bytes，先 preview manifest/预算；每页独立 SourceRevision/Bundle；循环/redirect/增量。不要合成一个巨型 Source。

## 14. Task WEB-12：安全/故障/恢复

每个网络/parse/extract/model/bundle/commit边界 kill；SSRF/HTML/SVG/capture伪造/Prompt injection/secret/log；同 snapshot restart 不联网/不重复模型；Browser Pack缺失不影响公开 HTTP 快路径。

## 15. Task WEB-13：真实质量验收

对五类真实样本由人工检查：核心事实完整、表格/code、章节连贯、来源/翻译、Evidence点击、低置信披露。记录调用/耗时/成本/repair，不保存完整私密正文。

## 16. Task WEB-14：Review/Publisher

Article Draft -> Candidate -> evidence定位 -> edit/stale/approve -> personal plan/apply -> Vault search；网页更新新 revision -> new Draft diff，不覆盖旧 published。

## 17. Task WEB-15：验收与交接

输出 `docs/acceptance/article-wiki-recipe-v1.md`；更新 `RECIPE-WEB-01` 和 Recipe contract反馈。汇总 Video/Document/Web 字段消费者与兼容矩阵，全部 Gate 通过后才冻结 internal v1；公共插件 SDK 继续保持关闭。只有 Browser Capture真实链路通过才宣称登录/动态页支持；只有 Wiki API真实 revision通过才宣称相应 Wiki 支持。

## 18. 执行顺序

```text
Recipe X0 + Document 第二消费者 Gate
 -> WEB-00..04
 -> WEB-05/06
 -> WEB-07/08
 -> WEB-09
 -> WEB-10
 -> WEB-12..15
 -> WEB-11（单页稳定后）
```
