"""`rewrite init`: layout, prompt copy, sandbox files, .gitignore, idempotence."""

from __future__ import annotations

import json
from pathlib import Path

from app.cli import main as cli_main
from app.config import Config, load_config
from app.repo import (
    GITIGNORE_LINES,
    SANDBOX_HEADER,
    Repo,
    default_prompts_dir,
    detect_main,
    init_repo,
)
from app.spec import empty_spec, serialize, validate

PASSES = {
    "message-drift",
    "claims-evidence",
    "section-promises",
    "paragraph-function",
    "clarity",
    "epistemic-calibration",
    "reviewer-2",
    "latex-hygiene",
    "ab-compare",
    "triage",
}


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_init_creates_layout(paper_repo: Path) -> None:
    report = init_repo(paper_repo)
    pb = paper_repo / ".rewrite"
    assert report.changed
    for d in ("prompts", "prompts/passes", "sandbox", "build"):
        assert (pb / d).is_dir(), d
    assert load_config(pb / "config.toml") == Config()
    assert {p.stem for p in (pb / "prompts" / "passes").glob("*.md")} == PASSES
    system = (pb / "prompts" / "system.md").read_text()
    assert system == (default_prompts_dir() / "system.md").read_text()
    for name in ("CLAUDE.md", "AGENTS.md"):
        assert (pb / "sandbox" / name).read_text() == SANDBOX_HEADER + system
    text = (paper_repo / "paper.json").read_text()
    spec = json.loads(text)
    assert spec["version"] == 1 and spec["claims"] == []
    assert spec["message"]["level"] == "message" and spec["tree"]["children"] == []
    assert spec["message"]["notes"] == "" and spec["tree"]["notes"] == ""
    # the editor's canonical serialization, so the first edit is a one-line diff
    assert text == serialize(empty_spec()) and validate(empty_spec()) == []


def test_gitignore_lines_appended(paper_repo: Path) -> None:
    (paper_repo / ".gitignore").write_text("*.aux\n*.log")  # no trailing newline
    init_repo(paper_repo)
    lines = (paper_repo / ".gitignore").read_text().splitlines()
    assert lines[:2] == ["*.aux", "*.log"]
    for want in GITIGNORE_LINES:
        assert lines.count(want) == 1


def test_gitignore_created_when_absent(paper_repo: Path) -> None:
    init_repo(paper_repo)
    lines = (paper_repo / ".gitignore").read_text().splitlines()
    assert all(want in lines for want in GITIGNORE_LINES)


def test_init_twice_changes_nothing(paper_repo: Path) -> None:
    init_repo(paper_repo)
    before = snapshot(paper_repo)
    report = init_repo(paper_repo)
    assert not report.changed, (report.created, report.updated)
    assert snapshot(paper_repo) == before


def test_init_keeps_user_files(paper_repo: Path) -> None:
    init_repo(paper_repo)
    pb = paper_repo / ".rewrite"
    (pb / "config.toml").write_text('[paper]\nmain = "paper.tex"\n')
    (pb / "prompts" / "passes" / "clarity.md").write_text("my own pass\n")
    (pb / "prompts" / "system.md").write_text("my system prompt\n")
    (paper_repo / "paper.json").write_text('{"mine": true}\n')
    (pb / "prompts" / "passes" / "reviewer-2.md").unlink()

    report = init_repo(paper_repo)

    assert (pb / "config.toml").read_text() == '[paper]\nmain = "paper.tex"\n'
    assert (pb / "prompts" / "passes" / "clarity.md").read_text() == "my own pass\n"
    assert (paper_repo / "paper.json").read_text() == '{"mine": true}\n'
    # a deleted default pass is restored, and the sandbox follows the edited system.md
    assert (pb / "prompts" / "passes" / "reviewer-2.md").is_file()
    assert (pb / "sandbox" / "CLAUDE.md").read_text() == SANDBOX_HEADER + "my system prompt\n"
    assert ".rewrite/sandbox/CLAUDE.md" in report.updated


def test_detect_main(tmp_path: Path) -> None:
    (tmp_path / "notes.tex").write_text("% no documentclass here\n")
    (tmp_path / "paper.tex").write_text("\\documentclass{article}\n")
    assert detect_main(tmp_path) == "paper.tex"
    init_repo(tmp_path)
    assert Repo.at(tmp_path).main == "paper.tex"
    (tmp_path / "main.tex").write_text("\\documentclass{article}\n")
    assert detect_main(tmp_path) == "main.tex"


def test_cli_init(paper_repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert cli_main(["init", str(paper_repo)]) == 0
    assert "created" in capsys.readouterr().out
    assert cli_main(["init", str(paper_repo)]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert cli_main(["init", str(paper_repo / "nope")]) == 1
