# ReviewCandidate 只读 show 验收

日期：2026-08-01
基线：`605e4ab` 之后的当前候选改动
范围：Video 与 born-digital PDF 的只读 ReviewCandidate 投影及单点 Evidence 导航

## 结论

本增量关闭 Produce 完成后的首个用户交付缺口：用户不再只能得到 Bundle/Draft opaque ID，而可以用 `alltonote review show <draft-id>` 查看来源、发布资格与完整质量理由，并且只在显式指定时展开一个 Video Evidence 或一个 Document note item。

ReviewCandidate 不是新的内容副本，也没有 Review 数据库。每次查询都从已提交 Portable Bundle 重新验证并投影；Draft、Source metadata、Quality report、Evidence、Knowledge Map 与 normalized content 的 hash、source revision 和 parent lineage 必须一致，否则 fail closed。

## 已冻结的命令合同

```text
alltonote review show DRAFT_ID \
  [--evidence-id EVIDENCE_ID | --note-item-id NOTE_ITEM_ID] \
  [--workspace PATH] [--config-profile PROFILE] [--json]
```

- 默认视图返回 Draft ID/hash/document kind、Bundle ID、安全来源摘要，以及每个关联 Quality report 的 profile、overall、全部 checks/reason 和 messages。
- Video `--evidence-id` 返回一个 `video-time-range.v1` locator 与对应 transcript excerpt；excerpt 的 SHA-256 必须匹配 Evidence record，target transcript 的 hash 与 Bundle reference 必须一致。
- Document `--note-item-id` 要求 `selected draft -> knowledge map -> normalized content` 父链成立，返回独立 verifier status/identity 以及关联 block 的 page、bbox、kind、basis 和原文。
- JSON 使用既有 Automation Protocol v1 单行 envelope；人类输出隐藏 Bundle/Quality artifact ID，只展示标题、质量问题和一个可读定位。

## 安全与边界

- 查询只使用 `PortableInspectionPort.inspect_committed`，不写 Vault、machine state、Draft 或 Bundle；
- 不调用模型、网络、下载器、转录器、Docling 或 Pack Worker；
- payload 有独立上限，JSON/NDJSON 拒绝重复键、非法 UTF-8、非有限数值、超限和不一致引用；
- Video link 只允许无用户凭证、无敏感 token/signature query 的 HTTP(S) URL；
- Document `file_name` 必须是 portable basename，盘符、rooted/UNC/device/path separator 输入直接拒绝，绝不显示私有本地路径；
- 不返回 Draft body、prompt、provider payload、绝对路径、source hash 或原始内部 JSON。

## 验证结果

定向 Review、Artifact、PortableGateway 与 cold-import 回归：

```text
87 passed
```

其中 11 项 Review 测试覆盖真实 Video Bundle、真实 Document Bundle、质量通过/失败、单 Evidence、Document note item、缺失 focus、双 focus usage error、Draft tamper、私有路径与对抗 lineage mutation。

独立 Gate `task_266b796972fa` 首轮发现 Source revision 和 Document artifact parent lineage 两项 P1。修复后定向 re-Gate `task_7e03b9fac0a9` PASS，结论为无剩余 P0/P1，工作器回归 `11 passed`。

完整 backend 回归（禁用 bytecode 与 pytest cache）：

```text
2598 passed, 3 skipped, 3 warnings, 3 subtests passed in 170.09s
```

跳过项均为既有平台/环境条件；三条 warning 为既有 downloader 转义与第三方 `pkg_resources` 弃用提示，不属于本 diff。本文件不声明 Publisher 或桌面端完成。

## 明确未完成

- 没有 Candidate list/filter/cursor；
- 没有 ReviewRecord、approve/reject/revoke 或 stale decision；
- 没有 Draft 编辑/diff；
- 没有 PublishPlan、personal/common 发布或 compare-and-apply；
- 没有 Desktop Review UI；
- 没有扩大 Portable Bundle schema 或引入第二正文副本。

下一步应继续 RP-02 的 Candidate discovery，或在真实用户审阅反馈证明需要后进入 RP-01/RP-03；不能把本只读切片误称为 Publisher 完成。
