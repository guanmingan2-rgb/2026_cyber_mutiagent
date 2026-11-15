#!/usr/bin/env bash
#SBATCH --job-name=cg-pipeline
#SBATCH --account=users
#SBATCH --partition=compute
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=24G
#SBATCH --time=04:00:00
#SBATCH --chdir=/home/ybgao/2026_cyber_mutiagent
#SBATCH --output=/home/ybgao/2026_cyber_mutiagent/runs/pipeline-%j.log
#
# End-to-end CyberGym pipeline inside one Slurm job:
#   data read -> agent session -> official vulnerable/fixed scoring
# Usage:
#   sbatch run_pipeline.sbatch arvo:10013
#   sbatch run_pipeline.sbatch arvo:10013 --mode solve \
#     --source src-vul/graphicsmagick/coders/tiff.c --agent-timeout 900
set -euo pipefail
umask 077
: "${SLURM_JOB_ID:?Run through sbatch}"
project=/home/ybgao/2026_cyber_mutiagent
solver=/home/ybgao/2026_cyber_agents/safegym_solver
run="$project/runs/pipeline-$SLURM_JOB_ID"
mkdir "$run"
task="${1:-arvo:3938}"
shift || true
base="${SAFEGYM_PIPELINE_TMP:-/dev/shm}/$(id -un)/safegym-slurm-$SLURM_JOB_ID"
[[ ! -e "$base" ]] || { printf 'Refusing existing runtime: %s\n' "$base"; exit 1; }
mkdir -p "$base/xdg"
export XDG_RUNTIME_DIR="$base/xdg"
export SAFEGYM_DOCKER_RUNTIME="$base/runtime" SAFEGYM_DOCKER_DATA="$base/docker"
export DOCKER_HOST="unix://$SAFEGYM_DOCKER_RUNTIME/docker.sock"
export PATH="$solver/tools/docker:$PATH"
export SAFEGYM_LOG_DIR="$run/server_poc" SAFEGYM_DB_PATH="$run/server_poc/poc.db"
export PYTHONUNBUFFERED=1
mode="${SAFEGYM_PIPELINE_MODE:-image}"
server_pid=''
stage=runtime_preflight
status() { "$solver/.venv/bin/python" -m pipeline.status "$run" "$stage" "$1" "${2:-0}"; }
cleanup() {
    rc=$?
    trap - EXIT
    if [[ "$rc" != 0 ]]; then status failed "$rc"; else status completed 0; fi
    if [[ -n "$server_pid" ]]; then kill "$server_pid" 2>/dev/null || true; wait "$server_pid" || true; fi
    "$solver/.venv/bin/python" "$solver/scripts/slurm_runtime_check.py" stop || true
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
status running
df -h "$base" >"$run/storage.txt"
"$solver/.venv/bin/python" "$solver/scripts/slurm_runtime_check.py" preflight
"$solver/scripts/rootless_docker.sh" start
stage=image_prepare; status running
"$solver/.venv/bin/python" "$solver/scripts/slurm_runtime_check.py" prepare --mode "$mode" --task-id "$task"
stage=isolation_check; status running
"$solver/.venv/bin/python" "$solver/scripts/slurm_runtime_check.py" probe --report "$run/runtime-probe.json"
"$solver/scripts/rootless_docker.sh" exec "$solver/.venv/bin/python" "$solver/scripts/setup_firewall.py" --report "$run/firewall.json"
"$solver/.venv/bin/python" "$solver/scripts/slurm_runtime_check.py" check --container-id cybergym-proxy --report "$run/proxy.json"
stage=scoring_service; status running
# Pick a free loopback port so two jobs on the same node cannot collide on 18666.
port="$("$solver/.venv/bin/python" -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()")"
"$solver/.venv/bin/python" "$solver/scripts/harness.py" serve --mode "$mode" --host 127.0.0.1 --port "$port" >"$run/server.log" 2>&1 &
server_pid=$!
ready=0
for ((i=0;i<60;i++)); do
    kill -0 "$server_pid" || { cat "$run/server.log"; exit 1; }
    if curl --noproxy '*' -fsS "http://127.0.0.1:$port/openapi.json" >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
done
[[ "$ready" == 1 ]] || { cat "$run/server.log"; exit 1; }
stage=task_agent_score; status running
"$project/run_deepseek.sh" -m pipeline.run --task-id "$task" --output "$run/result" --server "http://127.0.0.1:$port" "$@"
