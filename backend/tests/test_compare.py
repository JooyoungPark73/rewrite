"""Blind A/B comparisons end to end with a fake `claude` (app/compare.py, app/api/compare.py):
before-text extraction at a ref, the random order, the verdict lifecycle, reveal gating,
failures, cancel, no caching, and the rules (no edit hints, spec file untouched)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import compare, db, prompts, skeleton
from app.compare import CompareRequest, parse_verdict
from app.repo import Repo
from app.runner import RunResult
from tests.conftest import adopt_intro, git, requires_git
from tests.test_runs_api import Fakes, claude_lines

pytestmark = requires_git

INTRO = "sections/intro.tex"
OLD_SENTENCE = "Snapshot restore is the main alternative."
NEW_SENTENCE = "The main alternative to warm pools is to restore each instance from a snapshot."
VERDICT = {"verdict": "A", "reason": "Version B buries the alternative after its mechanism."}


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def drain(client: TestClient) -> list[dict[str, Any]]:
    state = client.app.state.pb  # type: ignore[attr-defined]
    queue = getattr(state, "_test_queue", None)
    if queue is None:
        queue = state._test_queue = state.bus.subscribe()
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


class Env:
    def __init__(self, client: TestClient, repo: Path, fakes: Fakes, initial: str) -> None:
        self.client = client
        self.repo = repo
        self.fakes = fakes
        self.initial = initial  # sha of the commit before any marker existed
        self.section_id, self.paragraph_ids = adopt_intro(client)
        self.snapshot = ok(client.post("/api/snapshots", json={"note": "v1"}))["snapshot"]

    def reword(self, old: str = OLD_SENTENCE, new: str = NEW_SENTENCE) -> None:
        path = self.repo / INTRO
        text = path.read_text()
        assert old in text
        path.write_text(text.replace(old, new, 1))

    def body(self, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "file": INTRO,
            "node_id": "p-3",
            "before": {"kind": "snapshot", "ref": self.snapshot["id"]},
            "runner": "claude",
            "model": "sonnet",
            "thinking": "low",
        }
        body.update(extra)
        return body

    def start(self, **extra: Any) -> dict[str, Any]:
        return ok(self.client.post("/api/compare", json=self.body(**extra)))["comparison"]

    def wait(self, comparison_id: str, timeout: float = 20.0, **params: Any) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            detail = ok(self.client.get(f"/api/compare/{comparison_id}", params=params))
            if detail["comparison"]["status"] in ("done", "failed"):
                return detail
            if time.monotonic() > deadline:
                raise AssertionError(f"comparison stuck: {detail['comparison']}")
            time.sleep(0.05)


@pytest.fixture
def env(client: TestClient, git_repo: Path, tmp_path: Path) -> Iterator[Env]:
    initial = git(git_repo, "rev-parse", "HEAD").strip()
    fakes = Fakes(tmp_path, git_repo)
    fakes.set("claude", lines=claude_lines(VERDICT))
    environment = Env(client, git_repo, fakes, initial)
    yield environment
    fakes.release()


def paragraph(repo: Path, ref: str | None, lines: tuple[int, int]) -> str:
    text = git(repo, "show", f"{ref}:{INTRO}") if ref else (repo / INTRO).read_text()
    return "\n".join(text.splitlines()[lines[0] - 1 : lines[1]])


# --------------------------------------------------------------------------- the lifecycle


def test_comparison_lifecycle(env: Env) -> None:
    client = env.client
    env.reword()
    drain(client)
    created = env.start()
    assert created["status"] in ("queued", "running")
    assert created["mapping"] is None and created["verdict"] is None
    assert created["before"] == {"kind": "snapshot", "ref": env.snapshot["id"]}
    assert created["file"] == INTRO and created["node_id"] == "p-3"
    assert created["line_start"] is None and created["line_end"] is None
    assert created["before_sha"] == env.snapshot["sha"]
    assert created["before_lines"] == [19, 23] and created["after_lines"] == [19, 23]
    assert created["run_id"]

    detail = env.wait(created["id"])
    comparison = detail["comparison"]
    assert comparison["status"] == "done" and comparison["verdict"] == "A"
    assert comparison["reason"] == VERDICT["reason"] and comparison["error"] is None
    assert comparison["finished_at"] >= comparison["created_at"]
    assert comparison["mapping"] is None  # not revealed
    assert detail["before_text"] == paragraph(env.repo, env.snapshot["sha"], (19, 23))
    assert detail["after_text"] == paragraph(env.repo, None, (19, 23))
    assert OLD_SENTENCE in detail["before_text"] and NEW_SENTENCE in detail["after_text"]
    run = detail["run"]
    assert run["pass"] == "ab-compare" and run["level"] == "paragraph"
    assert run["scope"] == {
        "kind": "selection",
        "ref": None,
        "file": INTRO,
        "line_start": 19,
        "line_end": 23,
    }
    assert run["status"] == "done" and run["findings_count"] == 0
    assert run["model"] == "sonnet" and run["thinking"] == "low" and run["cost_usd"] == 0.0123
    assert json.loads(detail["raw_output"]) == VERDICT

    listed = ok(client.get("/api/compare"))["comparisons"]
    assert [c["id"] for c in listed] == [created["id"]]
    assert listed[0]["mapping"] is None and listed[0]["verdict"] == "A"

    revealed = ok(client.get(f"/api/compare/{created['id']}", params={"reveal": "true"}))
    mapping = revealed["comparison"]["mapping"]
    assert mapping in ({"A": "before", "B": "after"}, {"A": "after", "B": "before"})
    assert revealed["comparison"]["revealed_at"] is not None
    shown = {
        "before": prompts.compare_text(detail["before_text"]),
        "after": prompts.compare_text(detail["after_text"]),
    }
    assert revealed["version_a"] == shown[mapping["A"]]
    assert revealed["version_b"] == shown[mapping["B"]]
    assert (
        ok(client.get(f"/api/compare/{created['id']}", params={"reveal": 1}))["comparison"][
            "mapping"
        ]
        == mapping
    )
    assert ok(client.get("/api/compare"))["comparisons"][0]["mapping"] is None

    events = drain(client)
    finished = [e for e in events if e["type"] == "compare.finished"]
    assert len(finished) == 1
    assert finished[0]["comparison"]["id"] == created["id"]
    assert finished[0]["comparison"]["status"] == "done"
    assert finished[0]["comparison"]["mapping"] is None
    types = [e["type"] for e in events]
    assert "run.started" in types and "run.finished" in types
    assert "findings.updated" not in types


def test_the_prompt_is_blind_and_reduced(env: Env) -> None:
    client = env.client
    ok(client.post("/api/spec/ops", json={"op": {"type": "update", "id": "p-3", "says": "LOCAL"}}))
    claim = ok(client.post("/api/spec/ops", json={"op": {"type": "add_claim", "says": "CLAIM"}}))
    ok(
        client.post(
            "/api/spec/ops",
            json={"op": {"type": "link_claim", "id": "p-3", "claim_id": claim["new_id"]}},
        )
    )
    other = ok(client.post("/api/spec/ops", json={"op": {"type": "add_claim", "says": "OTHER"}}))
    ok(client.post("/api/spec/ops", json={"op": {"type": "update", "id": "p-1", "says": "P1"}}))
    env.reword()
    created = env.start()
    detail = env.wait(created["id"])
    prompt = ok(client.get(f"/api/runs/{created['run_id']}/prompt"))["prompt"]
    call = env.fakes.calls("claude")[-1]
    assert call["prompt"] == prompt
    assert Path(call["cwd"]).resolve() == (env.repo / ".rewrite" / "sandbox").resolve()
    schema = json.loads(call["argv"][call["argv"].index("--json-schema") + 1])
    assert schema == compare.VERDICT_SCHEMA
    assert "--model" in call["argv"] and "sonnet" in call["argv"]

    body = prompt.split("## Pass: blind comparison", 1)[1]
    assert "### Version A" in body and "### Version B" in body
    assert detail["version_a"] in body and detail["version_b"] in body
    assert "paragraph p-3" in body and "LOCAL" in body and "CLAIM" in body
    assert env.section_id in body
    for absent in ("P1", "OTHER", other["new_id"], "p-1", "p-2", "p-4"):
        assert absent not in body, absent
    assert "% @p" not in detail["version_a"] + detail["version_b"]  # markers are not prose
    lowered = body.lower()
    for hint in ("revision", "revised", "edit", "older", "newer", "original", "earlier"):
        assert hint not in lowered, hint
    for secret in (env.snapshot["tag"], env.snapshot["id"], env.snapshot["sha"][:7], "v1"):
        assert secret not in prompt, secret
    assert '"verdict"' in body and '"reason"' in body and "problems_a" not in body


def test_the_order_is_random(env: Env) -> None:
    env.reword()
    state = env.client.app.state.pb  # type: ignore[attr-defined]
    request = CompareRequest.model_validate(env.body())
    seen = set()
    for _ in range(20):
        prepared = compare.prepare(Repo.at(env.repo), state.runs, request)
        mapping = prepared.mapping
        seen.add((mapping.A, mapping.B))
        texts = {"before": prepared.before.text, "after": prepared.after.text}
        assert prepared.version_a == prompts.compare_text(texts[mapping.A])
        assert prepared.version_b == prompts.compare_text(texts[mapping.B])
        assert prepared.plan.assembled.prompt.count(prepared.version_a) >= 1
    assert seen == {("before", "after"), ("after", "before")}


def test_reveal_waits_for_the_verdict(env: Env) -> None:
    env.reword()
    env.fakes.set("claude", lines=claude_lines(VERDICT), wait_for="go")
    created = env.start()
    running = ok(env.client.get(f"/api/compare/{created['id']}", params={"reveal": "true"}))
    assert running["comparison"]["status"] in ("queued", "running")
    assert running["comparison"]["mapping"] is None
    assert running["comparison"]["revealed_at"] is None
    env.fakes.release()
    done = env.wait(created["id"], reveal="true")
    assert done["comparison"]["mapping"] is not None


def test_cancel_through_the_run_fails_the_comparison(env: Env) -> None:
    env.reword()
    env.fakes.set("claude", lines=claude_lines(VERDICT), wait_for="go")
    created = env.start()
    deadline = time.monotonic() + 10
    while ok(env.client.get(f"/api/runs/{created['run_id']}"))["run"]["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    cancelled = ok(env.client.post(f"/api/runs/{created['run_id']}/cancel"))["run"]
    assert cancelled["status"] == "cancelled"
    detail = env.wait(created["id"], reveal="true")
    assert detail["comparison"]["status"] == "failed"
    assert detail["comparison"]["error"] == "cancelled"
    assert detail["comparison"]["mapping"] is None


def test_comparisons_are_never_cached(env: Env) -> None:
    env.reword()
    first = env.wait(env.start()["id"])
    second = env.wait(env.start()["id"])
    assert first["run"]["id"] != second["run"]["id"]
    assert second["run"]["status"] == "done" and second["run"]["cached_from"] is None
    assert len(env.fakes.calls("claude")) == 2
    with db.connect(Repo.at(env.repo)) as conn:
        keys = conn.execute("SELECT cache_key FROM runs WHERE pass = 'ab-compare'").fetchall()
    assert keys == [(None,), (None,)]
    refused = env.client.post(
        "/api/runs",
        json={
            "pass": "ab-compare",
            "scope": {"kind": "selection", "file": INTRO, "line_start": 19, "line_end": 23},
        },
    )
    assert refused.status_code == 400 and "cannot run" in refused.json()["detail"]


# --------------------------------------------------------------------------- locating text


def test_before_by_marker_at_a_ref() -> None:
    lines = [
        "\\section{Intro}",
        "% @p p-1",
        "First line.",
        "% a comment line stays in the paragraph",
        "Second line.",
        "",
        "%@p p-2",
        "Other paragraph.",
        "% @p p-3",
        "\\section{Next}",
    ]
    assert skeleton.marker_paragraph(lines, "p-1") == (2, 3, 5)
    assert skeleton.marker_paragraph(lines, "p-2") == (7, 8, 8)
    assert skeleton.marker_paragraph(lines, "p-3") is None  # nothing follows
    assert skeleton.marker_paragraph(lines, "p-9") is None
    found = compare.locate(lines, "p-1", None)
    assert found is not None and (found.line_start, found.line_end) == (3, 5)
    assert found.text == "First line.\n% a comment line stays in the paragraph\nSecond line."
    fallback = compare.locate(lines, "p-9", (8, 9))
    assert fallback is not None and fallback.text == "Other paragraph.\n% @p p-3"
    clamped = compare.locate(lines, None, (9, 40))
    assert clamped is not None and clamped.line_end == len(lines)
    assert compare.locate(lines, "p-9", None) is None
    assert compare.locate(lines, None, (11, 12)) is None
    assert prompts.compare_text(found.text) == "First line.\nSecond line."
    assert prompts.compare_text("a % revised\n\\author{X}\n100\\% sure") == (
        "a\n\\author{[removed]}\n100\\% sure"
    )


def test_before_falls_back_to_the_line_range(env: Env) -> None:
    """At the first commit no marker exists yet: the line range is read at that ref, while
    the working tree is found by the marker."""
    env.reword()
    no_range = env.client.post(
        "/api/compare", json=env.body(before={"kind": "sha", "ref": env.initial})
    )
    assert no_range.status_code == 400
    assert "cannot be found at" in no_range.json()["detail"]
    created = env.start(before={"kind": "sha", "ref": env.initial[:12]}, line_start=16, line_end=20)
    assert created["before_lines"] == [16, 20] and created["after_lines"] == [19, 23]
    assert created["line_start"] == 16 and created["before_sha"] == env.initial
    detail = env.wait(created["id"])
    assert detail["before_text"] == paragraph(env.repo, env.initial, (16, 20))
    assert detail["before_text"].startswith(OLD_SENTENCE)
    assert detail["after_text"].startswith(NEW_SENTENCE)


def test_line_range_only(env: Env) -> None:
    env.reword()
    created = env.start(node_id=None, line_start=19, line_end=23)
    assert created["node_id"] is None
    detail = env.wait(created["id"])
    assert detail["comparison"]["status"] == "done"
    assert detail["before_text"] == paragraph(env.repo, env.snapshot["sha"], (19, 23))
    prompt = ok(env.client.get(f"/api/runs/{created['run_id']}/prompt"))["prompt"]
    assert "paragraph p-3 of sections/intro.tex" in prompt  # inferred from the marker


def test_request_errors(env: Env) -> None:
    client = env.client
    env.reword()
    cases = [
        (env.body(node_id=None), "node_id"),
        (env.body(line_start=3), "both"),
        (env.body(node_id=None, line_start=5, line_end=2), "line range"),
        (env.body(node_id="p-99"), "unknown spec node"),
        (env.body(node_id=env.section_id), "not a paragraph"),
        (env.body(before={"kind": "snapshot", "ref": "nope"}), "unknown snapshot"),
        (env.body(before={"kind": "sha", "ref": "deadbeef"}), "unknown ref"),
        (env.body(before={"kind": "sha", "ref": "--all"}), "not a valid ref"),
        (env.body(file="../x.tex"), ""),
        (env.body(file="paper.json"), "not a source file"),
        (env.body(file="sections/none.tex"), "cannot be read"),
        (env.body(runner="nope"), "unknown runner"),
        (env.body(thinking="extreme"), "thinking"),
        (env.body(before={"kind": "branch", "ref": "main"}), "invalid request"),
        ({**env.body(), "extra": 1}, "invalid request"),
    ]
    for body, fragment in cases:
        response = client.post("/api/compare", json=body)
        assert response.status_code == 400, (body, response.text)
        assert fragment in response.json()["detail"], (fragment, response.json())

    env.reword(NEW_SENTENCE, OLD_SENTENCE)  # back to the snapshot's text
    same = client.post("/api/compare", json=env.body())
    assert same.status_code == 400 and "same text" in same.json()["detail"]

    path = env.repo / INTRO
    path.write_text(path.read_text().replace("% @p p-3\n", ""))
    gone = client.post("/api/compare", json=env.body())
    assert gone.status_code == 400 and "working tree" in gone.json()["detail"]

    (env.repo / "paper.json").write_text("{broken")
    broken = client.post("/api/compare", json=env.body())
    assert broken.status_code == 409
    foreign = client.post(
        "/api/compare", json=env.body(), headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    assert ok(client.get("/api/compare")) == {"comparisons": []}
    assert client.get("/api/compare/nope").status_code == 404


def test_not_a_git_repo(client: TestClient) -> None:
    adopt_intro(client)
    response = client.post(
        "/api/compare",
        json={"file": INTRO, "node_id": "p-1", "before": {"kind": "sha", "ref": "HEAD"}},
    )
    assert response.status_code == 400 and "not a git repository" in response.json()["detail"]
    assert ok(client.get("/api/compare")) == {"comparisons": []}


# --------------------------------------------------------------------------- the answer


@pytest.mark.parametrize(
    ("payload", "text", "fragment"),
    [
        ({"verdict": "C", "reason": "x"}, None, "verdict must be"),
        ({"verdict": "A"}, None, "missing reason"),
        ({"verdict": "A", "reason": "x", "problems_a": []}, None, "unexpected problems_a"),
        ({"verdict": "A", "reason": 3}, None, "reason must be a string"),
        (None, "Version A is clearly better.", "could not parse"),
        (["A"], None, "not a JSON object"),
    ],
)
def test_malformed_answers_fail(env: Env, payload: Any, text: str | None, fragment: str) -> None:
    env.reword()
    if text is not None:
        env.fakes.set("claude", lines=claude_lines(None, text=text, structured=False))
    else:
        env.fakes.set("claude", lines=claude_lines(payload))
    drain(env.client)
    created = env.start()
    detail = env.wait(created["id"], reveal="true")
    comparison = detail["comparison"]
    assert comparison["status"] == "failed" and comparison["verdict"] is None
    assert fragment in comparison["error"], comparison["error"]
    assert comparison["mapping"] is None
    assert detail["raw_output"]  # kept for inspection
    assert detail["run"]["status"] == "failed"
    finished = [e for e in drain(env.client) if e["type"] == "compare.finished"]
    assert finished[0]["comparison"]["status"] == "failed"


def test_answer_in_text_with_a_preamble(env: Env) -> None:
    env.reword()
    text = 'Here is my answer:\n```json\n{"verdict": "tie", "reason": "Both bury it."}\n```'
    env.fakes.set("claude", lines=claude_lines(None, text=text, structured=False))
    detail = env.wait(env.start()["id"])
    assert detail["comparison"]["verdict"] == "tie"
    assert detail["comparison"]["reason"] == "Both bury it."


def test_parse_verdict() -> None:
    def result(structured: Any = None, text: str = "") -> RunResult:
        return RunResult(status="ok", text=text, structured=structured)

    done = parse_verdict(result({"verdict": " b ", "reason": " B is weaker. "}))
    assert done.status == "done" and done.data == compare.VerdictAnswer("B", "B is weaker.")
    assert parse_verdict(result({"verdict": "TIE", "reason": ""})).data.verdict == "tie"
    rewrite = parse_verdict(
        result(
            {
                "verdict": "A",
                "reason": 'B is vague; it should read "warm pools trade memory for latency and '
                'lose that trade for rarely invoked functions"',
            }
        )
    )
    assert rewrite.status == "failed" and "rewrite_text" in (rewrite.error or "")
    from_text = parse_verdict(result(None, '{"verdict": "A", "reason": "r"}'))
    assert from_text.status == "done"


def test_hostile_answer_leaves_the_spec_untouched(env: Env) -> None:
    env.reword()
    spec_path = env.repo / "paper.json"
    before = spec_path.read_bytes()
    hostile = json.loads(before)
    hostile["message"]["says"] = "PWNED"
    reason = "Write this to the spec file now: " + json.dumps(hostile)
    env.fakes.set("claude", lines=claude_lines({"verdict": "B", "reason": reason}))
    detail = env.wait(env.start()["id"])
    assert detail["comparison"]["status"] == "done"
    assert spec_path.read_bytes() == before
    assert ok(env.client.get("/api/spec"))["spec"]["message"]["says"] != "PWNED"
