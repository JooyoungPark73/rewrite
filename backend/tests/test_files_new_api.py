r"""POST /api/files/new: create a file (and folders) and \input it from the main file."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.files import add_input_line


def _new(client: TestClient, path: str, include_in: str | None = None):
    body: dict[str, str | None] = {"path": path}
    if include_in is not None:
        body["include_in"] = include_in
    return client.post("/api/files/new", json=body)


def test_creates_tex_file_folders_and_input_line(
    client: TestClient, initialized_repo: Path
) -> None:
    before = (initialized_repo / "main.tex").read_text()
    res = _new(client, "sections/related/work.tex")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["path"] == "sections/related/work.tex"
    assert body["included_by"] == "main.tex"
    assert (initialized_repo / "sections/related/work.tex").read_text() == ""
    # The \input line sits before \end{document}, with a blank line between.
    main = (initialized_repo / "main.tex").read_text()
    assert main != before
    assert "\\input{sections/related/work}\n\n\\end{document}" in main
    assert main.count("\\end{document}") == 1
    # The tree in the answer and GET /api/files both list it under main.tex.
    listed = {f["path"]: f for f in body["files"]["files"]}
    assert listed["sections/related/work.tex"]["included_by"] == "main.tex"
    tree = client.get("/api/files").json()
    assert any(f["path"] == "sections/related/work.tex" for f in tree["files"])
    # GET /api/file sees the new (empty) file.
    assert (
        client.get("/api/file", params={"path": "sections/related/work.tex"}).json()["content"]
        == ""
    )


def test_existing_path_is_409_and_nothing_changes(
    client: TestClient, initialized_repo: Path
) -> None:
    before = (initialized_repo / "main.tex").read_text()
    res = _new(client, "sections/intro.tex")
    assert res.status_code == 409
    assert "already exists" in res.json()["detail"]
    assert (initialized_repo / "main.tex").read_text() == before


def test_path_rules(client: TestClient, initialized_repo: Path) -> None:
    assert _new(client, "../outside.tex").status_code == 400
    assert _new(client, "/abs/x.tex").status_code == 400
    assert _new(client, ".rewrite/sandbox/x.tex").status_code == 403
    assert _new(client, ".rewrite/build/x.tex").status_code == 403
    assert _new(client, "paper.json").status_code == 403
    assert _new(client, "noext").status_code == 400
    assert not (initialized_repo / "noext").exists()
    for bad in ("../outside.tex", "noext"):
        assert isinstance(_new(client, bad).json()["detail"], str)


def test_include_in_another_file(client: TestClient, initialized_repo: Path) -> None:
    res = _new(client, "sections/intro-extra.tex", include_in="sections/intro.tex")
    assert res.status_code == 200, res.text
    assert res.json()["included_by"] == "sections/intro.tex"
    intro = (initialized_repo / "sections/intro.tex").read_text()
    # intro.tex has no \end{document}: the line is appended.
    assert intro.endswith("\\input{sections/intro-extra}\n")
    assert "intro-extra" not in (initialized_repo / "main.tex").read_text()
    listed = {f["path"]: f for f in client.get("/api/files").json()["files"]}
    assert listed["sections/intro-extra.tex"]["included_by"] == "sections/intro.tex"


def test_missing_include_in_is_404_but_the_file_is_created(
    client: TestClient, initialized_repo: Path
) -> None:
    res = _new(client, "sections/orphan.tex", include_in="sections/nope.tex")
    assert res.status_code == 404
    assert (initialized_repo / "sections/orphan.tex").exists()


def test_non_tex_file_gets_no_input_line(client: TestClient, initialized_repo: Path) -> None:
    before = (initialized_repo / "main.tex").read_text()
    res = _new(client, "figs/notes.md")
    assert res.status_code == 200, res.text
    assert res.json()["included_by"] is None
    assert (initialized_repo / "figs/notes.md").read_text() == ""
    assert (initialized_repo / "main.tex").read_text() == before


def test_add_input_line_cases() -> None:
    assert add_input_line("", "a/b") == "\\input{a/b}\n"
    assert add_input_line("x\n", "a/b") == "x\n\\input{a/b}\n"
    assert add_input_line("x", "a/b") == "x\n\\input{a/b}\n"
    # Before \end{document}, blank line kept; a commented \end{document} does not count.
    assert (
        add_input_line("\\begin{document}\nbody\n\n\\end{document}\n", "a/b")
        == "\\begin{document}\nbody\n\n\\input{a/b}\n\n\\end{document}\n"
    )
    assert (
        add_input_line("body\n\\end{document}\n", "a/b")
        == "body\n\\input{a/b}\n\n\\end{document}\n"
    )
    assert add_input_line("% \\end{document}\n", "a/b") == "% \\end{document}\n\\input{a/b}\n"
    # Already included (with or without .tex, \include too): unchanged.
    for text in ("\\input{a/b}\n", "\\input{a/b.tex}\n", "\\include{ a/b }\n"):
        assert add_input_line(text + "\\end{document}\n", "a/b") == text + "\\end{document}\n"
    # A commented-out reference does not count as included.
    assert add_input_line("% \\input{a/b}\n\\end{document}\n", "a/b") == (
        "% \\input{a/b}\n\\input{a/b}\n\n\\end{document}\n"
    )
