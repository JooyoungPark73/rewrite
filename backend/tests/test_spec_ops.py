"""Spec edit ops (apply_op), id generation and level changes."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter

from app.spec import (
    Anchor,
    InvalidOpError,
    Node,
    NotFoundError,
    Op,
    PaperSpec,
    all_ids,
    apply_op,
    change_level,
    find_claim,
    find_tree_node,
    next_id,
    slugify,
    validate,
)
from tests.test_spec_model import sample_spec

OP = TypeAdapter(Op)


def run(spec: PaperSpec, reserved: set[str] | None = None, **op: Any) -> tuple[PaperSpec, Any]:
    new, new_id = apply_op(spec, OP.validate_python(op), frozenset(reserved or ()))
    assert validate(new) == [], validate(new)
    return new, new_id


def node(spec: PaperSpec, node_id: str) -> Node:
    found = find_tree_node(spec, node_id)
    assert found is not None, node_id
    return found[0]


def child_ids(parent: Node) -> list[str]:
    return [child.id for child in parent.children]


# --------------------------------------------------------------------------- update


def test_update_says_fields_notes() -> None:
    spec = sample_spec()
    spec.tree.children[0].children[0].fields = {"function": "hook"}
    new, new_id = run(spec, type="update", id="p-1", says="New", fields={"function": ""})
    assert new_id is None
    p1 = node(new, "p-1")
    assert p1.says == "New" and p1.fields == {"function": ""} and p1.notes == ""
    new, _ = run(new, type="update", id="p-1", notes="todo")
    assert node(new, "p-1").notes == "todo" and node(new, "p-1").says == "New"
    assert node(spec, "p-1").says == "Cold starts are frequent."  # input not mutated


def test_update_message_and_claim() -> None:
    spec = sample_spec()
    new, _ = run(spec, type="update", id="message", fields={"thesis": "T"})
    assert new.message.fields == {"problem": "", "stakes": "", "thesis": "T"}
    assert new.tree.says == ""  # the structural root is not the card
    new, _ = run(new, type="update", id="c1", says="Edited claim")
    assert find_claim(new, "c1").says == "Edited claim"  # type: ignore[union-attr]


def test_update_unknown_id_and_bad_field() -> None:
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="update", id="nope", says="x")
    new, _ = apply_op(
        sample_spec(), OP.validate_python({"type": "update", "id": "p-1", "fields": {"x": "1"}})
    )
    assert "tree.children[0].children[0].fields.x" in {e.path for e in validate(new)}


# --------------------------------------------------------------------------- insert


def test_insert_paragraph_and_section() -> None:
    spec = sample_spec()
    new, new_id = run(spec, type="insert", parent_id="introduction-1", index=1, level="paragraph")
    assert new_id == "p-4"
    assert child_ids(node(new, "introduction-1")) == ["p-1", "p-4", "p-2"]
    inserted = node(new, "p-4")
    assert inserted.level == "paragraph" and inserted.fields == {"function": ""}
    assert inserted.anchor is None and inserted.says == "" and inserted.notes == ""

    new, sec_id = run(new, type="insert", parent_id="message", index=2, level="section")
    assert sec_id == "section-1" and child_ids(new.tree)[-1] == "section-1"
    assert node(new, "section-1").fields == {"figure_story": ""}
    new, sec_id = run(
        new, type="insert", parent_id="message", index=0, level="section", says="Related Work!"
    )
    assert sec_id == "related-work-1" and child_ids(new.tree)[0] == "related-work-1"
    assert node(new, sec_id).says == "Related Work!"


@pytest.mark.parametrize(
    ("op", "error"),
    [
        ({"parent_id": "message", "index": 0, "level": "paragraph"}, InvalidOpError),
        ({"parent_id": "introduction-1", "index": 0, "level": "section"}, InvalidOpError),
        ({"parent_id": "p-1", "index": 0, "level": "paragraph"}, InvalidOpError),
        ({"parent_id": "c1", "index": 0, "level": "paragraph"}, InvalidOpError),
        ({"parent_id": "introduction-1", "index": 3, "level": "paragraph"}, InvalidOpError),
        ({"parent_id": "introduction-1", "index": -1, "level": "paragraph"}, InvalidOpError),
        ({"parent_id": "nope", "index": 0, "level": "paragraph"}, NotFoundError),
    ],
)
def test_insert_errors(op: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        run(sample_spec(), type="insert", **op)


# --------------------------------------------------------------------------- delete


def test_delete_paragraph_and_section() -> None:
    spec = sample_spec()
    new, new_id = run(spec, type="delete", id="p-1")
    assert new_id is None and child_ids(node(new, "introduction-1")) == ["p-2"]
    assert find_claim(new, "c1") is not None  # claims referenced by nothing stay in the bank
    new, _ = run(new, type="delete", id="introduction-1")
    assert child_ids(new.tree) == ["design-1"] and "p-2" not in all_ids(new)


def test_delete_claim_through_delete_removes_references() -> None:
    new, _ = run(sample_spec(), type="delete", id="c1")
    assert find_claim(new, "c1") is None and node(new, "p-1").claims == []


def test_delete_errors() -> None:
    with pytest.raises(InvalidOpError):
        run(sample_spec(), type="delete", id="message")
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="delete", id="nope")


# --------------------------------------------------------------------------- move


def test_move_reorders_and_reparents() -> None:
    spec = sample_spec()
    new, _ = run(spec, type="move", id="p-1", parent_id="introduction-1", index=1)
    assert child_ids(node(new, "introduction-1")) == ["p-2", "p-1"]
    new, _ = run(new, type="move", id="p-1", parent_id="design-1", index=0)
    assert child_ids(node(new, "design-1")) == ["p-1", "p-3"]
    assert child_ids(node(new, "introduction-1")) == ["p-2"]
    new, _ = run(new, type="move", id="design-1", parent_id="message", index=0)
    assert child_ids(new.tree) == ["design-1", "introduction-1"]
    assert child_ids(node(new, "design-1")) == ["p-1", "p-3"]  # moves with its subtree


@pytest.mark.parametrize(
    ("op", "error", "needle"),
    [
        (
            {"id": "introduction-1", "parent_id": "introduction-1", "index": 0},
            InvalidOpError,
            "own subtree",
        ),
        ({"id": "introduction-1", "parent_id": "p-1", "index": 0}, InvalidOpError, "own subtree"),
        ({"id": "p-1", "parent_id": "message", "index": 0}, InvalidOpError, "sections"),
        (
            {"id": "design-1", "parent_id": "introduction-1", "index": 0},
            InvalidOpError,
            "paragraphs",
        ),
        ({"id": "p-1", "parent_id": "p-2", "index": 0}, InvalidOpError, "cannot have children"),
        ({"id": "p-1", "parent_id": "design-1", "index": 2}, InvalidOpError, "out of range"),
        ({"id": "message", "parent_id": "message", "index": 0}, InvalidOpError, "message"),
        ({"id": "c1", "parent_id": "design-1", "index": 0}, InvalidOpError, "claim"),
        ({"id": "nope", "parent_id": "design-1", "index": 0}, NotFoundError, "unknown id"),
        ({"id": "p-1", "parent_id": "nope", "index": 0}, NotFoundError, "unknown parent"),
    ],
)
def test_move_errors(op: dict[str, Any], error: type[Exception], needle: str) -> None:
    with pytest.raises(error, match=needle):
        run(sample_spec(), type="move", **op)


# --------------------------------------------------------------------------- set_level


def test_promote_paragraph_to_section() -> None:
    spec = sample_spec()
    spec.tree.children[0].children[0].fields = {"function": "motivate"}
    new, _ = run(spec, type="set_level", id="p-1", level="section")
    assert child_ids(new.tree) == ["introduction-1", "p-1", "design-1"]
    promoted = node(new, "p-1")
    assert promoted.level == "section" and promoted.says == "Cold starts are frequent."
    assert promoted.fields == {"figure_story": ""} and promoted.notes == "function: motivate"
    assert promoted.claims == ["c1"] and promoted.anchor is None
    assert child_ids(node(new, "introduction-1")) == ["p-2"]


def test_demote_section_to_paragraph() -> None:
    spec = sample_spec()
    spec.tree.children[1].children = []
    spec.tree.children[1].fields = {"figure_story": "Fig 2 shows the pipeline"}
    spec.tree.children[1].notes = "keep"
    new, _ = run(spec, type="set_level", id="design-1", level="paragraph")
    assert child_ids(new.tree) == ["introduction-1"]
    assert child_ids(node(new, "introduction-1")) == ["p-1", "p-2", "design-1"]
    demoted = node(new, "design-1")
    assert demoted.level == "paragraph" and demoted.fields == {"function": ""}
    assert demoted.notes == "keep\nfigure_story: Fig 2 shows the pipeline"
    assert demoted.claims == ["c2"]


def test_promote_then_demote_keeps_everything_in_notes() -> None:
    spec = sample_spec()
    spec.tree.children[0].children[1].fields = {"function": "contrast"}
    new, _ = run(spec, type="set_level", id="p-2", level="section")
    new, _ = run(new, type="update", id="p-2", fields={"figure_story": "none"})
    new, _ = run(new, type="set_level", id="p-2", level="paragraph")
    back = node(new, "p-2")
    assert back.level == "paragraph" and back.says == "Warm pools waste memory."
    assert back.notes == "function: contrast\nfigure_story: none"
    assert child_ids(node(new, "introduction-1")) == ["p-1", "p-2"]


def test_set_level_errors_and_noop() -> None:
    with pytest.raises(InvalidOpError, match="has paragraphs"):
        run(sample_spec(), type="set_level", id="introduction-1", level="paragraph")
    spec = sample_spec()
    spec.tree.children[0].children = []
    with pytest.raises(InvalidOpError, match="first section"):
        run(spec, type="set_level", id="introduction-1", level="paragraph")
    with pytest.raises(InvalidOpError):
        run(sample_spec(), type="set_level", id="message", level="section")
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="set_level", id="nope", level="section")
    same, _ = run(sample_spec(), type="set_level", id="p-1", level="paragraph")
    assert same == sample_spec()


def test_change_level_appends_only_non_empty_fields() -> None:
    para = Node(id="p-1", level="paragraph", fields={"function": ""}, notes="n")
    moved = change_level(para, "section")
    assert moved.fields == {"figure_story": ""} and moved.notes == "n"
    message = Node(id="m", level="message", fields={"problem": "P", "stakes": "", "thesis": "T"})
    assert change_level(message, "section").notes == "problem: P\nthesis: T"


# --------------------------------------------------------------------------- claims


def test_lift_to_claim() -> None:
    new, new_id = run(sample_spec(), type="lift_to_claim", id="p-2")
    assert new_id == "c3"
    lifted = find_claim(new, "c3")
    assert lifted is not None and lifted.says == "Warm pools waste memory."
    assert lifted.fields == {"evidence": "", "scope": ""} and lifted.level == "claim"
    assert node(new, "p-2").claims == ["c3"]
    new, new_id = run(new, type="lift_to_claim", id="message")
    assert new_id == "c4" and new.message.claims == ["c4"]
    with pytest.raises(InvalidOpError):
        run(sample_spec(), type="lift_to_claim", id="c1")
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="lift_to_claim", id="nope")


def test_link_and_unlink_claim() -> None:
    new, _ = run(sample_spec(), type="link_claim", id="p-2", claim_id="c2")
    new, _ = run(new, type="link_claim", id="p-2", claim_id="c1")
    new, _ = run(new, type="link_claim", id="p-2", claim_id="c2")  # idempotent
    assert node(new, "p-2").claims == ["c2", "c1"]
    new, _ = run(new, type="unlink_claim", id="p-2", claim_id="c2")
    assert node(new, "p-2").claims == ["c1"]
    new, _ = run(new, type="unlink_claim", id="p-2", claim_id="c2")  # not linked: no-op
    assert node(new, "p-2").claims == ["c1"]
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="link_claim", id="p-2", claim_id="c9")
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="unlink_claim", id="p-2", claim_id="c9")
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="link_claim", id="nope", claim_id="c1")
    with pytest.raises(InvalidOpError):
        run(sample_spec(), type="link_claim", id="c2", claim_id="c1")


def test_add_update_delete_claim() -> None:
    new, new_id = run(sample_spec(), type="add_claim", says="New", fields={"scope": "Linux"})
    assert new_id == "c3"
    added = find_claim(new, "c3")
    assert added is not None and added.fields == {"evidence": "", "scope": "Linux"}
    new, _ = run(
        new, type="update_claim", id="c3", says="S", fields={"evidence": "Tab 1"}, notes="n"
    )
    updated = find_claim(new, "c3")
    assert updated is not None and (updated.says, updated.notes) == ("S", "n")
    assert updated.fields == {"evidence": "Tab 1", "scope": "Linux"}
    new, _ = run(new, type="link_claim", id="message", claim_id="c2")
    new, _ = run(new, type="delete_claim", id="c2")
    assert find_claim(new, "c2") is None
    assert new.message.claims == [] and node(new, "design-1").claims == []
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="update_claim", id="p-1", says="x")
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="delete_claim", id="nope")


# --------------------------------------------------------------------------- set_anchor


def test_set_anchor() -> None:
    anchor = {"file": "sections/intro.tex", "marker": "@p p-2"}
    new, _ = run(sample_spec(), type="set_anchor", id="p-2", anchor=anchor)
    assert node(new, "p-2").anchor == Anchor(**anchor)
    new, _ = run(new, type="set_anchor", id="p-2", anchor=None)
    assert node(new, "p-2").anchor is None
    with pytest.raises(InvalidOpError):
        run(sample_spec(), type="set_anchor", id="message", anchor=None)
    with pytest.raises(NotFoundError):
        run(sample_spec(), type="set_anchor", id="nope", anchor=None)
    bad, _ = apply_op(
        sample_spec(),
        OP.validate_python(
            {"type": "set_anchor", "id": "p-2", "anchor": {"file": "a.tex", "marker": "@p p-1"}}
        ),
    )
    assert validate(bad)  # the API turns this into a 422


def test_op_models_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        OP.validate_python({"type": "update", "id": "p-1", "say": "typo"})
    with pytest.raises(ValueError):
        OP.validate_python({"type": "explode", "id": "p-1"})


# --------------------------------------------------------------------------- ids


def test_next_id_goes_past_the_highest_suffix() -> None:
    assert next_id("p", set()) == "p-1"
    assert next_id("p", {"p-1", "p-3", "p-x", "pp-9", "intro-7"}) == "p-4"
    assert next_id("c", {"c1", "c12", "c-40"}, sep="") == "c13"
    assert next_id("intro", {"intro-2", "introduction-9"}) == "intro-3"


def test_ids_are_never_reused() -> None:
    spec = sample_spec()  # p-1..p-3
    spec, a = run(spec, type="insert", parent_id="design-1", index=0, level="paragraph")
    spec, b = run(spec, type="insert", parent_id="design-1", index=0, level="paragraph")
    assert (a, b) == ("p-4", "p-5")
    spec, _ = run(spec, type="delete", id="p-4")  # a gap is not refilled
    spec, c = run(spec, type="insert", parent_id="design-1", index=0, level="paragraph")
    assert c == "p-6"
    spec, _ = run(spec, type="delete", id="p-6")  # the highest id: remembered via `reserved`
    spec, d = run(spec, {"p-6"}, type="insert", parent_id="design-1", index=0, level="paragraph")
    assert d == "p-7"
    spec, e = run(spec, {"p-40"}, type="insert", parent_id="design-1", index=0, level="paragraph")
    assert e == "p-41"
    _, c3 = run(spec, type="add_claim", says="x")
    spec, _ = run(spec, type="delete_claim", id="c1")
    _, again = run(spec, type="add_claim", says="x")
    assert c3 == again == "c3"  # c1 was below the highest (c2): not reused


def test_slugify() -> None:
    assert slugify("Introduction") == "introduction"
    assert slugify("\\textsc{Prefetch}: Design & Implementation") == "prefetch-design-implemen"
    assert len(slugify("x" * 40)) == 24
    assert slugify("A very long heading with-") == "a-very-long-heading-with"
    assert slugify("$\\alpha$") == "section"
    assert slugify("Évaluation") == "evaluation"
