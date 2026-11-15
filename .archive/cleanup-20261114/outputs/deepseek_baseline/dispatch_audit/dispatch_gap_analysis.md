# 从就绪到正式派发的差距

## 中断前究竟执行到哪里

本轮 RUN 为 `/home/ybgao/2026_cyber_agents/safegym_solver/runs/unified-migration-20261113T032648Z-ca5b9e`。`dispatch.json` 的命令是 `sbatch --wait ... check.sbatch RUN`，stdout=235495，exit_status=0；它是验收提交回执，不能当作正式提交回执。

readiness 与 verification 均通过，但没有顶层正式 receipt、task-dispatch-1.json、正式任务 Slurm 记录、results/deepseek/tasks/01 或评分 DB。已证实结论是“正式 submit 尚未执行到留下可见回执的阶段”。会话中断与这一状态一致，但仅凭缺失文件不能证明中断是唯一原因，也不能推断提交器存在 Bug。

下一步入口应是保持配置覆盖环境后执行 `run.py submit cybergym --allow-external-api`。无需生成额外本轮配置，无需传 readiness 的字段，无需自行启动服务。CLI 的显式授权 flag 和凭据变量是必要条件；CLI 不弹出另一层交互确认。手册中的正式命令须由有相应授权的研究者执行，本审计未执行。

## 问题排序

| 优先级 | 问题 | 证据等级与处置 |
|---|---|---|
| P0 | 正式 `/tmp` 存储门禁尚未实测满足 | 源码明确20 GiB余量/配额要求；此前gpu19仅5 GiB为历史证据。当前容量未知。正式派发前确认，不宣称一定失败或已满足 |
| P0 | 不能把 SDK fixture 或 smoke 当正式 Baseline | 本轮 external_model_calls=0；无正式候选。所有成绩为空，首题正式记录是必要验收 |
| P0 | 缺密钥或中断可能留下非成功总回执，阻止直接重提 | run.py 在调用 entry 前写 dispatching；已有失败回执返回 dispatch_requires_review。先核对 Slurm 和所有回执，不删文件强行重跑 |
| P1 | DeepSeek 进度查看器路径错误 | cybergym_progress.py:progress 写死 results/openhands/tasks；默认配置还忽略本轮覆盖变量。可能把已完成DeepSeek显示为排队，TTY submit后自动 watch亦受影响。用手册只读查询代替，后续修复工具 |
| P1 | summarize.py 不是本轮通用汇总器 | 写死旧目录/旧任务，运行会写旧文件。不要运行它生成本轮报告。需要按profile.harness和实际记录实现统一只读汇总 |
| P1 | 无提交并发上限、自动重试、部分派发恢复或已完成跳过 | entry.submit直接循环全部attempts，逐题文件用open('x')。有防重保护但不是resume。批次中途失败需人工对账并在新轮次安排未完成题 |
| P1 | 取消与早期失败状态不完全一致 | production内部捕获信号可写cancelled；shell先于production终止时退出143/130，但cleanup可能写 infrastructure_error，并且setup发生在trap安装前。必须联合sacct、worker和task结果判定 |
| P1 | completed/false可能并未完成官方双侧评分 | production允许Agent正常结束但无最终候选，success=False；official-records=[]。不能仅用status=completed推定evaluated |
| P1 | 评分完成状态可能覆盖先前Agent超时状态 | 有效选择被admin_finalize评完后status改为completed；Agent原始状态应从execution/summary.json读取 |
| P1 | cleanup异常不一定反映在Slurm退出码 | task.sbatch最终exit body_exit；cleanup_exit单独记录。Slurm COMPLETED不能代替清理和评分验收 |
| P1 | 模型/Harness泛化错误归因较粗 | outcome把多种失败归infrastructure_error/harness_execution_failed，缺足够轨迹时只能保留原错误，不可直接认定模型能力失败 |
| P2 | 共享最新验收状态限制并行管理多轮 | run.py check_state按benchmark命名并用全局lock；后一次check改变最近报告身份。多批次应串行完成check→submit，不同时重写验证状态 |
| P2 | 有效任务指标与attempt pass@1未统一 | 进度器用计划题数作为成功数分母，不能称已评估成功率。手册给出明确分母，完整聚合待实现 |
| P2 | provenance仍需整洁版本管理 | 本轮有冻结代码哈希和基准commit，但工作区存在未提交改动；不能声称只检出commit即可重现。保留原冻结目录与身份清单 |

本轮未修复上述问题，遵守只读代码审计边界。此前已修复的配置覆盖、storage_guard冻结遗漏、contract_hash导入，不再重复开发。

## 回执与失败位置

顶层回执预期路径为 `/home/ybgao/2026_cyber_agents/benchmark_runner/runs/cybergym-submit-e034748a0fbc2e6b3aac2916a91b7022a158c39d3e4b6688c3fcb84910c6d3b4.json`，目前不存在。哈希来自本轮发布的验收报告。

逐题回执是 RUN/task-dispatch-N.json，含command、stdout、stderr、exit_status。stdout的`sbatch --parsable`值可为JobID或JobID;cluster，取分号前数字查询。调度成功只证明sbatch接受。

无凭据、sbatch失败等会写总回执的stderr/stdout；run.py返回not_ready时可能还没有提交回执。若进程死于写入回执之后，dispatching或空的逐题文件是“未知提交状态”，必须查sacct和Slurm Command/StdOut匹配，不能当作未提交。
