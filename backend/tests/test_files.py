"""\\input resolution, path containment, atomic save."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.files import (
    NotTextError,
    PathEscapeError,
    ProtectedPathError,
    read_text_file,
    resolve_in_repo,
    resolve_tex_tree,
    strip_tex_comment,
    write_text_file,
)


def tree_pairs(root: Path, main: str = "main.tex") -> list[tuple[str, str | None]]:
    return [(f.path, f.included_by) for f in resolve_tex_tree(root, main).files]


def test_sample_paper_tree(paper_repo: Path) -> None:
    tree = resolve_tex_tree(paper_repo, "main.tex")
    assert [(f.path, f.included_by, f.kind) for f in tree.files] == [
        ("main.tex", None, "tex"),
        ("sections/intro.tex", "main.tex", "tex"),
        ("sections/method.tex", "main.tex", "tex"),
        ("sections/results.tex", "main.tex", "tex"),
        ("sections/conclusion.tex", "main.tex", "tex"),
        ("refs.bib", "main.tex", "bib"),
    ]
    assert tree.missing == []


def test_nested_include_subfile_comments_and_cycles(tmp_path: Path) -> None:
    (tmp_path / "sections").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\\includegraphics{fig}\n"  # not an include
        "% \\input{commented}\n"
        "text 50\\% done \\input{a}  % \\input{also-commented}\n"
        "\\begin{comment}\n\\input{in-comment-env}\n\\end{comment}\n"
        "\\include{sections/b}\n"
        "\\subfile{sections/c.tex}\n"
        "\\input{a}\n"  # repeated: listed once
        "\\input{missing}\n"
    )
    (tmp_path / "a.tex").write_text("\\input{sections/nested}\n")
    (tmp_path / "sections" / "nested.tex").write_text("\\input{a}\n")  # cycle back to a
    (tmp_path / "sections" / "b.tex").write_text("B\n")
    (tmp_path / "sections" / "c.tex").write_text("\\input{d}\n")  # relative to its own dir
    (tmp_path / "sections" / "d.tex").write_text("D\n")
    for name in ("commented", "also-commented", "in-comment-env"):
        (tmp_path / f"{name}.tex").write_text("x\n")

    tree = resolve_tex_tree(tmp_path, "main.tex")
    assert [(f.path, f.included_by) for f in tree.files] == [
        ("main.tex", None),
        ("a.tex", "main.tex"),
        ("sections/nested.tex", "a.tex"),
        ("sections/b.tex", "main.tex"),
        ("sections/c.tex", "main.tex"),
        ("sections/d.tex", "sections/c.tex"),
    ]
    assert [(f.path, f.included_by) for f in tree.missing] == [("missing.tex", "main.tex")]


UNBRACED_MAIN = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\section{Intro}\n"
    "\\input section\n"
    '\\input "part two"\n'
    "\\input sub/third.tex % a comment after the name\n"
    "\\input  fourth\\relax\n"
    "\\end{document}\n"
)
UNBRACED_FILES = ["main.tex", "section.tex", "part two.tex", "sub/third.tex", "fourth.tex"]


def write_unbraced_paper(root: Path) -> None:
    """A minimal paper that pdflatex compiles, pulling in four files with unbraced `\\input`
    (bare name, quoted name with a space, explicit `.tex`, name ended by a macro)."""
    (root / "sub").mkdir(parents=True)
    (root / "main.tex").write_text(UNBRACED_MAIN)
    (root / "section.tex").write_text("Section body sentence lives here.\n")
    (root / "part two.tex").write_text("Part two sentence.\n")
    (root / "sub" / "third.tex").write_text("Third sentence.\n")
    (root / "fourth.tex").write_text("Fourth sentence.\n")


def test_unbraced_input_in_tree_skeleton_and_whole_paper_prompt(tmp_path: Path) -> None:
    """Audit 2026-10-18 item 11: `\\input section` compiles, so the file belongs to the
    editor's tree, the skeleton (adoption) and a whole-paper review, and an edit to it
    changes the assembled scope and the cache key."""
    from app import skeleton
    from app.prompts import Scope, assemble_prompt, resolve_scope
    from app.repo import Repo, init_repo
    from app.spec import load_spec

    root = tmp_path / "paper"
    write_unbraced_paper(root)
    init_repo(root)
    tree = resolve_tex_tree(root, "main.tex")
    assert [f.path for f in tree.files] == UNBRACED_FILES
    assert [f.included_by for f in tree.files[1:]] == ["main.tex"] * 4
    assert tree.missing == []

    scan = skeleton.scan_document(root, "main.tex")
    assert scan.files == UNBRACED_FILES
    assert [p.file for p in scan.paragraphs] == UNBRACED_FILES[1:]
    assert scan.paragraphs[0].text == "Section body sentence lives here."

    repo = Repo.at(root)
    spec, _, errors = load_spec(repo)
    assert spec is not None, errors
    resolved = resolve_scope(repo, spec, Scope(kind="paper"))
    assert list(resolved.files) == UNBRACED_FILES
    assert "   1 | Section body sentence lives here." in resolved.source
    assert "=== file: part two.tex ===" in resolved.source

    def key() -> str:
        return assemble_prompt(repo, "paragraph-function", Scope(kind="paper")).cache_key(
            "claude", "sonnet", "low"
        )

    before = key()
    (root / "section.tex").write_text("Section body sentence changed.\n")
    assert key() != before

    if shutil.which("latexmk") is None:
        return  # the compile half needs a TeX installation
    from app.compiler.native import NativeLatexmk

    result = NativeLatexmk().compile(root, "main.tex")
    assert result.ok, result.errors
    recorded = (root / ".rewrite" / "build" / "main.fls").read_text().splitlines()
    inputs = {line.removeprefix("INPUT ").removeprefix("./") for line in recorded}
    assert set(UNBRACED_FILES) <= inputs  # what TeX read is what the tree lists


def test_unbraced_input_skips_computed_commented_and_other_commands(tmp_path: Path) -> None:
    for name in ("commented", "computed", "encoding", "latin1", "chapter"):
        (tmp_path / f"{name}.tex").write_text("x\n")
    (tmp_path / "main.tex").write_text(
        "% \\input commented\n"
        "\\input\\computed\n"
        "\\input{\\computed}\n"
        "\\inputencoding{latin1}\n"  # inputenc, not \input
        "\\input chapter\n"
    )
    assert tree_pairs(tmp_path) == [("main.tex", None), ("chapter.tex", "main.tex")]


def test_missing_main(tmp_path: Path) -> None:
    tree = resolve_tex_tree(tmp_path, "main.tex")
    assert tree.files == [] and tree.missing[0].path == "main.tex"


def test_include_outside_repo_is_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.tex").write_text("x\n")
    (repo / "main.tex").write_text("\\input{../outside}\n\\input{/etc/passwd}\n")
    assert tree_pairs(repo) == [("main.tex", None)]


def test_strip_tex_comment() -> None:
    assert strip_tex_comment("a % b") == "a "
    assert strip_tex_comment(r"50\% of it % comment") == r"50\% of it "
    assert strip_tex_comment(r"line\\% comment") == "line\\\\"


@pytest.mark.parametrize(
    "bad",
    ["../x.tex", "sections/../../x.tex", "/etc/passwd", "", "C:\\x.tex", "a\\..\\b", "a\x00b"],
)
def test_path_escape_rejected(paper_repo: Path, bad: str) -> None:
    with pytest.raises(PathEscapeError):
        resolve_in_repo(paper_repo, bad)


def test_symlink_escaping_repo_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    os.symlink(secret, repo / "link.tex")
    os.symlink(tmp_path, repo / "dirlink")
    with pytest.raises(PathEscapeError):
        read_text_file(repo, "link.tex")
    with pytest.raises(PathEscapeError):
        write_text_file(repo, "dirlink/new.tex", "x")
    assert secret.read_text() == "top secret"


def test_symlink_inside_repo_is_followed(tmp_path: Path) -> None:
    (tmp_path / "real.tex").write_text("one\n")
    os.symlink(tmp_path / "real.tex", tmp_path / "alias.tex")
    write_text_file(tmp_path, "alias.tex", "two\n")
    assert (tmp_path / "alias.tex").is_symlink()
    assert (tmp_path / "real.tex").read_text() == "two\n"


def test_save_roundtrip_is_atomic(paper_repo: Path) -> None:
    target = paper_repo / "sections" / "intro.tex"
    os.chmod(target, 0o640)
    content = "\\section{Introduction}\r\nNew text with ünïcode.\n"
    mtime = write_text_file(paper_repo, "sections/intro.tex", content)
    read, read_mtime = read_text_file(paper_repo, "sections/intro.tex")
    assert read == content  # newlines preserved byte for byte
    assert read_mtime == mtime == target.stat().st_mtime
    assert (target.stat().st_mode & 0o777) == 0o640
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_write_creates_new_file_and_dirs(paper_repo: Path) -> None:
    write_text_file(paper_repo, "sections/new/part.tex", "x\n")
    assert (paper_repo / "sections" / "new" / "part.tex").read_text() == "x\n"


@pytest.mark.parametrize(
    "protected",
    [
        "paper.json",
        "./paper.json",
        "Paper.JSON",  # same file on case-insensitive file systems (macOS default)
        ".GIT/config",
        ".git/config",
        ".rewrite/build/main.log",
        ".rewrite/state.db",
        ".rewrite/sandbox/CLAUDE.md",
    ],
)
def test_protected_paths_not_writable(initialized_repo: Path, protected: str) -> None:
    with pytest.raises(ProtectedPathError):
        write_text_file(initialized_repo, protected, "{}")


def test_prompts_are_writable(initialized_repo: Path) -> None:
    write_text_file(initialized_repo, ".rewrite/prompts/passes/clarity.md", "edited\n")


def test_binary_file_rejected(paper_repo: Path) -> None:
    (paper_repo / "fig.pdf").write_bytes(b"%PDF-1.5\n\xff\xfe\x00binary")
    with pytest.raises(NotTextError):
        read_text_file(paper_repo, "fig.pdf")
