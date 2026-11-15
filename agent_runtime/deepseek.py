"""独立 DeepSeek 官方 SDK 适配器；依赖仅在子进程中加载。"""

import json
import importlib.metadata
import os
from pathlib import Path
import signal
import sys
from urllib.parse import urlsplit


def verify_runtime(profile):
    if f"{sys.version_info.major}.{sys.version_info.minor}" != profile["runtime"]["python"]:
        raise ValueError("Python 版本与 Harness 配置不一致")
    actual = {
        name: importlib.metadata.version(name)
        for name in profile["runtime"]["distributions"]
    }
    if actual != profile["runtime"]["distributions"]:
        raise ValueError("Harness 依赖版本与配置不一致")
    return actual


def local_url(value):
    url = urlsplit(value)
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "localhost"}
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != "/v1"
    ):
        raise ValueError("DeepSeek Harness 只接受本地 HTTP /v1 网关")
    url.port
    return value


def main():
    spec = json.loads(Path(sys.argv[1]).read_text())
    profile = spec['agent_profile']
    output = Path(spec['output_dir'])
    result = {'status': 'failed', 'agent_status': 'not_started', 'final_text': ''}

    def interrupt(signum, _):
        raise InterruptedError(signum)

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        versions = verify_runtime(profile)
        base_url = local_url(spec['base_url'])
        from deepseek_harness import DeepSeekHarness

        route = {
            'apiKeyEnv': profile['api_key_env'], 'api': 'openai-completions', 'baseURL': base_url,
            'defaultContextWindow': spec['context_tokens'],
            'defaultMaxTokens': spec['max_output_tokens'],
            'models': [{'id': profile['model']['served_name'], 'reasoningEfforts': False}],
            'retryPolicy': {'mode': 'normal', 'maxRetries': 0},
            'timeoutMs': spec['request_timeout_seconds'] * 1000,
        }
        patches = [
            {'insert': [{
                'id': 'llm-gateway',
                'name': '@deepseek-ai/dsh-llm-pi-ai',
                'config': {'providers': {'local-gateway': route}},
            }]},
            {'id': 'tool-fs', 'config': {
                'read': False, 'readImage': False, 'write': False, 'edit': False,
            }},
            {'id': 'tool-working-directory', 'disabled': True},
            {'id': 'persistent-bash', 'disabled': True},
        ]
        patch = output / 'profile.patch.yml'
        patch.write_text(json.dumps(patches, indent=2) + '\n')
        (output / 'agent-contract.json').write_text(json.dumps(
            {'agent_profile': profile, 'actual_runtime_versions': versions,
             'provider': '@deepseek-ai/dsh-llm-pi-ai', 'profile': 'sdk-minimal'},
            ensure_ascii=False, indent=2) + '\n')
        with (output / 'trajectory.jsonl').open('w') as trajectory:
            def record(notification):
                trajectory.write(json.dumps({'method': notification.method, 'payload': notification.payload},
                                             ensure_ascii=False) + '\n')
                trajectory.flush()

            with DeepSeekHarness(
                dsh_home=str(output / 'dsh-home'), cwd=spec['workspace'],
                runtime_cwd=spec['workspace'], profile=profile['runtime']['profile'],
                provider='local-gateway', model=profile['model']['served_name'],
                api_key=os.environ.pop(profile['api_key_env']), base_url=base_url,
                max_tokens=spec['max_output_tokens'], patches=(str(patch),),
                request_timeout_seconds=spec['request_timeout_seconds'],
            ) as harness:
                observed = harness.run(spec['prompt'], on_notification=record)
        result.update(agent_status=observed.finish_reason, final_text=observed.final_response)
        result['status'] = {
            'completed': 'completed',
            'max-tokens': 'token_limit',
        }.get(observed.finish_reason, 'failed')
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    (output / 'adapter-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
