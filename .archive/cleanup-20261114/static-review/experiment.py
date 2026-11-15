"""Run comparable static-review experiments on a fixed teaching corpus."""

import argparse
from collections import Counter
import copy
import csv
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time

from agent_runtime.coding import DEEPSEEK_PROFILE, resolve_profile
from evaluation import EvaluationAdapter, FIXTURES, evaluation_hook, materialize, official_status
from review import no_duplicate_keys, save
from review_task import review_task
from task_materials import read_bytes


VARIANTS = ('single', 'manager_worker', 'manager_worker_critic', 'full')
BUDGET_KEYS = {'wall_seconds', 'max_requests', 'max_generated_tokens'}
PRICE_KEYS = {'input_cache_hit', 'input_cache_miss', 'output'}


class TaskBudget:
    """Share a wall, SDK request and generation budget across roles."""

    def __init__(self, config, cost_guard=None):
        self.deadline = time.monotonic() + config['wall_seconds']
        self.requests = config['max_requests']
        self.tokens = config['max_generated_tokens']
        self.cost_guard = cost_guard

    def role_profile(self, profile):
        wall = int(self.deadline - time.monotonic())
        if min(wall, self.requests, self.tokens) <= 0:
            raise ValueError('task budget exhausted before role start')
        role = copy.deepcopy(profile)
        budgets, policy = role['budgets'], role['policy']
        budgets['wall_seconds'] = min(budgets['wall_seconds'], wall)
        budgets['request_timeout_seconds'] = min(budgets['request_timeout_seconds'], wall)
        budgets['max_steps'] = min(budgets['max_steps'], self.requests)
        policy['max_total_generated_tokens'] = min(policy['max_total_generated_tokens'], self.tokens)
        policy['max_output_tokens'] = min(policy['max_output_tokens'], policy['max_total_generated_tokens'])
        return role

    def settle(self, execution, result):
        self.requests -= execution['model_requests']
        usage = result.get('usage')
        if usage is None or usage['accounting_failed'] or usage['uncertain_reserved_tokens']:
            raise ValueError('task budget cannot be settled: incomplete token accounting')
        self.tokens -= usage['generated_tokens']


def configuration(path):
    raw = read_bytes(path, 64 * 1024)
    cfg = json.loads(raw.decode(), object_pairs_hook=no_duplicate_keys)
    required = {'tasks', 'variants', 'model', 'budget', 'evaluator'}
    optional = {'pricing_usd_per_million', 'official_records'}
    if not isinstance(cfg, dict) or not required <= cfg.keys() or cfg.keys() - required - optional:
        raise ValueError('invalid experiment configuration fields')
    for name, allowed in (('tasks', FIXTURES), ('variants', VARIANTS)):
        values = cfg[name]
        if (not isinstance(values, list) or not values
                or not all(isinstance(item, str) and item in allowed for item in values)
                or len(values) != len(set(values))):
            raise ValueError(f'{name} must be a distinct nonempty list from {tuple(allowed)}')
    budget = cfg['budget']
    if not isinstance(budget, dict) or budget.keys() != BUDGET_KEYS:
        raise ValueError('budget must specify wall_seconds, max_requests, max_generated_tokens')
    for name, maximum in (('wall_seconds', 14400), ('max_requests', 1000), ('max_generated_tokens', 512000)):
        if type(budget[name]) is not int or not 1 <= budget[name] <= maximum:
            raise ValueError(f'budget.{name} must be a positive integer no greater than {maximum}')
    evaluator = cfg['evaluator']
    if not isinstance(evaluator, dict) or evaluator.keys() != {'mode', 'timeout_seconds'}:
        raise ValueError('evaluator must specify mode and timeout_seconds')
    adapter = EvaluationAdapter(evaluator['mode'], evaluator['timeout_seconds'])
    price = cfg.get('pricing_usd_per_million')
    if price is not None and (not isinstance(price, dict) or price.keys() != PRICE_KEYS or any(
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
            for value in price.values())):
        raise ValueError('provide nonnegative finite USD prices for input_cache_hit, input_cache_miss, output')
    records = cfg.get('official_records', {})
    if (not isinstance(records, dict) or any(task in FIXTURES or not isinstance(record, str)
                                             for task, record in records.items())):
        raise ValueError('official_records must map external task IDs to result paths')
    imports = {task: official_status(task, path.parent / record) for task, record in records.items()}
    return cfg, sha256(raw).hexdigest(), adapter, imports


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def ledger(output, prices, scripted):
    counted = []
    requests = upstream_attempts = roles = 0
    incomplete = False
    for folder in sorted(output.glob('*/execution')):
        ledger_path, execution_path = folder / 'usage.json', folder / 'execution.json'
        roles += 1
        if execution_path.is_file():
            requests += load_json(execution_path)['model_requests']
        else:
            incomplete = True
        if not ledger_path.is_file():
            incomplete = True
            continue
        usage = load_json(ledger_path)
        incomplete |= (usage['summary']['accounting_failed']
                       or bool(usage['summary']['uncertain_reserved_tokens']))
        for row in usage['requests']:
            upstream_attempts += row.get('attempt_count', int(row.get('backend_dispatched', False)))
            if row['state'] == 'accounted':
                counted.append(row['usage'])
            elif row.get('backend_dispatched'):
                incomplete = True
    prompt = sum(row['prompt_tokens'] for row in counted)
    generated = sum(row['completion_tokens'] for row in counted)
    cached = (sum(row['cached_tokens'] for row in counted)
              if all('cached_tokens' in row for row in counted) else None)
    cost = None
    if prices is not None and not incomplete and not scripted:
        if cached is not None:
            cost = (cached * prices['input_cache_hit'] + (prompt - cached) * prices['input_cache_miss']
                    + generated * prices['output']) / 1_000_000
        elif prices['input_cache_hit'] == prices['input_cache_miss']:
            cost = (prompt * prices['input_cache_miss'] + generated * prices['output']) / 1_000_000
    return {'roles_started': roles, 'sdk_requests': requests, 'upstream_attempts': upstream_attempts,
            'prompt_tokens': prompt, 'generated_tokens': generated, 'cached_prompt_tokens': cached,
            'usage_complete': not incomplete, 'estimated_cost_usd': cost}


def run_case(task, data, readme, variant, adapter, cfg, output, fixture_url, profile):
    start = time.monotonic()
    exit_code = review_task(
        task.task_id, data, readme, [task.source_name], output, sys.executable,
        mapper=variant == 'full', critic=variant in ('manager_worker_critic', 'full'),
        variant=variant, task_budget=TaskBudget(cfg['budget']), fixture_url=fixture_url,
        frozen_profile=profile,
        evaluate_worker=lambda root, analysis: evaluation_hook(task, adapter, root, analysis))
    events_file = output / 'events.jsonl'
    events = [json.loads(line) for line in events_file.read_text().splitlines()] if events_file.is_file() else []
    last = events[-1] if events else {}
    report = load_json(output / 'report.json') if exit_code == 0 else None
    expected = (task.source_name, task.expected_line)
    locations = {(finding['file'], finding['line']) for finding in report['findings']} if report else set()
    artifact = output / 'agent_result.json'
    local = (load_json(artifact)['verification'] if artifact.is_file()
             else {'origin': adapter.mode, 'status': 'not_evaluated'})
    return {
        'task_id': task.task_id, 'variant': variant, 'output': str(output),
        'review_status': 'completed' if report is not None else last.get('status', 'failed'),
        'failure_reason': ((last.get('error') or last.get('reason') or last.get('status', 'failed'))
                           if exit_code else local.get('detail') if local['status'] not in ('pass', 'simulated') else None),
        'exit_code': exit_code, 'elapsed_seconds': round(time.monotonic() - start, 4),
        'evaluation_origin': local['origin'], 'evaluation_status': local['status'],
        'evaluation_elapsed_seconds': local.get('elapsed_seconds'),
        'official_status': 'not_evaluated', 'official_success': None,
        'expected_location_found': expected in locations if report is not None else None,
        'other_locations_reported': len(locations - {expected}) if report is not None else None,
        **ledger(output, cfg.get('pricing_usd_per_million'), fixture_url is not None),
    }


def run_experiment(config, output, scripted):
    cfg, config_hash, adapter, imports = configuration(config)
    profile = resolve_profile(DEEPSEEK_PROFILE)
    if cfg['model'] != profile['model']['served_name']:
        raise ValueError('configured model does not match the frozen DeepSeek profile')
    output = output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    paths = [Path(__file__), Path(__file__).with_name('review_task.py'),
             Path(__file__).with_name('review.py'), Path(__file__).with_name('evaluation.py'),
             Path(__file__).with_name('teaching_checks.py'), Path(__file__).with_name('task_materials.py')]
    runtime = Path(__file__).parent / 'agent_runtime'
    paths += [runtime / name for name in ('coding.py', 'deepseek.py', 'gateway.py', 'coding_relay.py')]
    if scripted:
        paths.append(Path(__file__).with_name('experiment_fixture.py'))
    save(output / 'manifest.json', {
        'config': cfg, 'config_sha256': config_hash, 'profile': profile,
        'model_mode': 'scripted' if scripted else 'deepseek_api',
        'source_sha256': {path.name: sha256(path.read_bytes()).hexdigest() for path in paths},
    })
    if imports:
        save(output / 'external_official_results.json', imports)

    rows = []
    with (output / 'results.jsonl').open('x', encoding='utf-8') as stream:
        for task_id in cfg['tasks']:
            directory = output / 'tasks' / task_id.replace(':', '-')
            task, data, readme = materialize(task_id, directory)
            for variant in cfg['variants']:
                target = directory / variant
                if scripted:
                    from experiment_fixture import scripted_model
                    with scripted_model(task) as url:
                        row = run_case(task, data, readme, variant, adapter, cfg, target, url, profile)
                else:
                    row = run_case(task, data, readme, variant, adapter, cfg, target, None, profile)
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                stream.flush()
    with (output / 'results.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {}
    for variant in cfg['variants']:
        group = [row for row in rows if row['variant'] == variant]
        costs = [row['estimated_cost_usd'] for row in group]
        summary[variant] = {
            'tasks': len(group),
            'review_status': dict(Counter(row['review_status'] for row in group)),
            'evaluation_status': dict(Counter(row['evaluation_status'] for row in group)),
            'expected_locations_found': sum(row['expected_location_found'] is True for row in group),
            'other_locations_reported': sum(row['other_locations_reported'] or 0 for row in group),
            'elapsed_seconds': round(sum(row['elapsed_seconds'] for row in group), 4),
            'sdk_requests': sum(row['sdk_requests'] for row in group),
            'estimated_cost_usd': sum(costs) if all(cost is not None for cost in costs) else None,
            'official_evaluated': 0,
        }
    save(output / 'report.json', {'model_mode': 'scripted' if scripted else 'deepseek_api',
                                  'summary': summary, 'rows': rows})
    return int(any(row['exit_code'] or row['evaluation_status'] not in ('pass', 'simulated') for row in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--live', action='store_true', help='use configured paid API on teaching fixtures')
    args = parser.parse_args()
    try:
        return run_experiment(args.config, args.output, scripted=not args.live)
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
