# CyberGym 离线结果报告

在项目目录运行以下命令，将静态验收结果整理为带独立评测状态的 JSON 和 CSV。

```bash
python3 -B -m official_report \
  --report runs/RUN_ID/report.json \
  --output runs/RUN_ID/official-offline
```

默认读取报告旁边的 `manifest.json`，也可以使用 `--manifest` 指定原始清单。
输出目录必须尚未存在。报告记录原始报告、配置清单以及可用输入快照的 SHA-256。
已有运行的角色日志与实验文件保留在原目录中。报告中的 `output_tokens` 是非推理的生成量，
`completion_tokens` 是服务商报告的全部生成量。推理用量无法完整核对时，报告保留空值。

## 导入已有评测记录

有独立取得的结果文件时，可以额外传入 `--links LINKS.json`。链接文件的字段为
`schema_version`（整数 1）、`run_id`（原报告的父目录名）、`report_sha256` 和
`records`。每个链接条目包含 `task_id`、`harness_variant`、`path` 和 `sha256`。
`path` 是相对于链接文件目录的 JSON 路径。运行 ID、任务与配置组合、报告摘要及每份
结果的摘要必须全部吻合。该关联由操作者提供，摘要可检验内容完整性和关联一致性。

沿用现有的 `selected_at_1` 离线格式。导入保留 `completed` 记录中的显式布尔判定。
可选 `vul_exit_code`、`fix_exit_code` 从 `selected_at_1` 中读取，缺失时保留为空。
只有显式判定才会产生 `verified_successful` 或 `verified_unsuccessful`。
基础设施错误或缺少判定时写入 `verification_error`，没有关联记录时写入 `not_evaluated`。

静态运行的工件状态为 `artifact_missing`。接受状态和工件有效性为空。
`verification_executed_locally` 始终为 `false`。它与外部记录的 `verification_completed`
及独立的 `agent_completed` 字段分别记录。
