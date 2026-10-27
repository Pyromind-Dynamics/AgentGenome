# 原生 await 真实任务样例

需要 Pi 1.1.0。显式同时加载 AgentGenome 插件和本目录的 `demo-extension.mjs`。
示例工具会创建指定的新目录、启动一个独立 Node 进程，立即返回 task_id/output_dir；
约四秒后才生成 report.json 和 processed.jsonl。不会访问远程服务。

先让 Agent 调用一次 genome_list，建立当前会话的真实工具上下文，然后：

```text
/genome import /绝对路径/templates/native-await-check
/genome validate native-await-check 1.0.0 {"output_dir":"/绝对路径/尚不存在的测试目录"}
```

应先看到外部等待提示，随后自动 succeeded，无需 genome_resume。
最终产物为 `{"actual":true}` 加换行；submit-count.txt 必须只有一个 x。
不要把工具返回的受理单当最终产物。取消只停止工作流等待，示例后台进程仍会完成。

当前适配器仅支持本地 output_dir/report.json（status 为 succeeded/failed/cancelled/error）
与 processed.jsonl；不等同于通用远程任务查询/回调。
若服务或 Pi 重启，重新打开原会话；需要工具的图须先通过 genome_list 绑定新上下文。
原始 deadline 不延长。自动探测不调用模型、不运行 shell 轮询脚本。
