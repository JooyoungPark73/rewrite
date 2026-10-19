"""app/runner: flags verified against the captured --help, argv building, event parsing from
real CLI streams, the environment, and the process handling (stdin, cwd, cancel, timeout)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.config import RunnerCliConfig, runner_default
from app.findings import findings_schema
from app.runner import ClaudeRunner, CodexRunner, ProcessHandle, RunEvent
from app.runner.auth import AuthStatus, auth_error, auth_status, auth_warning
from app.runner.base import (
    API_KEY_ENV,
    CliProbe,
    child_env,
    help_argv,
    parse_help_flags,
    probe_cli,
)
from app.runner.claude import ClaudeStreamParser
from app.runner.codex import CodexStreamParser
from tests.conftest import FIXTURES, fake_exe

RUNNER_FIXTURES = FIXTURES / "runner"
CLAUDE_FLAGS = parse_help_flags((RUNNER_FIXTURES / "claude-help.txt").read_text())
CODEX_FLAGS = parse_help_flags((RUNNER_FIXTURES / "codex-exec-help.txt").read_text())


def full_probe(flags: frozenset[str]) -> CliProbe:
    return CliProbe(True, "/usr/bin/x", flags)


def feed(parser: ClaudeStreamParser | CodexStreamParser, name: str) -> list[RunEvent]:
    events: list[RunEvent] = []
    for line in (RUNNER_FIXTURES / name).read_text().splitlines():
        events += parser.feed(line)
    return events


# --------------------------------------------------------------------------- flags


@pytest.mark.parametrize(("name", "flags"), [("claude", CLAUDE_FLAGS), ("codex", CODEX_FLAGS)])
def test_default_config_flags_are_in_the_installed_help(name: str, flags: frozenset[str]) -> None:
    """Every flag the default config uses appears in the captured --help (claude 2.1.302,
    codex-cli 0.162.1)."""
    cli = runner_default(name)
    groups = [
        cli.command[1:],
        cli.no_tools_args,
        cli.isolation_args,
        cli.stream_args,
        cli.thinking_args,
        cli.schema_args,
        [cli.model_flag],
    ]
    probe = full_probe(flags)
    for group in groups:
        assert probe.missing(group) == [], (name, group)


def test_help_argv() -> None:
    assert help_argv(runner_default("claude").command) == ["claude", "--help"]
    assert help_argv(runner_default("codex").command) == ["codex", "exec", "--help"]


def test_claude_command_with_all_flags(tmp_path: Path) -> None:
    runner = ClaudeRunner(runner_default("claude"), full_probe(CLAUDE_FLAGS), tmp_path)
    schema = findings_schema()
    argv = runner.command("sonnet", "low", schema=schema)
    assert argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--no-session-persistence",
        "--setting-sources",
        "",  # no setting source: no CLAUDE.md from the paper repo (audit item 7)
        "--strict-mcp-config",
        "--include-partial-messages",
        "--model",
        "sonnet",
        "--effort",
        "low",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
    ]
    assert not any(a in argv for a in ("--resume", "--continue", "--session-id", "-c"))


def test_codex_command_with_all_flags(tmp_path: Path) -> None:
    runner = CodexRunner(runner_default("codex"), full_probe(CODEX_FLAGS), tmp_path)
    argv = runner.command(None, "high", schema=findings_schema(), schema_file="/tmp/s.json")
    assert argv == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-c",
        "project_doc_max_bytes=0",  # no AGENTS.md from the paper repo (audit item 7)
        "-c",
        "project_root_markers=[]",  # no repo skills either
        "--disable",
        "shell_tool",
        "--disable",
        "unified_exec",
        "--ignore-user-config",
        "--ignore-rules",
        "-c",
        "model_reasoning_effort=high",
        "--output-schema",
        "/tmp/s.json",
    ]
    assert runner.uses_schema_file()


def test_optional_groups_dropped_when_the_cli_lacks_them(tmp_path: Path) -> None:
    cli = runner_default("claude")
    cli.thinking = {"low": "low", "medium": "medium", "high": "max"}
    old = CliProbe(True, "/x", frozenset({"-p", "--output-format", "--verbose", "--model"}))
    argv = ClaudeRunner(cli, old, tmp_path).command("", "high", schema=findings_schema())
    assert argv == cli.command  # no model (empty), no effort, schema, isolation or stream flags
    argv = ClaudeRunner(cli, full_probe(CLAUDE_FLAGS), tmp_path).command(None, "high")
    assert argv[-2:] == ["--effort", "max"]  # the configured mapping
    codex = runner_default("codex")
    bare = CodexRunner(codex, CliProbe(True, "/x", frozenset()), tmp_path).command("m", "low")
    assert bare == codex.command + codex.no_tools_args  # tools are always disabled


# --------------------------------------------------------------------------- parsers


def test_claude_fixture_stream() -> None:
    parser = ClaudeStreamParser()
    events = feed(parser, "claude-stream.jsonl")
    parsed = parser.finish()
    assert parser.abort_reason is None
    assert parsed.text == '{"ok": true}' and parsed.completed and parsed.error is None
    assert parsed.model == "claude-sonnet-5-5"
    assert parsed.usage is not None
    assert parsed.usage.cost_usd == pytest.approx(0.0083749)
    assert parsed.usage.input_tokens == 2 + 2054 + 649 and parsed.usage.output_tokens == 9
    assert events[0].kind == "status" and "tools: none" in events[0].text
    assert any(e.kind == "text" and e.text == '{"ok": true}' for e in events)


def test_claude_structured_output_stream() -> None:
    """Captured with --json-schema --include-partial-messages: StructuredOutput is the only
    tool, deltas stream the JSON, the result carries structured_output."""
    parser = ClaudeStreamParser()
    events = feed(parser, "claude-schema-stream.jsonl")
    parsed = parser.finish()
    assert parser.abort_reason is None
    assert parsed.structured == {
        "findings": [{"word": "red", "json_ref": None}, {"word": "blue", "json_ref": None}]
    }
    streamed = "".join(e.text for e in events if e.kind == "text")
    assert json.loads(streamed) == parsed.structured  # no duplicate from the assistant message
    assert parsed.usage is not None and parsed.usage.cost_usd == pytest.approx(0.014688)


def test_claude_tools_enabled_aborts() -> None:
    parser = ClaudeStreamParser()
    parser.feed(json.dumps({"type": "system", "subtype": "init", "tools": ["Bash", "Read"]}))
    assert parser.abort_reason is not None and "Bash" in parser.abort_reason
    late = ClaudeStreamParser()
    late.feed(json.dumps({"type": "system", "subtype": "init", "tools": []}))
    late.feed(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Write", "input": {}}]},
            }
        )
    )
    assert late.abort_reason is not None and "Write" in late.abort_reason


def test_claude_error_result() -> None:
    parser = ClaudeStreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "model not found",
                "usage": {},
            }
        )
    )
    assert parser.finish().error == "model not found"


def test_codex_fixture_stream() -> None:
    parser = CodexStreamParser()
    events = feed(parser, "codex-stream.jsonl")
    parsed = parser.finish()
    assert parser.abort_reason is None and parsed.error is None and parsed.completed
    assert parsed.text == '{"ok": true}'
    assert parsed.usage is not None
    assert (parsed.usage.input_tokens, parsed.usage.output_tokens) == (15358, 9)
    assert parsed.usage.cost_usd is None
    assert [e.text for e in events if e.kind == "text"] == ['{"ok": true}']


def test_codex_schema_stream_answer_is_the_last_message() -> None:
    parser = CodexStreamParser()
    feed(parser, "codex-schema-stream.jsonl")
    assert json.loads(parser.finish().text)["findings"][0]["word"] == "red"


def test_codex_tool_use_aborts() -> None:
    """Shape observed on codex-cli 0.162.1 with the shell tool enabled."""
    parser = CodexStreamParser()
    feed(parser, "codex-tool-stream.jsonl")
    assert parser.abort_reason is not None and "command_execution" in parser.abort_reason


def test_codex_failed_turn() -> None:
    parser = CodexStreamParser()
    parser.feed(json.dumps({"type": "error", "message": "Reconnecting... 1/5"}))
    parser.feed(json.dumps({"type": "turn.failed", "error": {"message": "quota exceeded"}}))
    assert parser.finish().error == "quota exceeded"


# --------------------------------------------------------------------------- env and probe


def test_child_env_strips_parent_session_variables() -> None:
    env = child_env(
        {
            "PATH": "/bin",
            "HOME": "/home/x",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "abc",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_PID": "42",
            "CLAUDE_EFFORT": "max",
            "CLAUDE_CODE_OAUTH_TOKEN": "keep-me",
            "CLAUDE_CONFIG_DIR": "/cfg",
        }
    )
    assert env == {
        "PATH": "/bin",
        "HOME": "/home/x",
        "CLAUDE_CODE_OAUTH_TOKEN": "keep-me",
        "CLAUDE_CONFIG_DIR": "/cfg",
    }


# Everything that would bill an API account or a cloud provider instead of the login.
API_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-x",
    "ANTHROPIC_AUTH_TOKEN": "tok",
    "ANTHROPIC_BASE_URL": "https://gateway.example",
    "OPENAI_API_KEY": "sk-x",
    "OPENAI_BASE_URL": "https://proxy.example",
    "CODEX_API_KEY": "sk-c",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH": "1",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH": "1",
}


def test_child_env_strips_api_keys_unless_allowed() -> None:
    """The CLIs run on the claude.ai / ChatGPT login: API-key and cloud-provider variables
    are removed by default and kept only with allow_api_keys. CLAUDE_CODE_OAUTH_TOKEN is a
    login token and always stays; session variables always go."""
    assert set(API_ENV) == API_KEY_ENV
    base = {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "login", "CLAUDECODE": "1", **API_ENV}
    assert child_env(base) == {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "login"}
    assert child_env(base, allow_api_keys=True) == {
        "PATH": "/bin",
        "CLAUDE_CODE_OAUTH_TOKEN": "login",
        **API_ENV,
    }


def test_probe_cli(tmp_path: Path) -> None:
    exe = fake_exe(tmp_path, "fakecli", 'echo "usage: --model <m> --effort <e>  -p, --print"\n')
    probe = probe_cli([str(exe), "-p", "--model", "x"])
    assert probe.available and probe.flags >= {"--model", "--effort", "-p", "--print"}
    assert probe.accepts(["--effort", "{thinking}"]) and not probe.accepts(["--json-schema"])
    missing = probe_cli([str(tmp_path / "nope")])
    assert not missing.available and "not found" in (missing.error or "")


# --------------------------------------------------------------------------- processes

FAKE_STREAM = """\
case "$*" in *--help*) echo "-p --model --effort --json-schema"; exit 0;; esac
pwd > "$(dirname "$0")/cwd"
cat > "$(dirname "$0")/stdin"
env > "$(dirname "$0")/env"
cat "{fixture}"
"""


def make_runner(tmp_path: Path, body: str, timeout_s: float = 30.0) -> tuple[ClaudeRunner, Path]:
    bin_dir = tmp_path / "bin"
    exe = fake_exe(bin_dir, "fake-claude", body)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    cli = RunnerCliConfig(command=[str(exe), "-p"], model_flag="--model")
    probe = probe_cli(cli.command)
    return ClaudeRunner(cli, probe, sandbox, timeout_s=timeout_s), bin_dir


async def test_run_streams_prompt_on_stdin_in_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    fixture = RUNNER_FIXTURES / "claude-stream.jsonl"
    runner, bin_dir = make_runner(tmp_path, FAKE_STREAM.replace("{fixture}", str(fixture)))
    events: list[RunEvent] = []
    prompt = "Review this.\n" + "x" * 200_000  # larger than a pipe buffer
    result = await runner.run(prompt, None, "sonnet", None, on_event=events.append)
    assert result.ok, result.error
    assert result.text == '{"ok": true}'
    assert (bin_dir / "stdin").read_text() == prompt
    assert Path((bin_dir / "cwd").read_text().strip()).resolve() == (tmp_path / "sandbox").resolve()
    env = (bin_dir / "env").read_text()
    assert "CLAUDECODE=" not in env and "CLAUDE_CODE_SESSION_ID=" not in env
    assert "ANTHROPIC_API_KEY=" not in env and "OPENAI_API_KEY=" not in env
    assert result.command[-2:] == ["--model", "sonnet"]
    assert prompt not in " ".join(result.command)  # never in argv
    assert result.raw_events == fixture.read_text()
    assert result.usage.wall_ms is not None and result.usage.cost_usd is not None
    assert events[0].text.startswith("started claude")


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def wait_gone(pgid: int, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        await asyncio.sleep(0.05)
    return False


SLEEPER = """\
case "$*" in *--help*) echo "-p --model"; exit 0;; esac
echo $$ > "$(dirname "$0")/pid"
sleep 30 &
echo $! > "$(dirname "$0")/child"
wait
"""


async def wait_file(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


async def test_cancel_kills_the_process_group(tmp_path: Path) -> None:
    runner, bin_dir = make_runner(tmp_path, SLEEPER)
    handle = ProcessHandle()
    task = asyncio.create_task(runner.run("prompt", None, None, None, handle=handle))
    pid = int(await wait_file(bin_dir / "pid"))
    child = int(await wait_file(bin_dir / "child"))
    assert handle.pid == pid and os.getpgid(child) == pid  # the sleep is in the CLI's group
    started = time.monotonic()
    handle.cancel()
    result = await task
    assert result.status == "cancelled"
    assert time.monotonic() - started < 5
    assert await wait_gone(pid), "a process of the cancelled run's group survived"
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


async def test_sigterm_ignored_gets_sigkill(tmp_path: Path) -> None:
    body = SLEEPER.replace("sleep 30 &", "trap '' TERM; sleep 30 &")
    runner, bin_dir = make_runner(tmp_path, body)
    handle = ProcessHandle()
    task = asyncio.create_task(runner.run("p", None, None, None, handle=handle))
    pid = int(await wait_file(bin_dir / "pid"))
    handle.cancel()
    result = await task
    assert result.status == "cancelled"
    assert await wait_gone(pid, timeout=8.0)


async def test_hard_timeout_kills_the_run(tmp_path: Path) -> None:
    runner, bin_dir = make_runner(tmp_path, SLEEPER, timeout_s=0.5)
    result = await runner.run("p", None, None, None)
    pid = int((bin_dir / "pid").read_text())
    assert result.status == "timeout" and "timed out" in (result.error or "")
    assert await wait_gone(pid)


async def test_enabled_tools_abort_the_run(tmp_path: Path) -> None:
    body = (
        'case "$*" in *--help*) echo "-p"; exit 0;; esac\n'
        'echo $$ > "$(dirname "$0")/pid"\n'
        'echo \'{"type":"system","subtype":"init","tools":["Bash","Edit"]}\'\n'
        "sleep 30\n"
    )
    runner, bin_dir = make_runner(tmp_path, body)
    result = await runner.run("p", None, None, None)
    assert result.status == "failed" and "tools enabled" in (result.error or "")
    assert await wait_gone(int((bin_dir / "pid").read_text()))


async def test_exit_status_and_stderr(tmp_path: Path) -> None:
    body = (
        'case "$*" in *--help*) echo "-p"; exit 0;; esac\n'
        'cat >/dev/null\necho "bad flag --foo" >&2\nexit 2\n'
    )
    runner, _ = make_runner(tmp_path, body)
    result = await runner.run("p", None, None, None)
    assert result.status == "failed"
    assert result.exit_code == 2 and "bad flag --foo" in (result.error or "")


async def test_missing_executable(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    cli = RunnerCliConfig(command=[str(tmp_path / "missing")], model_flag="")
    runner = ClaudeRunner(cli, CliProbe(True, None, frozenset()), sandbox)
    result = await runner.run("p", None, None, None)
    assert result.status == "failed" and "could not start" in (result.error or "")


@pytest.mark.parametrize(
    ("parser_cls", "name", "count"),
    [
        (ClaudeStreamParser, "claude-review-stream.jsonl", 3),
        (CodexStreamParser, "codex-review-stream.jsonl", 5),
    ],
)
def test_real_review_streams_parse_validate_and_anchor(
    parser_cls: type[ClaudeStreamParser] | type[CodexStreamParser], name: str, count: int
) -> None:
    """Streams of the phase-4 smoke runs (paragraph-function on the sample Introduction,
    claude sonnet/low with --json-schema --include-partial-messages; codex default/low with
    --output-schema): every finding validates and anchors to the real lines."""
    from app.findings import anchor_new, parse_output, validate_findings

    parser = parser_cls()
    events = feed(parser, name)
    assert parser.abort_reason is None
    parsed = parser.finish()
    assert parsed.error is None and parsed.completed
    output = parse_output(parsed.text, parsed.structured)
    accepted, rejected = validate_findings(output.items, allow_rewrites=False)
    assert (len(accepted), rejected, output.rejected) == (count, [], [])
    lines = (RUNNER_FIXTURES / "intro-adopted.tex").read_text().splitlines()
    for _, item in accepted:
        assert item.file == "sections/intro.tex" and item.json_ref in ("p-1", "p-2", "p-3", "p-4")
        assert anchor_new(item.quote, lines, item.line_start, item.line_end).state == "ok"
    if parser_cls is ClaudeStreamParser:
        assert output.method == "structured"
        assert sum(1 for e in events if e.kind == "text") > 20  # streamed deltas
        assert parsed.usage is not None and parsed.usage.cost_usd == pytest.approx(0.0577539)
    else:
        assert parsed.usage is not None and parsed.usage.output_tokens == 710


# --------------------------------------------------------------------------- audit item 7


def canary_paper(root: Path) -> Path:
    """A git paper repo whose root carries instruction files a CLI could discover from the
    sandbox (cwd) by walking up; returns the sandbox. Every file holds a distinct canary."""
    import subprocess

    paper = root / "paper"
    sandbox = paper / ".rewrite" / "sandbox"
    sandbox.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(paper)], check=True)
    (paper / "AGENTS.md").write_text("ROOT_AGENTS_CANARY\n")
    (paper / "CLAUDE.md").write_text("ROOT_CLAUDE_CANARY\n")
    (paper / ".claude").mkdir()
    (paper / ".claude" / "CLAUDE.md").write_text("ROOT_DOTCLAUDE_CANARY\n")
    for base in (".agents", ".codex"):
        skill = paper / base / "skills" / f"{base.strip('.')}-canary"
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            f"---\nname: {base.strip('.')}-canary\ndescription: SKILL_CANARY_{base.strip('.')}\n"
            "---\nbody\n"
        )
    (sandbox / "AGENTS.md").write_text("SANDBOX_AGENTS_CANARY\n")
    (sandbox / "CLAUDE.md").write_text("SANDBOX_CLAUDE_CANARY\n")
    return sandbox


def _option_pairs(argv: list[str], flags: tuple[str, ...]) -> list[str]:
    out = []
    for index, token in enumerate(argv[:-1]):
        if token in flags:
            out += [token, argv[index + 1]]
    return out


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex is not installed")
def test_codex_production_argv_ignores_ancestor_instruction_files(tmp_path: Path) -> None:
    """Audit 2026-10-18 item 7: from cwd .rewrite/sandbox/ codex also reads the paper
    root's AGENTS.md (and repo skills) unless told not to. `codex debug prompt-input`
    (offline, no model call, empty CODEX_HOME) shows the effective instructions for the
    production argv's `-c`/`--disable` options."""
    sandbox = canary_paper(tmp_path)
    home = tmp_path / "codex-home"
    home.mkdir()
    probe = CliProbe(True, shutil.which("codex"), CODEX_FLAGS)
    argv = CodexRunner(runner_default("codex"), probe, sandbox).command(None, "low")
    options = _option_pairs(argv, ("-c", "--config", "--disable", "--enable"))
    env = {**child_env(), "CODEX_HOME": str(home)}
    env.pop("OPENAI_API_KEY", None)

    def prompt_input(opts: list[str]) -> str:
        proc = subprocess.run(
            ["codex", *opts, "debug", "prompt-input", "Review the submitted paper."],
            cwd=sandbox,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    tools_only = _option_pairs(argv, ("--disable", "--enable"))
    control = prompt_input(tools_only)  # what the old argv let through: the leak is visible
    assert "ROOT_AGENTS_CANARY" in control and "SKILL_CANARY_agents" in control
    effective = prompt_input(options)
    for canary in ("ROOT_AGENTS_CANARY", "SKILL_CANARY_agents", "SKILL_CANARY_codex"):
        assert canary not in effective, canary
    assert "Review the submitted paper." in effective


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not installed")
def test_claude_production_argv_ignores_ancestor_instruction_files(tmp_path: Path) -> None:
    """Audit item 7 for claude: with `--setting-sources project` it loads every CLAUDE.md
    from the sandbox up to / (the paper root's included). Checked with the local `/context`
    command under an empty CLAUDE_CONFIG_DIR and no credentials, so no model call can be
    made; the memory-file table lists what a run would load."""
    sandbox = canary_paper(tmp_path)
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    probe = CliProbe(True, shutil.which("claude"), CLAUDE_FLAGS)
    argv = ClaudeRunner(runner_default("claude"), probe, sandbox).command(None, "low")
    isolation = _option_pairs(argv, ("--setting-sources",))
    assert isolation, argv
    env = {**child_env(), "CLAUDE_CONFIG_DIR": str(config_dir)}
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(key, None)

    def context(sources: list[str]) -> str:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                "--output-format",
                "text",
                "--tools",
                "",
                "--no-session-persistence",
                "--strict-mcp-config",
                *sources,
                "/context",
            ],
            cwd=sandbox,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout

    root_claude = str(sandbox.parent.parent / "CLAUDE.md")
    control = context(["--setting-sources", "project"])  # the phase-4 argv: leaks
    assert root_claude in control
    effective = context(isolation)
    assert root_claude not in effective
    assert str(sandbox.parent.parent / ".claude" / "CLAUDE.md") not in effective
    assert "## Context Usage" in effective


def test_context_probe_lists_ancestor_instruction_files(tmp_path: Path) -> None:
    """Audit item 7: the probe walks from the sandbox up to / and lists the instruction
    files and folders the CLIs could discover outside the sandbox, marking whether the
    argv in use keeps each one out of the run (as verified with the installed CLIs)."""
    from app.runner.context import context_leaks, context_warning

    outer = tmp_path / "outer"
    sandbox = canary_paper(outer)
    paper = sandbox.parent.parent.resolve()
    (outer / "AGENTS.md").write_text("above the git root\n")
    (outer / "CLAUDE.md").write_text("above the git root\n")
    (paper / "CLAUDE.local.md").write_text("local\n")
    (paper / ".claude" / "settings.json").write_text("{}\n")
    (paper / ".claude" / "settings.local.json").write_text("{}\n")
    (paper / ".claude" / "agents").mkdir()
    (paper / ".claude" / "skills").mkdir()

    codex = CodexRunner(runner_default("codex"), full_probe(CODEX_FLAGS), sandbox)
    leaks = context_leaks(sandbox, "codex", codex.command(None, "low"))
    found = {leak.path: leak.suppressed for leak in leaks}
    assert {leak.runner for leak in leaks} == {"codex"}
    for path in (
        paper / "AGENTS.md",
        paper / ".agents" / "skills",
        paper / ".codex",
        outer.resolve() / "AGENTS.md",
    ):
        assert found[str(path)] is True, path
    assert not any(path.startswith(str(sandbox.resolve())) for path in found)
    assert context_warning(leaks) is None

    old = ["codex", "exec", "--json", "--disable", "shell_tool"]  # the phase-4 command
    stale = {leak.path: leak.suppressed for leak in context_leaks(sandbox, "codex", old)}
    assert stale[str(paper / "AGENTS.md")] is False and stale[str(paper / ".codex")] is False
    doc_only = {
        leak.path: leak.suppressed
        for leak in context_leaks(sandbox, "codex", [*old, "-c", "project_doc_max_bytes=0"])
    }
    assert doc_only[str(paper / "AGENTS.md")] is True  # docs off, but repo skills still found
    assert doc_only[str(paper / ".agents" / "skills")] is False
    warning = context_warning(context_leaks(sandbox, "codex", old))
    assert warning is not None and str(paper / "AGENTS.md") in warning

    claude = ClaudeRunner(runner_default("claude"), full_probe(CLAUDE_FLAGS), sandbox)
    leaks = context_leaks(sandbox, "claude", claude.command(None, "low"))
    found = {leak.path: leak.suppressed for leak in leaks}
    for name in (
        "CLAUDE.md",
        ".claude/CLAUDE.md",
        "CLAUDE.local.md",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/agents",
        ".claude/skills",
    ):
        assert found[str(paper / name)] is True, name
    assert found[str(outer.resolve() / "CLAUDE.md")] is True
    assert context_warning(leaks) is None
    project = {
        leak.path: leak.suppressed
        for leak in context_leaks(
            sandbox, "claude", ["claude", "-p", "--setting-sources", "project"]
        )
    }
    assert project[str(paper / "CLAUDE.md")] is False
    assert project[str(outer.resolve() / "CLAUDE.md")] is False  # claude walks up to /
    assert project[str(paper / "CLAUDE.local.md")] is True  # a "local" source
    assert project[str(paper / ".claude" / "settings.json")] is True  # read from cwd only
    every_source = {
        leak.path: leak.suppressed for leak in context_leaks(sandbox, "claude", ["claude", "-p"])
    }
    assert every_source[str(paper / "CLAUDE.local.md")] is False


# --------------------------------------------------------------------------- login state

# What the CLIs print, captured 2026-10-18 from claude 2.1.303 and codex-cli 0.163.0
# (personal fields left out). claude: JSON on stdout; codex: one line on stderr.
CLAUDE_SUBSCRIPTION = json.dumps(
    {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "analyticsDisabled": False,
        "subscriptionType": "team",
    },
    indent=2,
)
CLAUDE_LOGGED_OUT = json.dumps(
    {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}, indent=2
)
CLAUDE_API_KEY = json.dumps(
    {
        "loggedIn": True,
        "authMethod": "api_key",
        "apiProvider": "firstParty",
        "apiKeySource": "ANTHROPIC_API_KEY",
    },
    indent=2,
)
CLAUDE_BEDROCK = json.dumps(
    {"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"}, indent=2
)
CLAUDE_SETUP_TOKEN = json.dumps({"loggedIn": True, "authMethod": "oauth_token"}, indent=2)
CODEX_WARNING = (
    "WARNING: proceeding, even though we could not create PATH aliases: Refusing to create "
    'helper binaries under temporary dir "/tmp"'
)
API_KEY_WARNING = "logged in with an API key; this app expects a subscription login"


def status_exe(tmp_path: Path, stdout: str, stderr: str, exit_code: int) -> RunnerCliConfig:
    """A fake CLI that answers `<cli> auth status` / `<cli> login status` (and only that)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "out.txt").write_text(stdout)
    (tmp_path / "err.txt").write_text(stderr)
    body = (
        'case "$1 $2" in "auth status"|"login status") ;; *) echo "unexpected: $*" >&2; '
        "exit 9;; esac\n"
        f'cat "{tmp_path}/out.txt"; cat "{tmp_path}/err.txt" >&2; exit {exit_code}\n'
    )
    exe = fake_exe(tmp_path / "bin", "fake-cli", body)
    return RunnerCliConfig(command=[str(exe), "-p"])


@pytest.mark.parametrize(
    ("runner", "stdout", "stderr", "exit_code", "expected", "warning"),
    [
        (
            "claude",
            CLAUDE_SUBSCRIPTION,
            "",
            0,
            AuthStatus(logged_in=True, method="claude.ai", detail="team subscription"),
            None,
        ),
        (
            "claude",
            CLAUDE_LOGGED_OUT,
            "",
            1,
            AuthStatus(logged_in=False, method=None, detail="not logged in"),
            None,
        ),
        (
            "claude",
            CLAUDE_API_KEY,
            "",
            0,
            AuthStatus(logged_in=True, method="api_key", detail="API key from ANTHROPIC_API_KEY"),
            API_KEY_WARNING,
        ),
        (
            "claude",
            CLAUDE_SETUP_TOKEN,  # CLAUDE_CODE_OAUTH_TOKEN: a login token, no warning
            "",
            0,
            AuthStatus(logged_in=True, method="oauth_token"),
            None,
        ),
        (
            "codex",
            "",
            "Logged in using ChatGPT\n",
            0,
            AuthStatus(logged_in=True, method="ChatGPT"),
            None,
        ),
        (
            "codex",
            "",
            f"{CODEX_WARNING}\nNot logged in\n",
            1,
            AuthStatus(logged_in=False, detail="not logged in"),
            None,
        ),
        (
            "codex",
            "",
            f"{CODEX_WARNING}\nLogged in using an API key - sk-fa***\n",
            0,
            AuthStatus(logged_in=True, method="API key"),
            API_KEY_WARNING,
        ),
    ],
    ids=[
        "claude-subscription",
        "claude-logged-out",
        "claude-api-key",
        "claude-setup-token",
        "codex-chatgpt",
        "codex-logged-out",
        "codex-api-key",
    ],
)
def test_auth_status_parses_the_cli_answer(
    tmp_path: Path,
    runner: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    expected: AuthStatus,
    warning: str | None,
) -> None:
    status = auth_status(runner, status_exe(tmp_path, stdout, stderr, exit_code))
    assert status == expected
    assert auth_warning(runner, status) == warning
    login = "claude auth login" if runner == "claude" else "codex login"
    assert auth_error(runner, status) == (
        f"not logged in: run `{login}`" if expected.logged_in is False else None
    )


def test_auth_status_never_raises(tmp_path: Path) -> None:
    """Unknown output, a missing CLI and a CLI that hangs each give an AuthStatus that only
    says why (logged_in None: the runner stays available, without a note)."""
    garbage = auth_status("claude", status_exe(tmp_path / "a", "Usage: claude ...\n", "", 2))
    assert garbage.logged_in is None and "no JSON (exit 2)" in (garbage.detail or "")
    odd = auth_status("codex", status_exe(tmp_path / "b", "", "something else\n", 0))
    assert odd.logged_in is None and "something else" in (odd.detail or "")
    missing = auth_status("claude", RunnerCliConfig(command=[str(tmp_path / "nope")]))
    assert missing.logged_in is None and "not found" in (missing.detail or "")
    assert auth_status("claude", RunnerCliConfig(command=[])).detail
    hang = fake_exe(tmp_path / "c", "hang", "sleep 30\n")
    started = time.monotonic()
    slow = auth_status("codex", RunnerCliConfig(command=[str(hang)]), timeout_s=0.5)
    assert time.monotonic() - started < 10
    assert slow.logged_in is None and "did not answer" in (slow.detail or "")
    assert auth_warning("claude", AuthStatus()) is None and auth_error("x", None) is None
    bedrock = auth_status("claude", status_exe(tmp_path / "d", CLAUDE_BEDROCK, "", 0))
    assert bedrock == AuthStatus(logged_in=True, method="third_party", detail="provider: bedrock")
    assert "not a claude.ai / ChatGPT subscription" in (auth_warning("claude", bedrock) or "")


def test_auth_status_uses_the_run_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login check sees what a run would see: no API keys unless allowed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    exe = fake_exe(
        tmp_path,
        "envcli",
        'printf \'{"loggedIn": true, "authMethod": "%s"}\' "${ANTHROPIC_API_KEY:+api_key}"\n',
    )
    cli = RunnerCliConfig(command=[str(exe)])
    assert auth_status("claude", cli).method is None  # "" -> no method: the key was stripped
    assert auth_status("claude", cli, allow_api_keys=True).method == "api_key"


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_auth_status_with_the_installed_cli(name: str) -> None:
    """The real `claude auth status` / `codex login status` (no model call): an answer or a
    reason, never an exception."""
    if shutil.which(name) is None:
        pytest.skip(f"{name} is not installed")
    status = auth_status(name, runner_default(name))
    assert isinstance(status, AuthStatus)
    assert status.logged_in is not None or status.detail, status
    if status.logged_in:
        assert status.method
