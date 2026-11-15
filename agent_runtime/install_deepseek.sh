#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python=${1:?请提供 Python 3.12 的绝对路径}
venv="$root/../.venv-deepseek-harness"
[[ "$python" = /* ]] || { printf 'Python 路径必须为绝对路径\n' >&2; exit 1; }
"$python" -c 'import sys; assert sys.version_info[:2] == (3, 12)'
if [[ ! -d "$venv" ]]; then
    "$python" -m venv "$venv"
fi
"$venv/bin/python" -m pip install --disable-pip-version-check -r "$root/requirements-deepseek.txt"
"$venv/bin/python" -m pip check
PYTHONPATH="$root/.." "$venv/bin/python" -B -c 'from agent_runtime.coding import resolve_profile, verify_runtime, DEEPSEEK_PROFILE; verify_runtime(resolve_profile(DEEPSEEK_PROFILE)); print("DeepSeek 独立依赖检查通过")'
