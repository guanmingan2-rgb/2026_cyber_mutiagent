# Agent Runtime 集成：变更、测试与运行说明

## 1. 代码变更

| 文件 | 类型 | 作用 |
|---|---|---|
| `pipeline/runtime.py` | 新增 | `TaskContext` / `RuntimeConfig` / `AgentRunResult` / `AgentRunner` 协议 / `RunStatus` / `get_runner()` |
| `pipeline/runtimes/__init__.py` | 新增 | 包标记 |
| `pipeline/runtimes/oneshot.py` | 新增 | `OneShotRunner`：原单轮逻辑迁入，保留 smoke/solve 提示词与校验 |
| `pipeline/runtimes/frozen.py` | 新增 | `FrozenExecutorAdapter`：子进程调用旧执行器 `agent_harness.py run` |
| `pipeline/replay.py` | 新增 | 历史执行器产物 → 统一结果（只读回放） |
| `pipeline/run.py` | 修改 | 新增 `--runtime {oneshot,frozen}`、上下文构建、结果交接、错误分类、`success` 三态 |
| `pipeline/common.py` | 修改 | `save()` 自动创建父目录 |
| `tests/test_runtime.py` | 新增 | 29 项测试中的 15 项（协议、run 流程、两适配器、历史回放） |

未修改：`2026_cyber_agents` 任何文件、官方评分逻辑（`pipeline/scoring.py` 的判据未动）、任务数据。

### 统一接口

```python
class AgentRunner(Protocol):
    def run(self, context: TaskContext, config: RuntimeConfig) -> AgentRunResult: ...
```

`AgentRunResult` 含：`run_status`、`candidate_bytes/sha256`、`trajectory_path`、
`llm_requests`、`input_tokens`、`output_tokens`、`walltime_sec`、`error_type/message`、`details`。

### 两个适配器

**OneShotRunner**：单会话、无工具。提示词与校验逻辑从原 `run.py` 原样迁入，
`run.py` 保留 `SMOKE_INSTRUCTION/SOLVE_INSTRUCTION/validate_smoke/validate_solve` 的再导出，
旧调用方式不变。

**FrozenExecutorAdapter**：

```text
build_workspace 复制 task/ 材料到 output/workspace（普通文件，非硬链接）
write_prompt    生成 prompt.txt（要求写 /workspace/poc.bin，不依赖 submit.sh）
subprocess      <solver venv python> agent_harness.py run --harness deepseek \
                --workspace ... --task-file ... --output-dir ... --agent-profile <frozen profile>
                HARNESS_CONFIG=<frozen agent-harnesses.json>
解析            summary.json（status/exit/model_usage）+ workspace/poc.bin
```

不传 `--score-socket`：Agent 无评分通道，评分只在 `pipeline/scoring.py` 发生。

## 2. 测试

命令：

```bash
cd /home/ybgao/2026_cyber_mutiagent
/home/ybgao/2026_cyber_agents/.venv-deepseek-harness/bin/python -B -m unittest discover -s tests -v
```

结果：**29 / 29 OK**（原有 12 项 + 新增 17 项）。新增覆盖：

| 测试 | 验证点 |
|---|---|
| `test_both_runtimes_satisfy_protocol` | 两个 runtime 实现同一接口 |
| `test_unknown_runtime_rejected` | 未知 runtime 报错 |
| `test_completed_runtime_reaches_official_score` | 候选 → 评分 → `success=true`，产物齐全 |
| `test_official_failure_is_success_false_not_null` | 官方有效但不成功 → `success=false` |
| `test_runtime_failure_keeps_success_null` | no_candidate/truncated/timeout/infra → `success=null`、`official_status=not_evaluated` |
| `test_scorer_rejection_reports_infra_error` | 评分拒绝 → `official_status=infra_error`，不伪造 false |
| `test_dry_run_builds_command_and_workspace` | frozen 命令与工作区构建 |
| `test_fake_executor_produces_candidate` | 假执行器 → 候选与 metrics 映射 |
| `test_fake_executor_without_candidate` | 执行器无候选 → `no_candidate` |
| `test_arvo_10013_replay_matches_official_record` | 历史回放：hash 一致、vul=77/fix=0、success=true |

### 真实任务 dry-run（不调 API、不评分）

```
python -m pipeline.run --task-id arvo:10013 --output /tmp/frozen-dryrun \
  --mode solve --runtime frozen --frozen-dry-run \
  --source src-vul/graphicsmagick/coders/tiff.c
```

- 生成的命令与 `agent_harness.py run --help` 的参数完全一致；
- 工作区通过执行器自带的 `audit_task`：`passed True, forbidden [], archive []`；
- `report.json` 正确记为 `runtime_status=infra_error`（DryRun），`success=null`。

### Slurm 兼容

`run_pipeline.sbatch` 用 `"$@"` 透传参数，`--runtime frozen` 可直接使用。
首次 dry-run 作业 237860 在 gpu12 因节点 `/dev/shm` 余量不足被运行时检查拒绝
（`需要新增 2.7 GiB 并保留 20 GiB`）——节点容量问题，不是适配器问题；
已在合格节点（gpu04/gpu13）重新提交。

## 3. 单题运行说明

### One-Shot Solve（无工具）

```bash
cd /home/ybgao/2026_cyber_mutiagent
sbatch run_pipeline.sbatch arvo:10013 --mode solve --runtime oneshot \
  --source src-vul/graphicsmagick/coders/tiff.c --agent-timeout 900
```

### Frozen Runtime（正式执行器，多轮 + bash）

```bash
sbatch --nodelist=gpu04,gpu13 --export=ALL,SAFEGYM_PIPELINE_MODE=binary \
  run_pipeline.sbatch arvo:10013 --mode solve --runtime frozen \
  --source src-vul/graphicsmagick/coders/tiff.c --agent-timeout 14400
```

先做不花钱的命令构建检查：在上一行加 `--frozen-dry-run`。

### 查看状态与结果

```bash
R=runs/pipeline-<job>
cat $R/job-status.json          # 阶段
cat $R/result/report.json       # 统一报告（success 三态）
cat $R/result/score.json        # 官方 vul/fix
cat $R/result/agent/runtime-result.json   # runtime 状态与 metrics
ls  $R/result/agent $R/result/workspace
tail -f runs/pipeline-<job>.log
```

## 4. 后续 Baseline 计划（授权后）

1. 固定任务清单与 seed，冻结 `--runtime frozen` + frozen profile；
2. 每题一个 Slurm 作业，由 `pipeline/run.py` 统一编排；
3. 汇总：Final-submission Success Rate（官方）、签名匹配率、Token、费用、墙钟、失败分类；
4. 失败分类固定为：runtime 失败 / 无候选 / 截断 / 超时 / 评分拒绝 / 评分完成但未成功 / 基础设施；
5. 与 `2026_cyber_agents` 的正式结果对比时，必须使用相同的 frozen profile 与预算。

## 5. 判定

| 维度 | 结论 |
|---|---|
| Adapter 集成 | **完成**（接口、两适配器、run 流程、错误分类、回放） |
| 测试 | **通过**（29/29，离线） |
| 真实环境就绪 | **基本就绪**（命令与工作区在真实任务上 dry-run 通过；Slurm 节点容量需选点） |
| 真实 frozen 运行 | **未执行**（本轮禁止付费 API） |

**总体：PARTIAL** —— 接口与测试完成，真实 frozen 运行尚未验证；获得付费授权后可立即执行单题。

## 6. 已知限制

- Frozen 适配器的提示词要求 `workspace/poc.bin`，与旧执行器原生的
  `final_submission.json`+`poc_id` 协议不同；这是为了让评分统一走 `pipeline/scoring.py`；
- 若要与旧基线严格同分布，应使用旧执行器原生提示词 + `TaskScoreGateway`，但那会重复评分路径，
  本轮按“统一收敛到 pipeline 评分”的要求实现；
- 执行器需要 `HARNESS_CONFIG`、toolchain/dependencies manifest 与 runtimes 完好（已核实存在）。
