# AgentGenome 轨迹沉淀（Capture）架构评审

> 评审日期：2026-10-21
> 评审对象：`/Users/zhangpeng/PycharmProjects/AgentGenome`（`agentgenome/capture/*`、`agentgenome/{captures,dispatch,plans}.py`、`pi-extension/session-trace.js`）
> 关联宿主：`software-agent-sdk/harness-adapter/agentgenome_bridge.py`、`pi-runtime/src/genome-host.ts`
> 评审视角：批判性 —— 先假定它有系统性问题，再去找证据；找不到证据的地方如实说明

---

## 结论先行

**机制是完整的，工程质量在平均线以上；但"沉淀"这件事的语义基础建立在对 shell 命令的词法猜测上，精度天花板被锁死在解析器上；而验证链只证明"命令跑完了、文件非空"，不证明"沉淀下来的是原会话真正想做的事"。**

一句话概括风险：**这套系统能稳定地把"一段命令序列"复制成可复用资产，但不能保证复制的是"意图"。** 而用户说"沉淀一下"时，要的是后者。

三条最硬的证据：

1. `trace.py` 判定文件读写方向靠硬编码的 flag 白名单（`_WRITE_FLAGS` / `_READ_FLAGS` / `_PROBE_COMMANDS`），新工具、新 flag 一律静默退化为"读"或"未知"。
2. `plan.py` 用中文正则 `复核|导出|标注|打完|弄好|改好|人工|手工` 判断"这一步是不是人工交接"—— 这是在"确定性机械计算"的外衣下做自然语言语义猜测。
3. `emit.py:_root_verify()` 在未声明 `output_contract` 时，最终验收就是 `test -s {output.key}`（文件非空）；`baseline.py` 的回归断言是 **sha256 字节摘要**，源码注释自己写着 `bytes, not behaviour`。

---

## 一、功能全貌

### 1.1 分层结构

```
┌─ 采集面 ────────────────────────────────────────────────────────┐
│ Pi 会话 (session.jsonl)                                          │
│   └─ pi-extension/session-trace.js                               │
│        traceSteps()  配对 toolCall ↔ toolResult，裁剪 2000 字符   │
│        buildTrace()  产出 trace_version=1 的 JSON                │
│        exportTraces() → public_data/agentgenome-traces/*.json    │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌─ 分析面（agentgenome/capture/，7 个阶段模块）────────────────────┐
│ Stage 0  distill.py   agent 提交语义链路 → 轻量结构计划           │
│ Stage A  trace.py     JSON → Step（kind/shape/read/write/rejected）│
│ Stage B  align.py     Needleman-Wunsch 多轨迹对齐 → skeleton/branch/garbage │
│ Stage C-1 slice.py    最终产物反向闭包 → 保留节点 / 辅助节点      │
│ Stage C-2 emit.py     → graph.yaml + scripts/ + manifest.json    │
│ Stage D  replay.py    回放 A（原参数）/ 回放 B（换参数，泛化探针）│
│ Stage E  baseline.py  采样输入 + 产物摘要 → 后续修订的回归基线    │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌─ 资产面 ────────────────────────────────────────────────────────┐
│ GenomeService：assets / versions / captures / plans / revisions  │
│ 纯文件存储：manifest.json + graph.yaml + JSONL 台账 + SQLite(阶段)│
│ 状态机：draft → queued → running → published / reused            │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌─ 复用面 ────────────────────────────────────────────────────────┐
│ genome_run（原参数）/ genome_prepare_revision（改脚本再验）       │
│ CORE 引擎执行 graph.yaml，verify 编译期钉死、运行期不可降级        │
└──────────────────────────────────────────────────────────────────┘
```

### 1.2 两个入口，语义重叠

| 入口 | 谁做语义归纳 | 适用 | 代码 |
|---|---|---|---|
| `genome_capture` | 无（纯机械） | 命令全部可固化、无 agent 裁量 | `pipeline.prepare_capture` |
| `genome_distill` | Pi 里的 agent | 含编辑工具写入 / agent 判断 / 人工门禁 | `distill.distill` |

两条路径最终都汇到 `prepare_capture`，但**校验逻辑不共享**：`pipeline` 里的 `_forbid_replay_literals` 和 `distill` 里的 `_check_replay_literals` 是同一件事的两份实现。

### 1.3 代码体量（实测）

| 项 | 数值 |
|---|---|
| 生产代码 `agentgenome/**/*.py` | 10,719 行 |
| 其中 `capture/distill.py` | 2,222 行（占 21%） |
| 其中 `capture/emit.py` | 1,023 行（`_Plan` 类单类 328 行） |
| 测试 | 7,667 行（测试/生产 ≈ 0.71） |
| 设计文档 `docs/v2-capture.md` | 1,211 行 |
| 插件打包版本 `pi-extension/*.tgz` | 31 个（0.5.0 → 0.6.38，跨度 9 天） |
| Git 提交 | 10 个 |

---

## 二、做得对的地方

批评之前先说清楚哪些设计是站得住的，否则后面的建议会显得没依据。

**1. 只收完整固化的链路（`_require_solidified`）。**
拒绝"半覆盖的图"，并且拒绝时逐条列出 `#序号 + 原因`。这是对的取舍：一个 60% 固化的资产比没有资产更危险，因为它会以"可复用"的名义骗人。

**2. 降级必写报告，不静默吞。**
`_samples()` 捕获所有异常转成 `{"error": ...}` 写进报告；`pipeline` 每个降级分支都有 note。这个纪律在同类系统里很少见。

**3. 参数边界而不是字面量（`_forbid_replay_literals`）。**
识别出"回放工作目录 ≠ 会话工作目录"这个隐蔽陷阱，并且要求 `--images-root storage` 这种值必须变成 `{参数}`。这是真正踩过坑才能写出来的检查。

**4. 资产隔离 + 回放跑真实沙箱。**
`PYROMIND_AGENT_STORAGE_ROOT/<conversation>/genome-inputs/` 的隔离，以及 `AgentGenomeBackend` 让回放走 SDK 的真实执行面而不是本地进程——这两点让"沉淀结果可执行"这件事不是空话。

**5. 文档诚实。**
`docs/v2-capture.md` 开头就写"本文是实施方案，不是宣传稿"，并且 §5.10–5.20 每节标题带版本号，把每个线上 bug 的成因和修法都记下来了。这份文档本身是资产。

---

## 三、问题清单

按"会不会导致沉淀出错且不被发现"排序。

---

### P0-1　数据流靠 shell 词法解析，精度天花板被锁死

**证据**

`capture/trace.py:56-87`：

```python
_WRITE_FLAGS = {"-o", "--output", "--out", "--outdir", ...}
_READ_FLAGS  = {"-i", "--input", "--in", "--data", "--config", "--model", ...}
_PROBE_COMMANDS = {"cat", "head", "ls", "grep", "rg", "wc", ...}
_READONLY_GIT = {"status", "diff", "log", ...}
```

`_is_path()`（`trace.py:226`）的判定是：`以 / ./ ../ ~/ 开头` 或 `含 "/"` 或 `有扩展名且不以 - 开头`。

**后果**

- 任何不在白名单里的 flag（`--manifest`、`--label-map`、`--src-dir`）都会被归到 `literals`，然后要么被 `_forbid_replay_literals` 拦下要求改写成参数，要么在解析层就丢了方向。
- 更根本的问题：**文件读写方向是命令行的语法属性，不是语义属性。** `script.py in.csv out.csv` 里 `out.csv` 是写，但解析器只看到"位置参数"，一律按读处理。文档 §5.7 自己承认了这点，解法是引入 `declared_writes` 补丁——用一个声明去补解析器的洞。
- 静默退化：解析错了**不报错**，只是读/写集合变小。后续 `_check_chain` 因此认为"上游没产出这个文件"，于是不报"链路不完整"，而是把错误链路当成完整链路放过去。

**这是整个系统最大的单点风险**：`slice` 的反向闭包、`_check_chain` 的完整性判断、`_plan_outputs` 的主产物选择、`emit` 的 `{input.*}` 连线，全部建立在同一个解析结果上。解析偏一寸，下游全偏。

**建议**

- 短期：把 `_WRITE_FLAGS`/`_READ_FLAGS` 从"白名单"改成"黑名单 + 声明优先"。解析不出来时**主动报告不确定**（写进 trace 的 `unresolved`），而不是默认成读。当前"默认读"的取向会把写误判成读，恰好是最危险的错误方向。
- 中期（真正的解法）：**在执行面埋点**。`harness-adapter/pi-runtime/src/sandbox-terminal.ts` 已经是 63KB 的执行面实现，完全有能力在 shell 执行前后记录进程的 open/write（哪怕只记录 mtime + size 变化的工作区文件快照 diff）。有真实 I/O 事实后，`parse_command` 从"唯一来源"降级为"辅助提示"，精度天花板才抬得起来。
- 验收方式：拿 20 条真实会话，对比"解析推断的 I/O" vs "沙箱实测的 I/O"，统计准确率。这个数字目前没人知道。

---

### P0-2　用中文正则做语义判断，与"不调模型"的设计前提冲突

**证据**

`capture/plan.py:38-42`：

```python
_HUMAN_HANDOFF = re.compile(
    r"(复核|导出|标注|打完|弄好|改好|人工|手工|"
    r"label\s*studio|reviewed|exported|handoff)", re.IGNORECASE)
```

`plan.py:43-56` 还有：

```python
_MUTATING_TOOL = re.compile(r"(?:^|[_-])(?:create|import|upload|submit|launch|...)(?:$|[_-])")
_READONLY_TOOL = re.compile(r"(?:^|[_-])(?:get|list|read|search|find|query|...)(?:$|[_-])")
_EPHEMERAL_STEM = re.compile(r"(?:^|[._-])(?:tmp|temp|test|probe|smoke|wtest)(?:$|[._-])")
```

**后果**

文档 §0 明确写"归纳由谁做：**Pi 里的 agent，不是插件**；插件只做机械工作（归一化/对齐/切片/校验/编译/回放），不调用模型"。但上面这三个正则实际上是在**用词法模拟语义**：

- 用户说"我把标签都核对完了，文件在 xxx"——`核对` 不在词表里，漏判。
- 用户说"数据我处理好了"——漏判。
- 工具叫 `process_dataset`（既读又写）——`_MUTATING_TOOL` 和 `_READONLY_TOOL` 都匹配不上，落到"未知"。
- 文件叫 `test_data.csv`——被 `_EPHEMERAL_STEM` 判成临时文件，可能被剪掉。

**这不是"确定性计算"，是"确定性的猜测"。** 它比调模型便宜、可复现，但准确率不可度量，而且失败是静默的（只是某条分支没进图）。

**建议**

- 承认这里需要语义判断，然后**把它交给唯一有语义能力的地方**：`genome_distill` 的 `steps` 里显式声明 `human` 节点。正则只作为"候选提示"写进报告，供 agent 参考，**不参与阻断性判断**。
- 如果坚持不调模型，至少要给这些正则加**置信度 + 反例测试**：把"漏判"和"误判"分别做成回归用例，让准确率变成一个可观测数字。

---

### P0-3　验证只证形状，不证内容

**证据**

`capture/emit.py:722-731`：

```python
def _root_verify(plan, composed):
    contract = plan.output_contract.get(key)
    if contract is None:
        ...
        return f"test -s {{output.{key}}}"     # ← 默认验收：文件非空
```

即使声明了 `output_contract`，也只检查三件事：能按 jsonl/json/text 解析、行非空、必需字段存在（`emit.py:745-760`）。**不检查任何数值、不检查行数、不检查内容分布。**

`capture/baseline.py:46`：

```python
"""One digest assertion per recorded artifact, capped: bytes, not behaviour."""
```

**后果**

回放 A 通过 = "命令退出码为 0 且产物文件非空"。
回放 B 通过 = "换个输入再跑一遍，产物文件还是非空"。

于是：**一条把数据过滤错的链路，只要它跑得完，就能通过全部机械闸门并发布。** 而 §9 的标题恰恰是"怎么保证提取出来的不是垃圾"。

真正能抓出"沉淀错了"的，只有 `baseline` 的 sha256——但那个只在**后续修订**时才用（`baseline.py:5-7` 注释："only enter an asset's suite when a revision actually promotes a changed package"），首次沉淀时完全不生效。

**建议**

- 回放 A 的验收必须从"非空"升级为"**与原轨迹产物对比**"。会话里已经有原产物的路径，回放后拿两份文件比：行数、字段集合、数值分布（均值/分位数）、枚举字段的取值集合。允许小误差，但要有一个可解释的相似度。
- 这个对比不需要模型，纯结构统计就够，而且能抓住 90% 的"链路固化错了"。
- 如果连原产物都取不到（会话产物已被覆盖），那**必须降级为"未验证"并在报告里明写**，而不是用 `test -s` 冒充验证通过。

---

### P0-4　含人工节点的链路被验证豁免

**证据**

`capture/pipeline.py:183-184`：

```python
if "human" in package.graph.node.needs:
    return None, "链路含人工门禁，回放 B 无法自动恢复，跳过换参数回放并按窄口径发布"
```

**后果**

含人工环节的链路 —— 也就是文档 §1 那个核心场景"预打标 → **Label Studio 人工复核** → 清洗 → 训练 → 评测" —— **永远拿不到泛化验证**，却仍然会被发布（`generalized=False`，窄口径）。

这恰好颠倒了风险分布：**越复杂、越需要验证的链路，验证越少。** 简单脚本链路反而被换参数回放验了两遍。

**建议**

- 人工节点不是"不能回放"，是"需要一个人"。可以让回放 B 停在人工节点，**用第一次人工产出的副本**继续跑后续步骤，验证"人工之后的部分"是否可复现。这部分恰恰是最容易固化错的。
- 退一步：如果确实做不了，至少把 `generalized=False` 的资产在 `list`/`get` 里**显著标记为"未泛化"**，别让调用方以为它和验过的资产等价。

---

### P1-1　"谁产出了这个文件"在两处定义相反

**证据**

`capture/slice.py:48-62`：

```python
def producer_map(slots, bindings=None):
    """Last skeleton writer wins: a later step overwrites the earlier product."""
    for order, slot in enumerate(slots):
        for path in slot.write_paths:
            result[canonical_path(path, given)] = order   # ← 后者覆盖，last wins
```

`capture/distill.py:374-382`：

```python
produced = {}
for trace in traces:
    for step in trace.steps:
        for path in (*step.write_paths, *step.reported_writes):
            produced.setdefault(path, (...))               # ← 先者占位，first wins
```

**后果**

同一个路径被写两次（先草稿、后正式；或先小样、后全量）时：

- `slice` 认为最终产物由**最后一步**产出，反向闭包从最后一步回溯 —— 正确。
- `_check_chain` 认为该路径由**第一步**产出，于是当链路只包含最后一步时，会判定"上游步骤缺失"或反之放过 —— 与切片结论矛盾。

结果是同一份轨迹，切片说"链路完整"，完整性检查说"缺上游"，或者反过来。这类不一致最难查，因为两处代码单独看都"有注释、有理由"。

**建议**

把 `producer_map` 提升为唯一权威，`_check_chain` 复用它。顺便把"last wins"这个语义写进 `Step` 的 docstring，因为它是整个数据流模型的核心约定。

---

### P1-2　18 个独立校验函数，没有不变量模型

**证据**

`capture/distill.py` 里有 18 个 `_check_*` / `_*_issues` 函数：

```
_check_chain / _check_replay_literals / _check_unconsumed / _check_llm_promotions
_check_final_artifact / _check_command / _check_llm_prompt
_slice_boundary_issues / _generated_root_issues / _spelling_issues
_static_blocking_issues / _async_wait_issues / _cardinality_issue
_scope_split / _review_scope / _handover_steps / _uncovered_steps / _superseded_by_plan
```

`_static_blocking_issues()`（`distill.py:1604-1704`，100 行）就是把其中 5 个 `extend` 进去，再加 4 段内联逻辑。

对照文档 §5.10–5.20，**每一节标题都带版本号**：

```
### 5.10 llm 节点保留原始执行方式（0.6.5）
### 5.11 scripts-first 与 artifact 扩展名（0.6.6）
### 5.12 tool 节点与宿主工具端口（0.6.8）
### 5.13 人工交接的前半段（0.6.12）
### 5.14 本地试跑与平台提交共用脚本（0.6.13）
### 5.15 完整轨迹导出与截断拒收（0.6.14–0.6.17）
### 5.16 人工闸门的唤醒与回放补队（0.6.19）
### 5.17 产物落点跟随输入（artifact.at）
### 5.18 参数类型与平台提交路径（0.6.27）
### 5.20 计划预检误判修正（0.6.29）
### 5.19 机械候选链路（0.6.28）
```

再对照 `pi-extension/` 下 **31 个 tgz、9 天**（平均每天 3.4 个版本）。

**这不是在否定这些修复**——每一个都对应一个真实踩坑，修得也对。问题是**修复的形态**：每遇到一个真实会话的失败，就加一条独立的 `_check_*`，而没有一个统一的不变量模型去约束它们之间的关系。

**后果**

- 新增规则可能破坏旧规则（规则之间没有优先级/依赖声明）。
- 规则的实际触发条件无法推理，只能靠 2340 行的 `test_distill.py` 逐个盯。
- `distill.py` 已经 2222 行，读一遍需要一小时以上，新人无法接手。

**建议**

把 18 条规则收敛成 **4 条不变量**，每条不变量内部再细分检查项：

1. **闭合性**：链路里每个输入，要么由链路内更早的步骤产出，要么是显式声明的参数边界。（吸收 `_check_chain` / `_check_unconsumed` / `_slice_boundary_issues` / `_handover_steps`）
2. **可回放性**：链路里每个字面量，要么在包目录下可解析，要么是参数。（吸收 `_check_replay_literals` / `_spelling_issues` / `_generated_root_issues`）
3. **可固化性**：链路里每个步骤，要么有可机械重放的命令/工具/提示词，要么是 human。（吸收 `_check_llm_promotions` / `_require_solidified` / `_superseded_by_plan`）
4. **参数一致性**：声明的参数类型、基数、实际取值三者自洽。（吸收 `_cardinality_issue` / 参数归一化检查）

收敛之后，新增 bug 的修复会落到某条不变量下，而不是再造第 19 个 `_check_*`。

---

### P1-3　同一逻辑两份实现

| 逻辑 | 位置 A | 位置 B |
|---|---|---|
| 回放字面量检查 | `pipeline._forbid_replay_literals` | `distill._check_replay_literals` |
| distill 入口 | `dispatch: action == "prepare_distill"` | `dispatch: action == "distill"`（同一函数） |
| 产物主键选择 | `emit._plan_outputs`（`write_paths[-1]` 兜底） | `slice.producer_map`（last wins） |

**后果**：改一处漏一处，且两处的错误文案还不一样，日志里出现时很难判断走的是哪条路径。

**建议**：`_check_replay_literals` 直接调用 `pipeline` 的实现（或抽到 `trace.py` 作为共享工具）。`prepare_distill` 作为 `distill` 的别名保留一版后删除。

---

### P1-4　对齐顺序敏感，同一批轨迹结果不可复现

**证据**

`capture/align.py:1-5`：

```
Alignment is progressive: the first trace seeds the columns, every later trace is
aligned against the current columns with Needleman-Wunsch. The result therefore
depends on trace order, which is deterministic and stated in the report.
```

**后果**

"确定性"只对**固定顺序**成立。用户选了同样 3 条轨迹，只是勾选顺序不同，就可能得到不同的 skeleton（因为第一条轨迹的步骤成为列种子，后续轨迹的插入/删除位置随之改变）。

对于一个宣称"append-only、可审计、可复现"的系统，这是一个语义上的漏洞：**同样的输入集合，不同的输入顺序，产出不同的资产**。

**建议**

- 对齐前先按 `trace_id` 排序（确定性且与用户操作无关），代价几乎为零。
- 或者做真正的多序列对齐（progressive alignment 先建 guide tree），但那是另一个量级的工程。排序是性价比最高的补丁。

---

### P2-1　巨型单元

- `emit._Plan` 单类 328 行，`plan()` 分 5 步，`_classify` / `_plan_outputs` / `_build_nodes` 各自 30–80 行，职责从"节点命名"一路到"artifact 落点"。
- `distill._static_blocking_issues` 100 行，内含 4 段无注释的内联条件。
- `distill.py` 单文件 2222 行。

**建议**：`_Plan` 至少拆成 `NodeNamer` / `OutputPlanner` / `NodeBuilder` 三个纯函数式步骤，`_Plan` 只持有状态。这一步不做，P1-2 的规则收敛也做不动。

---

### P2-2　测试用的是合成轨迹

**证据**

`tests/capture/conftest.py` 里只有 5 个手写脚本常量（`PREL_LABEL` / `CLEAN` / `TRAIN` / `EVALUATE` / `WAIT`），`tests/` 下**没有任何真实会话的 trace fixture**（`find tests -name "*.json*"` 为空）。

**后果**

7,667 行测试全部围绕 5 个已知脚本的形状。而真实会话里出现的是 `df_submit_pipeline`、`sandbox_sample.py`、`label_studio_project`、带中文路径的 `cp`、`python3 -c "..."` 内联脚本——这些形状**一条都没进回归**。

这解释了为什么会有 31 个 tgz：**真实数据的多样性只能靠线上踩坑来发现，测试集无法预测它。**

**建议**

从 `workspace/conversations/*/public_data/agentgenome-traces/*.json` 里挑 3–5 条真实轨迹（脱敏后）作为 golden fixture，断言"给定这条轨迹，沉淀出的 graph 结构稳定"。这条测试的价值高于现有 2000 行合成用例。

---

### P2-3　异步语义容易被误读为成功

**证据**

`capture/pipeline.py:8-9`：

```
Nothing here executes a graph: replay A is queued on the host and replay B runs
when that run finalizes, so a capture call returns as soon as the package is
frozen.
```

**后果**

`genome_capture` 返回 `CapturePlan` 时，回放 A 还在队列里。调用方（尤其是 agent）看到"成功返回 + 有 report 路径"，很容易当成"沉淀完成"，而实际状态是 `draft`，可能几分钟后失败。

**建议**：返回体里把 `status: "queued"` 放在最外层且第一位，`report` 路径标注"当前为构建报告，回放结果稍后写入"。当前 `payload()` 里 `run_id` 和 `reused` 并列，没有显式的状态字段。

---

## 四、三个根本矛盾

上面是逐条问题，下面是它们在架构层面的共同根源。

### 矛盾一：「确定性机械计算」 vs 「靠自然语言正则猜语义」

文档反复强调"插件不调模型、只做机械工作"，这是为了可复现和可审计，动机正确。但实现里出现了 `_HUMAN_HANDOFF`（中文动词表）、`_MUTATING_TOOL`（工具名前缀表）、`_EPHEMERAL_STEM`（文件名关键词表）。

**这三者是语义判断，只是用正则实现了。** 它们不调模型，但也不"机械"——它们是在猜，而且猜错了不报警。

这个矛盾不解决，系统就处在一种尴尬位置：既没有模型的语义能力，也没有解析器的确定性。

**出路只有两条**：要么把语义判断显式交给 agent（并接受它不可机械验证），要么在执行面采集真实事实（I/O 埋点），让"猜"变成"读"。当前是两条都没走，走了第三条"用正则假装机械"。

### 矛盾二：「沉淀意图」 vs 「固化命令」

用户说"沉淀一下"，想沉淀的是"**我知道怎么把这类数据洗干净**"这件事；系统沉淀下来的是"**这 7 条命令，按这个顺序**"。

两者的差距在会话里由 agent 的判断填补：为什么选这个文件、为什么这个阈值、为什么先过滤后去重。这些判断沉淀后：
- 如果落在 `llm` 节点里 → 变成一段提示词，不可验证；
- 如果落在 `label` / `description` 里 → 只是人看的文字；
- 如果落在 shell 命令的参数里 → 变成硬编码字面量，换数据就错。

文档 §0 的取舍是"脚本只搬运不重写"、"语义归纳由 agent 做"——这在工程上是对的（不重写就不会引入新 bug），但它同时意味着**资产的上限就是"命令模板"，不是"可迁移的方法"**。

这不是 bug，是产品定位问题。但如果对外宣称"沉淀经验"，就要接受用户会拿它去跑不一样的数据，然后失败。**建议在 `manifest` 里显式记录"这条资产的适用范围"**（输入 schema、行数量级、依赖的外部服务），让复用方在跑之前就知道边界。

### 矛盾三：「append-only 可审计」 vs 「规则随版本漂移」

`captures` 是 append-only 的，`manifest` 带 `schema_version`，回放记录 `provenance`。这套审计设计是认真的。

但**生成资产的规则本身没有版本化**：同样两条轨迹，用 0.6.12 的代码和 0.6.29 的代码跑，得到的 graph 不同，而 manifest 里没有任何字段记录"这条资产是由哪版规则生成的"。

于是"可审计"只覆盖了"资产被谁、何时、用哪条轨迹生成"，没覆盖"用什么逻辑生成的"。当 0.6.30 修了一个解析 bug，历史资产的对错就无法追溯。

**建议**：`manifest.capture` 里加 `engine_version` + `rule_set_digest`（18 个 check 函数的源码 hash）。成本极低，但让"规则漂移"变成可观测的。

---

## 五、优化路线

> **⚠️ 本节排序已被 §8 的真实会话复盘部分推翻，请以 §8.8 的重排版本为准。**
> 保留原文是为了记录「静态分析的判断」与「真实故障暴露的问题」之间的差距：
> 本节把「回放验收太弱」排在很前，但那条会话根本没走到回放，卡在构建阶段；
> 本节完全没有预见「路径未归一化」「`artifact.at` 放弃占位符产物」「`validation_params` 覆盖」这三个 bug。
> 本节仍然成立的部分：P0-1（词法解析）、P1-2（规则堆叠）、P1-4（对齐顺序敏感）。

按"投入产出比"排序，不按理论重要性。

### 立刻做（1–2 天，改动小、收益确定）

| # | 事项 | 对应问题 | 改动量 |
|---|---|---|---|
| 1 | `align_traces` 前按 `trace_id` 排序 | P1-4 | 3 行 |
| 2 | `_check_chain` 复用 `slice.producer_map` | P1-1 | 抽函数 + 2 处调用 |
| 3 | `CapturePlan.payload()` 加 `status` 字段并置顶 | P2-3 | 5 行 |
| 4 | `manifest.capture` 加 `engine_version` + `rule_set_digest` | 矛盾三 | 10 行 |
| 5 | 删 `prepare_distill` 别名，`_check_replay_literals` 委托 `pipeline` | P1-3 | 删 30 行 |

### 近期做（1–2 周，需要设计）

| # | 事项 | 对应问题 |
|---|---|---|
| 6 | 回放 A 验收从 `test -s` 升级为"与原产物结构对比"（行数/字段/分布） | P0-3 |
| 7 | 含 human 链路：回放 B 停在人工节点后用首次人工产物续跑 | P0-4 |
| 8 | 3 条真实轨迹做成 golden fixture | P2-2 |
| 9 | 18 个 check 收敛为 4 条不变量 | P1-2 |

### 中期做（1 月+，架构级）

| # | 事项 | 对应问题 |
|---|---|---|
| 10 | 执行面 I/O 埋点（沙箱记录 open/write），`parse_command` 降级为辅助 | P0-1 |
| 11 | `_HUMAN_HANDOFF` 等正则降级为"候选提示"，不参与阻断 | P0-2 |
| 12 | `_Plan` 拆分为三个纯函数步骤 | P2-1 |

### 不建议做

- **不要**为了"更聪明"引入模型去替换正则。那会把"确定性的错"变成"不可复现的错"，审计链直接断掉。
- **不要**在 v2 里开 `try` 节点。文档 §0 已经决定不开，这个决定是对的——分支会让"完整固化"的判定失去意义。

---

## 六、给决策者的三句话

1. **这套东西现在能交付"可复用的命令模板"，交付不了"可迁移的方法"。** 如果业务方期待的是后者，需要在产品叙事上先对齐，否则每一次跨数据集复用失败都会被记成 bug。
2. **最大的技术债不是 2222 行的 `distill.py`，是"文件读写方向靠命令行猜"这个根基。** 规则可以慢慢收敛，根基不换，精度就永远卡在解析器上。
3. **验证链的强度决定了这套系统的可信度上限。** 现在它只能证明"跑得完"，不能证明"跑得对"。把回放验收从"非空"升级到"结构对比"，是投入产出比最高的一次改动——比再修 10 个 `_check_*` 都值。

---

## 七、三个根本矛盾有解法吗

先给判断，再给方案。

| 矛盾 | 可解性 | 性质 |
|---|---|---|
| 一、机械计算 vs 正则猜语义 | **可解** | 假矛盾。缺的不是"选边"，是第三种东西：可观测事实 |
| 二、沉淀意图 vs 固化命令 | **不可全解** | 真矛盾。意图不可沉淀，但**意图的边界**可以 |
| 三、append-only 可审计 vs 规则漂移 | **可解** | 纯工程问题。缺一个字段和一个动作 |

三个矛盾看着互不相干，其实在问同一件事：**这条资产凭什么可以被信任？**

- 矛盾一问「数据流是真的吗」→ 需要 I/O 事实
- 矛盾二问「它适用于我的数据吗」→ 需要适用边界
- 矛盾三问「它还是按当初的逻辑生成的吗」→ 需要规则指纹

三个答案都是 contract 的不同侧面。所以不需要推翻重来，只要把已经散落在 `manifest` 里的碎片收敛成一个显式的 `contract` 段。

---

### 7.1 矛盾一：引入「观测事实」，让猜变成读

**为什么是假矛盾**

"确定性"和"语义"本身不冲突。冲突的是当前这个组合：**用词法去模拟语义，结果两头不占**——既没有解析器的确定性（因为默认值会静默猜错），也没有语义的准确率（因为词表永远不全）。

第三条路是**观测**。文件被写了就是被写了，这是事实，既确定又有语义。

**三级真相来源**

优先级从高到低，每条 I/O 记录都带 `source` 字段：

```python
# agentgenome/capture/trace.py —— Step 增加字段
observed: Mapping[str, tuple[str, ...]] | None = None   # 执行面实测
io_source: str = "inferred"                             # observed | declared | inferred
```

| 级别 | 来源 | 能拿到什么 | 成本 |
|---|---|---|---|
| `observed` | 沙箱执行面埋点 | **写**（可靠）、读（部分） | 中 |
| `declared` | `genome_distill` 的 `steps` 显式声明 | 读写都全 | 零（已有） |
| `inferred` | `parse_command` 兜底 | 读写都可能错 | 零（已有） |

**关键实现点**

执行面已经在 `harness-adapter/pi-runtime/src/sandbox-terminal.ts`（63KB），加一层工作区监听即可：

```
执行前:  watch 工作区（inotify recursive，退化到 snapshot）
执行中:  收集 CREATE / MODIFY / CLOSE_WRITE / MOVED_TO
执行后:  汇总成 writes[]，与命令行解析结果取并集
```

降级链：`inotify` → `snapshot diff`（只扫命令行提到的目录，避免全盘扫）→ `纯解析`。每一级都写进 `io_source`，审计时能看出这条轨迹的 I/O 有多可信。

**诚实的地方**：`writes` 可以精确观测，`reads` 不能（读不改 mtime，atime 在 noatime 挂载上不可靠）。但这不要紧——**producer 判定、反向切片、`{input.*}` 连线全部依赖 writes**，reads 错了只影响连线顺序，影响面小一个数量级。所以 `writes` 用实测、`reads` 用解析 + 声明，是合理的非对称设计。

**最小可行解（1 天，不动执行面）**

在埋点落地之前，先把"静默猜错"改成"显式不确定"：

```python
# trace.py:_simple_command 里，direction is None 且不是已知命令时
# 现状：一律当读（write_paths if written else read_paths）
# 改为：
if direction is None and name not in _WRITE_COMMANDS and name not in _MOVE_COMMANDS:
    unresolved.append((name, token))     # 记进 Command.unresolved
```

然后 `_check_chain` 遇到 `unresolved` 就报"这一步的 I/O 方向无法确定，请用 `genome_distill` 声明"。**把默认值从"猜读"改成"报错"，一行改动就能消灭一整类静默错误。**

---

### 7.2 矛盾二：承认意图不可沉淀，但把边界沉淀下来

**为什么不可全解**

用户说"沉淀一下"，脑子里想的是"我知道怎么洗这类数据"。这个"知道"包含判断：为什么选这个文件、为什么阈值是 0.8、为什么先过滤后去重。这些判断**依赖当时的数据形态**，换一份数据可能就不成立。

任何试图把判断固化成资产的系统，都会在跨数据集复用时失败。这不是 AgentGenome 的缺陷，是"经验"这件事的性质。

所以正解不是"把意图塞进资产"，而是三件事：

**（1）把「意图的边界」做成可检查的契约**

意图不可沉淀，但"这类数据长什么样"可以。而且**这些数据已经有了**——`pipeline._samples()` 返回的 `files[]` 里已经带了 `format` / `schema.columns` / `bytes` / `truncated`，只是用完就丢进报告了。

```yaml
# manifest.json 新增 applicability 段（由 samples 自动推导，不需要人写）
applicability:
  inputs:
    - key: raw_data
      format: csv
      columns: [text, label]
      bytes: [1024, 5000000]        # 本次采样值的 1/10x ~ 10x 作为容忍区间
  environment:
    python: ">=3.10"
    requires: [pandas]              # 从脚本 import 静态扫描
  excluded: []                       # 人工补充，默认为空
```

复用前先查：`genome_run` 收到 params 后，拿新输入的 `format` / `columns` 和 `applicability` 比。不匹配就直接说：

> 这条资产要求输入含 `text`、`label` 两列，你的文件是 `content`、`tag`。要么改列名，要么用 `genome_distill` 重新沉淀。

这比"沉淀意图"便宜得多，而且是**机械可验证**的——正好补上矛盾一留下的语义缺口。

**（2）让 `llm` 节点承担运行时判断，但约束它的边界**

`llm` 节点的 prompt 就是"意图的运行时表达"。它本来就不该被机械验证（判断没有标准答案），但它的**输入输出**必须被契约约束：读哪些文件、写哪个 artifact、`verify` 命令是什么。这三样现在 `_llm_step` 已经强制要求了（`distill.py:645-658`），保持即可。

**（3）在报告里明写"未沉淀的判断"**

`distill` 已经收集了 `branches` / `scope_warnings` / `unresolved`。再加一段 `judgement_gaps`：哪些步骤是 agent 当场判断的、判断依据是什么（从 trace 的 assistant 文本里提取）。这不是为了机器验证，是为了**复用人能看见边界在哪**。

**明确反对的做法**：不要"用更强的模型把意图蒸馏出来"。那会把"确定性的错"换成"不可复现的错"，审计链直接断掉——而这套系统最值钱的部分恰恰是审计链。

---

### 7.3 矛盾三：给规则打指纹，让漂移变成可观测

这个最便宜，纯加字段 + 一个动作。

**（1）规则指纹**

```python
# agentgenome/manifest.py
def rule_set_digest() -> str:
    """The capture rules that produced this asset, as one hash."""
    root = Path(__file__).parent / "capture"
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
```

写进 `manifest.capture`：

```yaml
capture:
  traces: [...]
  engine_version: "0.6.38"        # 新增
  rule_set_digest: "a3f9c21e7b04d5e8"  # 新增
```

**（2）重验动作**

```python
# dispatch.py 新增 action
if action == "revalidate":
    # 拿历史资产的原轨迹 + 当前规则重新生成，diff 两份 graph
    return service.captures.revalidate(str(arguments["asset_id"]),
                                       str(arguments["version"]))
```

返回三种结果：

- `unchanged`：规则改了但产出一样 → 安全
- `changed`：产出变了 → 列出 diff（哪些节点/参数/连线变了），**必须人工确认才更新指纹**
- `broken`：新规则下压根生成不出来 → 这条资产已经失效，标记 `stale`

**（3）CI 里跑**

规则变更的 PR 必须对 golden fixtures 跑一遍 revalidate。如果 `changed`/`broken` 的数量 > 0，PR 描述里自动贴出"本次改动会让 N 条历史资产失效"。**改之前就知道影响面，而不是上线后被用户发现。**

这本质上是把数据库 migration 的思路搬到规则上：**schema 变更要有 migration，规则变更要有 revalidation。**

---

### 7.4 落地顺序

不能一次全做。按"投入产出比 × 风险"排序：

| 顺序 | 事项 | 工期 | 对应矛盾 | 风险 |
|---|---|---|---|---|
| 1 | 规则指纹 + `revalidate` 动作 | 2 天 | 三 | 极低（纯加字段） |
| 2 | `parse_command` 默认从"猜读"改为"报错" | 1 天 | 一（最小解） | 低 |
| 3 | `applicability` 从 samples 推导 + 复用前检查 | 1 周 | 二 | 低 |
| 4 | 回放验收升级为结构对比 | 1–2 周 | 二（补强） | 中 |
| 5 | 执行面 I/O 埋点 | 2–4 周 | 一（完整解） | 高（动 SDK 侧） |

**为什么把矛盾三放第一**：它是唯一一个"做完之后立刻能回答'这次改动安全吗'"的改动。而当前团队 9 天发 31 个版本，最缺的就是这个反馈回路。

**为什么矛盾一的最小解排第二**：一行改动，把一整类静默错误变成显式失败。在埋点落地之前，这是性价比最高的一步。

**为什么埋点排最后**：它需要动 `software-agent-sdk` 侧的执行面，跨仓库、影响所有会话，风险最高。而且前四项做完之后，"猜错"的窗口已经从"静默通过"缩到"显式报错"，埋点的紧迫性下降了——它从"救命"变成"提精度"。

---

### 7.5 一句话总结

**三个矛盾的本质是同一个缺失：系统里没有一层显式的 contract。**

- 数据流契约（observed/declared/inferred 三级来源）
- 适用范围契约（applicability）
- 生成规则契约（rule_set_digest + revalidate）

`manifest` 里已经有 `parameters`、`expected_outputs`、`capture.traces`——把这三块信息收敛成一个 `contract` 段，不需要重构，只需要把散落的东西摆到一起。

而"沉淀意图"这件事，永远做不到。能做到的是：**把意图的边界写清楚，让复用人知道自己在边界内还是边界外。** 这比假装能沉淀意图，诚实得多，也有用得多。

---

## 八、真实会话复盘：`675361cc9ae84e3ab0c4c829d5127834`

> 素材：`workspace/conversations/675361cc9ae84e3ab0c4c829d5127834`
> 轨迹：`public_data/agentgenome-traces/01a13e51-….json`（151 步）
> 任务：PCB 裸板 AVI/AOI 预打标 → Label Studio 人工复核 → 转 SFT 训练 JSONL

### 8.1 事实：14 次 distill、5 次 confirm，一次都没成功

| # | 调用 | 结果 |
|---|---|---|
| 1 | `genome_list` | 成功，看到已有资产带 `applies_when` / `input_contract` |
| 2 | `genome_trace` | 成功，108 步 |
| 3 | `genome_distill`（机械候选） | 成功，但 **151 步里排除 22 步内联代码后只剩 4 步候选** |
| 4 | `genome_distill` | 失败：`should_review: schema is false`（字段不存在） |
| 5 | `genome_distill` | 失败：4 条（args 缺输入、命令缺产出、human 提示词缺路径） |
| 6 | `genome_distill` | 成功（改用手写 `steps`） |
| 7 | `genome_confirm_distill` | 失败：缺 `final_artifact`；`field_map.json` 由 edit 写入无法固化 |
| 8 | `genome_distill` | 成功 |
| 9 | `genome_distill` | 成功 |
| 10 | `genome_confirm_distill` | 失败：第 1 步产出「没有被任何后续步骤消费」 |
| 11 | `genome_distill`（加 `keep: true`） | 成功 |
| 12 | `genome_confirm_distill` | 失败：`output_contract must include the final graph output sft` |
| 13 | `genome_distill` | 成功 |
| 14 | `genome_confirm_distill` | 失败：`params 必须覆盖链路的根输入：需要 [image_root, label_config_2, prelabel_manifest, prelabel_manifest_slice_2, prelabel_review_slice_2, records]，收到 [...]` |
| 15 | `genome_distill`（字面量改占位符） | 成功 |
| 16 | `genome_distill` | 失败：human 提示词缺 `{prelabel_review_slice}`、`{label_config}` |
| 17 | `genome_distill` | 成功，但仍有 2 条 `blocking_issues` |
| 18 | `genome_confirm_distill` | 失败：`参数不匹配：需要 [image_root, prelabel_manifest, records]，收到 ['records']` |
| 19 | `genome_distill`（补全 `validation_params`） | 成功 |
| 20 | `genome_confirm_distill` | **挂住**：`operation.started` 无对应 completed/failed，会话卡死 |

**agent 最后一段独白**（原文）：

> "Still fails with '需要 [image_root, prelabel_manifest, records]，收到 [records]'. Even though I put all three in `params`. … Why would `image_root` and `prelabel_manifest` be dropped? … That strongly implies the engine extrac—"

话没说完，会话断了。它猜了五种可能，全错。

### 8.2 根因链：三个 bug 串起来，每一个都足以致命

#### 根因 A（决定性）：`validation_params` 覆盖了完整参数，而不是合并

`dispatch.py:537-539`：

```python
replay_params=(
    dict(parsed.validation_params) if parsed.validation_params is not None
    else dict(plan.bound)
),
```

`validation_params` 的语义是「**验证回放时覆盖少数参数**」（比如把 `records` 从 10 降到 3），agent 在第 11–13 次调用里传的正是 `{"records": 3}`。

但这里用的是**替换**：整个 `plan.bound`（7 个参数）被 `{records: 3}` 覆盖，只剩 1 个。

下游 `replay.py:60-64` 要求**严格相等**：

```python
expected = {key for key, _ in package.params}
if set(params) != expected:
    raise ReplayError(f"参数不匹配：需要 {sorted(expected)}，收到 {sorted(params)}")
```

于是 3 个根输入的链路，报「需要 3 个，收到 ['records']」—— agent 看到的是「我明明传了三个」，因为**它看的是 `params`，出错的是 `validation_params`，而报错文案只提 params，从不提 validation_params**。

**修复**（`dispatch.py:537`）：

```python
replay_params=(
    {**dict(plan.bound),
     **{key: value for key, value in (parsed.validation_params or {}).items()
        if key in dict(plan.bound)}}
    if parsed.validation_params is not None
    else dict(plan.bound)
),
```

语义：`plan.bound` 是完整参数集，`validation_params` 只允许**覆盖**其中已有的 key，不允许增删。

> 注意 agent 在第 14 次调用里已经自己把 `validation_params` 补全成 3 个了——它靠猜绕过了 bug A，但代价是 4 轮对话和一次卡死。

#### 根因 B：`storage/x` 与 `x` 被当成两个文件，于是生出 `_2` 参数

第 14 次失败的报错里有三个带 `_2` 的参数：

```
需要 ['image_root', 'label_config_2', 'prelabel_manifest',
      'prelabel_manifest_slice_2', 'prelabel_review_slice_2', 'records']
```

`_2` 来自 `emit._Keys.claim()`（`emit.py:258-268`）：同名 key 第二次被申请时加后缀。

而它被申请两次，是因为**同一个文件有两种写法**：

- shell 步骤写 `storage/datasets/test_100/test_100/prelabel_manifest_slice.jsonl`（沙箱挂载别名）
- `df_submit_pipeline` 的 `input_path` 写 `datasets/test_100/test_100/prelabel_manifest_slice.jsonl`（对象存储路径）

`agentgenome/paths.py` **已经正确处理**了这件事：

```python
def canonical(value: str) -> tuple[str, str]:
    ...
    if head == _STORAGE_ALIAS:        # storage/datasets/…
        return STORAGE, rest or head  # → ("storage", "datasets/…")
    if head in _OBJECT_ROOTS:         # datasets/…
        return STORAGE, text          # → ("storage", "datasets/…")
```

两者归一化成同一个 `(namespace, path)`。**但这个函数没有被用在最关键的三个地方**：

| 位置 | 用的是什么 | 后果 |
|---|---|---|
| `trace.canonical_path()`（`trace.py:242`） | 只做占位符替换，返回原字符串 | 两种写法产生两个 key |
| `emit._Plan.file_of()` | 同上 | `linkable` 查不到，连线断 |
| `distill._check_chain` 的 `produced` / `plan_paths` | 同上 | 判定「产出没被消费」 |

`_same_path()`（`distill.py:1198`）确实调了 `paths.same`，但只用在少数几个地方——**正确的抽象存在，只是没用全**。

**修复**（`trace.py:242`）：

```python
def canonical_path(token: str, bindings: Mapping[str, str]) -> str:
    key = placeholder_key(token)
    resolved = bindings.get(key, token) if key is not None else token
    namespace, path = paths.canonical(resolved)
    return f"{namespace}:{path}"
```

把返回值改成带命名空间的稳定串，所有比较自然一致。注意这是**全局改动**，会影响 `producer_map`、`linkable`、`self.params` 的键——需要同时改 `_input_for` 里 `self.params[path] = key` 存原始 path 的地方（否则 `params` 的 value 会变成 `storage:datasets/…` 这种内部串，回放时会传给引擎）。

更小的改法（推荐先试）：只改 `emit._Plan.file_of()` 和 `_input_for` 的查表键，保留 `self.params` 存原始 path：

```python
def file_of(self, path: str) -> str:
    namespace, canonical = paths.canonical(canonical_path(path, self.bindings))
    return f"{namespace}:{canonical}"
```

**这个 bug 修掉之后，第 10 次失败（「产出没被消费」）会自动消失**，agent 当时加的 `keep: true` 反而变成多余的副作用声明，需要回退。

#### 根因 C：human 提示词的路径校验与参数化原则互相矛盾

第 16 次失败：

```
第 8 步的 human 提示词必须原样出现它声明的输入/产出路径，缺少：`{prelabel_review_slice}`、`{label_config}`
```

校验代码（`distill.py:1646-1653`）：

```python
for path in (*_path_reads(step, draft.params, draft.input_contract), *step.writes):
    if path not in step.run:
        issues.append(f"人工节点第 {step.order} 步的工作单没有写明路径 `{path}`")
```

`_path_reads()` 返回的是**展开后的具体路径**（`storage/datasets/…/prelabel_review_slice.jsonl`），而 agent 在工作单里写的是**占位符**（`{prelabel_review_slice}`）——这是参数化的正确写法。

字符串包含检查两边都不满足：

- 写占位符 → 具体路径不在文本里 → 报错
- 写死路径 → 违反「参数化、可换数据」的原则 → 换个数据集就错

**agent 无法同时满足两条规则。** 这是「规则互相矛盾」最硬的证据。

**修复**：检查时把两种写法都接受。

```python
for path in (*_path_reads(step, draft.params, draft.input_contract), *step.writes):
    key = next((k for k, v in bindings(draft.params).items()
                if paths.same(v, path)), None)
    if path not in step.run and (key is None or f"{{{key}}}" not in step.run):
        issues.append(...)
```

#### 根因 D：静态配置文件不在搬运范围

第 7 次失败：`field_map.json` 由 edit 工具写入，「内容无法机械固化」。

`emit._plan_scripts()` 只搬运 `ROLE_SCRIPT`（`.py/.sh/.js/.ts/.r/.rb/.sql`，见 `trace.py:45`）。而 `.json` / `.xml` / `.yaml` 一律不搬。

于是 agent 手写的 `field_map.json`（Label Studio 的字段绑定，12 次 edit 之一）和 `label_config.xml` 只能被声明成「输入边界」——**但它们其实是可以随包搬运的静态文件**，跟脚本没有本质区别。

**修复**：把 `_plan_scripts` 的判定从「是不是脚本」放宽到「是不是包内可以原样携带的静态资源」：

```python
_CARRY_EXTS = _SCRIPT_EXTS | {".json", ".xml", ".yaml", ".yml", ".txt", ".csv"}
```

但要加限制：只搬**小文件**（比如 < 1 MiB）且**由 write/edit 步骤产出**的，避免把数据集误搬进包里。`_RUNTIME_LIMIT` 已经是这个思路，扩展它即可。

#### 根因 E：工具入参校验报的是裸 JSON Schema

第 4 次失败：

```
Validation failed for tool "genome_distill":
  - should_review: schema is false
  - root: must not have additional properties
```

`should_review` 在整个 AgentGenome 仓库里**不存在**——是 agent 幻觉的字段。但报错说的是 JSON Schema 术语，没有告诉 agent「可用字段是哪些」。

**修复**：在 schema 校验失败时追加一行可用字段清单。改动在 `pi-extension` 的工具注册处（或 `dispatch.invoke` 的入口）：

```
unknown field `should_review`. Available: label, asset_id, steps, params,
validation_params, input_contract, output_contract, final_artifact, …
```

#### 根因 F：operation 挂住没有超时兜底

最后一条事件是 `operation.started`（`genome_confirm_distill`, plan `ae14b06c`），`inflight.json` 仍指向它，**没有 completed、没有 failed、没有 error**。

这与本仓库今天 16:09 诊断的「模型流中途断了、界面无限等」是同一类问题：**runtime 里没有任何 stall / idle timeout**。

对沉淀这个场景，后果尤其严重——用户看到的是「沉淀又失败了」，实际是「根本没返回」。

### 8.3 对我前几节判断的修正

必须诚实修正两处：

**修正一：当前的主要矛盾不是「验证太弱」，是「构建阶段根本收敛不了」。**

我在 §三 把 P0-3（回放只验非空）排在很前面。但这条会话**一次都没走到回放**——它卡在 `confirm_distill` 的构建阶段，连包都没冻出来。

所以对「反复沉淀失败」这个具体症状，优先级应该重排：

| 我原来的排序 | 实际优先级 | 理由 |
|---|---|---|
| P0-1 词法解析 | 降到 P1 | 这条会话的失败与解析无关 |
| P0-3 验证只证形状 | 保持 P0，但不解当前症状 | 到不了回放 |
| —— | **新增 P0：参数合并 bug（根因 A）** | 直接导致最后 4 轮失败 |
| —— | **新增 P0：路径未归一化（根因 B）** | 直接导致 `_2` 参数与「产出没被消费」 |
| —— | **新增 P0：human 校验自相矛盾（根因 C）** | agent 无法满足，必然死循环 |

**修正二：`applicability` 契约不是「缺失」，是「已有但没被用」**

我在 §七 建议「新增 `manifest.applicability`」。但 `genome_list` 的返回里已经有：

```json
"applies_when": [...],
"not_applies_when": [...],
"input_contract": {"input_csv": {"format": "csv", "required_fields": ["qid", "question_text"]}}
```

**矛盾二的一半已经解决了**，而且比我在 §七 设计的更贴近用户语言（`applies_when` 是自然语言）。缺的是**复用前没有拿它做检查**——`genome_run` 不会比对输入 schema。

所以 §七 的建议应该改成：不新增字段，而是在 `genome_run` 的 `validate_params` 里加一道 `input_contract` 比对。

### 8.4 修复顺序（针对「反复沉淀失败」）

按「修完立刻能让这条会话跑通」排序：

| 顺序 | 修复 | 位置 | 改动量 | 效果 |
|---|---|---|---|---|
| 1 | 参数合并而非覆盖（根因 A） | `dispatch.py:537` | 6 行 | 解除最后 4 轮的死循环 |
| 2 | human 校验接受占位符（根因 C） | `distill.py:1646` | 8 行 | 解除「怎么写都错」 |
| 3 | 路径归一化（根因 B） | `emit.py:_Plan.file_of` + `_input_for` | 15 行 | 消除 `_2`、消除「产出没被消费」 |
| 4 | 静态文件搬运（根因 D） | `trace.py:_CARRY_EXTS` + `emit._plan_scripts` | 20 行 | 让 `field_map.json` 能进包 |
| 5 | schema 报错加可用字段（根因 E） | 工具注册处 | 10 行 | 减少一轮无效调用 |
| 6 | operation 超时（根因 F） | `pyromind_runtime` / `pi-runtime` | 30 行 | 卡死变失败，至少能重试 |

**1–3 修完，这条会话应该能跑通。** 4 是让沉淀质量变好，5–6 是体验。

**一个陷阱**：修完根因 B 之后，agent 在第 11 次调用里加的 `keep: true` 会变成「多余的副作用声明」。回放时这一步会被强制保留，即使它的产出确实被消费了。建议在修复根因 B 的同时，把 `_check_unconsumed` 的报错文案改成先提示「检查是否路径写法不一致」，而不是直接建议加 `keep`——**因为 `keep` 是治标的，加错了还会掩盖真问题**。

### 8.5 `keep` 机制评估：必要，但被用错了

**代码事实**（`distill.py:593-598`）：

```python
for index, step in enumerate(planned):
    if step.keep: continue
    writes = _data_paths(step.writes, given, writing=True)
    if not writes or writes & handover or (output is not None and output in writes):
        continue
```

`if not writes: continue` —— **能触发 `keep` 建议的步骤，一定有产出。**

所以 `keep` 的真实语义不是文案里说的「提交任务、建项目这类有副作用的调用」（那类调用通常有 receipt / project_id 输出，且被下游消费，根本不会触发），而是：

> **「这一步有产出，但产出不在最终产物的依赖闭包里。」**

文案与实现不一致，是 agent 误解 `keep` 的第一层原因。

**必要性：机制必要，形态有害**

必要性成立：纯数据流 DAG 无法表达「执行即目的」的步骤。若没有 `keep`，只能伪造一个下游消费者（比如让最终 verify 去读它），更脏。

但当前形态有三个问题：

1. **命名误导**。`keep` 读作「保留」，实际含义是「产出不在闭包里但执行是链路的一部分」。二者差别很大，agent 会理解成「凡副作用都加」。
2. **零证据要求**。对比：输入边界必须配 `params`（`_check_chain`），人工交接必须有「用户说产出了 X」的证词（`_handover_steps`）。而 `keep: true` 不需要任何理由，是个自由开关。
3. **报错把它列为第一建议**。`_check_unconsumed` 的三条建议里，「加 `keep: true`」排第一。**逃生阀优先的文案，agent 一定选最省事的那条。**

**这次会话的证据：两个 `keep` 都是错的**

| 步骤 | agent 的处理 | 实际情况 |
|---|---|---|
| step1 `make_slice_manifest.py` | 第 11 次加 `keep: true` | 它的产出**确实被 step2 消费**，只是写法不同（`storage/x` vs `x`）。这是根因 B，不该 keep |
| step2 `df_submit_pipeline` | 更早就加了 `keep: true` | 产出 `out/receipts/df_submit.json` **被 step3 `wait_pipeline.py` 读**。有输出有消费，完全不需要 keep |

`keep` 被当成了「不确定时的保险」。它一旦泛滥，就失去了信号价值——之后没人能从 manifest 看出哪一步是真副作用。

**根因：`_data_paths` 也没做路径归一化**

```python
return {path for path in (canonical_path(item, given) for item in paths) ...}
```

`canonical_path` 只做占位符替换。于是：

- step1 writes = `{"storage/datasets/…/prelabel_manifest_slice.jsonl"}`
- step2 reads = `{"datasets/…/prelabel_manifest_slice.jsonl"}`
- 交集为空 → 判定「没被消费」→ 报错 → 建议 keep → agent 加 keep → **根因 B 被永久掩盖**

**建议：收缩 `keep`，用「多输出」替代大部分场景**

`keep` 应该只保留给「纯副作用、产出对外无意义」的步骤。**凡是产出有意义的，应该走多输出**：

```yaml
# manifest.json —— 让「建项目产生的 project_id」成为资产对外可见的输出
expected_outputs:
  sft:  {node: to-sft,  key: sft}
  project_id: {node: create-project, key: project_id}
```

这样复用方能看到「这条资产会产出一个 Label Studio 项目」，而不是一个不透明的「保留步骤」。

配套三处改动：

1. `_check_unconsumed` 的文案顺序改为：**先查路径写法是否一致** → 再问「产出是否该声明成资产输出」→ 最后才是 `keep`。
2. `keep` 从 `bool` 改成理由字符串（`keep: "建 Label Studio 项目，人工在那里复核"`），向后兼容 `true`。理由写进 manifest 与报告。
3. 加一条检查：`keep` 的步骤若其产出**被任何步骤以任一种写法读过**，报「这一步其实在数据流里，不该 keep」。

第 3 条修完，根因 B 和 `keep` 滥用会一起消失。

### 8.6 回放失败复盘：`artifact.at` 被一个防御性判断挡掉了

回放确实跑起来了（run `3f57427f…`），9 个节点走到第 2 个就失败：

```
node_failed  pcb-avi-aoi-sft.df-submit-pipeline
提交前预检失败：清单 `.../workflow-runs/3f57427f…/artifacts/pcb-avi-aoi-sft--make-slice-manifest.out`
中相对图片路径找不到文件（例如 1-0805Y/1/B0.bmp、1-0805Y/1/B0_cam.bmp、1-0805Y/1/B1.bmp）
```

**这是四层因果链，每一层单独看都"修好了"，组合起来仍然失败。**

#### 第 1 层：路径未归一化 → 生出 `_2` 参数（根因 B）

`storage/datasets/x` 与 `datasets/x` 是同一文件两种写法，`canonical_path` 只做占位符替换、不做归一化，于是 `_Keys.claim()` 生出 `prelabel_manifest_slice_2`。

#### 第 2 层：agent 为消除 `_2`，把中间产物改成了占位符

第 14 次失败的报错是「需要 `prelabel_manifest_slice_2`」。agent 的应对是把字面量 `storage/datasets/…/prelabel_manifest_slice.jsonl` 改成 `{prelabel_manifest_slice}` —— 因为占位符有 `params` 绑定，就不会被 claim 成 `_2`。

**它是被第 1 层逼的。** 而中间产物本来就不该参数化（参数化是为了换输入，中间产物由上游节点决定）。

#### 第 3 层：`_artifact_at` 看到 `{}` 就放弃 pin 落点（真正的阻塞）

`emit.py:654-676`：

```python
def _artifact_at(self, step: Step, order: int, primary: str) -> str | None:
    """Pin a run step's product next to the input it is derived from.

    A product can carry references to its neighbours -- a manifest naming sibling
    images -- and those references only resolve where the file already lives.
    """
    if step.kind != KIND_SHELL:
        return None
    product = PurePosixPath(self.file_of(primary))
    if "{" in product.name or "}" in product.name:
        return None          # ← 占位符产物直接放弃
```

`primary` 是 `{prelabel_manifest_slice}`，`file_of()` 在 `bindings` 里查不到这个 key，原样返回，名字含 `{` → **`return None`**。

于是 `graph.yaml` 里 `make-slice-manifest` 节点**没有 `artifact.at`**：

```yaml
- id: make-slice-manifest
  do:
    run: python3 scripts/make_slice_manifest.py {input.prelabel_manifest} {artifact} {input.records}
  output:
    prelabel_manifest_slice:
      type: path
  verify:
  - run: test -s {output.prelabel_manifest_slice}
```

产物落到默认位置 `workflow-runs/<run>/artifacts/pcb-avi-aoi-sft--make-slice-manifest.out`，而图片仍在 `storage/datasets/test_100/test_100/` 下。manifest 里的相对路径 `1-0805Y/1/B0.bmp` 按「相对 manifest 自身」解析，自然找不到。

**讽刺的是：`_artifact_at` 的 docstring 描述的正是这个场景（"a manifest naming sibling images"）。机制存在，注释准确，但被一个防御性判断挡在门外。**

#### 第 4 层：预检报的是症状，不是原因

`_MANIFEST_PRECHECK`（`agentgenome_bridge.py`）**是对的**——它精确拦住了「manifest 与图片分离」。但它说的是「图片找不到」，agent 会去查图片路径是否存在，而不会想到「manifest 落点错了」。

于是 agent 反复改 `steps` 里的写法，改不到 `emit._artifact_at`。**这就是「反复修复几轮还是这样」的机制：报错指向症状，阻塞点在四层之下的 emit 里。**

#### 为什么两个修复方向互斥

| agent 的写法 | `_check_unconsumed` | `_artifact_at` | 结果 |
|---|---|---|---|
| 字面量 `storage/datasets/…/slice.jsonl` | 路径不归一化 → 误报「没被消费」 | 名字不含 `{}` → **能 pin 落点** | 卡在构建阶段 |
| 占位符 `{prelabel_manifest_slice}` | 通过（有绑定） | 名字含 `{}` → **放弃 pin** | 卡在回放阶段 |

**两条路各堵一头。** agent 在两者之间来回撞，这就是「修了几轮还是这样」的全部原因。

#### 修复

**修复 1（治本）：中间产物禁止参数化**

在 `distill()` 的校验里加一条：`writes` 里的占位符若其值不是 `params` 里的根输入，报错。

```python
for step in planned:
    for path in step.writes:
        key = placeholder_key(path)
        if key is not None and key not in bindings(params):
            problems.append(
                f"第 {step.order} 步的产出 `{path}` 是链路中间产物，不应该写成占位符。"
                "中间产物的路径由上游节点决定；写成占位符会让 emit 无法确定落点"
                "（artifact.at），导致依赖同目录文件的产物在回放时失效。请写成具体路径。")
```

**修复 2（兜底）：`_artifact_at` 容忍占位符产物**

即使 agent 用了占位符，也应该 pin 目录——文件名不重要，重要的是 manifest 落在图片旁边。

```python
product = PurePosixPath(self.file_of(primary))
name = product.name
if "{" in name or "}" in name:
    key = placeholder_key(primary)
    # 占位符产物没有字面文件名：用参数名 + 产物类型推断的扩展名
    name = f"{key}{artifact_ext(primary) or '.out'}"
```

**修复 3：预检报错补一句落点提示**

`_MANIFEST_PRECHECK` 失败时追加：

```
该清单由节点 `make-slice-manifest` 产出，落在 `<run>/artifacts/`；
若它依赖同目录的图片，这个节点需要 artifact.at 指回源清单所在目录。
```

让 agent 一次就能定位到 emit，而不是去查图片。

**修复顺序**：先修 1（阻断错误写法）→ 再修 2（兜底）→ 最后修 3（改善可诊断性）。只修 2 不修 1，agent 会继续用占位符，`artifact.at` 依赖推断文件名，脆弱。

### 8.7 第一轮修复实施记录（2026-10-21 17:00）

针对 §8.2 / §8.6 的根因，已落地 5 处改动。`tests/` 全量 **391 passed**（改动前 capture 242 passed）。

| # | 文件 | 改动 | 对应根因 |
|---|---|---|---|
| 1 | `dispatch.py` | 新增 `_replay_params()`：`validation_params` 从**替换**改为**覆盖**，只允许覆盖 `plan.bound` 已有的 key | A |
| 2 | `capture/emit.py` | `_Plan.file_of()` 走 `paths.display()`，`storage/x` 与 `x` 归一成同一串 | B |
| 3 | `capture/emit.py` | 新增 `_pin_token_artifact()`：`{token}` 产物用 token 名 + 首个可解析输入的目录/后缀 pin 落点，不再直接 `return None` | 第 3 层 |
| 4 | `capture/distill.py` | 新增 `_intermediate_token_issues()`：`run` 节点的中间产物禁止写成占位符 | 第 2 层 |
| 5 | `capture/distill.py` | `_data_paths()` / `_check_chain()` 归一化；`capture/slice.py` 的 `producer_map()` / `reverse_slice()` 同步 | B |

**验证结果**

```
路径归一化：storage/datasets/…/slice.jsonl 与 datasets/…/slice.jsonl
  → 都归一成 {'storage/datasets/…/slice.jsonl'}，相等 True

中间产物校验（复现本次会话 step1）：
  第 1 步产出 {prelabel_manifest_slice} + 被 step2 读 + params 里没有
  → 拦截 1 条，提示「不该写成占位符」
  对照组（token 在 params 里）→ 拦截 0 条
```

**关键取舍**：`_intermediate_token_issues` 只检查 `kind == "run"`。`human` / `llm` 节点的产出**本来就应该**用占位符（由人或 agent 决定写在哪），第一版没排除它们，导致 6 个 human-gate 测试失败，已收窄。

**尚未实施**（下一轮）

- 静态文件搬运（根因 D，`field_map.json` 仍只能声明成输入边界）
- 工具 schema 报错加可用字段（根因 E）
- operation 超时兜底（根因 F）
- 为 #3 和 #4 补回归测试：目前靠 391 个既有测试兜底，但没有一个用例直接覆盖「占位符产物 pin 落点」

### 8.8 全部事项总表（截至 2026-10-21 17:05）

把 §三、§五、§七、§8.4、§8.6 和 keep 评估里提过的事项去重合并，共 **29 项**，已完成 **5 项**。
本节取代 §五 的排序（§五 是纯静态分析的产物，§8 的真实故障推翻了它一半的判断）。

#### A. 数据流与路径（5 项，已做 3）

| # | 事项 | 状态 | 量级 |
|---|---|---|---|
| 1 | 路径归一化：`emit._Plan.file_of` / `distill._data_paths` / `_check_chain` / `slice.producer_map` 走 `paths.display` | ✅ 已做 | 15 行 |
| 2 | `artifact.at` 容忍占位符产物（新增 `_pin_token_artifact`） | ✅ 已做 | 18 行 |
| 3 | `run` 节点中间产物禁止写成占位符（新增 `_intermediate_token_issues`） | ✅ 已做 | 28 行 |
| 4 | `parse_command` 遇到无法判定方向的 token 时，从「默认当读」改为记 `unresolved` 并报错 | ❌ 未做 | 1 天 |
| 5 | 执行面 I/O 埋点（沙箱 inotify / snapshot diff），`parse_command` 降级为辅助 | ❌ 未做 | 2–4 周 |

#### B. 参数与契约（4 项，已做 2）

| # | 事项 | 状态 | 量级 |
|---|---|---|---|
| 6 | `validation_params` 从替换改为覆盖（新增 `_replay_params`） | ✅ 已做 | 12 行 |
| 7 | human 工作单校验同时接受具体路径与 `{占位符}` 写法 | ✅ 已做 | 14 行 |
| 8 | `applicability`：`genome_run` 复用前比对 `input_contract`（字段已有，只是不用） | ❌ 未做 | 1 周 |
| 9 | 沉淀报告加 `judgement_gaps`：哪些判断是 agent 当场做的、依据是什么 | ❌ 未做 | 2 天 |

#### C. keep 与副作用（3 项，已做 0）

| # | 事项 | 状态 | 量级 |
|---|---|---|---|
| 10 | `keep` 收缩为「纯副作用、无对外产出」的最后手段 | ❌ 未做 | 3 天 |
| 11 | 有产出的副作用改走多输出（`manifest.expected_outputs` 加项） | ❌ 未做 | 3 天 |
| 12 | `_check_unconsumed` 报错顺序改为：先查路径写法 → 再问是否该多输出 → 最后才提 keep | ❌ 未做 | 10 行 |

#### D. 可诊断性（3 项，已做 0）

| # | 事项 | 状态 | 量级 |
|---|---|---|---|
| 13 | 工具入参校验失败时追加可用字段清单（现在报裸 JSON Schema） | ❌ 未做 | 10 行 |
| 14 | `_MANIFEST_PRECHECK` 失败时提示「产物落点可能错了」 | ❌ 未做 | 10 行 |
| 15 | operation 挂住加 idle timeout（现在 `operation.started` 后无兜底） | ❌ 未做 | 30 行 |

#### E. 规则治理（4 项，已做 0）

| # | 事项 | 状态 | 量级 |
|---|---|---|---|
| 16 | 18 个 `_check_*` 收敛为 4 条不变量（闭合性/可回放性/可固化性/参数一致性） | ❌ 未做 | 1–2 周 |
| 17 | `manifest.capture` 加 `engine_version` + `rule_set_digest` | ❌ 未做 | 10 行 |
| 18 | 新增 `genome_revalidate` 动作（原轨迹 + 当前规则重新生成，diff 两份 graph） | ❌ 未做 | 2 天 |
| 19 | CI 挂规则变更重验，PR 里列出「会让 N 条历史资产失效」 | ❌ 未做 | 2 天 |

#### F. 验证强度（4 项，已做 0）

| # | 事项 | 状态 | 量级 |
|---|---|---|---|
| 20 | 回放 A 验收从 `test -s` 升级为「与原产物结构对比」（行数/字段/分布） | ❌ 未做 | 1–2 周 |
| 21 | 含 human 的链路：回放 B 停在人工节点后用首次人工产物续跑 | ❌ 未做 | 1 周 |
| 22 | 3 条真实轨迹做 golden fixture（现在测试全是合成脚本） | ❌ 未做 | 2 天 |
| 23 | 给 #2 和 #3 补直接覆盖的回归测试 | ❌ 未做 | 半天 |

#### G. 代码结构（6 项，已做 0）

| # | 事项 | 状态 | 量级 |
|---|---|---|---|
| 24 | `align_traces` 前按 `trace_id` 排序（消除顺序敏感） | ❌ 未做 | 3 行 |
| 25 | `_check_chain` 复用 `slice.producer_map`（现在两处定义相反） | ❌ 未做 | 15 行 |
| 26 | `CapturePlan.payload()` 加 `status` 字段并置顶 | ❌ 未做 | 5 行 |
| 27 | 删 `prepare_distill` 别名；`_check_replay_literals` 委托 `pipeline` 的实现 | ❌ 未做 | 删 30 行 |
| 28 | `_Plan` 拆成 `NodeNamer` / `OutputPlanner` / `NodeBuilder` | ❌ 未做 | 3 天 |
| 29 | `_HUMAN_HANDOFF` / `_MUTATING_TOOL` 等正则降级为「候选提示」，不参与阻断 | ❌ 未做 | 3 天 |

#### 统计与建议

| 分组 | 项数 | 已做 |
|---|---|---|
| A 数据流与路径 | 5 | 3 |
| B 参数与契约 | 4 | 2 |
| C keep 与副作用 | 3 | 0 |
| D 可诊断性 | 3 | 0 |
| E 规则治理 | 4 | 0 |
| F 验证强度 | 4 | 0 |
| G 代码结构 | 6 | 0 |
| **合计** | **29** | **5（17%）** |

**已做的 5 项全部是「救火」**：让 `675361cc…` 这条会话能跑通的最小集。§五 的优化路线、§七 的三个矛盾解法，一项都没动。

**下一步建议（按「立刻降低复发概率」排序）**

1. #23 补回归测试（半天）——已做的 #2/#3 目前零覆盖，391 个既有用例只能证明没破坏旧行为
2. #24 排序 + #12 报错顺序 + #13 schema 报错（各 3–10 行，合计 1 天，全部零风险）
3. #4 `parse_command` 默认改报错（1 天）——把「静默猜错」变成「显式失败」，这是本次会话所有路径问题的总闸
4. 重跑 `675361cc…` 验证 A 组 5 项，通过后再进 C/E/F

### 8.9 模板化路径：真正的根因（2026-10-21 17:33 已修）

会话更新到 10 次 `genome_distill` 后，报错变了，露出真正的根因。

#### 一个缺口

agent 在 steps 里写了：

```json
{"output": "storage/.../prelabel_manifest_{records}.jsonl",
 "run": "head -n {records} {manifest} > .../prelabel_manifest_{records}.jsonl"}
```

这是**模板化路径**：`{records}` 嵌在文件名中间。而系统的占位符正则是：

```python
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
#                                                          ^^ 必须占满整个 token
```

`\Z` 要求占位符占满整个 token，所以这个路径被当成**字面文件名**。

#### 五个症状

| 症状 | 机制 |
|---|---|
| 参数名 `prelabel_manifest_records_2` | `_key_base` 把 `{records}` 也当名字的一部分；两条这种路径各自 claim → `_2` |
| `params 必须覆盖根输入：需要 5 个，收到 3 个` | 那两个怪参数不在 params 里，agent 根本不知道系统造了它们 |
| `_artifact_at` 放弃 pin | 产物名含 `{}` → `return None` → manifest 落到 run 目录 |
| 回放预检「相对图片路径找不到」 | 上一条的直接后果 |
| human 提示词「必须原样出现路径」 | 比对字面路径，agent 写的是展开值 |

#### 修复（3 处）

| # | 位置 | 改动 |
|---|---|---|
| 1 | `distill.py` 新增 `_TEMPLATE_TOKEN` + `_template_path_issues()` | 检测「含 `{}` 但不是完整占位符」，在 `distill()` 的 problems 里报错，给出两种改法 |
| 2 | `distill.py:_tool_io()` | `token in value` 子串比较改为 `paths.same()`，否则第 2 步的 input 声明会先被误报「args 里找不到」 |
| 3 | 报错文案 | 明确「目录也不要留在命令里」——`storage/x/{name}` 同样不合法，因为占位符仍没占满整个路径 |

#### 验证

```
原始 steps：4 条模板路径
按建议改完（整条路径换成 {slice_manifest} / {review_jsonl}，{trace} 写死）：0 条
tests/ 全量：395 passed
```

#### 教训

**报错从不说「我不支持路径里嵌占位符」。** 它说「参数不匹配」「产出没被消费」「提示词缺路径」——每条都指向不同方向，所以 agent 在症状层调参、人在症状层修 bug，五轮都没触到根。

这也修正了 §8.6 的定性：路径 identity 未统一只解释了 2 次失败；**真正的 5 次失败来自「系统缺少一种 agent 很自然会用、但系统不支持的写法，而且不支持时不给提示」**。

#### 遗留

`storage/x/{name}`（占位符占满最后一段但前面有目录）目前仍不合法。支持它需要改 `placeholder_key` 的语义 + `emit` 的 token 替换（保留目录前缀），属于架构改动，记入 §8.8 的 G 组。

---

## 附录：本次评审的证据索引

| 结论 | 文件:行 |
|---|---|
| flag 白名单 | `agentgenome/capture/trace.py:56-87` |
| 路径判定启发式 | `agentgenome/capture/trace.py:226-233` |
| 中文正则判人工 | `agentgenome/capture/plan.py:38-56` |
| 默认验收 `test -s` | `agentgenome/capture/emit.py:722-731` |
| baseline 字节断言 | `agentgenome/capture/baseline.py:45-62` |
| human 跳过回放 B | `agentgenome/capture/pipeline.py:183-184` |
| producer 后者胜 | `agentgenome/capture/slice.py:48-62` |
| producer 先者胜 | `agentgenome/capture/distill.py:374-382` |
| 18 个校验函数 | `agentgenome/capture/distill.py`（见 §三 P1-2 列表） |
| 规则版本化痕迹 | `docs/v2-capture.md:366-650` |
| 渐进式对齐顺序敏感 | `agentgenome/capture/align.py:1-5` |
| 异步返回语义 | `agentgenome/capture/pipeline.py:8-9` |
| 合成测试 fixture | `tests/capture/conftest.py:1-60` |
| 31 个插件版本 | `pi-extension/*.tgz`（0.5.0–0.6.38） |
