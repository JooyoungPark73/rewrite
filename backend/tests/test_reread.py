"""Claim anchors and re-read marks: the downward half of the spec/text loop."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.repo import Repo
from app.spec import PaperSpec, load_spec, reread_marks

INTRO = "sections/intro.tex"
SENTENCE = (
    "Warm pools trade memory for latency, and the\n"
    "trade is poor for the long tail of rarely invoked functions:"
)


def ok(res):  # type: ignore[no-untyped-def]
    assert res.status_code == 200, res.text
    return res.json()


def op(client: TestClient, o: dict):  # type: ignore[no-untyped-def]
    return ok(client.post("/api/spec/ops", json={"op": o}))


def status(client: TestClient) -> dict:
    return ok(client.get("/api/spec/status"))["nodes"]


def setup(client: TestClient) -> tuple[str, str, str]:
    """Introduction adopted with its first two paragraphs; returns (section, p1, p2)."""
    section = ok(
        client.post("/api/spec/adopt", json={"kind": "section", "file": INTRO, "line": 1})
    )["new_id"]
    ids = []
    for _ in range(2):
        para = next(
            p
            for p in ok(client.get("/api/spec/skeleton"))["paragraphs"]
            if p["file"] == INTRO and p["section_heading"] and not p["marker_id"]
        )
        ids.append(
            ok(
                client.post(
                    "/api/spec/adopt",
                    json={"kind": "paragraph", "file": INTRO, "line_start": para["line_start"]},
                )
            )["new_id"]
        )
    return section, ids[0], ids[1]


# --------------------------------------------------------------------------- claim anchors


def test_claim_anchor_status(client: TestClient, initialized_repo: Path) -> None:
    c = op(client, {"type": "add_claim", "says": "Warm pools are a poor trade"})["new_id"]
    assert status(client)[c] == {"status": "planned", "file": None, "line": None, "reread": None}
    # Anchor on the sentence (whitespace as in the source; matched collapsed, across lines).
    op(client, {"type": "set_anchor", "id": c, "anchor": {"file": INTRO, "marker": SENTENCE}})
    st = status(client)[c]
    assert st["status"] == "linked" and st["file"] == INTRO and st["line"] == 12
    # Reword the sentence: stale. Put it back: linked again.
    path = initialized_repo / INTRO
    text = path.read_text()
    path.write_text(
        text.replace("Warm pools trade memory for latency", "Warm pools swap memory for latency")
    )
    assert status(client)[c]["status"] == "stale"
    assert status(client)[c]["line"] is None
    path.write_text(text)
    assert status(client)[c]["status"] == "linked"
    # A file outside the include tree: orphaned.
    op(
        client,
        {"type": "set_anchor", "id": c, "anchor": {"file": "sections/nope.tex", "marker": "x y"}},
    )
    assert status(client)[c]["status"] == "orphaned"
    # Clearing the anchor: planned.
    op(client, {"type": "set_anchor", "id": c, "anchor": None})
    assert status(client)[c]["status"] == "planned"


def test_add_claim_with_anchor_and_validation(client: TestClient) -> None:
    res = op(
        client,
        {
            "type": "add_claim",
            "says": "S",
            "anchor": {"file": INTRO, "marker": "Function-as-a-Service (FaaS) platforms"},
        },
    )
    c = res["new_id"]
    claim = next(x for x in res["spec"]["claims"] if x["id"] == c)
    assert claim["anchor"] == {"file": INTRO, "marker": "Function-as-a-Service (FaaS) platforms"}
    assert status(client)[c]["status"] == "linked" and status(client)[c]["line"] == 4
    bad = client.post(
        "/api/spec/ops",
        json={"op": {"type": "add_claim", "says": "S", "anchor": {"file": INTRO, "marker": ""}}},
    )
    assert bad.status_code == 422 and "empty" in bad.text


# --------------------------------------------------------------------------- re-read marks


def _spec(repo_root: Path) -> PaperSpec:
    spec, _, _ = load_spec(Repo.at(repo_root))
    assert spec is not None
    return spec


def test_reread_marks_rules(initialized_repo: Path, client: TestClient) -> None:
    section, p1, p2 = setup(client)
    c = op(client, {"type": "add_claim", "says": "A claim"})["new_id"]
    op(client, {"type": "link_claim", "id": p1, "claim_id": c})
    before = _spec(initialized_repo)

    # Pure function: a changed section promise marks its paragraphs; the section is cleared.
    after = before.model_copy(deep=True)
    after.tree.children[0].says = "New promise"
    marks, cleared = reread_marks(before, after)
    assert marks == {
        p1: f"the promise of {section} changed",
        p2: f"the promise of {section} changed",
    }
    assert cleared == {section}

    # A changed claim marks the nodes linking it.
    after = before.model_copy(deep=True)
    after.claims[0].fields["scope"] = "narrower"
    marks, cleared = reread_marks(before, after)
    assert marks == {p1: f"claim {c} changed"} and cleared == {c}

    # A changed message marks the sections (not the paragraphs).
    after = before.model_copy(deep=True)
    after.message.fields["thesis"] = "New argument"
    marks, cleared = reread_marks(before, after)
    assert marks == {section: "the message changed"} and cleared == {"message"}

    # Links, notes, anchors and moves mark nothing.
    after = before.model_copy(deep=True)
    after.tree.children[0].children[1].claims.append(c)
    after.tree.children[0].notes = "a note"
    after.claims[0].anchor = None
    assert reread_marks(before, after) == ({}, set())

    # The first write (no previous spec) marks nothing.
    assert reread_marks(None, after) == ({}, set())


def test_reread_marks_through_the_api(client: TestClient, initialized_repo: Path) -> None:
    section, p1, p2 = setup(client)
    c = op(client, {"type": "add_claim", "says": "A claim"})["new_id"]
    op(client, {"type": "link_claim", "id": p1, "claim_id": c})
    op(client, {"type": "link_claim", "id": section, "claim_id": c})
    assert all(v["reread"] is None for v in status(client).values())

    # Narrow the claim's scope: the paragraph and the section that argue it are marked.
    op(client, {"type": "update_claim", "id": c, "fields": {"scope": "only on one trace"}})
    st = status(client)
    assert st[p1]["reread"] == f"claim {c} changed"
    assert st[section]["reread"] == f"claim {c} changed"
    assert st[p2]["reread"] is None and st[c]["reread"] is None

    # Editing the paragraph's own says clears its mark; the section keeps its mark.
    op(client, {"type": "update", "id": p1, "says": "Revised local message"})
    st = status(client)
    assert st[p1]["reread"] is None and st[section]["reread"] == f"claim {c} changed"

    # Reconcile clears the section's mark; the answer says so.
    res = ok(client.post(f"/api/spec/nodes/{section}/reconcile"))
    assert res["reread"] is None and res["status"] == "linked"
    assert status(client)[section]["reread"] is None

    # A changed promise marks both paragraphs; a changed message marks the section.
    op(client, {"type": "update", "id": section, "says": "A new promise"})
    st = status(client)
    assert st[p1]["reread"] == f"the promise of {section} changed"
    assert st[p2]["reread"] == f"the promise of {section} changed"
    op(client, {"type": "update", "id": "message", "fields": {"stakes": "new stakes"}})
    assert status(client)[section]["reread"] == "the message changed"

    # Deleting a marked node drops its row (no error, no leak into a new node with the id).
    op(client, {"type": "delete", "id": p2})
    assert p2 not in status(client)

    # Undo through PUT /api/spec is a write like any other: restoring the old promise marks
    # the paragraph again (the author changed it back).
    spec = ok(client.get("/api/spec"))["spec"]
    spec["tree"]["children"][0]["says"] = ""
    ok(client.put("/api/spec", json={"spec": spec}))
    assert status(client)[p1]["reread"] == f"the promise of {section} changed"


def test_reconcile_paragraph_clears_both_stale_and_reread(
    client: TestClient, initialized_repo: Path
) -> None:
    section, p1, _ = setup(client)
    op(client, {"type": "update", "id": section, "says": "Promise"})
    path = initialized_repo / INTRO
    path.write_text(
        path.read_text().replace("bill tenants per invocation", "bill tenants per call")
    )
    st = status(client)[p1]
    assert st["status"] == "stale" and st["reread"] is not None
    res = ok(client.post(f"/api/spec/nodes/{p1}/reconcile"))
    assert res["status"] == "linked" and res["reread"] is None
