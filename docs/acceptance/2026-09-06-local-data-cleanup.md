# 本地资料整理与冗余清理

```yaml
doc_type: acceptance
status: completed
authority: evidence
verified_at: 2026-09-06
source_commit: 913a3a270fe7e81c3add23beae0c992e37454275
```

## 整理结果

G 盘根目录散落的 14 个资料目录已归入 `G:\AllToNote\local-data`，按工作区、发布候选、
浏览器配置、验收证据和备份分类。该目录同时由 `.gitignore` 与 `.dockerignore` 排除；
视频、笔记、模型、Cookie、机器配置及本地清理脚本不进入 Git 或 Docker 构建上下文。

第一轮将 59 个旧构建、实验环境和重复依赖目标（约 25.663 GiB）送入回收站。
第二轮再将 8 个目标（约 1.063 GiB）送入回收站：

- 两套独立 Docling 模型下载；所有大于等于 1 MiB 的文件均与保留的签名文档 Pack
  实际文件 SHA-256 一致，小文件另存为下载元数据。
- 三个明确标记 `INVALIDATED` 的 Wave 0 快照中的恢复检验副本；原始 Git bundle、
  补丁和元数据保留，未将失效快照误标为有效恢复点。
- 前端 `dist`、空 V5 验收目录、已备份的旧 BiliNote checkout。

回收站未清空，上述数字不代表已经释放的磁盘空间。前端已安装依赖、V20 两套完整验收环境、
V20 Runtime、签名 Pack、有效笔记和原始输入继续保留。

## 重复工作树与恢复性

`codex/video-dogfood-validation` 与清理前的 master 同为 `913a3a2`，没有独有提交或
未提交源码，且没有活动终端。移出 8 个试用及构建资料目录和日志后，通过 Orca 移除该工作树
及可再生虚拟环境；试用资料位于 `local-data/workspaces/video-pilot/`。
独立 `codex/iwiki-readonly-client` 分支及工作树保留，其 9 个独有提交未被合并或删除。

两个新备份均通过 `git bundle verify`，并包含完整历史：

| 本地备份（位于 `local-data/backups/`） | SHA-256 |
| --- | --- |
| `2026-09-06-before-redundancy-cleanup.bundle` | `9c820012dd998bbca4ee3b7c6f9982ab80f6dc44620e6ff9f0955694d072d063` |
| `BiliNote-upstream-20260906.bundle` | `aa5e595b997fa2a523719bdafb12526ece2cad71f7713fd6a5ffce705391f003` |

Git bundle 可恢复源码及分支历史；虚拟环境需要重建。回收站可恢复被回收的目录。

## 验证

- 首次搬迁前后，14 个目录的文件数量与总大小一致。
- V20 与三个签名 Pack 的 46,754 个清单文件全部通过长度与 SHA-256 校验。
- 9 个搬迁工作区保留原实例 ID，登记路径均存在；默认工作区可读取。
- YouTube 工作区原有 3 条任务和成功笔记可读取；四个试用工作区分别可读取 4、3、2、2 条任务。
- YouTube 指南的 10 段 PowerShell 示例通过语法解析；入口修正为 V20 的 `alltonote.cmd`，
  下载依赖使用独立虚拟环境，不修改候选内置 Python。
- `git check-ignore` 确认本地资料被排除，Docker 根忽略规则已覆盖 `local-data/`。
- `git diff --check` 通过。

本轮没有修改业务源码，没有重新生成笔记、运行全量业务测试或构建 Docker 镜像。
历史报告、数据库请求和签名清单中的原始路径不改写；重跑历史任务应使用新路径重新提交。
V20 源码仍为 `68d517f`，并非当前 master 的同提交运行包。
