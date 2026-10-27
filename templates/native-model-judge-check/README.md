# 原生模型验收最小样例

仅要求 `model_judge`，不含 llm 节点。固定事实及验收标准位于
`prompts/criterion.txt`；`scripts/judge.py` 原样来自 `agentgenome/judge.py`。
`scripts/report.py` 只把输入报告写成产物，便于独立验证验收能力。

在安装了本次插件的原生 Pi 中执行（import 路径换成自己的仓库绝对路径）：

```text
/genome import /你的仓库/templates/native-model-judge-check
/genome validate native-model-judge-check 1.0.0 {"summary":"订单总数12笔，已发货9笔，待发货3笔。"}
```

预期：模型验收通过，run 为 `succeeded`，验收脚本退出码为 0。

```text
/genome validate native-model-judge-check 1.0.0 {"summary":"订单总数100笔，全部已经发货。"}
```

预期：模型验收不通过，run 为 `failed`，验收脚本退出码为 1。
缺失凭据、HTTP 错误、超时或非法判定时，run 同样失败，但退出码为 2，
具体原因保留在执行日志和运行工作目录 `.judge/` 下的响应中。
不应通过改写事实、judge 脚本或标准把不合格报告变成合格。

修订只允许收集 `scripts/report.py`，验收仍使用原始标准。当前保护等级为
提示约束，不包含操作系统只读隔离。模型判断存在波动，这两个样例不代表全面质量评测。
