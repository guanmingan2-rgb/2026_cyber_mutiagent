# 正式实验架构审计

审计日期：2026-11-13（Asia/Shanghai）。本轮仅阅读源码、历史记录及查询 Slurm，未派发作业、调用模型或执行评分。文档中“实现存在”不等于“正式实验已验证”。

路径约定：`ROOT=/home/ybgao/2026_cyber_agents`，`PROJECT=/home/ybgao/2026_cyber_mutiagent`，`RUN=$ROOT/safegym_solver/runs/unified-migration-20261113T032648Z-ca5b9e`。这些是本文缩写，不是程序自带环境变量。

## 实际调用关系

```text
benchmark_runner/run.py main()
  ├─ load_benchmark_config / check_profile / verify_evidence
  ├─ check → cybergym/entry.py freeze()
  │           → sbatch --wait check.sbatch → migration_check.py
  │           → 官方 Task Loader + 本地模型/真实 SDK 工具隔离测试
  └─ submit --allow-external-api → cybergym/entry.py main(submit)
              → 每个 run-plan.attempt 独立 sbatch --parsable task.sbatch
                  → storage_guard / production.py verify
                  → rootless Docker / binary runner / firewall
                  → harness.py serve（本作业内部启动）
                  → production.py task --number N --score-port PORT
                      → harness.make_task → generate_task(level1)
                      → TaskScoreGateway
                      → agent_harness.py run → DeepSeek SDK + 原生 Bash
                      → Agent 经网关提交 /submit-vul（可多次）
                      → SDK 退出、网关排空、冻结 Agent 最终选择
                      → admin_finalize → /submit-fix → /query-poc
                      → official_success → task-result.json
```

调度器等待的是 `sbatch` 提交返回，不等待正式任务完成；因此派发异步。单个作业内部，Agent 与可用评分服务同时存在，Agent 的评分请求获得同步反馈；最终 fixed 评分发生在 Agent 退出和网关关闭之后。不能将其描述成“全部评分都等 Agent 完成后才开始”。

## 隔离和输入

正式 profile 为 `$ROOT/agent_runtime/profiles/deepseek-harness.json`，通过 shared_profile 继承模型参数，声明原生 `bash`。`agent_harness.py` 与 `agent_runtime/deepseek_tool_runner.py` 使用现有工具根、依赖快照及 mount/PID/network namespace。SDK/工具与官方目标 runner 分离；官方 runner 是作业内 rootless Docker 容器。Slurm/cgroup 为作业提供资源边界。

Agent 工作区来自本轮冻结的 Level 1 描述和未修复源码。任务提示词从现有 `run_multi_harness_task.py` 的 TASK_PROMPT 冻结为 `frozen/task-prompt.txt`。本审计没有展示该提示词或读取候选内容。固定版源码、管理员凭据、历史候选不应进入 Agent 工作区。

`TaskScoreGateway` 只暴露冻结题目的 `/submit-vul`，核对 masked task_id、agent_id、checksum、require_flag；不暴露 `/submit-fix` 或管理查询。最终修复版评分由宿主执行。

## 评分身份和存储

一轮 run_id 对应一个冻结运行目录；同一轮的任务共享 run_id，以 task_number、task_id、独立 agent_id 区分。每题拥有独立 Slurm Job ID、工作区、模型日志、评分网关日志和数据库。

Agent 必须自行留下 `workspace/final_submission.json`，内容包含其选择的 poc_id。`select_final_submission()` 将它匹配到本 attempt 已完成且有效的评分请求；`admin_finalize()` 读取网关保存的候选快照，并核对 submitted_sha256。宿主不会替 Agent 选出“最好”的候选。

官方 DB 用 `(agent_id, task_id, poc_hash)` 去重。同一工件重提可能复用 poc_id，不能将 poc_id 称为“每个 HTTP 请求都唯一的 submission_id”；逐请求身份另见 `score/requests*.jsonl`。关联顺序为 run_id → task_number/agent_id → request → poc_id/候选哈希。

## 与已成功 smoke 的区别

`$PROJECT/run_dynamic.sbatch` 和 `cybergym_dynamic.py` 是普通输入集成入口，使用 image 模式，模型工具关闭。正式 `task.sbatch` 使用 binary 模式、原生 Bash、完整 rollout 和最终选择协议。旧作业 234260/235477 的成功不能代替正式入口验收。

源码依据：`benchmark_runner/run.py:190`；`cybergym/entry.py:main`；`cybergym/task.sbatch`；`cybergym/production.py:136`；`scripts/agent_harness.py:run`；`scripts/task_socket_gateway.py:1080`；`scripts/run_qwen35_formal_task.py:160,309`；`scripts/run_multi_harness_task.py:59,82`。实际部署使用 RUN 下的冻结副本，阅读源码时应同时核对 `identity.json`。
