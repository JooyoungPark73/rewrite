"""The paper repo on disk and `rewrite init`.

This module and app/spec.py are the only modules allowed to write `paper.json`. Here that
write is limited to `init_repo` creating the empty spec (app.spec.empty_spec, serialized by
app.spec.serialize) when none exists: a human-run command with no LLM input.
tests/test_invariants.py enforces the rule.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Config, default_config_text, load_config

REWRITE_DIR = ".rewrite"
PAPER_JSON_NAME = "paper.json"
GITIGNORE_LINES = (".rewrite/state.db*", ".rewrite/build/")
GITIGNORE_HEADER = "# rewrite (rewrite init)"
SANDBOX_HEADER = (
    "<!-- Written by rewrite from .rewrite/prompts/system.md. "
    "Edit that file instead; this one is regenerated. -->\n\n"
)


def default_prompts_dir() -> Path:
    """The app's default prompt set.

    An installed wheel carries it inside the package (app/default_prompts, see
    pyproject.toml). A source checkout, including the editable install made by `uv sync`,
    reads `prompts/` at the app repo root so edits there take effect immediately.
    """
    bundled = Path(__file__).resolve().parent / "default_prompts"
    if (bundled / "system.md").is_file():
        return bundled
    source = Path(__file__).resolve().parents[2] / "prompts"
    if (source / "system.md").is_file():
        return source
    raise FileNotFoundError(f"default prompt set not found (looked in {bundled} and {source})")


@dataclass(frozen=True)
class Repo:
    """A paper repo. `root` is absolute and resolved."""

    root: Path

    @classmethod
    def at(cls, path: str | Path) -> Repo:
        return cls(Path(path).expanduser().resolve())

    @property
    def rewrite_dir(self) -> Path:
        return self.root / REWRITE_DIR

    @property
    def config_path(self) -> Path:
        return self.rewrite_dir / "config.toml"

    @property
    def prompts_dir(self) -> Path:
        return self.rewrite_dir / "prompts"

    @property
    def sandbox_dir(self) -> Path:
        return self.rewrite_dir / "sandbox"

    @property
    def build_dir(self) -> Path:
        return self.rewrite_dir / "build"

    @property
    def paper_json_path(self) -> Path:
        return self.root / PAPER_JSON_NAME

    @property
    def is_initialized(self) -> bool:
        return self.rewrite_dir.is_dir()

    def load_config(self) -> Config:
        return load_config(self.config_path)

    @property
    def main(self) -> str:
        return self.load_config().paper.main

    def build_output(self, suffix: str) -> Path:
        """Path of a build artifact for the main file, e.g. `.pdf` → build/main.pdf."""
        return self.build_dir / (Path(self.main).stem + suffix)


@dataclass
class InitReport:
    repo: Repo
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated)


_DOCUMENTCLASS_RE = re.compile(r"^[^%]*\\documentclass", re.MULTILINE)


def detect_main(root: Path) -> str:
    """`main.tex` if present, else the only top-level .tex file with \\documentclass."""
    if (root / "main.tex").is_file():
        return "main.tex"
    roots = []
    for path in sorted(root.glob("*.tex")):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue
        if _DOCUMENTCLASS_RE.search(head):
            roots.append(path.name)
    return roots[0] if len(roots) == 1 else "main.tex"


def sandbox_text(system_md: str) -> str:
    return SANDBOX_HEADER + system_md


def init_repo(path: str | Path) -> InitReport:
    """Create `.rewrite/` in a paper repo. Idempotent; never overwrites user files.

    Existing config.toml, prompts and paper.json are kept. The sandbox instruction files are
    derived from `.rewrite/prompts/system.md` and rewritten only if they differ.
    """
    from app.files import atomic_write_text  # local imports avoid import cycles
    from app.spec import empty_spec, serialize

    repo = Repo.at(path)
    if not repo.root.is_dir():
        raise NotADirectoryError(f"not a directory: {repo.root}")
    report = InitReport(repo=repo)

    def rel(p: Path) -> str:
        return p.relative_to(repo.root).as_posix()

    for d in (repo.rewrite_dir, repo.prompts_dir, repo.sandbox_dir, repo.build_dir):
        if not d.is_dir():
            d.mkdir(parents=True)
            report.created.append(rel(d) + "/")

    if repo.config_path.exists():
        report.kept.append(rel(repo.config_path))
    else:
        atomic_write_text(repo.config_path, default_config_text(detect_main(repo.root)))
        report.created.append(rel(repo.config_path))

    defaults = default_prompts_dir()
    for src in sorted(defaults.rglob("*.md")):
        dest = repo.prompts_dir / src.relative_to(defaults)
        if dest.exists():
            report.kept.append(rel(dest))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        report.created.append(rel(dest))

    system_md = (repo.prompts_dir / "system.md").read_text(encoding="utf-8")
    for name in ("CLAUDE.md", "AGENTS.md"):
        dest = repo.sandbox_dir / name
        want = sandbox_text(system_md)
        if dest.is_file() and dest.read_text(encoding="utf-8") == want:
            report.kept.append(rel(dest))
            continue
        existed = dest.exists()
        atomic_write_text(dest, want)
        (report.updated if existed else report.created).append(rel(dest))

    gitignore = repo.root / ".gitignore"
    existed = gitignore.exists()
    current = gitignore.read_text(encoding="utf-8") if existed else ""
    present = {line.strip() for line in current.splitlines()}
    to_add = [line for line in GITIGNORE_LINES if line not in present]
    if to_add:
        prefix = "" if not current or current.endswith("\n") else "\n"
        if current.strip():
            prefix += "\n"
        block = "\n".join([GITIGNORE_HEADER, *to_add]) + "\n"
        atomic_write_text(gitignore, current + prefix + block)
        (report.updated if existed else report.created).append(".gitignore")
    else:
        report.kept.append(".gitignore")

    if repo.paper_json_path.exists():
        report.kept.append(PAPER_JSON_NAME)
    else:
        atomic_write_text(repo.paper_json_path, serialize(empty_spec()))
        report.created.append(PAPER_JSON_NAME)

    return report
