"""Pass prompts (PLAN.md "Prompt templates", "Review pass"; phase-4 brief "Decisions").

- Passes are `.rewrite/prompts/passes/*.md`: YAML front matter (`name, level, scope,
  allow_rewrites, model, thinking`) and a Jinja2 body. They are re-read when a file's
  mtime or size changes, so editing a pass and re-running is the whole workflow.
- `assemble_prompt` builds the exact text a run sends on stdin: `system.md`, then the pass
  body rendered with `spec_outline, spec_json, source, scope_name, claims, findings_schema`.
- The scoped source has `\\author{...}` (and affiliation/email/institute) blocks replaced by
  `\\author{[removed]}` padded with the same line breaks, then every line is numbered as
  `"{n:>4} | {line}"`, so the numbers match the editor.
- `ensure_sandbox` rewrites `.rewrite/sandbox/CLAUDE.md` and `AGENTS.md` from
  `system.md` when they differ: the only instruction files the CLIs find in their cwd.
- `assemble_compare_prompt` (phase 5) builds the blind A/B prompt from the `ab-compare`
  pass: the same system.md and variables, the outline reduced to the entries that matter
  for one paragraph (`reduced_spec`), plus `version_a` and `version_b`.
- Phase 6: a pass has a `kind` (front matter, default `review`). Only review passes run
  through POST /api/runs; `triage` passes are rendered by `assemble_triage_prompt` (the
  one place earlier findings enter a prompt, as the `findings` variable) and `compare`
  passes by `assemble_compare_prompt`. Scope kind `ends` is the abstract plus the first
  and last linked sections. A fan-out member (`fanout=True`) gets a short note before the
  pass body saying it reviews one section of a longer paper, unless the pass body uses
  the `fanout` variable itself.

This module only reads the spec (through app.spec.load_spec); it never writes it.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import jinja2
import jinja2.meta
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from app import anchors, skeleton
from app.config import ConfigError
from app.files import (
    NotTextError,
    PathEscapeError,
    TooLargeError,
    atomic_write_text,
    is_protected_for_write,
    normalize_rel,
    read_text_file,
    resolve_tex_tree,
    strip_tex_comment,
)
from app.findings import findings_schema_text
from app.repo import Repo, sandbox_text
from app.spec import LEVEL_FIELDS, Node, PaperSpec, find_tree_node, load_spec

log = logging.getLogger(__name__)

ScopeKind = Literal["paper", "section", "file", "selection", "ends"]
SCOPE_KINDS: tuple[str, ...] = ("paper", "section", "file", "selection", "ends")
PassLevel = Literal["message", "claim", "section", "paragraph", "sentence"]
PASS_LEVELS: tuple[str, ...] = ("message", "claim", "section", "paragraph", "sentence")
PassKind = Literal["review", "triage", "compare"]
PASS_KINDS: tuple[str, ...] = ("review", "triage", "compare")
# Every pass may use these. `fanout` is true only for the members of a per-section fan-out
# (phase 6); a pass that does not use it gets FANOUT_NOTE in front of its body instead.
TEMPLATE_VARS = frozenset(
    {"spec_outline", "spec_json", "source", "scope_name", "claims", "findings_schema", "fanout"}
)
# The blind A/B compare (phase 5) also supplies these; a pass that uses them is not a
# review pass (GET /api/passes lists it as not runnable).
COMPARE_VARS = frozenset({"version_a", "version_b"})
COMPARE_PASS = "ab-compare"
# The triage call (phase 6) also supplies `findings`: the triaged run's open findings.
TRIAGE_VARS = frozenset({"findings"})
TRIAGE_PASS = "triage"
KIND_VARS: dict[str, frozenset[str]] = {
    "review": TEMPLATE_VARS,
    "triage": TEMPLATE_VARS | TRIAGE_VARS,
    "compare": TEMPLATE_VARS | COMPARE_VARS,
}
FANOUT_NOTE = (
    "Note: this request is one part of a per-section review of a longer paper. The spec "
    "outline covers the whole paper and is there for context; the source below holds only "
    "{scope_name}. Report problems in this source only, and do not report something as "
    "missing because another section of the paper covers it."
)
SANDBOX_FILES = ("CLAUDE.md", "AGENTS.md")
SELECTION_CONTEXT = 3  # lines of context shown before and after a selection
DESCRIPTION_MAX = 120


class PromptError(ValueError):
    """A pass, scope or prompt that cannot be used (→ HTTP 400)."""


# --------------------------------------------------------------------------- passes


class PassMeta(BaseModel):
    """The YAML front matter of a pass file."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: PassKind = "review"  # phase 6: only review passes run through POST /api/runs
    level: PassLevel
    scope: ScopeKind = "section"
    allow_rewrites: bool = False
    model: str | None = None  # null → the config default for this level
    thinking: Literal["low", "medium", "high"] | None = None


class PassInfo(BaseModel):
    """A pass as GET /api/passes shows it."""

    name: str
    level: str
    scope: str  # default scope kind
    allow_rewrites: bool
    model: str | None
    thinking: str | None
    file: str  # repo-relative path of the pass file
    description: str  # first body line (or the leading {# comment #}), ≤ 120 characters
    error: str | None = None  # bad front matter or template; the pass is not runnable
    runnable: bool = True  # a review pass without errors (POST /api/runs accepts it)
    kind: PassKind = "review"  # review | triage | compare


@dataclass(frozen=True)
class PassDef:
    info: PassInfo
    text: str  # the whole file, hashed into the cache key
    template: jinja2.Template | None  # None when the pass has an error
    variables: frozenset[str] = frozenset()  # every variable the body uses


_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.DOTALL)
_LEADING_COMMENT_RE = re.compile(r"\A\s*\{#-?(.*?)-?#\}", re.DOTALL)
_ENV = jinja2.Environment(
    undefined=jinja2.StrictUndefined, keep_trailing_newline=True, autoescape=False
)
_PASS_CACHE: dict[Path, tuple[int, int, PassDef]] = {}


def describe(body: str) -> str:
    """The first non-empty line of the body (Markdown heading marks dropped). A body that
    starts with a `{# ... #}` comment is described by the comment's first line instead."""
    match = _LEADING_COMMENT_RE.match(body)
    text = match.group(1) if match else body
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= DESCRIPTION_MAX else line[: DESCRIPTION_MAX - 1] + "…"
    return ""


def _error_pass(rel: str, stem: str, text: str, error: str) -> PassDef:
    info = PassInfo(
        name=stem,
        level="",
        scope="",
        allow_rewrites=False,
        model=None,
        thinking=None,
        file=rel,
        description="",
        error=error,
        runnable=False,
    )
    return PassDef(info=info, text=text, template=None)


def parse_pass(text: str, rel: str, stem: str) -> PassDef:
    """Parse one pass file. Errors are reported on the result, never raised."""
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        return _error_pass(rel, stem, text, "missing YAML front matter (--- ... ---)")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return _error_pass(rel, stem, text, f"front matter is not valid YAML: {exc}")
    if not isinstance(data, dict):
        return _error_pass(rel, stem, text, "front matter must be a YAML mapping")
    try:
        meta = PassMeta.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        return _error_pass(rel, stem, text, f"front matter: {loc}: {first['msg']}")
    if meta.model == "":
        meta.model = None
    body = match.group(2)
    info = PassInfo(
        name=meta.name,
        level=meta.level,
        scope=meta.scope,
        allow_rewrites=meta.allow_rewrites,
        model=meta.model,
        thinking=meta.thinking,
        file=rel,
        description=describe(body),
        kind=meta.kind,
        runnable=meta.kind == "review",
    )
    try:
        template = _ENV.from_string(body)
        variables = frozenset(jinja2.meta.find_undeclared_variables(_ENV.parse(body)))
    except jinja2.TemplateSyntaxError as exc:
        info.error = f"template error on body line {exc.lineno}: {exc.message}"
        info.runnable = False
        return PassDef(info=info, text=text, template=None)
    unknown = sorted(variables - KIND_VARS[meta.kind])
    if unknown:
        if meta.kind == "review":
            info.error = (
                f"uses variables a review run does not provide: {', '.join(unknown)} "
                "(a pass for another feature, e.g. the blind A/B compare)"
            )
        else:
            info.error = f"uses variables a {meta.kind} pass does not get: {', '.join(unknown)}"
        info.runnable = False
    return PassDef(info=info, text=text, template=template, variables=variables)


def _load_pass_file(repo: Repo, path: Path) -> PassDef:
    rel = path.relative_to(repo.root).as_posix()
    try:
        stat = path.stat()
    except OSError as exc:
        return _error_pass(rel, path.stem, "", f"cannot read: {exc}")
    cached = _PASS_CACHE.get(path)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _error_pass(rel, path.stem, "", f"cannot read: {exc}")
    parsed = parse_pass(text, rel, path.stem)
    _PASS_CACHE[path] = (stat.st_mtime_ns, stat.st_size, parsed)
    return parsed


def load_passes(repo: Repo) -> list[PassDef]:
    """Every pass file, coarse to fine (level order, then name); broken ones last."""
    directory = repo.prompts_dir / "passes"
    paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
    passes = [_load_pass_file(repo, path) for path in paths]
    seen: set[str] = set()
    out: list[PassDef] = []
    for item in passes:
        if item.info.error is None and item.info.name in seen:
            info = item.info.model_copy(
                update={"error": f"duplicate pass name {item.info.name!r}", "runnable": False}
            )
            item = PassDef(info=info, text=item.text, template=None)
        seen.add(item.info.name)
        out.append(item)

    def order(item: PassDef) -> tuple[int, int, int, str]:
        level = item.info.level
        rank = PASS_LEVELS.index(level) if level in PASS_LEVELS else len(PASS_LEVELS)
        broken = 0 if item.info.error is None else 1
        return (0 if item.info.runnable else 1, broken, rank, item.info.name)

    return sorted(out, key=order)


_KIND_FEATURE = {"triage": "the triage call", "compare": "the blind A/B compare"}


def get_pass(repo: Repo, name: str) -> PassDef:
    """A runnable review pass by name (PromptError otherwise)."""
    for item in load_passes(repo):
        if item.info.name == name:
            if item.info.error is None and item.info.kind != "review":
                feature = _KIND_FEATURE[item.info.kind]
                raise PromptError(
                    f"pass {name!r} cannot run as a review: it is a {item.info.kind} pass "
                    f"(used by {feature})"
                )
            if not item.info.runnable or item.template is None:
                raise PromptError(f"pass {name!r} cannot run: {item.info.error}")
            return item
    raise PromptError(f"unknown pass {name!r}")


def get_triage_pass(repo: Repo, name: str = TRIAGE_PASS) -> PassDef:
    """The triage pass: front matter `kind: triage`, a body that renders with the review
    variables plus `findings` and shows both the findings and the source (PromptError
    otherwise)."""
    for item in load_passes(repo):
        if item.info.name != name:
            continue
        if item.info.kind != "triage":
            raise PromptError(f"pass {name!r} is not a triage pass (front matter `kind: triage`)")
        if item.template is None or item.info.error is not None:
            raise PromptError(f"pass {name!r} cannot be used: {item.info.error}")
        missing = sorted({"findings", "source"} - item.variables)
        if missing:
            raise PromptError(
                f"pass {name!r} must show the findings and the source; missing {missing}"
            )
        return item
    raise PromptError(
        f"unknown pass {name!r}: this repo has no triage pass (run `rewrite init` to add "
        "the default .rewrite/prompts/passes/triage.md)"
    )


def get_compare_pass(repo: Repo, name: str = COMPARE_PASS) -> PassDef:
    """The blind A/B pass: it must render with the review variables plus `version_a` and
    `version_b`, and use both versions (PromptError otherwise). Its front matter says
    `kind: compare` (a pass file from before phase 6 has no kind and still works)."""
    for item in load_passes(repo):
        if item.info.name != name:
            continue
        if item.info.kind == "triage":
            raise PromptError(f"pass {name!r} is a triage pass, not a comparison")
        if item.template is None:
            raise PromptError(f"pass {name!r} cannot be used: {item.info.error}")
        unknown = sorted(item.variables - TEMPLATE_VARS - COMPARE_VARS)
        if unknown:
            raise PromptError(f"pass {name!r} uses unknown variables: {', '.join(unknown)}")
        missing = sorted(COMPARE_VARS - item.variables)
        if missing:
            raise PromptError(f"pass {name!r} must show both versions; missing {missing}")
        return item
    raise PromptError(f"unknown pass {name!r}")


def load_system(repo: Repo) -> str:
    path = repo.prompts_dir / "system.md"
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptError(f"cannot read .rewrite/prompts/system.md: {exc}") from exc


def ensure_sandbox(repo: Repo, system_text: str | None = None) -> list[str]:
    """Make `.rewrite/sandbox/` hold CLAUDE.md and AGENTS.md generated from system.md.

    Files are rewritten only when their content differs. Returns the names written. Other
    files found there are reported in the log, not deleted."""
    system_text = load_system(repo) if system_text is None else system_text
    sandbox = repo.sandbox_dir
    sandbox.mkdir(parents=True, exist_ok=True)
    want = sandbox_text(system_text)
    written: list[str] = []
    for name in SANDBOX_FILES:
        target = sandbox / name
        try:
            current = target.read_text(encoding="utf-8") if target.is_file() else None
        except (OSError, UnicodeDecodeError):
            current = None
        if current != want:
            atomic_write_text(target, want)
            written.append(name)
    stray = sorted(p.name for p in sandbox.iterdir() if p.name not in SANDBOX_FILES)
    if stray:
        log.warning("unexpected files in %s: %s", sandbox, ", ".join(stray))
    return written


# --------------------------------------------------------------------------- outline


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _says(node: Node) -> str:
    return _one_line(node.says) or "(not stated yet)"


def _anchor_note(node: Node) -> str:
    if node.anchor is None:
        return " [planned: no text yet]"
    if node.level == "paragraph":
        return f" [marker `% {node.anchor.marker}` in {node.anchor.file}]"
    return f" [`{node.anchor.marker}` in {node.anchor.file}]"


def _detail_lines(node: Node, indent: str) -> list[str]:
    lines = []
    for key in LEVEL_FIELDS.get(node.level, ()):
        value = _one_line(node.fields.get(key, ""))
        if value:
            lines.append(f"{indent}{key}: {value}")
    if node.claims:
        lines.append(f"{indent}claims: {', '.join(node.claims)}")
    return lines


def render_claims(spec: PaperSpec) -> str:
    """The claim bank, one claim per entry with its id, evidence and scope."""
    if not spec.claims:
        return "CLAIMS: (none yet)"
    lines = ["CLAIMS:"]
    for claim in spec.claims:
        lines.append(f"- {claim.id}: {_says(claim)}")
        lines.extend(_detail_lines(claim, "    "))
    return "\n".join(lines)


def render_outline(spec: PaperSpec) -> str:
    """The spec as an indented outline: the message with its fields, then sections with
    their promise and fields and their paragraphs nested under them, then the claim bank.
    Every entry carries its id so findings can point at it (`json_ref`)."""
    message = spec.message
    lines = [f"MESSAGE ({message.id}): {_says(message)}"]
    lines.extend(_detail_lines(message, "  "))
    lines.append("")
    if spec.tree.children:
        lines.append("SECTIONS (each line is the section's promise; paragraphs nested):")
        for section in spec.tree.children:
            lines.append(f"- section {section.id}{_anchor_note(section)}: {_says(section)}")
            lines.extend(_detail_lines(section, "    "))
            for paragraph in section.children:
                lines.append(
                    f"  - paragraph {paragraph.id}{_anchor_note(paragraph)}: {_says(paragraph)}"
                )
                lines.extend(_detail_lines(paragraph, "      "))
    else:
        lines.append("SECTIONS: (none yet)")
    lines.append("")
    lines.append(render_claims(spec))
    return "\n".join(lines)


def reduced_spec(spec: PaperSpec, node_id: str | None) -> PaperSpec:
    """The part of the spec a blind A/B comparison of one paragraph may see: the message,
    the paragraph's section with only that paragraph in it, and the claims either of them
    links. Without a node (a plain line range) only the message is kept."""
    found = find_tree_node(spec, node_id) if node_id else None
    tree = spec.tree.model_copy(update={"children": []})
    linked: list[str] = []
    if found is not None:
        node, parent = found
        if node.level == "paragraph" and parent.level == "section":
            section = parent.model_copy(update={"children": [node]})
        else:  # a section: keep it without its paragraphs
            section = node.model_copy(update={"children": []})
        tree = tree.model_copy(update={"children": [section]})
        for item in (section, *section.children):
            linked += [claim for claim in item.claims if claim not in linked]
    claims = [claim for claim in spec.claims if claim.id in linked]
    return spec.model_copy(update={"tree": tree, "claims": claims})


# --------------------------------------------------------------------------- source text

_AUTHOR_RE = re.compile(
    r"\\(?P<cmd>author|affiliation|email|institute)\*?(?![A-Za-z@])\s*(?:\[[^\]]*\]\s*)?\{"
)
_BREAK_RE = re.compile(r"\r\n|\r|\n")


def _closing_brace(text: str, open_pos: int) -> int | None:
    depth = 0
    i = open_pos
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def strip_authors(text: str) -> str:
    """Replace each `\\author{...}` (also `\\affiliation`, `\\email`, `\\institute`, with an
    optional `[...]` argument; braces balanced, may span lines) by `\\author{[removed]}`
    followed by the same line breaks, so every later line keeps its number. An unclosed
    group is removed to the end of its line."""
    out: list[str] = []
    pos = 0
    while True:
        match = _AUTHOR_RE.search(text, pos)
        if match is None:
            out.append(text[pos:])
            return "".join(out)
        open_pos = match.end() - 1
        close = _closing_brace(text, open_pos)
        if close is None:
            eol = _BREAK_RE.search(text, open_pos)
            end = eol.start() if eol else len(text)
        else:
            end = close + 1
        breaks = "".join(_BREAK_RE.findall(text, match.start(), end))
        out.append(text[pos : match.start()])
        out.append(f"\\{match.group('cmd')}{{[removed]}}{breaks}")
        pos = end


def number_lines(lines: list[str], start: int, end: int) -> list[str]:
    """`"{n:>4} | {line}"` for lines start..end (1-based, inclusive)."""
    return [f"{n:>4} | {lines[n - 1]}" for n in range(start, end + 1)]


def source_lines(text: str) -> list[str]:
    """Lines as the editor counts them (\\n, \\r\\n and \\r breaks)."""
    return skeleton.split_lines(text)


# --------------------------------------------------------------------------- scope


class Scope(BaseModel):
    """What a run reviews (API shape). Kind `ends` (phase 6) is the abstract environment of
    the main file plus the first and the last linked section, in document order."""

    kind: ScopeKind
    ref: str | None = None  # section: the section node id
    file: str | None = None  # file / selection: repo-relative path
    line_start: int | None = None  # selection (section: the resolved range, for display)
    line_end: int | None = None


@dataclass
class ResolvedScope:
    scope: Scope  # normalized
    name: str  # scope_name, e.g. 'section "Introduction" (introduction-1)'
    source: str  # the numbered, author-stripped source sent to the model
    files: dict[str, list[str]] = field(default_factory=dict)  # file → current lines


def _read_lines(repo: Repo, rel: str) -> tuple[str, list[str]]:
    """(normalized path, lines) of a repo source file (PromptError when unusable)."""
    path = normalize_rel(rel.strip())
    if is_protected_for_write(path) or path.split("/", 1)[0] == ".rewrite":
        raise PromptError(f"{rel} is not a source file of the paper")
    try:
        text, _ = read_text_file(repo.root, path)
    except PathEscapeError as exc:
        raise PromptError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise PromptError(f"file not found: {rel}") from exc
    except (NotTextError, TooLargeError, IsADirectoryError, NotADirectoryError) as exc:
        raise PromptError(f"cannot use {rel}: {exc}") from exc
    return path, source_lines(text)


def _stripped_lines(lines: list[str]) -> list[str]:
    stripped = source_lines(strip_authors("\n".join(lines)))
    # strip_authors keeps every line break, so the count matches (a trailing empty line
    # can only differ by the final newline, which "\n".join never adds).
    if len(stripped) < len(lines):
        stripped += [""] * (len(lines) - len(stripped))
    return stripped[: len(lines)]


def _main(repo: Repo) -> str:
    try:
        return repo.main
    except ConfigError as exc:
        raise PromptError(str(exc)) from exc


def _section_blocks(
    repo: Repo, scan: skeleton.DocScan, file: str, line: int
) -> tuple[list[str], dict[str, list[str]], int]:
    """The numbered blocks of the section whose heading is at file:line, one
    `=== file: <path> (lines a-b) ===` block per file segment in document order (the body
    may live in `\\input` files; see skeleton.section_segments), each numbered by its own
    file's lines so findings anchor. Returns (blocks, file → lines, last line of the
    section in the heading's own file)."""
    segments = skeleton.section_segments(scan, file, line) or [(file, line, line)]
    blocks: list[str] = []
    files: dict[str, list[str]] = {}
    read: dict[str, tuple[str, list[str]]] = {}  # scan path → (path, author-stripped lines)
    last_in_heading_file = line
    for rel, first, last in segments:
        if rel not in read:
            path, lines = _read_lines(repo, rel)
            files[path] = lines
            read[rel] = (path, _stripped_lines(lines))
        path, stripped = read[rel]
        last = min(last, len(stripped))
        if first > last:
            continue  # the file shrank since the scan
        header = f"=== file: {path} (lines {first}-{last}) ==="
        blocks.append("\n".join([header, *number_lines(stripped, first, last)]))
        if rel == file:
            last_in_heading_file = max(last_in_heading_file, last)
    return blocks, files, last_in_heading_file


def resolve_scope(repo: Repo, spec: PaperSpec, scope: Scope) -> ResolvedScope:
    """Turn a Scope into the numbered source the model sees (PromptError when it cannot be
    resolved: a planned or orphaned section, a missing file, a bad line range)."""
    if scope.kind == "paper":
        tree = resolve_tex_tree(repo.root, _main(repo))
        tex = [f.path for f in tree.files if f.kind == "tex"]
        if not tex:
            raise PromptError(f"the main file {tree.main} was not found")
        files: dict[str, list[str]] = {}
        blocks = []
        for rel in tex:
            path, lines = _read_lines(repo, rel)
            files[path] = lines
            numbered = number_lines(_stripped_lines(lines), 1, len(lines))
            blocks.append("\n".join([f"=== file: {path} ===", *numbered]))
        return ResolvedScope(
            scope=Scope(kind="paper"),
            name="the whole paper",
            source="\n\n".join(blocks),
            files=files,
        )

    if scope.kind == "section":
        if not scope.ref:
            raise PromptError("a section scope needs `ref`: the section's spec id")
        found = find_tree_node(spec, scope.ref)
        if found is None:
            raise PromptError(f"unknown section id {scope.ref!r}")
        node = found[0]
        if node.level != "section":
            raise PromptError(f"{scope.ref!r} is a {node.level}, not a section")
        if node.anchor is None:
            raise PromptError(f"section {scope.ref!r} is planned (no heading in the source yet)")
        scan = skeleton.scan_document(repo.root, _main(repo))
        where = anchors.resolve(node, scan)
        if where is None:
            raise PromptError(
                f"section {scope.ref!r} is orphaned: {node.anchor.marker} was not found"
            )
        file, line = where
        blocks, files, end = _section_blocks(repo, scan, file, line)
        heading = scan.heading_at(file, line)
        title = heading.heading if heading is not None else node.anchor.marker
        return ResolvedScope(
            scope=Scope(
                kind="section",
                ref=scope.ref,
                file=normalize_rel(file),
                line_start=line,
                line_end=end,
            ),
            name=f'section "{title}" ({scope.ref})',
            source="\n\n".join(blocks),
            files=files,
        )

    if scope.kind == "ends":
        return _resolve_ends(repo, spec)

    if scope.kind == "file":
        rel = scope.file or scope.ref
        if not rel:
            raise PromptError("a file scope needs `file`")
        path, lines = _read_lines(repo, rel)
        if not lines:
            raise PromptError(f"{path} is empty")
        numbered = number_lines(_stripped_lines(lines), 1, len(lines))
        return ResolvedScope(
            scope=Scope(kind="file", file=path),
            name=f"file {path}",
            source="\n".join([f"=== file: {path} ===", *numbered]),
            files={path: lines},
        )

    # selection
    if not scope.file or scope.line_start is None or scope.line_end is None:
        raise PromptError("a selection scope needs `file`, `line_start` and `line_end`")
    path, lines = _read_lines(repo, scope.file)
    start, end = scope.line_start, scope.line_end
    if start < 1 or end < start or start > len(lines):
        raise PromptError(f"lines {start}-{end} are not a range in {path} ({len(lines)} lines)")
    end = min(end, len(lines))
    stripped = _stripped_lines(lines)
    before_from = max(1, start - SELECTION_CONTEXT)
    after_to = min(len(lines), end + SELECTION_CONTEXT)
    parts = [
        f"=== file: {path} (selected lines {start}-{end}; review only the selection, "
        "the context lines are there for orientation) ==="
    ]
    if before_from < start:
        parts += ["--- context before ---", *number_lines(stripped, before_from, start - 1)]
    parts += ["--- selection ---", *number_lines(stripped, start, end)]
    if end < after_to:
        parts += ["--- context after ---", *number_lines(stripped, end + 1, after_to)]
    return ResolvedScope(
        scope=Scope(kind="selection", file=path, line_start=start, line_end=end),
        name=f"lines {start}-{end} of {path}",
        source="\n".join(parts),
        files={path: lines},
    )


_BEGIN_ABSTRACT_RE = re.compile(r"\\begin\s*\{abstract\}")
_END_ABSTRACT_RE = re.compile(r"\\end\s*\{abstract\}")


def abstract_range(lines: list[str]) -> tuple[int, int] | None:
    """(first, last) line of the first `abstract` environment (comments ignored), from its
    `\\begin` line to its `\\end` line; None when there is none or it is not closed."""
    start = None
    for number, line in enumerate(lines, start=1):
        active = strip_tex_comment(line)
        if start is None and _BEGIN_ABSTRACT_RE.search(active):
            start = number
        if start is not None and _END_ABSTRACT_RE.search(active):
            return start, number
    return None


def linked_sections(repo: Repo, spec: PaperSpec) -> list[tuple[Node, str, int]]:
    """The spec's section nodes whose heading resolves in the source, as (node, file, heading
    line), in document order. Planned and orphaned sections are left out."""
    scan = skeleton.scan_document(repo.root, _main(repo))
    found: list[tuple[tuple[int, int], Node, str, int]] = []
    for index, node in enumerate(spec.tree.children):
        if node.level != "section" or node.anchor is None:
            continue
        where = anchors.resolve(node, scan)
        if where is None:
            continue
        ordinal = scan.ordinal(*where)
        order = (ordinal if ordinal is not None else len(scan.ordinals), index)
        found.append((order, node, *where))
    found.sort(key=lambda item: item[0])
    return [(node, file, line) for _, node, file, line in found]


def _resolve_ends(repo: Repo, spec: PaperSpec) -> ResolvedScope:
    """Scope `ends`: the abstract environment of the main file (when there is one), then the
    first and the last linked section (one section when only one is linked), resolved like
    scope `section` (in document order across includes), each block with its
    `=== file: ... (lines a-b) ===` header, as message-level passes need them."""
    sections = linked_sections(repo, spec)
    if not sections:
        raise PromptError(
            "the `ends` scope needs at least one linked section (adopt the paper's sections "
            "in the Spec view first)"
        )
    chosen = [sections[0]] if len(sections) == 1 else [sections[0], sections[-1]]
    scan = skeleton.scan_document(repo.root, _main(repo))
    files: dict[str, list[str]] = {}
    blocks: list[str] = []
    names: list[str] = []
    main_path, main_lines = _read_lines(repo, _main(repo))
    abstract = abstract_range(main_lines)
    if abstract is not None:
        first, last = abstract
        files[main_path] = main_lines
        numbered = number_lines(_stripped_lines(main_lines), first, last)
        blocks.append("\n".join([f"=== file: {main_path} (lines {first}-{last}) ===", *numbered]))
        names.append("the abstract")
    for node, file, line in chosen:
        section_blocks, section_files, _ = _section_blocks(repo, scan, file, line)
        heading = scan.heading_at(file, line)
        title = heading.heading if heading is not None else node.id
        for path, lines in section_files.items():
            files.setdefault(path, lines)
        blocks.extend(section_blocks)
        names.append(f'section "{title}" ({node.id})')
    name = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return ResolvedScope(
        scope=Scope(kind="ends"), name=name, source="\n\n".join(blocks), files=files
    )


# --------------------------------------------------------------------------- assembly


def sha256(*parts: str | bytes) -> str:
    """sha256 over length-prefixed parts (so ("ab", "c") and ("a", "bc") differ)."""
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8") if isinstance(part, str) else part
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
    return digest.hexdigest()


@dataclass
class AssembledPrompt:
    pass_def: PassDef
    resolved: ResolvedScope
    prompt: str  # exactly what goes on stdin
    system_text: str
    spec_text: str
    prompt_hash: str  # the pass file and system.md
    scope_hash: str  # the numbered, author-stripped source
    spec_hash: str  # the spec file text
    # "" for a plain run; "fanout" for a fan-out member, whose prompt differs (phase 6). It
    # is part of the cache key, so a member never reuses a plain run's answer or vice versa.
    variant: str = ""

    @property
    def scope(self) -> Scope:
        return self.resolved.scope

    @property
    def scope_name(self) -> str:
        return self.resolved.name

    def cache_key(self, runner: str, model: str | None, thinking: str | None) -> str:
        """sha256 over the pass file, system.md, the scoped source, the spec file, the
        runner, the model and the thinking level (phase-4 brief "Cache key"), plus the
        prompt variant when there is one."""
        return sha256(
            self.pass_def.text,
            self.system_text,
            self.resolved.source,
            self.spec_text,
            runner,
            model or "",
            thinking or "",
            *([self.variant] if self.variant else []),
        )


def load_spec_for_prompt(repo: Repo) -> tuple[PaperSpec, str]:
    spec, raw, errors = load_spec(repo)
    if spec is None:
        first = f": {errors[0].path or 'file'}: {errors[0].message}" if errors else ""
        raise PromptError(f"the spec file is missing or invalid; fix it in the Spec view{first}")
    return spec, raw


def render_pass(pass_def: PassDef, variables: dict[str, Any]) -> str:
    if pass_def.template is None:
        raise PromptError(f"pass {pass_def.info.name!r} cannot run: {pass_def.info.error}")
    try:
        return pass_def.template.render(**variables)
    except jinja2.TemplateError as exc:
        raise PromptError(f"pass {pass_def.info.name!r} failed to render: {exc}") from exc


def join_prompt(parts: Iterable[str]) -> str:
    return "\n\n".join(part.strip("\n") for part in parts if part.strip()) + "\n"


def compare_text(text: str) -> str:
    """A paragraph as a blind comparison shows it: authors stripped, LaTeX comments (which
    may carry notes such as "revised") removed, comment-only lines dropped, no numbers."""
    out = []
    for line in source_lines(strip_authors(text)):
        active = strip_tex_comment(line).rstrip()
        if active.strip():
            out.append(active)
        elif not line.strip():
            out.append("")
    return "\n".join(out).strip("\n")


def assemble_compare_prompt(
    repo: Repo,
    *,
    spec: PaperSpec,
    spec_text: str,
    node_id: str | None,
    scope: Scope,
    scope_name: str,
    version_a: str,
    version_b: str,
    answer_schema: str,
    pass_name: str = COMPARE_PASS,
) -> AssembledPrompt:
    """The prompt for one blind A/B comparison: system.md + the `ab-compare` body rendered
    with the reduced outline and the two versions in the order given. `source` is empty (a
    comparison has no numbered source) and `findings_schema` carries the verdict schema."""
    pass_def = get_compare_pass(repo, pass_name)
    system_text = load_system(repo)
    reduced = reduced_spec(spec, node_id)
    source = f"=== Version A ===\n{version_a}\n\n=== Version B ===\n{version_b}"
    variables = {
        "spec_outline": render_outline(reduced),
        "spec_json": reduced.model_dump_json(indent=2),
        "source": "",
        "scope_name": scope_name,
        "claims": render_claims(reduced),
        "findings_schema": answer_schema,
        "fanout": False,
        "version_a": version_a,
        "version_b": version_b,
    }
    body = render_pass(pass_def, variables)
    return AssembledPrompt(
        pass_def=pass_def,
        resolved=ResolvedScope(scope=scope, name=scope_name, source=source, files={}),
        prompt=join_prompt([system_text, body]),
        system_text=system_text,
        spec_text=spec_text,
        prompt_hash=sha256(pass_def.text, system_text),
        scope_hash=sha256(source),
        spec_hash=sha256(spec_text),
    )


def assemble_prompt(
    repo: Repo, pass_name: str, scope: Scope, *, fanout: bool = False
) -> AssembledPrompt:
    """The full prompt for one run: system.md + the rendered pass body. With `fanout` (a
    member of a per-section fan-out) the body is preceded by FANOUT_NOTE, unless the pass
    uses the `fanout` variable to word that itself."""
    pass_def = get_pass(repo, pass_name)
    system_text = load_system(repo)
    spec, spec_text = load_spec_for_prompt(repo)
    resolved = resolve_scope(repo, spec, scope)
    variables = {
        "spec_outline": render_outline(spec),
        "spec_json": spec_text.rstrip("\n"),
        "source": resolved.source,
        "scope_name": resolved.name,
        "claims": render_claims(spec),
        "findings_schema": findings_schema_text(),
        "fanout": fanout,
    }
    body = render_pass(pass_def, variables)
    parts = [system_text, body]
    if fanout and "fanout" not in pass_def.variables:
        parts.insert(1, FANOUT_NOTE.format(scope_name=resolved.name))
    return AssembledPrompt(
        pass_def=pass_def,
        resolved=resolved,
        prompt=join_prompt(parts),
        system_text=system_text,
        spec_text=spec_text,
        prompt_hash=sha256(pass_def.text, system_text),
        scope_hash=sha256(resolved.source),
        spec_hash=sha256(spec_text),
        variant="fanout" if fanout else "",
    )


def assemble_triage_prompt(
    repo: Repo, scope: Scope, findings_text: str, answer_schema: str
) -> AssembledPrompt:
    """The prompt for one triage call (phase 6): system.md + the `triage` pass rendered with
    the full outline, the triaged run's scope resolved against the current source, the run's
    open findings as `findings` and the verdict schema as `findings_schema`. This is the one
    prompt that contains earlier findings (PLAN.md "Rule enforcement": clean context)."""
    pass_def = get_triage_pass(repo)
    system_text = load_system(repo)
    spec, spec_text = load_spec_for_prompt(repo)
    resolved = resolve_scope(repo, spec, scope)
    variables = {
        "spec_outline": render_outline(spec),
        "spec_json": spec_text.rstrip("\n"),
        "source": resolved.source,
        "scope_name": resolved.name,
        "claims": render_claims(spec),
        "findings_schema": answer_schema,
        "fanout": False,
        "findings": findings_text,
    }
    body = render_pass(pass_def, variables)
    return AssembledPrompt(
        pass_def=pass_def,
        resolved=resolved,
        prompt=join_prompt([system_text, body]),
        system_text=system_text,
        spec_text=spec_text,
        prompt_hash=sha256(pass_def.text, system_text),
        scope_hash=sha256(resolved.source),
        spec_hash=sha256(spec_text),
        variant="triage",
    )
