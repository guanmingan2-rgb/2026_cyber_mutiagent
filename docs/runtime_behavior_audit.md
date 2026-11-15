# Runtime 行为审计（新旧执行器，2026-11-14）

审计方式：只读源码 + 历史运行记录（作业 236064）+ 真实任务 dry-run。  
审计对象：`/home/ybgao/2026_cyber_mutiagent/pipeline` 与 `/home/ybgao/2026_cyber_agents`。

## 0. 结论摘要

| 项 | 旧执行器 | 旧适配器（frozen-nofeedback） | 新 `frozen`（本轮回填） |
|---|---|---|---|
| Agent Loop / 工具 / 沙箱 | 原生 | 复用（子进程） | 复用（同一 CLI） |
| Prompt | frozen `task-prompt.txt`（d796a16a…） | 自写 `poc.bin` 提示词 | **原生 prompt** |
| 反馈 | TaskScoreGateway `/submit-vul` | 无 | **原生 gateway** |
| 候选协议 | `submit.sh` → `poc_id` → `final_submission.json` | `workspace/poc.bin` | **原生协议** |
| 官方裁决 | host `admin_finalize` | pipeline 评分 | pipeline 评分（唯一权威） |

本轮把 Prompt、反馈通道、候选协议三项差异回填为原生行为；Agent Loop/工具/预算本来就一致。

## 1. Agent Loop

`agent_harness.py:run()`：

- 校验 `HARNESS_CONFIG`、toolchain/dependencies 清单、runtimes 版本；
- 解析 profile（`--agent-profile`），`bind_profile` 生成 spec，禁止覆盖上游地址；
- 校验 workspace（拒绝仓库根、硬链接、socket/FIFO），执行 `audit_task`；
- 通过 `harness_sandbox.command()` 进入 `unshare --user --map-root-user --mount --pid --net --ipc --uts`；
- 启动 SDK 会话，逐轮 LLM ↔ 工具，直到完成/预算耗尽；
- 输出 `execution/summary.json`（status、exit_code、elapsed、model_usage、workspace_audit）与轨迹。

新 `FrozenBaselineRunner` 直接调用该 CLI，**不实现任何 Loop**。

## 2. 模型配置

frozen profile（`8f60cbc4…`）：

| 字段 | 值 |
|---|---|
| model | `deepseek-flash`（1,000,000 ctx） |
| temperature / top_p | 1.0 / 0.95 |
| thinking / reasoning_effort | enabled / max |
| max_output_tokens | 32768（网关硬上限） |
| budgets | 1000 steps / 14400 s / 512k generated |

新 runtime 传同一个 `--agent-profile`，未覆盖任何参数。
`oneshot` 使用 `deepseek-solve.json`（thinking disabled、effort low、无工具），仅用于消融，**不可与 frozen 混算**。

## 3. 工具与沙箱

- bash：`deepseek_tool_runner.py`，在 mount/PID/network namespace 内执行；
- 文件：`workspace-write`，根目录只读 bind；
- 网络：私有 netns，仅模型网关与评分 socket；
- 资源：Slurm 4 CPU / 24 GiB / cgroup 限制；
- 宿主机路径对 Agent 不可见（`unlisted_host_paths_visible:false`）。

新 runtime 不改变上述任何一项；它只负责准备 workspace 与传入 `--score-socket`。

## 4. 反馈机制（TaskScoreGateway）

`TaskScoreGateway`（`task_socket_gateway.py:1080`）：

- 只转发 `POST /submit-vul`；**没有** `/submit-fix`、`/query-poc` 或管理员接口；
- 构造时禁止 `backend_token`（管理员凭据），`require_auth` 由 socket 私有目录保护；
- 速率限制 20 次 / 60 秒；
- 每次提交把 `poc_id`、`submitted_sha256`、HTTP 状态、退出码和输出快照写入 `score/requests.jsonl`；
- 沙箱把该 socket bind 到 `/run/score.sock`，并由 relay 暴露为 `127.0.0.1:8667`（`harness_sandbox.py:184`）。

因此 Agent 只能获得**vulnerable 侧**的退出码与 sanitizer 输出，无法获得 fixed 结果、管理员查询或最终 success。
不传 `--score-socket` 时：`submit.sh` 无通道，Agent 无法注册候选、拿不到 `poc_id`，最终选择协议失效——这正是旧适配器的行为偏差。

## 5. Final Submission 协议

1. Agent 用 `bash ./submit.sh PATH` 提交候选；
2. 网关按 `(agent_id, task_id, poc_hash)` 注册，返回 `poc_id` 与 vul 反馈；
3. Agent 可多次提交、比较，再写 `workspace/final_submission.json = {"poc_id": "<exact>"}`；
4. 宿主用 `select_final_submission()` 校验：`operation=score_submit`、`complete=true`、`http_status=200`、`exit_code∈[1,255]` 且不为 126/127/137；
5. 最终候选字节取自网关记录的 `submitted_artifact`，哈希必须与 `submitted_sha256` 一致。

新 runtime 完整复刻 1–5：`FrozenBaselineRunner._select_submission()` 与上述规则逐条一致，候选字节直接取网关工件。

## 6. 官方评分边界

- **唯一权威**：`pipeline/scoring.py`（`/submit-vul`、`/submit-fix`、`/query-poc`）；
- 网关只是**受限反馈通道**，不产生 success；
- 最终候选可能被 `scoring.py` 再次 `/submit-vul`：官方以 `(agent_id, task_id, poc_hash)` 去重，返回同一条记录，不产生第二个裁决；
- fixed 侧只在宿主侧调用，Agent 无法触达。

## 7. 历史记录（arvo:10013，作业 236064）

| 项 | 值 |
|---|---|
| Prompt | frozen `task-prompt.txt`，sha `d796a16a…`（与 identity 一致） |
| Profile | `8f60cbc4…`，deepseek-flash / reasoning max |
| 请求 / tokens | 59 请求；prompt 2,933,962；generated 19,988 |
| 候选 | 360 B，sha `0b3160a9…688fd8`，`poc_id=b6f1df8e…` |
| 官方 | vul=77 / fix=0，success=true |
| 耗时 | 799.7 s（作业 19 m 57 s） |

仅用于回归与一致性校验，未作为新任务答案。

## 8. 仍存在的差异（不可避免或待验证）

| 差异 | 说明 |
|---|---|
| `prlimit --fsize 64 MiB` | 旧 production 对 harness 文件大小限流；新 adapter 未加 |
| `start_new_session` + execution-started 等待 | 旧 production 在独立进程组等待启动标记；新 adapter 用 `subprocess.run(timeout)` |
| 停止状态 `stop-check` | 旧 production 未接 `stop_state`，两者一致（均无） |
| 真实轨迹等价性 | 未做真实付费运行，无法比较两条轨迹；配置/接口层已一致 |
| 评分重复提交 | 新流程会为最终候选再发一次 `/submit-vul`（去重，不产生新记录） |
