#!/usr/bin/env bash
set +x
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
runtime_root=${CYBERGYM_ROOT:-$HOME/2026_cyber_agents}

if [[ -z ${DEEPSEEK_API_KEY:-} ]]; then
    env_file=${DEEPSEEK_ENV_FILE:-"$runtime_root/safegym_solver/config/deepseek-official.env"}
    if [[ ! -r $env_file ]]; then
        printf 'Cannot read DeepSeek configuration at %s\n' "$env_file" >&2
        exit 1
    fi
    source "$env_file"
fi
if [[ -z ${DEEPSEEK_API_KEY:-} ]]; then
    printf 'DEEPSEEK_API_KEY is missing from the DeepSeek configuration\n' >&2
    exit 1
fi
export DEEPSEEK_API_KEY

cd -- "$project_dir"
exec "$runtime_root/.venv-deepseek-harness/bin/python" -B "$@"
