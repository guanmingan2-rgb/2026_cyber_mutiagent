# CyberGym DeepSeek 正式派发前 P0 检查

**结论：NO-GO。** 当前检查 shell 未导出 Runner 所需的 `DEEPSEEK_API_KEY`；`gpu19` 正式运行目录的实时容量、权限和实验授权仍无充分证据。即使补齐凭据，也不能在其余必要条件仍为 UNKNOWN 时判定 GO。

检查日期：2026-11-13（北京时间）。任务：`arvo:3938`，Level 1。运行：`unified-migration-20261113T032648Z-ca5b9e`。以下 PASS 只覆盖对应证据，不代表正式 Rollout 已验收。

路径：代码根为 `/home/ybgao/2026_cyber_agents`，项目根为 `/home/ybgao/2026_cyber_mutiagent`。`RUN` 指代码根下的 `safegym_solver/runs/unified-migration-20261113T032648Z-ca5b9e`。

| P0 条件 | 状态 | 源码或实测证据 |
|---|---|---|
| 解释器、正式入口及配置身份 | PASS | 使用 `/home/ybgao/2026_cyber_agents/safegym_solver/.venv/bin/python`（3.12.13），显式选择项目 `outputs/deepseek_baseline/benchmark-config.json`。`benchmark_runner/run.py status cybergym` 返回 `passed`、退出码 0；门禁核对了报告、冻结输入及源码身份。配置只含 `arvo:3938`，不会误用默认 100 题。 |
| 模型、Harness 和预算 | PASS | `RUN/frozen/profile.json`：`deepseek-flash`、DeepSeek Harness、原生 `bash`；1000 个请求步骤上限、墙钟 14400 秒、单请求 1800 秒。此项核对配置，不验证模型端点或权重。 |
| Task 与 binary 评分资源映射 | PASS | 描述和源码归档分别为 132 和 556231 字节；隔离验收曾生成该 Level 1 工作区。冻结 binary 完成标记通过 `binary_state.extraction_complete()`；双侧 `arvo`、`out`、`libs` 布局存在，所需五个 runner/proxy 归档在清单中 complete 且可读。此次没有重新加载镜像。 |
| 官方评分连接配置 | PASS | `RUN/frozen/entry/task.sbatch` 负责启动作业内 loopback binary 服务，`production.py` 使用作业端口和任务 Unix socket。`frozen/code/config/server.env` 有非默认的评分管理员凭据配置项。未启动服务、未验证本次正式评分请求。 |
| Slurm 配置与目标节点 | PASS | `compute` 分区 `UP`，`gpu19` 属于该分区。`task.sbatch` 声明 `users` account、1 节点、1 task、4 CPU、24G、5 小时、无 GPU 请求；`entry.py` 将正式作业绑定到验收报告的 `gpu19`。此项不保证调度时有资源或当前账户实际可获配额。 |
| 目录隔离与结果路径设计 | PASS | `storage_guard.py:setup/private`：节点 `/tmp/cybergym-UID/safegym-slurm-JOBID` 独立，`/run/user/UID/cg-*` 私有；父目录须为本人所有、0700 且路径解析一致。工作目录 `RUN/frozen/code`，日志 `RUN/task-N-JOBID.log`，状态 `RUN/worker-logs/N/`，结果 `RUN/results/deepseek/tasks/NN/`。这是源码确认，目标节点上的实际权限另见下项。 |
| 持久化输出：当前权限、容量、inode 和配额 | PASS | 对 `RUN` 只读测得可写且可进入，可用空间 605741232685056 字节，可用 inode 216112036；`resource_check.quota_snapshot()` 返回配额可验证、硬剩余额度 9034463707136 字节。满足 20 GiB、10000 inode 门槛。未创建探针或结果文件来验证作业内实际写入。 |
| `gpu19` 正式 `/tmp` 和 runtime 当前状态 | UNKNOWN | `storage_guard.py:sample/check` 要求节点本地和运行输出各至少 20 GiB、10000 可用 inode；`setup/private` 另校验私有父目录。连接 `gpu19` 时收到 `Host key verification failed`，未绕过验证；没有可归属该节点的当前挂载、容量或权限数据。旧记录中的约 5 GiB 低于门槛，但不能当作实时值；此前 smoke 的 `/dev/shm` 和作业 235495 的离线验收均不能证明正式路径满足。 |
| DeepSeek 凭据文件配置 | PASS | `profile.api_key_env=DEEPSEEK_API_KEY`；只检查 `$CG_ROOT/safegym_solver/config/<deepseek-credential>.env` 中对应值是否非空，结果为是。没有打印密钥。 |
| Runner 可从当前 shell 读取凭据 | FAIL | 检查进程继承的环境中该变量为空或未设置。当前没有可比较的非空环境值，不能确认研究者未来提交会话是否存在覆盖。`entry.py` 的正式分支读取 `os.environ`，不会自动加载该文件；缺变量会在创建逐题回执前报错，顶层总回执则可能已创建。 |
| 凭据有效性、API 访问和费用授权 | UNKNOWN | 未调用真实 API；非空配置不能证明凭据有效、账户可用或费用已获批准。此次只读请求不授权正式派发。 |
| 正式实验访问范围与授权 | PASS（配置范围）；UNKNOWN（正式授权） | 冻结配置仅包含已安装的 CyberGym 单题；隔离作业 `235495` 在 `gpu19` 为 `COMPLETED / 0:0`，9 分 15 秒，外部模型调用为 0。这支持已有环境的隔离验收记录，不代替对本轮正式实验、实际操作者及预算的明确授权。 |
| 未重复派发、唯一作业记录 | PASS | 未发现按本轮验收 SHA-256 派生的总回执、逐题回执或正式 `results`；同期 Slurm 历史只有本轮离线验收 `235495`，现有队列未发现本轮正式作业。`run.py` 使用锁和总回执，`entry.py` 独占创建逐题回执；正式 Job ID 来自 `sbatch --parsable`，目前不存在。 |

## 正式单题运行前必须人工完成

1. 在已有授权、可信的 `gpu19` 节点会话中确认 `/tmp` 挂载为可读写、容量和 inode 达标。核对 `/tmp/cybergym-$(id -u)` 如果已存在，以及必需的 `/run/user/$(id -u)`：真实路径无符号链接、本人所有、权限 0700。通过管理员或可信渠道核实主机身份；不要关闭 SSH 主机密钥校验。若容量不足，先由运维处理，再重查，不能降低门槛或换用 smoke 路径。
2. 在实际提交会话通过现有凭据管理方式加载并导出 `DEEPSEEK_API_KEY`。只验证是否非空；核对已有值是否来自正确账户和配置，确认 Slurm 环境继承策略。由凭据所有者确认账户状态，不打印、复制或提交密钥。
3. 确认正式实验权限和费用范围，覆盖现有工具、单题和冻结预算。核对实际操作者的 Slurm account/QoS；当前节点状态和历史离线作业不能代替正式授权。
4. 在授权后的实际会话重新运行只读 `status cybergym`，核对题单、运行身份和防重复回执仍一致，并复核输出目录与配额。任何身份变化走原有验收流程；未知回执先对账，不覆盖或删除。所有必要项都确认后，才可按人工手册进入正式步骤。

本轮仅进行只读检查并保存本报告。未提交作业、调用 DeepSeek、执行官方评分或更改冻结配置。

## 复查补充（2026-11-13 13:16 +08:00）

本次完善为只读基础设施复核与证据表述修订；没有修复运行环境、导入凭据、修改冻结配置或启动任务。结论维持 **NO-GO**。原表中的 PASS 是历史采样结论，不能视为本次全部复验。

| 新复核条件 | 状态 | 实际证据及影响 |
|---|---|---|
| 登录节点磁盘健康 | FAIL | `hostname` 为 `hpc-login-01`；`df -Pk /tmp` 显示根分区使用率 100%，可用 488212 KiB（约 477 MiB）。这是登录节点基础设施问题，不能冒充 gpu19 的 20 GiB 门禁检测。未删除任何文件。 |
| 持久化文件系统当前容量 | PASS（仅容量） | `/home` 的可用空间 591537061888 KiB，可用 inode 216103923。未重新验证配额、作业内身份或实际写入。 |
| gpu19 当前存储与访问 | UNKNOWN | 通过既有 SSH 路径查询 hostname，仍返回 `Host key verification failed`；未禁用校验、未更换访问路径。无法确认节点当前挂载、容量和目录权限。 |
| 235495 顶层完成状态 | PASS（调度层） | `sacct` 确认为 `COMPLETED / 0:0 / 00:09:15 / gpu19`。 |
| 235495 所有子步骤是否通过 | UNKNOWN | `.2` 子步骤为 `FAILED / 2:0`，`.0` 和 `.1` 为 COMPLETED。失败是否为预期负向检查，现有证据不足；不能根据顶层完成状态推断子步骤全部通过。 |
| 当前队列 | PASS（查询可用） | `squeue -u ybgao` 有历史作业 196913、196678，均 PENDING。队列非空；这些名称和 ID 本身不能证明与当前 run_id 关联，也不能替代派发回执对账。 |
| 当前分区与目标节点 | PASS（状态查询） | `sinfo` 显示 gpu19 在 compute，状态 mix；不代表资源即时可分配或用户授权通过。 |
| 凭据是否可被实际提交会话读取 | UNKNOWN（本次未复验） | 上轮检查 shell 缺少环境变量只是该进程的事实，不证明所有登录方式或研究人员实际提交会话均缺失；也不证明凭据文件错误。本次没有加载或输出密钥。 |

### 子步骤失败的证据边界

对 `check-235495.log` 检索基础设施错误关键词，仅匹配到 storage_guard 源码哈希条目，未取得 `.2` 失败原因。关键词未命中不代表不存在错误。需将 `.2` 的标准输出、标准错误与原验收脚本对应起来，确认是否是预期失败测试；在取得这组证据前，该项保留 UNKNOWN，不认定提交器存在 Bug。

### 必须由环境维护者补齐的事项

- **P0：** 通过可信管理渠道核实 gpu19 主机指纹并按组织流程修复信任记录；取得该节点正式路径的容量、inode、挂载及目录权限证据。不要关闭主机密钥校验。
- **P0：** 运维确认登录节点根分区满的原因并恢复容量，完成后重新采样；本轮不删除用户或系统数据。
- **P0：** 明确 `.2` 失败的原因及对验收结论的影响。只有证明其为预期检查，或完成组织认可的整改验收后，才能关闭该项。
- **P0：** 由凭据所有者在实际使用会话确认变量可读和账户范围；只记录是否配置，不能记录密钥。由项目负责人确认实验范围和预算。用户已说明环境获授权，但本次操作范围仍仅为检查，不能解释为允许派发。
- **P1：** 后续状态报告同时记录作业总状态与子步骤状态；明确标记采样节点、采样时间、证据来源及未复验项。
- **P2：** 由维护者评估为登录节点磁盘容量建立告警，避免只在运行前发现容量耗尽。

### 重新评审标准

必须分别证明目标节点存储、持久化写入条件、实际会话凭据可读、授权范围、作业资源条件及历史验收异常解释均满足要求。现有就绪报告不替代这些条件；没有新的正式作业证据时，Agent 已执行、评分已完成和官方任务成功均不得标记为 PASS。本次未运行正式派发入口，未查询或展示任务载荷。
