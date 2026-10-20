"""Spec editor HTTP API (phase-3 brief "API contract"), on a tmp copy of the sample paper."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.spec import empty_spec, serialize
from tests.test_spec_model import sample_spec

INTRO = "sections/intro.tex"


def drain(client: TestClient) -> list[dict[str, Any]]:
    """Events published since the last drain (the subscriber is created on first use)."""
    state = client.app.state.pb  # type: ignore[attr-defined]
    queue = getattr(state, "_test_queue", None)
    if queue is None:
        queue = state._test_queue = state.bus.subscribe()
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def op(client: TestClient, **body: Any) -> httpx.Response:
    return client.post("/api/spec/ops", json={"op": body})


def ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


def adopt_section(client: TestClient, file: str, line: int = 1) -> str:
    return ok(client.post("/api/spec/adopt", json={"kind": "section", "file": file, "line": line}))[
        "new_id"
    ]


def adopt_paragraph(client: TestClient, file: str, line_start: int, **kw: Any) -> dict[str, Any]:
    body = {"kind": "paragraph", "file": file, "line_start": line_start, **kw}
    return ok(client.post("/api/spec/adopt", json=body))


def status(client: TestClient) -> dict[str, dict[str, Any]]:
    return ok(client.get("/api/spec/status"))["nodes"]


def edit_tex(client: TestClient, path: str, old: str, new: str) -> None:
    content = ok(client.get("/api/file", params={"path": path}))["content"]
    assert content.count(old) == 1, old
    ok(client.put("/api/file", json={"path": path, "content": content.replace(old, new)}))


def children(spec: dict[str, Any], node_id: str | None = None) -> list[str]:
    parent = spec["tree"]
    if node_id is not None:
        parent = next(s for s in spec["tree"]["children"] if s["id"] == node_id)
    return [child["id"] for child in parent["children"]]


# --------------------------------------------------------------------------- GET / PUT


def test_get_spec_after_init(client: TestClient, initialized_repo: Path) -> None:
    body = ok(client.get("/api/spec"))
    assert body == {
        "spec": empty_spec().model_dump(mode="json"),
        "raw": (initialized_repo / "paper.json").read_text(),
        "errors": [],
    }


def test_get_spec_never_500s(client: TestClient, initialized_repo: Path) -> None:
    path = initialized_repo / "paper.json"
    path.write_text('{\n  "version": 1,\n  nope\n}\n')
    body = ok(client.get("/api/spec"))
    assert body["spec"] is None and body["raw"].startswith('{\n  "version"')
    assert body["errors"][0]["line"] == 3 and "invalid JSON" in body["errors"][0]["message"]

    path.write_text(json.dumps({"version": 1, "message": {}, "claims": [], "tree": {}}))
    body = ok(client.get("/api/spec"))
    assert body["spec"] is None and {"message.id", "tree.level"} <= {
        e["path"] for e in body["errors"]
    }

    path.write_bytes(b"\xff\xfe")
    assert ok(client.get("/api/spec"))["spec"] is None

    path.unlink()
    path.mkdir()
    body = ok(client.get("/api/spec"))
    assert body == {"spec": None, "raw": "", "errors": [body["errors"][0]]}
    path.rmdir()
    assert "not found" in ok(client.get("/api/spec"))["errors"][0]["message"]


def test_put_spec(client: TestClient, initialized_repo: Path) -> None:
    drain(client)
    spec = sample_spec()
    body = ok(client.put("/api/spec", json={"spec": spec.model_dump(mode="json")}))
    assert body["spec"] == spec.model_dump(mode="json")
    assert body["raw"] == serialize(spec) == (initialized_repo / "paper.json").read_text()
    assert drain(client) == [{"type": "spec.changed"}]

    mtime = (initialized_repo / "paper.json").stat().st_mtime_ns
    ok(client.put("/api/spec", json={"spec": spec.model_dump(mode="json")}))
    assert (initialized_repo / "paper.json").stat().st_mtime_ns == mtime  # unchanged: no write
    assert drain(client) == []


def test_put_spec_invalid_is_422(client: TestClient, initialized_repo: Path) -> None:
    before = (initialized_repo / "paper.json").read_text()
    bad = sample_spec().model_dump(mode="json")
    bad["tree"]["children"][0]["children"][0]["claims"] = ["c9"]
    response = client.put("/api/spec", json={"spec": bad})
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], str) and "unknown claim 'c9'" in body["detail"]
    assert body["errors"] == [
        {
            "path": "tree.children[0].children[0].claims[0]",
            "message": "unknown claim 'c9'",
            "line": None,
            "column": None,
        }
    ]
    response = client.put("/api/spec", json={"spec": [1, 2]})
    assert response.status_code == 422 and response.json()["errors"][0]["path"] == ""
    assert (initialized_repo / "paper.json").read_text() == before


def test_put_raw_round_trip(client: TestClient, initialized_repo: Path) -> None:
    spec = sample_spec()
    ok(client.put("/api/spec", json={"spec": spec.model_dump(mode="json")}))
    raw = ok(client.get("/api/spec"))["raw"]
    before = (initialized_repo / "paper.json").read_bytes()
    body = ok(client.put("/api/spec/raw", json={"raw": raw}))
    assert body["raw"] == raw and (initialized_repo / "paper.json").read_bytes() == before

    # any formatting is accepted and written back canonically
    reformatted = json.dumps(json.loads(raw), indent=4)
    body = ok(client.put("/api/spec/raw", json={"raw": reformatted}))
    assert body["raw"] == raw and (initialized_repo / "paper.json").read_bytes() == before

    response = client.put(
        "/api/spec/raw", json={"raw": raw.replace('"version": 1', '"version": 2')}
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["path"] == "version"
    response = client.put("/api/spec/raw", json={"raw": raw[:-5]})
    assert response.status_code == 422 and response.json()["errors"][0]["line"] is not None
    assert (initialized_repo / "paper.json").read_bytes() == before


# --------------------------------------------------------------------------- ops


def test_ops_round_trip(client: TestClient, initialized_repo: Path) -> None:
    drain(client)
    body = ok(op(client, type="insert", parent_id="message", index=0, level="section"))
    assert body["new_id"] == "section-1" and children(body["spec"]) == ["section-1"]
    assert body["raw"] == (initialized_repo / "paper.json").read_text()
    body = ok(op(client, type="insert", parent_id="section-1", index=0, level="paragraph"))
    assert body["new_id"] == "p-1"
    body = ok(op(client, type="update", id="p-1", says="Cold starts hurt."))
    assert body["new_id"] is None
    body = ok(op(client, type="lift_to_claim", id="p-1"))
    assert body["new_id"] == "c1" and body["spec"]["claims"][0]["says"] == "Cold starts hurt."
    assert [e["type"] for e in drain(client)] == ["spec.changed"] * 4


def test_op_errors(client: TestClient, initialized_repo: Path) -> None:
    ok(client.put("/api/spec", json={"spec": sample_spec().model_dump(mode="json")}))
    before = (initialized_repo / "paper.json").read_text()

    def error(response: httpx.Response, code: int) -> str:
        assert response.status_code == code, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        return detail

    assert "parent_id" in error(op(client, type="insert", index=0, level="section"), 400)
    assert "op" in error(op(client, type="explode", id="p-1"), 400)
    assert "say" in error(op(client, type="update", id="p-1", say="typo"), 400)
    assert "op" in error(client.post("/api/spec/ops", json={"nope": 1}), 400)
    error(op(client, type="update", id="nope", says="x"), 404)
    assert "own subtree" in error(
        op(client, type="move", id="introduction-1", parent_id="p-1", index=0), 400
    )
    assert "has paragraphs" in error(
        op(client, type="set_level", id="introduction-1", level="paragraph"), 400
    )
    response = op(client, type="update", id="p-1", fields={"colour": "red"})
    assert error(response, 422).startswith("invalid spec:")
    assert response.json()["errors"][0]["path"] == "tree.children[0].children[0].fields.colour"
    assert (initialized_repo / "paper.json").read_text() == before

    (initialized_repo / "paper.json").write_text("{")
    response = op(client, type="update", id="p-1", says="x")
    assert "Raw JSON" in error(response, 409) and response.json()["errors"]
    assert (initialized_repo / "paper.json").read_text() == "{"


async def test_concurrent_ops_are_serialised(initialized_repo: Path) -> None:
    app = create_app(initialized_repo, watch=False, frontend_dist=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        await c.post(
            "/api/spec/ops",
            json={"op": {"type": "insert", "parent_id": "message", "index": 0, "level": "section"}},
        )
        insert = {"type": "insert", "parent_id": "section-1", "index": 0, "level": "paragraph"}
        responses = await asyncio.gather(
            *(c.post("/api/spec/ops", json={"op": insert}) for _ in range(5))
        )
    assert [r.status_code for r in responses] == [200] * 5
    assert sorted(r.json()["new_id"] for r in responses) == [f"p-{n}" for n in range(1, 6)]
    final = json.loads((initialized_repo / "paper.json").read_text())
    assert len(final["tree"]["children"][0]["children"]) == 5


def test_post_endpoints_refuse_foreign_origins(client: TestClient) -> None:
    insert = {"op": {"type": "insert", "parent_id": "message", "index": 0, "level": "section"}}
    evil = {"origin": "http://evil.example"}
    assert client.post("/api/spec/ops", json=insert, headers=evil).status_code == 403
    adopt = {"kind": "section", "file": INTRO, "line": 1}
    assert client.post("/api/spec/adopt", json=adopt, headers=evil).status_code == 403
    assert client.post("/api/spec/nodes/x/reconcile", headers=evil).status_code == 403
    local = {"origin": "http://localhost:5173"}
    assert client.post("/api/spec/ops", json=insert, headers=local).status_code == 200


def test_spec_endpoints_need_a_repo(bare_client: TestClient) -> None:
    for method, url in [
        ("GET", "/api/spec"),
        ("PUT", "/api/spec/raw"),
        ("POST", "/api/spec/ops"),
        ("GET", "/api/spec/status"),
        ("GET", "/api/spec/skeleton"),
        ("POST", "/api/spec/adopt"),
        ("POST", "/api/spec/nodes/x/reconcile"),
    ]:
        assert bare_client.request(method, url, json={}).status_code == 409, url


# --------------------------------------------------------------------------- skeleton


def test_skeleton_endpoint(client: TestClient) -> None:
    body = ok(client.get("/api/spec/skeleton"))
    assert len(body["sections"]) == 4 and len(body["paragraphs"]) == 13
    assert body["sections"][0] == {
        "file": INTRO,
        "line": 1,
        "kind": "section",
        "heading": "Introduction",
        "label": "sec:intro",
        "adopted_id": None,
    }
    assert body["paragraphs"][0] == {
        "file": "main.tex",
        "line_start": 22,
        "line_end": 28,
        "preview": (
            "Serverless platforms hide cold-start latency by keeping idle instances warm, but"
        ),
        "section_heading": None,
        "marker_id": None,
    }
    section_id = adopt_section(client, INTRO)
    adopt_paragraph(client, INTRO, 11)
    body = ok(client.get("/api/spec/skeleton"))
    assert body["sections"][0]["adopted_id"] == section_id
    assert [p["marker_id"] for p in body["paragraphs"]][:4] == [None, None, "p-1", None]
    assert body["paragraphs"][2]["line_start"] == 12  # shifted by the marker line


def test_skeleton_works_with_a_broken_spec(client: TestClient, initialized_repo: Path) -> None:
    (initialized_repo / "paper.json").write_text("not json")
    body = ok(client.get("/api/spec/skeleton"))
    assert len(body["sections"]) == 4 and body["sections"][0]["adopted_id"] is None


# --------------------------------------------------------------------------- adopt


def test_adopt_sections_in_document_order(client: TestClient, initialized_repo: Path) -> None:
    assert adopt_section(client, "sections/results.tex") == "evaluation-1"
    assert adopt_section(client, INTRO) == "introduction-1"
    assert adopt_section(client, "sections/method.tex") == "design-1"
    body = ok(
        client.post(
            "/api/spec/adopt",
            json={
                "kind": "section",
                "file": "./sections/conclusion.tex",
                "line": 1,
                "says": "Wrap up.",
            },
        )
    )
    assert body["new_id"] == "conclusion-1"
    assert (body["file"], body["line"]) == ("sections/conclusion.tex", 1)
    spec = body["spec"]
    assert children(spec) == ["introduction-1", "design-1", "evaluation-1", "conclusion-1"]
    intro = spec["tree"]["children"][0]
    assert intro["says"] == "" and intro["fields"] == {"figure_story": ""}
    assert intro["anchor"] == {"file": INTRO, "marker": "\\section{Introduction}"}
    assert spec["tree"]["children"][3]["says"] == "Wrap up."
    assert body["raw"] == (initialized_repo / "paper.json").read_text()

    def refused(file: str, line: int, code: int) -> str:
        response = client.post(
            "/api/spec/adopt", json={"kind": "section", "file": file, "line": line}
        )
        assert response.status_code == code, response.text
        return response.json()["detail"]

    assert "already adopted as 'introduction-1'" in refused(INTRO, 1, 409)
    assert "no \\section" in refused(INTRO, 2, 400)
    assert "not part of the document" in refused("refs.bib", 1, 400)
    assert refused("../outside.tex", 1, 400)


def test_adopt_paragraphs(client: TestClient, initialized_repo: Path) -> None:
    refused = client.post(
        "/api/spec/adopt", json={"kind": "paragraph", "file": INTRO, "line_start": 4}
    )
    assert refused.status_code == 400 and "adopt a section first" in refused.json()["detail"]
    adopt_section(client, INTRO)
    drain(client)

    first = adopt_paragraph(client, INTRO, 16, says="Snapshots are the alternative.")
    assert (first["new_id"], first["file"], first["line"]) == ("p-1", INTRO, 16)
    events = drain(client)
    assert [e["type"] for e in events] == ["file.changed", "spec.changed"]
    assert events[0]["path"] == INTRO and events[0]["change"] == "modified"

    second = adopt_paragraph(client, INTRO, 6)  # any line of the paragraph (4-9) works
    assert (second["new_id"], second["line"]) == ("p-2", 4)
    spec = second["spec"]
    assert children(spec, "introduction-1") == ["p-2", "p-1"]  # document order
    p1 = spec["tree"]["children"][0]["children"][1]
    assert p1 == {
        "anchor": {"file": INTRO, "marker": "@p p-1"},
        "children": [],
        "claims": [],
        "fields": {"function": ""},
        "id": "p-1",
        "level": "paragraph",
        "notes": "",
        "says": "Snapshots are the alternative.",
    }
    lines = (initialized_repo / INTRO).read_text().splitlines()
    assert lines[3:5] == [
        "% @p p-2",
        "Function-as-a-Service (FaaS) platforms bill tenants per invocation and scale",
    ]
    assert lines[16:18] == [
        "% @p p-1",
        "Snapshot restore is the main alternative. The platform checkpoints an initialized",
    ]
    assert sum(line.startswith("% @p") for line in lines) == 2

    # the same paragraph again: 409 without an id, a no-op with its own id
    again = client.post(
        "/api/spec/adopt", json={"kind": "paragraph", "file": INTRO, "line_start": 5}
    )
    assert again.status_code == 409 and "already adopted as 'p-2'" in again.json()["detail"]
    text = (initialized_repo / INTRO).read_text()
    drain(client)
    same = adopt_paragraph(client, INTRO, 5, id="p-2")
    assert (same["new_id"], same["line"]) == ("p-2", 4)
    assert (initialized_repo / INTRO).read_text() == text and drain(client) == []


def test_adopt_paragraph_parents(client: TestClient) -> None:
    adopt_section(client, INTRO)
    adopt_section(client, "sections/results.tex")
    # same file → that file's section
    assert adopt_paragraph(client, "sections/results.tex", 9)["new_id"] == "p-1"
    # no adopted section in method.tex → the adopted section before it in document order
    body = adopt_paragraph(client, "sections/method.tex", 4)
    assert children(body["spec"], "introduction-1") == ["p-2"]
    # the abstract precedes every section → the last section (brief's fallback)
    body = adopt_paragraph(client, "main.tex", 22)
    assert children(body["spec"], "evaluation-1") == ["p-3", "p-1"]
    # explicit parent
    body = adopt_paragraph(client, INTRO, 22, parent_id="evaluation-1")
    assert children(body["spec"], "evaluation-1") == ["p-3", "p-4", "p-1"]

    def refused(code: int, **kw: Any) -> str:
        body = {"kind": "paragraph", "file": INTRO, "line_start": 11, **kw}
        response = client.post("/api/spec/adopt", json=body)
        assert response.status_code == code, response.text
        return response.json()["detail"]

    assert "not a section" in refused(400, parent_id="p-1")
    assert "unknown parent" in refused(404, parent_id="nope")
    assert "no paragraph" in refused(400, line_start=3)
    assert "not a paragraph" in refused(400, id="introduction-1")
    assert "invalid id" in refused(400, id="has space")
    assert "invalid request" in refused(400, kind="sentence")
    assert "invalid request" in refused(400, line=11)


def test_adopt_with_new_explicit_id_and_unowned_marker(
    client: TestClient, initialized_repo: Path
) -> None:
    adopt_section(client, INTRO)
    body = adopt_paragraph(client, INTRO, 4, id="intro-hook")
    assert body["new_id"] == "intro-hook"
    assert "% @p intro-hook\n" in (initialized_repo / INTRO).read_text()
    # a marker typed by hand (or left by a deleted node) is adopted under its own id
    edit_tex(client, INTRO, "Providers mitigate", "% @p p-7\nProviders mitigate")
    text = (initialized_repo / INTRO).read_text()
    body = adopt_paragraph(client, INTRO, 13)
    assert body["new_id"] == "p-7" and (initialized_repo / INTRO).read_text() == text
    assert status(client)["p-7"]["status"] == "linked"
    # ...but not when a different id is requested for it
    response = client.post(
        "/api/spec/adopt", json={"kind": "paragraph", "file": INTRO, "line_start": 13, "id": "p-1"}
    )
    assert response.status_code == 409


def test_relink_moves_the_marker(client: TestClient, initialized_repo: Path) -> None:
    adopt_section(client, INTRO)
    adopt_paragraph(client, INTRO, 11)  # p-1 above line 11
    body = adopt_paragraph(client, INTRO, 17, id="p-1")  # the next paragraph (16-20 → 17-21)
    assert body["line"] == 16
    lines = (initialized_repo / INTRO).read_text().splitlines()
    assert [i + 1 for i, line in enumerate(lines) if line == "% @p p-1"] == [16]
    assert lines[16].startswith("Snapshot restore")
    assert children(body["spec"], "introduction-1") == ["p-1"]
    assert status(client)["p-1"] == {"status": "linked", "file": INTRO, "line": 16, "reread": None}


# --------------------------------------------------------------------------- status


def test_status_transitions(client: TestClient, initialized_repo: Path) -> None:
    """planned → linked → stale → (reconcile) linked → stale → (edit says) linked → orphaned
    → (re-link) linked, all through the HTTP API."""
    section_id = ok(op(client, type="insert", parent_id="message", index=0, level="section"))[
        "new_id"
    ]
    para_id = ok(op(client, type="insert", parent_id=section_id, index=0, level="paragraph"))[
        "new_id"
    ]
    assert status(client) == {
        section_id: {"status": "planned", "file": None, "line": None, "reread": None},
        para_id: {"status": "planned", "file": None, "line": None, "reread": None},
    }

    adopt_paragraph(client, INTRO, 11, id=para_id)  # drag the planned node onto the paragraph
    ok(
        op(
            client,
            type="set_anchor",
            id=section_id,
            anchor={"file": INTRO, "marker": "\\label{sec:intro}"},
        )
    )
    assert status(client) == {
        section_id: {"status": "linked", "file": INTRO, "line": 1, "reread": None},
        para_id: {"status": "linked", "file": INTRO, "line": 11, "reread": None},
    }

    edit_tex(client, INTRO, "Warm pools trade memory", "Warm pools swap memory")
    assert status(client)[para_id]["status"] == "stale"
    assert status(client)[section_id]["status"] == "linked"  # sections are never stale

    reconciled = ok(client.post(f"/api/spec/nodes/{para_id}/reconcile"))
    assert reconciled == {
        "id": para_id,
        "status": "linked",
        "file": INTRO,
        "line": 11,
        "reread": None,
    }
    assert status(client)[para_id]["status"] == "linked"

    # comment and whitespace edits do not make a paragraph stale
    edit_tex(client, INTRO, "for latency, and the", "for latency,   and the % note")
    assert status(client)[para_id]["status"] == "linked"

    edit_tex(client, INTRO, "Warm pools swap memory", "Warm pools spend memory")
    assert status(client)[para_id]["status"] == "stale"
    ok(op(client, type="update", id=para_id, fields={"function": "contrast"}))
    assert status(client)[para_id]["status"] == "stale"  # fields do not reconcile
    ok(op(client, type="update", id=para_id, says="Warm pools waste memory."))
    assert status(client)[para_id]["status"] == "linked"  # editing says does

    edit_tex(client, INTRO, f"% @p {para_id}\n", "")
    assert status(client)[para_id] == {
        "status": "orphaned",
        "file": INTRO,
        "line": None,
        "reread": None,
    }
    assert ok(client.post(f"/api/spec/nodes/{para_id}/reconcile"))["status"] == "orphaned"
    edit_tex(client, INTRO, "\\section{Introduction}", "\\section{Overview}")
    edit_tex(client, INTRO, "\\label{sec:intro}", "\\label{sec:overview}")
    assert status(client)[section_id]["status"] == "orphaned"

    adopt_paragraph(client, INTRO, 11, id=para_id)  # re-link the orphan
    assert status(client)[para_id]["status"] == "linked"


def test_status_and_reconcile_errors(client: TestClient, initialized_repo: Path) -> None:
    ok(client.put("/api/spec", json={"spec": sample_spec().model_dump(mode="json")}))
    nodes = status(client)
    assert set(nodes) - {"c1", "c2"} == {"introduction-1", "p-1", "p-2", "design-1", "p-3"}
    assert nodes["introduction-1"]["status"] == "linked"
    assert nodes["p-1"] == {
        "status": "orphaned",
        "file": INTRO,
        "line": None,
        "reread": None,
    }  # no marker yet
    assert nodes["p-2"]["status"] == "planned"
    assert client.post("/api/spec/nodes/nope/reconcile").status_code == 404
    assert client.post("/api/spec/nodes/message/reconcile").status_code == 400
    # A claim can be reconciled now (it clears its re-read mark; no baseline to reset).
    assert ok(client.post("/api/spec/nodes/c1/reconcile"))["status"] == "planned"
    assert ok(client.post("/api/spec/nodes/p-2/reconcile"))["status"] == "planned"
    (initialized_repo / "paper.json").write_text("[]")
    assert client.get("/api/spec/status").status_code == 409


def test_new_ids_skip_markers_and_baselines(client: TestClient, initialized_repo: Path) -> None:
    section_id = adopt_section(client, INTRO)
    assert adopt_paragraph(client, INTRO, 4)["new_id"] == "p-1"
    ok(op(client, type="delete", id="p-1"))  # its marker stays in the .tex
    insert = {"type": "insert", "parent_id": section_id, "index": 0, "level": "paragraph"}
    assert ok(op(client, **insert))["new_id"] == "p-2"
    ok(op(client, type="delete", id="p-2"))
    edit_tex(client, INTRO, "% @p p-1\n", "")  # marker gone; the baseline row remembers p-1
    assert ok(op(client, **insert))["new_id"] == "p-3"


# --------------------------------------------------------------------------- audit item 10

DISCUSSION = "sections/conclusion.tex"
TWO_DISCUSSIONS = (
    "\\section{Discussion}\n"  # line 1
    "First discussion text.\n"
    "\n"
    "\\section{Discussion}\n"  # line 4
    "Second discussion text.\n"
)


def test_duplicate_unlabeled_heading_is_not_adopted(client: TestClient) -> None:
    """Audit 2026-10-18 item 10: with two unlabeled `\\section{Discussion}` in one file,
    the heading marker would resolve to the first one, so adopting the second produced a
    node linked to the wrong heading (repeatedly). Adoption must refuse (409) and ask for
    a label, and leave the spec unchanged."""
    ok(client.put("/api/file", json={"path": DISCUSSION, "content": TWO_DISCUSSIONS}))
    before = ok(client.get("/api/spec"))["raw"]
    for line in (4, 4, 1):
        response = client.post(
            "/api/spec/adopt", json={"kind": "section", "file": DISCUSSION, "line": line}
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert "\\label{" in detail and "Discussion" in detail
    assert ok(client.get("/api/spec"))["raw"] == before
    sections = ok(client.get("/api/spec/skeleton"))["sections"]
    assert [s["adopted_id"] for s in sections if s["file"] == DISCUSSION] == [None, None]


def test_duplicate_labeled_headings_anchor_by_label(client: TestClient) -> None:
    labeled = TWO_DISCUSSIONS.replace(
        "Discussion}\nFirst", "Discussion}\\label{sec:disc-a}\nFirst"
    ).replace("Discussion}\nSecond", "Discussion}\n\\label{sec:disc-b}\nSecond")
    ok(client.put("/api/file", json={"path": DISCUSSION, "content": labeled}))
    second = adopt_section(client, DISCUSSION, 4)
    first = adopt_section(client, DISCUSSION, 1)
    spec = ok(client.get("/api/spec"))["spec"]
    anchors = {node["id"]: node["anchor"] for node in spec["tree"]["children"]}
    assert anchors[second] == {"file": DISCUSSION, "marker": "\\label{sec:disc-b}"}
    assert anchors[first] == {"file": DISCUSSION, "marker": "\\label{sec:disc-a}"}
    nodes = status(client)
    assert nodes[second] == {"status": "linked", "file": DISCUSSION, "line": 4, "reread": None}
    assert nodes[first] == {"status": "linked", "file": DISCUSSION, "line": 1, "reread": None}
    again = client.post("/api/spec/adopt", json={"kind": "section", "file": DISCUSSION, "line": 4})
    assert again.status_code == 409 and "already adopted" in again.json()["detail"]
    sections = ok(client.get("/api/spec/skeleton"))["sections"]
    assert [s["adopted_id"] for s in sections if s["file"] == DISCUSSION] == [first, second]


def test_existing_ambiguous_heading_marker_is_orphaned(
    client: TestClient, initialized_repo: Path
) -> None:
    """A spec written before the fix (or by hand) may hold `\\section{Discussion}` while the
    source has two such headings: that is not a confident link but an orphan."""
    ok(client.put("/api/file", json={"path": DISCUSSION, "content": TWO_DISCUSSIONS}))
    section_id = ok(op(client, type="insert", parent_id="message", index=0, level="section"))[
        "new_id"
    ]
    ok(
        op(
            client,
            type="set_anchor",
            id=section_id,
            anchor={"file": DISCUSSION, "marker": "\\section{Discussion}"},
        )
    )
    assert status(client)[section_id] == {
        "status": "orphaned",
        "file": DISCUSSION,
        "line": None,
        "reread": None,
    }
    preview = client.post(
        "/api/runs/preview",
        json={"pass": "section-promises", "scope": {"kind": "section", "ref": section_id}},
    )
    assert preview.status_code == 400 and "orphaned" in preview.json()["detail"]
    # Renaming one of the headings leaves a single match: the anchor links again.
    edit_tex(client, DISCUSSION, "\\section{Discussion}\nSecond", "\\section{Limits}\nSecond")
    assert status(client)[section_id] == {
        "status": "linked",
        "file": DISCUSSION,
        "line": 1,
        "reread": None,
    }


# --------------------------------------------------------------------------- audit item 5

OLD_PROSE = "Warm pools trade memory"
NEW_PROSE = "Warm pools spend memory"


class MarkerGate:
    """Pauses skeleton.place_marker immediately before its (first) write, as in the audit's
    reproduction, until `resume` is set."""

    def __init__(self, monkeypatch: Any) -> None:
        from app import skeleton

        self.paused = threading.Event()
        self.resume = threading.Event()
        real = skeleton.write_text_file

        def gated(*args: Any, **kwargs: Any) -> float:
            if not self.paused.is_set():
                self.paused.set()
                assert self.resume.wait(15), "the test never resumed the marker write"
            return real(*args, **kwargs)

        monkeypatch.setattr(skeleton, "write_text_file", gated)


def _paragraph_nodes(client: TestClient) -> list[str]:
    spec = ok(client.get("/api/spec"))["spec"]
    return [p["id"] for s in spec["tree"]["children"] for p in s["children"]]


def test_marker_write_refuses_a_file_changed_after_the_scan(
    client: TestClient, initialized_repo: Path, monkeypatch: Any
) -> None:
    """Audit 2026-10-18 item 5, reproduced literally: pause the marker insertion right
    before its write, save new prose with the writer /api/file uses (bypassing the API, like
    another editor), resume. The adoption must not restore the old prose: 409, the new
    prose stays, no marker and no node."""
    from app.files import write_text_file

    adopt_section(client, INTRO)
    original = (initialized_repo / INTRO).read_text()
    gate = MarkerGate(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        adoption = pool.submit(
            client.post,
            "/api/spec/adopt",
            json={"kind": "paragraph", "file": INTRO, "line_start": 11},
        )
        assert gate.paused.wait(15)
        write_text_file(initialized_repo, INTRO, original.replace(OLD_PROSE, NEW_PROSE))
        gate.resume.set()
        response = adoption.result(timeout=15)
    assert response.status_code == 409, response.text
    assert "changed on disk" in response.json()["detail"]
    final = (initialized_repo / INTRO).read_text()
    assert final == original.replace(OLD_PROSE, NEW_PROSE)  # new prose, untouched
    assert "% @p" not in final and _paragraph_nodes(client) == []


@pytest.mark.parametrize("with_base", [True, False], ids=["base-revision", "no-base"])
def test_file_save_waits_for_a_marker_edit(
    client: TestClient, initialized_repo: Path, monkeypatch: Any, with_base: bool
) -> None:
    """The same interleaving through PUT /api/file: the save waits for the source lock the
    marker edit holds, so it can never land between the edit's check and its write. With
    the revision it was based on (`base_sha256` from GET /api/file) the stale save is an
    explicit 409; without one it is a plain overwrite that keeps the new prose (the marker
    is then gone and the node shows as orphaned, not silently reverted prose)."""
    adopt_section(client, INTRO)
    loaded = ok(client.get("/api/file", params={"path": INTRO}))
    assert loaded["sha256"] == hashlib.sha256(loaded["content"].encode()).hexdigest()
    body: dict[str, Any] = {
        "path": INTRO,
        "content": loaded["content"].replace(OLD_PROSE, NEW_PROSE),
    }
    if with_base:
        body["base_sha256"] = loaded["sha256"]
    gate = MarkerGate(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        adoption = pool.submit(
            client.post,
            "/api/spec/adopt",
            json={"kind": "paragraph", "file": INTRO, "line_start": 11},
        )
        assert gate.paused.wait(15)
        save = pool.submit(client.put, "/api/file", json=body)
        time.sleep(0.5)
        assert not save.done(), "the save ran while the marker edit was in progress"
        gate.resume.set()
        adopted = adoption.result(timeout=15)
        saved = save.result(timeout=15)
    assert adopted.status_code == 200, adopted.text
    node = adopted.json()["new_id"]
    final = (initialized_repo / INTRO).read_text()
    if with_base:
        assert saved.status_code == 409, saved.text
        assert "changed on disk" in saved.json()["detail"]
        assert OLD_PROSE in final and f"% @p {node}" in final  # nothing saved over the edit
        assert status(client)[node]["status"] == "linked"
    else:
        assert saved.status_code == 200, saved.text
        assert final == body["content"]  # the explicit save is what is on disk
        assert status(client)[node]["status"] == "orphaned"
    assert not (OLD_PROSE in final and saved.status_code == 200)  # never a reverted save


def test_save_with_a_stale_base_revision_is_409(client: TestClient, initialized_repo: Path) -> None:
    loaded = ok(client.get("/api/file", params={"path": INTRO}))
    edit_tex(client, INTRO, OLD_PROSE, NEW_PROSE)  # someone else saved in between
    stale = client.put(
        "/api/file",
        json={"path": INTRO, "content": loaded["content"] + "x", "base_sha256": loaded["sha256"]},
    )
    assert stale.status_code == 409 and "changed on disk" in stale.json()["detail"]
    assert NEW_PROSE in (initialized_repo / INTRO).read_text()
    current = ok(client.get("/api/file", params={"path": INTRO}))
    saved = ok(
        client.put(
            "/api/file",
            json={
                "path": INTRO,
                "content": current["content"] + "y",
                "base_sha256": current["sha256"],
            },
        )
    )
    assert saved["sha256"] == hashlib.sha256((current["content"] + "y").encode()).hexdigest()
    missing = client.put(
        "/api/file", json={"path": "sections/new.tex", "content": "x", "base_sha256": "0" * 64}
    )
    assert missing.status_code == 409  # a base revision of a file that does not exist
