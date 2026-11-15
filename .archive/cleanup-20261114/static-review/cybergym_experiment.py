"""Run tool-free defensive reviews of explicitly selected CyberGym Level 1 source."""

import argparse
from collections import Counter
from contextlib import nullcontext
import csv
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
from threading import Lock
from types import SimpleNamespace
import time

from agent_runtime.coding import DEEPSEEK_PROFILE, resolve_profile
from experiment import PRICE_KEYS, TaskBudget, VARIANTS, ledger, load_json
from review import save
from review_task import review_task
from task_materials import load_task


class CostGuard:
    """Reserve a worst-case charge before dispatch, including possible retries."""

    def __init__(self, limit, prices, context_tokens):
        self.limit = Decimal(str(limit))
        self.prices = {key: Decimal(str(value)) / 1_000_000 for key, value in prices.items()}
        self.max_input_rate = max(self.prices['input_cache_hit'], self.prices['input_cache_miss'])
        self.context_tokens = context_tokens
        self.charged = Decimal(0)
        self.pending = {}
        self.blocked = False
        self.lock = Lock()

    def reserve(self, request_id, output_tokens, attempts):
        per_attempt = self.context_tokens * self.max_input_rate + output_tokens * self.prices['output']
        with self.lock:
            reserved = sum(amount * count for key, (amount, count) in self.pending.items()
                           if key != request_id)
            if self.blocked or self.charged + reserved + per_attempt * attempts > self.limit:
                self.blocked = True
                return False
            self.pending[request_id] = per_attempt, attempts
            return True

    def settle(self, request_id, usage, uncertain_attempts, dispatched):
        with self.lock:
            per_attempt, _ = self.pending.pop(request_id)
            if usage is None:
                self.charged += per_attempt * max(uncertain_attempts, int(dispatched))
                self.blocked |= dispatched
                return
            prompt = usage['prompt_tokens']
            cached = usage.get('cached_tokens')
            input_cost = (prompt * self.max_input_rate if cached is None else
                          cached * self.prices['input_cache_hit']
                          + (prompt - cached) * self.prices['input_cache_miss'])
            self.charged += input_cost + usage['completion_tokens'] * self.prices['output']
            self.charged += per_attempt * uncertain_attempts
            self.blocked |= uncertain_attempts > 0


def role_records(case, events):
    by_role = {}
    for event in events:
        role = event['role']
        if role != 'host':
            by_role.setdefault(role, []).append(event)
    records = []
    for role, history in by_role.items():
        path = case / role / 'execution' / 'usage.json'
        requests = load_json(path)['requests'] if path.is_file() else []
        structured = next((event for event in history if event['status'] == 'structured_output'), {})
        finish = next((event for event in reversed(history)
                       if event['status'] not in ('started', 'structured_output')), {})
        accounted = [row['usage'] for row in requests if row['state'] == 'accounted']
        reasoning = (sum(usage['reasoning_tokens'] for usage in accounted)
                     if accounted and all('reasoning_tokens' in usage for usage in accounted) else None)
        completion = sum(usage['completion_tokens'] for usage in accounted) if accounted else None
        execution = path.with_name('execution.json')
        records.append({
            'role': role, 'status': finish.get('status', 'unknown'),
            'parse_status': structured.get('parse_status', 'not_attempted'),
            'handoff_saved': structured.get('handoff_saved', False),
            'elapsed_seconds': finish.get('elapsed_seconds'),
            'sdk_requests': load_json(execution)['model_requests'] if execution.is_file() else None,
            'upstream_attempts': sum(row.get('attempt_count', 0) for row in requests),
            'prompt_tokens': sum(usage['prompt_tokens'] for usage in accounted) if accounted else None,
            'completion_tokens': completion, 'reasoning_tokens': reasoning,
            'non_reasoning_tokens': completion - reasoning if reasoning is not None else None,
            'output_bytes': sum(row.get('content_bytes', 0) for row in requests),
            'finish_reasons': [row.get('finish_reason') for row in requests if row['backend_dispatched']],
            'request_log': str(path) if path.is_file() else None,
        })
    return records


def run(data_root, tasks, variants, output, *, live, prices, max_cost=None, stop_on_error=False):
    profile = resolve_profile(DEEPSEEK_PROFILE)
    key_name = profile['api_key_env']
    task_budget = {
        'wall_seconds': profile['budgets']['wall_seconds'],
        'max_requests': profile['budgets']['max_steps'],
        'max_generated_tokens': profile['policy']['max_total_generated_tokens'],
    }
    guard = CostGuard(max_cost, prices, profile['model']['context_tokens']) if max_cost is not None else None
    output = output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    model_mode = 'deepseek_api' if live else 'scripted'
    modules = ('cybergym_experiment.py', 'review_task.py', 'task_materials.py',
               'experiment.py', 'review.py', 'experiment_fixture.py')
    save(output / 'manifest.json', {
        'model_mode': model_mode, 'profile': profile, 'data_root': str(data_root.resolve()),
        'tasks': tasks, 'variants': variants, 'budget': task_budget,
        'pricing_usd_per_million': prices,
        'max_cost_usd': max_cost, 'stop_on_error': stop_on_error,
        'source_sha256': {name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                          for name in modules},
    })
    for task_id, source in tasks.items():
        load_task(task_id, data_root, None, [source])
    if live and not os.environ.get(key_name, '').strip():
        message = f'{key_name} is missing from the run environment'
        save(output / 'report.json', {'model_mode': model_mode, 'status': 'blocked',
                                      'reason': message, 'upstream_attempts': 0,
                                      'official_status': 'not_evaluated', 'rows': []})
        print(message, file=sys.stderr)
        return 2

    rows, roles = [], []
    aborted = False
    with (output / 'results.jsonl').open('x', encoding='utf-8') as stream:
        for task_id, source in tasks.items():
            for variant in variants:
                if aborted:
                    break
                case = output / 'tasks' / task_id.replace(':', '-') / variant
                context = nullcontext(None)
                if not live:
                    from experiment_fixture import scripted_model
                    context = scripted_model(SimpleNamespace(source_name=source, expected_line=1),
                                             static_level1=True)
                failure = None
                with context as url:
                    start = time.monotonic()
                    try:
                        code = review_task(
                            task_id, data_root, None, [source], case, sys.executable,
                            mapper=variant == 'full',
                            critic=variant in ('manager_worker_critic', 'full'),
                            variant=variant, fixture_url=url, frozen_profile=profile,
                            task_budget=TaskBudget(task_budget, cost_guard=guard))
                    except (OSError, ValueError) as exc:
                        code, failure = 1, str(exc)
                    elapsed = round(time.monotonic() - start, 4)
                events_path = case / 'events.jsonl'
                events = ([json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()]
                          if events_path.is_file() else [])
                if code and failure is None:
                    last = next((event for event in reversed(events)
                                 if event['status'] not in ('structured_output', 'started')), None)
                    if last is not None:
                        failure = last.get('error') or last.get('reason') or last.get('status')
                if guard is not None and guard.blocked:
                    code = code or 1
                    failure = 'cost_guard_stopped' if failure is None else failure
                case_roles = role_records(case, events)
                if case.is_dir():
                    save(case / 'role_metrics.json', case_roles)
                roles.extend({'task_id': task_id, 'variant': variant, **role} for role in case_roles)
                row = {
                    'task_id': task_id, 'source': source, 'variant': variant,
                    'review_status': 'completed' if code == 0 else 'failed',
                    'failure_reason': failure, 'exit_code': code, 'elapsed_seconds': elapsed,
                    'official_status': 'not_evaluated', 'official_success': None,
                    'evaluation_mode': 'static_review',
                    'official_status_reason': 'scorer_not_invoked',
                    'submission_status': 'not_submitted',
                    'candidate_status': 'not_produced',
                    'output': str(case), **ledger(case, prices, not live),
                }
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                stream.flush()
                aborted = bool((guard is not None and guard.blocked) or (code and stop_on_error))

    with (output / 'results.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    if roles:
        with (output / 'roles.csv').open('x', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(roles[0]))
            writer.writeheader()
            writer.writerows({**role, 'finish_reasons': json.dumps(role['finish_reasons'])} for role in roles)
    summary = {}
    for variant in variants:
        group = [row for row in rows if row['variant'] == variant]
        costs = [row['estimated_cost_usd'] for row in group]
        summary[variant] = {
            'tasks': len(group),
            'review_status': dict(Counter(row['review_status'] for row in group)),
            'official_evaluated': 0,
            'elapsed_seconds': round(sum(row['elapsed_seconds'] for row in group), 4),
            'sdk_requests': sum(row['sdk_requests'] for row in group),
            'upstream_attempts': sum(row['upstream_attempts'] for row in group),
            'prompt_tokens': sum(row['prompt_tokens'] for row in group),
            'generated_tokens': sum(row['generated_tokens'] for row in group),
            'estimated_cost_usd': sum(costs) if all(cost is not None for cost in costs) else None,
        }
    save(output / 'report.json', {'model_mode': model_mode, 'summary': summary, 'rows': rows,
                                  'roles': roles, 'aborted': aborted,
                                  'cost_guard_charged_upper_usd': float(guard.charged) if guard else None})
    return int(any(row['exit_code'] for row in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--task', action='append', nargs=2, required=True,
                        metavar=('TASK_ID', 'SOURCE_MEMBER'))
    parser.add_argument('--variant', action='append', choices=VARIANTS)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pricing', type=Path, help='JSON object of USD prices per million tokens')
    parser.add_argument('--live', action='store_true', help='send selected source to configured DeepSeek API')
    parser.add_argument('--max-cost-usd', type=float, help='conservative experiment-wide spend ceiling; requires pricing')
    parser.add_argument('--stop-on-error', action='store_true', help='stop after the first incomplete case')
    args = parser.parse_args()
    tasks = dict(args.task)
    variants = args.variant or VARIANTS
    if len(tasks) != len(args.task) or len(set(variants)) != len(variants):
        parser.error('tasks and variants must be distinct')
    try:
        prices = None
        if args.pricing is not None:
            prices = json.loads(args.pricing.read_text(encoding='utf-8'))
            if (not isinstance(prices, dict) or prices.keys() != PRICE_KEYS
                    or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                           for value in prices.values())):
                raise ValueError('pricing requires finite nonnegative input_cache_hit, input_cache_miss and output')
        if args.max_cost_usd is not None and (
                prices is None or not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0):
            raise ValueError('--max-cost-usd requires --pricing and a positive finite amount')
        return run(args.data_root, tasks, variants, args.output, live=args.live, prices=prices,
                   max_cost=args.max_cost_usd, stop_on_error=args.stop_on_error)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
