# 在 SDK 的 Pi 中复用固定脚本

AgentGenome 提供资产、版本、图执行和 Pi 工具；宿主提供执行环境。插件不创建沙箱、不保存平台凭据，也不另起 Pi worker。

```text
Pi → genome_* 工具 → SDK Runtime → AgentGenome
                                   ↓ 命令与产物请求
                              SDK 执行接口 → 当前会话沙箱
```

## 快速验证

在 AgentGenome 目录执行：

```bash
./start_sdk.sh test   # 安装依赖，跑两批 CSV，验收并发布示例包
./start_sdk.sh start  # 启用插件，启动 SDK 服务供聊天测试
```

`test` 不调用模型、不需要 API Key，使用 SDK 的实际 Pi OS 沙箱；`start` 沿用 SDK 原有的模型和平台配置。默认 SDK 位于相邻的 `software-agent-sdk`，可通过 `SOFTWARE_AGENT_SDK_DIR` 修改。重复测试可设置 `AGENTGENOME_SKIP_INSTALL=1` 跳过依赖安装。

两者共享 SDK 的 `workspace/agentgenome` 资产目录，也可用 `AGENTGENOME_HOME` 指定。测试前先停止占用该目录的 SDK。预期两批均输出 `succeeded`，最终输出 `published`；这不是独立的 AgentGenome 服务。

SDK 中的等价命令是 `./start_inference.sh --test-agentgenome` 和 `./start_inference.sh --agentgenome`。源码修改后须重新构建 SDK 锁定的 wheel/npm 包，命令见 SDK 的 `docs/agentgenome-v1.md`；启动脚本不会直接导入跨仓库源码。

## 第一版接口

`createGenomeExtension(host)` 注册 `genome_list/get/run/status/cancel`。`host.invoke(action, arguments, callId, signal)` 由 SDK 注入。启动返回运行 ID，不等待脚本完成。

Python `GenomeService` 管理共享资产与运行；`Host.prepare(...)` 返回执行端口和解析后的参数。`Ports.shell` 执行命令，`Ports.artifacts` 提供产物路径、摘要和小型验收结果。默认文件端口保留现有 CLI 行为。SDK 通过已安装的 Python wheel 和 npm 包接入，不需要引用此仓库的源码目录。

`manifest.json` 定义 `id/version/name/description/parameters/outputs`，参数类型支持 `path/string/number/boolean`。第一版参数必须全部显式传入，不能包含额外字段；path 由宿主解析。不支持用参数携带平台登录凭据。

## 手工导入与发布

```bash
agentgenome-assets --home /path/to/assets-root import templates/data-cleaning
agentgenome-assets --home /path/to/assets-root validate data-cleaning 1.0.0 \
  --params '{"data_file":"/path/to/test.csv"}'
agentgenome-assets --home /path/to/assets-root publish data-cleaning 1.0.0
```

本地 `validate` 用于包开发。SDK 接入时优先用 SDK 的 `scripts/validate_agentgenome.py`，它会使用 SDK 实际的 Pi OS 沙箱完成两批测试后发布。

公共包位于 `assets/<id>/<version>/`，不能覆盖同一版本。`latest` 指同一资产最后一次发布的版本；重复发布不改变顺序。只有成功验收过的版本才允许发布。包摘要在导入、启动和准备执行后校验，运行固定到具体版本。包作者负责脚本适用场景与验收标准。

SQLite 记录资产索引、运行摘要和请求回执；`runs/<id>/checkpoint.json`、`ledger.jsonl` 保留图内证据，`execution.log` 保存宿主传回的完整命令输出。业务数据和脚本产物留在宿主工作区，产物引用包含执行路径、大小和摘要。

## 边界

- 仅开放固定脚本顺序流程，支持已有 retry、脚本验收和 metric；需要 LLM、人工或动态选路的图在导入时拒绝。
- 每个会话最多一个活动图；重复请求返回同一运行，重复请求 ID 配不同参数会报错。
- 一份 SQLite 同时只能由一个 SDK 执行宿主持有；资产可统一管理，运行按会话隔离。当前支持 Linux/macOS。
- 取消只有在宿主确认命令停止后才标记 `cancelled`。连接中断、取消未确认等情况标记 `interrupted`，不自动重放。再次运行前须确认外部命令和副作用。
- 重启后可查历史，但不自动续跑。Pi 节点、脚本修复、轨迹提炼和原生 Pi 独立安装留给后续版本。
