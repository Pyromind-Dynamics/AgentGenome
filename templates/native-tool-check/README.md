# 原生 Pi 真实 tool 节点验证

本样例要求 `tool`，调用当前 Pi 会话的内置 `write` 工具，再读取真实文件验收。
参数 `target` 是测试文件的绝对路径；请使用临时目录中的新文件，避免覆盖已有内容。

工具名与 Pi 当前可调用工具的名称精确匹配，不做别名猜测。实际调用经过 Pi 的
参数校验、tool_call/tool_result 钩子及权限处理。未知工具、缺少必填参数和权限拒绝
会失败，不能当作成功回显。AgentGenome 自身的 `genome_*` 控制工具不可成为 tool 节点。

原生 Pi 需要提供 `ExtensionToolContext.executeTool`（实测 Pi 1.1.0）。正常对话中
通过 `genome_prepare_run` / `genome_confirm_run` 等工具发起运行会自动绑定该上下文。
开发时若直接使用 `/genome validate`，先在同一会话让 Agent 调用一次 `genome_list`；
命令上下文本身没有工具执行接口，未绑定时会明确拒绝，不绕过 Pi 直接调用工具实现。

```text
/genome import /你的仓库/templates/native-tool-check
/genome validate native-tool-check 1.0.0 {"target":"/你的临时目录/created.txt"}
```

成功标准：文件内容是 `agentgenome real tool` 加换行，run 为 succeeded，工具 receipt
为 completed。只看到工具名/参数的回显不算成功。

取消请求会传给真实工具的 AbortSignal。工具已完成的副作用不会被取消撤销；只有工具
返回明确结果才记录 completed/failed。断连、超时或无法确认停止时记录 unknown 并阻止
自动重放；不能承诺对任意外部系统实现事务式的 exactly-once。服务重启不重新派发旧调用。
