"""Run groups end to end with fake `claude` / `codex` executables (app/groups.py,
app/api/groups.py): "run both" with the merged list, partial and cancelled groups, cache
hits, the fan-out member set with the `ends` scope, and the concurrency cap of 3."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import load_config, save_config
from app.groups import derive_status
from app.prompts import FANOUT_NOTE
from tests.conftest import adopt_intro, fake_exe
from tests.test_runs_api import GOOD, Fakes, claude_lines, codex_lines, wait_for

INTRO = "sections/intro.tex"
# sections/intro.tex after adopt_intro: paragraph 2 text on 13-16, paragraph 3 on 19-23,
# paragraph 4 on 26-29.
CLAUDE_ONLY = {
    **GOOD,
    "line_start": 26,
    "line_end": 26,
    "quote": "This paper makes two observations.",
    "category": "multiple-jobs",
    "json_ref": "p-4",
}
CODEX_AGREES = {
    **GOOD,
    "line_start": 13,
    "line_end": 13,
    "quote": "Providers mitigate cold starts with warm pools that keep recently used instances",
    "severity": "critical",
}
CODEX_ONLY = {
    **GOOD,
    "line_start": 19,
    "line_end": 19,
    "quote": "Snapshot restore is the main alternative.",
    "category": "off-message",
    "json_ref": "p-3",
}
CLAUDE_PAYLOAD = {"findings": [GOOD, CLAUDE_ONLY]}
CODEX_PAYLOAD = {"findings": [CODEX_AGREES, CODEX_ONLY]}
FINAL = ("done", "partial", "failed", "cancelled")


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def drain(client: TestClient) -> list[dict[str, Any]]:
    """Events published since the first call (a bus subscription kept on the app state)."""
    state = client.app.state.pb  # type: ignore[attr-defined]
    queue = getattr(state, "_group_test_queue", None)
    if queue is None:
        queue = state._group_test_queue = state.bus.subscribe()
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def wait_group(client: TestClient, group_id: str, timeout: float = 20.0) -> dict[str, Any]:
    """The group detail once its status is final and no member is active."""
    deadline = time.monotonic() + timeout
    while True:
        detail = ok(client.get(f"/api/groups/{group_id}"))
        settled = all(r["status"] not in ("queued", "running") for r in detail["runs"])
        if detail["group"]["status"] in FINAL and settled:
            return detail
        if time.monotonic() > deadline:
            raise AssertionError(f"group stuck: {detail['group']}")
        time.sleep(0.05)


def adopt_sections(client: TestClient) -> list[str]:
    """Adopt every `\\section` of the sample paper (document order); returns the ids."""
    ids = []
    for section in client.get("/api/spec/skeleton").json()["sections"]:
        if section["adopted_id"] is None:
            body = {"kind": "section", "file": section["file"], "line": section["line"]}
            ids.append(ok(client.post("/api/spec/adopt", json=body))["new_id"])
    return ids


@pytest.fixture
def env(
    client: TestClient, initialized_repo: Path, tmp_path: Path
) -> Iterator[tuple[TestClient, Fakes, str]]:
    fakes = Fakes(tmp_path, initialized_repo)
    fakes.set("claude", lines=claude_lines(CLAUDE_PAYLOAD))
    fakes.set("codex", lines=codex_lines(CODEX_PAYLOAD))
    section_id, _ = adopt_intro(client)
    yield client, fakes, section_id
    fakes.release()


def both_body(section_id: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "pass": "paragraph-function",
        "scope": {"kind": "section", "ref": section_id},
        "claude": {"model": "sonnet", "thinking": "low"},
        "codex": {"thinking": "low"},
    }
    body.update(extra)
    return body


# --------------------------------------------------------------------------- status rule


def test_derive_status() -> None:
    assert derive_status("queued", ["queued", "queued"]) == "queued"
    assert derive_status("queued", ["running", "queued"]) == "running"
    assert derive_status("queued", ["cached", "queued"]) == "running"
    assert derive_status("running", ["done", "cached"]) == "done"
    assert derive_status("running", ["done", "failed"]) == "partial"
    assert derive_status("running", ["cancelled", "done"]) == "partial"
    assert derive_status("running", ["failed", "cancelled"]) == "failed"
    assert derive_status("running", ["cancelled", "cancelled"]) == "cancelled"
    assert derive_status("cancelled", ["done", "running"]) == "cancelled"  # sticky


# --------------------------------------------------------------------------- run both


def test_run_both_merges_agreements_first(env: tuple[TestClient, Fakes, str]) -> None:
    client, fakes, section_id = env
    drain(client)
    group = ok(client.post("/api/groups/both", json=both_body(section_id)))["group"]
    assert group["kind"] == "both" and group["pass"] == "paragraph-function"
    assert group["scope"]["kind"] == "section" and group["scope"]["ref"] == section_id
    assert group["status"] in ("queued", "running", "done") and len(group["run_ids"]) == 2

    detail = wait_group(client, group["id"])
    group = detail["group"]
    assert group["status"] == "done" and group["finished_at"] >= group["created_at"]
    runs = detail["runs"]
    assert [r["id"] for r in runs] == group["run_ids"]
    assert [(r["runner"], r["model"], r["thinking"]) for r in runs] == [
        ("claude", "sonnet", "low"),
        ("codex", None, "low"),
    ]
    assert all(r["status"] == "done" and r["findings_count"] == 2 for r in runs)
    assert group["findings_count"] == 4 and group["agreements_count"] == 1
    assert group["cost_usd"] == pytest.approx(0.0123)  # codex reports no cost

    merged = detail["merged"]
    assert [(m["agreement"], m["line_start"]) for m in merged] == [(2, 13), (1, 19), (1, 26)]
    top = merged[0]
    assert top["runners"] == ["claude", "codex"] and top["severity"] == "critical"
    assert top["file"] == INTRO and [f["run_id"] for f in top["findings"]] == group["run_ids"]
    assert top["key"] == "+".join(f["id"] for f in top["findings"])
    assert merged[1]["runners"] == ["codex"] and merged[2]["runners"] == ["claude"]

    assert len(fakes.calls("claude")) == 1 and len(fakes.calls("codex")) == 1
    events: list[dict[str, Any]] = []

    def final_event() -> bool:
        events.extend(e for e in drain(client) if e["type"] == "group.updated")
        return bool(events) and events[-1]["group"]["status"] == "done"

    wait_for(final_event)  # the last event can land just after the GET saw "done"
    assert all(e["group"]["id"] == group["id"] for e in events)
    statuses = [e["group"]["status"] for e in events]
    assert statuses.count("done") == 1 and len(statuses) >= 2  # one event per change
    assert events[-1]["group"]["agreements_count"] == 1
    listed = ok(client.get("/api/groups"))["groups"]
    assert listed[0]["id"] == group["id"] and listed[0]["status"] == "done"


def test_run_both_members_run_together_but_exclude_a_plain_run(
    env: tuple[TestClient, Fakes, str],
) -> None:
    client, fakes, section_id = env
    fakes.set("claude", lines=claude_lines(CLAUDE_PAYLOAD), wait_for="go")
    fakes.set("codex", lines=codex_lines(CODEX_PAYLOAD), wait_for="go")
    group = ok(client.post("/api/groups/both", json=both_body(section_id)))["group"]
    wait_for(lambda: len(fakes.calls()) == 2)  # both members started side by side
    scope = {"kind": "section", "ref": section_id}
    plain = client.post("/api/runs", json={"pass": "paragraph-function", "scope": scope})
    assert plain.status_code == 409
    again = client.post("/api/groups/both", json=both_body(section_id))
    assert again.status_code == 409
    other_pass = client.post("/api/runs", json={"pass": "clarity", "scope": scope})
    assert other_pass.status_code == 200
    fakes.release()
    assert wait_group(client, group["id"])["group"]["status"] == "done"
    # and a plain run blocks a group on its scope
    fakes.set("claude", lines=claude_lines(CLAUDE_PAYLOAD), wait_for="go2")
    run = ok(
        client.post("/api/runs", json={"pass": "paragraph-function", "scope": scope, "force": True})
    )["run"]
    blocked = client.post("/api/groups/both", json=both_body(section_id, force=True))
    assert blocked.status_code == 409 and run["id"] in blocked.json()["detail"]
    fakes.release("go2")


def test_run_both_partial_when_one_member_fails(env: tuple[TestClient, Fakes, str]) -> None:
    client, fakes, section_id = env
    fakes.set("codex", lines=[], stderr="error: model overloaded\n", exit=1)
    group = ok(client.post("/api/groups/both", json=both_body(section_id)))["group"]
    detail = wait_group(client, group["id"])
    assert detail["group"]["status"] == "partial"
    assert [r["status"] for r in detail["runs"]] == ["done", "failed"]
    assert detail["group"]["agreements_count"] == 0 and detail["group"]["findings_count"] == 2
    assert all(m["agreement"] == 1 for m in detail["merged"])


def test_run_both_failed_when_every_member_fails(env: tuple[TestClient, Fakes, str]) -> None:
    client, fakes, section_id = env
    fakes.set("claude", lines=[], stderr="boom\n", exit=1)
    fakes.set("codex", lines=[], stderr="boom\n", exit=1)
    group = ok(client.post("/api/groups/both", json=both_body(section_id)))["group"]
    detail = wait_group(client, group["id"])
    assert detail["group"]["status"] == "failed" and detail["merged"] == []


def test_cancel_a_group_kills_every_member(env: tuple[TestClient, Fakes, str]) -> None:
    client, fakes, section_id = env
    fakes.set("claude", lines=claude_lines(CLAUDE_PAYLOAD), wait_for="never")
    fakes.set("codex", lines=codex_lines(CODEX_PAYLOAD), wait_for="never")
    group = ok(client.post("/api/groups/both", json=both_body(section_id)))["group"]
    wait_for(lambda: len(fakes.calls()) == 2)
    pids = [call["pid"] for call in fakes.calls()]
    for pid in pids:
        assert os.getpgid(pid) == pid  # each CLI leads its own process group
    cancelled = ok(client.post(f"/api/groups/{group['id']}/cancel"))["group"]
    assert cancelled["status"] == "cancelled"

    def gone(pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(lambda: all(gone(pid) for pid in pids), timeout=8)
    detail = wait_group(client, group["id"])
    assert detail["group"]["status"] == "cancelled" and detail["group"]["finished_at"]
    assert [r["status"] for r in detail["runs"]] == ["cancelled", "cancelled"]
    again = ok(client.post(f"/api/groups/{group['id']}/cancel"))["group"]
    assert again["status"] == "cancelled"  # a no-op once nothing is active
    assert client.post("/api/groups/nope/cancel").status_code == 404


def test_cache_hits_count_as_done_and_force_skips_them(
    env: tuple[TestClient, Fakes, str],
) -> None:
    client, fakes, section_id = env
    first = ok(client.post("/api/groups/both", json=both_body(section_id)))["group"]
    wait_group(client, first["id"])
    cached = ok(client.post("/api/groups/both", json=both_body(section_id)))["group"]
    assert cached["status"] == "done" and cached["agreements_count"] == 1
    detail = wait_group(client, cached["id"])
    assert [r["status"] for r in detail["runs"]] == ["cached", "cached"]
    assert detail["group"]["cost_usd"] == 0.0
    assert len(fakes.calls()) == 2
    forced = ok(client.post("/api/groups/both", json=both_body(section_id, force=True)))["group"]
    assert wait_group(client, forced["id"])["group"]["status"] == "done"
    assert len(fakes.calls()) == 4


def test_group_request_errors(env: tuple[TestClient, Fakes, str]) -> None:
    client, fakes, section_id = env
    scope = {"kind": "section", "ref": section_id}
    bad = [
        ({"pass": "nope", "scope": scope}, "unknown pass"),
        ({"pass": "triage", "scope": scope}, "cannot run"),
        ({"pass": "paragraph-function", "scope": {"kind": "section", "ref": "p-1"}}, "paragraph"),
        ({"pass": "paragraph-function"}, "invalid request"),
        ({"pass": "paragraph-function", "scope": scope, "runner": "claude"}, "invalid request"),
        ({"pass": "paragraph-function", "scope": scope, "codex": {"x": 1}}, "invalid request"),
        ({"pass": "paragraph-function", "scope": scope, "codex": {"thinking": "max"}}, "thinking"),
    ]
    for body, fragment in bad:
        response = client.post("/api/groups/both", json=body)
        assert response.status_code == 400, (body, response.text)
        assert fragment in response.json()["detail"], (body, response.json())
    for body, fragment in (
        ({"pass": "paragraph-function", "runner": "gpt"}, "unknown runner"),
        ({"pass": "paragraph-function", "scope": scope}, "invalid request"),
        ({"pass": "ab-compare"}, "cannot run"),
    ):
        response = client.post("/api/groups/fanout", json=body)
        assert response.status_code == 400, (body, response.text)
        assert fragment in response.json()["detail"], (body, response.json())
    path = fakes.repo / ".rewrite/config.toml"
    config = load_config(path)
    config.runner.codex.command = [str(fakes.bin / "no-codex"), "exec"]
    save_config(path, config)
    response = client.post("/api/groups/both", json=both_body(section_id))
    assert response.status_code == 400 and "codex is not available" in response.json()["detail"]
    foreign = client.post(
        "/api/groups/both", json=both_body(section_id), headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    assert client.get("/api/groups/nope").status_code == 404
    assert fakes.calls() == [] and ok(client.get("/api/groups")) == {"groups": []}


# --------------------------------------------------------------------------- fan-out


def test_fanout_needs_a_linked_section(client: TestClient, initialized_repo: Path) -> None:
    response = client.post("/api/groups/fanout", json={"pass": "paragraph-function"})
    assert response.status_code == 400 and "no section is linked" in response.json()["detail"]
    preview = client.post(
        "/api/runs/preview", json={"pass": "message-drift", "scope": {"kind": "ends"}}
    )
    assert preview.status_code == 400 and "linked section" in preview.json()["detail"]


def test_fanout_member_set(client: TestClient, initialized_repo: Path, tmp_path: Path) -> None:
    fakes = Fakes(tmp_path, initialized_repo)
    # quotes the introduction; "refs.bib" keeps it out of the other one-file section scopes
    fakes.set("claude", lines=claude_lines({"findings": [{**GOOD, "file": "refs.bib"}]}))
    section_ids = adopt_sections(client)
    assert len(section_ids) == 4
    body = {"pass": "paragraph-function", "runner": "claude", "model": "haiku", "thinking": "low"}
    group = ok(client.post("/api/groups/fanout", json=body))["group"]
    assert group["kind"] == "fanout" and group["scope"]["kind"] == "paper"
    detail = wait_group(client, group["id"])
    runs = detail["runs"]
    assert len(runs) == len(section_ids) + 1 and detail["group"]["status"] == "done"
    sections, drift = runs[:-1], runs[-1]
    assert [r["scope"]["ref"] for r in sections] == section_ids  # document order
    assert all(r["pass"] == "paragraph-function" for r in sections)
    assert drift["pass"] == "message-drift" and drift["scope"]["kind"] == "ends"
    assert {(r["runner"], r["model"], r["thinking"]) for r in runs} == {("claude", "haiku", "low")}
    # GOOD quotes the introduction: only the introduction's run can anchor it
    assert [r["findings_count"] for r in sections] == [1, 0, 0, 0]
    assert drift["findings_count"] == 1  # the ends scope holds the introduction too
    assert all(m["agreement"] == 1 for m in detail["merged"])
    assert detail["group"]["agreements_count"] == 0 and len(detail["merged"]) == 2

    prompts = {call["prompt"]: call for call in fakes.calls("claude")}
    assert len(prompts) == 5
    note = FANOUT_NOTE.split("{", 1)[0]
    reviews = [p for p in prompts if "## Pass: paragraph function" in p]
    (drift_prompt,) = [p for p in prompts if "## Pass: message drift" in p]
    assert len(reviews) == 4 and all(note in p for p in reviews)
    assert note not in drift_prompt
    for prompt in reviews:  # the full outline is global context in every member
        assert all(f"section {sid}" in prompt for sid in section_ids)
    assert "=== file: main.tex (lines 21-29) ===" in drift_prompt
    assert "Serverless platforms hide cold-start latency" in drift_prompt
    assert "=== file: sections/intro.tex" in drift_prompt
    assert "=== file: sections/conclusion.tex" in drift_prompt
    assert "sections/method.tex" not in drift_prompt.split("### Source", 1)[1]
    assert "We replay a 24-hour trace" not in drift_prompt
    assert "\\author{[removed]}" not in drift_prompt  # the abstract only, not the title block


def test_fanout_level_defaults_apply_per_member(
    client: TestClient, initialized_repo: Path, tmp_path: Path
) -> None:
    fakes = Fakes(tmp_path, initialized_repo)
    fakes.set("claude", lines=claude_lines({"findings": []}))
    adopt_sections(client)
    group = ok(client.post("/api/groups/fanout", json={"pass": "clarity"}))["group"]
    runs = wait_group(client, group["id"])["runs"]
    settings = [(r["pass"], r["runner"], r["model"], r["thinking"]) for r in runs]
    assert settings[:4] == [("clarity", "claude", "sonnet", "low")] * 4  # sentence level
    assert settings[4] == ("message-drift", "claude", "opus", "high")  # message level
    # cached on a second fan-out; a plain run of the same section is not a cache source
    again = ok(client.post("/api/groups/fanout", json={"pass": "clarity"}))["group"]
    assert [r["status"] for r in wait_group(client, again["id"])["runs"]] == ["cached"] * 5
    section = runs[0]["scope"]
    plain = ok(client.post("/api/runs", json={"pass": "clarity", "scope": section}))["run"]
    assert plain["status"] != "cached"


COUNTER_CLI = """#!{python}
import os, pathlib, sys, time
here = pathlib.Path(__file__).resolve().parent
if "--help" in sys.argv[1:]:
    sys.stdout.write((here / "fake-claude.help").read_text())
    sys.exit(0)
sys.stdin.read()

def mark(sign):
    fd = os.open(here / "concurrency.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    os.write(fd, (sign + " " + str(os.getpid()) + "\\n").encode())
    os.close(fd)

mark("+")
time.sleep(1)
mark("-")
sys.stdout.write((here / "counter.out").read_text())
"""


def test_fanout_runs_at_most_three_processes_at_once(
    client: TestClient, initialized_repo: Path, tmp_path: Path
) -> None:
    fakes = Fakes(tmp_path, initialized_repo)
    counter = fake_exe(fakes.bin, "counter-claude", "")
    counter.write_text(COUNTER_CLI.format(python=sys.executable))
    (fakes.bin / "counter.out").write_text("\n".join(claude_lines({"findings": []})) + "\n")
    path = initialized_repo / ".rewrite/config.toml"
    config = load_config(path)
    config.runner.claude.command = [str(counter), *config.runner.claude.command[1:]]
    save_config(path, config)
    adopt_sections(client)
    started = time.monotonic()
    group = ok(client.post("/api/groups/fanout", json={"pass": "clarity", "model": "haiku"}))
    detail = wait_group(client, group["group"]["id"], timeout=30)
    assert detail["group"]["status"] == "done" and len(detail["runs"]) == 5
    assert time.monotonic() - started >= 2.0  # 5 one-second runs, 3 at a time
    alive = peak = 0
    lines = (fakes.bin / "concurrency.log").read_text().split()
    marks = lines[::2]
    assert marks.count("+") == 5 and marks.count("-") == 5
    for sign in marks:
        alive += 1 if sign == "+" else -1
        peak = max(peak, alive)
    assert peak == 3


# --------------------------------------------------------------------------- restart


def test_groups_left_active_are_settled_on_start(initialized_repo: Path) -> None:
    from app import db, runstore
    from app.repo import Repo
    from tests.conftest import make_client

    def run_row(run_id: str, status: str) -> dict[str, Any]:
        return {
            "id": run_id,
            "pass": "clarity",
            "level": "sentence",
            "scope_kind": "paper",
            "scope_json": '{"kind": "paper"}',
            "scope_name": "the whole paper",
            "scope_key": "clarity:paper:",
            "runner": "claude",
            "prompt": "p",
            "status": status,
            "created_at": time.time(),
        }

    with db.connect(Repo.at(initialized_repo)) as conn:  # as a crashed server left it
        runstore.insert_run(conn, run_row("r1", "done"))
        runstore.insert_run(conn, run_row("r2", "running"))
        conn.execute(
            "INSERT INTO run_groups (id, kind, pass, scope_json, run_ids_json, status, "
            "created_at) VALUES ('g1', 'both', 'clarity', '{\"kind\": \"paper\"}', ?, "
            "'running', ?)",
            (json.dumps(["r1", "r2"]), time.time()),
        )
    with make_client(initialized_repo) as client:
        detail = ok(client.get("/api/groups/g1"))
    assert [r["status"] for r in detail["runs"]] == ["done", "failed"]
    assert detail["group"]["status"] == "partial"
    with db.connect(Repo.at(initialized_repo)) as conn:
        row = conn.execute("SELECT status, finished_at FROM run_groups WHERE id = 'g1'").fetchone()
    assert row[0] == "partial" and row[1] is not None
