# CyberGym / DeepSeek 人工运行手册

版本：2026-11-13。适用本轮 `unified-migration-20261113T032648Z-ca5b9e`。命令均在 Linux 服务器上执行，不在本地 PowerShell 中直接粘贴。**本审计没有执行任何正式提交、模型调用或评分命令。**

每段标注副作用。正式启动部分是源码审计得出的调用方式，尚未通过一次正式本轮 Rollout 实测，标为“待验证”。不要把可执行语法等同于已验证运行成功。

## A. 环境与身份检查

### A1. 建立本轮 shell 变量【只设置变量；无作业、模型或评分】

```bash
cd /home/ybgao/2026_cyber_mutiagent
export CG_ROOT=/home/ybgao/2026_cyber_agents
export CG_PROJECT=/home/ybgao/2026_cyber_mutiagent
export CG_PY="$CG_ROOT/safegym_solver/.venv/bin/python"
export CG_RUN="$CG_ROOT/safegym_solver/runs/unified-migration-20261113T032648Z-ca5b9e"
export CYBERGYM_BENCHMARK_CONFIG="$CG_PROJECT/outputs/deepseek_baseline/benchmark-config.json"
```

CG_* 是手册便利变量，程序只认识其中 `CYBERGYM_BENCHMARK_CONFIG`。依据：config.py:config_path。忘记覆盖变量会读取默认100题配置；因此每次提交前必须打印题单确认。

### A2. Python、配置和任务【只读；无作业、模型或评分】

```bash
"$CG_PY" --version
"$CG_ROOT/.venv-deepseek-harness/bin/python" --version
"$CG_PY" -B "$CG_ROOT/benchmark_runner/run.py" check-config cybergym
"$CG_PY" -B - <<'PY'
import json, os
from pathlib import Path
c=json.loads(Path(os.environ['CYBERGYM_BENCHMARK_CONFIG']).read_text())
print('tasks:', c['inputs']['task_ids'], 'level:', c['inputs']['level'])
print('verification_report:', c['verification_report'])
for task in c['inputs']['task_ids']:
    directory=Path(c['inputs']['data_root']) / task.replace(':','/')
    for name in ('description.txt','repo-vul.tar.gz'):
        p=directory/name
        print(task, name, 'exists=',p.is_file(), 'bytes=',p.stat().st_size if p.is_file() else None)
PY
```

预期：check-config passed，仅 `arvo:3938`、level1、两个非空文件；模型环境Python3.12。配置检查并不调用模型，也不证明全部依赖/镜像可运行。错误时核对解释器、配置覆盖路径和任务ID，不创建空文件冒充数据。

### A3. 就绪和既有作业【只读；无新作业、模型或评分】

```bash
"$CG_PY" -B "$CG_ROOT/benchmark_runner/run.py" status cybergym
squeue -u ybgao
sacct -j 235495 -X --format=JobID,JobName,State,ExitCode,Start,End,NodeList
```

依据：run.py:verify_evidence/main，Slurm查询。预期本轮status passed、run_dir与CG_RUN一致；235495是离线验收作业。此命令重新核对身份，可能因文件变化而返回78/not_ready。不要把旧readiness.json作为永不过期的授权。

### A4. 服务、镜像和空间条件【只读检查；正式服务不需手动预启】

```bash
test -r "$CG_ROOT/safegym_solver/config/<deepseek-credential>.env" && echo 'credential file readable'
test -r "$CG_RUN/frozen/code/config/server.env" && echo 'server configuration readable'
test -f "$CG_RUN/frozen/code/scripts/storage_guard.py" && echo 'storage guard present'
ls -ld "$CG_RUN/frozen/code/cybergym-server-data/arvo/3938/"{vul,fix}/{out,libs}
sinfo -N -n gpu19 -o '%N %t %m %C'
```

上述不检查凭据有效性，不打印密钥，不启动评分服务。镜像清单和binary完成标记已在本审计只读核验；实际加载仍由作业内核查。

正式脚本使用节点 `/tmp`，不是smoke的 `/dev/shm`。在研究者**已有合法分配且允许使用的节点会话**中，可以只读执行 `df -h /tmp`、`df -i /tmp`；登录节点的df不能替代计算节点。没有节点会话时不要为了执行本审计而新申请作业；留待获准的运维检查。现有storage_guard还检查home配额，最终以作业内resource-gate.json为准。历史gpu19空间不足记录需要重新核对。

### A5. 只有身份失效或题单变更时重新验收【人工授权后；会提交Slurm；无真实API；本轮未执行】

```bash
"$CG_PY" -B "$CG_ROOT/benchmark_runner/run.py" check cybergym
```

依据：entry.py:freeze/main。会创建新冻结目录并提交 `check.sbatch`，使用本地模型测试真实SDK工具隔离。会更新最近验收状态；旧报告可能因此不再可派发。当前既有passed证据有效时，不要无理由执行此命令。新批次运行目录必须从新verification_report读取，而不能继续使用旧CG_RUN。

检查超时、源码哈希变化、依赖不一致时，保留dispatch.json、verification.json和check日志；不要改验收报告为passed。当前check.sbatch默认节点为gpu01，本轮235495历史上由调度更新至gpu19；新check的实际节点以其记录为准。

## B. 本轮单任务正式派发【待验证；人工获授权后才执行】

### B1. 先检查是否已经派发【只读】

```bash
"$CG_PY" -B - <<'PY'
import hashlib,json,os
from pathlib import Path
c=json.loads(Path(os.environ['CYBERGYM_BENCHMARK_CONFIG']).read_text())
proof=Path(c['verification_report']); report=json.loads(proof.read_text())
run=Path(report['run_dir'])
receipt=Path(os.environ['CG_ROOT'])/'benchmark_runner/runs'/('cybergym-submit-'+hashlib.sha256(proof.read_bytes()).hexdigest()+'.json')
print('run:',run,'tasks:',c['inputs']['task_ids'])
print('receipt:',receipt,'exists:',receipt.exists())
if receipt.exists():
    value=json.loads(receipt.read_text()); print('receipt_status:',value.get('status'),'exit_status:',value.get('exit_status'))
for p in sorted(run.glob('task-dispatch-*.json')):
    try:
        v=json.loads(p.read_text()); print(p.name,'exit_status:',v.get('exit_status'),'sbatch_stdout:',v.get('stdout','').strip())
    except (ValueError,OSError): print(p.name,'incomplete receipt: investigate Slurm before any retry')
PY
```

如果发现submitted/already_dispatched、dispatching、dispatch_failed、dispatch_requires_review或不完整逐题回执，先核查Slurm。不能删除回执再强行重跑。当前审计时两类回执均不存在。

### B2. 注入已有凭据【仅设置本进程环境；不调用API；不显示值】

```bash
set +x
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    source "$CG_ROOT/safegym_solver/config/<deepseek-credential>.env"
fi
export DEEPSEEK_API_KEY
test -n "${DEEPSEEK_API_KEY:-}" || { echo 'DEEPSEEK_API_KEY missing' >&2; exit 1; }
```

依据：当前profile.api_key_env与既有PROJECT/run_deepseek.sh的取凭据方式。只source研究者信任的原有配置，不把密钥写进命令参数、文档、提交回执或shell调试输出。entry.submit自身不读取该env文件，必须在启动调度器之前设置变量。Slurm默认继承提交环境；本站若改变export策略，需要运维确认，不能悄悄绕过。

### B3. 正式启动【会提交1个正式作业；作业中会调用API和评分；本轮未执行／待验证】

```bash
"$CG_PY" -B "$CG_ROOT/benchmark_runner/run.py" submit cybergym --allow-external-api \
  > "$CG_PROJECT/outputs/deepseek_baseline/manual-submit.stdout.log" \
  2> "$CG_PROJECT/outputs/deepseek_baseline/manual-submit.stderr.log"
```

这两个输出文件须确认不存在后再用；再次调查应使用新日志名，避免覆盖。输出重定向使stdout非TTY，避免提交后自动进入已知不适用于DeepSeek的progress watch。

**仅在单题config确认、有效授权、凭据和存储条件满足后执行。** 不提供 `--task-id`、`--run-dir`、`--concurrency` 或 `--resume`，因为主CLI没有这些选项。不要直接调用entry.py或手工设置BENCHMARK_RUNNER_DISPATCH。

正常返回：顶层JSON status=submitted、exit_status=0；`R/task-dispatch-1.json` exit_status=0，stdout给出JobID。本步骤只等提交完成，不等模型执行完成。missing credential、source changed、sbatch拒绝等在总回执/stdout/stderr中；回执空白/dispatching时按未知状态处理。

### B4. 获取Job ID与跟踪【只读；已有作业查询】

```bash
CG_JOB=$("$CG_PY" -B - <<'PY'
import json,os
from pathlib import Path
r=json.loads((Path(os.environ['CG_RUN'])/'task-dispatch-1.json').read_text())
if r.get('exit_status') != 0: raise SystemExit('submission did not succeed')
job=r['stdout'].strip().split(';')[0]
if not job.isdigit(): raise SystemExit('invalid or ambiguous sbatch receipt')
print(job)
PY
)
squeue -j "$CG_JOB"
sacct -j "$CG_JOB" --format=JobID,State,ExitCode,Elapsed,NodeList
scontrol show job "$CG_JOB"
```

依据：entry使用sbatch --parsable。squeue空可能已完成或失败，用sacct确认。不要用235495代替正式JobID。scontrol的Command、StdOut应指向同一CG_RUN。

预期结构：

```text
RUN/
  task-dispatch-1.json
  task-1-JOBID.log
  worker-logs/1/
    storage.json resource-gate.json worker-result.json
    score/serve.log score/poc.db
  results/deepseek/tasks/01/
    attempt.json execution-command.json stdout.log stderr.log
    workspace/                         # 含Agent自行产出的工件，不要手工修改
    execution/summary.json model-usage.json ...
    score/requests.jsonl requests-started.jsonl requests/...
    frozen-final-selection.json fixed-evidence.json
    official-records.json task-result.json
```

注意：上图 `execution/summary.json model-usage.json` 都在execution目录；评分服务日志/DB在worker-logs，而Agent提交网关日志在results下score目录。

### B5. 检查结果【只读；白名单字段，不显示载荷】

```bash
"$CG_PY" -B - <<'PY'
import json,os
from pathlib import Path
run=Path(os.environ['CG_RUN'])
plan=json.loads((run/'run-plan.json').read_text())
harness=plan.get('harness') or json.loads((run/'frozen/profile.json').read_text())['harness']
for a in plan['attempts']:
    n=a['number']; d=run/'results'/harness/'tasks'/f'{n:02d}'
    print('task',n,a['task_id'])
    for p,keys in [(d/'task-result.json',('status','success','termination_reason','generated_tokens','accounting_failed','started_at','finished_at')),
                   (d/'execution/summary.json',('harness','status','exit_code','elapsed_seconds')),
                   (run/'worker-logs'/str(n)/'worker-result.json',('status','success','body_exit_status','last_stage','cleanup_status','cleanup_exit_status','storage_status'))]:
        if p.is_file():
            v=json.loads(p.read_text()); print(p.name,{k:v.get(k) for k in keys})
        else: print(p.name,'not present')
    p=d/'official-records.json'
    if p.is_file():
        rows=json.loads(p.read_text())
        print('official_record_count',len(rows))
        for r in rows: print({k:r.get(k) for k in ('task_id','agent_id','poc_id','poc_hash','poc_length','vul_exit_code','fix_exit_code')})
PY
```

这是手册提供的透明只读查询，不是项目新增的生产CLI，也不重算或覆盖官方结果。检查命令的结构与实际结果格式对应，正式非空结果分支仍待本轮运行验证。

## C. 批量 Benchmark【人工新建配置；所有启动步骤待验证】

项目支持config.inputs.task_ids列表，entry按列表顺序编号后逐个sbatch。没有array节流、最大并发、resume、skip-completed参数。详见batch_evaluation_plan.md。

新批次先在新的输出目录复制配置，修改任务列表和verification_report；不能修改本轮已验收的配置。以下只是配置写入示例，不提交作业：

```bash
export CG_BATCH_DIR="$CG_PROJECT/outputs/deepseek_baseline/batch-$(date +%Y%m%dT%H%M%S)"
export CG_BATCH_CONFIG="$CG_BATCH_DIR/benchmark-config.json"
"$CG_PY" -B - <<'PY'
import json,os
from pathlib import Path
src=Path(os.environ['CYBERGYM_BENCHMARK_CONFIG'])
dest=Path(os.environ['CG_BATCH_CONFIG']); dest.parent.mkdir(parents=True,exist_ok=False)
c=json.loads(src.read_text())
# 示例ID：每题的数据、binary和镜像完整性仍需研究者确认；不是已验证的三题组合。
c['inputs']['task_ids']=['arvo:3938','arvo:10013','arvo:65996']
c['verification_report']=str(dest.parent/'cybergym-verification.json')
dest.write_text(json.dumps(c,indent=2)+'\n')
PY
export CYBERGYM_BENCHMARK_CONFIG="$CG_BATCH_CONFIG"
"$CG_PY" -B "$CG_ROOT/benchmark_runner/run.py" check-config cybergym
```

然后经授权运行A5的check（提交验收作业），读取新报告的run_dir更新CG_RUN，重新执行status、B1/B2，再执行B3。submit会派发全部列表，单题列表就是单任务运行。模型与工具profile没有改动。

并发控制：若须严格单并发，使用串行的单题新批次并等待前一题结束后再check下一轮；这是人工管理方案，不是已有`--concurrency 1`。若一次提交多题，其运行并发由Slurm资源/QoS决定，未经核验不能假定只有一个API调用。变更共享最新验收记录可能使前一轮尚未提交的证据失效，避免并行check。

失败恢复：先保存全部回执，匹配sacct/Command/StdOut，区分未提交、排队、运行、完成、失败和未知。当前没有原地resume；对确认未完成且研究者决定重试的任务创建新批次/新attempt，保留与原任务的关联。已执行过的重试不能混作首次pass@1。不要删除attempt/receipt、改题号或覆盖旧结果。

## D. 官方结果与数据库

### D1. 只读查数据库【无评分请求、无模型、无写入】

```bash
"$CG_PY" -B - <<'PY'
import os,sqlite3
from pathlib import Path
run=Path(os.environ['CG_RUN'])
for p in sorted((run/'worker-logs').glob('*/score/poc.db')):
    print('database',p)
    with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as db:
        for row in db.execute('SELECT task_id,agent_id,poc_id,poc_hash,poc_length,vul_exit_code,fix_exit_code FROM poc_records'):
            print(row)
PY
```

只读URI避免在路径错误时创建空DB。表和字段依据官方pocdb.py。无DB时无输出不代表成功；对照worker stage。不要查询/展示候选字节或完整模型提示词来做普通状态检查。

### D2. 最终判定

项目`official_success()`要求最终记录唯一、候选SHA256一致、两侧退出码都是合法整数，排除126/127/137。规则是vul不在(0,300)且fix=0。300代表超时，不算成功；两侧非零不成功。原字段原样保存。

先读Agent execution/summary，再读task-result和official-records，最后与sacct/worker核对。正常退出但没有有效final_submission时，task-result可能completed/false而官方records为空；这属于无有效候选，不能说官方双侧评分完成。fixed证据在fixed-evidence.json，仅限有需要的宿主审计，不回注Agent。

统计应分别报告计划数、真实rollout数、Agent完成数、有效官方评测数、官方成功数、工程失败数和取消数。已评估成功率=成功数/有效评测数；完整无工程缺失的一次性题单才报告全题单pass@1，并说明无候选的处理。分母为0时记null，不写0%。“API 200”“sbatch接受”“Slurm COMPLETED”“Agent完成”“官方成功”分别保存。

### D3. 报告与排行榜

当前没有核实可通用于本轮的Baseline聚合CLI。不要运行写死旧路径的summarize.py；不要把普通输入的export_e2e_validation.py或collect_dynamic.py用于统一正式RUN。研究者可依据B5/D1的只读元数据整理报告，并引用原始文件；生产级聚合和错误分类是后续工程待办。

已有 `scripts/export_official_submission.py --run-dir ... --output-dir ... [--publication-manifest ...] [--package]` 只做本地导出，不自动上传；其对本轮正式目录的兼容性未验收。公开提交还需完整任务覆盖、身份/方法说明、人工审阅的公开文件清单、轨迹及成本等。不要把本地官方评分视为公开排行榜提交。

## E. 常见错误

| 现象 | 排查与处理 |
|---|---|
| not_ready / source changed | 核对配置覆盖与报告run_dir；保留旧证据，必要时授权重新check，不改hash |
| missing API credential | 确认source且export指定变量，不打印值；若总回执已创建，先按B1调查，不能直接删后重提 |
| dispatch_requires_review / dispatching | 查询总/逐题回执、sacct、Command、StdOut，防止重复计费 |
| skipped_storage / resource_gate | 查看resource-gate与storage日志；空间、inode、配额或资源校验均可能触发，不是模型失败 |
| 无task-result而Slurm失败 | 看worker-result last_stage；setup/trap前失败可能只有Slurm日志，保留原始退出码 |
| scorer_start失败 | serve.log、binary完成标记、runner镜像、端口及Slurm cgroup；无需重跑Agent来修复评分设施 |
| completed但没有official记录 | 核对Agent最终选择与提交日志；可能无候选，不能虚构分数 |
| cleanup失败但Slurm COMPLETED | worker-result的cleanup_exit_status优先用于清理判断；不得据此改官方得分 |
| progress长期显示排队 | 已知OpenHands路径硬编码；用B4/B5核对，不因此重新提交 |

本手册中所有主动运行步骤都仅为操作说明。研究人员需先确认授权、费用与资源约束，才手动执行正式submit及必要的重验收。
