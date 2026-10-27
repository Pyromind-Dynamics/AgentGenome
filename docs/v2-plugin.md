# v2 插件能力（包版本 0.6.0）

同一个 Pi 标准插件入口负责 `genome_*` 工具，Python 服务管理资产、节点请求、验收和连续修订。SDK 只提供当前会话、执行环境和模型适配，不注册另一套工具，也不再通过扩展工厂加载插件。

轨迹沉淀（`genome_trace` / `genome_capture` / `genome_distill` / `genome_capture_status`）见 `docs/v2-capture.md`。

## 节点交接

`do.llm` 使用带上下文的 `Ports.agent_task`；原有 `AIPort.work(prompt)` 继续兼容。请求包含运行、节点、尝试、输入、资源目录和预期产物。宿主安排当前 Agent 会话执行，工具 `genome_step_result` 只登记回执；对应轮次正常结束且回执匹配后，内核才检查文件和执行验收。消息交付确认不是节点结果。

节点记录在 SQLite 的 `stage_requests`。重启将未结束请求标记中断，不投递旧任务、不重放脚本。等待不持有终端锁，状态与取消仍可用。SDK 使用它的通用阶段任务端口；原生插件在所属 Pi 会话空闲时交接任务，绑定客户端连接和执行轮次，自动补入回执的 execution_id。只有匹配回执和正常轮次结束后才验收产物。取消、交接连接断开或会话切换不会重发旧任务；原生交接最多等待 30 分钟。交接要求启动本次执行的 stage 客户端连接持续存活；即使在前置脚本结束前重新打开同一会话，也不能接续旧连接的阶段任务。失效的阶段消息在投递当轮被拦截并中止，插件不筛选或改写 Pi 发给模型的会话历史。独立的换参数 probe 尚未接入原生阶段交接，不应据主运行成功宣称这类链路已验证泛化。

运行中断需要停止当前阶段轮次时，插件先等待 Pi 回到空闲，再领取并投递结束通知；通知实际写入会话记录后才确认送达。

## 人工门禁与唤醒

人工门禁是 graph 里的普通节点（`do: {human}`）。回放跑到这里会停下等人：

- 运行状态变成 `waiting`，结果里带 `node` / `question` / `artifact`（人工产物要写到
  的确切路径），checkpoint 保留，执行线程释放；capture 回放停在人工节点时，沉淀报告
  写明「停在人工节点」，不会当成回放失败；
- 工具 `genome_resume(run_id)` 唤醒同一条 run：回到队列后从停下的节点继续，只批准
  这一个已答过的门禁，后面的门禁照常停下；run 不在 `waiting` 时只返回当前状态；
- 宿主必须提供交互式 human 端口（`capabilities()["interactive"] is True`），否则
  handshake 报 `E_NEEDS_NONINTERACTIVE`；SDK 桥接与原生插件都在停下时把
  node/question/artifact 交给 agent 转达用户，用户做完人工步骤后由 agent 唤醒；
- SDK 侧 `waiting` 不算终态：不发布 `operation.completed`，operation 保持打开；会话
  不在线时通知转为 pending，下一次会话开始再投递，门禁不会丢；
- agent 不得替用户伪造人工产物，也不能自己重放 graph 里的节点来「绕过」门禁。

## 沉淀范围复核

`genome_distill` 的计划比会话窄时，先由 `_scope_split` 判断被落下的步骤是否和链路相连：
相连的（产物喂给链路、或接在链路后面）直接报「链路范围需要用户复核」并列出，agent
必须先把这些步骤给用户看，用户确认这次只要后半段后，才带 `review_confirmed=true` +
`review_reason` 重新提交，确认过的边界写进报告；不相连的旁支（本地试跑小样、只写调试
文件的探测、被后续步骤取代的尝试）不挡沉淀，写进报告的「旁支步骤（未进链路，链路不
依赖它）」段，agent 不需要为它们问用户，也不该把它们硬塞进 `steps`。
`genome_capture` 侧的对应约束是：链路只收完整固化的图（`_require_solidified`）。
无法机械固化的步骤不会被声明进 manifest 蒙混过关，capture 直接失败并列出每一步的
序号与原因；改用 `genome_distill` 把这些步骤换成 `run` / `llm` / `tool` / `human`
节点，或者用 `final_artifact` 重新切片、把本次特有的产物声明成 `{参数名}` 输入边界。

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

原生 Pi 使用当前选定模型及 Pi 的凭据路由发起独立验收请求，不注入阶段消息，不使用 `genome_step_result`。只有支持模型调用的客户端连接才声明 `model_judge`；验收属于启动运行的具体连接，断连或取消不会把旧请求转交给重开的会话。模型调用上限 90 秒，本地交接上限 110 秒；超时、无凭据、模型错误、无效 JSON 或非布尔判定均按验收异常处理（退出码 2），不会视为内容不合格（退出码 1）或通过。两种失败都会使节点失败，详细区别保留在执行退出码和 judge 响应中。

验收脚本及标准从冻结源复制到每次运行独立的 `work/verification/`，复制后校验 pin；修订运行及候选回归的副本来自原始验收包，不来自可编辑草稿。验收命令在该副本中执行，普通相对路径的缓存或日志不会写入资产存储目录。人工恢复时重新准备验收副本，清除临时文件并恢复原标准，业务工作区和产物保持不变。提交不收集草稿中的标准或 judge 脚本修改。此处仍是提示约束，未提供操作系统只读隔离，也不声明 `protected_verification`；同用户权限进程显式访问或写入其他路径仍不在隔离保证内。用 `templates/native-model-judge-check` 独立验证该能力；它不要求 llm 阶段节点、通用工具或只读保护。

第一版限定每条 `verify.run` 命令只调用一次 judge；需要多个判定时，拆成多条独立验收规则。同一命令内重复或并行调用不受支持。严格布尔值和非空 reason 校验只保证判定格式，不保证模型能够抵抗证据中的提示注入。

## 当前支持范围

原生 `tool` 节点通过发起运行的 Pi 工具上下文执行当前会话的可调用工具，工具名精确匹配，保留 Pi 参数校验、钩子和权限处理，不直接调用工具实现。正常的计划/确认和沉淀工具入口自动绑定上下文；直接使用 `/genome validate` 的新会话须先通过一次真实 `genome_*` 工具调用建立上下文（例如 `genome_list`），命令上下文本身没有 `executeTool`。缺少接口时拒绝执行；`genome_*` 控制工具不能被图嵌套调用。要求 Pi 的 `ExtensionToolContext.executeTool`（实测 1.1.0）。

每个 execution_id 只派发一次，结果落盘并写入持久化 tool receipt。取消传递 AbortSignal，并等待真实调用返回；已完成副作用不会被撤销。不能确认结果或停止时保留 unknown，不自动重试，重开会话不接续旧调用。工具是否配合取消取决于具体实现；这不是任意外部系统的 exactly-once 保证。用 `templates/native-tool-check` 验证真实写文件及其验收。

Pi 会把工具抛出的异常和结果钩子错误包装成 `isError`，不能据此认定工具没有产生副作用。工具执行已经开始（Pi 返回 `durationMs`）后报错时，原生宿主记录 unknown，阻止节点自动重试、修订及回放重排；执行前被参数校验或权限钩子拒绝则保留明确失败。工具正常返回成功时沿用现有完成回执。

- 原生 Pi：支持固定脚本、人工等待与恢复、当前会话的阶段节点、独立模型验收、当前 Pi 会话的真实工具调用，以及提示约束的业务脚本修订（草稿在注册表 `revision-drafts/` 下准备与回收，提交只收集允许的业务脚本，验收脚本结构性保留原资产）；尚不声明只读验收保护能力。
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

### 原生本地异步等待

`await` 复用已有 checkpoint；本地服务每秒做一次短文件探测，pending 时不重跑图、不占图执行 worker，也不调用模型或 shell 轮询。当前协议为受理单 `{task_id?, output_dir}`，目录包含 `report.json` 和最终 `processed.jsonl`。相对 output_dir 以原工作区解析。成功报告先到而产物未就绪时继续等待，绝不将状态 JSON 冒充产物；无效受理单明确失败。

任务完成、明确失败或 deadline 到期后，服务恢复同一 run，内核验收并继续，上游提交节点不重跑。等待按 `kind: external` / `phase: waiting_external` 展示，不发人工任务单。超时沿用原 checkpoint deadline，不因重开会话延长。

服务重启会从持久化等待记录重建监测；存在外部等待时服务不会因空闲退出。纯本地脚本链可在 Pi 断开后继续；含工具、模型阶段或模型验收的图采取保守策略，须有同会话的有效能力上下文才继续（包括到期失败的处理）。重开原 Pi 会话后先调用一次 genome_list 绑定新工具上下文，插件状态同步后恢复等待；旧连接的执行请求不会转移。进程被停止、电脑关机期间没有后台探测，下次启动按原 deadline 处理。

取消停止该 run 的等待和后续执行，不保证停止已提交的后台任务；迟到结果不会唤醒 cancelled run。此功能不是通用远程平台查询或公网回调服务。用 `templates/native-await-check` 和其可选 Pi 演示工具验证真实后台进程，而不是仅用模拟 AwaitPort。
