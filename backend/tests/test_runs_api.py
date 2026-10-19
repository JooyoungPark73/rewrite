"""The pass-runner API end to end, with fake `claude` / `codex` executables that print the
event streams the real CLIs print (shapes from tests/fixtures/runner/)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db, runstore
from app.config import load_config, save_config
from app.findings import findings_schema
from app.repo import Repo
from app.runs import EventThrottle
from tests.conftest import FIXTURES, adopt_intro, fake_exe, make_client

RUNNER_FIXTURES = FIXTURES / "runner"

FAKE_CLI = """#!{python}
import json, os, pathlib, sys, time
here = pathlib.Path(__file__).resolve().parent
name = pathlib.Path(__file__).name
if "--help" in sys.argv[1:]:
    sys.stdout.write((here / (name + ".help")).read_text())
    sys.exit(0)
if sys.argv[1:3] == ["auth", "status"]:  # claude: JSON on stdout
    print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}}))
    sys.exit(0)
if sys.argv[1:3] == ["login", "status"]:  # codex: one line on stderr
    print("Logged in using ChatGPT", file=sys.stderr)
    sys.exit(0)
prompt = sys.stdin.read()
behavior = json.loads((here / (name + ".json")).read_text())
schema = None
if "--output-schema" in sys.argv:
    schema = pathlib.Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text()
calls = here / "calls"
calls.mkdir(exist_ok=True)
record = {{"argv": sys.argv, "cwd": os.getcwd(), "prompt": prompt, "pid": os.getpid(),
          "claudecode": os.environ.get("CLAUDECODE"), "schema": schema,
          "api_keys": {{k: v for k, v in os.environ.items()
                       if k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")}}}}
tmp = calls / ".tmp-{{}}".format(os.getpid())
tmp.write_text(json.dumps(record))
tmp.rename(calls / "{{}}-{{}}.json".format(name, time.time_ns()))
gate = behavior.get("wait_for")
while gate and not (here / gate).exists():
    time.sleep(0.02)
time.sleep(behavior.get("sleep", 0))
for line in behavior.get("lines", []):
    print(line, flush=True)
sys.stderr.write(behavior.get("stderr", ""))
sys.exit(behavior.get("exit", 0))
"""

# sections/intro.tex after adopt_intro: paragraph 2 starts on line 13.
GOOD = {
    "file": "sections/intro.tex",
    "line_start": 2,  # wrong on purpose: the quote anchors it
    "line_end": 2,
    "quote": "Providers mitigate cold starts with warm pools",
    "level": "paragraph",
    "category": "topic-sentence",
    "severity": "major",
    "problem": "The paragraph opens with the mitigation instead of its point.",
    "why": "The reader cannot tell what the paragraph argues until its end.",
    "json_ref": "p-2",
}
PARAPHRASED = {**GOOD, "quote": "a quote that is not in the file", "line_start": 19, "line_end": 20}
WITH_REPLACEMENT = {**GOOD, "replacement": "Warm pools waste memory."}
PRAISE = {**GOOD, "problem": "Great motivation, nothing to fix."}
REWRITE = {
    **GOOD,
    "problem": 'The opening is weak; it should read "Warm pools trade memory for latency and '
    'lose that trade for rarely invoked functions."',
}
PAYLOAD = {"findings": [GOOD, WITH_REPLACEMENT, PRAISE, PARAPHRASED, REWRITE]}


def claude_lines(
    payload: Any = PAYLOAD,
    *,
    text: str | None = None,
    structured: bool = True,
    tools: list[str] | None = None,
    cost: float = 0.0123,
) -> list[str]:
    answer = text if text is not None else json.dumps(payload)
    events = [
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-sonnet-5-5",
            "tools": tools if tools is not None else (["StructuredOutput"] if structured else []),
            "claude_code_version": "2.1.302",
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": answer,
            "structured_output": payload if structured else None,
            "total_cost_usd": cost,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": 50,
            },
        },
    ]
    return [json.dumps(event) for event in events]


def codex_lines(payload: Any = PAYLOAD) -> list[str]:
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "hm"}},
        {
            "type": "item.completed",
            "item": {"id": "i1", "type": "agent_message", "text": json.dumps(payload)},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 2000, "output_tokens": 80}},
    ]
    return [json.dumps(event) for event in events]


class Fakes:
    """Fake CLIs in a tmp bin dir, wired into the repo's config.toml."""

    def __init__(self, root: Path, repo: Path) -> None:
        self.bin = root / "fakebin"
        self.repo = repo
        self.bin.mkdir()
        for name, help_file in (("claude", "claude-help.txt"), ("codex", "codex-exec-help.txt")):
            exe = fake_exe(self.bin, f"fake-{name}", "")
            exe.write_text(FAKE_CLI.format(python=sys.executable))
            shutil.copyfile(RUNNER_FIXTURES / help_file, self.bin / f"fake-{name}.help")
        self.set("claude", lines=claude_lines())
        self.set("codex", lines=codex_lines())
        self.configure()

    def configure(self, **runner: Any) -> None:
        path = self.repo / ".rewrite" / "config.toml"
        config = load_config(path)
        config.runner.claude.command = [
            str(self.bin / "fake-claude"),
            *config.runner.claude.command[1:],
        ]
        config.runner.codex.command = [
            str(self.bin / "fake-codex"),
            *config.runner.codex.command[1:],
        ]
        for key, value in runner.items():
            setattr(config.runner, key, value)
        save_config(path, config)

    def set(self, name: str, **behavior: Any) -> None:
        (self.bin / f"fake-{name}.json").write_text(json.dumps(behavior))

    def calls(self, name: str | None = None) -> list[dict[str, Any]]:
        directory = self.bin / "calls"
        if not directory.is_dir():
            return []
        paths = sorted(directory.glob("fake-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
        return [
            json.loads(p.read_text())
            for p in paths
            if name is None or p.name.startswith(f"fake-{name}-")
        ]

    def release(self, gate: str = "go") -> None:
        (self.bin / gate).write_text("")


@pytest.fixture
def env(
    client: TestClient, initialized_repo: Path, tmp_path: Path
) -> Iterator[tuple[TestClient, Fakes, str, list[str]]]:
    fakes = Fakes(tmp_path, initialized_repo)
    section_id, paragraph_ids = adopt_intro(client)
    assert paragraph_ids[1] == "p-2"
    yield client, fakes, section_id, paragraph_ids
    fakes.release()  # never leave a gated fake waiting


def wait_run(
    client: TestClient,
    run_id: str,
    until: Callable[[dict[str, Any]], bool] = lambda r: (
        r["status"] in ("done", "failed", "cancelled", "cached")
    ),
    timeout: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        detail = client.get(f"/api/runs/{run_id}").json()
        if until(detail["run"]):
            return detail
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} stuck: {detail['run']}")
        time.sleep(0.05)


def wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.05)


def start(client: TestClient, section_id: str, **extra: Any) -> dict[str, Any]:
    body = {"pass": "paragraph-function", "scope": {"kind": "section", "ref": section_id}}
    body.update(extra)
    response = client.post("/api/runs", json=body)
    assert response.status_code == 200, response.text
    return response.json()["run"]


# --------------------------------------------------------------------------- passes, runners


def test_passes_endpoint(client: TestClient, initialized_repo: Path) -> None:
    (initialized_repo / ".rewrite/prompts/passes/broken.md").write_text("no front matter\n")
    passes = client.get("/api/passes").json()["passes"]
    by_name = {p["name"]: p for p in passes}
    assert set(by_name) >= {"paragraph-function", "message-drift", "ab-compare", "broken"}
    pf = by_name["paragraph-function"]
    assert set(pf) >= {
        "name",
        "level",
        "scope",
        "allow_rewrites",
        "model",
        "thinking",
        "file",
        "description",
    }
    assert pf["level"] == "paragraph" and pf["scope"] == "section" and pf["runnable"]
    assert pf["file"] == ".rewrite/prompts/passes/paragraph-function.md"
    assert by_name["latex-hygiene"]["allow_rewrites"] is True
    assert by_name["broken"]["error"] and by_name["broken"]["runnable"] is False
    assert by_name["ab-compare"]["runnable"] is False


def test_runners_endpoint(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, _, _ = env
    data = client.get("/api/runners").json()
    runners = {r["name"]: r for r in data["runners"]}
    assert runners["claude"]["available"] and runners["codex"]["available"]
    assert runners["claude"]["models"] == ["opus", "sonnet", "haiku"]
    assert runners["claude"]["suppress_global"] is True
    assert runners["codex"]["suppress_global"] is False
    assert runners["claude"]["flags"] == {
        "model": True,
        "thinking": True,
        "schema": True,
        "isolation": True,
        "stream": True,
    }
    assert data["thinking_levels"] == ["low", "medium", "high"]
    assert data["defaults"]["message"] == {"runner": "claude", "model": "opus", "thinking": "high"}
    assert data["defaults"]["sentence"] == {
        "runner": "claude",
        "model": "sonnet",
        "thinking": "low",
    }
    assert {g["runner"] for g in data["global_instructions"]} == {"claude", "codex"}
    for entry in data["global_instructions"]:
        assert entry["present"] == Path(entry["path"]).is_file()
    # The fakes answer `claude auth status` / `codex login status` like a subscription login.
    assert runners["claude"]["auth"] == {
        "logged_in": True,
        "method": "claude.ai",
        "detail": "max subscription",
    }
    assert runners["codex"]["auth"] == {"logged_in": True, "method": "ChatGPT", "detail": None}
    assert runners["claude"]["warning"] is None and runners["codex"]["warning"] is None
    assert fakes.calls() == []  # login checks are not runs
    fakes.configure()
    config_path = fakes.repo / ".rewrite/config.toml"
    config = load_config(config_path)
    config.runner.codex.command = [str(fakes.bin / "missing-codex"), "exec"]
    save_config(config_path, config)
    codex = next(r for r in client.get("/api/runners").json()["runners"] if r["name"] == "codex")
    assert codex["available"] is False and "not found" in codex["error"]
    assert codex["auth"] is None  # no login check for a CLI that is not there


def test_runners_report_login_state(
    env: tuple[TestClient, Fakes, str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/runners carries each CLI's login (`auth`). Not logged in makes the runner
    unavailable with an error naming the login command (and is not cached, so logging in
    brings it back at once); an API-key login only adds a warning. The check gets the
    config's allow_api_keys and is cached per runner for 60 s."""
    from app.runner import auth as auth_module
    from app.runner.auth import AuthStatus

    client, fakes, _, _ = env
    answers = {
        "claude": AuthStatus(logged_in=False, detail="not logged in"),
        "codex": AuthStatus(logged_in=True, method="API key"),
    }
    calls: list[tuple[str, bool]] = []

    def fake_status(runner: str, cli: Any, *, allow_api_keys: bool = False, **_: Any) -> Any:
        calls.append((runner, allow_api_keys))
        return answers[runner]

    monkeypatch.setattr(auth_module, "auth_status", fake_status)

    def runners() -> dict[str, dict[str, Any]]:
        response = client.get("/api/runners")
        assert response.status_code == 200, response.text
        return {r["name"]: r for r in response.json()["runners"]}

    data = runners()
    claude, codex = data["claude"], data["codex"]
    assert claude["available"] is False
    assert claude["error"] == "not logged in: run `claude auth login`"
    assert claude["auth"] == {"logged_in": False, "method": None, "detail": "not logged in"}
    assert claude["warning"] is None
    assert codex["available"] is True and codex["error"] is None
    assert codex["auth"]["method"] == "API key"
    assert codex["warning"] == "logged in with an API key; this app expects a subscription login"
    assert sorted(calls) == [("claude", False), ("codex", False)]

    # The user logs in: the next request asks claude again (a "not logged in" answer is not
    # cached), while codex's answer comes from the cache.
    answers["claude"] = AuthStatus(logged_in=True, method="claude.ai")
    data = runners()
    assert data["claude"]["available"] is True and data["claude"]["error"] is None
    assert data["claude"]["auth"]["method"] == "claude.ai" and data["claude"]["warning"] is None
    assert sorted(calls) == [("claude", False), ("claude", False), ("codex", False)]
    runners()
    assert len(calls) == 3  # both answers cached now

    # allow_api_keys is part of the check (the CLI sees the environment a run would get).
    fakes.configure(allow_api_keys=True)
    runners()
    assert sorted(calls[3:]) == [("claude", True), ("codex", True)]


def test_api_keys_are_kept_out_of_runs_unless_allowed(
    env: tuple[TestClient, Fakes, str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLIs run on the subscription login: API-key variables in the server's environment
    never reach a run unless `[runner] allow_api_keys = true`."""
    client, fakes, section_id, _ = env
    keys = {"ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-x", "CODEX_API_KEY": "sk-c"}
    for key, value in keys.items():
        monkeypatch.setenv(key, value)
    for runner in ("claude", "codex"):
        run = wait_run(client, start(client, section_id, runner=runner)["id"])["run"]
        assert run["status"] == "done", run["error"]
        assert fakes.calls(runner)[-1]["api_keys"] == {}
    fakes.configure(allow_api_keys=True)
    for runner in ("claude", "codex"):
        run = wait_run(client, start(client, section_id, runner=runner, force=True)["id"])["run"]
        assert run["status"] == "done", run["error"]
        assert fakes.calls(runner)[-1]["api_keys"] == keys


# --------------------------------------------------------------------------- live runs


def test_claude_run_end_to_end(
    env: tuple[TestClient, Fakes, str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fakes, section_id, _ = env
    monkeypatch.setenv("CLAUDECODE", "1")  # as if the server ran inside Claude Code
    preview = client.post(
        "/api/runs/preview",
        json={
            "pass": "paragraph-function",
            "scope": {"kind": "section", "ref": section_id},
            "runner": "claude",
            "model": "sonnet",
            "thinking": "low",
        },
    ).json()
    assert preview["cache_hit"] is False and preview["model"] == "sonnet"
    assert preview["scope_name"] == f'section "Introduction" ({section_id})'
    with client.websocket_connect("ws://127.0.0.1/ws/events") as ws:
        time.sleep(0.1)
        run = start(client, section_id, runner="claude", model="sonnet", thinking="low")
        assert run["status"] in ("queued", "running") and run["pass"] == "paragraph-function"
        events = []
        for _ in range(200):
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "findings.updated":
                break
    types = [e["type"] for e in events]
    assert (
        types.index("run.started") < types.index("run.finished") < types.index("findings.updated")
    )
    assert any(e["type"] == "run.event" and e["kind"] == "text" for e in events)
    finished = next(e for e in events if e["type"] == "run.finished")["run"]
    assert finished["id"] == run["id"] and finished["status"] == "done"

    detail = wait_run(client, run["id"])
    run = detail["run"]
    assert run["status"] == "done" and run["error"] is None
    assert (run["findings_count"], run["rejected_count"]) == (2, 3)
    assert run["cost_usd"] == pytest.approx(0.0123)
    assert (run["input_tokens"], run["output_tokens"]) == (1110, 50)
    assert run["model"] == "sonnet" and run["model_used"] == "claude-sonnet-5-5"
    assert run["git_sha"] is None  # the tmp repo is not a git repo
    assert run["duration_ms"] is not None and run["started_at"] <= run["finished_at"]
    assert run["scope"]["kind"] == "section" and run["scope"]["ref"] == section_id

    good, unverified = detail["findings"]
    assert good["id"] == f"{run['id']}-0" and good["state"] == "open"
    assert (good["anchor_state"], good["anchor_line_start"], good["anchor_line_end"]) == (
        "ok",
        13,
        13,
    )
    assert (good["line_start"], good["line_end"]) == (2, 2)  # as the model said
    assert good["json_ref"] == "p-2" and good["severity"] == "major"
    assert unverified["anchor_state"] == "unverified"
    assert (unverified["anchor_line_start"], unverified["anchor_line_end"]) == (19, 20)
    assert [(r["index"], r["reason"]) for r in detail["rejected"]] == [
        (1, "unknown_field:replacement"),
        (2, "praise"),
        (4, "rewrite_text"),
    ]
    assert detail["rejected"][0]["raw"] == WITH_REPLACEMENT
    assert json.loads(detail["raw_output"]) == PAYLOAD

    (call,) = fakes.calls("claude")
    stored = client.get(f"/api/runs/{run['id']}/prompt").json()
    assert stored["prompt"] == preview["prompt"] == call["prompt"]  # exactly what was sent
    assert stored["command"] == preview["command"] == call["argv"][: len(stored["command"])]
    assert Path(call["cwd"]).resolve() == (fakes.repo / ".rewrite/sandbox").resolve()
    assert call["claudecode"] is None
    assert "--json-schema" in call["argv"] and "--effort" in call["argv"]

    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["id"] == run["id"]


def test_codex_run(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, _ = env
    run = start(client, section_id, runner="codex", thinking="low")
    assert run["model"] is None  # a claude level default does not apply to codex
    detail = wait_run(client, run["id"])
    run = detail["run"]
    assert run["status"] == "done" and run["findings_count"] == 2
    assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (2000, 80, None)
    assert run["model_used"] is None
    (call,) = fakes.calls("codex")
    assert json.loads(call["schema"]) == findings_schema()  # --output-schema file
    argv = call["argv"]
    assert argv[1:3] == ["exec", "--json"] and "model_reasoning_effort=low" in argv
    assert ["--disable", "shell_tool"] == argv[
        argv.index("--disable") : argv.index("--disable") + 2
    ]
    command = client.get(f"/api/runs/{run['id']}/prompt").json()["command"]
    assert command[command.index("--output-schema") + 1].endswith("findings.schema.json")


def test_level_defaults_and_overrides(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, _ = env
    run = start(client, section_id)  # paragraph level: claude / sonnet / medium
    assert (run["runner"], run["model"], run["thinking"]) == ("claude", "sonnet", "medium")
    wait_run(client, run["id"])
    run = start(client, section_id, model="", thinking="high")
    assert (run["model"], run["thinking"]) == (None, "high")
    wait_run(client, run["id"])
    argv = fakes.calls("claude")[-1]["argv"]
    assert "--model" not in argv and argv[argv.index("--effort") + 1] == "high"


# --------------------------------------------------------------------------- cache


def test_cache_hit_copies_findings(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, _ = env
    first = wait_run(client, start(client, section_id, model="sonnet", thinking="low")["id"])
    preview = client.post(
        "/api/runs/preview",
        json={
            "pass": "paragraph-function",
            "scope": {"kind": "section", "ref": section_id},
            "model": "sonnet",
            "thinking": "low",
        },
    ).json()
    assert preview["cache_hit"] is True
    cached = start(client, section_id, model="sonnet", thinking="low")
    assert cached["status"] == "cached" and cached["cached_from"] == first["run"]["id"]
    assert (cached["findings_count"], cached["rejected_count"]) == (2, 3)
    assert cached["cost_usd"] == 0.0 and cached["input_tokens"] is None
    assert len(fakes.calls()) == 1  # no process for a cache hit
    detail = client.get(f"/api/runs/{cached['id']}").json()
    copied = detail["findings"]
    assert [f["id"] for f in copied] == [f"{cached['id']}-0", f"{cached['id']}-1"]
    assert copied[0]["anchor_state"] == "ok" and copied[0]["anchor_line_start"] == 13
    original_id = first["findings"][0]["id"]
    client.patch(f"/api/findings/{original_id}", json={"state": "dismissed"})
    assert client.get(f"/api/runs/{cached['id']}").json()["findings"][0]["state"] == "open"
    assert client.get(f"/api/runs/{cached['id']}/prompt").json()["prompt"] == preview["prompt"]

    forced = start(client, section_id, model="sonnet", thinking="low", force=True)
    assert forced["status"] in ("queued", "running")
    assert wait_run(client, forced["id"])["run"]["status"] == "done"
    assert len(fakes.calls()) == 2
    other = start(client, section_id, model="sonnet", thinking="high")
    assert other["status"] != "cached"
    wait_run(client, other["id"])


def test_cache_misses_after_an_edit(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, _ = env
    wait_run(client, start(client, section_id)["id"])
    content = client.get("/api/file", params={"path": "sections/intro.tex"}).json()["content"]
    client.put(
        "/api/file",
        json={"path": "sections/intro.tex", "content": content.replace("tenants", "customers")},
    )
    again = start(client, section_id)
    assert again["status"] != "cached"
    wait_run(client, again["id"])
    assert len(fakes.calls()) == 2


# --------------------------------------------------------------------------- locking and limits


def test_one_active_run_per_scope(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, _ = env
    fakes.set("claude", lines=claude_lines(), wait_for="go")
    run = start(client, section_id)
    second = client.post(
        "/api/runs",
        json={"pass": "paragraph-function", "scope": {"kind": "section", "ref": section_id}},
    )
    assert second.status_code == 409 and run["id"] in second.json()["detail"]
    other_pass = client.post(
        "/api/runs", json={"pass": "clarity", "scope": {"kind": "section", "ref": section_id}}
    )
    assert other_pass.status_code == 200
    fakes.release()
    wait_run(client, run["id"])
    wait_run(client, other_pass.json()["run"]["id"])
    third = start(client, section_id, force=True)  # the scope is free again
    wait_run(client, third["id"])


def test_concurrency_cap(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, _, _ = env
    fakes.set("claude", lines=claude_lines({"findings": []}), wait_for="go")
    files = ["main.tex", "sections/intro.tex", "sections/method.tex", "sections/results.tex"]
    runs = [
        client.post(
            "/api/runs", json={"pass": "clarity", "scope": {"kind": "file", "file": f}}
        ).json()["run"]
        for f in files
    ]
    wait_for(lambda: len(fakes.calls()) == 3)
    time.sleep(0.5)
    assert len(fakes.calls()) == 3  # the 4th waits for a slot
    statuses = sorted(client.get(f"/api/runs/{r['id']}").json()["run"]["status"] for r in runs)
    assert statuses == ["queued", "running", "running", "running"]
    fakes.release()
    for run in runs:
        assert wait_run(client, run["id"])["run"]["status"] == "done"
    assert len(fakes.calls()) == 4


SLEEPER = """\
case "$*" in *--help*) cat "$(dirname "$0")/fake-claude.help"; exit 0;; esac
echo $$ > "$(dirname "$0")/sleeper.pid"
sleep 30 &
echo $! > "$(dirname "$0")/sleeper.child"
wait
"""


def test_cancel_kills_the_process_group(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, _ = env
    sleeper = fake_exe(fakes.bin, "sleeper", SLEEPER)
    path = fakes.repo / ".rewrite/config.toml"
    config = load_config(path)
    config.runner.claude.command = [str(sleeper), *config.runner.claude.command[1:]]
    save_config(path, config)
    run = start(client, section_id)
    pid_file, child_file = fakes.bin / "sleeper.pid", fakes.bin / "sleeper.child"
    wait_for(lambda: pid_file.is_file() and child_file.is_file() and bool(child_file.read_text()))
    pgid, child = int(pid_file.read_text()), int(child_file.read_text())
    assert os.getpgid(child) == pgid
    response = client.post(f"/api/runs/{run['id']}/cancel")
    assert response.status_code == 200
    cancelled = response.json()["run"]
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"] is not None

    def group_gone() -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(group_gone, timeout=8)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    again = client.post(f"/api/runs/{run['id']}/cancel")  # already over: returns the run
    assert again.status_code == 200 and again.json()["run"]["status"] == "cancelled"


def test_cancel_a_queued_run(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, _ = env
    fakes.configure(concurrency=1)
    fakes.set("claude", lines=claude_lines(), wait_for="go")
    first = start(client, section_id)
    wait_for(lambda: len(fakes.calls()) == 1)
    queued = client.post(
        "/api/runs", json={"pass": "clarity", "scope": {"kind": "file", "file": "main.tex"}}
    ).json()["run"]
    assert client.get(f"/api/runs/{queued['id']}").json()["run"]["status"] == "queued"
    response = client.post(f"/api/runs/{queued['id']}/cancel")
    assert response.json()["run"]["status"] == "cancelled"
    fakes.release()
    assert wait_run(client, first["id"])["run"]["status"] == "done"
    assert len(fakes.calls()) == 1  # the cancelled run never started a process


# --------------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    ("behavior", "fragment"),
    [
        ({"lines": [], "stderr": "error: unknown option '--foo'\n", "exit": 1}, "unknown option"),
        (
            {"lines": claude_lines(text="Overall this is a fine paper.", structured=False)},
            "could not parse findings",
        ),
        ({"lines": claude_lines(tools=["Bash", "Write"]), "sleep": 5}, "tools enabled"),
    ],
)
def test_failed_runs(
    env: tuple[TestClient, Fakes, str, list[str]], behavior: dict[str, Any], fragment: str
) -> None:
    client, fakes, section_id, _ = env
    fakes.set("claude", **behavior)
    run = start(client, section_id)
    detail = wait_run(client, run["id"])
    assert detail["run"]["status"] == "failed"
    assert fragment in detail["run"]["error"]
    assert detail["findings"] == []


def test_request_errors(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, paragraph_ids = env
    planned = client.post(
        "/api/spec/ops",
        json={"op": {"type": "insert", "parent_id": "message", "index": 1, "level": "section"}},
    ).json()["new_id"]
    scope = {"kind": "section", "ref": section_id}
    bad = [
        ({"pass": "nope", "scope": scope}, "unknown pass"),
        ({"pass": "ab-compare", "scope": scope}, "cannot run"),
        ({"pass": "paragraph-function", "scope": {"kind": "section", "ref": planned}}, "planned"),
        (
            {"pass": "paragraph-function", "scope": {"kind": "section", "ref": paragraph_ids[0]}},
            "paragraph",
        ),
        ({"pass": "paragraph-function", "scope": scope, "runner": "gpt"}, "unknown runner"),
        ({"pass": "paragraph-function", "scope": scope, "thinking": "max"}, "thinking"),
        ({"pass": "paragraph-function"}, "invalid request"),
        ({"pass": "paragraph-function", "scope": {"kind": "chapter"}}, "invalid request"),
        ({"pass": "paragraph-function", "scope": scope, "extra": 1}, "invalid request"),
        ({"pass": "clarity", "scope": {"kind": "file", "file": "paper.json"}}, "not a source file"),
    ]
    for body, fragment in bad:
        for endpoint in ("/api/runs", "/api/runs/preview"):
            response = client.post(endpoint, json=body)
            assert response.status_code == 400, (endpoint, body, response.text)
            assert fragment in response.json()["detail"], (body, response.json())
    path = fakes.repo / ".rewrite/config.toml"
    config = load_config(path)
    config.runner.claude.command = [str(fakes.bin / "not-installed"), "-p"]
    save_config(path, config)
    response = client.post("/api/runs", json={"pass": "paragraph-function", "scope": scope})
    assert response.status_code == 400 and "not available" in response.json()["detail"]
    preview = client.post("/api/runs/preview", json={"pass": "paragraph-function", "scope": scope})
    assert preview.status_code == 200  # the prompt can still be previewed and copied
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/prompt").status_code == 404
    assert client.post("/api/runs/nope/cancel").status_code == 404
    foreign = client.post(
        "/api/runs",
        json={"pass": "clarity", "scope": scope},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert fakes.calls() == []


# --------------------------------------------------------------------------- paste-back


def test_paste_back(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, fakes, section_id, _ = env
    scope = {"kind": "section", "ref": section_id}
    raw = "Here are the findings you asked for:\n```json\n" + json.dumps(PAYLOAD) + "\n```\nCheers!"
    response = client.post(
        "/api/runs/paste", json={"pass": "paragraph-function", "scope": scope, "raw": raw}
    )
    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run["runner"] == "paste" and run["status"] == "done"
    assert (run["findings_count"], run["rejected_count"]) == (2, 3)
    assert run["model"] is None and run["cost_usd"] is None
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["raw_output"] == raw
    assert detail["findings"][0]["anchor_state"] == "ok"
    prompt = client.get(f"/api/runs/{run['id']}/prompt").json()
    preview = client.post(
        "/api/runs/preview", json={"pass": "paragraph-function", "scope": scope}
    ).json()
    assert prompt["prompt"] == preview["prompt"] and prompt["command"] == []
    garbage = client.post(
        "/api/runs/paste",
        json={"pass": "paragraph-function", "scope": scope, "raw": "looks good to me"},
    ).json()["run"]
    assert garbage["status"] == "failed" and "could not parse" in garbage["error"]
    assert (
        client.post("/api/runs/paste", json={"pass": "x", "scope": scope, "raw": "{}"}).status_code
        == 400
    )
    assert fakes.calls() == []
    # a paste never seeds the cache for live runs
    assert start(client, section_id)["status"] != "cached"


# --------------------------------------------------------------------------- findings


def test_finding_states_and_filters(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, _, section_id, _ = env
    detail = wait_run(client, start(client, section_id)["id"])
    finding_id = detail["findings"][0]["id"]
    for state in ("addressed", "dismissed", "open"):
        response = client.patch(f"/api/findings/{finding_id}", json={"state": state})
        assert response.status_code == 200 and response.json()["finding"]["state"] == state
    assert client.patch(f"/api/findings/{finding_id}", json={"state": "done"}).status_code == 400
    assert (
        client.patch(f"/api/findings/{finding_id}", json={"state": "open", "x": 1}).status_code
        == 400
    )
    assert client.patch("/api/findings/nope", json={"state": "open"}).status_code == 404
    client.patch(f"/api/findings/{finding_id}", json={"state": "dismissed"})
    everything = client.get("/api/findings").json()["findings"]
    assert len(everything) == 2
    in_file = client.get(
        "/api/findings", params={"file": "sections/intro.tex", "state": "open"}
    ).json()
    assert [f["id"] for f in in_file["findings"]] == [detail["findings"][1]["id"]]
    by_run = client.get("/api/findings", params={"run_id": detail["run"]["id"]}).json()["findings"]
    assert len(by_run) == 2
    assert client.get("/api/findings", params={"file": "main.tex"}).json()["findings"] == []


def test_findings_reanchor_on_save(env: tuple[TestClient, Fakes, str, list[str]]) -> None:
    client, _, section_id, _ = env
    detail = wait_run(client, start(client, section_id)["id"])
    good, unverified = detail["findings"]
    client.patch(f"/api/findings/{unverified['id']}", json={"state": "dismissed"})
    content = client.get("/api/file", params={"path": "sections/intro.tex"}).json()["content"]
    with client.websocket_connect("ws://127.0.0.1/ws/events") as ws:
        time.sleep(0.1)
        client.put(
            "/api/file", json={"path": "sections/intro.tex", "content": "% new\n\n" + content}
        )
        event = ws.receive_json()
        while event["type"] != "findings.updated":
            event = ws.receive_json()
        assert event == {"type": "findings.updated", "run_id": None, "file": "sections/intro.tex"}
    moved = client.get(f"/api/runs/{detail['run']['id']}").json()["findings"]
    assert (moved[0]["anchor_state"], moved[0]["anchor_line_start"]) == ("moved", 15)
    assert moved[1]["anchor_state"] == "unverified" and moved[1]["anchor_line_start"] == 19
    edited = content.replace("Providers mitigate cold starts with warm pools", "Warm pools exist")
    client.put("/api/file", json={"path": "sections/intro.tex", "content": "% new\n\n" + edited})
    stale = client.get(f"/api/runs/{detail['run']['id']}").json()["findings"][0]
    assert (stale["anchor_state"], stale["anchor_line_start"]) == ("stale", 15)
    client.put("/api/file", json={"path": "sections/intro.tex", "content": content})
    back = client.get(f"/api/runs/{detail['run']['id']}").json()["findings"][0]
    assert (back["anchor_state"], back["anchor_line_start"]) == ("ok", 13)  # back home


# --------------------------------------------------------------------------- state


def test_interrupted_runs_fail_on_startup(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    with db.connect(repo) as conn:
        runstore.insert_run(
            conn,
            {
                "id": "r1",
                "pass": "clarity",
                "level": "sentence",
                "scope_kind": "paper",
                "scope_json": '{"kind": "paper"}',
                "scope_name": "the whole paper",
                "scope_key": "clarity:paper:",
                "runner": "claude",
                "prompt": "p",
                "status": "running",
                "created_at": time.time(),
            },
        )
    with make_client(initialized_repo) as client:
        run = client.get("/api/runs/r1").json()["run"]
    assert run["status"] == "failed" and "interrupted" in run["error"]


def test_runs_without_state_db(client: TestClient, initialized_repo: Path) -> None:
    assert client.get("/api/runs").json() == {"runs": []}
    assert client.get("/api/findings").json() == {"findings": []}
    assert not (initialized_repo / ".rewrite/state.db").exists()


async def test_event_throttle_merges_to_at_most_ten_per_second() -> None:
    import asyncio

    from app.events import EventBus
    from app.runner import RunEvent

    bus = EventBus()
    queue = bus.subscribe()
    throttle = EventThrottle(bus, "r", interval=0.1)
    started = time.monotonic()
    for index in range(200):
        throttle.push(RunEvent(kind="text", text=str(index % 10)))
        if index % 20 == 0:
            await asyncio.sleep(0.01)
    throttle.push(RunEvent(kind="status", text="done"))
    await asyncio.sleep(0.25)
    throttle.close()
    published = []
    while not queue.empty():
        published.append(queue.get_nowait())
    elapsed = time.monotonic() - started
    assert len(published) <= 10 * elapsed + 2
    text = "".join(e["text"] for e in published if e["kind"] == "text")
    assert text == "".join(str(i % 10) for i in range(200))
    assert published[-1]["kind"] == "status" and published[-1]["text"] == "done"


def test_context_leaks_in_runners_and_on_runs(
    env: tuple[TestClient, Fakes, str, list[str]],
) -> None:
    """Audit 2026-10-18 item 7: GET /api/runners lists instruction files outside the sandbox
    that a CLI could pick up, and each run records a warning when its argv does not keep
    them out."""
    client, fakes, section_id, _ = env
    paper = fakes.repo.resolve()
    (paper / "AGENTS.md").write_text("Paper history: reviewers said ...\n")
    (paper / "CLAUDE.md").write_text("Paper notes\n")
    leaks = {
        (leak["runner"], leak["path"]): leak["suppressed"]
        for leak in client.get("/api/runners").json()["context_leaks"]
    }
    assert leaks[("codex", str(paper / "AGENTS.md"))] is True
    assert leaks[("claude", str(paper / "CLAUDE.md"))] is True
    run = wait_run(client, start(client, section_id, runner="codex")["id"])["run"]
    assert run["status"] == "done" and run["context_warning"] is None

    # A hand-edited codex command that sets the discovery options back to codex's defaults.
    config_path = fakes.repo / ".rewrite" / "config.toml"
    config = load_config(config_path)
    config.runner.codex.command = [
        str(fakes.bin / "fake-codex"),
        "exec",
        "--json",
        "-c",
        "project_doc_max_bytes=32768",
        "-c",
        'project_root_markers=[".git"]',
    ]
    save_config(config_path, config)
    leaks = {
        (leak["runner"], leak["path"]): leak["suppressed"]
        for leak in client.get("/api/runners").json()["context_leaks"]
    }
    assert leaks[("codex", str(paper / "AGENTS.md"))] is False
    run = wait_run(client, start(client, section_id, runner="codex", force=True)["id"])["run"]
    assert run["status"] == "done"
    assert run["context_warning"] and str(paper / "AGENTS.md") in run["context_warning"]
    listed = next(r for r in client.get("/api/runs").json()["runs"] if r["id"] == run["id"])
    assert listed["context_warning"] == run["context_warning"]
