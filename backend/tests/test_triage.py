"""Triage calls end to end with a fake `claude` (app/triage.py, the triage endpoints): the
prompt (findings + scoped source, never cached), verdict storage, ignored entries,
failures, "dismiss all flagged", and pass kinds."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.repo import Repo
from app.runner import RunResult
from app.triage import TriageAnswer, parse_verdicts
from tests.conftest import adopt_intro
from tests.test_groups import CLAUDE_ONLY, CODEX_ONLY, drain, ok
from tests.test_runs_api import GOOD, Fakes, claude_lines, wait_for, wait_run

FINDINGS = {"findings": [GOOD, CLAUDE_ONLY, CODEX_ONLY]}
VERDICTS = {
    "verdicts": [
        {"index": 1, "verdict": "keep", "reason": "The opening hides the point."},
        {"index": 2, "verdict": "trivial", "reason": "Two observations fit one paragraph."},
        {"index": 3, "verdict": "wrong", "reason": "The sentence states the alternative."},
        {"index": 9, "verdict": "wrong", "reason": "No such finding."},
        {"index": 1, "verdict": "wrong", "reason": "A second verdict for the first one."},
    ]
}


class Env:
    def __init__(self, client: TestClient, repo: Path, fakes: Fakes) -> None:
        self.client = client
        self.repo = repo
        self.fakes = fakes
        self.section_id, _ = adopt_intro(client)

    def review(self) -> dict[str, Any]:
        """A finished paragraph-function run with the three FINDINGS."""
        self.fakes.set("claude", lines=claude_lines(FINDINGS))
        body = {
            "pass": "paragraph-function",
            "scope": {"kind": "section", "ref": self.section_id},
            "force": True,
        }
        run = ok(self.client.post("/api/runs", json=body))["run"]
        detail = wait_run(self.client, run["id"])
        assert detail["run"]["status"] == "done" and len(detail["findings"]) == 3
        return detail

    def triage(self, run_id: str, answer: Any = VERDICTS, **body: Any) -> dict[str, Any]:
        self.fakes.set("claude", lines=claude_lines(answer))
        body = {"runner": "claude", "model": "haiku", "thinking": "low", **body}
        return ok(self.client.post(f"/api/runs/{run_id}/triage", json=body))["triage"]

    def wait(self, run_id: str, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            detail = ok(self.client.get(f"/api/runs/{run_id}/triage"))
            triage = detail["triage"]
            if triage is not None and triage["status"] in ("done", "failed"):
                return detail
            if time.monotonic() > deadline:
                raise AssertionError(f"triage stuck: {triage}")
            time.sleep(0.05)


@pytest.fixture
def env(client: TestClient, initialized_repo: Path, tmp_path: Path) -> Iterator[Env]:
    fakes = Fakes(tmp_path, initialized_repo)
    yield Env(client, initialized_repo, fakes)
    fakes.release()


# --------------------------------------------------------------------------- lifecycle


def test_triage_lifecycle(env: Env) -> None:
    client = env.client
    review = env.review()
    run_id = review["run"]["id"]
    before = ok(client.get(f"/api/runs/{run_id}/triage"))
    assert before["triage"] is None and len(before["findings"]) == 3
    assert all(
        f["triage_verdict"] is None and f["triage_reason"] is None for f in before["findings"]
    )
    drain(client)

    created = env.triage(run_id)
    assert created["run_id"] == run_id and created["status"] in ("queued", "running")
    assert created["triage_run_id"] and created["counts"] == {"keep": 0, "wrong": 0, "trivial": 0}
    assert created["sent"] == 3 and created["finished_at"] is None

    detail = env.wait(run_id)
    triage = detail["triage"]
    assert triage["status"] == "done" and triage["error"] is None
    assert triage["counts"] == {"keep": 1, "wrong": 1, "trivial": 1}
    assert triage["ignored"] == 2  # index 9 is not in the list; index 1 came twice
    assert triage["finished_at"] >= triage["created_at"]
    by_quote = {f["quote"]: f for f in detail["findings"]}
    assert by_quote[GOOD["quote"]]["triage_verdict"] == "keep"
    assert by_quote[GOOD["quote"]]["triage_reason"] == "The opening hides the point."
    assert by_quote[CLAUDE_ONLY["quote"]]["triage_verdict"] == "trivial"
    assert by_quote[CODEX_ONLY["quote"]]["triage_verdict"] == "wrong"
    assert all(f["state"] == "open" for f in detail["findings"])  # verdicts change no state

    run = wait_run(client, triage["triage_run_id"])["run"]
    assert run["pass"] == "triage" and run["level"] == "message" and run["status"] == "done"
    assert run["scope"] == review["run"]["scope"] and run["findings_count"] == 0
    assert (run["model"], run["thinking"]) == ("haiku", "low")
    events = drain(client)
    finished = [e for e in events if e["type"] == "triage.finished"]
    assert len(finished) == 1 and finished[0]["triage"]["counts"]["wrong"] == 1
    assert {"type": "findings.updated", "run_id": run_id, "file": None} in events
    assert not any(e["type"] == "findings.updated" and e["run_id"] == run["id"] for e in events)

    # the prompt: system.md + the triage pass, with the findings and the scoped source
    call = env.fakes.calls("claude")[-1]
    prompt = call["prompt"]
    assert prompt == ok(client.get(f"/api/runs/{run['id']}/prompt"))["prompt"]
    assert "## Pass: triage of screening findings" in prompt
    assert "1. sections/intro.tex, line 13 (paragraph, topic-sentence, major; spec entry p-2)" in (
        prompt
    )
    assert f"   quote: {GOOD['quote']}" in prompt and f"   problem: {GOOD['problem']}" in prompt
    assert "3. sections/intro.tex, line 19" in prompt and "4. sections/" not in prompt
    assert "  13 | Providers mitigate cold starts with warm pools" in prompt
    assert f'section "Introduction" ({env.section_id})' in prompt
    schema = json.loads(call["argv"][call["argv"].index("--json-schema") + 1])
    assert schema["required"] == ["verdicts"]

    # "Dismiss all flagged": wrong and trivial become dismissed, once
    assert ok(client.post("/api/findings/dismiss-flagged", json={"run_id": run_id})) == {
        "updated": 2
    }
    states = {f["quote"]: f["state"] for f in ok(client.get(f"/api/runs/{run_id}"))["findings"]}
    assert states == {
        GOOD["quote"]: "open",
        CLAUDE_ONLY["quote"]: "dismissed",
        CODEX_ONLY["quote"]: "dismissed",
    }
    assert ok(client.post("/api/findings/dismiss-flagged", json={"run_id": run_id})) == {
        "updated": 0
    }


def test_triage_is_never_cached_and_judges_only_open_findings(env: Env) -> None:
    client = env.client
    run_id = env.review()["run"]["id"]
    env.triage(run_id)
    env.wait(run_id)
    ok(client.post("/api/findings/dismiss-flagged", json={"run_id": run_id}))
    calls = len(env.fakes.calls("claude"))
    # second triage: only the one open finding is sent; the dismissed keep their verdicts
    second = env.triage(
        run_id, {"verdicts": [{"index": 1, "verdict": "trivial", "reason": "Minor."}]}
    )
    assert second["sent"] == 1
    detail = env.wait(run_id)
    assert detail["triage"]["id"] == second["id"]  # the latest triage
    assert detail["triage"]["counts"] == {"keep": 0, "wrong": 0, "trivial": 1}
    verdicts = {f["quote"]: f["triage_verdict"] for f in detail["findings"]}
    assert verdicts == {
        GOOD["quote"]: "trivial",
        CLAUDE_ONLY["quote"]: "trivial",
        CODEX_ONLY["quote"]: "wrong",
    }
    assert len(env.fakes.calls("claude")) == calls + 1
    prompt = env.fakes.calls("claude")[-1]["prompt"]
    assert GOOD["quote"] in prompt and CODEX_ONLY["quote"] not in prompt.split("### Source")[0]
    # the same triage again is a new call (never cached)
    ok(client.patch(f"/api/findings/{detail['findings'][0]['id']}", json={"state": "open"}))
    third = env.triage(run_id, {"verdicts": []})
    final = env.wait(run_id)
    assert final["triage"]["id"] == third["id"] and final["triage"]["status"] == "done"
    assert len(env.fakes.calls("claude")) == calls + 2
    # a finding that was sent but got no verdict loses its earlier one
    assert final["findings"][0]["triage_verdict"] is None
    with db.connect(Repo.at(env.repo)) as conn:
        rows = conn.execute("SELECT status, cache_key FROM runs WHERE pass = 'triage'").fetchall()
    assert len(rows) == 3 and all(row == ("done", None) for row in rows)


def test_triage_failures(env: Env) -> None:
    client = env.client
    run_id = env.review()["run"]["id"]
    env.triage(run_id)
    first = env.wait(run_id)
    # an answer without a verdict list fails the triage and changes no verdict
    env.fakes.set("claude", lines=claude_lines(text="All of them are fine.", structured=False))
    ok(client.post(f"/api/runs/{run_id}/triage", json={"runner": "claude"}))
    failed = env.wait(run_id)
    assert failed["triage"]["status"] == "failed" and "verdicts" in failed["triage"]["error"]
    assert [f["triage_verdict"] for f in failed["findings"]] == [
        f["triage_verdict"] for f in first["findings"]
    ]
    # an answer that is a JSON object without `verdicts`
    env.triage(run_id, {"findings": []})
    assert "no `verdicts` list" in env.wait(run_id)["triage"]["error"]


def test_triage_request_errors(env: Env) -> None:
    client = env.client
    assert client.post("/api/runs/nope/triage", json={}).status_code == 404
    assert client.get("/api/runs/nope/triage").status_code == 404
    run_id = env.review()["run"]["id"]
    for body in ({"runner": "gpt"}, {"thinking": "ultra"}):
        response = client.post(f"/api/runs/{run_id}/triage", json=body)
        assert response.status_code == 400, response.text
    bad = client.post(f"/api/runs/{run_id}/triage", json={"model": "x", "extra": 1})
    assert bad.status_code == 400 and "invalid request" in bad.json()["detail"]
    env.fakes.set("claude", lines=claude_lines(VERDICTS))
    no_body = client.post(f"/api/runs/{run_id}/triage")  # every field is optional
    assert no_body.status_code == 200
    assert env.wait(run_id)["triage"]["status"] == "done"
    env.fakes.set("claude", lines=claude_lines(VERDICTS), wait_for="go")
    gated = ok(client.post(f"/api/runs/{run_id}/triage", json={}))["triage"]
    wait_for(lambda: len(env.fakes.calls("claude")) == 3)  # the review and two triages
    busy = client.post(f"/api/runs/{run_id}/triage", json={})
    assert busy.status_code == 409 and gated["id"] in busy.json()["detail"]
    env.fakes.release()
    env.wait(run_id)
    # a triage run has no findings of its own
    triage_run = gated["triage_run_id"]
    response = client.post(f"/api/runs/{triage_run}/triage", json={})
    assert response.status_code == 400 and "no open findings" in response.json()["detail"]
    for finding in ok(client.get(f"/api/runs/{run_id}"))["findings"]:
        client.patch(f"/api/findings/{finding['id']}", json={"state": "addressed"})
    response = client.post(f"/api/runs/{run_id}/triage", json={})
    assert response.status_code == 400 and "no open findings" in response.json()["detail"]
    # dismiss-flagged: body and run checks
    for body in ({}, {"run_id": ""}, {"run_id": run_id, "x": 1}, {"run_id": 3}):
        assert client.post("/api/findings/dismiss-flagged", json=body).status_code == 400
    assert client.post("/api/findings/dismiss-flagged", json={"run_id": "nope"}).status_code == 404
    foreign = client.post(
        f"/api/runs/{run_id}/triage", json={}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403


def test_a_repo_without_the_triage_pass(env: Env) -> None:
    run_id = env.review()["run"]["id"]
    (env.repo / ".rewrite/prompts/passes/triage.md").unlink()
    response = env.client.post(f"/api/runs/{run_id}/triage", json={})
    assert response.status_code == 400 and "rewrite init" in response.json()["detail"]


def test_interrupted_triages_fail_on_startup(env: Env) -> None:
    from tests.conftest import make_client

    run_id = env.review()["run"]["id"]
    with db.connect(Repo.at(env.repo)) as conn:
        conn.execute(
            "INSERT INTO triages (id, run_id, status, finding_ids_json, created_at) "
            "VALUES ('t1', ?, 'running', '[]', ?)",
            (run_id, time.time()),
        )
    with make_client(env.repo) as other:
        triage = ok(other.get(f"/api/runs/{run_id}/triage"))["triage"]
    assert triage["id"] == "t1" and triage["status"] == "failed"
    assert "interrupted" in triage["error"]


# --------------------------------------------------------------------------- parsing


def result(value: Any = None, text: str = "") -> RunResult:
    return RunResult(status="ok", structured=value, text=text)


def test_parse_verdicts() -> None:
    entries = [
        {"index": 1, "verdict": "KEEP ", "reason": "Real."},
        {"index": 2, "verdict": "wrong"},  # reason missing: accepted, empty
        {"index": 3, "verdict": "maybe", "reason": "x"},
        {"index": True, "verdict": "keep", "reason": "x"},
        {"index": 4, "verdict": "keep", "reason": "x", "replacement": "Better text."},
        {
            "index": 5,
            "verdict": "trivial",
            "reason": 'It should read "the restored instance faults in all of its memory pages '
            'at once"',
        },
        "keep",
    ]
    outcome = parse_verdicts(result({"verdicts": entries}))
    assert outcome.status == "done" and isinstance(outcome.data, TriageAnswer)
    assert [(v.index, v.verdict, v.reason) for v in outcome.data.verdicts] == [
        (1, "keep", "Real."),
        (2, "wrong", ""),
    ]
    assert outcome.data.ignored == 5
    bare = parse_verdicts(result(text='[{"index": 1, "verdict": "trivial", "reason": "x"}]'))
    assert bare.status == "done" and bare.data.verdicts[0].verdict == "trivial"
    fenced = parse_verdicts(result(text='Sure:\n```json\n{"verdicts": []}\n```'))
    assert fenced.status == "done" and fenced.data.verdicts == []
    for value, text, fragment in (
        (None, "no json here", "could not parse"),
        ({"verdict": "keep"}, "", "no `verdicts` list"),
        ({"verdicts": "keep"}, "", "no `verdicts` list"),
        ("keep", "", "not a JSON object"),
    ):
        failed = parse_verdicts(result(value, text))
        assert failed.status == "failed" and fragment in (failed.error or ""), failed


# --------------------------------------------------------------------------- pass kinds


def test_pass_kinds(env: Env) -> None:
    client = env.client
    passes = {p["name"]: p for p in ok(client.get("/api/passes"))["passes"]}
    assert passes["triage"]["kind"] == "triage" and passes["triage"]["runnable"] is False
    assert passes["triage"]["error"] is None
    assert passes["ab-compare"]["kind"] == "compare" and passes["ab-compare"]["runnable"] is False
    review = [p for p in passes.values() if p["kind"] == "review"]
    assert len(review) == 8 and all(p["runnable"] for p in review)
    scope = {"kind": "section", "ref": env.section_id}
    for endpoint in ("/api/runs", "/api/runs/preview"):
        response = client.post(endpoint, json={"pass": "triage", "scope": scope})
        assert response.status_code == 400
        assert "cannot run as a review: it is a triage pass" in response.json()["detail"]
    pass_dir = env.repo / ".rewrite/prompts/passes"
    text = (pass_dir / "clarity.md").read_text()
    (pass_dir / "odd.md").write_text(text.replace("name: clarity", "name: odd\nkind: other"))
    (pass_dir / "uses-findings.md").write_text(
        text.replace("name: clarity", "name: uses-findings") + "\n{{ findings }}\n"
    )
    (pass_dir / "my-triage.md").write_text(
        text.replace("name: clarity", "name: my-triage\nkind: triage") + "\n{{ version_a }}\n"
    )
    passes = {p["name"]: p for p in ok(client.get("/api/passes"))["passes"]}
    assert passes["odd"]["error"] and "kind" in passes["odd"]["error"]
    assert "findings" in passes["uses-findings"]["error"]  # a review pass gets no findings
    assert passes["uses-findings"]["runnable"] is False
    assert "version_a" in passes["my-triage"]["error"]
    assert env.fakes.calls() == []
