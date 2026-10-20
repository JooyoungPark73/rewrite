"""Where spec nodes meet the LaTeX source: status dots, skeleton adoption, document order.

Status (PLAN.md "Spec editor", phase-3 brief "Stale"):
- planned:  the node has no anchor;
- orphaned: the anchor does not resolve in the include tree (or is ambiguous: a heading
            marker that matches several headings is not resolved to any of them);
- stale:    (paragraphs only) the text under the marker hashes differently from the
            baseline in state.db; a paragraph seen for the first time gets a baseline and
            is linked;
- linked:   otherwise.

Adoption writes the .tex (marker insertion, via app/skeleton.py) but never the spec; the
API layer hands the planned spec to app.spec.write_spec.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from app import db, skeleton
from app.files import normalize_rel, strip_tex_comment
from app.repo import Repo
from app.skeleton import DocScan, Heading, Paragraph
from app.spec import (
    ID_RE,
    Anchor,
    ConflictError,
    InvalidOpError,
    Node,
    NotFoundError,
    PaperSpec,
    empty_fields,
    find_tree_node,
    iter_tree,
    new_paragraph_id,
    new_section_id,
    paragraph_marker_id,
)

StatusValue = Literal["planned", "linked", "stale", "orphaned"]


class NodeStatus(BaseModel):
    status: StatusValue
    file: str | None  # where the anchor resolves (the anchor's file when orphaned)
    line: int | None  # 1-based: the heading line, or the paragraph's marker line
    # Why this node should be re-read after a spec change above it (see spec.reread_marks),
    # or null. Cleared by reconcile or by editing the node.
    reread: str | None = None


# --------------------------------------------------------------------------- resolution


def resolve(node: Node, scan: DocScan) -> tuple[str, int] | None:
    """(file, line) the node's anchor resolves to, or None (planned or orphaned)."""
    if node.anchor is None:
        return None
    if node.level == "section":
        return skeleton.find_section(scan, node.anchor.marker, node.anchor.file)
    marker_id = paragraph_marker_id(node)
    if marker_id is None:
        return None
    marker = skeleton.find_marker(scan, marker_id, node.anchor.file)
    return (marker.file, marker.line) if marker else None


def _find_sentence(scan: DocScan, file: str, sentence: str) -> int | None:
    """1-based line where the whitespace-collapsed `sentence` starts in `file`, or None.
    Comments are ignored; the match may span lines."""
    lines = scan.lines.get(file)
    if lines is None:
        return None
    needle = skeleton.collapse(sentence)
    if not needle:
        return None
    pieces: list[tuple[int, str]] = []
    for lineno, raw in enumerate(lines, start=1):
        text = skeleton.collapse(strip_tex_comment(raw))
        if text:
            pieces.append((lineno, text))
    joined = " ".join(text for _, text in pieces)
    at = joined.find(needle)
    if at < 0:
        return None
    offset = 0
    for lineno, text in pieces:
        if offset + len(text) >= at:
            return lineno
        offset += len(text) + 1
    return pieces[-1][0] if pieces else None


def claim_status(node: Node, scan: DocScan) -> NodeStatus:
    """A claim: planned without an anchor; linked when its sentence is in the file; stale
    when the file is in the include tree but the sentence is gone or reworded; orphaned when
    the file is not in the include tree."""
    if node.anchor is None:
        return NodeStatus(status="planned", file=None, line=None)
    if node.anchor.file not in scan.lines:
        return NodeStatus(status="orphaned", file=node.anchor.file, line=None)
    line = _find_sentence(scan, node.anchor.file, node.anchor.marker)
    if line is None:
        return NodeStatus(status="stale", file=node.anchor.file, line=None)
    return NodeStatus(status="linked", file=node.anchor.file, line=line)


def node_status(node: Node, scan: DocScan, conn: sqlite3.Connection) -> NodeStatus:
    if node.level == "claim":
        return claim_status(node, scan)
    if node.anchor is None:
        return NodeStatus(status="planned", file=None, line=None)
    where = resolve(node, scan)
    if where is None:
        return NodeStatus(status="orphaned", file=node.anchor.file, line=None)
    file, line = where
    if node.level != "paragraph":
        return NodeStatus(status="linked", file=file, line=line)
    marker = skeleton.find_marker(scan, paragraph_marker_id(node) or "", node.anchor.file)
    assert marker is not None
    current = skeleton.marker_hash(scan, marker)
    baseline = db.get_baseline(conn, node.id)
    if baseline is None:
        db.set_baseline(conn, node.id, current)
        baseline = current
    return NodeStatus(status="linked" if baseline == current else "stale", file=file, line=line)


def compute_status(repo: Repo, spec: PaperSpec, scan: DocScan) -> dict[str, NodeStatus]:
    """Status of every section, paragraph and claim node, keyed by id, with its re-read mark."""
    with db.connect(repo) as conn:
        out = {node.id: node_status(node, scan, conn) for node, _, _ in iter_tree(spec)}
        out.update({claim.id: claim_status(claim, scan) for claim in spec.claims})
        for node_id, reason in db.get_rereads(conn).items():
            if node_id in out:
                out[node_id].reread = reason
        return out


def reconcile(repo: Repo, spec: PaperSpec, scan: DocScan, node_id: str) -> NodeStatus:
    """ "I checked": for a paragraph, accept the current prose (reset its baseline); for every
    section, paragraph or claim, clear its re-read mark."""
    found = find_tree_node(spec, node_id)
    claim = next((c for c in spec.claims if c.id == node_id), None)
    if found is None and claim is None:
        if node_id == spec.message.id:
            raise InvalidOpError("the message has no status to reconcile")
        raise NotFoundError(f"unknown id {node_id!r}")
    node = claim if claim is not None else found[0]  # type: ignore[index]
    with db.connect(repo) as conn, db.transaction(conn):
        if node.level == "paragraph" and node.anchor is not None:
            marker = skeleton.find_marker(scan, paragraph_marker_id(node) or "", node.anchor.file)
            if marker is not None:
                db.set_baseline(conn, node.id, skeleton.marker_hash(scan, marker))
        db.clear_reread(conn, node.id)
        return node_status(node, scan, conn)


def reserved_ids(repo: Repo, scan: DocScan | None) -> set[str]:
    """Ids the generator must not hand out although the spec does not contain them: marker
    ids in the source (a deleted node's marker may still be there, or a collaborator's) and
    every id the spec editor has written on this machine or kept a baseline for."""
    ids = {m.id for m in scan.markers} if scan is not None else set()
    try:
        with db.connect(repo) as conn:
            ids |= db.recorded_ids(conn) | db.baseline_ids(conn)
    except (sqlite3.Error, OSError):  # ids still come from the spec and the source
        pass
    return ids


# --------------------------------------------------------------------------- skeleton view


@dataclass
class SkeletonView:
    sections: list[tuple[Heading, str | None]]  # heading, adopted node id
    paragraphs: list[Paragraph]


def skeleton_view(spec: PaperSpec | None, scan: DocScan) -> SkeletonView:
    adopted: dict[tuple[str, int], str] = {}
    if spec is not None:
        for node, _, _ in iter_tree(spec):
            if node.level == "section":
                where = resolve(node, scan)
                if where is not None:
                    adopted.setdefault(where, node.id)
    return SkeletonView(
        sections=[(h, adopted.get((h.file, h.line))) for h in scan.headings],
        paragraphs=list(scan.paragraphs),
    )


# --------------------------------------------------------------------------- document order


def _position(node: Node, scan: DocScan) -> float | None:
    where = resolve(node, scan)
    return None if where is None else scan.ordinal(*where)


def insertion_index(children: list[Node], scan: DocScan, position: float) -> int:
    """Where a node at document `position` goes among `children`: after the last child that
    resolves before it, else before the first child that resolves after it, else at the end.
    Children without a resolvable anchor keep their places."""
    placed = [
        (i, pos) for i, child in enumerate(children) if (pos := _position(child, scan)) is not None
    ]
    before = [i for i, pos in placed if pos < position]
    if before:
        return max(before) + 1
    if placed:
        return min(i for i, _ in placed)
    return len(children)


# --------------------------------------------------------------------------- adoption


def _tex_file(scan: DocScan, file: str) -> str:
    rel = normalize_rel(file)
    if rel not in scan.lines:
        raise InvalidOpError(f"{file} is not part of the document (not reached from the main file)")
    return rel


def adopt_section(
    spec: PaperSpec,
    scan: DocScan,
    file: str,
    line: int,
    says: str,
    reserved: set[str],
) -> tuple[PaperSpec, str, Heading]:
    """Add a section node for the heading at file:line under the tree root, at the index
    matching document order. Returns (new spec, new id, heading). Does not write."""
    rel = _tex_file(scan, file)
    heading = scan.heading_at(rel, line)
    if heading is None:
        raise InvalidOpError(f"no \\section or \\subsection heading at {rel}:{line}")
    for node, _, _ in iter_tree(spec):
        if node.level == "section" and resolve(node, scan) == (rel, line):
            raise ConflictError(f"this heading is already adopted as {node.id!r}")
    try:
        marker = skeleton.section_marker(scan, heading)
    except skeleton.AmbiguousHeadingError as exc:
        raise ConflictError(str(exc)) from exc
    spec = spec.model_copy(deep=True)
    new_id = new_section_id(spec, heading.heading, reserved)
    node = Node(
        id=new_id,
        level="section",
        says=says,
        fields=empty_fields("section"),
        anchor=Anchor(file=rel, marker=marker),
    )
    index = insertion_index(spec.tree.children, scan, heading.ordinal)
    spec.tree.children.insert(index, node)
    return spec, new_id, heading


@dataclass
class ParagraphAdoption:
    spec: PaperSpec  # the spec to write
    node_id: str
    paragraph: Paragraph  # the paragraph that receives the marker
    created: bool  # a new node (False: an existing node was re-linked)


def plan_paragraph_adoption(
    spec: PaperSpec,
    scan: DocScan,
    file: str,
    line_start: int,
    parent_id: str | None,
    node_id: str | None,
    says: str | None,
    reserved: set[str],
) -> ParagraphAdoption:
    """Decide which node gets a marker above the paragraph at file:line_start and where it
    goes in the tree. Does not write anything.

    `line_start` may be any line of the paragraph (or its marker line). With `node_id` of an
    existing paragraph node the node is re-linked (anchor set, tree position unchanged);
    otherwise a new paragraph node is added under `parent_id`, by default the adopted section
    whose heading precedes the paragraph in the same file (else in document order), else the
    last section; it goes at the index matching document order.
    """
    rel = _tex_file(scan, file)
    paragraph = scan.paragraph_at(rel, line_start)
    if paragraph is None:
        raise InvalidOpError(f"no paragraph at {rel}:{line_start}")
    spec = spec.model_copy(deep=True)

    existing: Node | None = None
    if node_id is not None:
        found = find_tree_node(spec, node_id)
        if found is not None:
            existing = found[0]
            if existing.level != "paragraph":
                raise InvalidOpError(f"{node_id!r} is a {existing.level}, not a paragraph")
        elif node_id == spec.message.id or any(c.id == node_id for c in spec.claims):
            raise InvalidOpError(f"{node_id!r} is not a paragraph node")
        elif not ID_RE.match(node_id):
            raise InvalidOpError(f"invalid id {node_id!r}")

    marked = paragraph.marker_id
    if marked is not None and marked != node_id:
        if find_tree_node(spec, marked) is not None:
            raise ConflictError(f"this paragraph is already adopted as {marked!r}")
        if node_id is not None:
            raise ConflictError(
                f"this paragraph carries the marker '% @p {marked}' that no node owns; "
                "delete that line first"
            )
        node_id = marked  # an unowned marker (its node was deleted): adopt under that id

    if existing is not None:
        existing.anchor = Anchor(file=rel, marker=skeleton.paragraph_marker(existing.id))
        if says is not None:
            existing.says = says
        return ParagraphAdoption(spec=spec, node_id=existing.id, paragraph=paragraph, created=False)

    new_id = node_id or new_paragraph_id(spec, reserved)
    if parent_id is None:
        parent = _default_parent(spec, scan, paragraph)
    else:
        found = find_tree_node(spec, parent_id)
        if found is None:
            raise NotFoundError(f"unknown parent id {parent_id!r}")
        parent = found[0]
        if parent.level != "section":
            raise InvalidOpError(f"parent {parent_id!r} is a {parent.level}, not a section")
    node = Node(
        id=new_id,
        level="paragraph",
        says=says or "",
        fields=empty_fields("paragraph"),
        anchor=Anchor(file=rel, marker=skeleton.paragraph_marker(new_id)),
    )
    # The marker goes directly above line_start: between the line before and the paragraph.
    position = paragraph.ordinal - 0.5
    parent.children.insert(insertion_index(parent.children, scan, position), node)
    return ParagraphAdoption(spec=spec, node_id=new_id, paragraph=paragraph, created=True)


def _default_parent(spec: PaperSpec, scan: DocScan, paragraph: Paragraph) -> Node:
    sections = spec.tree.children
    if not sections:
        raise InvalidOpError("adopt a section first: there is no section to put the paragraph in")
    located: list[tuple[Node, str, int, int]] = []
    for section in sections:
        where = resolve(section, scan)
        if where is None:
            continue
        ordinal = scan.ordinal(*where)
        if ordinal is not None and ordinal < paragraph.ordinal:
            located.append((section, where[0], where[1], ordinal))
    same_file = [item for item in located if item[1] == paragraph.file]
    if same_file:
        return max(same_file, key=lambda item: item[2])[0]
    if located:
        return max(located, key=lambda item: item[3])[0]
    return sections[-1]
