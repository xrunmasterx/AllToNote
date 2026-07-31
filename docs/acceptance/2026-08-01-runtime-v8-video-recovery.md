# Runtime Windows V8 视频恢复验收

日期：2026-08-01

状态：当前主机验收通过；不是公开发行或干净虚拟机认证

## 1. 验收范围

本轮验证聚焦两个实际故障闭环：

1. `llm-iwiki` 在 Windows / CPython 3.14 下使用混合文件身份来源，导致提交阶段错误判断文件被替换。
2. `alltonote job retry` 创建子 Job 时没有继承原 Job 的冻结 Pack 环境快照，导致重试在执行前失败。

验收使用既有的三个真实 Bilibili 输入，要求生成任务成功、质量门通过、结果可发布，并确认面向人的正文不再出现编号式机器证据列表。

## 2. 固定的软件身份

### llm-iwiki 0.1.3

- 源提交：`1fff39fe54ba0cff16df0a4d31111dbc966dd88b`
- 变更：Windows 文件与路径身份统一由 Win32 原生句柄获取；POSIX 保持原有 stat 身份逻辑。
- Wheel：`llm_iwiki-0.1.3-py3-none-any.whl`
- Wheel SHA-256：`b8cfa583b008f4840688ab5ee703fe49e5d36e0ad3d38ba13c590d172495b7be`
- CPython 3.14 可移植契约测试：208 passed，8 skipped。
- CPython 3.11 全套测试：658 passed，14 skipped。
- 独立 Gate：PASS，无 P0/P1。

### AllToNote Runtime V8 候选

- 源提交：`edc3a6d91af1c77f1357bbc1019189c188474bf4`
- Python：CPython 3.14.6，Windows x86_64。
- SQLite：3.53.4，WAL Gate 通过。
- Runtime Wheel：`alltonote_runtime-0.1.0-py3-none-any.whl`
- Runtime Wheel SHA-256：`e7282cd7744b363d149a900d4f19d484ad705dee56eda49b678b45d952c8acd7`
- 候选目录清单：696 个文件，独立逐文件复核 0 个错误。
- `release/file-manifest.json` SHA-256：`e4e83d6e395f99ff73ece567b95b2982cecd5312aa3e69b89eccb2e96c7c07b4`

运行前的动态 Pack doctor 均通过：

- `media-basic`：`sha256:c50a8edb2b530b70fdccade4fb7ddfebf8c3a6792702e660eb85f69f5c189e24`
- `transcribe-cpu`：`sha256:d47c7568cc0e27b4f75fb63b86a8195ddc09a1f260e9512238e17220b8e3f970`

## 3. 真实输入结果

| 输入 | Job | Bundle | 结果 | 耗时 |
| --- | --- | --- | --- | ---: |
| V01：Kimi K3 + Freqtrade 量化策略 | `job_019fb976-ed88-7e21-8e07-af5f10cfe05e` | `bnd_019fb976-ed88-78f5-b6a2-0b528a45fe61` | quality pass；publishable | 约 367.6 秒 |
| V02：从零实现自己的 Agent 第一期 | `job_019fb97d-cdfe-7011-adc0-a9bb7c5238e6` | `bnd_019fb97d-cdfe-7592-af4b-a05aa8789855` | quality pass；publishable | 约 231.3 秒 |
| V03：Orca ADE 多 Agent 编程工作流 | `job_019fb981-9178-7dc9-94f5-85029bb3b19d` | `bnd_019fb981-9178-7095-a5fe-8dcaea1f11b8` | quality pass；publishable | 约 355.5 秒 |

V01 原 Job `job_019fb940-e2ce-7d8b-8ffc-9f26340e11f0` 已生成候选结果，但在旧版 `llm-iwiki` 提交阶段失败。第一次缺少 Pack 快照的重试子 Job `job_019fb96a-b94e-7169-8356-9faf61babaeb` 在排队状态被显式取消，没有进入执行。V8 修复后的重试继承了原 Job 的冻结 Pack 快照并成功完成。

## 4. 重试语义边界

本轮修复只解决冻结 Pack 环境的继承，不改变 checkpoint 所有权：

- 子 Job 继承原 Job 的有效配置快照与唯一的 `execution.pack-environment.v1` 事件。
- Pack 快照缺失、重复或格式损坏时继续 fail closed。
- 跨 Job 不迁移父 Job checkpoint，也不复用以父 `job_id` 为键的外部操作结果。
- 因此 V01 的成功重试重新执行了采集、ASR 和模型调用；这符合当前实现，但不是低成本的断点恢复。

独立 Gate 对 Pack 快照继承给出 PASS，同时明确禁止把这次修复描述为“跨 Job checkpoint 恢复”。本轮没有为减少一次重跑而引入 checkpoint 迁移协议。

## 5. 自动化验证

- AllToNote 后端全套：2301 passed，2 skipped，3 subtests passed。
- 最终 Runtime 锁文件与相关路径测试：73 passed。
- 重试与直接影响面测试：117 passed。
- V8 Runtime 文件清单独立复核：696/696 通过。

## 6. 已知但不阻塞的问题

1. 规范化 Draft 仍保留大量 Markdown 脚注定义；编号式机器证据列表已移除，但脚注尾部的阅读密度仍高。用户已经将整体格式优化安排到后续使用阶段，本轮不扩大范围。
2. Bilibili 来源元数据中的展示标题仍可能乱码，而生成正文标题和中文内容正常。这是独立的来源元数据编码问题。
3. 当前 `job retry` 是“带冻结配置重新执行”，不是同一 Job 的断点续跑，可能重复产生下载、ASR 和模型成本。
4. 本记录只证明当前 Windows 主机、隔离用户目录和真实输入通过；未完成无源码 checkout、非管理员、Defender 开启、全新 Windows VM 的公开发行验收。
5. Runtime V8 是可复核的候选，不等同于正式签名安装器、在线更新通道或完整 Windows 稳定版。

## 7. 结论

Windows / CPython 3.14 文件身份问题和跨 Job 重试缺少 Pack 快照的问题都已修复。三个最终真实输入在 Runtime V8 上全部通过质量门并生成可发布结果，V8 可以作为后续干净机器发行验证的固定候选。
