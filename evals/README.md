# Agent 评测数据集

本目录保存可版本管理的 Agent 评测规范。本阶段只定义输入和确定性期望，不连接真实 LLM、Tavily、RAGFlow 或 MySQL。

## 数据约定

- `required_services` 表示在线运行该用例时必须启用的服务，不代表加载或校验 YAML 时会访问服务。
- `required_test_data` 描述尚待准备的 RAGFlow 助手、知识库或 MySQL 只读测试数据。
- `tools` 检查工具集合、禁止调用、调用顺序、参数子集和调用次数。
- `answer` 只检查稳定的关键词、正则和最低长度，不比较完整 LLM 文本。
- `files` 检查 Agent 最终生成的文件扩展名。
- `expect_error` 表示运行器本身是否应以错误结束；业务拒绝通常仍是成功回答，因此保持 `false`。

确定性评分器返回每项规则的通过状态、说明、总分和整体是否通过。`runner.py` 负责从 LangChain/DeepAgents 标准事件中收集工具、子 Agent、最终回答、错误和运行期间新增文件，并转换为 `AgentRunResult`。

`fixtures/offline_traces.yaml` 为全部 24 个用例提供可重复的参考轨迹，用于在 CI 中验证采集和评分管道。参考轨迹由 Fake Agent/Fake Model 驱动，不代表真实模型的路由或回答质量；真实能力只能通过后续在线评测得出。

运行数据集和评分器测试：

```bash
uv run pytest -m eval --no-cov
```

真实服务评测不会加入默认离线 CI。进入在线评测前，需要先确认对应 `required_services` 和 `required_test_data` 已准备完成。

运行全部离线参考轨迹并生成 JSON 汇总报告：

```bash
uv run python -m evals.offline
```

默认报告写入 `eval-results/offline-report.json`。默认 CI 也会执行该命令，并将结果保存为 `backend-test-reports` 构件中的 `offline-eval.json`。该结果只证明离线轨迹管道和规则一致，不能作为真实模型质量分数。

## 真实服务评测

`live_datasets/` 是与当前测试环境匹配的真实评测 profile。真实评测必须显式运行，不属于默认 `pytest` 或 PR CI：

```bash
uv run python -m evals.live --preflight-only
uv run python -m evals.live --repeats 1
```

程序会先检查 LLM、Tavily、RAGFlow 和只读 MySQL；任何一项失败都会以退出码 2 跳过正式运行。每个用例具有独立目录和超时，并记录成功率、规则得分、耗时、Token 以及按类别结果。配置 `LIVE_EVAL_INPUT_USD_PER_MILLION` 和 `LIVE_EVAL_OUTPUT_USD_PER_MILLION` 后还会估算成本。

原始报告写入被 Git 忽略的 `eval-results/`。要建立或对比只含汇总指标的基线：

```bash
uv run python -m evals.live --save-baseline eval-results/live-baseline.json
uv run python -m evals.live --baseline evals/baselines/live-baseline.json
```

GitHub Actions 的 `Live Agent evaluations` 只能通过 `workflow_dispatch` 手动触发；需在 `live-evals` Environment 中配置 Secrets。若 MySQL 或 RAGFlow 不允许 GitHub 托管 Runner 访问，应只在本地运行，或改用处于同一网络的自托管 Runner。
