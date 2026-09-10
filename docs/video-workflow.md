# 默认视频笔记 workflow

采用此前 `context-evidence` 实验版（B9RCf6aAO-4 对照中的 `experimental`），不是后续 visual-demand v1/v2。

## 唯一维护入口

- `backend/app/core/application/faithful_edition_compiler.py`
- `backend/app/core/recipes/video/faithful_edition/contextual.py`
- `backend/app/core/recipes/video/faithful_edition/` 中的契约、提示词和复核逻辑
- `backend/app/runtime.py` 中的 `_RuntimeFaithfulEditionCompiler`

正式入口启用 `contextual_workflow=True`、局部回退以及每段最多24条来源引用。编译器保留原接口的兼容分支，不另存旧workflow源码副本。

流程：来源转录及候选帧 → 全文语境/章节规划 → 分批纠错及核验 → 事实与图像理解 → 正文及摘要 → 语义复核与局部恢复 → 按阅读章节合并、选择全文概览。

模型仍尊重运行时选择；Codex通道的全部faithful阶段使用high思考强度，与实验一致。选择gpt-5.6-terra时保留已有的明确容量不足降级到gpt-5.6-luna策略，不把鉴权、策略拒绝或未知超时当容量不足。

原实验运行器里的完整末尾JSON恢复已接入正式模型适配器，仅用于faithful结构化响应；不修改对象值，也不跳过后续契约/语义校验。运行时编译身份和执行策略身份已更新，防止旧流程检查点被当成新流程产物。

## 保留内容

本地 `local-data/retained-workflow/` 保存该版本两次成功生成的成稿、图片、调用记录及原始计时：

- `B9RCf6aAO-4/note.md`：模型阶段25分06秒，103次请求，13章、53张图。
- `nFKiwGHqpsA/note.md`：模型阶段22分54秒，96次请求，12章、67张图。

这两个目录不是可执行workflow副本。原实验目录及其余版本移入系统回收站。用户Trading笔记、输入媒体、运行环境、Cookie不在清理范围内；成稿与认证数据不提交远端。

## 结论边界

这是已有对比中质量、速度折中更好的主线，不是已证明无误的转录系统。仍有图内定位、多锚点说明、部分术语和指代问题。25分06秒是单视频历史模型阶段耗时，不含下载/转录，不能作为平均耗时或并发吞吐保证；推广接线后未重新付费跑整片。

## 回归验证

首批288项通过，包含contextual生成、来源纠错、局部恢复、图片预算、阅读布局、模型适配以及正式入口开关。

广泛回归命令：`python -m pytest tests/core tests/adapters tests/runtime tests/integration tests/test_model_slots.py -q --tb=short`。首次2418通过、9失败、2跳过；7项失败为旧集成测试替身没有模拟新阶段/逐段检查，已适配新契约及调用次数断言。

另2项失败在修改前冻结源码上也复现：`test_fast_whisper_module_import_does_not_require_legacy_event_entrypoint` 缺少可选的faster_whisper包；`test_runtime_doctor_dynamic_results_are_structured` 的环境健康断言失败。未修改无关环境或放宽测试。

排除这2项后复跑：2424通过、2跳过、2未选中，另1项已有后台worker测试在读取刚创建但尚未写入的PID文件时失败（空字符串转整数）。该测试文件未修改，随后单独重跑整个 `test_video_detach_engine_e2e.py`：5项全部通过（17.71秒）。测试验证行为和契约，不代表所有模型输出都准确。
