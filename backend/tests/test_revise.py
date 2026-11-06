"""Revise (phase 8): author comments as findings, rewrite proposals with a fake `claude`,
apply with `\\aigen{...}` wrappers and the `% aigen-was:` line, the preamble, outstanding."""

from __future__ import annotations

import json
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
from tests.test_runs_api import Fakes, claude_lines, start, wait_run

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
    # A comment on the last line must not comment out the closing brace.
    assert _wrap("One.%") == "\\aigen{One.%\n}"
    assert _wrap("One \\% more.") == "\\aigen{One \\% more.}"


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


def test_apply_in_main_tex_reports_lines_after_the_macro_block(
    env: Env, initialized_repo: Path
) -> None:
    # The abstract is in main.tex, below \begin{document}: the \aigen block inserted above it
    # pushes the rewrite down, and the reported lines follow.
    quote = "Serverless platforms hide cold-start latency by keeping idle instances warm"
    finding = env.comment(file="main.tex", line_start=22, line_end=23, quote=quote, json_ref=None)
    rev = env.wait(env.start(finding["id"])["id"])["revision"]
    applied = ok(env.client.post(f"/api/revisions/{rev['id']}/apply", json={}))
    lines = (initialized_repo / "main.tex").read_text().split("\n")
    assert lines[applied["line_start"] - 1].startswith("\\aigen{")
    assert lines[applied["line_start"] - 2].startswith("% aigen-was: Serverless platforms")


def test_rewrite_refused_when_the_quote_is_only_in_a_was_line(
    env: Env, initialized_repo: Path
) -> None:
    # After a whole-paragraph rewrite, another comment on that paragraph re-anchors onto the
    # `% aigen-was:` line (the old wording lives on there); rewriting it there would change
    # nothing in the document.
    first = env.comment()
    other = env.comment(quote="the long tail of", problem="Say which functions.")
    rev = env.wait(env.start(first["id"], span="paragraph")["id"])["revision"]
    ok(env.client.post(f"/api/revisions/{rev['id']}/apply", json={}))
    res = env.client.post("/api/revisions", json={"finding_id": other["id"], "runner": "claude"})
    assert res.status_code == 400 and "comment" in res.json()["detail"]


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


# --------------------------------------------------------------------------- 2026-11-05


def codex_text(text: str) -> list[str]:
    """Fake codex events whose final message is free text (a rewrite answer)."""
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": 900, "output_tokens": 40}},
    ]
    return [json.dumps(event) for event in events]


def test_edit_comment_changes_only_its_text(env: Env) -> None:
    finding = env.comment()
    url = f"/api/findings/{finding['id']}/comment"
    edited = ok(env.client.patch(url, json={"problem": "  Cut the clause. ", "why": " Long. "}))
    edited = edited["finding"]
    assert (edited["problem"], edited["why"]) == ("Cut the clause.", "Long.")
    for key in ("id", "file", "quote", "anchor_line_start", "anchor_line_end", "state", "json_ref"):
        assert edited[key] == finding[key], key
    listed = ok(env.client.get("/api/findings", params={"file": INTRO}))["findings"]
    assert next(f for f in listed if f["id"] == finding["id"])["problem"] == "Cut the clause."
    # The next rewrite sends the new text.
    assert env.wait(env.start(finding["id"])["id"])["revision"]["status"] == "done"
    prompt = env.fakes.calls("claude")[-1]["prompt"]
    assert "Cut the clause." in prompt and "Too verbose; say it in one clause." not in prompt
    # Errors: empty text, a key that is not the comment's text, unknown finding.
    assert env.client.patch(url, json={"problem": "  "}).status_code == 400
    assert env.client.patch(url, json={"problem": "x", "quote": "y"}).status_code == 400
    assert env.client.patch("/api/findings/nope/comment", json={"problem": "x"}).status_code == 404


def test_model_findings_cannot_be_edited(env: Env) -> None:
    env.fakes.set("claude", lines=claude_lines())
    detail = wait_run(env.client, start(env.client, env.section_id)["id"])
    assert detail["run"]["status"] == "done" and detail["findings"]
    model_finding = detail["findings"][0]
    res = env.client.patch(f"/api/findings/{model_finding['id']}/comment", json={"problem": "x"})
    assert res.status_code == 400 and "model finding" in res.json()["detail"]
    after = ok(env.client.get("/api/findings", params={"run_id": detail["run"]["id"]}))
    assert after["findings"][0]["problem"] == model_finding["problem"]


def test_rewrite_defaults_to_codex_gpt_6_astra_medium(env: Env) -> None:
    env.fakes.set("codex", lines=codex_text(answer(REWRITE_TEX)))
    finding = env.comment()
    revision = ok(env.client.post("/api/revisions", json={"finding_id": finding["id"]}))
    detail = env.wait(revision["revision"]["id"])
    run = detail["run"]
    assert (run["runner"], run["model"], run["thinking"]) == ("codex", "gpt-6-astra", "medium")
    assert detail["revision"]["status"] == "done" and detail["revision"]["text"] == REWRITE_TEX
    argv = env.fakes.calls("codex")[-1]["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-6-astra"
    assert "model_reasoning_effort=medium" in argv
    # The dialog's "default (...)" labels come from GET /api/runners.
    defaults = ok(env.client.get("/api/runners"))["defaults"]
    assert defaults["rewrite"] == {"runner": "codex", "model": "gpt-6-astra", "thinking": "medium"}
    assert defaults["sentence"]["runner"] == "claude"  # review passes keep their defaults
    # Another runner without a model: that CLI's own default model, as for review passes.
    other = env.wait(env.start(finding["id"], model=None, thinking=None)["id"])["run"]
    assert (other["runner"], other["model"], other["thinking"]) == ("claude", None, "medium")


# --------------------------------------------------------------------------- across paragraphs
# Decided 2026-11-06: a comment that spans paragraphs rewrites all of them. The `% @p` lines
# inside the span go to the model, come back unchanged and stay in place on apply.

BRIDGE = "No bridge from warm pools to snapshots; add one."
END_OF_P1 = "costs memory and saves nothing."
START_OF_P2 = "Snapshot restore is the main alternative."


def intro_lines(repo: Path) -> list[str]:
    return (repo / INTRO).read_text().split("\n")


def line_of(lines: list[str], text: str) -> int:
    """1-based number of the first line containing `text`."""
    return next(i for i, line in enumerate(lines, 1) if text in line)


def cross_comment(env: Env) -> dict[str, Any]:
    """A comment from the end of the second intro paragraph to the start of the third,
    across the third one's `% @p` line (as the editor's selection sends it)."""
    lines = intro_lines(env.repo)
    start, end = line_of(lines, END_OF_P1), line_of(lines, START_OF_P2)
    quote = f"{END_OF_P1}\n\n% @p {env.paragraph_ids[2]}\n{START_OF_P2}"
    assert quote in "\n".join(lines[start - 1 : end])
    return env.comment(line_start=start, line_end=end, quote=quote, problem=BRIDGE)


def marker_links(client: TestClient) -> list[str | None]:
    return [
        p["marker_id"]
        for p in ok(client.get("/api/spec/skeleton"))["paragraphs"]
        if p["file"] == INTRO
    ]


def test_whole_paragraphs_across_a_marker(env: Env, initialized_repo: Path) -> None:
    p1, p2 = env.paragraph_ids[1], env.paragraph_ids[2]
    before = intro_lines(initialized_repo)
    first = line_of(before, "Providers mitigate cold starts")
    last = line_of(before, "time, and these page faults dominate restore latency.")
    original = "\n".join(before[first - 1 : last])
    proposal = original.replace(
        END_OF_P1, "costs memory and saves nothing, a waste that\nsnapshots avoid."
    ).replace(START_OF_P2, "Snapshot restore avoids it.")
    env.fakes.set("claude", lines=claude_lines(text=answer(proposal), structured=False))
    finding = cross_comment(env)
    assert finding["anchor_state"] == "ok"
    rev = env.wait(env.start(finding["id"], span="paragraph")["id"])["revision"]
    assert rev["status"] == "done", rev
    assert (rev["line_start"], rev["line_end"]) == (first, last)
    assert rev["original"] == original and f"% @p {p2}" in rev["original"]
    prompt = env.fakes.calls("claude")[-1]["prompt"]
    assert "## Rewrite: the whole paragraphs across 2 paragraphs" in prompt
    assert f"(`% @p {p2}`)" in prompt and "keep every one of them exactly as it is" in prompt
    assert "no `% @p` line" not in prompt

    applied = ok(env.client.post(f"/api/revisions/{rev['id']}/apply", json={}))
    after = intro_lines(initialized_repo)
    # Each paragraph: its marker, its own `% aigen-was:` line, its own wrapper.
    at = first - 1
    assert after[at - 1] == f"% @p {p1}"
    assert after[at].startswith("% aigen-was: Providers mitigate cold starts")
    assert after[at].endswith(END_OF_P1)
    assert after[at + 1].startswith("\\aigen{Providers mitigate cold starts")
    assert after[at + 5] == "snapshots avoid.}" and after[at + 6] == ""
    assert after[at + 7] == f"% @p {p2}"
    assert after[at + 8].startswith("% aigen-was: " + START_OF_P2)
    assert after[at + 9].startswith("\\aigen{Snapshot restore avoids it.")
    assert applied["outstanding"] == 2 and applied["line_start"] == first + 1
    # Every paragraph is still linked to its node, in order; the two rewritten ones are stale.
    assert marker_links(env.client) == env.paragraph_ids
    status = ok(env.client.get("/api/spec/status"))["nodes"]
    assert status[p1]["status"] == "stale" and status[p2]["status"] == "stale"
    assert status[env.paragraph_ids[3]]["status"] != "stale"


def test_quote_across_a_marker_rewrites_both_sides(env: Env, initialized_repo: Path) -> None:
    p2 = env.paragraph_ids[2]
    proposal = (
        "costs memory and saves nothing, the waste snapshots avoid.\n\n"
        f"% @p {p2}\nSnapshot restore is the main alternative to warm pools."
    )
    env.fakes.set("claude", lines=claude_lines(text=answer(proposal), structured=False))
    finding = cross_comment(env)
    rev = env.wait(env.start(finding["id"])["id"])["revision"]
    assert rev["status"] == "done", rev
    assert rev["original"] == f"{END_OF_P1}\n\n% @p {p2}\n{START_OF_P2}"
    prompt = env.fakes.calls("claude")[-1]["prompt"]
    assert "## Rewrite: the quoted span across 2 paragraphs" in prompt
    start = finding["anchor_line_start"]
    ok(env.client.post(f"/api/revisions/{rev['id']}/apply", json={}))
    after = intro_lines(initialized_repo)
    assert after[start - 1] == "% aigen-was: " + END_OF_P1
    assert after[start] == (
        "kept warm for ten minutes and then evicted unused "
        "\\aigen{costs memory and saves nothing, the waste snapshots avoid.}"
    )
    assert after[start + 1] == "" and after[start + 2] == f"% @p {p2}"
    assert after[start + 3] == "% aigen-was: " + START_OF_P2
    assert after[start + 4] == (
        "\\aigen{Snapshot restore is the main alternative to warm pools.} The platform "
        "checkpoints an initialized"
    )
    assert marker_links(env.client) == env.paragraph_ids


def test_unchanged_paragraph_is_left_alone(env: Env, initialized_repo: Path) -> None:
    # Only the second paragraph's part changes: the first keeps its text, with no wrapper and
    # no `% aigen-was:` line.
    p2 = env.paragraph_ids[2]
    proposal = f"{END_OF_P1}\n\n% @p {p2}\nSnapshots avoid that waste."
    env.fakes.set("claude", lines=claude_lines(text=answer(proposal), structured=False))
    finding = cross_comment(env)
    rev = env.wait(env.start(finding["id"])["id"])["revision"]
    start = finding["anchor_line_start"]
    applied = ok(env.client.post(f"/api/revisions/{rev['id']}/apply", json={}))
    after = intro_lines(initialized_repo)
    assert after[start - 1].endswith("evicted unused " + END_OF_P1)
    assert after[start + 1] == f"% @p {p2}"
    assert after[start + 2] == "% aigen-was: " + START_OF_P2
    assert after[start + 3].startswith("\\aigen{Snapshots avoid that waste.} The platform")
    assert applied["outstanding"] == 1 and applied["line_start"] == start


def test_markers_must_come_back_unchanged(env: Env, initialized_repo: Path) -> None:
    p2 = env.paragraph_ids[2]
    # The model merged the two parts and dropped the marker: the run fails, nothing to apply.
    env.fakes.set(
        "claude",
        lines=claude_lines(text=answer("costs memory; snapshots avoid that."), structured=False),
    )
    finding = cross_comment(env)
    failed = env.wait(env.start(finding["id"])["id"])["revision"]
    assert failed["status"] == "failed" and f"`% @p {p2}`" in failed["error"]
    # A good proposal for the whole paragraphs, then edits that break it are refused on apply;
    # the file is untouched.
    good = f"First.\n\n% @p {p2}\nSecond."
    env.fakes.set("claude", lines=claude_lines(text=answer(good), structured=False))
    rev = env.wait(env.start(finding["id"], span="paragraph")["id"])["revision"]
    assert rev["status"] == "done"
    before = (initialized_repo / INTRO).read_text()
    for text, words in [
        ("Merged text.", "keeps their `% @p` lines"),
        (f"First.\n\n% @p {p2}\n", f"under `% @p {p2}` empty"),
        (f"% @p {p2}\nSecond.", f"before `% @p {p2}` empty"),
        ("First.\n\n% @p other\nSecond.", "this text has `% @p other`"),
        (rev["original"], "nothing to write"),
    ]:
        res = env.client.post(f"/api/revisions/{rev['id']}/apply", json={"text": text})
        assert res.status_code == 400 and words in res.json()["detail"], (text, res.text)
    assert (initialized_repo / INTRO).read_text() == before
    # A quote that starts and ends inside the paragraphs may empty either part: each
    # paragraph keeps its text outside the quote.
    env.fakes.set(
        "claude", lines=claude_lines(text=answer(f"% @p {p2}\nBridge."), structured=False)
    )
    rev = env.wait(env.start(finding["id"])["id"])["revision"]
    assert rev["status"] == "done", rev
    ok(env.client.post(f"/api/revisions/{rev['id']}/apply", json={}))
    after = (initialized_repo / INTRO).read_text()
    assert "evicted unused \n\n% @p" in after and "\\aigen{Bridge.} The platform" in after
    assert marker_links(env.client) == env.paragraph_ids


def test_no_marker_may_appear_in_a_single_paragraph_rewrite(env: Env) -> None:
    finding = env.comment()
    rev = env.wait(env.start(finding["id"])["id"])["revision"]
    res = env.client.post(
        f"/api/revisions/{rev['id']}/apply", json={"text": "One.\n\n% @p p-new\nTwo."}
    )
    assert res.status_code == 400 and "from the Spec tab" in res.json()["detail"]


def test_span_across_a_heading_is_refused(env: Env, initialized_repo: Path) -> None:
    path = initialized_repo / INTRO
    path.write_text(
        path.read_text().rstrip("\n") + "\n\n\\subsection{Scope}\nWe study FaaS only.\n"
    )
    lines = intro_lines(initialized_repo)
    start, end = line_of(lines, "in Section~\\ref{sec:results}."), line_of(lines, "We study FaaS")
    finding = env.comment(
        line_start=start,
        line_end=end,
        quote="in Section~\\ref{sec:results}.\n\n\\subsection{Scope}\nWe study FaaS only.",
        json_ref=env.paragraph_ids[3],
    )
    for span in ("quote", "paragraph"):
        res = env.client.post(
            "/api/revisions", json={"finding_id": finding["id"], "span": span, "runner": "claude"}
        )
        assert res.status_code == 400 and "crosses the heading" in res.json()["detail"], res.text


def test_older_template_copy_refuses_spans_across_paragraphs(
    env: Env, initialized_repo: Path
) -> None:
    # The paper's copy of the template is what runs. A copy from before 2026-11-06 cannot ask
    # the model to keep the `% @p` lines: within one paragraph it works, across paragraphs
    # the request is refused with the file named, and nothing is read from the app instead.
    copy = initialized_repo / ".rewrite" / "prompts" / "drafts" / "rewrite.md"
    copy.write_text(
        "---\nname: draft-rewrite\nkind: draft\nlevel: sentence\n---\nMine: {{ original }}\n"
    )
    res = env.client.post(
        "/api/revisions", json={"finding_id": cross_comment(env)["id"], "runner": "claude"}
    )
    assert res.status_code == 400 and "predates rewrites across paragraphs" in res.json()["detail"]
    assert ".rewrite/prompts/drafts/rewrite.md" in res.json()["detail"]
    single = env.comment()
    assert env.wait(env.start(single["id"])["id"])["revision"]["status"] == "done"
    assert env.fakes.calls("claude")[-1]["prompt"].count("Mine: ") == 1


def test_wrap_keeps_comments_and_displays_outside() -> None:
    assert _wrap("% says: x\nText.") == "% says: x\n\\aigen{Text.}"
    figure = "\\begin{figure}\n\\includegraphics{a}\n\\end{figure}"
    assert (
        _wrap(f"Text.\n\n{figure}\n\nMore.") == f"\\aigen{{Text.}}\n\n{figure}\n\n\\aigen{{More.}}"
    )
    assert _wrap("% only a comment") == "% only a comment"
