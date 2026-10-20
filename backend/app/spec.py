"""The paper spec (`paper.json` at the repo root): model, validator, serializer and edit ops.

`write_spec` is THE ONLY code path that writes paper.json during normal use. It is called
only by the spec editor endpoints (app/api/spec.py), which take their input from the human
using the editor, never from model output. `rewrite init` (app/repo.py) is the one other
writer, and it only creates the empty spec below. tests/test_invariants.py enforces this.

On-disk shape (PLAN.md "paper.json data model", phase-3 brief "Decisions fixed"):

    { "claims": [node], "message": node, "tree": node, "version": 1 }

Every node: { anchor, children, claims, fields, id, level, notes, says }. `message` is the
semantic card (children always []); `tree` is the structural root: level "message", the same
id as `message`, empty says/fields/claims/notes, no anchor, children = sections, whose
children are paragraphs. `claims` is a flat bank of claim nodes referenced by id.

The serialization is deterministic: sorted keys, 2-space indent, every node on one line
except that a non-empty `children` array is expanded one child per line. Changing one field
of one node therefore changes one line of the file.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import db, skeleton, specwatch
from app.config import ConfigError
from app.events import SpecChanged
from app.files import atomic_write_text
from app.repo import Repo

log = logging.getLogger(__name__)

Level = Literal["message", "claim", "section", "paragraph"]
TreeLevel = Literal["section", "paragraph"]

LEVEL_FIELDS: dict[str, tuple[str, ...]] = {
    "message": ("problem", "stakes", "thesis"),
    "claim": ("evidence", "scope"),
    "section": ("figure_story",),
    "paragraph": ("function",),
}

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
SLUG_MAX = 24
CLAIM_PREFIX = "c"
PARAGRAPH_PREFIX = "p"


# --------------------------------------------------------------------------- model


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str  # repo-relative posix path of the .tex file
    marker: str  # paragraph: "@p <id>"; section: "\\section{<heading>}" or "\\label{<label>}"


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    level: Level
    says: str = ""
    children: list[Node] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)
    anchor: Anchor | None = None
    notes: str = ""


class PaperSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    message: Node
    claims: list[Node] = Field(default_factory=list)
    tree: Node


class SpecError(BaseModel):
    path: str  # e.g. "tree.children[0].level"; "" for the whole document
    message: str
    line: int | None = None  # 1-based, for JSON syntax errors
    column: int | None = None


def empty_fields(level: str) -> dict[str, str]:
    return {key: "" for key in LEVEL_FIELDS[level]}


def empty_spec() -> PaperSpec:
    """The spec `rewrite init` writes when the repo has none."""
    message = Node(id="message", level="message", fields=empty_fields("message"))
    tree = Node(id="message", level="message")
    return PaperSpec(version=1, message=message, claims=[], tree=tree)


# --------------------------------------------------------------------------- errors


class SpecOpError(Exception):
    """An edit that cannot be applied; `status` is the HTTP status the API returns."""

    status = 400

    def __init__(self, detail: str, errors: list[SpecError] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors


class InvalidOpError(SpecOpError):
    status = 400


class NotFoundError(SpecOpError):
    status = 404


class ConflictError(SpecOpError):
    status = 409


class SpecInvalidError(SpecOpError):
    status = 422

    def __init__(self, errors: list[SpecError], detail: str | None = None) -> None:
        first = f"{errors[0].path}: {errors[0].message}" if errors else "invalid spec"
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        super().__init__(detail or f"invalid spec: {first}{more}", errors)


# --------------------------------------------------------------------------- traversal


def iter_tree(spec: PaperSpec) -> Iterator[tuple[Node, Node, str]]:
    """(node, parent, path) for every section and paragraph, depth-first in tree order."""

    def walk(parent: Node, path: str) -> Iterator[tuple[Node, Node, str]]:
        for index, child in enumerate(parent.children):
            child_path = f"{path}.children[{index}]"
            yield child, parent, child_path
            yield from walk(child, child_path)

    yield from walk(spec.tree, "tree")


def all_ids(spec: PaperSpec) -> set[str]:
    ids = {spec.message.id, spec.tree.id}
    ids.update(claim.id for claim in spec.claims)
    ids.update(node.id for node, _, _ in iter_tree(spec))
    return ids


def find_tree_node(spec: PaperSpec, node_id: str) -> tuple[Node, Node] | None:
    """(node, parent) for a section or paragraph id."""
    for node, parent, _ in iter_tree(spec):
        if node.id == node_id:
            return node, parent
    return None


def find_claim(spec: PaperSpec, claim_id: str) -> Node | None:
    return next((claim for claim in spec.claims if claim.id == claim_id), None)


def paragraph_marker_id(node: Node) -> str | None:
    """The marker id of a paragraph anchor ("@p <id>" → id)."""
    if node.anchor is None:
        return None
    m = PARAGRAPH_MARKER_RE.match(node.anchor.marker)
    return m.group("id") if m else None


# --------------------------------------------------------------------------- validation

PARAGRAPH_MARKER_RE = re.compile(r"^@p (?P<id>\S+)$")


CLAIM_ANCHOR_MAX = 400  # a claim's anchor is one sentence


def _anchor_file_error(file: str) -> str | None:
    if not file.strip():
        return "anchor.file is empty"
    if "\\" in file or "\x00" in file:
        return "anchor.file must be a repo-relative path with forward slashes"
    posix = PurePosixPath(file)
    if posix.is_absolute() or PureWindowsPath(file).drive:
        return "anchor.file must be repo-relative, not absolute"
    if any(part == ".." for part in posix.parts):
        return "anchor.file must not contain '..'"
    return None


def _validate_node(node: Node, path: str, errors: list[SpecError]) -> None:
    def err(suffix: str, message: str) -> None:
        errors.append(SpecError(path=f"{path}{suffix}", message=message))

    if not ID_RE.match(node.id):
        err(
            ".id",
            f"invalid id {node.id!r}: use letters, digits, '_', '.', ':' and '-' "
            "(starting with a letter or digit, at most 80 characters)",
        )
    allowed = LEVEL_FIELDS[node.level]
    for key in node.fields:
        if key not in allowed:
            err(
                f".fields.{key}",
                f"unknown field for level {node.level!r} (allowed: {', '.join(allowed)})",
            )
    seen: set[str] = set()
    for index, claim_id in enumerate(node.claims):
        if claim_id in seen:
            err(f".claims[{index}]", f"claim {claim_id!r} is linked twice")
        seen.add(claim_id)

    anchor = node.anchor
    if anchor is None:
        return
    if node.level == "message":
        err(".anchor", "a message node has no anchor; use null")
        return
    file_error = _anchor_file_error(anchor.file)
    if file_error:
        err(".anchor.file", file_error)
    marker = anchor.marker
    if node.level == "claim":
        # A claim's anchor is the sentence that states it (found by whitespace-collapsed match).
        if not marker.strip():
            err(".anchor.marker", "a claim anchor is the sentence that states it; it is empty")
        elif len(marker) > CLAIM_ANCHOR_MAX:
            err(
                ".anchor.marker",
                f"a claim anchor is one sentence (at most {CLAIM_ANCHOR_MAX} characters)",
            )
    elif node.level == "paragraph":
        if marker != skeleton.paragraph_marker(node.id):
            err(
                ".anchor.marker",
                f"a paragraph marker must be {skeleton.paragraph_marker(node.id)!r}",
            )
    elif not (skeleton.SECTION_MARKER_RE.match(marker) or skeleton.LABEL_MARKER_RE.match(marker)):
        err(
            ".anchor.marker",
            "a section marker must be \\section{<heading>}, \\subsection{<heading>} "
            "or \\label{<label>}",
        )


def validate(spec: PaperSpec) -> list[SpecError]:
    """Structural rules the pydantic model cannot express. Empty list = valid."""
    errors: list[SpecError] = []

    def err(path: str, message: str) -> None:
        errors.append(SpecError(path=path, message=message))

    if spec.version != 1:
        err("version", f"unsupported version {spec.version}; expected 1")

    message = spec.message
    if message.level != "message":
        err("message.level", "must be 'message'")
    if message.children:
        err("message.children", "must be [] (the structure lives in 'tree')")
    _validate_node(message, "message", errors)

    tree = spec.tree
    if tree.level != "message":
        err("tree.level", "the tree root must have level 'message'")
    if tree.id != message.id:
        err("tree.id", f"the tree root's id must equal message.id ({message.id!r})")
    if tree.says:
        err("tree.says", 'the tree root carries structure only; says must be ""')
    if tree.fields:
        err("tree.fields", "the tree root carries structure only; fields must be {}")
    if tree.claims:
        err("tree.claims", "the tree root carries structure only; claims must be []")
    if tree.anchor is not None:
        err("tree.anchor", "the tree root has no anchor; use null")
    if tree.notes:
        err("tree.notes", 'the tree root carries structure only; notes must be ""')

    claim_ids: set[str] = set()
    owners: dict[str, str] = {message.id: "message"}

    def claim_owner(node_id: str, path: str) -> None:
        if node_id in owners:
            err(f"{path}.id", f"duplicate id {node_id!r} (also at {owners[node_id]})")
        else:
            owners[node_id] = path

    for index, claim in enumerate(spec.claims):
        path = f"claims[{index}]"
        if claim.level != "claim":
            err(f"{path}.level", "claim bank entries must have level 'claim'")
        if claim.children:
            err(f"{path}.children", "a claim has no children")
        if claim.claims:
            err(f"{path}.claims", "a claim does not link other claims")
        _validate_node(claim, path, errors)
        claim_owner(claim.id, path)
        claim_ids.add(claim.id)

    for index, section in enumerate(tree.children):
        if section.level != "section":
            err(f"tree.children[{index}].level", "children of the tree root must be sections")
    for node, parent, path in iter_tree(spec):
        if parent is not tree and parent.level == "section" and node.level != "paragraph":
            err(f"{path}.level", "children of a section must be paragraphs")
        if node.level == "paragraph" and node.children:
            err(f"{path}.children", "a paragraph has no children")
        if node.level in ("message", "claim"):
            err(f"{path}.level", f"a {node.level} node cannot appear inside the tree")
        _validate_node(node, path, errors)
        claim_owner(node.id, path)

    for node, path in [(message, "message")] + [(n, p) for n, _, p in iter_tree(spec)]:
        for index, claim_id in enumerate(node.claims):
            if claim_id not in claim_ids:
                err(f"{path}.claims[{index}]", f"unknown claim {claim_id!r}")
    return errors


# --------------------------------------------------------------------------- parse


def _loc_path(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def parse_spec_data(data: Any) -> tuple[PaperSpec | None, list[SpecError]]:
    """Validate already-decoded JSON (model + structural rules)."""
    if not isinstance(data, dict):
        return None, [SpecError(path="", message="the spec must be a JSON object")]
    try:
        spec = PaperSpec.model_validate(data)
    except ValidationError as exc:
        return None, [
            SpecError(path=_loc_path(tuple(e["loc"])), message=e["msg"]) for e in exc.errors()
        ]
    errors = validate(spec)
    return (None, errors) if errors else (spec, [])


def parse_spec(raw: str) -> tuple[PaperSpec | None, list[SpecError]]:
    """Parse and validate the file text."""
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        error = SpecError(
            path="", message=f"invalid JSON: {exc.msg}", line=exc.lineno, column=exc.colno
        )
        return None, [error]
    except ValueError as exc:  # duplicate key
        return None, [SpecError(path="", message=f"invalid JSON: {exc}")]
    return parse_spec_data(data)


def load_spec(repo: Repo) -> tuple[PaperSpec | None, str, list[SpecError]]:
    """(spec or None, file text or "", errors). Never raises for a missing or broken file."""
    path = repo.paper_json_path
    if not path.is_file():
        return None, "", [SpecError(path="", message="paper.json not found at the repo root")]
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, "", [SpecError(path="", message=f"cannot read paper.json: {exc}")]
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        raw = data.decode("utf-8", errors="replace")
        return None, raw, [SpecError(path="", message="paper.json is not valid UTF-8")]
    spec, errors = parse_spec(raw)
    return spec, raw, errors


def require_spec(repo: Repo) -> PaperSpec:
    """The on-disk spec, or ConflictError (409) when it is missing or invalid."""
    spec, _, errors = load_spec(repo)
    if spec is None:
        raise ConflictError(
            "paper.json is missing or invalid; fix it in the Raw JSON tab first", errors
        )
    return spec


# --------------------------------------------------------------------------- serialize


def _inline(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))


def _members(obj: dict[str, Any]) -> str:
    return ", ".join(f"{_inline(key)}: {_inline(obj[key])}" for key in sorted(obj))


def _node_lines(node: dict[str, Any], indent: int, prefix: str, suffix: str) -> list[str]:
    pad = " " * indent
    children = node.get("children") or []
    if not children:
        return [f"{pad}{prefix}{_inline(node)}{suffix}"]
    before = {k: v for k, v in node.items() if k < "children"}
    after = {k: v for k, v in node.items() if k > "children"}
    head = _members(before)
    lines = [f'{pad}{prefix}{{{head}{", " if head else ""}"children": [']
    for index, child in enumerate(children):
        comma = "," if index < len(children) - 1 else ""
        lines += _node_lines(child, indent + 2, "", comma)
    tail = _members(after)
    lines.append(f"{pad}]{', ' if tail else ''}{tail}}}{suffix}")
    return lines


def serialize(spec: PaperSpec) -> str:
    """Deterministic file text (see the module docstring); ends with a newline."""
    data = spec.model_dump(mode="json")
    lines = ["{"]
    claims = data["claims"]
    if claims:
        lines.append('  "claims": [')
        for index, claim in enumerate(claims):
            lines += _node_lines(claim, 4, "", "," if index < len(claims) - 1 else "")
        lines.append("  ],")
    else:
        lines.append('  "claims": [],')
    lines += _node_lines(data["message"], 2, '"message": ', ",")
    lines += _node_lines(data["tree"], 2, '"tree": ', ",")
    lines.append(f'  "version": {_inline(data["version"])}')
    lines.append("}")
    text = "\n".join(lines) + "\n"
    # Line separators are legal inside JSON strings but some tools split lines on them.
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


# --------------------------------------------------------------------------- write


def write_spec(
    repo: Repo,
    spec: PaperSpec,
    publish: Callable[[SpecChanged], None] | None = None,
    *,
    rebaseline: Iterable[str] = (),
) -> str:
    """Validate, serialize and atomically write paper.json; returns the file text.

    THE ONLY writer of paper.json in normal use (see the module docstring). Unchanged
    content is not rewritten and emits nothing. After a write, paragraph nodes whose `says`
    or anchor changed get their stale baseline reset to the current paragraph text, and a
    `spec.changed` event is published. Ids in `rebaseline` are re-baselined even when the
    file did not change (adoption moved their marker). The text's hash is recorded in
    app/specwatch.py before the write, so the file watcher does not announce the editor's
    own save as an external change (phase 5).
    """
    errors = validate(spec)
    if errors:
        raise SpecInvalidError(errors)
    text = serialize(spec)
    path = repo.paper_json_path
    previous_text: str | None = None
    if path.is_file():
        try:
            previous_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            previous_text = None
    changed = previous_text != text
    force = set(rebaseline)
    # Before the write: the watcher must recognise this content as the editor's own.
    specwatch.remember(repo.root, text)
    if not changed and not force:
        return text
    previous = parse_spec(previous_text)[0] if previous_text is not None else None
    if changed:
        atomic_write_text(repo.paper_json_path, text)
    try:
        rebaseline_changed(repo, previous, spec, force)
        propagate_rereads(repo, previous, spec)
        with db.connect(repo) as conn:
            db.record_ids(conn, all_ids(spec))
    except Exception:  # the spec is written; a state.db hiccup only affects stale dots
        log.exception("could not update state.db after writing the spec")
    if changed and publish is not None:
        publish(SpecChanged())
    return text


def rebaseline_changed(
    repo: Repo,
    previous: PaperSpec | None,
    spec: PaperSpec,
    force: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Reset the baseline of every anchored paragraph that is new, re-anchored, or whose
    `says` changed since `previous`, plus those in `force`. Returns the node ids touched."""
    before = {node.id: node for node, _, _ in iter_tree(previous)} if previous else {}
    changed = [
        node
        for node, _, _ in iter_tree(spec)
        if node.level == "paragraph"
        and node.anchor is not None
        and (
            node.id in force
            or node.id not in before
            or before[node.id].says != node.says
            or before[node.id].anchor != node.anchor
            or before[node.id].level != node.level
        )
    ]
    if not changed:
        return []
    try:
        scan = skeleton.scan_document(repo.root, repo.main)
    except ConfigError:
        scan = None
    with db.connect(repo) as conn, db.transaction(conn):
        for node in changed:
            marker_id = paragraph_marker_id(node)
            assert node.anchor is not None
            marker = (
                skeleton.find_marker(scan, marker_id, node.anchor.file)
                if scan is not None and marker_id is not None
                else None
            )
            if marker is None or scan is None:
                db.delete_baseline(conn, node.id)
            else:
                db.set_baseline(conn, node.id, skeleton.marker_hash(scan, marker))
    return [node.id for node in changed]


# --------------------------------------------------------------------------- re-read marks


def _content(node: Node) -> tuple[str, tuple[tuple[str, str], ...]]:
    """What a dependent node is written against: says and fields (not links, notes, anchors)."""
    return node.says, tuple(sorted(node.fields.items()))


def _all_nodes(spec: PaperSpec | None) -> dict[str, Node]:
    if spec is None:
        return {}
    nodes = {spec.message.id: spec.message}
    nodes.update({c.id: c for c in spec.claims})
    nodes.update({n.id: n for n, _, _ in iter_tree(spec)})
    return nodes


def reread_marks(previous: PaperSpec | None, spec: PaperSpec) -> tuple[dict[str, str], set[str]]:
    """(marks, cleared): nodes to mark for re-reading after `previous` became `spec`, with a
    reason, and nodes whose own says/fields changed (their mark, if any, is cleared).

    A changed message marks every section; a changed section marks its paragraphs; a changed
    claim marks every section and paragraph that links it. Links, moves, notes and anchors
    change nothing. A node revised in this write is never marked by the same write."""
    before = _all_nodes(previous)
    marks: dict[str, str] = {}
    cleared: set[str] = set()
    if not before:
        return marks, cleared
    after = _all_nodes(spec)
    for node_id, node in after.items():
        prev = before.get(node_id)
        if prev is None or _content(prev) == _content(node):
            continue
        cleared.add(node_id)
        if node.level == "message":
            for section in spec.tree.children:
                marks[section.id] = "the message changed"
        elif node.level == "section":
            for paragraph in node.children:
                marks[paragraph.id] = f"the promise of {node.id} changed"
        elif node.level == "claim":
            for tree_node, _, _ in iter_tree(spec):
                if node_id in tree_node.claims:
                    marks[tree_node.id] = f"claim {node_id} changed"
    for node_id in cleared:
        marks.pop(node_id, None)
    return marks, cleared


def propagate_rereads(repo: Repo, previous: PaperSpec | None, spec: PaperSpec) -> dict[str, str]:
    """Record the re-read marks of this write in state.db; returns the marks set."""
    marks, cleared = reread_marks(previous, spec)
    with db.connect(repo) as conn, db.transaction(conn):
        for node_id in cleared:
            db.clear_reread(conn, node_id)
        for node_id, reason in marks.items():
            db.set_reread(conn, node_id, reason)
        db.prune_rereads(conn, set(_all_nodes(spec)))
    return marks


# --------------------------------------------------------------------------- ids


def slugify(text: str, max_len: int = SLUG_MAX, default: str = "section") -> str:
    """Lowercase ASCII slug of a heading: LaTeX commands dropped, accents stripped, runs of
    other characters turned into '-', at most `max_len` characters."""
    text = re.sub(r"\\[A-Za-z@]+\*?", " ", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or default


def next_id(prefix: str, taken: set[str], sep: str = "-") -> str:
    """`<prefix><sep><n>` with n one past the highest suffix already used with that prefix.

    Gaps are never filled, so deleting a node in the middle never recycles its id. `taken`
    should include ids that existed outside the spec (source markers, baselines) so a
    deleted node's id is not reused either.
    """
    pattern = re.compile(re.escape(prefix + sep) + r"(\d+)$")
    highest = max((int(m.group(1)) for i in taken if (m := pattern.match(i))), default=0)
    return f"{prefix}{sep}{highest + 1}"


def new_section_id(spec: PaperSpec, heading: str, reserved: set[str] | frozenset[str]) -> str:
    return next_id(slugify(heading), all_ids(spec) | set(reserved))


def new_paragraph_id(spec: PaperSpec, reserved: set[str] | frozenset[str]) -> str:
    return next_id(PARAGRAPH_PREFIX, all_ids(spec) | set(reserved))


def new_claim_id(spec: PaperSpec, reserved: set[str] | frozenset[str] = frozenset()) -> str:
    return next_id(CLAIM_PREFIX, all_ids(spec) | set(reserved), sep="")


# --------------------------------------------------------------------------- ops


class _OpBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UpdateOp(_OpBase):
    type: Literal["update"]
    id: str
    says: str | None = None
    fields: dict[str, str] | None = None  # merged key-wise; "" values are kept as ""
    notes: str | None = None


class InsertOp(_OpBase):
    type: Literal["insert"]
    parent_id: str  # message id (= tree root) for a section, a section id for a paragraph
    index: int
    level: TreeLevel
    says: str = ""


class DeleteOp(_OpBase):
    type: Literal["delete"]
    id: str


class MoveOp(_OpBase):
    type: Literal["move"]
    id: str
    parent_id: str
    index: int  # position in the new parent's children after the node is taken out


class SetLevelOp(_OpBase):
    type: Literal["set_level"]
    id: str
    level: TreeLevel


class LiftToClaimOp(_OpBase):
    type: Literal["lift_to_claim"]
    id: str


class LinkClaimOp(_OpBase):
    type: Literal["link_claim"]
    id: str
    claim_id: str


class UnlinkClaimOp(_OpBase):
    type: Literal["unlink_claim"]
    id: str
    claim_id: str


class AddClaimOp(_OpBase):
    type: Literal["add_claim"]
    says: str = ""
    fields: dict[str, str] | None = None
    # The sentence that states the claim ("Lift to claim" passes the selection).
    anchor: Anchor | None = None


class UpdateClaimOp(_OpBase):
    type: Literal["update_claim"]
    id: str
    says: str | None = None
    fields: dict[str, str] | None = None
    notes: str | None = None


class DeleteClaimOp(_OpBase):
    type: Literal["delete_claim"]
    id: str


class SetAnchorOp(_OpBase):
    type: Literal["set_anchor"]
    id: str
    anchor: Anchor | None


Op = Annotated[
    UpdateOp
    | InsertOp
    | DeleteOp
    | MoveOp
    | SetLevelOp
    | LiftToClaimOp
    | LinkClaimOp
    | UnlinkClaimOp
    | AddClaimOp
    | UpdateClaimOp
    | DeleteClaimOp
    | SetAnchorOp,
    Field(discriminator="type"),
]


def change_level(node: Node, level: Level) -> Node:
    """A copy of `node` at `level`: `says` kept, the old non-empty fields appended to `notes`
    as "<key>: <value>" lines, the new level's fields started empty. Children are kept."""
    displaced = [f"{key}: {value}" for key, value in sorted(node.fields.items()) if value]
    notes = node.notes
    if displaced:
        sep = "\n" if notes and not notes.endswith("\n") else ""
        notes = notes + sep + "\n".join(displaced)
    return node.model_copy(update={"level": level, "fields": empty_fields(level), "notes": notes})


def _editable(spec: PaperSpec, node_id: str) -> Node:
    """The message card, a section/paragraph, or a claim, by id (404 otherwise)."""
    if node_id == spec.message.id:
        return spec.message
    found = find_tree_node(spec, node_id)
    if found is not None:
        return found[0]
    claim = find_claim(spec, node_id)
    if claim is not None:
        return claim
    raise NotFoundError(f"unknown id {node_id!r}")


def _tree_node(spec: PaperSpec, node_id: str, action: str) -> tuple[Node, Node]:
    """A section or paragraph and its parent (404 unknown, 400 for message/claims)."""
    found = find_tree_node(spec, node_id)
    if found is not None:
        return found
    if node_id == spec.message.id:
        raise InvalidOpError(f"cannot {action} the message")
    if find_claim(spec, node_id) is not None:
        raise InvalidOpError(f"cannot {action} a claim with this op")
    raise NotFoundError(f"unknown id {node_id!r}")


def _container(spec: PaperSpec, parent_id: str) -> Node:
    """The tree root (by the message id) or a section/paragraph, as a parent."""
    if parent_id == spec.message.id:
        return spec.tree
    found = find_tree_node(spec, parent_id)
    if found is None:
        if find_claim(spec, parent_id) is not None:
            raise InvalidOpError("a claim cannot have children")
        raise NotFoundError(f"unknown parent id {parent_id!r}")
    return found[0]


def _check_child_level(parent: Node, level: str) -> None:
    want = "section" if parent.level == "message" else "paragraph"
    if parent.level not in ("message", "section"):
        raise InvalidOpError(f"a {parent.level} cannot have children")
    if level != want:
        where = "the message" if parent.level == "message" else f"section {parent.id!r}"
        raise InvalidOpError(f"children of {where} must be {want}s, not {level}s")


def _check_index(index: int, length: int) -> None:
    if not 0 <= index <= length:
        raise InvalidOpError(f"index {index} is out of range 0..{length}")


def _contains(node: Node, node_id: str) -> bool:
    return node.id == node_id or any(_contains(child, node_id) for child in node.children)


def _merge_fields(node: Node, fields: dict[str, str] | None) -> None:
    if fields:
        node.fields = {**node.fields, **fields}


def apply_op(
    spec: PaperSpec, op: Any, reserved: set[str] | frozenset[str] = frozenset()
) -> tuple[PaperSpec, str | None]:
    """Apply one op to a copy of `spec`; returns (new spec, new id or None).

    Raises NotFoundError (404) for unknown ids and InvalidOpError (400) for ops that make
    no sense. The result is not validated here; write_spec rejects an invalid result (422).
    `reserved` are ids that must not be generated (see next_id).
    """
    spec = spec.model_copy(deep=True)
    new_id: str | None = None

    if isinstance(op, UpdateOp):
        node = _editable(spec, op.id)
        if op.says is not None:
            node.says = op.says
        _merge_fields(node, op.fields)
        if op.notes is not None:
            node.notes = op.notes

    elif isinstance(op, InsertOp):
        parent = _container(spec, op.parent_id)
        _check_child_level(parent, op.level)
        _check_index(op.index, len(parent.children))
        if op.level == "section":
            new_id = new_section_id(spec, op.says, reserved)
        else:
            new_id = new_paragraph_id(spec, reserved)
        node = Node(id=new_id, level=op.level, says=op.says, fields=empty_fields(op.level))
        parent.children.insert(op.index, node)

    elif isinstance(op, DeleteOp):
        if op.id != spec.message.id and find_claim(spec, op.id) is not None:
            return apply_op(spec, DeleteClaimOp(type="delete_claim", id=op.id), reserved)
        node, parent = _tree_node(spec, op.id, "delete")
        _remove(parent.children, node)

    elif isinstance(op, MoveOp):
        node, parent = _tree_node(spec, op.id, "move")
        target = _container(spec, op.parent_id)
        if target is not spec.tree and _contains(node, target.id):
            raise InvalidOpError("cannot move a node into its own subtree")
        _check_child_level(target, node.level)
        _remove(parent.children, node)
        _check_index(op.index, len(target.children))
        target.children.insert(op.index, node)

    elif isinstance(op, SetLevelOp):
        node, parent = _tree_node(spec, op.id, "change the level of")
        if node.level != op.level:
            if op.level == "paragraph":
                if node.children:
                    raise InvalidOpError(
                        f"section {node.id!r} has paragraphs; move or delete them before "
                        "making it a paragraph"
                    )
                index = _index(spec.tree.children, node)
                if index == 0:
                    raise InvalidOpError(
                        "the first section has no section before it to become a paragraph of"
                    )
                new_parent = spec.tree.children[index - 1]
                _remove(spec.tree.children, node)
                new_parent.children.append(_relevel(node, "paragraph"))
            else:
                index = _index(spec.tree.children, parent)
                _remove(parent.children, node)
                spec.tree.children.insert(index + 1, _relevel(node, "section"))

    elif isinstance(op, LiftToClaimOp):
        node = _linkable(spec, op.id)
        new_id = new_claim_id(spec, reserved)
        claim = Node(id=new_id, level="claim", says=node.says, fields=empty_fields("claim"))
        spec.claims.append(claim)
        node.claims.append(new_id)

    elif isinstance(op, LinkClaimOp):
        node = _linkable(spec, op.id)
        if find_claim(spec, op.claim_id) is None:
            raise NotFoundError(f"unknown claim {op.claim_id!r}")
        if op.claim_id not in node.claims:
            node.claims.append(op.claim_id)

    elif isinstance(op, UnlinkClaimOp):
        node = _linkable(spec, op.id)
        if op.claim_id not in node.claims and find_claim(spec, op.claim_id) is None:
            raise NotFoundError(f"unknown claim {op.claim_id!r}")
        node.claims = [c for c in node.claims if c != op.claim_id]

    elif isinstance(op, AddClaimOp):
        new_id = new_claim_id(spec, reserved)
        claim = Node(id=new_id, level="claim", says=op.says, fields=empty_fields("claim"))
        _merge_fields(claim, op.fields)
        claim.anchor = op.anchor
        spec.claims.append(claim)

    elif isinstance(op, UpdateClaimOp):
        claim = _claim(spec, op.id)
        if op.says is not None:
            claim.says = op.says
        _merge_fields(claim, op.fields)
        if op.notes is not None:
            claim.notes = op.notes

    elif isinstance(op, DeleteClaimOp):
        claim = _claim(spec, op.id)
        _remove(spec.claims, claim)
        for node in [spec.message, *(n for n, _, _ in iter_tree(spec))]:
            node.claims = [c for c in node.claims if c != op.id]

    elif isinstance(op, SetAnchorOp):
        claim = find_claim(spec, op.id)
        if claim is not None:
            claim.anchor = op.anchor
        else:
            node, _ = _tree_node(spec, op.id, "anchor")
            node.anchor = op.anchor

    else:  # pragma: no cover - the discriminated union admits nothing else
        raise InvalidOpError(f"unknown op {op!r}")

    return spec, new_id


def _index(nodes: list[Node], node: Node) -> int:
    """Position by identity (pydantic `==` compares values)."""
    return next(i for i, candidate in enumerate(nodes) if candidate is node)


def _remove(nodes: list[Node], node: Node) -> None:
    del nodes[_index(nodes, node)]


def _relevel(node: Node, level: Level) -> Node:
    """change_level plus dropping the anchor: a paragraph marker cannot anchor a section and
    a heading cannot anchor a paragraph, so the node becomes planned."""
    moved = change_level(node, level)
    moved.anchor = None
    return moved


def _linkable(spec: PaperSpec, node_id: str) -> Node:
    """A node that may link claims: the message card, a section or a paragraph."""
    if node_id == spec.message.id:
        return spec.message
    found = find_tree_node(spec, node_id)
    if found is not None:
        return found[0]
    if find_claim(spec, node_id) is not None:
        raise InvalidOpError("a claim cannot link claims")
    raise NotFoundError(f"unknown id {node_id!r}")


def _claim(spec: PaperSpec, claim_id: str) -> Node:
    claim = find_claim(spec, claim_id)
    if claim is None:
        raise NotFoundError(f"unknown claim {claim_id!r}")
    return claim
