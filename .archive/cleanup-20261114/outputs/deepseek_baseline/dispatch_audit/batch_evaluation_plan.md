# 批量评测能力与管理方案

本文件描述现有能力和人工管理方案，不新增调度或求解功能。准确操作命令见manual_runbook.md。

## 能力矩阵

| 能力 | 当前支持情况 | 实际依据 |
|---|---|---|
| 单任务 | 支持题单长度为1；无顶层--task-id | config.inputs.task_ids → run-plan.attempts |
| 多任务列表 | 支持，禁止重复task_id，输入level固定1 | config.py:_check_inputs |
| 批量派发 | 支持循环每题sbatch；提交后异步执行 | entry.py:main(submit) |
| 最大并发 | 无应用层限额、无array百分比参数 | entry使用独立sbatch，没有依赖或throttle参数 |
| 每题独立目录 | 支持NN目录、独立agent_id和JobID | production.task和task.sbatch |
| 每题独立run_id | 不支持此语义；共享轮次run_id，另有task_number | freeze与run-plan |
| 防重复提交 | 支持总回执哈希、全局lock、逐题open(x)、attempt O_EXCL | run.py、entry.py、claim_attempt |
| 已完成跳过 | 无自动skip-completed；重复整轮直接already_dispatched | run.py不逐题查完成状态 |
| 失败重试 | 无正式CLI；需人工新轮次 | 没有resume/retry参数 |
| 断点恢复 | 能保留中间证据，不能自动续派部分任务 | 中途失败有回执，但下一次总入口拒绝复用失败回执 |
| 作业状态查询 | Slurm可靠；现有progress有DeepSeek路径问题 | progress.py写死openhands |
| 评分/用量记录 | 每题具备，需逐题核对有效性 | task-result/official-records/execution/model-usage |
| 通用Baseline汇总 | 未发现可直接用于本轮的完整入口 | summarize.py绑定历史目录；手册提供只读查询 |

## 推荐分阶段操作

1. 先完成本轮3938正式单次rollout，核对真实API、工具轨迹、最终选择、同一候选双侧评分及状态。
2. 有效评测后选2～4题，记录选择依据；仅凭任务目录存在不能保证binary/镜像完整。使用相同profile、预算和工具，单题一个首次attempt。
3. 每个新批次建立独立配置和verification_report，通过现有check重新冻结并逐题验收。题单不可在check后修改。
4. 若需严格并发1，在人工单题批次间等待前一题终态，再开始下一批。否则一次提交的数量可能就是API并发上界，Slurm不会自动按模型费用节流。
5. 不并行操作多个check；共享的cybergym-check-state.json会记录最近一次证据，可能使较早未提交批次失效。
6. 每题以run_id、task_number、task_id、agent_id、job_id和poc_id/hash建立关联，不把poc_id当每次HTTP唯一ID。

## 部分派发失败或会话中断

- 顶层receipt=dispatching或dispatch_failed时，先逐一核对task-dispatch-N.json。
- exit_status=0且stdout可解析的题：查对应JobID；不可重复派发。
- 回执不存在不充分证明未执行；同时核对sacct、Slurm脚本/StdOut与运行目录，尤其是写回执与sbatch返回之间崩溃。
- 回执不完整、任务状态未知时，不自动重试。
- 只有确认未执行或明确决定重试的题，才放入新一轮；保留旧记录，不覆盖冻结文件、attempt或候选。
- 对已执行任务的后续retry必须标为第二次尝试；不能与单次pass@1混算。基础设施失败与模型失败分列，若要剔除工程缺失题，应公开剩余分母及剔除数量。

## 结果判定与统计口径

至少同时维护以下互不替代的字段：scheduler_state、worker_stage/cleanup、agent_status、accounting_status、final_selection_valid、official_evaluation_status、official_success。

正式task-result的status=completed不保证双侧评分执行；无最终候选也可能completed/false。Agent超时但存在合法候选时，最终评分可能将task.status改为completed，因此原始Agent完成率必须从execution/summary读取。

建议统计：

- 计划任务数与实际启动rollout数分别报告。
- Agent完整运行率：完整Agent运行数 / 实际启动rollout数，工程启动失败另列。
- 官方评分完成率：有完整、匹配的双侧记录数 / 本轮实际启动rollout数，同时报告有效候选数。
- 已评估成功率：官方成功数 / 有效官方评估数。
- 完整首轮pass@1：只有定义清楚首次attempt、无候选失败和工程缺失的处理方式后报告；不得把未启动题当模型错误。
- 步骤、工具调用、模型请求、Token与耗时分别记录；不能用request_count替代所有执行步骤。
- 无有效分母时为null；不将缺失日志当零消耗。

本轮未实现上述聚合器。手册只读查询用于研究者核对，原评分仍以现有official_success及原始记录为依据。

## 需要补充的最小工程能力

P1：修正DeepSeek进度路径；增加只读通用汇总并联合sacct/worker/task；提供可审计的部分派发对账与重试策略；加入明确应用层并发/费用限制。

P2：各轮独立的最近验收状态、可重复的汇总schema、单次/多次attempt指标和失败分类。无需为这些工程功能重新设计Agent策略。
