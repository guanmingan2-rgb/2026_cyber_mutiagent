"""冻结模型、工具、预算与运行环境的统一规格。"""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = ROOT / 'profiles/deepseek-harness.json'


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('JSON 字段重复：' + key)
        value[key] = item
    return value


def resolve_profile(path=None):
    """解析完整配置、单字段引用或 DeepSeek 独立 Harness 声明。"""
    path = Path(path).resolve() if path is not None else DEFAULT_PROFILE
    seen = set()
    while True:
        if path in seen:
            raise ValueError('agent_profile 引用形成循环')
        seen.add(path)
        profile = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_object)
        if not isinstance(profile, dict):
            raise ValueError('Harness 配置必须为 JSON 对象')
        if set(profile) == {'agent_profile'}:
            reference = profile['agent_profile']
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError('agent_profile 必须是非空文件路径')
            path = (path.parent / reference).resolve()
            continue
        if 'shared_profile' not in profile:
            return profile
        if set(profile) != {'shared_profile', 'harness', 'runtime'}:
            raise ValueError('独立 Harness 配置只允许共享配置引用、harness 和 runtime')
        reference = profile['shared_profile']
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError('shared_profile 必须是非空文件路径')
        shared = resolve_profile(path.parent / reference)
        result = copy.deepcopy(shared)
        result.update(harness=profile['harness'], runtime=copy.deepcopy(profile['runtime']))
        return result


def load_profile(path=None):
    return resolve_profile(path)


def _flat(profile):
    return {
        'harness': profile['harness'],
        'model': profile['model']['served_name'],
        'api_key_env': profile['api_key_env'],
        'context_tokens': profile['model']['context_tokens'],
        'max_output_tokens': profile['policy']['max_output_tokens'],
        'max_total_generated_tokens': profile['policy']['max_total_generated_tokens'],
        'temperature': profile['policy']['temperature'],
        'top_p': profile['policy']['top_p'],
        'thinking': profile['policy']['thinking'],
        'reasoning_effort': profile['policy']['reasoning_effort'],
        **profile['budgets'],
    }


def bind_profile(spec, profile):
    result = copy.deepcopy(spec)
    if 'agent_profile' in result and result['agent_profile'] != profile:
        raise ValueError('agent_profile 与冻结规格冲突')
    for field, value in _flat(profile).items():
        if field in result and result[field] != value:
            raise ValueError(f'{field} 与共享 Agent 规格冲突')
        result[field] = copy.deepcopy(value)
    result['agent_profile'] = copy.deepcopy(profile)
    return result
