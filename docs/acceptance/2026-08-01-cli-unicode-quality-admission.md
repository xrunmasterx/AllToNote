# CLI Unicode 与 Review 发布准入验收

日期：2026-08-01
基线：`360f388` 之后的当前候选改动
范围：Windows 非 UTF-8 控制台输出，以及只读 ReviewCandidate 对历史 Quality 的当前发布准入解释

## 结论

本增量关闭两个会阻断后续 Review/Publisher 的真实问题：

1. CLI 的 human、JSON、JSONL 与错误输出在 Windows CP936/GBK 流上遇到无法编码的 Unicode 字符时，不再抛出 `UnicodeEncodeError`；进程输出统一切换为严格 UTF-8，保留原始码点，不使用替换字符或转义降级。
2. ReviewCandidate 不再把旧 Bundle 中历史的 `publish_eligible=true` 或 profile 名称直接当作当前发布准入。历史 `quality.overall` 保持原值；旧 `alltonote.document-note@1` 与 native-extraction profile 均 fail closed；Document Knowledge Note 还必须从已提交 Quality Report 与 Knowledge Map 交叉证明独立语义验证，不能只信任同名 profile。

这不是 ReviewRecord 或 Publisher 完成：没有审批数据库、approve/reject/revoke、PublishPlan 或 Wiki 写入。

## 输出合同

- 生产 `alltonote` entrypoint 在解析参数前配置 stdout/stderr 为 `UTF-8 + strict`；可注入的 `main()` 不修改调用方全局流；
- 所有结果输出继续走同一个 renderer，JSON 保持 Automation Protocol v1 单行 envelope，JSONL 每条记录后 flush；
- 注入的普通 UTF-8 流与 `StringIO/capsys` 行为不变；不能重新配置但具有 binary buffer 的流使用 UTF-8 字节 fallback，不静默丢失字符；
- redaction、stdout/stderr 分离、exit code 与路径隐藏合同不变。

真实 V03 Bundle 的来源标题以 `🚀Orca ADE` 开头。强制子进程 `PYTHONIOENCODING=cp936` 后：

| 命令 | 结果 |
|---|---|
| `draft show art_019fb981-9178-7bb3-b541-d1e5510d36c6 --json` | exit 0；单行有效 JSON；stderr 空 |
| `review show art_019fb981-9178-7bb3-b541-d1e5510d36c6 --json` | exit 0；单行有效 JSON；完整保留 `🚀`；stderr 空 |
| `review show art_019fb981-9178-7bb3-b541-d1e5510d36c6` | exit 0；human 输出完整保留 `🚀`；stderr 空 |

## Quality 准入合同

Review 投影新增 `quality.admission={status, reason}`，并把 `quality.publish_eligible` 定义为“存储事实为非失败且当前策略能够从持久化产物证明准入”。Video 的 `pass_with_warnings` 仍保持可发布。当前受支持集合为：

- `alltonote.video-course-note@1|2`；
- `alltonote.video-faithful-edition@1`；
- `alltonote.document-knowledge-note@1`，但必须同时满足：Quality Report 为 model 方法、`knowledge-note-quality` 与 `source-coverage` 均通过、Knowledge Map 中编写模型与验证模型不同、每个 note item 恰有一个 `supported` claim。

上述 Document 判定只读取 Bundle 内已经存在的哈希绑定产物。普通 `review show` 只输出准入布尔结果和稳定原因，不输出编写/验证模型身份；只有用户显式请求某个 `--note-item-id` 时，才按既有合同显示该 item 的验证状态。

旧 Bundle 不回写。真实中文 PDF Draft `art_019fb412-b713-7df0-b90e-485a546b85d3` 的历史 Quality 仍显示 `overall=pass`，但 `alltonote.document-note@1` 只证明当时的 native extraction 检查，因此当前结果为：

```text
publish_eligible=false
admission.status=blocked
admission.reason=legacy-document-quality-profile-not-publishable
```

Human 输出同步显示发布被阻断及原因。查询前后真实 Workspace 的全部文件大小和 mtime 快照一致，证明本切片保持只读。

## 自动化验证

- Unicode renderer、Review 与 Job focused：`64 passed`；
- 完整 backend：`2611 passed, 3 skipped, 3 warnings, 3 subtests passed`。

独立 Unicode 二次 Gate 已 PASS：17 个聚焦测试证明 CP936 fallback 从第一条记录开始保持纯 UTF-8，且可注入 `main()` 不污染调用方 stream。独立 Quality 二次 Gate 也已 PASS：18 个聚焦测试与补充准入矩阵证明 Document 独立验证绑定成立，Video course-note v1/v2 与 faithful-edition v1 的 `pass_with_warnings` 兼容性保持不变，且未知、存储失败、legacy 与 native profile 继续 fail closed。

三个 warning 是既有 downloader 转义与第三方 `pkg_resources` 弃用提示。三个 skip 是既有平台/环境条件。本增量使用 `PYTHONDONTWRITEBYTECODE=1` 且禁用 pytest cache。

## 仍未完成

- ReviewRecord 的精确 Candidate digest、approve/reject/revoke、SQLite CAS 与恢复；
- PublishPlan/no-op/apply/receipt/reconcile 和 personal/common 权限边界；
- Candidate list/filter/cursor 与 detached Job 结果收件箱；
- Engine 毒任务有限重试、idle-spin 修复与真实多 Job admission；
- 当前 HEAD Runtime V14、安装器、签名、更新/回滚/卸载和干净非管理员 Windows 验收。
