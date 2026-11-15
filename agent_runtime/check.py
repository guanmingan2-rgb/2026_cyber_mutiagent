#!/usr/bin/env python3
"""只检查 Harness 配置与本地冻结契约，不运行 SDK 或工具隔离。"""

import argparse
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

if __package__:
    from .profile import DEFAULT_PROFILE, ROOT, resolve_profile
else:
    from profile import DEFAULT_PROFILE, ROOT, resolve_profile

FORMAL_BUDGET_LIMITS = {
    'max_steps': 1000,
    'wall_seconds': 14400,
    'request_timeout_seconds': 1800,
}
MAX_OUTPUT_TOKENS = 32768
MAX_TOTAL_GENERATED_TOKENS = 512000


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'JSON 字段重复: {key}')
        value[key] = item
    return value


def check_profile(path=None):
    path = Path(path) if path is not None else DEFAULT_PROFILE
    result = {
        'status': 'failed',
        'ok': False,
        'profile': str(path.resolve()),
        'errors': [],
        'checks': [],
        'scope': {
            'configuration': 'local',
            'sdk_runtime': 'not_checked',
            'tool_isolation': 'not_checked',
            'model_endpoint': 'not_checked',
        },
    }
    errors = result['errors']
    try:
        raw = json.loads(path.resolve().read_text(encoding='utf-8'), object_pairs_hook=_object)
        if not isinstance(raw, dict):
            raise ValueError('Harness 配置必须为 JSON 对象')
        profile = resolve_profile(path)
    except (OSError, ValueError, UnicodeError) as exc:
        errors.append(str(exc))
        return result

    def fields(value, expected, name):
        if not isinstance(value, dict):
            errors.append(f'{name} 必须是对象')
            return False
        missing = set(expected) - set(value)
        extra = set(value) - set(expected)
        if missing:
            errors.append(f'{name} 缺少字段: {", ".join(sorted(missing))}')
        if extra:
            errors.append(f'{name} 含不属于该层的字段: {", ".join(sorted(extra))}')
        return not missing

    def string(value, name):
        valid = isinstance(value, str) and bool(value.strip())
        if not valid:
            errors.append(f'{name} 必须是非空字符串')
        return valid

    def integer(value, name, minimum=1):
        valid = type(value) is int and value >= minimum
        if not valid:
            errors.append(f'{name} 必须是至少 {minimum} 的整数')
        return valid

    def equals(value, expected, name):
        if type(value) is not type(expected) or value != expected:
            errors.append(f'{name} 必须为 {expected!r}')

    profile_fields = ('schema_version', 'harness', 'provider', 'base_url',
                      'api_key_env', 'model', 'policy', 'budgets', 'resources',
                      'runtime')
    if 'cost_reporting' in profile:
        profile_fields += ('cost_reporting',)
    if not fields(profile, profile_fields, 'profile'):
        return result
    equals(profile['schema_version'], 1, 'schema_version')
    if profile['harness'] != 'deepseek':
        errors.append('harness 必须为 deepseek')
    string(profile['provider'], 'provider')
    if string(profile['base_url'], 'base_url'):
        try:
            url = urlsplit(profile['base_url'])
            if (url.scheme != 'https' or not url.hostname or url.username
                    or url.password or url.query or url.fragment or url.path != '/v1'):
                errors.append('base_url 必须是无凭证、查询串或 fragment 的 HTTPS /v1 地址')
            url.port
        except ValueError:
            errors.append('base_url 格式无效')
    if string(profile['api_key_env'], 'api_key_env') and not re.fullmatch(
            r'[A-Za-z_][A-Za-z0-9_]*', profile['api_key_env']):
        errors.append('api_key_env 必须是环境变量名，不得填写密钥')

    model = profile['model']
    if fields(model, ('served_name', 'provider_route', 'requested_checkpoint',
                      'revision_evidence', 'context_tokens'), 'model'):
        for key in ('served_name', 'provider_route', 'requested_checkpoint'):
            string(model[key], f'model.{key}')
        if isinstance(model['provider_route'], str):
            route = model['provider_route'].split('/', 1)
            if len(route) != 2 or not route[0] or route[1] != model['served_name']:
                errors.append('model.provider_route 必须以 provider/served_name 指向同一模型')
        integer(model['context_tokens'], 'model.context_tokens')
        evidence = model['revision_evidence']
        if fields(evidence, ('source', 'kind'), 'model.revision_evidence'):
            for key in ('source', 'kind'):
                string(evidence[key], f'model.revision_evidence.{key}')

    policy = profile['policy']
    if fields(policy, ('protocol', 'temperature', 'top_p', 'thinking',
                       'reasoning_effort', 'max_output_tokens',
                       'max_total_generated_tokens'), 'policy'):
        equals(policy['protocol'], 'openai_chat', 'policy.protocol')
        for key, lower, upper in (('temperature', 0, 2), ('top_p', 0, 1)):
            value = policy[key]
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not lower <= value <= upper or (key == 'top_p' and value == 0)):
                errors.append(f'policy.{key} 超出有效采样范围')
        if fields(policy['thinking'], ('type',), 'policy.thinking'):
            if policy['thinking']['type'] not in ('enabled', 'disabled'):
                errors.append('policy.thinking.type 必须为 enabled 或 disabled')
        if policy['reasoning_effort'] not in ('low', 'high', 'max'):
            errors.append('policy.reasoning_effort 无效')
        output_valid = integer(policy['max_output_tokens'], 'policy.max_output_tokens')
        total_valid = integer(policy['max_total_generated_tokens'], 'policy.max_total_generated_tokens')
        if output_valid and policy['max_output_tokens'] > MAX_OUTPUT_TOKENS:
            errors.append(f'单次输出上限不得超过模型网关的 {MAX_OUTPUT_TOKENS} token 上限')
        if output_valid and total_valid and policy['max_total_generated_tokens'] < policy['max_output_tokens']:
            errors.append('总生成预算不得小于单次输出上限')
        if total_valid and policy['max_total_generated_tokens'] > MAX_TOTAL_GENERATED_TOKENS:
            errors.append(f'policy.max_total_generated_tokens 不得超过正式预算 {MAX_TOTAL_GENERATED_TOKENS}')
        if (output_valid and isinstance(model, dict)
                and type(model.get('context_tokens')) is int
                and policy['max_output_tokens'] > model['context_tokens']):
            errors.append('单次输出上限不得超过模型上下文窗口')

    budgets = profile['budgets']
    if fields(budgets, ('max_steps', 'wall_seconds', 'request_timeout_seconds'), 'budgets'):
        for key, value in budgets.items():
            if integer(value, f'budgets.{key}') and value > FORMAL_BUDGET_LIMITS[key]:
                errors.append(f'budgets.{key} 不得超过正式预算 {FORMAL_BUDGET_LIMITS[key]}')

    resources = profile['resources']
    if fields(resources, ('cpus', 'memory_gib', 'time', 'gpu_count'), 'resources'):
        integer(resources['cpus'], 'resources.cpus')
        integer(resources['memory_gib'], 'resources.memory_gib')
        integer(resources['gpu_count'], 'resources.gpu_count', 0)
        duration = resources['time']
        if (not isinstance(duration, str)
                or not re.fullmatch(r'\d+:[0-5]\d:[0-5]\d', duration)
                or not any(int(part) for part in duration.split(':'))):
            errors.append('resources.time 必须是正数 HH:MM:SS 时限')

    if 'cost_reporting' in profile:
        reporting = profile['cost_reporting']
        if fields(reporting, ('cache_creation_not_applicable', 'cache_creation_evidence'),
                  'cost_reporting'):
            if reporting['cache_creation_not_applicable'] is not True:
                errors.append('cost_reporting.cache_creation_not_applicable 必须为 true')
            string(reporting['cache_creation_evidence'],
                   'cost_reporting.cache_creation_evidence')

    runtime = profile['runtime']
    runtime_fields = ('python', 'distributions', 'tools', 'native_tool_calling', 'profile')
    if fields(runtime, runtime_fields, 'runtime'):
        equals(runtime['python'], '3.12', 'runtime.python')
        equals(runtime['native_tool_calling'], True, 'runtime.native_tool_calling')
        equals(runtime['profile'], 'sdk-minimal', 'runtime.profile')
        equals(runtime['tools'], [], 'runtime.tools')
        try:
            pins = dict(line.split('==', 1) for line in
                        (ROOT / 'requirements-deepseek.txt').read_text().splitlines()
                        if line.strip())
            equals(runtime['distributions'], pins, 'runtime.distributions')
        except (OSError, ValueError) as exc:
            errors.append(f'DeepSeek 冻结依赖不可读: {exc}')

    result['ok'] = not errors
    if result['ok']:
        result['status'] = 'passed'
        result['checks'] = ['field_boundaries', 'model_policy', 'agent_budgets',
                            'resource_shape', 'runtime_declarations', 'local_dependency_pins']
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args(argv)
    result = check_profile(args.profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
