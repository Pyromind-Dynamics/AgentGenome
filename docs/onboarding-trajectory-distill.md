# 轨迹沉淀（AgentGenome）新人入门

> 对应代码：AgentGenome `0.6.43`（`pyproject.toml`）+ SDK 侧 `harness_adapter/agentgenome_bridge.py`
> 设计文档：`docs/v2-capture.md`（原理）、`docs/v2-plugin.md`（插件）
> 本文只讲「现在是什么样、分几步、谁做哪一段」，不讲演进史。

---

## 0. 一句话

用户在会话里跑通一条链路后说「沉淀一下」，系统把**这次真实跑过的步骤**固化成
「一张图（graph）+ 一组脚本 + 一份契约（manifest）」，回放验证通过后发布成可复用资产；
以后同样的活直接 `genome_run` 重跑，而不是让模型再猜一遍。

沉淀的产物不是文档，是**能执行的东西**。这是理解全部设计的起点。

---

## 1. 四个角色

| 角色 | 是谁 | 干什么 | 会不会调模型 |
| --- | --- | --- | --- |
| 用户 | 人 | 说「沉淀一下」；确认计划；做人工门禁 | — |
| agent | Pi 里的 LLM | 语义判断：选哪条链路、哪步是什么、提示词怎么写 | 就是模型 |
| Pi 插件 | `pi-extension/index.js`（JS 薄壳） | 注册 17 个 `genome_*` 工具；导出会话轨迹；把工具调用转给 Python | **不调** |
| Python 服务 | `agentgenome/` | 全部确定性计算：解析、对齐、切片、生成、编译、回放、发布 | **不调** |
| SDK 宿主 | `harness_adapter/agentgenome_bridge.py` | 给图提供执行面：读文件、跑命令、调宿主工具、问人 | 不调 |

关键分界线：**插件和服务一行模型调用都没有。** 所有需要「理解」的地方都交回 agent，
所有需要「算准」的地方都留在 Python。这条边界让沉淀可测试、可复现、零 API 成本。

---

## 2. 能被固化的只有四种步骤

沉淀的每一步必须落进下面四种之一，否则拒绝发布（没有「先记个 gap 以后再说」这条后路）。

| 形态 | 语义 | 回放时谁执行 | 例子 |
| --- | --- | --- | --- |
| `run` | 固定 shell 命令 | 引擎跑命令 | `python3 scripts/clean.py {input} > out.jsonl` |
| `tool` | 固定宿主工具调用 | 引擎调宿主端口，无模型 | `df_submit_pipeline(dataset=...)` |
| `human` | 人工门禁 | 图停下等人，`genome_resume` 唤醒 | Label Studio 复核导出 |
| `llm` | 真正的 agent 裁量 | 交回 agent，`genome_step_result` 交账 | 「逐条生成回答」 |

判据是**调用形式是否固定**，不是「有没有用到 LLM」：
脚本内部调模型但命令固定 → `run`；没有固定调用形式 → `llm`。

---

## 3. 主路径：七步

```
①取轨迹 → ②归一化 → ③机械候选 → ④结构计划 → ⑤确认+预检 → ⑥冻结包 → ⑦回放+发布
   LLM     工程        工程         工程+LLM      工程          工程        工程
```

### ① 取轨迹（LLM 发起，工程执行）

- 触发只有一个：**用户明说**。agent 不主动提议。
- agent 调 `genome_trace` 列出会话 → 挑 `trace_ids` → 调 `genome_distill`。
- 插件 `session-trace.js` 用 Pi 标准 `ctx.sessionManager` 读会话，导出成文件，
  只把**路径**交给 Python（轨迹不走 RPC 帧，避开 1 MiB 单帧上限）。
- 导出用 `complete: true`：不裁剪字段、不丢步骤；超过 **4 MiB** 直接失败并让人拆会话，
  不悄悄降级。消费侧对 `dropped_steps > 0` 一律拒收。

> 为什么纠结这个：曾经因为导出被裁剪，蒸馏从残缺证据里「合法地」丢步骤，
> 链路看起来沉淀成功、复用却跑不动。

### ② 归一化（工程，`capture/trace.py`）

把 Pi 的原始事件流变成 `Step` 列表，每个 Step 带：

- `seq`：全局单调序，provenance 的锚点，必须无洞
- `kind`：`shell / llm / human / tool_call / write`
- `command_shape`：归一化命令形状（路径→`<path>`、数字→`<num>`、脚本→`<script:name>`）
- `reads` / `writes`：资源**角色**（`source / rules / script / model / report / output`）
- `read_paths` / `write_paths`：**具体路径**，只用于依赖切片
- `success` / `rejected`：失败和被用户拒绝的步骤保留并打标，留给后面判垃圾

同时丢掉两类纯噪声：只读探查（`read/grep/ls`、`cat/head/git status`）和只改计划的工具。

### ③ 机械候选（工程，`capture/plan.py`）

**模型不写整张图。** 插件从 trace 里按「产物由谁生产、被谁消费」机械抽取候选链路：

- 从 `final_artifact` 出发反向求依赖闭包，闭包外的探测、失败尝试、本地试跑 → 旁支
- 写后即删的临时文件、`/tmp` 试跑、只读工具 → 排除
- `create/import/upload/submit/label_studio_project` 这类有副作用的工具 → 保留
  （没有结果路径时生成稳定回执路径，并标成 ambiguity）
- `genome_*` 管理调用不进业务图（避免把「沉淀」这个动作本身当成链路一步）
- 根输入自动生成 `{参数名}` + `path` 类型的 `input_contract`

输出 `planning.mode = mechanical_candidate` + 少量 ambiguity。模型只需要展示和纠错。

### ④ 结构计划（工程，`capture/distill.py: draft_distill`）

这一步只做**结构**判断，不落盘、不排队：

- `plan_steps`：把四种形态规范化（`run` 命令必须输入产出都在命令行上；内联代码/探测命令直接拒）
- `_scope_split`：判「旁支」还是「链路被截断」。判据只有一条数据流规则——
  这步产物被链路后续读取、或它读了链路上游产物 → 相连；否则旁支。
  **脚本不算数据**（本地试跑和正式提交共用同一个脚本不算相连）
- `_handover_steps`：识别人工交接（用户中途说「我已经复核导出到 xxx」），
  这种产物**不是**根输入，必须补成 `human` 节点
- `unresolved`：轨迹不全、占位符缺值、`final_artifact` 对不上等问题先攒起来

然后 `dispatch._prepare_distill` 额外做两件事：

- `_plan_problems`：计划级问题
- `_dry_build_problems`：在**临时目录**里真跑一遍构建（切片/emit/编译），
  把编译错误直接带回这次响应，然后删掉临时目录

> 所以「计划阶段不读脚本」这句话现在要打个折：它不物化脚本、不建正式包、不排队，
> 但会做一次用完即弃的干跑构建，目的是别让用户在确认之后才第一次听说「编译不过」。

### ⑤ 确认 + 预检（工程，`genome_confirm_distill`）

- agent 把 `review` 展示给用户（步骤表、根输入、旁支、人工门禁、最终产物、验证范围）
- 用户确认后调 `genome_confirm_distill(plan_id, digest?)`
- 服务端重新读一次 trace，比对 `digest`（轨迹变了就拒），再跑**一次完整 preflight**
- `preflight_matches_draft` 比对预检结果与用户看过的计划：步骤类型、来源、输入输出、
  `keep`、最终产物、策略、实际入图步骤——**任何语义变化都拒绝**，不能让用户没看过的链路落库
- 通过才创建 capture（`capture_id = distill-<plan_id>`）
- 必须带 `validation_params` + `execution_scope.mode=sample|subset`；
  `full` / `unspecified` 不能当自动验证范围

### ⑥ 冻结包（工程，`capture/pipeline.py: prepare_capture`）

按顺序：

1. `align_traces` — 多轨迹签名对齐（见下方说明）
2. `reverse_slice` — 从最终产物反向切片，得到最小必需节点集，其余进 `auxiliary`
3. `build_package` / `emit.py` — 生成 `graph.yaml` + `scripts/` + `manifest.json`
   - 脚本**原样搬运**，只做槽位替换（`{input.x}` / `{output.x}` / `{artifact}`）
   - 脚本按文件名调用的同目录 helper（`.py/.sh`）传递性一起进包，上限 32 个
   - `artifact.at`：产物与某个输入同目录时，钉回原目录（否则相对引用会断）
   - 写盘前 `_verify_provenance`：节点 provenance 并集必须**恰好等于**骨架步骤集合，
     多报 `invented_steps`、漏报 `uncovered_steps` 直接抛错
4. `compile()` — 走 `builder/compiler.py` 同一条编译路径
5. `_require_solidified` — 有固不下来的步骤就失败，列序号和原因，给三条出路
6. `sample.inspect` — 对根输入做**有界采样**（CSV 表头+20 行、单文件 ≤32 KiB、
   总量 ≤512 KiB），落 `captures/<id>/samples/`，**不进包**（包字节是证据，事后改会让发布失效）
7. `queue_replay` — 回放 A 入队，工具立即返回，不阻塞会话

> 对齐算法（Needleman-Wunsch + 支持度阈值）代码还在，但新主路径下 `distill` 只喂
> **一条确定性轨迹**（由 label + 命令序列派生 `trace_id`），对齐退化成直通。
> 真正做多轨迹对齐的是旧的 `genome_capture` 直通通道（现在标为 legacy）。

### ⑦ 回放 + 发布（工程，`captures.finish`）

- 回放 A（同参数）在宿主 worker 上跑，成功就是发布证据
- 收尾时 `captures.finish` 分流：
  - `waiting` → 停在人工节点，不判定，等 `genome_resume`
  - `interrupted` 且检查点里还有人工节点 → 记 `interrupted`，可续跑
  - `succeeded` → （可选）回放 B → 生成迭代基线 → `publish(evidence=run_id)`
- 发布成功 `stage=published`，失败保持 `stage=draft` + 报告写明失败节点
- 幂等：包字节相同（`pin` 相同）直接复用，不产生新版本；同名不同内容按 patch 递增

产物位置（AgentGenome root，SDK 默认 `$WORKSPACE_DIR/agentgenome`）：

```
captures/<capture_id>/report.md      对齐、切片、旁支、未固化步骤、回放结论
captures/<capture_id>/package/       graph.yaml + scripts/ + manifest.json
captures/<capture_id>/state.json     stage / published / generalized
captures/<capture_id>/samples/       根输入采样（证据，不进包）
assets/<asset_id>/<version>/         发布后的可复用资产
```

---

## 4. 工程侧实现清单

| 模块 | 职责 | 关键函数 |
| --- | --- | --- |
| `pi-extension/index.js` | 17 个工具注册、system prompt 的 13 条 guidance、trace/capture 本地导出 | `createGenomeExtension` |
| `pi-extension/session-trace.js` | 会话枚举、完整导出、4 MiB 上限 | `listSessions` / `exportTraces` |
| `agentgenome/dispatch.py` | 动作分发；计划/确认两阶段；干跑构建 | `_prepare_distill` / `_confirm_distill` |
| `capture/trace.py` | 事件 → Step；命令解析；角色；噪声过滤 | `capture_trace` / `parse_command` |
| `capture/plan.py` | 机械候选链路 | `build_candidate_plan` |
| `capture/distill.py` | 计划、链路校验、范围复核、脚本物化、预检 | `draft_distill` / `distill` / `_check_chain` |
| `capture/align.py` | 多轨迹签名对齐 + 分类 | `align_traces` |
| `capture/slice.py` | 反向切片 | `reverse_slice` |
| `capture/emit.py` | 生成 graph/scripts/manifest；provenance 校验 | `build_package` |
| `capture/pipeline.py` | 冻结编排、幂等、失败报告 | `prepare_capture` |
| `capture/sample.py` | 根输入有界采样 | `inspect` |
| `capture/replay.py` | 回放 A / B | `queue_replay` / `run_probe` |
| `capture/baseline.py` | 迭代基线（sha256 断言） | `derive` |
| `captures.py` | capture 状态机、发布判定 | `finish` |
| `service.py` | 资产库、版本、publish、resume | `GenomeService` |
| `builder/compiler.py` | 图 schema 编译与校验 | `compile` |
| `core/engine.py` | 执行引擎：四种 `do` 节点 | `_ai_work` / `_call_tool` / `_human_work` |
| `core/ports.py` | 端口协议（shell/tool/human/agent） | `Ports` |
| SDK `agentgenome_bridge.py` | `ExecutionHost`：read/read_many/head/run/tool/ask/write | `ExecutionHost` |
| SDK `pi-runtime/src/genome-host.ts` | 把宿主能力注入 Pi 扩展，校验插件已连上 | `genomeHostAccess` |

---

## 5. LLM 侧实现清单

LLM 只做**语义**，而且被工具 schema 和 guidance 夹在很窄的通道里。

| LLM 做什么 | 为什么只能它做 |
| --- | --- |
| 判断该不该沉淀 | 触发点是用户意图，不是规则 |
| 选哪几条 trace、什么顺序 | 会话内容只有模型读得懂 |
| 起 `label`、`applies_when` / `not_applies_when` / `input_contract` | 适用范围的语义判断 |
| 把计划翻译成人话给用户确认 | 用户看不懂 YAML |
| 修 ambiguity（只提交被点名的节点） | 需要理解「这步到底在干嘛」 |
| 写 `llm` 节点提示词（含原 skill、命令形态、输入输出、格式校验） | 提示词质量决定回放质量 |
| 写 `human` 节点工作单 | 要告诉人具体做什么 |
| 判断范围要不要截取，并在必要时问用户 | 语义歧义 |
| 沉淀后读 `report` 汇报结论 | 人话 |
| 回放时执行 `llm` 节点、写文件、`genome_step_result` 交账 | 图里唯一的模型环节 |
| 失败分类：基础设施 → requeue / 脚本错 → 重新沉淀 / 人工 → resume | 需要判断失败性质 |
| 复用侧：`list → get → prepare_run → 确认 → confirm_run` | 匹配真实输入 |

### LLM 明确**不能**做的

- 手写完整 graph / steps（第一次调用**必须**省略 `steps`）
- 自己缩小沉淀范围（只能由插件报「范围需要复核」+ 用户确认）
- 编造 provenance、把固定命令包装成 `llm` 节点（`_check_llm_promotions` 机械拒绝）
- 把人工产物当根输入（`_handover_steps` 会拦）
- 替用户做人工步骤、伪造 artifact
- 用文件工具重写图里已固化的脚本，或用 terminal 手跑等价命令
- 决定能不能发布（回放说了算）

---

## 6. 分工的一句话原则

> **LLM 决定「哪条链路、哪一步是什么、要写什么话」；
> 工程决定「能不能建、建得对不对、跑不跑得通」。**

这样分的三个理由：

1. **可复现**：同样的 trace + 同样的 steps 必须得到同样的包（`trace_id` 由 label + 命令序列派生）
2. **可测试**：确定性部分 25 个测试文件覆盖，不需要真模型
3. **防幻觉**：模型最容易错的地方（路径、依赖、读写关系）恰好是机械可算的地方

---

## 7. 五道防线（怎么保证沉淀的不是垃圾）

| 防线 | 机制 | 位置 |
| --- | --- | --- |
| 结构 | 只看命令形状 + 资源角色，不看自然语言 | `trace.signature` |
| 频次 | 多轨迹支持度阈值，单条轨迹标注「无交叉验证」 | `align.classify` |
| 依赖 | 从最终产物反向切片，无关步骤剔除 | `slice.reverse_slice` |
| 溯源 | provenance 并集必须精确等于骨架集合 | `emit._verify_provenance` |
| 回放 | 回放 A 不通过不发布；失败留草稿 | `captures.finish` |

外加三类**发布前硬闸门**：

- 链路完整性（`_check_chain`）：某步读的路径由会话里更早的成功步骤产出、而那步没进计划 → 报错
- 完整固化（`_require_solidified`）：有固不下来的步骤 → 拒绝发布
- 异步工具（`_async_wait_issues`）：提交类工具只拿到受理单，链路里必须有一个消费受理单、
  等终态、写真实产物的步骤，否则直接拒

---

## 8. 常见失败与出路

| 现象 | 原因 | 出路 |
| --- | --- | --- |
| 「轨迹导出不完整（dropped_steps）」 | 会话太长或导出被裁 | 把链路拆到更短会话重新沉淀 |
| 「链路范围需要用户复核」 | 计划比会话窄，且丢掉的步骤与链路相连 | 用户确认后带 `review_confirmed` + `review_reason` 重提 |
| 「链路不完整，缺第 N 步」 | 少交了上游产出步骤 | 补进 steps；脚本可随包搬运，不算缺 |
| 「有步骤固不下来」 | 内联代码 / 探测命令 / 没有路径 | 改成 `run`/`llm`/`tool`/`human`，或声明成输入边界 |
| 回放停在 `waiting` | 人工门禁 | 用户做完 → `genome_resume(run_id, artifact?)` |
| 回放 `interrupted` | 沙箱/连接等基础设施问题 | `genome_replay_requeue(run_id)`，不算新沉淀 |
| 回放脚本报错 | 链路缺陷 | 修脚本 → 重新 `genome_distill`，不要用重排掩盖 |
| 编译不过 | 图结构非法 | 计划阶段的干跑构建已经会报，直接改计划 |

---

## 9. 新人读代码的顺序

1. `pi-extension/index.js` — 先看工具和 guidance，知道 agent 被允许做什么（30 分钟）
2. `agentgenome/dispatch.py` 的 `_prepare_distill` / `_confirm_distill` — 两阶段协议（30 分钟）
3. `capture/plan.py` 的 `build_candidate_plan` — 候选链路怎么来的（1 小时）
4. `capture/distill.py` 的 `draft_distill` / `distill` — 校验有哪些（1.5 小时）
5. `capture/emit.py` 的 `_Plan.plan` — 图是怎么长出来的（1 小时）
6. `capture/pipeline.py` 的 `prepare_capture` + `captures.py` 的 `finish` — 冻结与发布（1 小时）
7. `docs/v2-capture.md` — 回头再看，很多「为什么」在里面

本地验证：

```sh
cd /Users/zhangpeng/PycharmProjects/AgentGenome
python3 -m pytest -q                      # 引擎 + capture
cd pi-extension && npm test               # 插件
```

端到端（不调真实模型）：

```sh
cd /Users/zhangpeng/PycharmProjects/software-agent-sdk
uv run python scripts/validate_agentgenome.py \
  --template templates/data-cleaning --home <tmp>/home --workspace <tmp>/workspace
```

---

## 10. 术语速查

| 词 | 意思 |
| --- | --- |
| trace / 轨迹 | 一次会话的事件流，导出成 JSON 文件 |
| Step | 归一化后的一步，带 seq / 命令形状 / 读写 |
| 签名 | `(kind, command_shape, reads, writes)`，对齐和去重都靠它 |
| provenance | 节点指回真实 `seq` 的凭证，不能编 |
| 旁支（branch） | 与链路不相连的步骤，记录但不进图 |
| 门禁（gate） | `human` 节点，图停在这里等人 |
| capture | 一次沉淀的记录（状态 + 报告 + 包） |
| package | 冻结后的 `graph.yaml` + `scripts/` + `manifest.json` |
| pin | 包字节的摘要，用于幂等和版本判定 |
| 回放 A / B | 同参数回放（发布证据）/ 换参数回放（泛化证据，当前产品上先关掉） |
