# 派发前置条件

## 本轮只读核验结果

- 本轮 config 为 `/home/ybgao/2026_cyber_mutiagent/outputs/deepseek_baseline/benchmark-config.json`，题单仅 `arvo:3938`，level=1。
- 正式 `run.py status cybergym`（设置上述配置覆盖变量）本轮再次返回 passed；身份清单中的 source_hashes 全部与当前源码一致。
- 隔离作业 235495：gpu19，2026-11-13 11:28:09—11:37:24，COMPLETED，0:0，9分15秒。原始 verification 与发布报告相同。该作业使用测试模型，不是正式 DeepSeek rollout。
- task 数据 `description.txt` 和 `repo-vul.tar.gz` 存在；官方 Loader 已在隔离检查生成 level1 工作区。task.json 使用 masked ID，管理记录使用真实 arvo ID；代码负责转换，不能手工替换。
- frozen/code 的 binary-extracted 标记经现有 `extraction_complete()` 验证为 true；3938/vul、3938/fix 下的 arvo、out、libs 存在，runner 均为 `cybergym/oss-fuzz-base-runner:latest`。
- squid 与四个 binary runner（latest、20200102、20190802、20220102）在冻结镜像清单中 complete，归档路径存在且可读。清单指向 `/home/yrdu/2026_cyber_agents/safegym_solver/downloads/images/`；本轮仅确认可读，未重新对大型归档计算 SHA256，实际加载时原脚本仍会校验。
- `$CG_ROOT/safegym_solver/config/<deepseek-credential>.env` 可读；没有打印密钥，没有进行 API 可用性探测。文件可读不等于凭据有效或有余额。
- 正式提交回执、逐题回执、正式 results 目录仍不存在；未发现本轮正式评分 DB。

## 保持的模型与资源参数

正式冻结 profile：`RUN/frozen/profile.json`。当前模型路由 deepseek-flash，requested_checkpoint 字段为 DeepSeek-V4.1-Flash-1017；有文档声明但无独立权重验证，不应据此宣称固定权重哈希。

| 项目 | 冻结值/实现 |
|---|---|
| Harness | deepseek；SDK/runtime 0.2.1rc2；Python 3.12；sdk-minimal；原生 bash |
| 模型预算 | max_steps=1000；wall_seconds=14400；request_timeout_seconds=1800 |
| 生成预算 | 单次 max_output_tokens=32768；总 generated tokens=512000 |
| 推理/采样 | thinking enabled，reasoning_effort=max，temperature=1.0，top_p=0.95；实际服务是否采纳由 API 决定 |
| Slurm | account=users，partition=compute，1节点1task，4 CPU，24G，05:00:00，不申请 GPU |
| 节点 | entry.submit 绑定验收报告 hostname，本轮 gpu19；无 CLI 换节点选项 |
| 启动监督 | Harness 启动最长900秒；运行预算后另有120秒退出宽限；调度器不能把启动时间当作模型思考时间 |
| 正式存储 | `/tmp/cybergym-UID/safegym-slurm-JOBID`；私有 runtime 位于 `/run/user/UID/cg-*`；常规日志归 home |

`max_steps` 的执行解释须以 Harness/网关请求计数为准，不等同于工具调用次数。步骤、Token、墙钟和 Slurm 时间分别记录。

## 当前仍需确认的 P0 条件

1. 正式存储门禁：`storage_guard.sample/check` 要求 home 和节点本地盘保留20 GiB、足够 inode，以及可验证的个人配额。此前 gpu19 的 `/tmp` 实测仅约5 GiB，而 smoke 使用 `/dev/shm`。这不是当前时点复测，但足以说明“smoke通过”不能证明正式 `/tmp` 条件满足。本轮没有 SSH 到计算节点或新建探针作业。需研究者在已有授权节点会话中检查；不能把 smoke 的 SAFEGYM_DYNAMIC_TMP 变量当成正式脚本支持的参数。
2. Slurm 环境须能继承有效 DEEPSEEK_API_KEY。entry.submit 在生成逐题回执前检查变量；不过顶层会先创建总回执，缺凭据会留下 dispatch_failed，影响重试。
3. 当前验收身份须保持一致。任意题单、profile、源码或依赖更改均可能使门禁拒绝，必须按现有 check 重建证据，不能编辑 hash 让校验通过。
4. 正式部署的 rootless Docker、binary runner、目标程序、真实 API 和最终候选协议尚未一起跑完首题。它们的联合可用性仍待正式首题验证。

隔离检查真实执行了 SDK 原生工具与本地评分协议夹具；它不验证模型余额、正式 binary runner 行为、节点 `/tmp` 容量，也不构成付费或正式任务授权。
