"""The claims x sections coverage matrix (app/coverage.py, GET /api/spec/coverage)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.coverage import compute_coverage, section_heading
from app.skeleton import scan_document
from app.spec import Anchor, Node, PaperSpec, empty_spec
from tests.conftest import adopt_intro


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def op(client: TestClient, **body: Any) -> dict[str, Any]:
    return ok(client.post("/api/spec/ops", json={"op": body}))


def node(id_: str, level: str, claims: list[str] | None = None, **kw: Any) -> Node:
    return Node(id=id_, level=level, claims=claims or [], **kw)  # type: ignore[arg-type]


def spec_with(sections: list[Node], claims: list[str]) -> PaperSpec:
    spec = empty_spec()
    spec.tree.children = sections
    spec.claims = [node(c, "claim", says=f"claim {c}") for c in claims]
    return spec


def test_counts_unreferenced_claims_and_empty_sections() -> None:
    intro = node(
        "intro-1",
        "section",
        ["c1"],
        says="the promise",
        anchor=Anchor(file="sections/intro.tex", marker="\\section{Introduction}"),
        children=[node("p-1", "paragraph", ["c1", "c2"]), node("p-2", "paragraph", ["c1"])],
    )
    method = node("method-1", "section", children=[node("p-3", "paragraph", ["c2"])])
    empty = node("results-1", "section", children=[node("p-4", "paragraph")])
    coverage = compute_coverage(spec_with([intro, method, empty], ["c1", "c2", "c3"]))
    assert coverage.cells == {
        "c1": {"intro-1": 3, "method-1": 0, "results-1": 0},
        "c2": {"intro-1": 1, "method-1": 1, "results-1": 0},
        "c3": {"intro-1": 0, "method-1": 0, "results-1": 0},
    }
    assert coverage.unreferenced_claims == ["c3"]
    assert coverage.empty_sections == ["results-1"]
    assert [c.model_dump() for c in coverage.claims][0] == {"id": "c1", "says": "claim c1"}
    sections = [s.model_dump() for s in coverage.sections]
    assert sections[0] == {"id": "intro-1", "says": "the promise", "heading": "Introduction"}
    assert sections[1]["heading"] is None  # planned


def test_no_claims_or_no_sections() -> None:
    sections = [node("a-1", "section"), node("b-1", "section")]
    coverage = compute_coverage(spec_with(sections, []))
    assert coverage.cells == {} and coverage.unreferenced_claims == []
    assert coverage.empty_sections == ["a-1", "b-1"]
    coverage = compute_coverage(spec_with([], ["c1"]))
    assert coverage.cells == {"c1": {}} and coverage.unreferenced_claims == ["c1"]
    assert coverage.sections == [] and coverage.empty_sections == []


def test_heading_from_a_label_anchor(initialized_repo: Path) -> None:
    scan = scan_document(initialized_repo, "main.tex")
    labelled = node(
        "x-1", "section", anchor=Anchor(file="sections/intro.tex", marker="\\label{sec:intro}")
    )
    assert section_heading(labelled, scan) == "Introduction"
    assert section_heading(labelled, None) is None
    missing = node("y-1", "section", anchor=Anchor(file="a.tex", marker="\\label{nope}"))
    assert section_heading(missing, scan) is None


def test_coverage_endpoint_follows_links(client: TestClient, initialized_repo: Path) -> None:
    section_id, paragraphs = adopt_intro(client)
    empty = ok(client.get("/api/spec/coverage"))
    assert empty == {
        "claims": [],
        "sections": [{"id": section_id, "says": "", "heading": "Introduction"}],
        "cells": {},
        "unreferenced_claims": [],
        "empty_sections": [section_id],
    }
    claim = op(client, type="add_claim", says="Pages are stable")["new_id"]
    other = op(client, type="add_claim", says="Unused")["new_id"]
    op(client, type="link_claim", id=paragraphs[1], claim_id=claim)
    op(client, type="link_claim", id=section_id, claim_id=claim)
    data = ok(client.get("/api/spec/coverage"))
    assert data["cells"] == {claim: {section_id: 2}, other: {section_id: 0}}
    assert data["unreferenced_claims"] == [other] and data["empty_sections"] == []
    assert [c["says"] for c in data["claims"]] == ["Pages are stable", "Unused"]
    op(client, type="unlink_claim", id=paragraphs[1], claim_id=claim)
    op(client, type="unlink_claim", id=section_id, claim_id=claim)
    data = ok(client.get("/api/spec/coverage"))
    assert data["cells"][claim] == {section_id: 0}
    assert data["unreferenced_claims"] == [claim, other]
    assert data["empty_sections"] == [section_id]
    (initialized_repo / "paper.json").write_text("{")
    assert client.get("/api/spec/coverage").status_code == 409
