# 正式入口清单

所有下列相对路径均以 `/home/ybgao/2026_cyber_agents/` 为根；参数来自实际 argparse 或 shell 参数解析。R 表示本轮 RUN：`/home/ybgao/2026_cyber_agents/safegym_solver/runs/unified-migration-20261113T032648Z-ca5b9e`。

| 实际文件／函数 | 调用关系及参数 | 配置与环境变量名称 | 副作用与输出 |
|---|---|---|---|
| `benchmark_runner/run.py:main` | 主 CLI：`status/check-config/check/submit` + `harness/cybergym/e2e/exploitgym`；`--allow-external-api`；`--agent-profile` 仅 Harness 检查允许 | `benchmark_runner/config.py:config_path`；`CYBERGYM_BENCHMARK_CONFIG`；profile.api_key_env | status/check-config 只读；check cybergym 提交隔离作业；submit 提交正式作业。回执在 `benchmark_runner/runs/cybergym-submit-<验收报告SHA256>.json` |
| `benchmark_runner/config.py` | load_benchmark_config/load_benchmark_inputs/config_hash | 未设置覆盖变量时读取 `configs/cybergym.json`；本轮使用 PROJECT/outputs/deepseek_baseline/benchmark-config.json | 只读，不调用模型、不提交作业、不评分 |
| `benchmark_runner/cybergym/entry.py:freeze/main` | 由 run.py 调用，参数 `check` 或 `submit`；没有单题过滤、并发或重试 CLI | `BENCHMARK_RUNNER_DISPATCH=cybergym` 由父进程设置；config.inputs、verification_report；冻结 profile；profile.api_key_env | check 冻结并 `sbatch --wait`；submit 对 run-plan 每项 `sbatch --parsable`，写 `R/task-dispatch-N.json`。不要手动设置内部 dispatch 变量绕过父入口 |
| `benchmark_runner/cybergym/check.sbatch` → `migration_check.py:main` | 参数 RUN_DIR；真实 SDK/工具隔离、task audit、export/stream 夹具 | R/frozen/profile.json、inputs.json、agent-harnesses.json；`SLURM_JOB_ID` | 提交者为 entry.check；执行本地模型，不调用真实 DeepSeek，也不做正式目标评分。写 verification.json、task-N/fixture、isolation-acceptance.json |
| `R/frozen/entry/task.sbatch` | 两个位置参数 RUN_DIR NUMBER；由 entry.submit 调用，主脚本不建议人工直调 | R/frozen/code、run-plan、storage_guard；`SLURM_JOB_ID`、`XDG_RUNTIME_DIR`、`TMPDIR`、`DOCKER_HOST`、`SAFEGYM_DOCKER_RUNTIME/DATA/LOG_DIR/DB_PATH`；继承 `DEEPSEEK_API_KEY` | 作业内启动服务/Agent/评分。日志 `R/task-N-JOBID.log`、`worker-logs/N/`；没有额外 sbatch |
| `R/frozen/entry/production.py:verify/task` | `verify RUN_DIR`；`task RUN_DIR --number N --score-port PORT`；仅由作业启动 | frozen/profile、inputs、identity、verification；`SLURM_JOB_ID`、`HARNESS_CONFIG`；`CYBERGYM_*` 在宿主侧注入 | task 调用 API、管理评分并写 `results/deepseek/tasks/NN/`；verify 仅核验。禁止用 task 命令替代调度门禁 |
| `scripts/harness.py:make_task` | `(task_id, agent_id, out_dir, data_dir, server)` → `cybergym.task.gen_task.generate_task` | level1、mask_map、`CYBERGYM_SALT` | 不调用模型/Slurm/评分；生成 workspace 的描述、源码归档、README、submit.sh、task.json |
| `scripts/agent_harness.py:run` | `run --harness deepseek --workspace … --task-file … --output-dir … --score-socket … --agent-profile …`，由 production 调用 | frozen profile 和 agent-harnesses；`HARNESS_CONFIG`；`DEEPSEEK_API_KEY` | 启动隔离 Harness，通过 ModelGateway 调用 API；输出 execution/summary.json、model-usage.json、轨迹与 stdout/stderr。不独立提交 Slurm |
| `agent_runtime/deepseek.py` / `deepseek_tool_runner.py` | 由 Harness 适配器与工具包装器调用，使用已生成 spec；不是研究者的正式顶层入口 | frozen spec/profile、工具 namespace 参数、网关临时凭据 | SDK 执行原生 Bash 及任务动作，轨迹由现有实现保留。不能直接在登录节点使用裸工具入口替代沙箱 |
| `scripts/harness.py:serve` | 作业调用 `serve --mode binary --host 127.0.0.1 --port PORT --memory-gib 2` | versions.json、binary-extracted.json、server.env；`CYBERGYM_API_KEY`、`CYBERGYM_API_KEY_NAME`、`CYBERGYM_SALT`；SAFEGYM 路径 | 启动官方 FastAPI；接收请求才执行目标。DB 在 worker-logs/N/score/poc.db；serve.log 和官方原始输出同目录 |
| `scripts/task_socket_gateway.py:TaskScoreGateway` | production 构造 Unix socket，冻结 metadata、task_id、backend_port、report_dir | 随机任务 token、冻结题目身份；Agent 无管理员 Key | 转发 `/submit-vul`，记录 score/requests.jsonl、requests-started.jsonl 和请求快照；不提交 Slurm |
| `scripts/run_qwen35_formal_task.py:select_final_submission/admin_finalize` | production 调用；最终选择来自 Agent，fixed 使用同一快照哈希 | `CYBERGYM_API_KEY`、`CYBERGYM_API_KEY_NAME`；directory/score、metadata、score_port | `/submit-fix` → `/query-poc`；写 fixed-evidence.json、official-records.json；不调用模型 |
| `scripts/run_multi_harness_task.py:outcome/official_success` | production 解析 Harness 摘要及官方单条最终记录 | model_usage、status、exit_code、selected.submitted_sha256 | 无网络；区分 accounting error、timeout、completed 等，并按双侧退出码判定 |
| `benchmark_runner/cybergym_progress.py` | `--run-dir PATH [--watch] [--interval 5]` | 默认从固定 configs/cybergym.json 找报告，忽略环境覆盖 | 只读但结果目录写死 openhands，不适用于本轮 DeepSeek。不要把其“排队中”当权威状态 |
| `benchmark_runner/cybergym/summarize.py` | 无通用 run-dir CLI，顶层代码即执行 | 写死旧 20261101 实验和 arvo:61237 | 会写旧 manifest/result；本轮不执行，也不作为通用汇总入口 |
| `scripts/export_official_submission.py:main/export` | `--run-dir --output-dir [--publication-manifest] [--package]` | 人工审阅公开文件清单、正式结果 | 生成本地导出或 not_ready 诊断；不上传排行榜。与当前统一 run 格式的完整兼容性待验证，不推荐直接用于本轮验收 |

`DEEPSEEK_API_KEY` 是当前 profile 指定的名称，不在手册中提供值。`config/server.env` 中的管理员参数由作业加载；不是在 shell 中打印或发送给模型。`readiness.json` 只是检查输出，submit 并不把它作为参数读取；真正读取的是 config.verification_report 及其绑定的原始运行证据。
