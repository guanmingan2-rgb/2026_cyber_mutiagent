# DeepSeek 多角色代码审查

Manager 拟定审查范围，Worker 根据源码快照提交审查报告，Reviewer 对照同一份源码复核。每个角色启动独立的 DeepSeek Harness 会话、工作目录和运行日志。Manager 只接收源码元数据，Worker 接收结构化 assignment，Reviewer 接收 Worker 的结构化报告。Reviewer 返回 `accept` 或 `revise`，供人工处理。

角色只读取提示词里的数据。SDK 的 Bash、文件和工作目录工具均关闭，模型中继也会拒绝携带工具定义的请求。这里的隔离是会话、输入和工作目录隔离，三个 SDK 进程仍以同一 Linux 账号运行。

模型、API 地址、采样、推理和预算继承原项目的 OpenHands 共享 profile。每个角色独立计量用量，可用 `--role-timeout` 显式缩短单角色时限。请在运行环境设置配置中 `api_key_env` 指定的变量。

```bash
cd /home/ybgao/2026_cyber_mutiagent
bash agent_runtime/install_deepseek.sh /绝对路径/python3.12
.venv-deepseek-harness/bin/python -B -m review /绝对路径/example.py \
  --output /绝对路径/review-001 --role-timeout 120
```

可直接使用与依赖锁匹配的 Python 3.12 虚拟环境。输入是一份 UTF-8 文本文件，上限 64 KiB。可选 `--focus` 指定审查关注点。输出目录必须是新目录。输出含源码快照、`events.jsonl`、各角色的执行记录与交接 JSON，以及三段都完成后的 `report.json`。角色失败或超时会停止后续调度。日志可能包含源码与模型输出，按源文件的访问要求保管。

本地验证使用回环假模型，启动真实 SDK：

```bash
.venv-deepseek-harness/bin/python -B -m smoke
```

## CyberGym 只读材料审查

```bash
.venv-deepseek-harness/bin/python -B -m review_task \
  --task-id arvo:123 --data-root /绝对路径/cybergym_data/data \
  --readme /绝对路径/任务工作区/README.md --source src/example.py \
  --output /绝对路径/task-review-001
```

`--source` 可重复，值是源码归档内的相对文件名。题库提供 `description.txt` 和
`repo-vul.tar.gz`，任务工作区提供 README。入口按允许名单流式读取归档成员。
最多选择 32 个文件，源码合计不超过 64 KiB，描述和 README 各不超过 16 KiB。
归档扫描上限为 1 GiB 解压后字节和 10 万个成员。

可选 `--mapper` 建立源码索引，`--critic` 审阅分派。Critic 要求修订时只复审一次。
Verifier 逐条复核审查发现，最终 Manager 只能选入通过复核的条目。
`task_snapshot.json` 保存只读材料和各文件哈希，`report.json` 包含最终发现及复核理由。
可选 `--official-result` 导入同题已有的官方 `selected_at_1` 记录，单独写入
`external_official_result.json`。导入只读取 JSON。工具关闭和角色超时规则与单文件入口一致。

使用合成题库和本地假模型执行验收：

```bash
.venv-deepseek-harness/bin/python -B -m smoke_task
```

## 教学回归和消融实验

真实 Level 1 数据的四组实验使用独立入口 `cybergym_experiment`。输入仅为
`description.txt`、`repo-vul.tar.gz` 中显式选择的源码和固定审查指令。
Verifier 只读取 Worker 的结构化报告、完成状态和源码元数据。所有用例的官方状态均为
`not_evaluated`。输出目录须为新目录，内含 `results.csv`、`results.jsonl`、
`report.json` 和逐角色日志。

```bash
/home/ybgao/2026_cyber_agents/.venv-deepseek-harness/bin/python -B -m cybergym_experiment \
  --data-root /home/ybgao/2026_cyber_agents/safegym_solver/cybergym_data/data \
  --task arvo:10013 src-vul/graphicsmagick/magick/version.c \
  --task oss-fuzz:368076871 src-vul/mruby/mrbgems/mruby-data/src/data.c \
  --output runs/level1-static
```

每个真实题目与配置组合共用 profile 的总时间、请求数和生成 token 预算。默认走本地脚本模型，计量值用于校验记账链路。加 `--live` 使用当前 DeepSeek profile，
把示例首行的 Python 路径替换为 `./run_deepseek.sh` 即可复用原项目的凭据配置。脚本优先使用调用进程已有的 `DEEPSEEK_API_KEY`。
可用 `--variant full` 只跑全角色组，
用 `--pricing` 传入与账号计费相符的每百万 token 单价 JSON。

### 真实 API 验收记录

在同一个命令中按验收顺序传入多个 `--task`，并使用
`--live --variant full --stop-on-error --max-cost-usd 1.00 --pricing deepseek-flash-peak-pricing.json`。
成本上限适用于整次命令。网关在每次实际发送前按最大上下文、当次输出上限和传入单价
保守预留，随后按已确认的用量结算；遇到不可确认的上游用量时保留该笔预留并停止继续调度。
随附的价格文件采用 DeepSeek Flash 公布的峰值美元单价，报告金额是估算上限。

`roles.csv` 和每个用例的 `role_metrics.json` 保存结束原因、推理与输出 token、可见输出字节、
结构化解析与交接状态。逐请求的 `execution/usage.json` 保存 SDK 入站和发往 DeepSeek 的
参数、重试、耗时与用量。发生 `finish_reason=length` 时停止当前用例并保留诊断数据。
所有官方状态继续标记为 `not_evaluated`。

## 教学题运行

`experiment.py` 使用两道内置教学题与固定模型 profile，支持 `single`、`manager_worker`、`manager_worker_critic` 和 `full`。每个题目与配置组合共享一份 Agent 时间、SDK 请求次数和生成 token 预算。未启用 Verifier 的组保留 Worker 发现，复核字段为 null。

```bash
.venv-deepseek-harness/bin/python -B -m experiment experiment.example.json --output /绝对路径/新实验目录
.venv-deepseek-harness/bin/python -B -m unittest -v test_experiment
```

默认使用回环脚本模型和真实 SDK。脚本模型结果只检查实验流程。显式加 `--live` 才会按原 DeepSeek 配置调用真实 API，输入范围仍限内置教学题。教学题以预标注文件和行号统计定位命中，用作静态审查的有限指标。

Worker 交回后，宿主将分析、固定测试清单和验证状态归档到 `agent_result.json`。本地评估器在独立进程组和空工作目录执行随项目提供的固定回归检查。工件 JSON 只绑定教学用例和哈希。进程沿用当前 Linux 账号。评估器不执行源码快照、模型建议或任意外部工件。mock 仅返回 `simulated`。

`manifest.json` 记录输入配置、冻结 profile 与代码哈希，`results.jsonl`、`results.csv` 和 `report.json` 记录逐题结果及分组汇总。调用成本按可选 `pricing_usd_per_million` 的 `input_cache_hit`、`input_cache_miss`、`output` 计算。计价信息或计量不足时保留 null。官方成绩栏默认 `not_evaluated`。可选 `official_records` 将题号映射到已有本地结果 JSON 路径，导入记录单独归档并保留原文件哈希。

## 官方动态评分链路验收

普通测试输入的 task → 真实 DeepSeek Harness → 官方 vulnerable/fixed 评分已通过单题验收。诊断、修改清单、状态语义和复现命令见 [动态评分报告](DYNAMIC_SCORING.md)。入口为 `run_dynamic.sbatch`；使用 `collect_dynamic.py` 联合核对调度状态和实际结果。该入口用于集成检查，不表示完整 benchmark 评测。
