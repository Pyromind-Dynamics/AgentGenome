# AgentGenome 轨迹沉淀方案 v2（Capture）

把 Pi 会话里已经跑通的一条（或多条）操作链路，沉淀成可复用的 graph + 固定脚本，
落到用户自己的 Storage 空间，供后续直接复用、反复执行。

本文是实施方案，不是宣传稿。所有「能做什么」都对应到具体模块、数据结构和验证方式；
所有「暂时不做」都明确写出来，避免实施时蔓延。

## 0. 决策摘要

| 议题 | 决策 | 说明 |
| --- | --- | --- |
| `try` 节点 | **v2.0 不开** | 服务层闸门保持关闭（`agentgenome/service.py:148`）。多轨迹里识别出的分支步骤只记录进报告，不写进 graph |
| capture 触发点 | **用户明说「沉淀一下」** | 不做 agent 主动提议、不做自动触发。链路打通后再迭代 |
| 语义归纳的入口 | `genome_distill`（Stage 0） | 会话里混着内联代码、只读探测、脚本内部写出的产物时，机械固化必然失败。agent 先补全脚本、把输入产出写到命令行上，再提交规范化链路；插件校验契约后直接排队回放 |
| 归纳由谁做 | **Pi 里的 agent，不是插件** | 插件只做机械工作（归一化/对齐/切片/校验/编译/回放），不调用模型 |
| 脚本 | **只搬运，不重写** | agent 写过的 `.py` 原样进 `scripts/`，仅做槽位替换 |
| 资产隔离 | 用户 Storage 维度 | 依赖「一个 AgentGenome root 只服务一个用户」，见 §8 |
| 发布 | 机械闸门自动判定，不弹确认 | 回放通过即可发布；不通过保持草稿并返回报告 |

## 1. 目标

以一个真实场景为准绳：

```
预打标 → Label Studio 人工复核 → 数据清洗/处理 → 训练 → 评测
```

用户手动跑完这一整条链路后，说一句「沉淀一下」，AgentGenome 应当能：

1. 从会话事件里提取出**正确的**步骤序列，丢掉试错、失败、被拒绝、无关的步骤；
2. 把步骤之间的数据依赖还原出来（谁读什么、谁写什么）；
3. 生成 graph（骨架 + 参数槽位）和固定脚本，编译通过；
4. 用同一份数据回放成功（证明可复现），用另一份数据回放成功（证明可泛化）；
5. 发布写进用户自己的资产库，后续 `genome_run` 直接复用。

## 2. 非目标（v2 明确不做）

- 不做 agent 主动提议「要不要沉淀」。触发只有一个：用户明说。
- 不开 `try` 节点，不生成条件分支。多出来的走法只记录。
- 不做跨用户的经验共享、不做经验市场。
- 不调用模型做归纳。需要语义判断的地方，交回给 Pi 的 agent，插件只提供证据和校验。
- 不重写用户的脚本。语义改写由 agent 在 capture 之后按需发起 revision。
- 不做自动聚类判断「哪些会话是同一个任务」。v2 由用户/agent 显式给出 trace 列表。
- 不支持内联 shell 代码（heredoc / `python3 -c`）自动固化，第一版只支持落盘文件，其余记为未固化步骤。

## 3. 现状盘点

已经有、直接复用的部分：

| 能力 | 位置 | capture 里怎么用 |
| --- | --- | --- |
| GraphTree 编译与校验 | `builder/compiler.py` | 生成的 graph 走同一条编译路径，非法结构立刻报错 |
| 执行引擎 + ledger + artifacts | `core/engine.py`、`core/ledger.py`、`core/artifacts.py` | 回放验证直接用引擎跑 |
| 断点与 resume | `core/checkpoint.py` | 回放中断可恢复 |
| 资产库 + 版本 + 修订 | `agentgenome/service.py`、`agentgenome/revisions.py` | 发布走现有 `publish`，修订走现有 revision |
| 回归用例格式 | `docs/v2-plugin.md`（候选版本发布节） | 回放验证复用 `regression_cases` 断言格式 |
| Pi 插件入口与工具注册 | `pi-extension/index.js`、`session-trace.js` | `genome_trace` 已落地；`genome_capture` 随工具层一起注册 |
| Pi extension 上下文 | `@earendil-works/pi-coding-agent` 的 `ExtensionContext` | 读轨迹的唯一来源；`registerTool` 的 `ctx` 已接通 `dispatch` |
| SDK action 通道 | `harness_adapter/pi_adapter/adapter.py`、`conversation_runtime.workflow_call` | `workflow.invoke` 通用转发，新增 action 不改分发代码 |

缺口（本次要补的）——已全部补齐：

1. ~~没有接通 Pi 的 `ctx`。~~ 已补：`session-trace.js` 只依赖 `ctx.sessionManager`，
   桥接路径与原生 Pi 路径共用同一段代码。
2. ~~没有轨迹归一化层。~~ 已补：`agentgenome/capture/trace.py`（Stage A）。
3. ~~没有多轨迹对齐算法。~~ 已补：`agentgenome/capture/align.py`（Stage B）。
4. ~~没有「从产物回溯最小子图」的切片逻辑。~~ 已补：`agentgenome/capture/slice.py`（Stage C-1）。
5. ~~没有 provenance 校验。~~ 已补：`agentgenome/capture/emit.py` 的 `_verify_provenance`，
   生成时即校验，违规直接抛错，不允许绕过（Stage C-2）。

### 3.1 实现落点

| 层 | 文件 | 职责 |
| --- | --- | --- |
| Pi extension | `pi-extension/session-trace.js` | 会话枚举（`listSessions`）与轨迹导出（`exportTraces`），240 KiB 字节预算裁剪 |
| Pi extension | `pi-extension/index.js` | 注册 `genome_trace` / `genome_capture` / `genome_capture_status`，`dispatch` 里把 `trace_ids` 换成落盘的 `trace_refs` |
| 分发 | `agentgenome/dispatch.py` | `capture` / `capture_status` 分支；`_traces` 经 `host.read` 读回落盘轨迹 |
| 流水线 | `agentgenome/capture/pipeline.py` | 编排 A→E，决定降级并写报告 |
| 注册表 | `agentgenome/captures.py` | capture 状态、报告、回放 B、发布判定；状态不写回包内 manifest |
| 服务 | `agentgenome/service.py` | `captures` 注册表、`versions()`、`publish(evidence=...)`、run 收尾时调 `captures.finish` |
| 宿主 | `agentgenome/local_host.py`、`local_service.py` | `LocalExecutionHost.read`；`capture` action 复用同一 host |
| SDK 桥 | `harness_adapter/agentgenome_bridge.py` | `ExecutionHost.read()` 走 `execution.resolve` + `execution.read`（256 KiB） |

## 4. 核心原则

1. **插件不调模型。** 所有语义判断（哪一步是核心、哪个是垃圾、参数叫什么）由 Pi 的 agent
   基于插件给出的证据完成。插件只做确定性计算。这保证 capture 本身可测试、可复现、零 API 成本。
2. **证据优先于生成。** 每个生成的 graph 节点必须带 `provenance`，指回真实的 `step_seq`。
   校验时节点的 provenance 并集必须**恰好等于**骨架步骤集合；多报的报 `invented_steps`，
   漏报的报 `uncovered_steps`。
3. **脚本不重写。** 只做搬运和槽位替换（把硬编码路径换成 `{input.x}` / `{output.x}` / `{artifact}`）。
   重写脚本引入不可控风险，且违背「固化的就是用户真跑过的东西」这个前提。
4. **多轨迹对齐是核心。** 单条轨迹只能证明「这样能跑通」，多条轨迹才能区分
   「必需步骤」和「偶然步骤」。对齐单位是步骤签名，不是自然语言。
5. **失败可降级，不可静默。** 任何无法处理的东西（垃圾步骤、无法固化的内联代码、分支走法）
   都要出现在报告里，而不是悄悄丢掉。

## 5. 数据模型

### 5.1 原始轨迹来源

轨迹来自 **Pi 标准 extension API**，不依赖任何 SDK 私有接口。

`ExtensionContext.sessionManager`（只读）提供 `getEntries()`、`getTree()`、`getBranch()`、
`getSessionDir()`、`getSessionFile()`、`getSessionName()`。其中 `SessionMessageEntry.message`
是完整的 `AgentMessage`，工具调用与其结果都在里面。`registerTool` 的
`execute(callId, args, signal, onUpdate, ctx)` 本来就会传入 `ctx`；`genome_trace` /
`genome_capture` 直接用它（已接通），不再经过 `createBridgeHost`。

这条路径让插件对所有 Pi 项目开箱即用：SDK 集成场景和原生 Pi 场景用的是同一个 API。

**跨会话来源**：`ctx.sessionManager` 是当前会话的只读视图。`getSessionDir()` 给出会话目录，
目录里的 JSONL 可以用 Pi 导出的 `parseSessionEntries` 解析。但目录布局 per-host 不同
（SDK 是 `conversations/<id>/pi/`，原生 Pi 是 `~/.pi/sessions/...`），所以「列出候选轨迹」
这一步需要按宿主适配，适配层留在 extension 侧。

**v2.0 拉取范围（默认）**：显式指定的 trace 列表；每个 trace 拉取该会话的全量 entries，
可选 `max_steps` 上限。时间窗口过滤保留参数但不作为默认行为。

**容量约束**：SDK 侧 bridge 的单帧上限是 1 MiB（`agentgenome_bridge.py` 的 `_write`
按 256 KiB 分片即为此），而完整轨迹很容易超过。因此轨迹一律**落盘传递**：
extension 侧先把选中的会话导出成文件，capture 只接收路径，不内联传事件数组。

**单文件上限**：宿主读文件的 API 一次最多 256 KiB（`workflow-execution.ts` 的
`execution.read`），没有 offset 参数。所以 extension 侧导出时按 **240 KiB** 字节预算
裁剪：超出就从最旧的步骤开始丢，`dropped_steps` 记账。这样任何长度的会话都能被
一次读完，不需要给宿主加新接口。

### 5.2 归一化 Step

```python
@dataclass(frozen=True)
class Step:
    seq: int                      # 事件流里的全局单调序，provenance 的锚点
    trace_id: str
    kind: str                     # shell | llm | human | read | write | other
    tool_name: str                # 原始工具名，保留用于报告
    command_shape: str            # 归一化命令形状：去掉具体路径和值
    reads: tuple[str, ...]        # 归一化后的输入资源角色
    writes: tuple[str, ...]       # 归一化后的输出资源角色
    read_paths: tuple[str, ...]   # 具体输入路径，只用于依赖切片，不进签名
    write_paths: tuple[str, ...]  # 具体输出路径，只用于依赖切片，不进签名
    success: bool                 # 由工具结果条目判定（isError / 非零退出）
    rejected: bool                # 由用户拒绝工具调用的条目判定
    raw_ref: str                  # 指回原始 entry id，报告和排查用
```

`reads` / `writes` 是**角色**，用于签名对齐；`read_paths` / `write_paths` 是**具体路径**，
用于 Stage C 的反向切片。两者必须同时保留：只留角色无法建依赖图，只留路径则参数一变
签名就变。

`command_shape` 的归一化规则：把路径、数字、字符串字面量替换成占位符，
只保留命令名、子命令和旗标结构。占位符有五种：

- `<script:NAME>`：解释器调用的脚本，或命令本身就是脚本（`python3 scripts/clean.py`）；
- `<path>`：路径；
- `<num>`：数字；
- `<str>`：旗标的字符串取值（`git commit -m '...'`）；
- `<inline>`：内联代码（`python3 -c`、`node -e`、heredoc 体）。

例如
`python3 scripts/clean.py /data/raw.csv rules.json /out/clean.csv`
归一化为 `python3 <script:clean> <path> <path> <path>`。

`writes` 只来自三处：重定向（`>` / `>>`）、写旗标（`-o` / `--output` / `--outdir` 等）、
以及会改动文件系统的命令（`rm` / `mkdir` / `tee` / `cp` 的目标）。位置参数默认算**读**，
因为它们是脚本的入参；`cp` / `mv` 这类命令的最后一个位置参数算写。

`reads` / `writes` 的角色从命令参数、脚本内容和产物声明三处归纳，
只区分角色，不区分具体值：`source`（源数据）/ `rules`（规则、配置）/ `script`（脚本）/
`model`（模型产物）/ `report`（报告、指标）/ `output`（其余产物）。
角色由扩展名 + 文件名关键词 + 读写方向决定，是纯函数，所以同一套动作在不同轨迹里
得到同样的角色。

`inline_code` 不单独存字段，由 `command_shape` 里是否含 `<inline>` 判断。

### 5.3 步骤签名

```
signature = (kind, command_shape, reads, writes)
```

对齐、去重、判频全部基于签名。具体路径和数值变化不影响签名，因此参数替换
天然被识别为「同一个步骤，参数不同」，而不是「新步骤」。

`kind == human` 的步骤（用户发言）**不参与签名对齐**：它们没有命令形状，全部同签名会
被错误地合并成一个节点。用户发言只作为阶段边界和节点命名依据写进报告；骨架只由
工具步骤构成。

### 5.4 provenance

每个生成的 graph 节点和每个脚本必须声明：

```yaml
provenance: [12, 13, 17]
```

数字是 `Step.seq`。捕获与校验的规则：

- 骨架里的每个 step 至少要出现在一个节点的 provenance 里；
- 任何节点不得引用骨架之外的 seq；
- 违规分别报 `uncovered_steps` / `invented_steps`，capture 判定失败。

### 5.5 匹配与复用元数据（P0）

沉淀产物默认只有 `id / version / name / description`，复用方无法判断「适不适用、
能不能直接跑、缺哪一段」。可选字段补齐这块，缺省时旧包仍然有效：

| 字段 | 作用 |
| --- | --- |
| `applies_when` / `not_applies_when` | 适用与明确不适用的场景，匹配的第一道闸门 |
| `input_contract` / `output_contract` | 根输入/产出的格式与字段，例如 `{data_file: {format: "csv", required_fields: ["qid", "question_text"]}}` |
| `determinism` | `script` / `agent-assisted` / `human-gated`，缺省按图里的节点种类推导（`human` 优先于 `llm`，`llm` 优先于 `script`）；只有确实要覆盖推导结果时才显式声明 |

约束由 `agentgenome/manifest.py` 校验，导入与生成时都会跑：

- contract 只认 `format / fields / required_fields / optional_fields / description`，
  未知键直接拒绝，避免写进去没人消费的自由文本。

`genome_capture` / `genome_distill` 接受这些字段并写进 manifest；`genome_list` 只返回
紧凑视图（applies_when / not_applies_when / input_contract / determinism），
`genome_get` 返回完整 manifest 与沉淀时的输入采样。

旧包 manifest 里的 `references` / `gaps` 键不会再被读取，但也不会导致校验失败——
`validate()` 只校验它认识的键，老包因此仍可导入。

**为什么不做 system prompt 注入**：历史经验落在用户自己的 Storage 里，是用户维度资产，
插件拼 system prompt 时既拿不到、也不应该持有它。发现只能发生在运行时（`genome_list`），
prompt 只写通用流程：先 list，再按 applies_when / input_contract 对照真实输入；
输入不满足契约时先判断能否桥接出缺失的中间产物，不能桥接再回到 Skill。

### 5.6 参数占位符与输入边界（P1）

链路里写死的输入路径是「这一条轨迹的输入」，不是「这个资产要的参数」。agent 在
`genome_distill` 的 `steps` 里把这类输入写成 `{参数名}` 占位符，再用 `params` 声明
它的真实值（回放 A 用这份真实值跑，证明同参数可复现）：

```json
{"steps": [{"run": "python3 scripts/clean.py {input_csv} > out/clean.csv"}],
 "params": {"input_csv": "storage/datasets/raw.csv"}}
```

规则（`agentgenome/capture/distill.py` 的 `_bound_values` / `placeholder_key`）：

- 命令里出现 `{参数名}` 时 `params` 必填，且必须**覆盖链路的根输入**（少给根输入
  报错，错误信息列出缺的键和示例）；链路中间产物的 `{参数名}` 也可以一并给出，
  它们不是根输入、不进 graph 参数，只用于把同一个文件的两种写法归一；`params`
  的值原样替换回命令后再走原有固化流程；
- 同一条链路里一个文件可以用两种写法：写 `{参数名}`、读它绑定的字面路径（或反过来）。
  反向切片、产出配对与链路校验都通过 `trace.canonical_path` 归一后比较，不会把这种
  写法差异当成缺步骤或假根输入；
- 占位符是**显式声明的输入边界**，但它不能隐藏固定命令：如果本会话里更早的步骤
  用固定 shell 命令产出了这个路径（哪怕脚本内部调用 LLM），必须把该步骤补进
  `steps`，不能只在 `params` 里写占位符；只有编辑工具、人工或纯 agent 产生的
  产物才允许作为占位符边界。报告里仍会把合法的边界连同上游产出者一起列出来；
- 没有占位符时 `params` 仍可省略，行为与旧版本一致（向后兼容）。

### 5.7 只收完整固化的链路（v2.3）

v2.1/v2.2 曾用 `references`（图无法执行的 case playbook 与具体 case 资产指针）和
`gaps`（跑过但无法固化的步骤）承载「半固化」的链路：graph 只覆盖固定调用的部分，
其余步骤靠 agent 读 playbook 现场补做。这条路线和「历史经验 = 可重跑的固化链路」
的目标冲突——gap 里的步骤没有调用形式，回放时只能重新裁量，链路实际上没有沉淀下来。

v2.3 起两者一并移除，改成**要么整条固化、要么拒绝发布**：

1. 每步都必须是图能重跑的节点形态：固定命令 → `run`，固定宿主工具调用 → `tool`，
   人工门禁 → `human`，真正的 agent 裁量 → `llm`；
2. 固不下来的步骤不会被写进 manifest 蒙混过关。`capture/pipeline.py` 的
   `_require_solidified` 在发布前检查 `package.not_solidifiable`，非空就直接抛
   `EmitError`，把每个步骤的序号与原因列出来，并给出三条出路：换成可执行节点重新提交
   （`genome_distill` 的 `steps`）、用 `final_artifact` 重新切片把无关步骤排除、
   或把本次特有的产物声明成 `{参数名}` 输入边界；
3. 编辑工具写出的产物不再有「进 gaps」这条后路，只能改成脚本产出（`run`）或声明成
   输入边界；`_check_llm_promotions` 会把试图包装成 llm 节点的编辑写入直接拒绝。

`determinism` 因此不再由 gaps 推导，而是由图里的节点种类推导：有 `human` 节点是
`human-gated`，只有 `llm` 节点是 `agent-assisted`，全是固定调用才是 `script`。

**沉淀时的口径**：一条链路就是它跑过的那个具体场景（label / asset_id 写具体场景，
例如「恶意问题过滤后生成回答再转 SFT」）。泛化只能通过 `applies_when` /
`not_applies_when` / `input_contract` 声明匹配范围来做，不能靠把具体步骤泛化成
一段描述——描述无法执行，节点可以。

### 5.8 脚本参数物化与复用侧的执行边界（v2.2）

会话里的确定性脚本通常由编辑工具写进工作区（`write` / `edit`），而编辑工具写入的内容
按契约无法机械固化。把这种脚本写成 `{参数名}` 交给复用方自带，会让包不自洽：包里没有
这份脚本，复用必然要重新写一遍。v2.2 起 `_materialize_scripts` 在编译前处理这种情况：

- `params` 里指向脚本（`role(..., writing=False) == ROLE_SCRIPT`）的值，如果工作区读得到、
  且不是链路内其它步骤的产出，就把命令里的 `{参数名}` 改写成具体路径，脚本按原有
  「脚本搬运」机制复制进 `scripts/`，并从 `params` 中移除；
- 改写只在**原始占位符计划**上做链路完整性校验之后进行：占位符是显式输入边界，
  改写成字面路径后会变成「丢了上游步骤」，两者不能混在一起判断；
- 读不到的脚本保持输入边界，报告写明「仍按输入边界处理（包内没有这份脚本）」，
  不静默通过；
- 结果写进报告的「脚本参数已物化」与 `distill` 返回的 `materialized` 字段
  （`[{key, path}]`），对应的输入边界条目一并从 `chain.boundaries` 去掉。

复用侧的执行边界由 prompt 承担（`pi-extension/index.js`），因为「graph 覆盖的步骤」和
「哪些步骤由 graph 执行、哪些步骤交回 agent」只能靠语义区分：

- graph 覆盖的确定性步骤必须交给 `genome_run` 执行，agent 不得用文件工具重写 graph 里
  已固化的脚本、也不得用 terminal 手工跑等价命令替代；脚本确实不适用时走 `revision_id`
  通道（`change_summary` + `baseline_cases` / `regression_cases`）；
- 固化判据是「调用形式是否固定」，不是「有没有 LLM」：脚本内部调用模型、产出每次不同，
  只要命令固定就进 graph，由 `genome_run` 重跑；agent 只负责图里的 `llm` 节点（按节点
  提示词做完后用 `genome_step_result` 交回），`human` 节点则停下等人，由用户做完后
  `genome_resume` 唤醒；
- 沉淀带根输入的链路时必须给出 `new_params`（与 `params` 同键、换成另一组真实输入或
  按 `input_contract` 造的小样本），插件据此跑回放 B；缺省时经验只能发布成未泛化的
  窄口径版本，报告与 capture 状态里写明。

### 5.9 输入采样与迭代基线（v2.3）

泛化抽象会把「怎么处理这批数据」抽成通用步骤，但用户真正想反复用的往往是**这批数据
上的这条具体链路**。要让链路的识别、复用与迭代可靠，插件必须留下当时那批输入的痕迹，
而不是只靠文字描述。v2.3 起 capture 在冻结包的同时对**根输入**做有界采样，并在回放 A
成功后把这批输入连同产物摘要固化成一条**迭代基线**。

**采样（`agentgenome/capture/sample.py`）**

- 采样只针对 `bound` 里的根输入，走宿主读端口（`Source.head` / `ExecutionHost.head`），
  不把整份数据集拉进内存：先读 `limit + 1` 字节判断是否截断，再按格式裁剪到整条记录；
- 每种格式都有自己的读法与 schema：CSV/TSV 取表头 + 前 20 行（`columns`/`delimiter`）、
  JSONL 取前 20 条（`fields`）、JSON 取结构、文本取头部（`text`）、二进制只留
  `bytes` 与采样哈希；单文件上限 32 KiB、总量上限 512 KiB；
- 采样是**证据，不是门禁**：文件缺失、读取失败或预算用尽都记成 `format=unavailable`
  + `note`，绝不让 capture 失败；
- 采样落盘在 `captures/<id>/samples/`（`index.json` + 每个参数一个 `*.ext`），
  **不进包**——包字节是回放执行的证据，事后改写会让发布失效；
- `genome_get` 会返回 `input_samples`（有界预览，`_PREVIEW_TEXT`/`_PREVIEW_TOTAL`），
  `genome_list` 返回 `has_samples`；报告新增「### 输入采样」段。

**复用门（`dispatch._gate`）**

- 复用已发布资产时，用采样的 `format` / `schema` 对本次输入做机械核对：
  格式不符或缺少 `required_fields` 是硬失败，`genome_run` 直接拒绝并说明差在哪；
- `allow_mismatch=true` 表示「明知不符仍要按现状强行复用」，此时把差异记进
  run 结果（`compatibility.forced=true`）而不是中断；
- 声明字段与本次采样字段的差集记为 `warnings`，随 run 状态返回，供 agent 判断是否需要
  先做桥接。

**迭代基线（`agentgenome/capture/baseline.py`）**

- 回放 A 成功后，把采样文件转成回归 fixture（`inputs/<name>` + `fixture:` 参数），
  并把回放 A 记录到的产物**摘要**（`sha256`）作为 `hash` 断言，一条 capture 生成一条
  `baseline-<capture_id>` 用例，断言数量上限 12；
- 基线需要完整可回放：任何采样被截断、二进制、读取失败，或参数与 manifest 不一致、
  没有产物摘要时，基线标记为 `ready=false` 并写明原因（`baseline.json`）；
- 基线的存在由 `Captures._baseline` 写进 capture 状态（`ready/reason/path/cases`），
  报告追加「迭代基线」一行；
- 改脚本的修订会**自动继承**这条基线：`revisions.prepare` 在 `inherited`（已冻结
  suite）与 `pending`（上一次未发布修订）都为空时，用
  `captures.baseline_cases(asset_id, baseline_version)` 填充 `baseline_cases`，因此
  「改脚本 + 新用例」的修订不再需要手工重造旧场景用例；
- 回归执行支持 `kind=hash`：直接比对 `checkpoint` 里记录的产物 `sha256`，不需要把
  产物读回内存，天然适用大文件；
- 基线是**可再生成**的：它只在一次修订真正提升新包时才进入 `regression_suites`，
  所以一条过严的期望最多让一次修订失败，不会把资产锁死。

### 5.10 llm 节点保留原始执行方式（0.6.5）

llm 节点回放时由 agent 执行，agent 只拿到提示词，看不到「原会话是怎么做的」。此前沉淀
出的提示词只写任务目标（「过滤恶意问题」「生成回答」），回放的 agent 就自己挑一套做法
硬做：原会话里用 `data-processing` skill 的 `sandbox_sample.py --pipeline` 跑出的步骤，
回放时变成现场临时写的脚本——链路看似跑通，实际没沿原路径，产出口径也跟着漂。

0.6.5 把这条要求写进插件 prompt 的三处（系统提示词、`genome_distill` 工具描述、
`steps` schema 描述）：

- llm 节点的 `llm` 提示词必须写明这一步原本由**哪个 skill、以什么命令形态**执行
  （脚本名、`--pipeline`、模型 profile 等），只写任务目标不合格；
- 这是默认蒸馏行为，不需要用户在沉淀指令里额外交代；
- 约束只落在 llm 节点的提示词文本上：`run` 节点的命令原样保留执行方式，不受影响。

### 5.11 scripts-first 与 artifact 扩展名（0.6.6）

**artifact 扩展名（`builder/compiler.py`、`core/engine.py`、`capture/emit.py`）**

引擎此前把每个节点的产物统一命名成 `{node}.out`，而 DataFlow 的 `LazyFileStorage`
按扩展名推断读取类型，`.out` 直接报 `Unsupported file type`。现在：

- graph schema 支持叶子节点声明 `artifact: {ext: jsonl}`（短后缀，仅叶子、仅 `ext`
  一个键）；引擎按声明的后缀命名产物，缺省仍为 `.out`；
- `emit` 从会话里该步骤真实产出的路径推断后缀写入 graph.yaml，agent 不声明也不会
  丢类型；声明非法（非叶子、多余键、后缀不合规）在编译期直接报错。

**固化策略开关（`capture/policy.py`）**

`genome_distill` / `genome_capture` 新增 `policy` 参数，默认 `scripts-first`：

- llm 节点必须带 `from` 指向会话里真正来源的步骤，插件按 `source_traces` 核对：
  `from` 缺失、不在轨迹里、或来源是固定命令/人工门禁/编辑写入，全部直接报错并列出
  可选的会话步骤——把固定命令伪装成 llm 节点的后门被机械堵上；
- 人工门禁与编辑工具产物另有两道机械检查，不随策略放宽；
- `adaptive-first` 显式信任 agent 的判断，`from` 变成可选、只作审计；
- 蒸馏报告新增「固化策略」一行，payload 带回 `policy` 字段。

**运行目录随脚本一起冻结（`capture/emit.py`）**

搬运脚本时，脚本按文件名调用的同目录辅助文件（`.py`/`.sh`）会传递性地一起进包，
文件名保持不变（上限 32 个）。技能 CLI 不是单文件：`sandbox_sample.py` 会 shell out
到同目录的 `source_fingerprint.py`、`validate_prepared_data.py`、`generate_report.py`，
并把自身目录放进 `PYTHONPATH` 供 pipeline 导入 `df_logging`；只冻结 CLI 会让包在复用
时缺 helper 直接失败。引用了但工作区里不存在的名字会被跳过，不影响沉淀。

### 5.12 tool 节点与宿主工具端口（0.6.8）

平台提交（`df_submit_pipeline`）、Label Studio 导入建项目这类步骤是**固定的工具调用**：
没有 shell 命令可以固化，却也不需要模型在环。不把它们做成节点的话，复用时要 agent
现场重新调一遍，工具名与参数都可能漂。0.6.8 补上第四种节点形态：

```yaml
- id: submit-pretag
  input:
    dataset: {type: path, from: params.dataset}
  do:
    tool:
      name: df_submit_pipeline
      args:
        dataset: "{input.dataset}"   # 精确等于槽位的字符串保留 JSON 类型，嵌在文本里的按字符串替换
        task_name: pretag
  output:
    submission: {type: json}
  verify:
    - metric: "submission.task_id"
```

- `builder/compiler.py` 新增 `tool_call()`：`name` 必须匹配 `^[a-z][a-z0-9_.-]*$`，
  `args` 必须是映射（缺省 `{}`），`do` 只允许 `run`/`llm`/`tool` 三选一；`args` 里
  每个字符串都走与其他节点同一套槽位校验（`slots_value`，含 `E_SLOT`）；
- 内核新增 `ToolPort` 与 `Ports.tool`（`core/ports.py`）：`ToolResult(is_error, result, text)`
  是端口协议；`core/engine.py` 的 `_call_tool` 把填充后的参数交给宿主，把返回的 JSON
  写成节点 artifact（`artifacts/<node>.out`，可用 `artifact: {ext: json}` 改后缀），
  随后照常走 `output` / `verify`。`is_error=true` 按 `do` 阶段失败处理并保留 artifact，
  `danger: true` 沿用既有 gate；
- 需要宿主具备 `tool` 能力位：没有工具端口的宿主在 handshake 阶段报
  `E_NEEDS_PORT_ABSENT`（`core/api.py`）；`GenomeService.check_capabilities` 按
  `_requires_tool` 自动补 `tool` 依赖，宿主能力位缺失时在提交前就报错；
- **scripts-first 对称化**：会话里真实发生过的固定工具调用包装成 llm 节点会被拒
  （`_check_llm_promotions`），只能按 tool 步骤提交——固定调用一律进 graph；
- 蒸馏侧步骤形态是
  `{"tool": "df_submit_pipeline", "args": {...}, "output": "submission.json", "from": "s1:3"}`：
  `output` 必填（工具返回的 JSON 落到哪），`input` 可选但必须出现在 `args` 里，
  `args` 里出现的路径自动成为该步的 reads，`from` 指向 trace 里 `kind=tool_call`
  的来源步骤；emit 把它编译成上面那条节点，并在 `nodes` 账本里给出 `tool` 与 `args`。

### 5.13 人工交接的前半段（0.6.12）

人工交接（Label Studio 复核、人工导出）在会话里长得像一个断点：前半段是 agent 跑的固定
命令，用户离线做完人工工作，回来时只说一句「我已经复核完导出了 `<path>`」，后半段接着跑。
把这句话里的产物当成链路根输入，链路就从人交回产物的那一刻开始，前半段整段丢掉——而且
这次丢得「合法」：边界规则原本只排除固定命令产物，人工产物被并进「编辑工具 / 人工 /
纯 agent 产物」那一类，于是「复核导出 → SFT」从字面上看是干净的。

机械证据只能来自这句话本身，因为人工产物不是任何会话步骤写出来的，路径交集永远为空：

- `capture/trace.py` 的 user 步骤保留该轮文本（`Step.command`，此前是空串），它既是
  「人在这里做了事」的标记，也是点名产物的位置；
- `capture/distill.py` 的 `_handover_steps` 把两类被丢掉的步骤判成人工交接的前半段：
  后续用户轮点名了计划的根输入（`_names_artifact` 按路径尾部匹配，容忍
  `/workspace/datasets/x` 对 `storage/datasets/x` 这类宿主前缀差异），或该步骤落在计划
  第一步之前且其后仍有用户轮（用户的话插在中间，说明中间隔着一次交接）；第二个判据
  在 0.6.13 被删掉，理由见 §5.14；
- 命中后与 `_scope_split` 判出的「相连步骤」合并，一起走 `review_confirmed` 复核：
  人工门禁要作为 `human` 节点进图，前半段的固定命令作为 `run` 步骤进图，只有用户确认
  「这次只要后半段」时才允许带 `review_reason` 发布，确认过的边界写进沉淀报告。

### 5.14 本地试跑与平台提交共用脚本（0.6.13）

「本地试跑小样 → 提交平台执行」的会话里，用户要的是只沉淀提交那一步，但机械判据把试跑
也算进了链路，沉淀被范围复核挡住。三个原因，三处改法：

- **宿主工具调用的 args 曾是数据流黑洞**：`capture/trace.py` 的 `_tool_step` 对未知工具
  只写 `<tool:名字>`，reads/writes 全空，「提交平台」这一步在小样与提交之间不留任何痕迹。
  现在 args 里的路径进该步的 reads（`arg_paths`），固定宿主工具调用在数据流上可见；
- **脚本不是数据依赖**：试跑读的 `--pipeline <脚本>` 与计划里 tool 步骤的 `script_path`
  指向同一个脚本，路径交集把「反复试跑 4 次」判成与链路相连（`_scope_split`）。脚本是
  资产不是数据：连通性现在只看数据路径（`_data_paths` 过滤 `ROLE_SCRIPT`），试跑落进旁支；
- **人工交接的判据太宽**：`_handover_steps` 原先还有「该步骤落在计划第一步之前且其后仍有
  用户轮」这一条，普通指令（「本地先验证 3 条小样」「提交平台执行 10 条」）都会命中，于是
  任何丢了步骤的沉淀都被拦。现在只认用户点名交回产物的那一轮（`_names_artifact`），这也是
  真实会话里的说法（「我已经人工复核完打完标成功导出了 `<path>`」）。

同一版里脚本的处置也统一了：脚本由被丢掉的固定命令产出（`cp` 模板到工作区这类）不再算
「链路不完整」，tool 步骤 args 里的 `{脚本参数}` 会和 run 步骤的脚本参数一样物化——脚本
随包搬运，命令/args 改写成具体路径，参数从 params 去掉（`_check_chain` 的 asset 分支、
`_materialize_scripts` 的 `KIND_TOOL` 分支）。于是一条「提交平台 → 人工复核 → 格式转换」
的链路只留这三步，试跑与脚本暂存都进旁支段。

## 6. 流水线

### Stage 0：链路蒸馏（`genome_distill`）

**输入**：agent 规范化后的链路 `steps: [{run, from?} | {llm, output, input?, verify?, from?}]`，
按执行顺序。
**产出**：编译通过的 package + 排队中的回放 A，与 `capture` 同一套后续流程。
**实现**：`agentgenome/capture/distill.py`

Stage A 只能固化「输入产出都在命令行上」的命令，真实会话里往往不是：
`python3 -c`、heredoc、`2>/dev/null` 的探测、以及脚本内部写出的产物，都会让机械
固化失败。语义判断交回 agent，插件负责把它钉在契约上：

- 每步 `run` 是完整 shell 命令，**输入与产出路径必须出现在命令行上**；
- 内联代码（`-c`、heredoc）直接拒绝，要求先把代码落盘成脚本再调用；
- 只读探测命令（`ls`/`head`/`find`/`git status`…）直接拒绝，只留业务链路；
- 没有任何可识别路径的步骤直接拒绝；
- 真正的 agent 自由裁量步骤可以声明成 **llm 节点**（见下）；
- `final_artifact` 必须真的被某一步声明为产出（写 `{参数名}`、`final_artifact` 用它
  绑定的字面路径，或反过来，都算同一个文件），否则报出计划里的全部产出；
- 脚本文件必须已经存在于工作区（`host.read`），不存在时报出具体路径。

**链路完整性校验（P0）**：给了 `source_traces` 时，插件把导出的会话归一化后与计划
逐步对照（`distill._check_chain`）。计划里某一步读取的路径如果在本会话里由更早的
**成功**步骤产出，而该步骤不在计划里，直接报「链路不完整」并指出缺的是哪一步、
产出者是谁——这是对「agent 悄悄少交步骤」的机械防线。`{参数名}` 占位符不能再把
固定 shell 命令产出的路径降级成输入边界：这种步骤必须补进 `steps`；只有编辑工具、
人工或纯 agent 产生的产物才允许作为边界，并连同上游产出者一起写进报告的 boundaries
段。**脚本例外**（0.6.13）：脚本是资产，包会搬运它，所以由被丢掉的步骤产出的脚本不算
链路不完整——tool 步骤 args 里的脚本参数也会物化。没给 `source_traces` 时不做这项校验
（保持旧行为，仅依赖命令自身的契约检查）。

**范围复核**：计划比会话窄时不能静默固化，但缩小本身是允许的，界线由
`distill._scope_split` 机械判定——一个没进计划的步骤如果**和链路相连**（它读的路径是
计划产出的，或它写的路径被计划读取，含经其他旁支传递），就是链路被截断的形状，报
「链路范围需要用户复核」并逐条列出；用户确认这次只要后半段后，才带
`review_confirmed=true` 与 `review_reason` 重新提交，确认过的边界随报告持久化
（缺 `review_reason` 仍然拒绝）。**和链路不相连的旁支**（本地试跑小样、只写调试文件的
探测、被后续步骤取代的尝试）不挡沉淀：`_review_scope` 直接放行，步骤连命令一起写进
报告的「旁支步骤（未进链路，链路不依赖它）」段与 payload 的 `branches`，沉淀结果里
因此永远看得到「会话里有这些步骤，这次没进链路」。

校验通过后，distill 生成一条**确定性**轨迹（`trace_id = distilled-<sha256[:12]>`，
由 label + 命令序列派生），因此重试同一份计划会命中同一个 package 与版本，走幂等复用；
然后直接复用 `prepare_capture`：编译、排队回放 A、写报告，返回 `capture_id` 与 `run_id`。

返回的 `distilled` 字段是给人看的账本：`trace_id`、`report`、`package`、`steps`
（每步的 reads/writes）、`nodes`（入图节点与命令）、`auxiliary`（未进图的辅助步骤及原因）、
`not_solidifiable`、`final_artifact`。步骤编号从 1 开始，与 agent 提交的 `steps` 对齐。

**边界**：会话里的人工复核（`kind == human` 的步骤）没有声明过产出，因此机械固化时
只能当链路的边界：它的产物由人消费，不在最终产物的依赖闭包里，会进 `auxiliary` 并在
报告里写明。要把这段固化成可复用的门禁，用 `human` 步骤提交（见下），图会在那里停下等人。

#### llm 节点（`do: {llm}`）

有些步骤确实属于链路，但没有固定调用形式——例如「过滤恶意问题」「生成回答」这类
由 agent 自由裁量的 LLM 步骤。它们声明成 llm 步骤，回放时交回 agent 执行：

```yaml
- llm: "读 {data_file} 里的问题，逐条生成回答，把 {text, answer} 写到 out/answers.jsonl（JSONL）。"
  input: ["{data_file}"]        # 消费的输入路径（`{参数名}` 或更早步骤产出的字面路径），可省略
  output: "out/answers.jsonl"   # 产出的 artifact 路径，必填
  verify: "test -s {artifact}"  # 可选，覆盖默认的 `test -s {output.<key>}`
  from: "s1:2"                  # 可选，来源轨迹步骤
```

约束与实现：

- 提示词里必须**原样出现** `input`/`output` 声明的每一个路径；emit 与 `run` 步骤走同一套
  槽位替换，把它们换成 `{input.*}` 与 `{artifact}`，节点编译成 `do: {llm}`；
- 提示词还必须写明这一步**原本的执行方式**（原会话用哪个 skill、什么命令形态），
  见 §5.10；
- 节点由 `agentgenome/capture/emit.py` 的 `_classify` 分类为叶子，`do: {llm}` 由
  `core/engine.py` 的 `_ai_work` 交给宿主的 `agent_task` 端口执行：宿主提示 agent
  产出声明的 artifact，agent 写完文件后回报 `genome_step_result`；
- **硬边界**：一条真实跑过的固定 shell 命令（哪怕脚本内部调用 LLM、每次产出的文本
  都不同）不能写成 llm 节点，必须用 `run`；人工门禁用 `human` 步骤，编辑工具写入的
  产物必须改成脚本产出或声明成输入边界。给了 `source_traces` 时插件按 `from` 对照
  会话步骤，误判会直接报错（`_check_llm_promotions`）；
- 需要宿主具备 `agent_task` 能力；`manifest.requires` 由 `GenomeService._requires_agent`
  在回放前自动补上。

#### human 节点（`do: {human}`）

Label Studio 复核、人工导出这类步骤只能由人做，agent 不能替他做。它们声明成
human 步骤：图跑到这里停下等人，人工完成后唤醒同一条 run。

```yaml
- human: "请到 Label Studio 复核 {input.prelabeled}，把导出的复核结果写到 {artifact}"
  input: ["{input.prelabeled}"]   # 可选，工作单里必须原样出现
  output: "out/review.jsonl"      # 人工结果必须落到的 artifact，必填
  verify: "test -s {artifact}"    # 可选，默认 `test -s {artifact}`
  from: "s1:2"                    # 可选，来源轨迹步骤（kind=human）
```

约束与实现：

- 工作单（`human`）里必须**原样出现** `input`/`output` 的每个路径，emit 换成
  `{input.*}` / `{artifact}` 槽位；节点编译成 `do: {human}`（`emit._classify` 按
  `KIND_HUMAN` 分类为叶子）；
- `core/engine.py` 的 `_human_work` 把工作单交给 `human` 端口；端口返回
  `Answer(pending=True)` 时抛 `NodeWaiting`，运行停在这个节点（状态 `waiting`，
  结果里带 `node` / `question` / `artifact`），checkpoint 保留，不占执行线程；
- 唤醒用 `genome_resume(run_id)`：run 回到队列，`core/api.py` 的 `resume()` 重新进入
  那个节点，只对**这一个**已答过的门禁批准（`prepare(resuming=<节点路径>)`），下一个
  门禁照常停下；
- 需要宿主提供交互式 `human` 端口（`capabilities()["interactive"] is True`），否则
  handshake 直接报 `E_NEEDS_NONINTERACTIVE`；SDK 桥接里就是当前会话：停下时
  conversation 拿到 waiting 通知，用户做完人工步骤后由 agent 调 `genome_resume`；
- 人工步骤的 `output` 由人写入，回放不能替它生成，`agent` 也不得伪造；`verify`
  只检查人工产物是否真的存在且非空。

#### tool 节点（`do: {tool}`）

平台提交、Label Studio 导入建项目这类步骤是**固定调用**：会话里 agent 调了哪个宿主
工具、参数是什么，回放时就该一模一样地再调一次：

```yaml
- tool: "df_submit_pipeline"      # 宿主工具名，必填
  args:                            # 工具收到的参数，字符串里的路径会变成 graph 输入槽位
    dataset: "{input.dataset}"
    task_name: pretag
  output: "out/submission.json"    # 工具返回的 JSON 落到哪，必填
  input: ["{input.dataset}"]       # 可选，但必须出现在 args 里
  from: "s1:3"                     # 可选，来源轨迹步骤（kind=tool_call）
```

约束与实现：

- `output` 必填：工具没有 shell 命令那样的 stdout 约定，返回体是 JSON，落到
  artifact 后由 `output`/`verify` 消费；
- `args` 里的每个字符串按同一套槽位规则替换（`capture/emit.py` 的
  `_replace_json_tokens`）：**精确等于槽位**的字符串保留 JSON 类型（路径参数仍是
  宿主路径），嵌在文本里的按字符串替换；
- 出现在 `args` 里的路径自动成为该步的 reads，`_check_chain` 因此能识别它的上游
  依赖；`input` 只是显式声明，不能声明 `args` 里没有的路径；
- 节点编译成 `do: {tool: {name, args}}`，由 `core/engine.py` 的 `_call_tool` 交给
  宿主的 `tool` 端口执行，**没有模型在环**；
- 需要宿主具备 `tool` 能力（见 §5.12）；没有该能力的宿主在 handshake 阶段就报错。

### Stage A：采集与归一化

**输入**：trace id 列表。
**产出**：归一化后的 `Step` 列表（内存中传给 Stage B），以及报告里的原始段。
**实现**：`agentgenome/capture/trace.py`

- 读取 extension 导出的轨迹文件；
- 按 `SessionMessageEntry` 还原助手工具调用与对应结果的配对顺序；
- 标记 success / rejected；
- 归一化成 `Step` 列表；
- 丢弃明显的噪声：纯读取无副作用的探查、失败且未被后续修正的尝试、被用户拒绝的分支。

**验证**：用录制的 fixture 轨迹跑单测，断言 step 数量、seq 连续性、rejected 标记正确。

**噪声判定的实际口径**（Stage A 只丢这两种，其余留给 Stage B 判）：

- `probe`：纯读取无副作用的探查。包括 `read` / `grep` / `ls` / `glob` 工具调用，
  以及 `cat` / `head` / `grep` / `wc` / `sed` 等只读命令，`git status|diff|log` 这类
  只读子命令，和没有产物、没有入参的 `-c` 内联片段。
- `plan`：`todo` / `update_plan` 这类只改计划不改数据的工具。

**失败和被拒的步骤保留在 `Step` 列表里并打标**（`success=False` / `rejected=True`），
不在这里丢：Stage B 的「某签名全部失败或被拒 → 垃圾」需要看到它们才能判。
Pi 没有结构化的「用户拒绝」标记，拒绝是从工具结果的错误文本识别的
（`user denied` / `was rejected` 等），漏判只会少过滤一条垃圾，不会误判成功。

`seq` 在归一化后重新编号，保证从 0 开始连续——它是 provenance 的锚点，必须无洞。
需要机读落盘时调 `steps_payload()` / `write_steps()`，流水线默认只渲染报告。

### Stage B：多轨迹对齐

**输入**：多条 trace 的 step 列表。
**产出**：骨架、分支候选、垃圾、参数差异，渲染进 `captures/<id>/report.md`。
**实现**：`agentgenome/capture/align.py`（已完成）

- 用带 gap 的编辑距离（Needleman-Wunsch）按签名对齐；
- 统计每个签名的支持度 `support / total_traces`；
- 分类：
  - `support == total` → **骨架**（进入 graph）
  - `support >= threshold`（默认 `0.6 * total`，2 条轨迹时要求 2/2）→ 骨架
  - `0 < support < threshold` → **分支候选**（v2 只记录，不生成 try）
  - `support == 0` 或全部失败/被拒 → **垃圾**（丢弃并记录）
- 同一签名在不同轨迹里参数值不同 → 记录为**参数候选**。

阈值可配置，报告里必须显式写出用的是什么阈值。

**验证**：构造 3 条合成轨迹（2 条走 A→B→C，1 条走 A→X→B→C），断言
骨架是 A→B→C，X 进分支候选，参数差异被识别。

`alignment_payload()` / `write_alignment()` 提供结构化落盘，流水线默认只渲染报告；
需要机读结果时可直接调用。

### Stage C：反向切片 + 图/脚本生成

**输入**：骨架 + 参数候选。
**产出**：`graph.yaml` 草稿 + `scripts/` + `manifest.json` 草稿。
**实现**：`agentgenome/capture/slice.py`、`agentgenome/capture/emit.py`（已完成）

- **反向切片**：从用户声明的最终产物出发，沿「写→读」依赖图回溯，得到最小必需节点集合。
  不在切片里的骨架步骤标记为 `auxiliary`（辅助步骤，可保留为 `sequence` 尾部或丢弃，报告说明）。
- **脚本搬运**：按 `writes` 找到落盘的脚本文件，原样复制进 `scripts/`，只做槽位替换。
  替换规则是机械的：出现过的具体输入路径 → `{input.<key>}`，输出路径 → `{output.<key>}`，
  临时产物 → `{artifact}`。
- **产出物声明**：每个节点的 `output` 从脚本实际写出的文件归纳，类型从扩展名和内容推断。
- **生成 graph**：骨架按 `sequence` 串联；节点带 `provenance`；参数进 `params.*`。

生成完成后立刻走 `builder/compiler.py` 编译。编译失败则 capture 失败并返回编译错误。
`emit.py` 在写盘前先跑 `_verify_provenance`：节点 provenance 的并集必须恰好等于骨架
步骤集合，多报 `invented_steps`、漏报 `uncovered_steps` 都直接抛错。无法固化的步骤
（内联代码等）进 `manifest.not_solidifiable`，同时出现在报告里。

**验证**：用 §1 的五步场景的录制轨迹跑，断言生成的 graph 编译通过，
每个节点的 provenance 非空且并集等于骨架集合。

### Stage D：机械校验 + 回放

**输入**：草稿 graph + 脚本。
**产出**：回放 A / 回放 B 的运行记录和结论。
**实现**：`agentgenome/capture/replay.py` + `agentgenome/captures.py`（已完成）。
两次回放都不在工具调用里执行，capture 调用返回时只完成了「冻结包 + 排队」：

- **回放 A（同参数）**：`queue_replay()` 走 `import_asset` + `submit(allow_draft=True)`，
  进注册表，由宿主 worker 正常执行——发布需要的成功 run 证据就是它。
- **回放 B（换参数）**：`run_probe()` 在 **回放 A 收尾时**执行（`service._finish_run` →
  `captures.finish` → `probe_replay`），直接 `Package.load()` + `core.api.run`，
  不写注册表、不产生候选版本，证据是 `captures/<id>/replay/new/ledger.jsonl`。
- 参数来源：v2 只接受显式 `new_params`。没给或与 `package.params` 相同 → 不做回放 B，
  报告写明「未经泛化验证」。

回放 B 是「泛化证明」。**没有回放 B 的经验只能发布为窄口径版本**：capture 状态与报告里
`generalized=false`，`genome_capture_status` 会把它连同原因一起返回。
（资产元数据里暂未写 `generalized`，`genome_get` 目前看不到这个标记，见 §12。）

`generalized` 因此是三态：`None` 待定（已排定回放 B）/ `True` 通过 / `False` 未提供或未通过。
回放 A 的 run 直到 capture 结论写进同一行状态后才转终态，避免 observer 看到一个
「已完成但没有沉淀结论」的 run。

**验证**：`tests/capture/test_replay.py` 覆盖 probe 直跑与失败降级；
SDK 侧 `test_capture_freezes_a_graph_and_publishes_after_its_replay` 覆盖
「capture 立即返回 → 回放 A 排队 → 收尾时跑回放 B → 发布」的完整时序。

### Stage E：发布

**输入**：通过回放 A（可选 B）的草稿。
**产出**：已发布资产。
**实现**：`agentgenome/captures.py` + `GenomeService.publish`（已完成）。

- 资产 `id` 由 label 派生 ASCII slug（非 ASCII 降级为 `capture-<hash>`），也可显式给
  `asset_id`；`version` 默认 `0.1.0`，同名版本被占用时按 patch 位自动递增（`next_version`）；
- 包字节与已有版本一致（pin 相同）→ 幂等复用，capture 直接以 `stage=reused` 结束，
  不排队任何回放；
- 回放 A 成功 → `publish(evidence=run_id)`，允许「正在收尾的这次回放 A」作为发布证据；
  失败 → 保持 `stage=draft`，报告写明失败节点和证据路径；
- 状态与报告落在 `captures/<id>/state.json`、`captures/<id>/report.md`，
  反向索引在 `captures/by-run/<run_id>.json` 与 `captures/assets/<asset_id>/<version>.json`；
  包内 manifest 不改写——回放执行的就是发布的字节，事后改写会让证据失效。

**验证**：同一批 trace 连续 capture 两次，第二次幂等，不产生新版本。

## 7. 工具与接口

新增 Pi 工具（`pi-extension/index.js` 注册，`agentgenome/dispatch.py` 分发）。参数名以
extension 暴露给 agent 的 schema 为准：

```
genome_trace
  无参数。列出当前会话与最近的历史会话：
  {trace_id, session_id, session_name, current, step_count, preview, started_at, path}
  会话目录来自 ctx.sessionManager.getSessionDir()，跨会话布局由宿主适配。
  只读；用途是让 agent 和用户挑选要沉淀哪几条。

genome_capture
  trace_ids: [str]            必填。genome_trace 给出的会话 id，按链路执行顺序排列
  label: str                  必填。经验名（可中文），写进报告并派生成 asset_id
  asset_id: str | None        可选。复用句柄，ASCII slug；不给则由 label 派生
  final_artifact: str | None  可选。最终产物路径；给了就反向切片，不给则用全量骨架
  new_params: object | None   可选。换参数回放的输入，与 package.params 同键
  min_support: float | None   可选。骨架阈值，默认 0.6
  applies_when / not_applies_when: [str] | None
                              可选。适用与明确不适用的场景，写入 manifest
  input_contract / output_contract: object | None
                              可选。根输入/产出的格式与字段，键为参数名
  determinism: str | None     可选。script / agent-assisted / human-gated，缺省按图里的
                              节点种类推导（human > llm > script）
  version / description / owner  可选。版本固定、资产描述、owner 标注
  返回: {capture_id, asset_id, version, pin, scope, run_id, reused, generalized,
         notes, report, package}
  立即返回：此刻只完成「冻结包 + 排队回放 A」，回放结论稍后追加到 report。

genome_capture_status
  capture_id: str
  返回: state.json + 当前 run 状态。stage ∈ {replaying, probing, published, draft, reused}，
  含 report 路径、replay.same_params / replay.new_params、generalized、失败原因与草稿位置

genome_distill
  steps: [object]                  必填。规范化链路，按执行顺序。每步是 run / llm / tool /
                                   human 之一；只有能编译成 graph 节点的步骤才会被接受，
                                   固不下来的步骤直接报错
  label: str                       必填。经验名，同 genome_capture
  final_artifact: str | None       可选。同 genome_capture；给了就反向切片
  source_traces: [str] | None      可选。genome_trace 给出的会话 id；给了就跑 P0 链路
                                   完整性校验，并导出这些会话作为证据
  params: object | None            可选。根输入真实值：键与命令里的 `{参数名}` 一致，
                                   命令里出现占位符时必填；值就是回放 A 使用的输入
  asset_id / version / description / owner / min_support / new_params
                                   可选。含义与 genome_capture 一致
  applies_when / not_applies_when / input_contract / output_contract /
  determinism                      可选。含义与 genome_capture 一致
  返回: {capture_id, asset_id, version, pin, scope, run_id, reused, generalized,
         notes, report, package, distilled: {trace_id, report, package, steps,
         nodes, auxiliary, not_solidifiable, final_artifact}}
  契约违规（内联代码、探测命令、没有路径、final_artifact 无人产出、脚本不在磁盘上）
  直接报错并说明要改哪一步；通过后与 genome_capture 完全同路：编译 → 排队回放 A。
  链路里存在无法固化的步骤时，`_require_solidified` 会列出每一步的序号与原因并拒绝发布。

genome_capture 与 genome_distill 的区别只有输入：前者吃会话轨迹（命令必须本来就干净），
后者吃 agent 规范化后的链路。两者返回结构、报告、异步完成通知、幂等判定都相同。
```

extension 与 Python 的分工：`genome_capture` 的 `trace_ids` 先在 extension 侧经
`exportTraces(ctx, ids)` 导出成工作区文件（`public_data/agentgenome-traces/<id>.json`，
240 KiB 预算内裁剪，超出从最旧步骤丢并记 `dropped_steps`），再以 `trace_refs`
（`[{trace_id, path}]`）交给 Python；Python 只按路径读文件，不接触 Pi 会话对象。
`genome_trace` / `genome_capture` 在 extension 的 `dispatch` 里本地处理，其余 action
仍走 `host.invoke`。

报告（`report.md`）必须包含：

- 用了哪几条 trace、共多少 step；
- 骨架步骤表（含 provenance seq）；
- 分支候选（说明「try 未开，未纳入」）；
- 被判定为垃圾的步骤及理由；
- 参数候选；
- 编译结果；
- 回放 A / B 结论；
- 未固化步骤（内联代码等）。

新增 action 走已有的 `workflow.invoke` 通道（`pi_adapter` → `conversation_runtime.workflow_call`
→ `AgentGenomeBackend.invoke` → `dispatch.invoke`）。除 `run` / `step_result` 外都是通用转发，
新增 `capture` 只需要在 `dispatch.py` 加分支。

轨迹读取不进 SDK：extension 用 `ctx.sessionManager` 读会话，导出成文件后把路径传给
`genome_capture`。SDK 侧的宿主适配（已完成）：

- `ExecutionHost.read()`：按 `execution.resolve` 解析路径后调 `execution.read`（256 KiB），
  供 `dispatch._traces` 读回落盘轨迹；轨迹不经过 RPC 帧，不受 1 MiB 单帧限制；
- `pi_adapter._runner_request`：origin turn 校验覆盖 `capture`，与 `run` 同一道闸门；
- `ReusableWorkflows.started_run()`：`capture` 返回的 `run_id` 与 `run` 一样启动 `_execute`，
  回放 A 才真正跑起来；
- `ConversationRuntime`：capture 注册 external task，回放结束后自动回到会话，并在通知里
  指引 agent 读取 `report`、说明泛化结论与草稿位置。

## 8. 用户维度隔离

现状：

- SDK 侧 AgentGenome root = `conversations_dir.parent / ".pyromind-agent" / "agentgenome"`
  （平台即用户 Storage 挂载内的 `/workspace/.pyromind-agent/agentgenome`），
  或 `AGENTGENOME_HOME`；原生 Pi 用 `~/.agentgenome`。
- 冻结包同时位于沙箱可见的 Storage 树上：`/target-workspace/.pyromind-agent/agentgenome`，
  所以会话里的 agent 除了 `genome_*` 工具，也能直接读包里的脚本与 `graph.yaml`。
- 平台维度靠 warm pool：`/api/init` 只允许一次（`InitService.initialize` 在非 dormant 时返回 400），
  因此一个 pod 只服务一个用户，目录天然隔离。

**风险点（本次一并处理）**：`assets` 表目前没有 owner 列。

处理方式：

1. capture 生成的 `manifest.json` 里写入 `owner` 字段（已实现：`emit._manifest` 落 `owner`，
   报告里同样标注；当前只记录不过滤，前向兼容）；
2. 在 `docs/` 里写明前提：**一个 AgentGenome root 只允许服务一个用户**；
3. 若未来出现共享 root 的场景，必须加 `assets.owner` 列 + 查询过滤 + 迁移，
   这是一次 schema 变更，不在 v2.0 范围内。
4. `genome_trace` 枚举候选会话时以当前会话的 `getSessionDir()` 为界，不允许路径逃逸
   到会话目录之外；轨迹落盘路径同样受 `execution.resolve` 约束。

因此 capture 不做二次确认：生成物落在用户自己的 Storage 空间（AgentGenome root 本身就在
该用户账号下），发布闸门是机械的（回放 A 通过才发布），不通过就留在
`captures/<id>/` 草稿里，不产生跨用户可见的东西。

## 9. 可靠性：怎么保证提取出来的不是垃圾

针对「对话里可能有大量试错、垃圾步骤」这一点，五道防线：

1. **结构防线**：签名对齐只看结构（命令形状 + 资源角色），自然语言描述里的噪声不参与匹配。
2. **频次防线**：只出现一次的步骤默认不进骨架，进分支候选。单轨迹模式下直接标注
   「无交叉验证」，产物只能作为窄口径草稿。
3. **依赖防线**：反向切片从最终产物回溯，与最终产物无依赖关系的步骤被剔除。
4. **溯源防线**：provenance 并集必须精确等于骨架集合，编造节点机械报错。
5. **回放防线**：换参数回放失败 → 降级为 `generalized: false`，不允许冒充通用经验。

**单轨迹模式的诚实降级**（用户只走过一次是常见情况）：

- 无法区分必需与偶然，所以不做「骨架」宣称；
- 无回放 B 时只能发布为 `generalized: false`；
- 报告中显式标注「本条经验来自单次执行，未经泛化验证」。

## 10. 失败模式与降级

| 情况 | 处理 |
| --- | --- |
| `ctx.sessionManager` 不可用（非标准 Pi 运行时） | capture 启动前拒绝，返回明确错误，不产生半成品 |
| 轨迹超过 bridge 单帧上限（1 MiB） | 一律落盘传递；导出侧按 240 KiB 预算丢最旧步骤并记 `dropped_steps`；读不到文件时拒绝，不静默降级 |
| 轨迹里没有落盘脚本（全是内联代码） | capture 结果标记 `not_solidifiable`，报告中列出内联代码块，提示 agent 先落盘再 capture |
| 命令里有内联代码 / 探测命令 / 没有路径 | `genome_distill` 在建图前拒绝，报出第几步、原命令和该改成什么，不产生半成品 |
| 产出只在脚本内部写出（命令行上没有） | 反向切片报出骨架的全部产出 + 契约提示（把产出写到命令行上）；distill 同口径 |
| 命令里出现 `/dev/null` 等设备文件 | 不算读写路径，避免把 `2>/dev/null` 变成产出 `null_2` |
| 同一父节点下命令形状重复 | 节点 id 在规划阶段统一去重（`terminal`、`terminal-2`…），不再依赖「有产出的节点」才占位 |
| graph 编译失败 | capture 失败，返回编译错误和草稿位置，允许 agent 修完草稿后重试 |
| 构建阶段失败（切片/生成/编译） | 注册为 `stage: failed` 的 capture，`genome_capture_status` 能解释失败原因并给出草稿与报告路径，不再只报「capture not found」 |
| 回放 A 失败 | 保持草稿 + 报告，不发布；报告给出失败节点和 ledger 位置 |
| 回放 B 失败 | 发布为 `generalized: false`，报告说明原因 |
| provenance 校验失败 | capture 失败，不允许绕过 |
| 资产名冲突且内容不同 | 自动 patch 递增发布新版本（`next_version`），不覆盖已有版本 |
| capture 执行中服务重启 | `recover()` 把在跑的 run 标 `interrupted`，同时把对应 capture 收尾为 `draft`（报告写明中断原因），不自动重放 |

## 11. 实施顺序与工作量

按可独立验证的顺序推进，每步做完就能看到产出：

| 阶段 | 内容 | 预估 | 状态 |
| --- | --- | --- | --- |
| 0 | extension 接通 `ctx`、会话枚举与轨迹落盘 | 1 天 | 完成（`session-trace.js`） |
| A | `capture/trace.py` 归一化 + fixture 测试 | 1.5 天 | 完成 |
| B | `capture/align.py` 对齐 + 分类 + 测试 | 2 天 | 完成 |
| C | `capture/slice.py` + `emit.py` 生成 + 编译 | 2.5 天 | 完成 |
| D | 回放验证编排 + 降级逻辑 | 1.5 天 | 完成（A 排队，B 在 A 收尾时执行） |
| E | 发布 + 幂等 + 报告 | 1 天 | 完成 |
| 工具层 | `genome_capture` / `capture_status` 注册 + `dispatch.py` 分支 | 1 天 | 完成 |
| SDK | capture 异步完成通知 + 轨迹读取桥 | 0.5 天 | 完成 |
| 集成 | 端到端测试（录制轨迹 → capture → 发布 → 复用运行） | 1 天 | 完成 |
| v2.1-P0 | `distill` 链路完整性校验（`_check_chain`）+ 边界报告 | 0.5 天 | 完成 |
| v2.1-P1 | `{参数名}` 占位符 + `params` 绑定（回放 A 用真实值） | 0.5 天 | 完成 |
| v2.3 | 移除 `references` / `gaps`，改为「只收完整固化的链路」（`_require_solidified`），`determinism` 改由节点种类推导 | 1 天 | 完成 |

合计约 12 人日。Stage C 是风险最高的一步（生成质量直接决定可用性），
已用合成轨迹跑通；真实链路的生成质量仍建议先用一条真实会话做人工评估。

### 11.1 验证结果（本轮）

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| AgentGenome Python | `python3 -m pytest -q`（deselect 已知挂起的 `test_owner_race_recovery_and_queued_cancel`） | 213 passed / 1 deselected |
| pi-extension JS | `npm test` | 33 passed / 2 skipped |
| SDK Pyromind Runtime | `uv run pytest tests/pyromind_runtime -q` | 308 passed |
| SDK Pi runtime | `cd harness-adapter/pi-runtime && npm test` | 129 passed |
| SDK 端到端（os-sandbox，无模型调用） | `uv run python scripts/validate_agentgenome.py --template templates/data-cleaning --home <tmp>/home --workspace <tmp>/workspace` | 两批 CSV 均 `succeeded`（2 行 / 1 行），`data-cleaning 1.0.0` 发布 |

`test_owner_race_recovery_and_queued_cancel` 在改动前的干净 HEAD 上同样挂起
（竞争进程不退出），与 v2.1 无关，单独记录。

### 11.2 产品级人工验证（唯一主路径）

验证不是在仓库里跑脚本，而是在真实会话里走一遍：启动服务 → 正常跑完一条链路 →
在对话里说「沉淀一下」。触发点只有一个，就是用户明说。

```sh
# 1. 启动（SDK 侧入口；AgentGenome 的 ./start_sdk.sh start 等价，会补齐 AGENTGENOME_HOME）
export OPENAI_API_KEY=...        # 模型网关 key
export DF_API_KEY=...            # DataFlow 等业务工具用
./start_inference.sh --agentgenome
# 服务监听 127.0.0.1:8000，PYROMIND_AGENTGENOME_ENABLED=1 时才会注册 genome_* 工具
```

`PYROMIND_PI_TERMINAL_BACKEND` 用默认的 `sandbox`（平台远端执行面）或 `os-sandbox` 都行。
轨迹由 Pi 在会话目录旁导出，SDK 的 `ExecutionHost.read` 会在读取前把它镜像进执行面，
所以远端沙箱下 writer 和 reader 指向同一份文件；capture 生成的报告通过
`capture.report_text` 随结果返回，因为 AgentGenome root 在 agent 文件工具的可见范围之外。

然后在一个会话里正常干活，不要为了沉淀改写法：

1. 让 agent 写落盘脚本并执行，例如 `public_data/scripts/prelabel.py`、
   `public_data/scripts/clean.py`，输入输出都用 `public_data/` 或 `storage/` 路径；
2. 中间该人工复核就人工复核，该提交平台就提交平台，agent 的 tool call 都会进会话日志；
3. 链路跑完，在同一个会话里说「沉淀一下」。

agent 应当按顺序做这几件事，这就是验证点：

| 顺序 | 期望行为 | 看到什么 |
| --- | --- | --- |
| 1 | 调用 `genome_trace` 列会话，而不是凭空猜 | 返回当前会话（`current: true`）的 step 数与预览 |
| 2 | 调用 `genome_capture`，带 `trace_ids` + `label` | 立即返回 `capture_id`、`asset_id`、`version`、`report` 路径 |
| 3 | 结束本轮，不轮询 | 会话里出现"已提交"类回复，不再刷进度 |
| 4 | 回放 A（同参数）在后台跑完，SDK 发 external task 通知回来 | 会话自动收到新一轮消息 |
| 5 | 依据 `capture.report_text` 汇报 | 说明是否发布、泛化结论、哪些步骤没固化、草稿位置 |

想拿到完整的泛化证据，说「沉淀一下，再用 `storage/<另一份数据>` 验一遍」，
agent 会把第二份输入放进 `new_params`，回放 B 在回放 A 收尾时执行。
不给第二份输入时，报告会写明「未提供换参数回放数据，本条经验未经泛化验证」，
回放 A 通过即发布，`generalized=false`。

产物落在 AgentGenome root（SDK 默认 `$WORKSPACE_DIR/agentgenome`）：

- `captures/<capture_id>/report.md`：对齐、切片、未固化步骤、回放结论；
- `captures/<capture_id>/package/`：生成的 `graph.yaml` + `scripts/` + `manifest.json`；
- `captures/<capture_id>/state.json`：阶段、`published`、`generalized`；
- `assets/<asset_id>/<version>/`：发布后的可复用资产，之后 `genome_run` 直接复用。

这一步是真实链路的第一次人工评估：生成质量、哪些步骤被判 `not_solidifiable`、
agent 是否会自作主张扩大或缩小轨迹范围，都只能在这条路径上看出来。

关键用例：

- `tests/capture/test_pipeline.py`：冻结即返回、回放 A 排队、换参数回放构成/不构成泛化证据、
  失败保持草稿、同 trace 幂等、脚本变更产生新版本、服务重启把排队中的 capture 收尾为草稿；
- `tests/capture/test_emit.py`：provenance 校验、反向切片丢辅助步骤、内联代码记
  `not_solidifiable`、同 trace 同 pin；
- `tests/capture/test_dispatch.py`：`host.read` 读轨迹、缺参/缺文件报错、中文 label 降级 asset_id；
- `tests/test_native_service.py::test_capture_over_the_socket_publishes_and_reuses_a_graph`：
  原生服务 socket 路径端到端（capture → 发布 → 复用）；
- `pi-extension/test/extension.test.js`：`genome_capture` 导出会话并交文件引用、
  无 trace id 时拒绝；
- SDK `tests/pyromind_runtime/test_reusable_workflows.py::test_execution_host_mirrors_host_local_exports`：
  远端执行面下把宿主侧导出的轨迹镜像进执行面，执行面已有的文件不重复写入；
- SDK `tests/pyromind_runtime/test_reusable_workflows.py::test_capture_freezes_a_graph_and_publishes_after_its_replay`：
  capture 立即返回、回放 A 在 release 后执行、收尾时跑回放 B、发布后再次 capture 幂等。

## 12. 未决项（链路打通后再议）

- capture 触发点：是否允许 agent 主动提议。当前只做用户明说。
- `try` 节点闸门何时打开，以及打开后如何把「分支候选」自动转成候选池。
- 跨会话「同一任务」的自动聚类。
- 回放 B 的数据来源：当前只接受显式 `new_params`，尚未从其它轨迹的参数或合成小样本自动派生。
- 泛化结论尚未进资产元数据：`generalized` 只在 capture 状态与报告里，`genome_get` 看不到，
  需要决定是否在发布时写进 manifest（会改动 pin，得走版本递增）。
- 单轨迹经验是否要在资产元数据上显式标记（现在只在报告里写明「单轨迹，不做骨架宣称」）。
- 时间窗口过滤作为默认拉取策略。
- 共享 root 场景下的 owner 列迁移。
- 是否把 capture 能力反向输出为可导出的 LangGraph 图（见内部讨论结论：只做可选导出后端，不改内核）。
