"""Revise (phase 8): author comments as findings, rewrite proposals with a fake `claude`,
apply with `\\aigen{...}` wrappers and the `% aigen-was:` line, the preamble, outstanding."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.repo import Repo
from app.revise import _find_collapsed, _wrap, count_outstanding, parse_rewrite
from app.runner import RunResult
from tests.conftest import adopt_intro
from tests.test_runs_api import Fakes, claude_lines

INTRO = "sections/intro.tex"
QUOTE = (
    "Warm pools trade memory for latency, and the trade is poor for the long tail of "
    "rarely invoked functions"
)
REWRITE_TEX = (
    "Warm pools buy latency with memory, a poor trade for rarely invoked "
    "functions~\\cite{shahrad2020serverless}."
)


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

    def comment(self, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "file": INTRO,
            "line_start": 14,
            "line_end": 15,
            "quote": QUOTE,
            "problem": "Too verbose; say it in one clause.",
            "why": "The reader already knows what a warm pool is.",
            "json_ref": self.paragraph_ids[1],
        }
        body.update(extra)
        return ok(self.client.post("/api/findings/comment", json=body))["finding"]

    def start(self, finding_id: str, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "finding_id": finding_id,
            "runner": "claude",
            "model": "sonnet",
            "thinking": "low",
        }
        body.update(extra)
        return ok(self.client.post("/api/revisions", json=body))["revision"]

    def wait(self, revision_id: str, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            detail = ok(self.client.get(f"/api/revisions/{revision_id}"))
            if detail["revision"]["status"] in ("done", "failed"):
                return detail
            if time.monotonic() > deadline:
                raise AssertionError(f"revision stuck: {detail['revision']}")
            time.sleep(0.05)


@pytest.fixture
def env(client: TestClient, initialized_repo: Path, tmp_path: Path) -> Iterator[Env]:
    fakes = Fakes(tmp_path, initialized_repo)
    fakes.set(
        "claude",
        lines=claude_lines(text=answer(REWRITE_TEX, "kept the citation"), structured=False),
    )
    yield Env(client, initialized_repo, fakes)
    fakes.release()


# --------------------------------------------------------------------------- comments


def test_comment_is_a_finding_of_an_author_run(env: Env) -> None:
    finding = env.comment()
    assert finding["category"] == "comment" and finding["severity"] == "major"
    assert finding["problem"] == "Too verbose; say it in one clause."
    assert finding["json_ref"] == env.paragraph_ids[1] and finding["level"] == "paragraph"
    # Anchored by the quote (which spans two source lines), state open.
    assert finding["anchor_state"] == "ok" and (
        finding["anchor_line_start"],
        finding["anchor_line_end"],
    ) == (14, 15)
    assert finding["state"] == "open"
    run = ok(env.client.get(f"/api/runs/{finding['run_id']}"))["run"]
    assert run["runner"] == "author" and run["pass"] == "comment" and run["status"] == "done"
    assert run["scope"]["kind"] == "selection" and run["scope"]["file"] == INTRO
    # Listed with the file's findings, like a model finding.
    listed = ok(env.client.get("/api/findings", params={"file": INTRO}))["findings"]
    assert any(f["id"] == finding["id"] for f in listed)
    # Errors: empty comment, missing file, bad range.
    assert (
        env.client.post(
            "/api/findings/comment",
            json={"file": INTRO, "line_start": 1, "line_end": 1, "quote": "x", "problem": " "},
        ).status_code
        == 400
    )
    assert (
        env.client.post(
            "/api/findings/comment",
            json={"file": "nope.tex", "line_start": 1, "line_end": 1, "quote": "x", "problem": "p"},
        ).status_code
        == 404
    )
    assert (
        env.client.post(
            "/api/findings/comment",
            json={"file": INTRO, "line_start": 5, "line_end": 2, "quote": "x", "problem": "p"},
        ).status_code
        == 400
    )
    # A quote that is not in the file: unverified, still stored.
    loose = env.comment(quote="not in the file at all", line_start=14, line_end=14)
    assert loose["anchor_state"] == "unverified"


# --------------------------------------------------------------------------- helpers


def test_find_collapsed_and_wrap() -> None:
    hay = "alive for a fixed window. Warm pools trade memory for latency, and the\ntrade is poor."
    hit = _find_collapsed(hay, "Warm pools trade memory  for latency, and the trade is poor")
    assert (
        hit is not None
        and hay[hit[0] : hit[1]] == "Warm pools trade memory for latency, and the\ntrade is poor"
    )
    assert _find_collapsed(hay, "absent") is None
    assert _wrap("One.") == "\\aigen{One.}"
    assert _wrap("One.\n\nTwo.\n") == "\\aigen{One.}\n\n\\aigen{Two.}"


def test_parse_rewrite_rules() -> None:
    def result(text: str) -> RunResult:
        return RunResult(status="ok", text=text)

    done = parse_rewrite(result(answer("A \\ref{fig:x} rewrite.", "note")))
    assert (
        done.status == "done"
        and done.data.text == "A \\ref{fig:x} rewrite."
        and done.data.notes == "note"
    )
    # An empty block means "drop the span".
    empty = parse_rewrite(result("```latex\n```\n"))
    assert empty.status == "done" and empty.data.text == ""
    assert "no fenced" in (parse_rewrite(result("plain")).error or "")
    assert "rejected" in (parse_rewrite(result(answer("\\section{X} y"))).error or "")


# --------------------------------------------------------------------------- lifecycle


def test_rewrite_quote_span_and_apply(env: Env, initialized_repo: Path) -> None:
    finding = env.comment()
    revision = env.start(finding["id"])
    assert revision["status"] in ("queued", "running") and revision["span"] == "quote"
    assert revision["original"].replace("\n", " ") == QUOTE
    assert (revision["line_start"], revision["line_end"]) == (14, 15)
    detail = env.wait(revision["id"])
    rev = detail["revision"]
    assert (
        rev["status"] == "done"
        and rev["text"] == REWRITE_TEX
        and rev["notes"] == "kept the citation"
    )
    assert detail["run"]["pass"] == "draft-rewrite" and detail["finding"]["id"] == finding["id"]
    # The prompt is the drafting stack with the comment and the span; no JSON schema.
    call = env.fakes.calls("claude")[-1]
    assert call["prompt"].startswith("# Drafting from the spec")
    assert (
        "Too verbose; say it in one clause." in call["prompt"]
        and QUOTE.split(",")[0] in call["prompt"]
    )
    assert "## Rewrite: the quoted span in paragraph " + env.paragraph_ids[1] in call["prompt"]
    assert "--json-schema" not in call["argv"]
    before = (initialized_repo / INTRO).read_text()
    assert "aigen" not in before and "aigen" not in (initialized_repo / "main.tex").read_text()

    # Apply with the author's edit: `% aigen-was:` line, the wrapper in place of the span only.
    edited = REWRITE_TEX.replace("a poor trade", "a bad trade")
    applied = ok(env.client.post(f"/api/revisions/{rev['id']}/apply", json={"text": edited}))
    after = (initialized_repo / INTRO).read_text()
    lines = after.split("\n")
    assert lines[13] == "% aigen-was: " + QUOTE
    assert lines[14].startswith("alive for a fixed keep-alive window. \\aigen{" + edited + "}")
    assert "\\aigen{" + edited + "}" in after and QUOTE.split(",")[0] not in after.replace(
        lines[13], ""
    )
    assert applied["file"] == INTRO and applied["line_start"] == 15 and applied["outstanding"] == 1
    # The rest of the file is intact: the other markers and the sentence after the span.
    for pid in env.paragraph_ids:
        assert f"% @p {pid}" in after
    assert "kept warm for ten minutes" in after
    # main.tex got the macro once, before \begin{document}, with xcolor (not loaded before).
    main = (initialized_repo / "main.tex").read_text()
    assert main.count("\\providecommand{\\aigen}") == 1 and "\\usepackage{xcolor}" in main
    assert main.index("\\providecommand{\\aigen}") < main.index("\\begin{document}")
    assert "% \\renewcommand{\\aigen}[1]{#1}" in main
    # The finding is addressed; outstanding counts the wrapper; a second apply is refused.
    assert (
        ok(env.client.get("/api/findings", params={"file": INTRO}))["findings"][0]["state"]
        == "addressed"
    )
    assert ok(env.client.get("/api/revisions/outstanding")) == {"count": 1, "by_file": {INTRO: 1}}
    assert env.client.post(f"/api/revisions/{rev['id']}/apply", json={}).status_code == 409
    # The paragraph's text changed under its marker: stale, to be re-read.
    assert (
        ok(env.client.get("/api/spec/status"))["nodes"][env.paragraph_ids[1]]["status"] == "stale"
    )


def test_rewrite_whole_paragraph_split_and_drop(env: Env, initialized_repo: Path) -> None:
    finding = env.comment(problem="Two ideas; split into two paragraphs.")
    env.fakes.set(
        "claude",
        lines=claude_lines(
            text=answer("First idea.\n\nSecond idea~\\cite{jonas2019cloud}."), structured=False
        ),
    )
    rev = env.wait(env.start(finding["id"], span="paragraph")["id"])["revision"]
    assert rev["span"] == "paragraph" and (rev["line_start"], rev["line_end"]) == (13, 16)
    assert rev["original"].startswith("Providers mitigate cold starts")
    applied = ok(env.client.post(f"/api/revisions/{rev['id']}/apply", json={}))
    after = (initialized_repo / INTRO).read_text().split("\n")
    assert after[12].startswith("% aigen-was: Providers mitigate cold starts")
    assert (
        after[13] == "\\aigen{First idea.}"
        and after[14] == ""
        and after[15] == "\\aigen{Second idea~\\cite{jonas2019cloud}.}"
    )
    assert applied["outstanding"] == 2
    # Dropping: an empty approved text removes the span and leaves only the was-line.
    finding2 = env.comment(
        line_start=19,
        line_end=19,
        quote="Snapshot restore is the main alternative.",
        problem="Not needed.",
        json_ref=env.paragraph_ids[2],
    )
    rev2 = env.wait(env.start(finding2["id"])["id"])["revision"]
    ok(env.client.post(f"/api/revisions/{rev2['id']}/apply", json={"text": ""}))
    text = (initialized_repo / INTRO).read_text()
    assert "% aigen-was: Snapshot restore is the main alternative." in text
    assert text.count("Snapshot restore is the main alternative.") == 1  # only in the was-line
    # The macro block is added once, whatever the number of applies.
    assert (initialized_repo / "main.tex").read_text().count("\\providecommand{\\aigen}") == 1


def test_apply_refuses_when_the_text_moved(env: Env, initialized_repo: Path) -> None:
    finding = env.comment()
    rev = env.wait(env.start(finding["id"])["id"])["revision"]
    path = initialized_repo / INTRO
    path.write_text(
        path.read_text().replace(
            "Warm pools trade memory for latency", "Warm pools swap memory for latency"
        )
    )
    res = env.client.post(f"/api/revisions/{rev['id']}/apply", json={})
    assert res.status_code in (400, 409) and (
        "changed" in res.json()["detail"] or "stale" in res.json()["detail"]
    )


def test_request_errors(env: Env) -> None:
    client = env.client
    assert client.post("/api/revisions", json={"finding_id": "nope"}).status_code == 404
    finding = env.comment()
    assert (
        client.post(
            "/api/revisions", json={"finding_id": finding["id"], "span": "page"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/revisions", json={"finding_id": finding["id"], "runner": "gpt"}
        ).status_code
        == 400
    )
    assert client.get("/api/revisions/nope").status_code == 404
    assert client.post("/api/revisions/nope/apply", json={}).status_code == 404
    env.fakes.set("claude", lines=claude_lines(text=answer("x"), structured=False), sleep=1.0)
    first = env.start(finding["id"])
    assert (
        client.post(
            "/api/revisions", json={"finding_id": finding["id"], "runner": "claude"}
        ).status_code
        == 409
    )
    assert client.post(f"/api/revisions/{first['id']}/apply", json={}).status_code == 409
    env.wait(first["id"])
    assert (
        ok(client.get("/api/revisions", params={"finding_id": finding["id"]}))["revisions"][0]["id"]
        == first["id"]
    )


def test_count_outstanding_ignores_comments(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    path = initialized_repo / INTRO
    path.write_text(path.read_text() + "\n\\aigen{a} % \\aigen{commented}\n\\aigen{b}\n")
    assert count_outstanding(repo).by_file == {INTRO: 2}
