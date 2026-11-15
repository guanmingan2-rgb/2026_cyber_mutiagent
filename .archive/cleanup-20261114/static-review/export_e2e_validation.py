"""Export evidence from an existing integration run; never call models or scorers."""
import argparse
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-id', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    solver = Path('/home/ybgao/2026_cyber_agents/safegym_solver')
    root = project / 'runs' / f'dynamic-{args.job_id}'
    result = root / 'result'
    def read(path):
        return json.loads(path.read_text())
    def commit(path):
        process = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True)
        return process.stdout.strip() if process.returncode == 0 else None
    report = read(result / 'report.json')
    score = read(result / 'score.json')
    collected = read(root / 'collected-status.json')
    task = read(result / 'task/task.json')
    profile = read(result / 'agent/profile.json')
    execution = result / 'agent/input_agent/execution'
    usage = read(execution / 'usage.json')
    adapter = read(execution / 'adapter-result.json')
    handoff = read(result / 'agent/input_agent/handoff.json')
    inputs = read(result / 'input-manifest.json')
    selection = read(result / 'final-selection.json')
    records = read(result / 'official-records.json')
    candidate_hash = digest(result / 'candidate.bin')
    candidate_matches_agent = (result / 'candidate.bin').read_bytes() == handoff['input_text'].encode('utf-8')
    data = Path(inputs['data_root']) / report['task_id'].replace(':', '/')
    hashes = {name: {'source': digest(data / name), 'snapshot': digest(result / 'task' / name)}
              for name in ('description.txt', 'repo-vul.tar.gz')}
    db_path = root / 'server_poc/poc.db'
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('SELECT * FROM poc_records WHERE poc_id=?', (score['poc_id'],))]
    http = {side: read(result / f'{side}-http.json') for side in ('vul', 'fix')}
    checks = {
        'scheduler_completed': collected['scheduler_state'] == 'COMPLETED' and collected['scheduler_exit_code'] == '0:0',
        'collector_completed': collected['chain_status'] == 'completed',
        'agent_completed': adapter['status'] == 'completed' and report['agent_status'] == 'completed',
        'real_api_accounted': bool(usage['requests']) and all(r['backend_dispatched'] and r['state'] == 'accounted' and r['http_status'] == 200 for r in usage['requests']),
        'level1': task['difficulty'] == 'level1',
        'source_matches_snapshot': all(v['source'] == v['snapshot'] == inputs['sha256'][k] for k, v in hashes.items()),
        'candidate_from_agent': candidate_matches_agent,
        'candidate_hash_matches': candidate_hash == score['candidate_sha256'] == selection['candidate_sha256'],
        'http_both_200': all(v['status_code'] == 200 for v in http.values()),
        'response_identity_matches': all(score['responses'][s]['task_id'] == task['task_id'] and score['responses'][s]['poc_id'] == score['poc_id'] for s in ('vul', 'fix')),
        'database_identity_matches': len(rows) == 1 and rows[0]['task_id'] == report['task_id'] and rows[0]['agent_id'] == task['agent_id'] and rows[0]['poc_hash'] == candidate_hash,
        'database_scores_match': len(rows) == 1 and all(rows[0][s + '_exit_code'] == score['selected_at_1'][s + '_exit_code'] == score['responses'][s]['exit_code'] for s in ('vul', 'fix')),
        'export_matches_database': len(rows) == 1 and len(records) == 1 and all(rows[0][k] == records[0][k] for k in ('agent_id', 'task_id', 'poc_id', 'poc_hash', 'poc_length', 'vul_exit_code', 'fix_exit_code')),
    }
    run_id = root.name
    common = {'run_id': run_id, 'job_id': args.job_id, 'task_id': report['task_id'],
              'exported_at': datetime.now(timezone.utc).isoformat(), 'source_run': str(root)}
    args.output.mkdir(parents=True, exist_ok=False)
    def save(name, value):
        (args.output / name).write_text(json.dumps({**common, **value}, ensure_ascii=False, indent=2) + '\n')
    save('run_config.json', {'scope': 'existing_integration_smoke',
        'command': 'sbatch --nodelist=gpu19 --time=00:15:00 run_dynamic.sbatch arvo:3938',
        'project_git_commit': commit(project), 'solver_git_commit': commit(solver.parent),
        'cybergym_git_commit': commit(solver / 'cybergym'),
        'code_manifest': read(root / 'code-manifest.json'),
        'model': profile['model'], 'api_base_url': profile['base_url'],
        'policy': profile['policy'], 'budgets': profile['budgets'], 'runtime': profile['runtime'],
        'slurm': collected, 'resources': {'cpus': 4, 'memory_gib': 24, 'limit_minutes': 15},
        'isolation_reports': [str(root / x) for x in ('runtime-probe.json', 'proxy.json', 'firewall.json')]})
    save('task_info.json', {'difficulty': task['difficulty'], 'masked_task_id': task['task_id'],
        'agent_id': task['agent_id'], 'data_root': inputs['data_root'], 'file_hashes': hashes,
        'description': (result / 'task/description.txt').read_text(),
        'agent_input_files': ['description.txt', 'README.md'],
        'source_archive_loaded_by_task_generator': True, 'source_archive_read_by_agent': False,
        'input_manifest': inputs, 'database_created_at_raw': rows[0]['created_at'] if rows else None})
    save('agent_execution.json', {'status': adapter['status'], 'api_real_call': checks['real_api_accounted'],
        'model_requested': profile['model']['served_name'],
        'models_returned': sorted({r.get('response_model', 'not_recorded') for r in usage['requests']}),
        'usage': usage, 'output': handoff, 'tool_calls_enabled': False,
        'tools_exercised': [], 'preset_artifact_used': False, 'candidate_origin': 'this_run_agent_handoff',
        'tool_limit_reason': 'Existing integration entry disables filesystem and persistent Bash tools.',
        'events_path': str(result / 'agent/events.jsonl'), 'trajectory_path': str(execution / 'native/trajectory.jsonl'),
        'execution_path': str(execution), 'elapsed_seconds_task_agent_evaluator': report['elapsed_seconds']})
    save('evaluation_result.json', {'checks': checks, 'engineering_acceptance': all(checks.values()),
        'artifact_sha256': candidate_hash, 'artifact_bytes': (result / 'candidate.bin').stat().st_size,
        'submission_id': score['poc_id'], 'official_response_and_rule_result': score,
        'http': http, 'database_path': str(db_path), 'database_rows': rows,
        'official_export': records, 'full_benchmark_acceptance': False,
        'benchmark_reproduction_claimed': False,
        'interpretation': 'Real task + real model-generated ordinary input. Validates existing engineering chain, not autonomous benchmark solving.'})
    passed = all(checks.values())
    text = f'''# DeepSeek 端到端工程验收

本轮工程链路验收：{'通过' if passed else '未通过'}。真实任务 `arvo:3938`，run_id `{run_id}`，Slurm `{args.job_id}`，节点 `{collected['node']}`，状态 `{collected['scheduler_state']}`，耗时 `{collected['elapsed']}`。

## 验收范围

执行已有 `run_dynamic.sbatch` → `cybergym_dynamic.main()`，未修改 Agent 策略、任务定义、模型配置或官方评分器。测试工件来自本轮真实 DeepSeek 输出，没有预置或人工补写候选。当前入口是普通输入集成模式，文件与 Bash 工具关闭；只把任务描述和官方 README 交给模型，源码归档由 Task Loader 保存但没有让模型阅读。因此本轮通过的是现有链路的工程验收，不是完整自主解题 Benchmark 验收，也不证明工具调用循环可用。

## 实际命令

```bash
cd /home/ybgao/2026_cyber_mutiagent
sbatch --nodelist=gpu19 --time=00:15:00 run_dynamic.sbatch arvo:3938
```

GPU 未申请；作业使用 4 CPU、24 GiB、15 分钟上限。Agent 沿用当前配置，单角色超时 300 秒。重跑会生成新的作业目录和 Agent ID。

## 模块状态

| 模块 | 本轮证据 |
|---|---|
| Task Loader | 官方 level1 工作区；原数据与快照的 SHA256 核对：{checks['source_matches_snapshot']} |
| DeepSeek API | 真实派发并记账：{checks['real_api_accounted']}；请求模型 `{profile['model']['served_name']}`；返回模型见 agent_execution.json |
| Harness | `{adapter['status']}`；输出直接保存为候选，候选来源校验：{candidate_matches_agent} |
| 官方 Evaluator | vul HTTP {http['vul']['status_code']}，fix HTTP {http['fix']['status_code']} |
| Result Parser | 原始结构、masked task ID、poc_id、同一候选哈希、双侧退出码均交叉核对 |
| SQLite / Report | 直接只读查询本轮数据库，和 HTTP、导出记录、报告一致：{checks['database_scores_match'] and checks['export_matches_database']} |
| Slurm | `{collected['scheduler_state']}` / `{collected['scheduler_exit_code']}`；未凭 HTTP 200 或单一退出码验收 |

候选 SHA256：`{candidate_hash}`。官方 poc_id：`{score['poc_id']}`。vul/fix 退出码：`{score['selected_at_1']['vul_exit_code']}/{score['selected_at_1']['fix_exit_code']}`。按已有官方规则计算的 `selected_at_1.success={score['selected_at_1']['success']}` 原样保留，但普通输入的判定不用于宣称自主漏洞复现成功或 pass@1。

用量：prompt tokens {usage['summary']['prompt_tokens']}，generated tokens {usage['summary']['generated_tokens']}，请求数 {usage['summary']['request_count']}。只报告 API 实际提供的数据，未独立验证服务端权重版本，未估算费用。时间戳与逐请求参数在 JSON 中保留。

## 调用关系和真实入口

- `/home/ybgao/2026_cyber_mutiagent/run_dynamic.sbatch`：Slurm、rootless Docker、镜像校验、隔离探针、服务启动与结束。
- `/home/ybgao/2026_cyber_mutiagent/cybergym_dynamic.py:main`：任务 → Agent → 工件 → 评分。
- `/home/ybgao/2026_cyber_mutiagent/dynamic_score_backend.py:main`：复用任务生成及 `/submit-vul`、`/submit-fix`、`/query-poc`。
- `/home/ybgao/2026_cyber_agents/safegym_solver/scripts/harness.py:make_task` → `cybergym.task.gen_task.generate_task`：真实任务生成。
- `/home/ybgao/2026_cyber_mutiagent/review.py:ReviewRun.role` → `agent_runtime/coding.py:run` → `agent_runtime/gateway.py:ModelGateway` → `agent_runtime/deepseek.py:main`：现有真实 SDK 和 API 调用。
- `/home/ybgao/2026_cyber_agents/safegym_solver/cybergym/src/cybergym/server/server_utils.py:submit_poc`：候选与 runner；同目录 `pocdb.py:update_poc_output`：逐侧提交 SQLite。
- `/home/ybgao/2026_cyber_mutiagent/collect_dynamic.py:classify`：Slurm 与实际结果联合判定。

## 修改与工程问题

本轮没有修改上述执行逻辑。新增 `export_e2e_validation.py`，只读核对证据并生成本次要求的五个文件；它不调用模型或评分器。原报告分散在多处、缺少同一处的模型/作业/数据库证据关联，本导出补齐了这些信息。

本次现有链路未发现阻断错误。工程边界仍包括：评分端口固定 18666，尚未验证同节点多作业并发；节点空间会变化；顶层项目没有 Git commit，使用运行代码哈希记录，原依赖仓库 commit 单独保留；作业退出不自动删除节点镜像暂存，后续需要按原项目清理规范管理容量。没有改动其他作业或历史结果。

## 后续完整 Benchmark 的条件

本次满足真实 Task 读取、真实 API、既有 Harness 流程、输出追踪、官方接口、返回解析、数据库和报告写回八项工程检查。满足的是当前单题普通输入模式的工程条件。

完整 Benchmark 仍需独立验收已有的工具型 Agent 入口、Level 1 源码访问与上下文隔离、工具执行及反馈循环、最终候选选择、任务预算和多任务调度；不能把当前工具关闭的入口当作完整求解器。需要明确任务清单与版本、可用镜像/磁盘、固定预算、模型版本依据、成本上限及并发端口管理。本轮没有开展这些额外实验，也没有优化模型得分。

## 证据位置

原始运行：`{root}`。`code-manifest.json` 保存运行代码 SHA256；`result/agent/` 保存调用参数、交接和轨迹；`result/*-http.json` 保存 HTTP 返回；`server_poc/poc.db` 保存数据库；`server.log` 保存服务日志；隔离证明见 `runtime-probe.json`、`proxy.json`、`firewall.json`。本导出不复制大型源码归档、候选或完整轨迹，不导出 API Key。
'''
    (args.output / 'integration_report.md').write_text(text, encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'checks': checks, 'engineering_acceptance': passed}))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
