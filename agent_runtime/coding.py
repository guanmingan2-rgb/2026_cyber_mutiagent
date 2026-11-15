"""独立编程 Harness 入口；不读取或派发 benchmark。"""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

from agent_runtime.check import check_profile
from agent_runtime.deepseek import verify_runtime
from agent_runtime.profile import ROOT, bind_profile, load_profile

DEEPSEEK_PROFILE = ROOT / 'profiles/deepseek-harness.json'


def resolve_profile(path):
    path = Path(path).resolve()
    checked = check_profile(path)
    if not checked['ok']:
        raise ValueError('; '.join(checked['errors']))
    return load_profile(path)


def local_url(value):
    url = urlsplit(value)
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost'}
            or url.username or url.password or url.query or url.fragment or url.path != '/v1'):
        raise ValueError('独立编程入口只接受本地 HTTP /v1 假模型或网关')
    url.port
    return value


def run(spec_path, python, *, cost_guard=None):
    from agent_runtime.gateway import ModelGateway
    from agent_runtime.coding_relay import LimitedRelay

    source = Path(spec_path).resolve()
    spec = json.loads(source.read_text())
    profile = resolve_profile(source.parent / spec.get('agent_profile', DEEPSEEK_PROFILE))
    spec['agent_profile'] = profile
    spec = bind_profile(spec, profile)
    upstream = spec.get('base_url', profile['base_url'])
    if upstream == profile['base_url']:
        api_key = os.environ[profile['api_key_env']]
    else:
        local_url(upstream)
        api_key = 'local-fixture-only'
    if not isinstance(spec['prompt'], str) or not spec['prompt'].strip():
        raise ValueError('prompt 必须是非空字符串')
    workspace = (source.parent / spec['workspace']).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError('workspace 必须是目录')
    output = (source.parent / spec['output_dir']).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    native = output / 'native'
    native.mkdir(mode=0o700)
    result = {'status': 'failed', 'agent_status': 'not_started',
              'final_text': '', 'usage': None, 'harness': profile['harness']}
    exit_code = 1
    try:
        with ModelGateway(upstream, api_key,
                          profile['model']['served_name'], spec['request_timeout_seconds'],
                          output / 'gateway.jsonl', profile['policy'],
                          usage_path=output / 'usage.json', drain_timeout=5, cost_guard=cost_guard) as gateway:
            with LimitedRelay(gateway.socket_path, gateway.token, spec['max_steps'],
                              spec['request_timeout_seconds']) as relay:
                child_spec = dict(spec, workspace=str(workspace), output_dir=str(native),
                                  base_url=relay.base_url)
                (output / 'spec.json').write_text(json.dumps(child_spec, ensure_ascii=False, indent=2) + '\n')
                env = {key: value for key, value in os.environ.items()
                       if not key.upper().endswith(('_API_KEY', '_TOKEN', '_PROXY'))
                       and key not in {'PYTHONPATH', 'PYTHONHOME', 'DSH_HOME', 'DSH_BIN'}}
                env.update({profile['api_key_env']: gateway.token,
                            'PYTHONPATH': str(ROOT.parent), 'PYTHONDONTWRITEBYTECODE': '1',
                            'NO_PROXY': 'localhost,127.0.0.1', 'no_proxy': 'localhost,127.0.0.1'})
                interpreter = Path(python).absolute()
                if not interpreter.is_file():
                    raise FileNotFoundError(interpreter)
                command = [str(interpreter), '-B', '-m', 'agent_runtime.deepseek', str(output / 'spec.json')]
                with (output / 'stdout.txt').open('w') as stdout, (output / 'stderr.txt').open('w') as stderr:
                    child = subprocess.Popen(command, env=env, stdout=stdout, stderr=stderr,
                                             start_new_session=True)
                    try:
                        deadline = time.monotonic() + spec['wall_seconds']
                        while True:
                            if child.poll() is not None:
                                exit_code = child.returncode
                                break
                            if relay.exhausted or relay.rejected:
                                exit_code = 1
                                break
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                result.update(status='timeout', agent_status='interrupted')
                                exit_code = 124
                                break
                            try:
                                child.wait(timeout=min(.2, remaining))
                            except subprocess.TimeoutExpired:
                                pass
                    finally:
                        # 清理本轮进程组，包括 SDK 启动的 shell；不影响其他会话。
                        if child.poll() is None:
                            try:
                                os.killpg(child.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                            try:
                                child.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                pass
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        child.wait()
                raw_path = native / 'adapter-result.json'
                if raw_path.is_file() and result['status'] != 'timeout':
                    raw = json.loads(raw_path.read_text())
                    result.update({key: raw[key] for key in ('status', 'agent_status', 'final_text') if key in raw})
                    if 'error_type' in raw:
                        result['error_type'] = raw['error_type']
                if relay.exhausted and result['status'] != 'timeout':
                    result.update(status='step_limit', agent_status='interrupted')
                    exit_code = 2
                elif relay.rejected and gateway.usage_summary['budget_exhausted']:
                    result.update(status='token_limit', agent_status='interrupted')
                    exit_code = 2
                result['exit_status'] = exit_code
                (output / 'execution.json').write_text(json.dumps(
                    {'command': command, 'exit_status': exit_code, 'model_requests': relay.request_count,
                     'process_group': child.pid},
                    ensure_ascii=False, indent=2) + '\n')
            gateway.drain()
            result['usage'] = gateway.usage_summary
            if result['usage']['accounting_failed']:
                result['error_type'] = 'UsageAccountingError'
                if result['status'] != 'timeout':
                    result['status'] = 'failed'
                    exit_code = 1
            elif (result['usage']['budget_exhausted']
                  and result['status'] not in {'completed', 'timeout', 'step_limit'}):
                result.update(status='token_limit', agent_status='interrupted')
                exit_code = 2
            if exit_code != 0 and result['status'] == 'completed':
                result['status'] = 'failed'
            if result['status'] != 'completed' and exit_code == 0:
                exit_code = 1
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__)
        exit_code = 1
    result['exit_status'] = exit_code
    (output / 'adapter-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    check = commands.add_parser('check')
    check.add_argument('--profile', type=Path, default=DEEPSEEK_PROFILE)
    execute = commands.add_parser('run')
    execute.add_argument('spec', type=Path)
    execute.add_argument('--python', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == 'check':
            profile = resolve_profile(args.profile)
            print(json.dumps({'status': 'passed', 'harness': profile['harness'],
                              'profile': str(args.profile.resolve()),
                              'scope': 'configuration_only'}, ensure_ascii=False))
            return 0
        return run(args.spec, args.python)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
