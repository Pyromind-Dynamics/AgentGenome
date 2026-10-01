# v2 插件能力（包版本 0.3.0）

同一个 Pi 标准插件入口负责 `genome_*` 工具，Python 服务管理资产、节点请求、验收和连续修订。SDK 只提供当前会话、执行环境和模型适配，不注册另一套工具，也不再通过扩展工厂加载插件。

## 节点交接

`do.llm` 使用带上下文的 `Ports.agent_task`；原有 `AIPort.work(prompt)` 继续兼容。请求包含运行、节点、尝试、输入、资源目录和预期产物。宿主安排当前 Agent 会话执行，工具 `genome_step_result` 只登记回执；对应轮次正常结束且回执匹配后，内核才检查文件和执行验收。消息交付确认不是节点结果。

节点记录在 SQLite 的 `stage_requests`。重启将未结束请求标记中断，不投递旧任务、不重放脚本。等待不持有终端锁，状态与取消仍可用。SDK 使用它的通用阶段任务端口；原生宿主尚未接通阶段任务，会在运行前拒绝。

## 修订与接手

任务包显式声明修订策略，例如：

```json
{
  "requires": ["agent_task", "model_judge", "protected_verification"],
  "revision": {
    "safe_to_rerun": true,
    "editable_scripts": ["scripts/clean.py"],
    "verification_resources": ["scripts/verify.py", "scripts/judge.py", "prompts/criterion.txt"]
  }
}
```

`genome_prepare_revision` 返回独立可编辑目录、原始参数及允许修改的脚本。提示禁止修改验收；提交时只收集允许的业务脚本。`genome_run` 接受 `revision_id + params + change_summary`，每次提交冻结后从头运行，关联父运行，保留原公共版本。仅改本次参数值不发布；脚本或参数接口变化时冻结候选，当前任务及全部回归通过后自动发布新版本并切换 latest。修订不限制次数。每轮用最近的运行 ID 创建新副本，继承前一次冻结的脚本、参数与待验证案例；Pi 根据结果自行选择继续修订或 `genome_takeover`。每份副本只提交一次，重复请求返回原运行，避免误启动。执行已结束但结果不符合用户要求时，即使状态是 succeeded，也可 `genome_takeover`。接手保留原 graph 的执行状态和产物，不重跑脚本。活动运行或活动修订须先结束或取消。

`genome_get` 返回宿主与资产的 `revision_availability`，`genome_status` 返回本次运行的 `recovery`。显式禁止修订的资产、不支持修订文件准备和回收的宿主或用完修订预算的运行不能修订；可在运行结束后交还 Pi 按 Skill 完成剩余任务。

可选 `editable_parameters` 列出允许改声明的参数名，`editable_bindings` 列出允许改的 `节点完整路径.input.键名`。提交的 `parameter_patch` 仅含 `declarations` 和 `bindings`；保留旧参数类型及默认值，不允许删除旧参数；新增参数必须有默认值，绑定只能指向已声明的 `params.*`。无策略的旧纯脚本包允许新增可选参数及输入绑定，不能修改节点结构。不能改节点结构、输出或验收规则；编译和参数校验仍须通过。

修订依赖宿主的 `revision` 能力，与 `protected_verification` 分开。普通执行目录使用提示约束，返回 `verification_protection: prompt`；已有只读目录的宿主返回 `read_only`。重跑从原资产与允许的业务修改组装执行包，不采纳草稿中的验收或 graph 修改。提示不能保证模型不会修改运行时验收资源，不再将这种保证作为 revision 前提。显式声明需要 `protected_verification` 的旧任务包仍遵守其要求。

失败运行可直接申请修订；状态为 `succeeded` 但结果不符合要求时，须提供非空 `reason`，原因随修订记录持久化。`genome_prepare_revision` 的说明和返回结果都提示：只修改允许的业务脚本和参数，不得修改验收脚本、提示词、阈值或伪造结果；无法小改适配就 takeover。

## 模型验收

将 `agentgenome/judge.py` 复制到任务包并冻结，在 `verify.run` 中调用：

```text
python3 scripts/judge.py prompts/criterion.txt {artifact}
```

任务包声明 `requires: [model_judge]`。脚本使用宿主为本次验收提供的 `AGENTGENOME_JUDGE_DIR`，原子发布请求；宿主写完响应后发布完成标记。请求及响应有 256 KiB 上限，默认等待 120 秒。只有严格布尔值 `passed: true` 才通过，退出码为 0/1/2（通过/不通过/异常）。模型调用独立于聊天历史，没有工具和新 Pi worker；凭据不进入沙箱或文件。

## 当前支持范围

- 原生 Pi：继续支持已发布固定脚本经验及本地安装；新增能力未接通时启动前拒绝。
- SDK `os-sandbox`：支持阶段节点、受保护修订、模型验收。
- SDK 远程 `sandbox`：支持固定脚本和提示约束的修订；暂不声明阶段节点、只读验收保护和模型验收能力。

示例为 `templates/data-cleaning-v2`，原 `data-cleaning/1.0.0` 未修改。导入不等于发布，仍需验证成功后显式发布。

固定脚本修订示例为 `templates/data-cleaning-revision`（资产 `data-cleaning@1.1.0`），参数为 `data_file` 与 `delimiter`。保留旧版验收标准，声明安全重跑和可编辑脚本。`./start_sdk.sh test-revision` 现从旧 `1.0.0` 验证修订发布闭环；普通启动不会发布，原 `1.0.0` 保持不变。

## 候选版本发布

无 revision 声明的旧纯脚本包默认可修订，prepare 时填写 `editable_scripts` 和 `verification_resources`。已有策略继续使用原清单。验收资源不从草稿回收；约束以提示为主，不承诺远程运行目录只读。

脚本或接口变化时，`genome_run(revision_id, params, change_summary, ...)` 同时提交 `regression_cases`。旧版本没有样例时另提供 `baseline_cases`，AgentGenome 先用旧版本验证，再以候选执行全部案例。已冻结案例只能继承，新增案例不能使用相同 ID。共享样例只用小型合成数据。

案例格式：
```json
{"id":"comma","files":{"input.csv":"name,age\nAda,36\n"},"params":{"data_file":"fixture:input.csv"},"assertions":[{"node":"clean-sop.clean","output":"cleaned","kind":"csv","expected":[["name","age"],["Ada","36"]]}]}
```
`kind` 支持精确 CSV 行、text 和 JSON 字段子集。清洗案例同时断言列、有效行数与实际记录。各案例工作区隔离，整个验证只发送一次最终通知。

版本号事务分配为最高 `X.Y.Z` 的下一个补丁号；非该格式须传 `version`。候选内容提交后冻结。失败、取消、摘要变化不发布；latest 已离开基线时返回 publication.conflict，保留候选与证据，不覆盖他人版本。旧版本与旧运行记录不变。

已发布版本的回归样例不可改写。尚未验证通过的草稿样例可以纠正（如 CSV 断言漏掉表头），但仍须重新在原版本确认旧场景，并用新候选执行全部案例。历史运行和失败候选保留；服务重启不自动重放，已明确 takeover 的链路不会自行恢复。
