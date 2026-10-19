"""Which login each LLM CLI would run on (GET /api/runners `auth`).

The app is meant to run on the user's Claude Code and Codex subscriptions (claude.ai /
ChatGPT login), never on an API key: `child_env` strips the API-key and cloud-provider
variables unless `[runner] allow_api_keys = true`. This module asks each CLI how it is
logged in, with the same environment a run gets, so the UI can say "via claude.ai" and
refuse a runner that is not logged in at all.

Verified 2026-10-18 on claude 2.1.303 and codex-cli 0.163.0 (no model call either way):

- `claude auth status` prints JSON on stdout (exit 1 when not logged in):
  `{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "team", ...}`.
  authMethod is one of "claude.ai", "oauth_token" (CLAUDE_CODE_OAUTH_TOKEN, a login token),
  "api_key" (ANTHROPIC_API_KEY or an Anthropic Console login, which is a managed API key;
  `apiKeySource` names it), "api_key_helper", "third_party" (Bedrock / Vertex / Foundry)
  or "none".
- `codex login status` prints one line on stderr (exit 1 when not logged in):
  "Logged in using ChatGPT", "Logged in using an API key - sk-...***" or "Not logged in";
  the binary also knows "workload identity", "access token", "personal access token" and
  two Amazon Bedrock wordings. It does not report CODEX_API_KEY, which only `codex exec`
  honours (child_env strips it).

`auth_status` never raises; `AuthCache` (one per AppState) keeps an answer for 60 s.
Nothing here writes anything.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import RunnerCliConfig
from app.runner.base import child_env

AUTH_TIMEOUT_S = 15.0
AUTH_CACHE_TTL_S = 60.0

# The CLI subcommand that reports the login, after the executable.
STATUS_ARGS: dict[str, tuple[str, ...]] = {
    "claude": ("auth", "status"),
    "codex": ("login", "status"),
}
# What to tell the user to run when a CLI is not logged in.
LOGIN_COMMAND: dict[str, str] = {"claude": "claude auth login", "codex": "codex login"}
# `method` values that are a subscription login (claude: authMethod; codex: the wording
# after "Logged in using"). Anything else gets a warning on GET /api/runners.
SUBSCRIPTION_METHODS: dict[str, frozenset[str]] = {
    "claude": frozenset({"claude.ai", "oauth_token"}),
    "codex": frozenset({"ChatGPT"}),
}
API_KEY_WARNING = "logged in with an API key; this app expects a subscription login"


class AuthStatus(BaseModel):
    """GET /api/runners `auth`: None fields mean "could not tell"."""

    logged_in: bool | None = None
    method: str | None = None  # claude: authMethod ("claude.ai"); codex: "ChatGPT", "API key"
    detail: str | None = None  # subscription type, key source, or why it could not tell


# --------------------------------------------------------------------------- parsing


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _json_object(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def parse_claude_status(stdout: str, stderr: str = "", exit_code: int | None = 0) -> AuthStatus:
    """`claude auth status` (JSON on stdout)."""
    data = _json_object(stdout)
    if data is None:
        shown = _first_line(stdout) or _first_line(stderr) or "no output"
        return AuthStatus(
            detail=f"`claude auth status` gave no JSON (exit {exit_code}): {shown[:200]}"
        )
    logged_in = data.get("loggedIn")
    method = data.get("authMethod")
    status = AuthStatus(
        logged_in=logged_in if isinstance(logged_in, bool) else None,
        method=method if isinstance(method, str) and method and method != "none" else None,
    )
    if status.logged_in is False:
        status.detail = "not logged in"
    elif isinstance(data.get("apiKeySource"), str):
        status.detail = f"API key from {data['apiKeySource']}"
    elif isinstance(data.get("subscriptionType"), str):
        status.detail = f"{data['subscriptionType']} subscription"
    elif status.method == "third_party" and isinstance(data.get("apiProvider"), str):
        status.detail = f"provider: {data['apiProvider']}"
    return status


_CODEX_LOGGED_IN = re.compile(r"^Logged in using (?P<method>.+)$")


def parse_codex_status(stdout: str, stderr: str = "", exit_code: int | None = 0) -> AuthStatus:
    """`codex login status` (one line, on stderr; other lines are warnings)."""
    for raw in (stderr + "\n" + stdout).splitlines():
        line = raw.strip()
        if line.startswith("Not logged in"):
            return AuthStatus(logged_in=False, detail="not logged in")
        match = _CODEX_LOGGED_IN.match(line)
        if match:
            # "an API key - sk-proj-***": drop the masked key and the article.
            method = match.group("method").split(" - ", 1)[0].strip()
            method = re.sub(r"^(an|a)\s+", "", method)
            return AuthStatus(logged_in=True, method=method or None)
    shown = _first_line(stderr) or _first_line(stdout) or "no output"
    return AuthStatus(
        detail=f"`codex login status` gave an unknown answer (exit {exit_code}): {shown[:200]}"
    )


PARSERS: dict[str, Callable[[str, str, int | None], AuthStatus]] = {
    "claude": parse_claude_status,
    "codex": parse_codex_status,
}


# --------------------------------------------------------------------------- the check


def _run(
    argv: list[str], env: dict[str, str], cwd: Path | None, timeout_s: float
) -> tuple[int | None, str, str]:
    """(exit code, stdout, stderr); the whole process group is killed on timeout."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.communicate()
        raise
    return process.returncode, stdout, stderr


def auth_status(
    runner: str,
    cli: RunnerCliConfig,
    *,
    allow_api_keys: bool = False,
    cwd: Path | None = None,
    timeout_s: float = AUTH_TIMEOUT_S,
) -> AuthStatus:
    """Ask the configured CLI (`cli.command[0]`) how it is logged in, with the environment a
    run gets. Never raises: anything unexpected is an AuthStatus with only `detail` set."""
    try:
        if runner not in STATUS_ARGS:
            return AuthStatus(detail=f"no login check for runner {runner!r}")
        if not cli.command:
            return AuthStatus(detail="the runner command is empty")
        path = shutil.which(cli.command[0])
        if path is None:
            return AuthStatus(detail=f"{cli.command[0]} was not found on PATH")
        argv = [path, *STATUS_ARGS[runner]]
        if cwd is not None and not cwd.is_dir():
            cwd = None
        env = child_env(allow_api_keys=allow_api_keys)
        try:
            exit_code, stdout, stderr = _run(argv, env, cwd, timeout_s)
        except subprocess.TimeoutExpired:
            return AuthStatus(detail=f"`{' '.join(argv)}` did not answer within {timeout_s:.0f} s")
        except OSError as exc:
            return AuthStatus(detail=f"`{' '.join(argv)}` failed: {exc}")
        return PARSERS[runner](stdout, stderr, exit_code)
    except Exception as exc:  # a status check must never break GET /api/runners
        return AuthStatus(detail=f"login check failed: {exc}")


def auth_error(runner: str, status: AuthStatus | None) -> str | None:
    """The `error` of a runner that is not logged in (it is then unavailable)."""
    if status is None or status.logged_in is not False:
        return None
    return f"not logged in: run `{LOGIN_COMMAND.get(runner, runner + ' login')}`"


def auth_warning(runner: str, status: AuthStatus | None) -> str | None:
    """A warning for a login that is not a claude.ai / ChatGPT subscription (the runner stays
    available: `[runner] allow_api_keys` or the user's own setup may intend it)."""
    if status is None or status.logged_in is not True or status.method is None:
        return None
    if status.method in SUBSCRIPTION_METHODS.get(runner, frozenset()):
        return None
    lowered = status.method.lower()
    if "api key" in lowered or lowered.startswith("api_key"):
        return API_KEY_WARNING
    return (
        f"logged in via {status.method}, not a claude.ai / ChatGPT subscription; "
        "this app expects a subscription login"
    )


# --------------------------------------------------------------------------- cache


class AuthCache:
    """`auth_status` answers kept for AUTH_CACHE_TTL_S (thread-safe; one per AppState).

    A "not logged in" answer is not kept, so the runner comes back as soon as the user has
    logged in; answers that took the full timeout are kept like any other."""

    def __init__(self, ttl_s: float = AUTH_CACHE_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str, bool], tuple[float, AuthStatus]] = {}

    def get(
        self,
        runner: str,
        cli: RunnerCliConfig,
        *,
        allow_api_keys: bool = False,
        cwd: Path | None = None,
    ) -> AuthStatus:
        key = (runner, cli.command[0] if cli.command else "", allow_api_keys)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and now - entry[0] < self.ttl_s:
            return entry[1]
        status = auth_status(runner, cli, allow_api_keys=allow_api_keys, cwd=cwd)
        with self._lock:
            if status.logged_in is False:
                self._entries.pop(key, None)
            else:
                self._entries[key] = (time.monotonic(), status)
        return status

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
