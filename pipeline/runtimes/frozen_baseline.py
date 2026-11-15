"""Frozen baseline runtime: native multi-turn executor with controlled feedback.

This is the behaviour-preserving adapter. It reproduces the frozen executor's
protocol instead of replacing it:

* ``make_task(..., server="http://127.0.0.1:8667")`` so ``submit.sh`` talks to a
  loopback relay, exactly like the formal runner;
* a host-side ``TaskScoreGateway`` binds a private Unix socket and forwards only
  ``POST /submit-vul`` to the official service; the fixed side is never exposed;
* ``agent_harness.py run --score-socket <socket>`` with the frozen prompt, so the
  agent keeps its native candidate registration, ``poc_id`` feedback and
  ``final_submission.json`` selection;
* the adapter reads the selected ``poc_id`` from the workspace, resolves the
  matching submitted artifact from the gateway ledger, and hands those exact
  bytes to ``pipeline.scoring`` for the authoritative vulnerable/fixed verdict.

The previous adapter (``runtimes/frozen.py``) is kept as ``frozen-nofeedback``;
it is useful for isolation experiments but is **not** behaviour-equivalent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..runtime import AgentRunResult, RunStatus
from .frozen import DEFAULT_CONFIG, DEFAULT_PROFILE, DEFAULT_ROOT, TASK_PROMPT

DEFAULT_NATIVE_PROMPT = Path(os.environ.get(
    "CYBERGYM_FROZEN_TASK_PROMPT",
    "/home/ybgao/2026_cyber_agents/safegym_solver/runs/"
    "unified-migration-20261113T071227Z-95ab6f/frozen/task-prompt.txt"))
SCORE_PORT = 8667
WORKSPACE_FILES = ("description.txt", "README.md", "repo-vul.tar.gz", "submit.sh", "task.json")


class _NullGateway:
    """Offline gateway stub used by tests (no socket, no proxying)."""

    socket_path = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FrozenBaselineRunner:
    name = 'frozen'

    def __init__(self, code_root=None, harness_config=None, profile=None, python=None,
                 prompt=None, dry_run=False, fake_executor=None, gateway_factory=None,
                 score_port=18666, start_gateway=True):
        self.code_root = Path(code_root or DEFAULT_ROOT)
        self.harness_config = Path(harness_config or DEFAULT_CONFIG)
        self.profile = Path(profile or DEFAULT_PROFILE)
        self.python = Path(python or self.code_root / "safegym_solver/.venv/bin/python")
        self.prompt = Path(prompt or DEFAULT_NATIVE_PROMPT)
        self.dry_run = dry_run
        self.fake_executor = Path(fake_executor) if fake_executor else None
        self.gateway_factory = gateway_factory
        self.score_port = int(score_port)
        self.start_gateway = start_gateway
        self.harness = os.environ.get("CYBERGYM_FROZEN_HARNESS", "deepseek")

    # -- workspace / prompt --------------------------------------------
    def build_workspace(self, context, output):
        workspace = Path(output) / "workspace"
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, mode=0o700)
        for name in WORKSPACE_FILES:
            source = Path(context.task_dir) / name
            if source.is_file():
                shutil.copy2(source, workspace / name)
        return workspace

    def resolve_prompt(self, output):
        if self.prompt.is_file():
            return self.prompt
        # Fallback keeps the adapter usable without the frozen run tree.
        path = Path(output) / "task-prompt.txt"
        path.write_text(TASK_PROMPT)
        return path

    # -- gateway --------------------------------------------------------
    def open_gateway(self, task_dir, output):
        """Start the vulnerable-only gateway, or return a stub in offline mode."""
        if self.gateway_factory is not None:
            return self.gateway_factory(task_dir, output)
        if not self.start_gateway:
            return _NullGateway()
        task = json.loads((Path(task_dir) / "task.json").read_text())
        metadata = {key: task[key] for key in ("task_id", "agent_id", "checksum")}
        metadata["require_flag"] = False
        sys.path.insert(0, str(self.code_root / "safegym_solver/scripts"))
        from task_socket_gateway import TaskScoreGateway  # noqa: PLC0415 (frozen helper)
        score_dir = Path(output) / "score"
        score_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(score_dir, 0o700)
        socket_dir = Path(tempfile.mkdtemp(prefix="cg-score-", dir=str(Path(output).parent)))
        return TaskScoreGateway(socket_dir / "score.sock", metadata=metadata,
                                task_id=task["task_id"],
                                task_token=os.urandom(24).hex(),
                                backend_port=self.score_port,
                                report_dir=score_dir, request_timeout=1200)

    # -- executor -------------------------------------------------------
    def build_command(self, workspace, prompt, execution, gateway):
        entry = self.fake_executor or (self.code_root / "safegym_solver/scripts/agent_harness.py")
        command = [str(sys.executable if self.fake_executor else self.python), str(entry), "run",
                   "--harness", self.harness,
                   "--workspace", str(workspace),
                   "--task-file", str(prompt),
                   "--output-dir", str(execution),
                   "--agent-profile", str(self.profile)]
        if gateway.socket_path is not None:
            command += ["--score-socket", str(gateway.socket_path)]
        return command

    def build_env(self):
        env = dict(os.environ)
        env["HARNESS_CONFIG"] = str(self.harness_config)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    # -- run ------------------------------------------------------------
    def run(self, context, config):
        output = Path(config.output_dir)
        started = time.monotonic()
        result = AgentRunResult(task_id=context.task_id, run_status=RunStatus.FAILED,
                                runtime=self.name)
        workspace = self.build_workspace(context, output)
        prompt = self.resolve_prompt(output)
        execution = output / "execution"
        result.details.update(workspace=str(workspace), prompt=str(prompt),
                              feedback="gateway" if self.start_gateway else "none")
        with self.open_gateway(context.task_dir, output) as gateway:
            command = self.build_command(workspace, prompt, execution, gateway)
            result.details["command"] = command
            result.details["score_channel"] = gateway.socket_path is not None
            if self.dry_run:
                result.run_status = RunStatus.INFRA_ERROR
                result.error_type = "DryRun"
                result.error_message = "command built only; executor not started"
                result.walltime_sec = 0.0
                return result
            execution.parent.mkdir(parents=True, exist_ok=True)
            try:
                with (output / "executor.stdout.log").open("w") as out, \
                        (output / "executor.stderr.log").open("w") as err:
                    process = subprocess.run(command, env=self.build_env(), cwd=str(self.code_root),
                                             stdout=out, stderr=err, timeout=config.agent_timeout)
            except subprocess.TimeoutExpired:
                result.run_status = RunStatus.TIMEOUT
                result.error_type = "Timeout"
                result.error_message = f"executor exceeded {config.agent_timeout}s"
                result.walltime_sec = round(time.monotonic() - started, 3)
                return result
            except OSError as exc:
                result.run_status = RunStatus.INFRA_ERROR
                result.error_type = type(exc).__name__
                result.error_message = str(exc)
                result.walltime_sec = round(time.monotonic() - started, 3)
                return result
        summary = _read_json(execution / "summary.json")
        result.details.update(executor_exit=process.returncode, summary=summary)
        if summary:
            usage = summary.get("model_usage") or {}
            result.llm_requests = usage.get("request_count")
            result.input_tokens = usage.get("prompt_tokens")
            result.output_tokens = usage.get("generated_tokens")
            result.walltime_sec = summary.get("elapsed_seconds")
        if result.walltime_sec is None:
            result.walltime_sec = round(time.monotonic() - started, 3)
        result.trajectory_path = str(execution)
        if process.returncode != 0 or (summary or {}).get("status") not in (None, "completed"):
            result.run_status = RunStatus.INFRA_ERROR if process.returncode else RunStatus.FAILED
            result.error_type = "ExecutorFailed"
            result.error_message = f"exit={process.returncode} status={(summary or {}).get('status')}"
            return result
        selection = _read_json(workspace / "final_submission.json")
        poc_id = (selection or {}).get("poc_id")
        result.details["final_submission"] = selection
        if not poc_id:
            fallback = workspace / "poc.bin"
            if fallback.is_file() and fallback.stat().st_size:
                result.attach_candidate(fallback.read_bytes())
                result.final_candidate_path = str(fallback)
                result.run_status = RunStatus.COMPLETED
                result.details["selection_source"] = "poc.bin (fallback)"
                return result
            result.run_status = RunStatus.NO_CANDIDATE
            return result
        submission = _select_submission(Path(output) / "score", poc_id)
        if submission is None:
            result.run_status = RunStatus.FAILED
            result.error_type = "InvalidSelection"
            result.error_message = f"no valid submitted candidate for poc_id {poc_id}"
            return result
        artifact = submission.get("submitted_artifact")
        path = (Path(output) / "score" / artifact) if artifact else None
        if path is None or not path.is_file():
            result.run_status = RunStatus.FAILED
            result.error_type = "MissingArtifact"
            result.error_message = f"submitted artifact not found for {poc_id}"
            return result
        data = path.read_bytes()
        result.attach_candidate(data)
        result.final_candidate_path = str(path)
        result.details["poc_id"] = poc_id
        result.details["selection_source"] = "final_submission.json"
        result.run_status = RunStatus.COMPLETED
        return result


def _select_submission(score_dir, poc_id):
    """Mirror the frozen `select_final_submission` validation rules."""
    ledger = Path(score_dir) / "requests.jsonl"
    if not ledger.is_file():
        return None
    matches = []
    for line in ledger.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (row.get("poc_id") == poc_id and row.get("operation") == "score_submit"
                and row.get("complete") is True and row.get("http_status") == 200
                and type(row.get("exit_code")) is int and 1 <= row["exit_code"] <= 255
                and row["exit_code"] not in (126, 127, 137)):
            matches.append(row)
    return matches[-1] if matches else None


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
