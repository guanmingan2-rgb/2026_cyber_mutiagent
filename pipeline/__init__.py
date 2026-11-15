"""CyberGym evaluation pipeline: materials → agent → official dynamic score.

Stages live in one module each:

``data``     read task materials (bounded, hashed, read-only)
``agent``    run one DeepSeek Harness session and validate its JSON handoff
``scoring``  official vulnerable/fixed execution (runs in the solver venv)
``run``      orchestrate the three stages and write ``report.json``
``status``   persist Slurm stage state for ``run_pipeline.sbatch``
"""
