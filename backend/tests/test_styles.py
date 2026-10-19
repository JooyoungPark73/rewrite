"""Writing styles: prompts/styles/*.md, `[paper] style`, and the section they add to system.md."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import parse_config, save_config
from app.prompts import (
    STYLE_HEADING,
    PromptError,
    assemble_compare_prompt,
    assemble_prompt,
    get_pass,
    get_style,
    load_styles,
    system_with_style,
)
from app.repo import Repo, default_prompts_dir

EXPECTED_STYLES = {
    "computer-vision",
    "graphics",
    "hci-design",
    "machine-learning",
    "ml-systems",
    "nlp",
    "pl-formal-methods",
    "robotics",
    "systems",
    "theory-algorithms",
    "visualization",
}


def _set_style(repo_root: Path, name: str) -> None:
    repo = Repo.at(repo_root)
    config = repo.load_config()
    config.paper.style = name
    save_config(repo.config_path, config)


def test_default_styles_are_copied_and_parse(initialized_repo: Path) -> None:
    assert (default_prompts_dir() / "styles" / "systems.md").is_file()
    styles = load_styles(Repo.at(initialized_repo))
    assert {s.info.name for s in styles} == EXPECTED_STYLES  # README.md is not a style
    for style in styles:
        assert style.info.title and style.info.description
        assert style.info.file == f".rewrite/prompts/styles/{style.info.name}.md"
        # Every file answers the same five questions.
        for heading in (
            "## The field's question",
            "## What counts as evidence",
            "## What performance and contribution mean",
            "## What a reviewer here looks for first",
            "## Vocabulary",
        ):
            assert heading in style.body, (style.info.name, heading)
        assert "---" not in style.body.split("\n")[0]


def test_get_style_none_and_missing(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    assert get_style(repo, "") is None
    with pytest.raises(PromptError, match="no file .rewrite/prompts/styles/nope.md"):
        get_style(repo, "nope")


def test_bad_style_files(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    styles_dir = repo.prompts_dir / "styles"
    (styles_dir / "bad.md").write_text("no front matter\n")
    with pytest.raises(PromptError, match="no YAML front matter"):
        load_styles(repo)
    (styles_dir / "bad.md").write_text("---\nname: other\ntitle: T\n---\nbody\n")
    with pytest.raises(PromptError, match="must equal 'bad'"):
        load_styles(repo)
    (styles_dir / "bad.md").write_text("---\nname: bad\ntitle: T\nextra: 1\n---\nbody\n")
    with pytest.raises(PromptError, match="bad front matter"):
        load_styles(repo)
    (styles_dir / "bad.md").unlink()


def test_style_section_in_prompts_and_cache_key(client: TestClient, initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    section_id = client.post(
        "/api/spec/adopt", json={"kind": "section", "file": "sections/intro.tex", "line": 1}
    ).json()["new_id"]
    scope = {"kind": "section", "ref": section_id}
    from app.prompts import Scope

    plain = assemble_prompt(repo, "paragraph-function", Scope.model_validate(scope))
    assert STYLE_HEADING not in plain.prompt

    _set_style(initialized_repo, "systems")
    styled = assemble_prompt(repo, "paragraph-function", Scope.model_validate(scope))
    assert STYLE_HEADING in styled.prompt
    assert "This draft is a Systems, architecture, databases and networking paper." in styled.prompt
    assert "under workload W, on system S, with metric M" in styled.prompt
    # system.md first, then the style, then the pass body.
    assert styled.prompt.index("# Manuscript screening") < styled.prompt.index(STYLE_HEADING)
    assert styled.prompt.index(STYLE_HEADING) < styled.prompt.index("## Pass: paragraph function")
    # The style is part of system_text, so the cache key changes with it.
    assert styled.system_text != plain.system_text
    assert styled.cache_key("claude", "sonnet", "low") != plain.cache_key("claude", "sonnet", "low")

    # latex-hygiene opts out; ab-compare too.
    hygiene = assemble_prompt(repo, "latex-hygiene", Scope.model_validate({"kind": "paper"}))
    assert STYLE_HEADING not in hygiene.prompt
    assert system_with_style(repo, get_pass(repo, "latex-hygiene")) == system_with_style(
        repo, get_pass(repo, "latex-hygiene")
    )
    spec = client.get("/api/spec").json()["spec"]
    from app.spec import PaperSpec

    cmp = assemble_compare_prompt(
        repo,
        spec=PaperSpec.model_validate(spec),
        spec_text="{}",
        node_id=None,
        scope=Scope.model_validate(
            {"kind": "selection", "file": "sections/intro.tex", "line_start": 4, "line_end": 5}
        ),
        scope_name="x",
        version_a="a",
        version_b="b",
        answer_schema="{}",
    )
    assert STYLE_HEADING not in cmp.prompt

    # A configured style with no file is an error, not a silent drop.
    _set_style(initialized_repo, "nope")
    with pytest.raises(PromptError, match="writing style 'nope'"):
        assemble_prompt(repo, "paragraph-function", Scope.model_validate(scope))


def test_preview_and_passes_report_the_style(client: TestClient, initialized_repo: Path) -> None:
    section_id = client.post(
        "/api/spec/adopt", json={"kind": "section", "file": "sections/intro.tex", "line": 1}
    ).json()["new_id"]
    _set_style(initialized_repo, "machine-learning")
    preview = client.post(
        "/api/runs/preview",
        json={"pass": "paragraph-function", "scope": {"kind": "section", "ref": section_id}},
    )
    assert preview.status_code == 200, preview.text
    assert "This draft is a Machine learning paper." in preview.json()["prompt"]
    passes = {p["name"]: p for p in client.get("/api/passes").json()["passes"]}
    assert passes["paragraph-function"]["style"] is True
    assert passes["latex-hygiene"]["style"] is False
    assert passes["ab-compare"]["style"] is False


def test_config_api_lists_and_sets_the_style(client: TestClient, initialized_repo: Path) -> None:
    view = client.get("/api/config").json()
    assert view["paper"] == {"style": ""}
    names = {s["name"] for s in view["styles"]}
    assert names == EXPECTED_STYLES
    assert all({"name", "title", "description"} <= set(s) for s in view["styles"])

    res = client.put("/api/config", json={"paper": {"style": "hci-design"}})
    assert res.status_code == 200, res.text
    assert res.json()["paper"]["style"] == "hci-design"
    on_disk = parse_config((initialized_repo / ".rewrite" / "config.toml").read_text())
    assert on_disk.paper.style == "hci-design"
    assert on_disk.compiler.kind == "native"  # the rest is kept

    bad = client.put("/api/config", json={"paper": {"style": "nope"}})
    assert bad.status_code == 400 and "nope" in bad.json()["detail"]
    assert client.get("/api/config").json()["paper"]["style"] == "hci-design"

    # Empty clears it; a PUT without `paper` leaves it alone.
    assert client.put("/api/config", json={"paper": {"style": ""}}).json()["paper"]["style"] == ""
    client.put("/api/config", json={"paper": {"style": "robotics"}})
    assert (
        client.put("/api/config", json={"compiler": {"kind": "native"}}).json()["paper"]["style"]
        == "robotics"
    )


def test_old_config_without_style_defaults_to_none() -> None:
    config = parse_config('[paper]\nmain = "main.tex"\n')
    assert config.paper.style == ""


def test_repo_without_styles_folder_falls_back_to_the_defaults(initialized_repo: Path) -> None:
    """A repo initialised before styles existed: the default set is listed (read-only), a
    configured style still resolves, and the next `rewrite init` copies the files in."""
    import shutil

    from app.repo import init_repo

    repo = Repo.at(initialized_repo)
    shutil.rmtree(repo.prompts_dir / "styles")
    styles = load_styles(repo)
    assert {s.info.name for s in styles} == EXPECTED_STYLES
    assert all(s.info.file.startswith("(default) styles/") for s in styles)
    assert get_style(repo, "systems") is not None
    assert not (repo.prompts_dir / "styles").exists()  # nothing was written
    init_repo(initialized_repo)
    assert (repo.prompts_dir / "styles" / "systems.md").is_file()
    assert load_styles(repo)[0].info.file.startswith(".rewrite/prompts/styles/")
