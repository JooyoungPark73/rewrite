"""Drafting from the spec (phase 7) end to end with a fake `claude`: the prompt, the parse
rules, the draft lifecycle, apply (replace / insert) and the refusals."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import prompts
from app.drafts import parse_draft
from app.repo import Repo
from app.runner import RunResult
from tests.conftest import adopt_intro
from tests.test_runs_api import Fakes, claude_lines

INTRO = "sections/intro.tex"
DRAFT_TEX = (
    "Warm pools trade memory for latency, and the trade is poor for the long tail of "
    "rarely invoked functions: an instance kept warm for ten minutes and evicted unused costs "
    "memory and saves nothing~\\cite{shahrad2020serverless}."
)
# What the fake CLI answers: the fenced block plus a Notes line (the draft format).
DRAFT_ANSWER = f"```latex\n{DRAFT_TEX}\n```\nNotes: nothing left open.\n"


def answer(tex: str, notes: str | None = None) -> str:
    return f"```latex\n{tex}\n```\n" + (f"Notes: {notes}\n" if notes else "")


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


class Env:
    def __init__(self, client: TestClient, repo: Path, fakes: Fakes) -> None:
        self.client = client
        self.repo = repo
        self.fakes = fakes
        self.section_id, self.paragraph_ids = adopt_intro(client)

    def start(self, node_id: str | None = None, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "node_id": node_id or self.paragraph_ids[1],
            "runner": "claude",
            "model": "sonnet",
            "thinking": "low",
        }
        body.update(extra)
        return ok(self.client.post("/api/drafts", json=body))["draft"]

    def wait(self, draft_id: str, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            detail = ok(self.client.get(f"/api/drafts/{draft_id}"))
            if detail["draft"]["status"] in ("done", "failed"):
                return detail
            if time.monotonic() > deadline:
                raise AssertionError(f"draft stuck: {detail['draft']}")
            time.sleep(0.05)


@pytest.fixture
def env(client: TestClient, initialized_repo: Path, tmp_path: Path) -> Iterator[Env]:
    fakes = Fakes(tmp_path, initialized_repo)
    fakes.set("claude", lines=claude_lines(text=DRAFT_ANSWER, structured=False))
    yield Env(client, initialized_repo, fakes)
    fakes.release()


# --------------------------------------------------------------------------- prompt


def test_draft_prompt_is_its_own_stack(env: Env, initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    p2 = env.paragraph_ids[1]
    ok(
        env.client.post(
            "/api/spec/ops",
            json={
                "op": {
                    "type": "update",
                    "id": p2,
                    "says": "Warm pools are a poor trade for the long tail",
                    "fields": {"function": "Dismiss the standard fix"},
                }
            },
        )
    )
    c = ok(
        env.client.post(
            "/api/spec/ops",
            json={
                "op": {
                    "type": "add_claim",
                    "says": "Warm pools waste memory",
                    "fields": {"evidence": "Table 1", "scope": "the trace"},
                }
            },
        )
    )["new_id"]
    ok(
        env.client.post(
            "/api/spec/ops", json={"op": {"type": "link_claim", "id": p2, "claim_id": c}}
        )
    )
    assembled = prompts.assemble_draft_prompt(repo, p2, "FORMAT-TEXT")
    text = assembled.prompt
    # The drafting system prompt, not the review one.
    assert text.startswith("# Drafting from the spec")
    assert "Manuscript screening" not in text and "Problems only" not in text
    assert "## Draft: paragraph " + p2 in text
    assert "Warm pools are a poor trade for the long tail" in text
    assert "Dismiss the standard fix" in text
    assert f"`{c}`: Warm pools waste memory" in text and "scope: the trace" in text
    # The section's LaTeX, unnumbered, and the paragraph's current text.
    assert "Providers mitigate cold starts with warm pools" in text
    assert "   12 | " not in text
    assert "### The paragraph as it reads now" in text
    # Labels and bib keys the draft may use.
    assert "`sec:intro`" in text and "`shahrad2020serverless`" in text
    assert "FORMAT-TEXT" in text and "Output only JSON" not in text
    assert assembled.variant == "draft"
    # The writing style applies to drafts too.
    config = repo.load_config()
    config.paper.style = "systems"
    from app.config import save_config

    save_config(repo.config_path, config)
    styled = prompts.assemble_draft_prompt(repo, p2, "{}")
    assert "The field's standard of evidence" in styled.prompt
    # Not a paragraph: refused.
    with pytest.raises(prompts.PromptError, match="drafts target paragraphs"):
        prompts.assemble_draft_prompt(repo, env.section_id, "{}")


def test_parse_draft_rules() -> None:
    def result(text: str) -> RunResult:
        return RunResult(status="ok", text=text)

    done = parse_draft(
        result(answer("One paragraph with \\ref{fig:x} and \\textsc{Name}.", "a note"))
    )
    assert done.status == "done"
    assert (
        done.data.text == "One paragraph with \\ref{fig:x} and \\textsc{Name}."
    )  # backslashes intact
    assert done.data.notes == "a note"
    assert parse_draft(result(answer("Two\nlines."))).data.notes == ""
    # A plain ``` fence works too; a multi-line Notes is joined.
    assert (
        parse_draft(result("```\nText.\n```\nNotes: first\nsecond\n")).data.notes == "first second"
    )
    assert "no fenced" in (parse_draft(result("Here is the paragraph: Text.")).error or "")
    assert "2 fenced blocks" in (parse_draft(result(answer("a") + answer("b"))).error or "")
    assert "empty" in (parse_draft(result("```latex\n\n```\n")).error or "")
    for bad in ("\\section{New}\ntext", "% @p p-9\ntext", "\\begin{document}x"):
        assert "rejected" in (parse_draft(result(answer(bad))).error or ""), bad


# --------------------------------------------------------------------------- lifecycle


def test_draft_lifecycle_and_replace(env: Env, initialized_repo: Path) -> None:
    p2 = env.paragraph_ids[1]
    created = env.start(p2)
    assert created["status"] in ("queued", "running") and created["node_id"] == p2
    assert created["text"] is None and created["applied_at"] is None
    detail = env.wait(created["id"])
    draft, run = detail["draft"], detail["run"]
    assert (
        draft["status"] == "done"
        and draft["text"] == DRAFT_TEX
        and draft["notes"] == "nothing left open."
    )
    assert (
        run["pass"] == "draft-paragraph" and run["status"] == "done" and run["cached_from"] is None
    )
    # The run's prompt is the drafting prompt; it was sent as a normal run (schema on argv).
    call = env.fakes.calls("claude")[-1]
    assert call["prompt"].startswith("# Drafting from the spec")
    # Free text: no structured-output schema is forced on the CLI (JSON would mangle LaTeX).
    assert "--json-schema" not in call["argv"] and call["schema"] is None
    # Listed, newest first; the run list shows it too.
    assert ok(env.client.get("/api/drafts"))["drafts"][0]["id"] == created["id"]
    assert (
        ok(env.client.get("/api/drafts", params={"node_id": p2}))["drafts"][0]["id"]
        == created["id"]
    )
    # Nothing was written by the model call.
    before = (initialized_repo / INTRO).read_text()
    assert "drafted by" not in before

    # Apply: replace the paragraph under the marker with the author's edited text.
    edited = draft["text"].replace("saves nothing", "saves nothing at all")
    applied = ok(
        env.client.post(
            f"/api/drafts/{created['id']}/apply", json={"mode": "replace", "text": edited}
        )
    )
    after = (initialized_repo / INTRO).read_text()
    lines = after.split("\n")
    assert applied["file"] == INTRO and applied["draft"]["applied_mode"] == "replace"
    marker_line = lines.index(f"% @p {p2}") + 1
    assert lines[marker_line].startswith("% drafted by claude (sonnet), draft drf")
    assert lines[marker_line + 1] == edited
    assert "an instance that is" not in after  # the old paragraph is gone
    assert lines[applied["line_start"] - 1] == edited
    # The other paragraphs and markers are intact.
    for other in env.paragraph_ids:
        assert f"% @p {other}" in after
    # The node is stale now (its text changed): re-read applies to model text too.
    status = ok(env.client.get("/api/spec/status"))["nodes"][p2]
    assert status["status"] == "stale"
    # Applying twice is refused.
    again = env.client.post(f"/api/drafts/{created['id']}/apply", json={"mode": "replace"})
    assert again.status_code == 409 and "already applied" in again.json()["detail"]


def test_insert_for_a_planned_paragraph(env: Env, initialized_repo: Path) -> None:
    new = ok(
        env.client.post(
            "/api/spec/ops",
            json={
                "op": {
                    "type": "insert",
                    "parent_id": env.section_id,
                    "index": 4,
                    "level": "paragraph",
                    "says": "Closing remark",
                }
            },
        )
    )["new_id"]
    draft = env.wait(env.start(new)["id"])["draft"]
    # replace is refused for a node without a marker; insert needs a place.
    assert (
        env.client.post(f"/api/drafts/{draft['id']}/apply", json={"mode": "replace"}).status_code
        == 400
    )
    assert (
        env.client.post(f"/api/drafts/{draft['id']}/apply", json={"mode": "insert"}).status_code
        == 400
    )
    before = (initialized_repo / INTRO).read_text().split("\n")
    applied = ok(
        env.client.post(
            f"/api/drafts/{draft['id']}/apply",
            json={"mode": "insert", "file": INTRO, "line": len(before) + 1},
        )
    )
    after = (initialized_repo / INTRO).read_text().split("\n")
    i = after.index(f"% @p {new}")
    assert after[i + 1].startswith("% drafted by claude")
    assert after[i + 2] == DRAFT_TEX
    assert applied["line_start"] == i + 3 and applied["line_end"] == i + 3
    # The node is still planned until the frontend re-links it through adopt {id}.
    assert ok(env.client.get("/api/spec/status"))["nodes"][new]["status"] == "planned"
    linked = ok(
        env.client.post(
            "/api/spec/adopt",
            json={"kind": "paragraph", "id": new, "file": INTRO, "line_start": i + 3},
        )
    )
    assert linked["new_id"] == new
    assert ok(env.client.get("/api/spec/status"))["nodes"][new]["status"] == "linked"
    assert (initialized_repo / INTRO).read_text().count(f"% @p {new}") == 1


def planned_draft(env: Env) -> tuple[str, dict[str, Any]]:
    op = {"type": "insert", "parent_id": env.section_id, "index": 4, "level": "paragraph"}
    new = ok(env.client.post("/api/spec/ops", json={"op": op}))["new_id"]
    return new, env.wait(env.start(new)["id"])["draft"]


def test_insert_under_a_marker_keeps_the_marker_on_its_paragraph(
    env: Env, initialized_repo: Path
) -> None:
    # The cursor on the first line of a linked paragraph: the draft goes above that
    # paragraph's marker, not between the marker and its text.
    new, draft = planned_draft(env)
    target = env.paragraph_ids[1]
    before = (initialized_repo / INTRO).read_text().split("\n")
    first_text_line = before.index(f"% @p {target}") + 2
    ok(
        env.client.post(
            f"/api/drafts/{draft['id']}/apply",
            json={"mode": "insert", "file": INTRO, "line": first_text_line},
        )
    )
    after = (initialized_repo / INTRO).read_text().split("\n")
    i = after.index(f"% @p {target}")
    assert after[i + 1] == before[first_text_line - 1]
    assert after.index(f"% @p {new}") < i
    assert ok(env.client.get("/api/spec/status"))["nodes"][target]["status"] == "linked"


def test_insert_refuses_places_outside_the_document_body(env: Env) -> None:
    _, draft = planned_draft(env)
    url = f"/api/drafts/{draft['id']}/apply"
    for file, line in (("main.tex", 1), ("paper.json", 1), ("refs.bib", 1), ("../x.tex", 1)):
        response = env.client.post(url, json={"mode": "insert", "file": file, "line": line})
        assert response.status_code == 400, (file, response.text)


def test_replace_keeps_crlf_line_endings(env: Env, initialized_repo: Path) -> None:
    intro = initialized_repo / INTRO
    intro.write_bytes(intro.read_bytes().replace(b"\n", b"\r\n"))
    draft = env.wait(env.start(env.paragraph_ids[1])["id"])["draft"]
    ok(env.client.post(f"/api/drafts/{draft['id']}/apply", json={"mode": "replace"}))
    data = intro.read_bytes()
    assert DRAFT_TEX.encode() in data and data.count(b"\n") == data.count(b"\r\n")


def test_request_errors(env: Env) -> None:
    client = env.client
    assert client.post("/api/drafts", json={"node_id": "nope"}).status_code == 404
    assert client.post("/api/drafts", json={"node_id": env.section_id}).status_code == 400
    assert client.post("/api/drafts", json={"node_id": "message"}).status_code == 404
    assert (
        client.post(
            "/api/drafts", json={"node_id": env.paragraph_ids[0], "runner": "gpt"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/drafts", json={"node_id": env.paragraph_ids[0], "extra": 1}).status_code
        == 400
    )
    assert client.get("/api/drafts/nope").status_code == 404
    assert client.post("/api/drafts/nope/apply", json={"mode": "replace"}).status_code == 404
    # One draft per node at a time; a second node is fine.
    env.fakes.set("claude", lines=claude_lines(text=DRAFT_ANSWER, structured=False), sleep=1.0)
    first = env.start(env.paragraph_ids[0])
    dup = client.post("/api/drafts", json={"node_id": env.paragraph_ids[0], "runner": "claude"})
    assert dup.status_code == 409
    second = env.start(env.paragraph_ids[2])
    assert second["id"] != first["id"]
    # A draft that is not done cannot be applied.
    assert (
        client.post(f"/api/drafts/{first['id']}/apply", json={"mode": "replace"}).status_code == 409
    )
    env.wait(first["id"])
    env.wait(second["id"])


def test_failed_answer_keeps_the_raw_output(env: Env) -> None:
    env.fakes.set("claude", lines=claude_lines(text=answer("\\section{Nope}\nx"), structured=False))
    detail = env.wait(env.start()["id"])
    assert detail["draft"]["status"] == "failed" and "rejected" in detail["draft"]["error"]
    assert detail["run"]["status"] == "failed"
    raw = ok(env.client.get(f"/api/runs/{detail['run']['id']}"))["raw_output"]
    assert "Nope" in raw
