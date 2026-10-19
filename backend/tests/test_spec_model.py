"""Spec model: validator rules, parsing errors and the deterministic serializer."""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from app.repo import Repo, init_repo
from app.spec import (
    Anchor,
    Node,
    PaperSpec,
    empty_spec,
    load_spec,
    parse_spec,
    parse_spec_data,
    serialize,
    validate,
)


def paragraph(node_id: str, **kw: object) -> Node:
    return Node(id=node_id, level="paragraph", fields={"function": ""}, **kw)  # type: ignore[arg-type]


def section(node_id: str, children: list[Node] | None = None, **kw: object) -> Node:
    return Node(
        id=node_id,
        level="section",
        fields={"figure_story": ""},
        children=children or [],
        **kw,  # type: ignore[arg-type]
    )


def claim(node_id: str, says: str = "") -> Node:
    return Node(id=node_id, level="claim", says=says, fields={"evidence": "", "scope": ""})


def sample_spec() -> PaperSpec:
    """A valid spec with two sections, three paragraphs and two claims."""
    spec = empty_spec()
    spec.message.says = "Prefetching beats warm pools."
    spec.claims = [claim("c1", "Working sets are stable."), claim("c2", "They are small.")]
    spec.tree.children = [
        section(
            "introduction-1",
            [
                paragraph(
                    "p-1",
                    says="Cold starts are frequent.",
                    claims=["c1"],
                    anchor=Anchor(file="sections/intro.tex", marker="@p p-1"),
                ),
                paragraph("p-2", says="Warm pools waste memory."),
            ],
            anchor=Anchor(file="sections/intro.tex", marker="\\section{Introduction}"),
        ),
        section("design-1", [paragraph("p-3")], claims=["c2"]),
    ]
    assert validate(spec) == []
    return spec


def error_paths(spec: PaperSpec) -> dict[str, str]:
    return {e.path: e.message for e in validate(spec)}


# --------------------------------------------------------------------------- validator


def test_empty_spec_is_valid() -> None:
    assert validate(empty_spec()) == []


Breaker = Callable[[PaperSpec], None]


def _set_child(spec: PaperSpec, node: Node) -> None:
    spec.tree.children[0].children[0].children = [node]


BREAKERS: list[tuple[str, Breaker, str, str]] = [
    ("version", lambda s: setattr(s, "version", 2), "version", "expected 1"),
    ("message level", lambda s: setattr(s.message, "level", "claim"), "message.level", "message"),
    (
        "message children",
        lambda s: s.message.children.append(paragraph("p-9")),
        "message.children",
        "must be []",
    ),
    ("tree level", lambda s: setattr(s.tree, "level", "section"), "tree.level", "message"),
    ("tree id", lambda s: setattr(s.tree, "id", "root"), "tree.id", "message.id"),
    ("tree says", lambda s: setattr(s.tree, "says", "x"), "tree.says", "structure only"),
    ("tree fields", lambda s: setattr(s.tree, "fields", {"x": ""}), "tree.fields", "{}"),
    ("tree claims", lambda s: setattr(s.tree, "claims", ["c1"]), "tree.claims", "[]"),
    (
        "tree anchor",
        lambda s: setattr(s.tree, "anchor", Anchor(file="main.tex", marker="@p message")),
        "tree.anchor",
        "no anchor",
    ),
    ("tree notes", lambda s: setattr(s.tree, "notes", "x"), "tree.notes", "structure only"),
    (
        "duplicate id claim/tree",
        lambda s: setattr(s.claims[1], "id", "p-1"),
        "tree.children[0].children[0].id",
        "duplicate id 'p-1'",
    ),
    (
        "duplicate id section/paragraph",
        lambda s: setattr(s.tree.children[1], "id", "p-2"),
        "tree.children[1].id",  # the second p-2 in tree order is the one flagged
        "duplicate id 'p-2' (also at tree.children[0].children[1])",
    ),
    (
        "duplicate id message",
        lambda s: setattr(s.claims[0], "id", "message"),
        "claims[0].id",
        "duplicate id 'message'",
    ),
    (
        "dangling claim ref",
        lambda s: s.tree.children[0].children[1].claims.append("c9"),
        "tree.children[0].children[1].claims[0]",
        "unknown claim 'c9'",
    ),
    (
        "dangling claim ref on message",
        lambda s: s.message.claims.append("c9"),
        "message.claims[0]",
        "unknown claim",
    ),
    (
        "claim linked twice",
        lambda s: s.tree.children[1].claims.append("c2"),
        "tree.children[1].claims[1]",
        "linked twice",
    ),
    ("claim level", lambda s: setattr(s.claims[0], "level", "section"), "claims[0].level", "claim"),
    (
        "claim children",
        lambda s: s.claims[0].children.append(paragraph("p-9")),
        "claims[0].children",
        "no children",
    ),
    (
        "claim links claims",
        lambda s: s.claims[0].claims.append("c2"),
        "claims[0].claims",
        "does not link",
    ),
    (
        "claim anchor",
        lambda s: setattr(s.claims[0], "anchor", Anchor(file="a.tex", marker="@p c1")),
        "claims[0].anchor",
        "no anchor",
    ),
    (
        "message anchor",
        lambda s: setattr(s.message, "anchor", Anchor(file="a.tex", marker="@p message")),
        "message.anchor",
        "no anchor",
    ),
    (
        "paragraph under root",
        lambda s: s.tree.children.append(paragraph("p-9")),
        "tree.children[2].level",
        "must be sections",
    ),
    (
        "section under section",
        lambda s: s.tree.children[1].children.append(section("s-9")),
        "tree.children[1].children[1].level",
        "must be paragraphs",
    ),
    (
        "paragraph with children",
        lambda s: _set_child(s, paragraph("p-9")),
        "tree.children[0].children[0].children",
        "no children",
    ),
    (
        "unknown field",
        lambda s: s.tree.children[0].children[0].fields.update(colour="red"),
        "tree.children[0].children[0].fields.colour",
        "unknown field",
    ),
    (
        "bad id",
        lambda s: setattr(s.tree.children[0].children[1], "id", "has space"),
        "tree.children[0].children[1].id",
        "invalid id",
    ),
    (
        "paragraph marker for another id",
        lambda s: setattr(
            s.tree.children[0].children[0], "anchor", Anchor(file="a.tex", marker="@p p-2")
        ),
        "tree.children[0].children[0].anchor.marker",
        "'@p p-1'",
    ),
    (
        "section marker",
        lambda s: setattr(s.tree.children[0], "anchor", Anchor(file="a.tex", marker="Intro")),
        "tree.children[0].anchor.marker",
        "\\section{<heading>}",
    ),
    (
        "absolute anchor file",
        lambda s: setattr(
            s.tree.children[0], "anchor", Anchor(file="/etc/a.tex", marker="\\label{x}")
        ),
        "tree.children[0].anchor.file",
        "repo-relative",
    ),
    (
        "dotdot anchor file",
        lambda s: setattr(
            s.tree.children[0], "anchor", Anchor(file="../a.tex", marker="\\label{x}")
        ),
        "tree.children[0].anchor.file",
        "'..'",
    ),
    (
        "empty anchor file",
        lambda s: setattr(s.tree.children[0], "anchor", Anchor(file=" ", marker="\\label{x}")),
        "tree.children[0].anchor.file",
        "empty",
    ),
]


@pytest.mark.parametrize(
    ("breaker", "path", "needle"), [b[1:] for b in BREAKERS], ids=[b[0] for b in BREAKERS]
)
def test_validator_rejects(breaker: Breaker, path: str, needle: str) -> None:
    spec = sample_spec()
    breaker(spec)
    errors = error_paths(spec)
    assert path in errors, errors
    assert needle in errors[path]


def test_validator_accepts_section_marker_forms() -> None:
    spec = sample_spec()
    for marker in ("\\section{Intro}", "\\subsection{A {nested} title}", "\\label{sec:intro}"):
        spec.tree.children[0].anchor = Anchor(file="sections/intro.tex", marker=marker)
        assert validate(spec) == [], marker


# --------------------------------------------------------------------------- parsing


def test_parse_json_syntax_error_has_position() -> None:
    spec, errors = parse_spec('{\n  "version": 1,\n  oops\n}\n')
    assert spec is None
    assert errors[0].path == "" and errors[0].line == 3 and errors[0].column == 3
    assert "invalid JSON" in errors[0].message


def test_parse_rejects_duplicate_keys() -> None:
    raw = serialize(empty_spec()).replace('"version": 1', '"version": 1, "version": 1')
    spec, errors = parse_spec(raw)
    assert spec is None and "duplicate key 'version'" in errors[0].message


def test_parse_model_errors_have_paths() -> None:
    data = json.loads(serialize(sample_spec()))
    data["tree"]["children"][0]["children"][1]["level"] = "sentence"
    data["tree"]["children"][1]["extra"] = True
    del data["claims"][0]["id"]
    spec, errors = parse_spec_data(data)
    assert spec is None
    paths = {e.path for e in errors}
    assert "tree.children[0].children[1].level" in paths
    assert "tree.children[1].extra" in paths
    assert "claims[0].id" in paths


def test_parse_non_object() -> None:
    assert parse_spec("[]")[1][0].message == "the spec must be a JSON object"
    assert parse_spec_data(None)[0] is None


def test_parse_applies_defaults() -> None:
    raw = json.dumps(
        {
            "version": 1,
            "message": {"id": "message", "level": "message"},
            "claims": [],
            "tree": {"id": "message", "level": "message"},
        }
    )
    spec, errors = parse_spec(raw)
    assert errors == [] and spec is not None
    assert spec.message.notes == "" and spec.message.anchor is None


def test_load_spec_missing_and_broken(tmp_path: Path) -> None:
    repo = Repo.at(tmp_path)
    spec, raw, errors = load_spec(repo)
    assert spec is None and raw == "" and "not found" in errors[0].message
    (tmp_path / "paper.json").write_text('{"version": 2}')
    spec, raw, errors = load_spec(repo)
    assert spec is None and raw == '{"version": 2}' and errors
    (tmp_path / "paper.json").write_bytes(b'{"\xff": 1}')
    spec, raw, errors = load_spec(repo)
    assert spec is None and "UTF-8" in errors[0].message


def test_init_writes_canonical_empty_spec(paper_repo: Path) -> None:
    init_repo(paper_repo)
    text = (paper_repo / "paper.json").read_text()
    assert text == serialize(empty_spec())
    spec, errors = parse_spec(text)
    assert errors == [] and spec == empty_spec()
    assert json.loads(text)["message"]["notes"] == ""


# --------------------------------------------------------------------------- serializer


def test_serialize_is_deterministic_and_round_trips() -> None:
    spec = sample_spec()
    text = serialize(spec)
    assert text == serialize(spec)
    assert text.endswith("}\n") and not text.endswith("\n\n")
    reparsed, errors = parse_spec(text)
    assert errors == [] and reparsed == spec
    assert serialize(reparsed) == text
    assert json.loads(text) == spec.model_dump(mode="json")


def test_serialize_sorts_keys_everywhere() -> None:
    orders: list[list[str]] = []

    def hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        orders.append([k for k, _ in pairs])
        return dict(pairs)

    json.loads(serialize(sample_spec()), object_pairs_hook=hook)
    assert orders and all(keys == sorted(keys) for keys in orders)


def test_serialize_layout_one_node_per_line() -> None:
    spec = sample_spec()
    lines = serialize(spec).splitlines()
    assert lines[0] == "{" and lines[-1] == "}"
    assert lines[1] == '  "claims": ['
    # every leaf node (claims, message, paragraphs) is exactly one line that parses on its own
    leaves = {c.id: c for c in spec.claims}
    leaves.update({p.id: p for s in spec.tree.children for p in s.children})
    for node_id, node in leaves.items():
        hits = [line for line in lines if f'"id": "{node_id}"' in line]
        assert len(hits) == 1, node_id
        parsed = json.loads(hits[0].strip().rstrip(","))
        assert parsed == node.model_dump(mode="json")
    message_line = next(line for line in lines if line.startswith('  "message": '))
    assert json.loads(message_line.split(": ", 1)[1].rstrip(",")) == spec.message.model_dump()
    # indentation is two spaces per level
    assert any(line.startswith("    {") for line in lines)  # claims / sections
    assert any(line.startswith('      {"anchor"') for line in lines)  # paragraphs
    assert lines[-2] == '  "version": 1'


def test_serialize_keeps_unicode_and_escapes_line_separators() -> None:
    spec = empty_spec()
    spec.message.says = "Résumé — naïve next\nline"
    text = serialize(spec)
    assert "Résumé — naïve" in text and " " not in text and "\\u2028" in text
    assert parse_spec(text)[0] == spec


def changed_lines(before: str, after: str) -> tuple[int, int]:
    removed = added = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        removed += line.startswith("-")
        added += line.startswith("+")
    return removed, added


def test_one_field_change_is_a_one_line_diff() -> None:
    spec = sample_spec()
    before = serialize(spec)
    edits: list[Breaker] = [
        lambda s: setattr(s.tree.children[0].children[1], "says", "Warm pools cost memory."),
        lambda s: s.tree.children[0].children[1].fields.update(function="motivation"),
        lambda s: setattr(s.tree.children[0], "says", "The intro promises X."),  # has children
        lambda s: s.tree.children[1].claims.append("c1"),  # link a claim
        lambda s: setattr(s.message, "says", "New thesis."),
        lambda s: setattr(s.claims[0], "notes", "check Fig. 3"),
        lambda s: setattr(s.tree.children[1].children[0], "notes", "multi\nline"),
    ]
    for edit in edits:
        changed = spec.model_copy(deep=True)
        edit(changed)
        assert validate(changed) == []
        assert changed_lines(before, serialize(changed)) == (1, 1)


def test_structural_changes_touch_one_or_two_lines() -> None:
    spec = sample_spec()
    before = serialize(spec)
    inserted = spec.model_copy(deep=True)
    inserted.tree.children[0].children.insert(1, paragraph("p-4"))
    assert changed_lines(before, serialize(inserted)) == (0, 1)
    appended = spec.model_copy(deep=True)
    appended.tree.children[0].children.append(paragraph("p-4"))  # previous line gains a comma
    assert changed_lines(before, serialize(appended)) == (1, 2)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_diff_of_one_field_change_is_one_line(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    spec = sample_spec()
    git("init", "-q")
    (tmp_path / "paper.json").write_text(serialize(spec))
    git("add", "paper.json")
    git("commit", "-qm", "spec")
    spec.tree.children[0].children[0].says = "Cold starts dominate tail latency."
    (tmp_path / "paper.json").write_text(serialize(spec))
    assert git("diff", "--numstat").split() == ["1", "1", "paper.json"]
