"""Runner interface and the machinery the CLI runners share (PLAN.md "Runner details").

Every run is a fresh CLI process:
- argv = the configured `command` plus the optional argument groups the installed CLI
  accepts (probed once from its `--help`, see `probe_cli`), never a session id;
- cwd = `.rewrite/sandbox/` (only CLAUDE.md and AGENTS.md are there), tools disabled;
- the prompt goes in on stdin, never in argv;
- its own process group (`start_new_session=True`), so cancel and the hard timeout kill the
  CLI and everything it started: SIGTERM to the group, SIGKILL 3 s later;
- the environment minus the variables a parent Claude Code session sets (CLAUDECODE,
  CLAUDE_CODE_*), which would make a nested `claude` refuse to run, and, unless
  `[runner] allow_api_keys = true`, minus the API-key and cloud-provider variables
  (`API_KEY_ENV`), so the CLIs run on the user's claude.ai / ChatGPT login.

stdout is parsed line by line by a per-CLI `StreamParser` that turns events into progress
(`RunEvent`) and collects the answer, usage and errors. A parser can demand an abort, e.g.
when the CLI reports that a tool is enabled or was used: the run then fails.

Nothing here touches the paper repo besides reading the sandbox as cwd.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, Field

from app.config import RunnerCliConfig

Thinking = Literal["low", "medium", "high"]

KILL_GRACE_S = 3.0  # SIGTERM, then SIGKILL this much later
PROBE_TIMEOUT_S = 15.0
STREAM_LIMIT = 64 * 1024 * 1024  # longest stdout line accepted (one JSON event)
STDERR_TAIL_LINES = 200

# Set by a parent Claude Code session; a nested `claude -p` refuses to start with them.
SESSION_ENV_EXACT = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"})
SESSION_ENV_PREFIXES = ("CLAUDE_CODE_",)
# CLAUDE_CODE_* variables that are user configuration (auth, provider), not session state.
# CLAUDE_CODE_OAUTH_TOKEN is a login token (`claude setup-token`, subscription billing) and
# is always kept; the provider switches are kept only with allow_api_keys (API_KEY_ENV).
KEEP_ENV = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    }
)
# Variables that make a CLI bill an API account or a cloud provider instead of the user's
# claude.ai / ChatGPT login (or send its requests elsewhere). Removed from every child
# environment unless `[runner] allow_api_keys = true`. CODEX_API_KEY is honoured by
# `codex exec` only (not by `codex login status`), so it is stripped as well.
API_KEY_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    }
)


class RunEvent(BaseModel):
    """Progress from a running CLI: streamed answer text or a status line."""

    kind: Literal["text", "status"]
    text: str


class RunUsage(BaseModel):
    input_tokens: int | None = None  # claude: input + cache creation + cache read
    output_tokens: int | None = None
    cost_usd: float | None = None  # claude reports it; codex does not (null)
    wall_ms: int | None = None


RunStatus = Literal["ok", "failed", "cancelled", "timeout"]


class RunResult(BaseModel):
    status: RunStatus
    text: str = ""  # the model's final answer as text
    structured: Any | None = None  # structured output, when the CLI returned one
    usage: RunUsage = Field(default_factory=RunUsage)
    raw_events: str = ""  # stdout as received (JSONL)
    stderr: str = ""  # the last lines of stderr
    exit_code: int | None = None
    error: str | None = None
    model: str | None = None  # the model the CLI reported, if it did
    command: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


EventSink = Callable[[RunEvent], None]


class Runner(Protocol):
    """PLAN.md: `run(prompt, schema, model, thinking) -> RunResult`; the keyword arguments
    carry progress events and the cancel handle."""

    name: str

    def command(
        self,
        model: str | None,
        thinking: str | None,
        *,
        schema: dict[str, Any] | None = None,
        schema_file: str | None = None,
    ) -> list[str]: ...

    async def run(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        model: str | None,
        thinking: str | None,
        *,
        on_event: EventSink | None = None,
        handle: ProcessHandle | None = None,
    ) -> RunResult: ...


# --------------------------------------------------------------------------- probing

_FLAG_RE = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*)")


@dataclass(frozen=True)
class CliProbe:
    """What the installed CLI is: found on PATH or not, and the flags its --help lists."""

    available: bool
    path: str | None
    flags: frozenset[str]
    error: str | None = None

    def accepts(self, args: Sequence[str]) -> bool:
        """Every flag token in `args` is listed by --help (values are not checked)."""
        tokens = [a.split("=", 1)[0] for a in args if a.startswith("-") and len(a) > 1]
        return all(token in self.flags for token in tokens)

    def missing(self, args: Sequence[str]) -> list[str]:
        tokens = [a.split("=", 1)[0] for a in args if a.startswith("-") and len(a) > 1]
        return [token for token in tokens if token not in self.flags]


def help_argv(command: Sequence[str]) -> list[str]:
    """The command's leading words (`claude`, `codex exec`) plus `--help`."""
    words: list[str] = []
    for token in command:
        if token.startswith("-"):
            break
        words.append(token)
    return [*words, "--help"]


def parse_help_flags(text: str) -> frozenset[str]:
    return frozenset(_FLAG_RE.findall(text))


def probe_cli(command: Sequence[str], timeout_s: float = PROBE_TIMEOUT_S) -> CliProbe:
    """Run `<cli> [subcommand] --help` once and collect the flags it lists."""
    if not command:
        return CliProbe(False, None, frozenset(), "the runner command is empty")
    path = shutil.which(command[0])
    if path is None:
        return CliProbe(False, None, frozenset(), f"{command[0]} was not found on PATH")
    argv = help_argv(command)
    argv[0] = path
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            env=child_env(),
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CliProbe(True, path, frozenset(), f"`{' '.join(argv)}` failed: {exc}")
    flags = parse_help_flags(proc.stdout + "\n" + proc.stderr)
    error = None if flags else f"`{' '.join(argv)}` listed no flags (exit {proc.returncode})"
    return CliProbe(True, path, flags, error)


# --------------------------------------------------------------------------- environment


def child_env(
    base: Mapping[str, str] | None = None, *, allow_api_keys: bool = False
) -> dict[str, str]:
    """The environment for a CLI run: ours minus a parent Claude Code session's variables
    and, unless `allow_api_keys`, minus the API-key and cloud-provider ones (API_KEY_ENV)."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key in API_KEY_ENV:
            if not allow_api_keys:
                del env[key]
            continue
        if key in KEEP_ENV:
            continue
        if key in SESSION_ENV_EXACT or key.startswith(SESSION_ENV_PREFIXES):
            del env[key]
    return env


# --------------------------------------------------------------------------- process handle


def signal_group(pgid: int, sig: int) -> bool:
    """Send `sig` to a process group; False when the group no longer exists."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


class ProcessHandle:
    """The orchestrator's grip on a running CLI: cancel it (and its process group)."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.cancelled = False
        self.reason: str | None = None  # "cancelled", "timeout" or a parser's abort reason
        self._kill_timer: asyncio.TimerHandle | None = None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def attach(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        if self.cancelled:
            self._terminate()

    def cancel(self, reason: str = "cancelled") -> None:
        """SIGTERM the process group now and SIGKILL it KILL_GRACE_S later. Idempotent."""
        if self.cancelled:
            return
        self.cancelled = True
        self.reason = reason
        if self.process is not None:
            self._terminate()

    def _terminate(self) -> None:
        process = self.process
        if process is None:
            return
        signal_group(process.pid, signal.SIGTERM)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            signal_group(process.pid, signal.SIGKILL)
            return
        self._kill_timer = loop.call_later(KILL_GRACE_S, self._force_kill)

    def _force_kill(self) -> None:
        if self.process is not None:
            signal_group(self.process.pid, signal.SIGKILL)

    def close(self) -> None:
        """Called when the run is over; a pending SIGKILL still fires if the group lives."""
        timer, self._kill_timer = self._kill_timer, None
        if timer is not None and self.process is not None:
            pgid = self.process.pid
            if not signal_group(pgid, 0):  # group already gone: nothing left to kill
                timer.cancel()


# --------------------------------------------------------------------------- stream parsing


@dataclass
class ParsedStream:
    text: str = ""
    structured: Any | None = None
    usage: RunUsage | None = None
    error: str | None = None
    model: str | None = None
    completed: bool = False  # the CLI reported the end of its turn


class StreamParser(Protocol):
    abort_reason: str | None

    def feed(self, line: str) -> list[RunEvent]: ...

    def finish(self) -> ParsedStream: ...


def status(text: str) -> RunEvent:
    return RunEvent(kind="status", text=text)


def text_event(text: str) -> RunEvent:
    return RunEvent(kind="text", text=text)


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def load_event(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- CLI runner


class CliRunner:
    """Runs one configured CLI. Subclasses supply the stream parser."""

    name: ClassVar[str] = ""

    def __init__(
        self,
        config: RunnerCliConfig,
        probe: CliProbe,
        sandbox: Path,
        timeout_s: float = 1200.0,
        *,
        allow_api_keys: bool = False,  # [runner] allow_api_keys: keep API_KEY_ENV for the CLI
    ) -> None:
        self.config = config
        self.probe = probe
        self.sandbox = sandbox
        self.timeout_s = timeout_s
        self.allow_api_keys = allow_api_keys

    # ----------------------------------------------------------------- argv

    def _group(self, args: Sequence[str]) -> bool:
        return bool(args) and self.probe.accepts(args)

    def uses_schema_file(self) -> bool:
        return any("{schema_file}" in arg for arg in self.config.schema_args)

    def command(
        self,
        model: str | None,
        thinking: str | None,
        *,
        schema: dict[str, Any] | None = None,
        schema_file: str | None = None,
    ) -> list[str]:
        """The argv for one run (the prompt is not part of it: it goes on stdin)."""
        cfg = self.config
        argv = list(cfg.command)
        argv += cfg.no_tools_args  # always: a CLI that cannot disable tools must not run
        if self._group(cfg.isolation_args):
            argv += cfg.isolation_args
        if self._group(cfg.stream_args):
            argv += cfg.stream_args
        if model and cfg.model_flag and self.probe.accepts([cfg.model_flag]):
            argv += [cfg.model_flag, model]
        if thinking and self._group(cfg.thinking_args):
            value = cfg.thinking.get(thinking, thinking)  # type: ignore[call-overload]
            argv += [arg.replace("{thinking}", value) for arg in cfg.thinking_args]
        if schema is not None and self._group(cfg.schema_args):
            schema_text = json.dumps(schema, separators=(",", ":"))
            file_value = schema_file or "<findings.schema.json>"
            argv += [
                arg.replace("{schema_file}", file_value).replace("{schema}", schema_text)
                for arg in cfg.schema_args
            ]
        return argv

    def make_parser(self) -> StreamParser:
        raise NotImplementedError

    # ----------------------------------------------------------------- run

    async def run(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        model: str | None,
        thinking: str | None,
        *,
        on_event: EventSink | None = None,
        handle: ProcessHandle | None = None,
    ) -> RunResult:
        handle = handle or ProcessHandle()
        emit = on_event or (lambda event: None)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="rewrite-run-") as tmp:
            schema_file: str | None = None
            if schema is not None and self.uses_schema_file():
                path = Path(tmp) / "findings.schema.json"
                path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
                schema_file = str(path)
            argv = self.command(model, thinking, schema=schema, schema_file=schema_file)
            result = await self._run_process(argv, prompt, emit, handle)
        result.usage.wall_ms = int((time.monotonic() - started) * 1000)
        return result

    async def _run_process(
        self, argv: list[str], prompt: str, emit: EventSink, handle: ProcessHandle
    ) -> RunResult:
        if handle.cancelled:
            return RunResult(status="cancelled", command=argv, error="cancelled before start")
        parser = self.make_parser()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.sandbox,
                env=child_env(allow_api_keys=self.allow_api_keys),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=STREAM_LIMIT,
            )
        except OSError as exc:
            return RunResult(
                status="failed", command=argv, error=f"could not start {argv[0]}: {exc}"
            )
        handle.attach(process)
        emit(status(f"started {self.name} (pid {process.pid})"))
        stdout_lines: list[str] = []
        stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)

        async def write_stdin() -> None:
            assert process.stdin is not None
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.write(prompt.encode("utf-8"))
                await process.stdin.drain()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.close()

        async def read_stdout() -> None:
            assert process.stdout is not None
            while True:
                try:
                    raw = await process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    handle.cancel("a stdout line exceeded the size limit")
                    return
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace")
                stdout_lines.append(line)
                for event in parser.feed(line.rstrip("\r\n")):
                    emit(event)
                if parser.abort_reason and not handle.cancelled:
                    emit(status(f"aborting: {parser.abort_reason}"))
                    handle.cancel(parser.abort_reason)

        async def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    return
                stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

        work = asyncio.gather(write_stdin(), read_stdout(), read_stderr(), process.wait())
        try:
            await asyncio.wait_for(work, timeout=self.timeout_s)
        except TimeoutError:
            handle.cancel("timeout")
        except asyncio.CancelledError:
            handle.cancel("cancelled")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), KILL_GRACE_S + 2)
            handle.close()
            raise
        if process.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), KILL_GRACE_S + 2)
        handle.close()

        parsed = parser.finish()
        stderr_text = "\n".join(stderr_tail)
        result = RunResult(
            status="ok",
            text=parsed.text,
            structured=parsed.structured,
            usage=parsed.usage or RunUsage(),
            raw_events="".join(stdout_lines),
            stderr=stderr_text,
            exit_code=process.returncode,
            model=parsed.model,
            command=argv,
        )
        if handle.cancelled:
            if handle.reason == "timeout":
                result.status = "timeout"
                result.error = f"timed out after {self.timeout_s:.0f} s; the process was killed"
            elif handle.reason == "cancelled":
                result.status = "cancelled"
                result.error = "cancelled"
            else:
                result.status = "failed"
                result.error = handle.reason
            return result
        if parsed.error:
            result.status = "failed"
            result.error = parsed.error
        elif process.returncode != 0:
            tail = " / ".join(line for line in list(stderr_tail)[-3:] if line.strip())
            result.status = "failed"
            result.error = f"{self.name} exited with {process.returncode}: {tail or 'no output'}"
        elif not parsed.text.strip() and parsed.structured is None:
            result.status = "failed"
            result.error = f"{self.name} finished without an answer"
        return result
