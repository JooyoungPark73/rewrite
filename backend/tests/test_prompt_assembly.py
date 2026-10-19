"""app/prompts.py: pass loading (front matter, Jinja body, hot reload), the outline, author
stripping, source numbering, scope resolution and prompt assembly."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import prompts
from app.findings import findings_schema_text
from app.prompts import (
    PromptError,
    Scope,
    assemble_prompt,
    describe,
    ensure_sandbox,
    load_passes,
    number_lines,
    parse_pass,
    render_outline,
    resolve_scope,
    strip_authors,
)
from app.repo import Repo, init_repo, sandbox_text
from app.spec import Anchor, Node, PaperSpec, empty_fields, load_spec
from tests.conftest import adopt_intro, make_client

GOOD = """---
name: demo
level: paragraph
scope: section
allow_rewrites: false
model: null
thinking: null
---
{# Demo pass for the tests. #}
Check {{ scope_name }}.
{{ source }}
{{ findings_schema }}
"""


def write_pass(repo: Path, name: str, text: str) -> Path:
    path = repo / ".rewrite" / "prompts" / "passes" / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- front matter


def test_parse_pass_reads_front_matter_and_description() -> None:
    parsed = parse_pass(GOOD, "p/demo.md", "demo")
    info = parsed.info
    assert (info.name, info.level, info.scope, info.allow_rewrites) == (
        "demo",
        "paragraph",
        "section",
        False,
    )
    assert info.model is None and info.thinking is None
    assert info.description == "Demo pass for the tests."
    assert info.error is None and info.runnable and parsed.template is not None


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("no front matter\n", "missing YAML front matter"),
        ("---\nname: [unclosed\n---\nbody\n", "not valid YAML"),
        ("---\n- a\n- b\n---\nbody\n", "mapping"),
        ("---\nname: x\nlevel: chapter\n---\nbody\n", "level"),
        ("---\nname: x\nlevel: paragraph\ncolour: red\n---\nbody\n", "colour"),
        ("---\nname: x\nlevel: paragraph\nthinking: extreme\n---\nbody\n", "thinking"),
        ("---\nname: x\nlevel: paragraph\n---\n{% if %}\n", "template error"),
        ("---\nname: x\nlevel: paragraph\n---\n{{ version_a }}\n", "version_a"),
    ],
)
def test_bad_passes_are_reported_not_raised(text: str, fragment: str) -> None:
    info = parse_pass(text, "p/x.md", "x").info
    assert info.error is not None and fragment in info.error
    assert info.runnable is False


def test_description_rules() -> None:
    assert describe("\n## Pass: clarity\nmore") == "Pass: clarity"
    assert describe("{#- a comment line\n second -#}\nbody") == "a comment line"
    long = describe("x" * 300)
    assert len(long) == 120 and long.endswith("…")


def test_default_passes_load_in_level_order(initialized_repo: Path) -> None:
    passes = load_passes(Repo.at(initialized_repo))
    names = [p.info.name for p in passes]
    levels = [p.info.level for p in passes if p.info.runnable]
    order = ("message", "claim", "section", "paragraph", "sentence")
    assert levels == sorted(levels, key=order.index)
    assert set(names) >= {"paragraph-function", "clarity", "latex-hygiene", "ab-compare"}
    ab = next(p for p in passes if p.info.name == "ab-compare")
    assert not ab.info.runnable and ab.info.kind == "compare" and ab.info.error is None
    triage = next(p for p in passes if p.info.name == "triage")
    assert not triage.info.runnable and triage.info.kind == "triage" and triage.info.error is None
    assert names[-2:] == ["triage", "ab-compare"]  # not runnable → listed last, by level
    for item in passes:
        assert item.info.description and len(item.info.description) <= 120


def test_hot_reload_by_mtime(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    path = write_pass(initialized_repo, "demo", GOOD)
    first = next(p for p in load_passes(repo) if p.info.name == "demo")
    assert first.info.level == "paragraph"
    assert next(p for p in load_passes(repo) if p.info.name == "demo") is first  # cached
    path.write_text(GOOD.replace("level: paragraph", "level: sentence"), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    again = next(p for p in load_passes(repo) if p.info.name == "demo")
    assert again.info.level == "sentence"


def test_duplicate_names_are_errors(initialized_repo: Path) -> None:
    write_pass(initialized_repo, "zz-copy", GOOD)
    write_pass(initialized_repo, "demo", GOOD)
    passes = [p for p in load_passes(Repo.at(initialized_repo)) if p.info.name == "demo"]
    assert len(passes) == 2
    assert sum(1 for p in passes if p.info.error and "duplicate" in p.info.error) == 1


def test_get_pass_errors(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    with pytest.raises(PromptError, match="unknown pass"):
        prompts.get_pass(repo, "nope")
    with pytest.raises(PromptError, match="cannot run"):
        prompts.get_pass(repo, "ab-compare")


# --------------------------------------------------------------------------- outline


def _spec() -> PaperSpec:
    message = Node(
        id="message",
        level="message",
        says="Warm pools are not enough.",
        fields={"problem": "Cold starts", "stakes": "", "thesis": "Prefetch pages"},
        claims=["c1"],
    )
    paragraph = Node(
        id="p-1",
        level="paragraph",
        says="FaaS scales to zero.",
        fields={"function": "motivate"},
        anchor=Anchor(file="sections/intro.tex", marker="@p p-1"),
    )
    planned = Node(id="p-2", level="paragraph", says="", fields=empty_fields("paragraph"))
    section = Node(
        id="introduction-1",
        level="section",
        says="Cold starts hurt.",
        fields={"figure_story": ""},
        claims=["c1"],
        anchor=Anchor(file="sections/intro.tex", marker="\\section{Introduction}"),
        children=[paragraph, planned],
    )
    claim = Node(
        id="c1",
        level="claim",
        says="3.1x faster",
        fields={"evidence": "trace of 1,200 functions", "scope": "median"},
    )
    tree = Node(id="message", level="message", children=[section])
    return PaperSpec(version=1, message=message, claims=[claim], tree=tree)


def test_render_outline() -> None:
    outline = render_outline(_spec())
    lines = outline.splitlines()
    assert lines[0] == "MESSAGE (message): Warm pools are not enough."
    assert "  problem: Cold starts" in lines and "  thesis: Prefetch pages" in lines
    assert not any("stakes" in line for line in lines)  # empty fields are left out
    assert "  claims: c1" in lines
    assert (
        "- section introduction-1 [`\\section{Introduction}` in sections/intro.tex]: "
        "Cold starts hurt." in lines
    )
    assert (
        "  - paragraph p-1 [marker `% @p p-1` in sections/intro.tex]: FaaS scales to zero." in lines
    )
    assert "      function: motivate" in lines
    assert "  - paragraph p-2 [planned: no text yet]: (not stated yet)" in lines
    assert lines[-3:] == [
        "- c1: 3.1x faster",
        "    evidence: trace of 1,200 functions",
        "    scope: median",
    ]
    assert outline.index("SECTIONS") < outline.index("CLAIMS:")


# --------------------------------------------------------------------------- source text


def test_strip_authors_keeps_line_numbers() -> None:
    text = (
        "\\title{T}\n"
        "\\author{Ada {Example}\\\\\n"
        "  Dept \\{weird\\}\\\\\n"
        "  \\texttt{ada@example.edu}}\n"
        "\\date{}\n"
        "\\author[1]{Bob}\\affiliation{Uni}\n"
        "\\email{bob@x}\n"
        "after\n"
    )
    out = strip_authors(text)
    assert out.count("\n") == text.count("\n")
    lines = out.splitlines()
    assert lines[1] == "\\author{[removed]}"
    assert lines[2] == "" and lines[3] == ""
    assert lines[4] == "\\date{}"
    assert lines[5] == "\\author{[removed]}\\affiliation{[removed]}"
    assert lines[6] == "\\email{[removed]}" and lines[7] == "after"
    assert "Ada" not in out and "Bob" not in out and "example.edu" not in out


def test_strip_authors_unclosed_and_crlf() -> None:
    assert strip_authors("\\author{Ada\nnext\n") == "\\author{[removed]}\nnext\n"
    assert strip_authors("a\r\n\\author{X\r\nY}\r\nz") == "a\r\n\\author{[removed]}\r\n\r\nz"
    assert strip_authors("\\authorship{x}") == "\\authorship{x}"  # a different command


def test_number_lines() -> None:
    assert number_lines(["a", "b", "c"], 2, 3) == ["   2 | b", "   3 | c"]


# --------------------------------------------------------------------------- scopes


def _repo_spec(repo: Path) -> PaperSpec:
    spec, _, errors = load_spec(Repo.at(repo))
    assert spec is not None, errors
    return spec


def test_paper_scope(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    resolved = resolve_scope(repo, _repo_spec(initialized_repo), Scope(kind="paper"))
    assert resolved.name == "the whole paper"
    assert list(resolved.files) == [
        "main.tex",
        "sections/intro.tex",
        "sections/method.tex",
        "sections/results.tex",
        "sections/conclusion.tex",
    ]
    source = resolved.source
    headers = [line for line in source.splitlines() if line.startswith("=== file:")]
    assert headers[0] == "=== file: main.tex ===" and len(headers) == 5
    assert "Ada Example" not in source and "example.edu" not in source
    main_lines = (initialized_repo / "main.tex").read_text().splitlines()
    date_line = main_lines.index("\\date{}") + 1
    assert f"{date_line:>4} | \\date{{}}" in source  # numbering unchanged by the strip
    assert "  12 | \\author{[removed]}" in source


def test_section_scope(client: TestClient, initialized_repo: Path) -> None:
    section_id, _ = adopt_intro(client)
    repo = Repo.at(initialized_repo)
    resolved = resolve_scope(
        repo, _repo_spec(initialized_repo), Scope(kind="section", ref=section_id)
    )
    assert resolved.name == f'section "Introduction" ({section_id})'
    assert resolved.scope.file == "sections/intro.tex"
    assert (resolved.scope.line_start, resolved.scope.line_end) == (1, 29)
    lines = resolved.source.splitlines()
    assert lines[0] == "=== file: sections/intro.tex (lines 1-29) ==="
    assert lines[1] == "   1 | \\section{Introduction}"
    assert lines[4] == "   4 | % @p p-1"
    assert len(lines) == 30


def test_section_scope_stops_at_next_section(client: TestClient, initialized_repo: Path) -> None:
    (initialized_repo / "sections" / "intro.tex").write_text(
        "\\section{One}\nfirst para.\n\n\\subsection{Sub}\nsub text.\n\n\\section{Two}\nsecond.\n"
    )
    skeleton = client.get("/api/spec/skeleton").json()
    one = next(s for s in skeleton["sections"] if s["heading"] == "One")
    sub = next(s for s in skeleton["sections"] if s["heading"] == "Sub")
    ids = []
    for heading in (one, sub):
        response = client.post(
            "/api/spec/adopt",
            json={"kind": "section", "file": heading["file"], "line": heading["line"]},
        )
        ids.append(response.json()["new_id"])
    repo = Repo.at(initialized_repo)
    spec = _repo_spec(initialized_repo)
    first = resolve_scope(repo, spec, Scope(kind="section", ref=ids[0]))
    assert (first.scope.line_start, first.scope.line_end) == (1, 5)  # includes its subsection
    second = resolve_scope(repo, spec, Scope(kind="section", ref=ids[1]))
    assert (second.scope.line_start, second.scope.line_end) == (4, 5)


def test_section_scope_errors(client: TestClient, initialized_repo: Path) -> None:
    section_id, paragraph_ids = adopt_intro(client)
    planned = client.post(
        "/api/spec/ops",
        json={"op": {"type": "insert", "parent_id": "message", "index": 1, "level": "section"}},
    ).json()["new_id"]
    repo = Repo.at(initialized_repo)
    spec = _repo_spec(initialized_repo)
    for ref, fragment in (
        (planned, "planned"),
        ("nope", "unknown section"),
        (paragraph_ids[0], "is a paragraph"),
        (None, "needs `ref`"),
    ):
        with pytest.raises(PromptError, match=fragment):
            resolve_scope(repo, spec, Scope(kind="section", ref=ref))
    intro = initialized_repo / "sections" / "intro.tex"
    intro.write_text(intro.read_text().replace("\\section{Introduction}", "\\section{Intro}"))
    with pytest.raises(PromptError, match="orphaned"):
        resolve_scope(repo, spec, Scope(kind="section", ref=section_id))


def test_file_scope(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    spec = _repo_spec(initialized_repo)
    resolved = resolve_scope(repo, spec, Scope(kind="file", file="./sections//method.tex"))
    assert resolved.scope.file == "sections/method.tex"
    assert resolved.name == "file sections/method.tex"
    assert resolved.source.splitlines()[0] == "=== file: sections/method.tex ==="
    for bad, fragment in (
        ("paper.json", "not a source file"),
        (".rewrite/config.toml", "not a source file"),
        ("../etc/passwd", "not allowed"),
        ("sections/missing.tex", "not found"),
    ):
        with pytest.raises(PromptError, match=fragment):
            resolve_scope(repo, spec, Scope(kind="file", file=bad))


def test_selection_scope(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    spec = _repo_spec(initialized_repo)
    resolved = resolve_scope(
        repo, spec, Scope(kind="selection", file="sections/intro.tex", line_start=10, line_end=12)
    )
    assert resolved.name == "lines 10-12 of sections/intro.tex"
    lines = resolved.source.splitlines()
    assert lines[1] == "--- context before ---"
    assert [line[:4] for line in lines[2:5]] == ["   7", "   8", "   9"]
    assert lines[5] == "--- selection ---"
    assert [line[:4] for line in lines[6:9]] == ["  10", "  11", "  12"]
    assert lines[9] == "--- context after ---"
    assert [line[:4] for line in lines[10:13]] == ["  13", "  14", "  15"]
    first = resolve_scope(
        repo, spec, Scope(kind="selection", file="sections/intro.tex", line_start=1, line_end=1)
    )
    assert "--- context before ---" not in first.source
    for start, end in ((0, 3), (5, 2), (500, 501)):
        with pytest.raises(PromptError, match="not a range"):
            resolve_scope(
                repo,
                spec,
                Scope(kind="selection", file="sections/intro.tex", line_start=start, line_end=end),
            )
    with pytest.raises(PromptError, match="needs"):
        resolve_scope(repo, spec, Scope(kind="selection", file="sections/intro.tex"))


# --------------------------------------------------------------------------- assembly


def test_assemble_prompt(client: TestClient, initialized_repo: Path) -> None:
    section_id, _ = adopt_intro(client)
    repo = Repo.at(initialized_repo)
    assembled = assemble_prompt(repo, "paragraph-function", Scope(kind="section", ref=section_id))
    system = (initialized_repo / ".rewrite" / "prompts" / "system.md").read_text()
    prompt = assembled.prompt
    assert prompt.startswith(system.strip("\n"))
    assert "## Pass: paragraph function" in prompt
    assert f'Scope: section "Introduction" ({section_id})' in prompt
    assert assembled.resolved.source in prompt
    assert findings_schema_text() in prompt
    assert "{{" not in prompt and "{#" not in prompt
    assert render_outline(_repo_spec(initialized_repo)) in prompt


def test_cache_key_inputs(client: TestClient, initialized_repo: Path) -> None:
    section_id, _ = adopt_intro(client)
    repo = Repo.at(initialized_repo)
    scope = Scope(kind="section", ref=section_id)

    def key(runner: str = "claude", model: str | None = "sonnet", thinking: str = "low") -> str:
        return assemble_prompt(repo, "paragraph-function", scope).cache_key(runner, model, thinking)

    base = key()
    assert key() == base
    assert len({base, key("codex"), key(model=None), key(thinking="high")}) == 4
    intro = initialized_repo / "sections" / "intro.tex"
    intro.write_text(intro.read_text().replace("bill tenants", "charge tenants"))
    edited = key()
    assert edited != base
    # an edit outside the scope does not change the key
    method = initialized_repo / "sections" / "method.tex"
    method.write_text(method.read_text() + "\n% a comment\n")
    assert key() == edited
    client.post("/api/spec/ops", json={"op": {"type": "update", "id": section_id, "says": "x"}})
    after_spec = key()
    assert after_spec != edited
    system = initialized_repo / ".rewrite" / "prompts" / "system.md"
    system.write_text(system.read_text() + "\nOne more rule.\n")
    after_system = key()
    assert after_system != after_spec
    pass_file = initialized_repo / ".rewrite" / "prompts" / "passes" / "paragraph-function.md"
    pass_file.write_text(pass_file.read_text() + "\nAnd be brief.\n")
    assert key() != after_system


def test_ensure_sandbox(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    system = repo.prompts_dir / "system.md"
    assert ensure_sandbox(repo) == []  # init wrote them already
    system.write_text(system.read_text() + "\nNew rule.\n")
    assert ensure_sandbox(repo) == ["CLAUDE.md", "AGENTS.md"]
    for name in ("CLAUDE.md", "AGENTS.md"):
        assert (repo.sandbox_dir / name).read_text() == sandbox_text(system.read_text())
    assert ensure_sandbox(repo) == []
    assert sorted(p.name for p in repo.sandbox_dir.iterdir()) == ["AGENTS.md", "CLAUDE.md"]


# --------------------------------------------------------------------------- phase 6


def _adopt(client: TestClient, headings: list[str] | None = None) -> dict[str, str]:
    """Adopt `\\section` headings (all, or the named ones) as spec sections: heading → id."""
    ids = {}
    for section in client.get("/api/spec/skeleton").json()["sections"]:
        if headings is not None and section["heading"] not in headings:
            continue
        body = {"kind": "section", "file": section["file"], "line": section["line"]}
        response = client.post("/api/spec/adopt", json=body)
        assert response.status_code == 200, response.text
        ids[section["heading"]] = response.json()["new_id"]
    return ids


def test_ends_scope(client: TestClient, initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    with pytest.raises(PromptError, match="at least one linked section"):
        resolve_scope(repo, _repo_spec(initialized_repo), Scope(kind="ends"))
    ids = _adopt(client)
    assert list(ids) == ["Introduction", "Design", "Evaluation", "Conclusion"]
    resolved = resolve_scope(repo, _repo_spec(initialized_repo), Scope(kind="ends", ref="x"))
    assert resolved.scope == Scope(kind="ends")
    assert resolved.name == (
        f'the abstract, section "Introduction" ({ids["Introduction"]}) and section '
        f'"Conclusion" ({ids["Conclusion"]})'
    )
    assert list(resolved.files) == ["main.tex", "sections/intro.tex", "sections/conclusion.tex"]
    headers = [line for line in resolved.source.splitlines() if line.startswith("=== file:")]
    assert headers == [
        "=== file: main.tex (lines 21-29) ===",
        "=== file: sections/intro.tex (lines 1-25) ===",
        "=== file: sections/conclusion.tex (lines 1-7) ===",
    ]
    assert "  21 | \\begin{abstract}" in resolved.source
    assert "  29 | \\end{abstract}" in resolved.source
    assert "Serverless platforms hide cold-start latency" in resolved.source
    assert "Extending the approach" in resolved.source
    assert "Ada Example" not in resolved.source and "\\maketitle" not in resolved.source
    assert "Recording" not in resolved.source and "24-hour trace" not in resolved.source


def test_ends_scope_with_one_section_and_no_abstract(
    client: TestClient, initialized_repo: Path
) -> None:
    main = initialized_repo / "main.tex"
    main.write_text(main.read_text().replace("{abstract}", "{summary}"))
    ids = _adopt(client, ["Design"])
    planned = client.post(
        "/api/spec/ops",
        json={"op": {"type": "insert", "parent_id": "message", "index": 0, "level": "section"}},
    )
    assert planned.status_code == 200  # a planned section is not linked: ignored
    resolved = resolve_scope(
        Repo.at(initialized_repo), _repo_spec(initialized_repo), Scope(kind="ends")
    )
    assert resolved.name == f'section "Design" ({ids["Design"]})'
    assert resolved.source.splitlines()[0] == "=== file: sections/method.tex (lines 1-38) ==="
    assert list(resolved.files) == ["sections/method.tex"]


def test_abstract_range_ignores_comments() -> None:
    lines = ["% \\begin{abstract}", "x", "\\begin{abstract} a", "b", "\\end{abstract}", "y"]
    assert prompts.abstract_range(lines) == (3, 5)
    assert prompts.abstract_range(["\\begin{abstract}", "never closed"]) is None
    assert prompts.abstract_range([]) is None


def test_fanout_note_and_cache_key(client: TestClient, initialized_repo: Path) -> None:
    section_id, _ = adopt_intro(client)
    repo = Repo.at(initialized_repo)
    scope = Scope(kind="section", ref=section_id)
    plain = assemble_prompt(repo, "paragraph-function", scope)
    member = assemble_prompt(repo, "paragraph-function", scope, fanout=True)
    note = prompts.FANOUT_NOTE.format(scope_name=plain.scope_name)
    assert note not in plain.prompt and note in member.prompt
    system = prompts.load_system(repo).strip("\n")
    assert member.prompt.startswith(system + "\n\n" + note + "\n\n## Pass: paragraph function")
    assert plain.prompt.replace("\n\n## Pass", "\n\n" + note + "\n\n## Pass", 1) == member.prompt
    assert member.variant == "fanout" and plain.variant == ""
    assert plain.cache_key("claude", "x", "low") != member.cache_key("claude", "x", "low")
    # a pass that words the note itself through the `fanout` variable gets no prefix
    write_pass(
        initialized_repo,
        "own-note",
        GOOD.replace("name: demo", "name: own-note")
        + "{% if fanout %}PART OF A FAN-OUT{% endif %}\n",
    )
    own = assemble_prompt(repo, "own-note", scope, fanout=True)
    assert "PART OF A FAN-OUT" in own.prompt and note not in own.prompt
    assert "PART OF A FAN-OUT" not in assemble_prompt(repo, "own-note", scope).prompt


def test_pass_kind_front_matter() -> None:
    triage = parse_pass(
        GOOD.replace("name: demo", "name: t\nkind: triage") + "{{ findings }}\n", "p/t.md", "t"
    )
    assert triage.info.kind == "triage" and triage.info.error is None
    assert triage.info.runnable is False
    compare = parse_pass(
        GOOD.replace("name: demo", "name: c\nkind: compare") + "{{ version_a }}{{ version_b }}",
        "p/c.md",
        "c",
    )
    assert compare.info.kind == "compare" and compare.info.error is None
    assert parse_pass(GOOD, "p/demo.md", "demo").info.kind == "review"
    bad = parse_pass(GOOD.replace("name: demo", "name: b\nkind: nope"), "p/b.md", "b")
    assert bad.info.error and "kind" in bad.info.error and not bad.info.runnable


def test_triage_and_compare_pass_lookup(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    assert prompts.get_triage_pass(repo).info.kind == "triage"
    assert prompts.get_compare_pass(repo).info.kind == "compare"
    with pytest.raises(PromptError, match="not a triage pass"):
        prompts.get_triage_pass(repo, "clarity")
    with pytest.raises(PromptError, match="triage pass, not a comparison"):
        prompts.get_compare_pass(repo, "triage")
    with pytest.raises(PromptError, match="cannot run as a review"):
        prompts.get_pass(repo, "triage")
    # a pass file from before phase 6 (no `kind`) still works for the comparison
    path = initialized_repo / ".rewrite/prompts/passes/ab-compare.md"
    path.write_text(
        "".join(line for line in path.read_text().splitlines(True) if "kind:" not in line)
    )
    old = prompts.get_compare_pass(repo)
    assert old.info.kind == "review" and old.info.runnable is False


# --------------------------------------------------------------------------- audit item 8

INCLUDED_MAIN = """\\documentclass{article}
\\begin{document}
\\begin{abstract}
Abstract words.
\\end{abstract}
\\section{Introduction}
\\input{intro-body}
\\section{Method}
\\input{parts/method}
\\section{Conclusion}
Closing words.
\\end{document}
"""
INCLUDED_FILES = {
    "intro-body.tex": "The introduction argues INTRO_BODY_SENTENCE.\n\n\\input{parts/nested}\n",
    "parts/nested.tex": "A nested detail NESTED_SENTENCE.\n",
    "parts/method.tex": (
        "Method text METHOD_SENTENCE.\n"
        "\n"
        "\\subsection{Setup}\n"
        "Setup text.\n"
        "\n"
        "\\section{Results}\n"
        "Results text RESULTS_SENTENCE.\n"
    ),
}


@pytest.fixture
def included(tmp_path: Path) -> Iterator[tuple[TestClient, Repo, dict[str, str]]]:
    """A paper whose section bodies live in included files: (client, repo, heading → id)."""
    root = tmp_path / "paper"
    root.mkdir()
    (root / "main.tex").write_text(INCLUDED_MAIN)
    for rel, text in INCLUDED_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    init_repo(root)
    with make_client(root) as client:
        ids = _adopt(client, ["Introduction", "Method", "Results", "Conclusion"])
        yield client, Repo.at(root), ids


def _headers(source: str) -> list[str]:
    return [line for line in source.splitlines() if line.startswith("=== file:")]


def test_section_scope_follows_includes_in_document_order(
    included: tuple[TestClient, Repo, dict[str, str]],
) -> None:
    """Audit 2026-10-18 item 8: a section's body may live in `\\input` files; the section
    runs in document order to the next heading of the same or a higher level, with one
    block per file segment numbered by that file's own lines."""
    _, repo, ids = included
    spec = _repo_spec(repo.root)
    intro = resolve_scope(repo, spec, Scope(kind="section", ref=ids["Introduction"]))
    assert _headers(intro.source) == [
        "=== file: main.tex (lines 6-7) ===",
        "=== file: intro-body.tex (lines 1-3) ===",
        "=== file: parts/nested.tex (lines 1-1) ===",
    ]
    assert "   1 | The introduction argues INTRO_BODY_SENTENCE." in intro.source
    assert "   1 | A nested detail NESTED_SENTENCE." in intro.source
    assert "METHOD_SENTENCE" not in intro.source and "\\section{Method}" not in intro.source
    assert list(intro.files) == ["main.tex", "intro-body.tex", "parts/nested.tex"]
    assert (intro.scope.file, intro.scope.line_start, intro.scope.line_end) == ("main.tex", 6, 7)

    method = resolve_scope(repo, spec, Scope(kind="section", ref=ids["Method"]))
    assert _headers(method.source) == [
        "=== file: main.tex (lines 8-9) ===",
        "=== file: parts/method.tex (lines 1-4) ===",  # its subsection stays in
    ]
    assert "METHOD_SENTENCE" in method.source and "Setup text." in method.source
    assert "RESULTS_SENTENCE" not in method.source  # the boundary is inside the include

    results = resolve_scope(repo, spec, Scope(kind="section", ref=ids["Results"]))
    assert _headers(results.source) == ["=== file: parts/method.tex (lines 6-7) ==="]
    assert "Closing words" not in results.source

    conclusion = resolve_scope(repo, spec, Scope(kind="section", ref=ids["Conclusion"]))
    assert _headers(conclusion.source) == ["=== file: main.tex (lines 10-11) ==="]


def test_ends_and_fanout_prompts_include_section_bodies(
    included: tuple[TestClient, Repo, dict[str, str]],
) -> None:
    _, repo, ids = included
    ends = resolve_scope(repo, _repo_spec(repo.root), Scope(kind="ends"))
    assert _headers(ends.source) == [
        "=== file: main.tex (lines 3-5) ===",
        "=== file: main.tex (lines 6-7) ===",
        "=== file: intro-body.tex (lines 1-3) ===",
        "=== file: parts/nested.tex (lines 1-1) ===",
        "=== file: main.tex (lines 10-11) ===",
    ]
    assert "INTRO_BODY_SENTENCE" in ends.source and "NESTED_SENTENCE" in ends.source
    assert "METHOD_SENTENCE" not in ends.source and "RESULTS_SENTENCE" not in ends.source
    assert set(ends.files) == {"main.tex", "intro-body.tex", "parts/nested.tex"}

    member = assemble_prompt(
        repo, "paragraph-function", Scope(kind="section", ref=ids["Introduction"]), fanout=True
    )
    assert "INTRO_BODY_SENTENCE" in member.prompt and "NESTED_SENTENCE" in member.prompt
    assert "METHOD_SENTENCE" not in member.prompt
    before = member.cache_key("claude", "sonnet", "low")
    body = repo.root / "intro-body.tex"
    body.write_text(body.read_text().replace("argues", "claims"))
    after = assemble_prompt(
        repo, "paragraph-function", Scope(kind="section", ref=ids["Introduction"]), fanout=True
    )
    assert after.cache_key("claude", "sonnet", "low") != before  # the body is scope input
