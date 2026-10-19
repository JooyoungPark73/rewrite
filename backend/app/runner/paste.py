"""Paste-back: findings JSON copied from a web UI, treated like a runner's answer.

The text goes through the same parser and validator as a live run (app/findings.py); no
process is started and nothing is written besides the run's rows in state.db.
"""

from __future__ import annotations

from app.runner.base import RunResult, RunUsage

NAME = "paste"


def paste_result(raw: str) -> RunResult:
    """A RunResult whose answer is the pasted text."""
    if not raw.strip():
        return RunResult(status="failed", text=raw, error="the pasted text is empty")
    return RunResult(status="ok", text=raw, usage=RunUsage(), command=[])
