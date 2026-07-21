# Recipe X0-A Task 3 验收报告

> 日期：2026-07-21
> 状态：PASS
> 任务：建立最小 Recipe submission 合同

## 实现范围

新增两个文件：

- `backend/app/core/recipes/contracts.py`
- `backend/tests/core/test_recipe_contracts.py`

合同仅包含：

- `RecipeKey`
- `RecipeDescriptor`
- `InputDescriptor`
- `ProduceRequest`
- `ProduceSubmission`
- `RecipeEndpoint.submit`

未增加 Result、Plan、Artifact、Engine、资源 DSL、动态插件或 SecretReference 抽象。

## 不变量

- contracts 不导入 Video、Document、Web、Codebase、Runtime 或重型 Pack；
- DTO 使用 frozen slots；
- nested JSON mapping/list 深冻结；
- cycle、非有限数字、非 JSON 值和非字符串 key fail closed；
- contract version 只接受 1，拒绝 bool 和未知版本；
- Recipe version 与 contract version 独立；
- requested outputs 和 descriptor kind 序列保留调用顺序，不在合同层排序/去重；
- `InputDescriptor.attributes` 仅作为 spec 要求的轻量 JSON-safe attributes，不解释路径、授权或 Secret；
- errors 不回显原始输入值；
- `__all__` 不暴露 PreflightReport、RecipePlan、RecipeOutput、ProduceResult。

Secret 边界已保留为后续 Recipe-specific adapter 校验：合同不提供专用明文 Secret 槽位，但不能靠字符串启发式判断任意 opaque value 是否为 Secret。Task 5/7 必须在持久化前执行具体 profile/reference 校验。

## 测试证据

Task 3 专项：

```text
6 passed in 0.25s
```

X0-A 兼容集：

```text
298 passed in 11.05s
```

全量 Backend：

```text
1833 passed, 2 skipped, 1 warning, 3 subtests passed
```

`git diff --check` 将在提交前执行。

## 下一步

Task 4：实现静态 Recipe Registry 与薄 ProduceService。必须保持 contracts/registry/produce_service 不导入 Video；Video Adapter 仍延后到 Task 5。
