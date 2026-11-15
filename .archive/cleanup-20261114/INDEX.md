# 目录整理记录

日期 2026-11-14。原 README 已备份。下表列出从活动位置移入此目录的条目，归档沿用原相对路径。摘要见 `SHA256SUMS`。

| 原路径 | 用途 |
|---|---|
| `diagnose_dynamic.py` | 一次性历史元数据诊断 |
| `runs/link_dynamic_docs.py` | 一次性 README 链接生成 |
| `runs/prepare_baseline.py` | 一次性预检资料生成 |
| `DYNAMIC_SCORING.md` | 旧动态验收诊断报告 |
| `outputs/deepseek_baseline/dispatch_audit/acceptance_checklist.md` | 旧派发审计说明 |
| `outputs/deepseek_baseline/dispatch_audit/architecture.md` | 旧派发审计说明 |
| `outputs/deepseek_baseline/dispatch_audit/batch_evaluation_plan.md` | 旧派发审计说明 |
| `outputs/deepseek_baseline/dispatch_audit/dispatch_gap_analysis.md` | 旧派发审计说明 |
| `outputs/deepseek_baseline/dispatch_audit/entrypoints.md` | 旧派发审计说明 |
| `outputs/deepseek_baseline/dispatch_audit/manual_runbook.md` | 旧派发审计说明 |
| `outputs/deepseek_baseline/dispatch_audit/p0_preflight_report.md` | 旧派发审计说明 |
| `outputs/deepseek_baseline/dispatch_audit/prerequisites.md` | 旧派发审计说明 |

历史运行目录、JSON、评分记录及集成验收导出均保留。恢复时以此目录为源，将所需文件复制回原相对路径。恢复前应核对 SHA256 并确认目标文件未被后续工作修改。

本次也修正了两个本地假模型测试对结构化日志的断言。原件为 smoke.py.before 和 smoke_task.py.before。

## 2026-11-14 流水线重组

目标：只保留 CyberGym 评测主链路（数据读取 → Agent 分析 → 官方动态评分），把静态多角色审查、
教学消融、官方记录导入、一次性诊断等与评测主链路无关的代码移出活动目录。

新增活动代码：

| 路径 | 作用 |
|---|---|
| `pipeline/data.py` | 阶段 1：有界读取 description/README/repo-vul 成员并计算 SHA256 |
| `pipeline/agent.py` | 阶段 2：单个 DeepSeek Harness 会话与 JSON 交接校验 |
| `pipeline/scoring.py` | 阶段 3：官方 prepare/score（在 solver 虚拟环境运行） |
| `pipeline/run.py` | 编排三阶段，任何失败都写 report.json |
| `pipeline/status.py` | Slurm 阶段状态落盘 |
| `run_pipeline.sbatch` | 单题 Slurm 入口（替代 run_dynamic.sbatch） |

移入 `static-review/` 的原活动代码：

| 原路径 | 用途 |
|---|---|
| `review.py` | 三角色静态审查引擎（Manager/Worker/Reviewer） |
| `review_task.py` | CyberGym 题目的静态审查入口 |
| `cybergym_experiment.py` | Level 1 静态消融实验 |
| `experiment.py`、`experiment_fixture.py`、`experiment.example.json` | 教学样例与固定模型夹具 |
| `evaluation.py`、`teaching_checks.py` | 教学题评估与回归检查 |
| `official_report.py`、`OFFICIAL_RESULT_IMPORT.md` | 既有官方评分记录导入 |
| `historical_result_audit.py`、`export_e2e_validation.py` | 历史结果审计与导出 |
| `collect_dynamic.py` | 动态作业状态收集（一次性运维脚本） |
| `smoke.py`、`smoke_task.py` | 静态审查的本地假模型端到端测试 |
| `test_experiment.py`、`test_official_report.py`、`test_historical_result_audit.py`、`test_dynamic_status.py` | 随对应模块归档的测试 |
| `task_materials.py` | 由 `pipeline/data.py` 取代 |
| `dynamic_score_backend.py` | 由 `pipeline/scoring.py` 取代 |
| `cybergym_dynamic.py` | 由 `pipeline/run.py` 取代 |
| `dynamic_run_status.py` | 由 `pipeline/status.py` 取代 |
| `run_dynamic.sbatch` | 由 `run_pipeline.sbatch` 取代 |

注意：`static-review/` 中的模块仍按旧路径 `import review` / `import task_materials`，
活动目录已无这些模块，因此它们是**历史存档、不可直接运行**；恢复时需把依赖一起复制回根目录。
`runs/`、`outputs/` 与既有验收记录未做任何删除。备份：`.archive/backup-pre-pipeline-*.tgz`。
