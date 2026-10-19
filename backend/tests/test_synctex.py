"""SyncTeX: output parsing, path normalisation, forward/reverse queries and the API."""

from __future__ import annotations

import gzip
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import CompilerConfig
from app.synctex import (
    EditRecord,
    NoSynctexData,
    SyncBox,
    SyncEdit,
    SynctexUnavailable,
    ViewRecord,
    docker_runner,
    forward,
    normalize_path,
    parse_edit_output,
    parse_view_output,
    recorded_inputs,
    reverse,
    select_runner,
    view_box,
)
from tests.conftest import FIXTURES, fake_exe, make_client, requires_synctex

SYNCTEX_FIXTURES = FIXTURES / "synctex"

# ------------------------------------------------------------------ parsing


def test_parse_view_output_from_fixture() -> None:
    text = (SYNCTEX_FIXTURES / "view-intro-6.txt").read_text()
    records = parse_view_output(text)
    assert records == [
        ViewRecord(1, 71.999985, 400.241211, 468.0, 10.909073, ".rewrite/build/main.pdf"),
        ViewRecord(1, 71.999985, 413.184357, 468.0, 9.636166, ".rewrite/build/main.pdf"),
    ]


def test_view_box_is_top_left_origin() -> None:
    # synctex `v` is the box's bottom edge measured from the top; H extends upwards.
    box = view_box(ViewRecord(page=1, h=72.0, v=400.0, width=468.0, height=10.0))
    assert box == SyncBox(page=1, x=72.0, y=390.0, w=468.0, h=10.0)


def test_parse_view_output_without_match() -> None:
    assert parse_view_output((SYNCTEX_FIXTURES / "view-no-tag.txt").read_text()) == []
    assert parse_view_output("") == []
    assert parse_view_output("SyncTeX result begin\nSyncTeX result end\n") == []


def test_parse_view_output_skips_incomplete_and_bad_records() -> None:
    text = (
        "SyncTeX result begin\n"
        "Output:a.pdf\nPage:1\nh:1\nv:2\nW:3\n"  # no H
        "Output:a.pdf\nPage:x\nh:1\nv:2\nW:3\nH:4\n"  # bad page
        "Output:a.pdf\nPage:2\nh:1\nv:nan\nW:3\nH:4\n"  # not finite
        "Output:a.pdf\nPage:2\nh:10.5\nv:20\nW:30\nH:4\n"
        "SyncTeX result end\n"
        "Output:after-the-end.pdf\nPage:3\nh:1\nv:2\nW:3\nH:4\n"
    )
    assert parse_view_output(text) == [ViewRecord(2, 10.5, 20.0, 30.0, 4.0, "a.pdf")]


def test_parse_view_output_without_output_lines() -> None:
    # Records are also split when a key repeats (older synctex without Output: lines).
    text = "Page:1\nh:1\nv:2\nW:3\nH:4\nPage:2\nh:5\nv:6\nW:7\nH:8\n"
    assert [(r.page, r.h) for r in parse_view_output(text)] == [(1, 1.0), (2, 5.0)]


def test_parse_edit_output_from_fixture() -> None:
    records = parse_edit_output((SYNCTEX_FIXTURES / "edit-intro-4.txt").read_text())
    assert records == [
        EditRecord("/home/ada/paper/./sections/intro.tex", 4, -1, ".rewrite/build/main.pdf")
    ]


def test_parse_edit_output_multiple_and_odd_records() -> None:
    text = (
        "This is SyncTeX command line utility, version 1.5\n"
        "SyncTeX result begin\n"
        "Output:/r/b/main.pdf\nInput:/usr/share/texlive/article.cls\nLine:120\nColumn:-1\n"
        "Offset:0\nContext:\n"
        "Output:/r/b/main.pdf\nInput:/r/dir:with:colons/a.tex\nLine:7\nColumn:3\n"
        "Offset:0\nContext:some: text\n"
        "Output:/r/b/main.pdf\nInput:/r/x.tex\nLine:oops\n"
        "SyncTeX result end\n"
    )
    assert parse_edit_output(text) == [
        EditRecord("/usr/share/texlive/article.cls", 120, -1, "/r/b/main.pdf"),
        EditRecord("/r/dir:with:colons/a.tex", 7, 3, "/r/b/main.pdf"),
    ]
    assert parse_edit_output("This is SyncTeX command line utility, version 1.5\n") == []


# ------------------------------------------------------------------ normalize_path

MAC_REPO = Path("/var/folders/xy/T/paper")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/var/folders/xy/T/paper/./sections/intro.tex", "sections/intro.tex"),
        ("/private/var/folders/xy/T/paper/./sections/intro.tex", "sections/intro.tex"),
        ("/private/var/folders/xy/T/paper/main.tex", "main.tex"),
        ("/var/folders/xy/T/paper//sections/./intro.tex", "sections/intro.tex"),
        ("./sections/intro.tex", "sections/intro.tex"),
        ("sections/intro.tex", "sections/intro.tex"),
        ("  ./main.tex\n", "main.tex"),
        ("/var/folders/xy/T/paper/.rewrite/build/main.bbl", ".rewrite/build/main.bbl"),
        ("/usr/share/texlive/texmf-dist/tex/latex/base/article.cls", None),
        ("/var/folders/xy/T/paper-other/main.tex", None),
        ("/var/folders/xy/T/paper", None),
        ("/private/var/folders/xy/T/other/main.tex", None),
        ("../outside.tex", None),
        ("sections/../../outside.tex", None),
        ("", None),
        (".", None),
    ],
)
def test_normalize_path_table(raw: str, expected: str | None) -> None:
    assert normalize_path(raw, MAC_REPO) == expected


def test_normalize_path_repo_given_as_private() -> None:
    repo = Path("/private/var/folders/xy/T/paper")
    assert normalize_path("/var/folders/xy/T/paper/./a.tex", repo) == "a.tex"
    assert normalize_path("/private/var/folders/xy/T/paper/a.tex", repo) == "a.tex"
    assert normalize_path("/private/etc/passwd", repo) is None


def test_normalize_path_container_root() -> None:
    repo = Path("/home/ada/paper")
    assert normalize_path("/workdir/./sections/intro.tex", repo, container_root="/workdir/") == (
        "sections/intro.tex"
    )
    assert normalize_path("/workdir", repo, container_root="/workdir") is None
    assert normalize_path("/workdirx/a.tex", repo, container_root="/workdir") is None
    # the default container path is the host path, which needs no mapping
    assert normalize_path("/home/ada/paper/a.tex", repo, container_root=None) == "a.tex"


@pytest.fixture
def linked(tmp_path: Path) -> tuple[Path, Path]:
    """(real repo dir, the same dir reached through a symlinked parent)."""
    real = tmp_path / "real" / "paper"
    (real / "sections").mkdir(parents=True)
    (real / "sections" / "intro.tex").write_text("x")
    (real / ".rewrite" / "build").mkdir(parents=True)
    (real / ".rewrite" / "build" / "main.bbl").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    return real, tmp_path / "link" / "paper"


def test_normalize_path_symlinked_repo_root(linked: tuple[Path, Path]) -> None:
    real, via_link = linked
    assert via_link.resolve() == real.resolve()
    # TeX ran in the real dir, the app was given the link (and the other way round)
    assert normalize_path(f"{real}/./sections/intro.tex", via_link) == "sections/intro.tex"
    assert normalize_path(f"{via_link}/./sections/intro.tex", real) == "sections/intro.tex"
    assert normalize_path(f"{via_link}/sections/intro.tex", via_link) == "sections/intro.tex"
    assert normalize_path(f"{real.parent}/elsewhere.tex", via_link) is None


def test_normalize_path_symlinked_file_inside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "paper"
    repo.mkdir()
    outside = tmp_path / "shared.tex"
    outside.write_text("x")
    (repo / "shared.tex").symlink_to(outside)
    # TeX records the name it opened, which is the in-repo symlink
    assert normalize_path(f"{repo}/./shared.tex", repo) == "shared.tex"


def test_normalize_path_build_dir(linked: tuple[Path, Path]) -> None:
    real, _ = linked
    build = real / ".rewrite" / "build"
    # a source file reported under the build dir (relative name joined with the build dir)
    assert normalize_path(f"{build}/./sections/intro.tex", real, build_dir=build) == (
        "sections/intro.tex"
    )
    # a generated file that only exists in the build dir
    assert normalize_path("main.bbl", real, build_dir=build) == ".rewrite/build/main.bbl"
    assert normalize_path(f"{build}/main.bbl", real, build_dir=build) == (".rewrite/build/main.bbl")


# ------------------------------------------------------------- forward / reverse, fake synctex


class FakeRun:
    """A SynctexRun that records its arguments and answers from a table."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "view":
            name = args[2].split(":", 2)[2]
            return self.answers.get(name, "")
        return self.answers.get("edit", "")


@pytest.fixture
def fake_build(tmp_path: Path) -> tuple[Path, Path]:
    """(repo, build dir) with a dummy PDF and a .synctex.gz listing a few inputs."""
    repo = tmp_path / "paper"
    build = repo / ".rewrite" / "build"
    build.mkdir(parents=True)
    (repo / "sections").mkdir()
    (repo / "sections" / "intro.tex").write_text("x")
    (build / "main.pdf").write_bytes(b"%PDF-1.5\n")
    lines = [
        "SyncTeX Version:1",
        f"Input:1:{repo}/./main.tex",
        "Input:2:/usr/share/texlive/texmf-dist/tex/latex/base/article.cls",
        f"Input:3:{repo}/./sections/intro.tex",
        "Output:pdf",
        "Content:",
        f"Input:4:{repo}/.rewrite/build/main.bbl",
    ]
    with gzip.open(build / "main.synctex.gz", "wt") as fh:
        fh.write("\n".join(lines) + "\n")
    return repo, build


def test_recorded_inputs(fake_build: tuple[Path, Path]) -> None:
    repo, build = fake_build
    names = recorded_inputs(build / "main.synctex.gz")
    assert names is not None and len(names) == 4
    assert names[2] == f"{repo}/./sections/intro.tex"
    (build / "broken.synctex.gz").write_bytes(b"not gzip")
    assert recorded_inputs(build / "broken.synctex.gz") is None
    assert recorded_inputs(build / "missing.synctex.gz") is None


def test_forward_passes_the_recorded_input_name(fake_build: tuple[Path, Path]) -> None:
    repo, build = fake_build
    view = (SYNCTEX_FIXTURES / "view-intro-6.txt").read_text()
    run = FakeRun({f"{repo}/./sections/intro.tex": view})
    boxes = forward(repo, build, "main.tex", "sections/intro.tex", 6, run=run)
    assert run.calls == [
        ["view", "-i", f"6:0:{repo}/./sections/intro.tex", "-o", ".rewrite/build/main.pdf"]
    ]
    first = boxes[0]
    assert (first.page, first.x, first.w, first.h) == (1, 71.999985, 468.0, 10.909073)
    assert first.y == pytest.approx(400.241211 - 10.909073)
    assert len(boxes) == 2


def test_forward_falls_back_to_relative_then_absolute(fake_build: tuple[Path, Path]) -> None:
    repo, build = fake_build
    view = (SYNCTEX_FIXTURES / "view-intro-6.txt").read_text()
    run = FakeRun({str(repo / "sections/intro.tex"): view})
    boxes = forward(repo, build, "main.tex", "./sections/intro.tex", 6, col=3, run=run)
    assert [c[2] for c in run.calls] == [
        f"6:3:{repo}/./sections/intro.tex",
        "6:3:sections/intro.tex",
        f"6:3:{repo}/sections/intro.tex",
    ]
    assert len(boxes) == 2


def test_forward_for_a_file_that_is_not_an_input(fake_build: tuple[Path, Path]) -> None:
    repo, build = fake_build
    run = FakeRun({})
    assert forward(repo, build, "main.tex", "notes.tex", 1, run=run) == []
    assert run.calls == []  # known from the .synctex.gz; synctex is not even started


def test_forward_dedupes_boxes(fake_build: tuple[Path, Path]) -> None:
    repo, build = fake_build
    record = "Output:x\nPage:1\nh:1\nv:20\nW:3\nH:4\n"
    run = FakeRun({f"{repo}/./main.tex": record * 3})
    assert forward(repo, build, "main.tex", "main.tex", 1, run=run) == [
        SyncBox(page=1, x=1.0, y=16.0, w=3.0, h=4.0)
    ]


def test_reverse_skips_files_outside_the_repo_and_generated_files(
    fake_build: tuple[Path, Path],
) -> None:
    repo, build = fake_build
    edit = (
        "SyncTeX result begin\n"
        "Output:x\nInput:/usr/share/texlive/article.cls\nLine:3\nColumn:-1\n"
        f"Output:x\nInput:{repo}/.rewrite/build/main.bbl\nLine:9\nColumn:-1\n"
        f"Output:x\nInput:{repo}/./sections/intro.tex\nLine:4\nColumn:-1\n"
        "SyncTeX result end\n"
    )
    run = FakeRun({"edit": edit})
    found = reverse(repo, build, "main.tex", 1, 306.0, 381.2349, run=run)
    assert found == SyncEdit(file="sections/intro.tex", line=4, col=0)
    assert run.calls == [["edit", "-o", "1:306.000:381.235:.rewrite/build/main.pdf"]]
    assert reverse(repo, build, "main.tex", 1, 1, 1, run=FakeRun({})) is None


def test_queries_need_a_build(tmp_path: Path) -> None:
    build = tmp_path / ".rewrite" / "build"
    build.mkdir(parents=True)
    with pytest.raises(NoSynctexData, match="no PDF"):
        forward(tmp_path, build, "main.tex", "main.tex", 1, run=FakeRun({}))
    (build / "main.pdf").write_bytes(b"%PDF")
    with pytest.raises(NoSynctexData, match="SyncTeX"):
        reverse(tmp_path, build, "main.tex", 1, 1, 1, run=FakeRun({}))


# ------------------------------------------------------------------ choosing how to run synctex


def test_select_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert select_runner(tmp_path, CompilerConfig(), synctex=shutil.which("sh") or "sh")
    with pytest.raises(SynctexUnavailable, match="not found on PATH"):
        select_runner(tmp_path, CompilerConfig(), synctex="no-such-synctex")
    with pytest.raises(SynctexUnavailable, match="docker was not found"):
        select_runner(
            tmp_path, CompilerConfig(kind="docker"), synctex="no-such-synctex", docker="nope"
        )
    docker = fake_exe(tmp_path / "bin", "docker", "exit 0\n")
    run = select_runner(
        tmp_path, CompilerConfig(kind="docker"), synctex="no-such-synctex", docker=str(docker)
    )
    assert run(["view"]) == ""


def test_docker_runner_command(tmp_path: Path) -> None:
    argv = tmp_path / "argv.txt"
    docker = fake_exe(
        tmp_path / "bin",
        "docker",
        f'printf "%s\\n" "$@" > {argv}\ncat {SYNCTEX_FIXTURES / "view-intro-6.txt"}\n',
    )
    repo = tmp_path / "paper"
    repo.mkdir()
    run = docker_runner(repo, "texlive/texlive:TL2024-historic", docker=str(docker))
    out = run(["view", "-i", "6:0:sections/intro.tex", "-o", ".rewrite/build/main.pdf"])
    assert len(parse_view_output(out)) == 2
    args = argv.read_text().splitlines()
    name = args[args.index("--name") + 1]
    assert name.startswith("rewrite-")
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    assert args == [
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "-v",
        f"{repo}:{repo}",
        "-w",
        str(repo),
        "-u",
        uid_gid,
        "-e",
        "HOME=/tmp",
        "texlive/texlive:TL2024-historic",
        "synctex",
        "view",
        "-i",
        "6:0:sections/intro.tex",
        "-o",
        ".rewrite/build/main.pdf",
    ]


def test_runner_errors_and_timeout(tmp_path: Path) -> None:
    from app.synctex import SynctexError, SynctexTimeout, host_runner

    failing = fake_exe(tmp_path / "bin", "synctex", "echo 'SyncTeX ERROR: boom' >&2\nexit 255\n")
    with pytest.raises(SynctexError, match="exited with 255: SyncTeX ERROR: boom"):
        host_runner(tmp_path, synctex=str(failing))(["view"])
    slow = fake_exe(tmp_path / "bin2", "synctex", "exec sleep 10\n")
    with pytest.raises(SynctexTimeout):
        host_runner(tmp_path, synctex=str(slow), timeout_s=0.3)(["view"])


# ------------------------------------------------------------------ the real thing


INTRO_LINES = (4, 11, 22)  # first lines of three paragraphs in sections/intro.tex


@requires_synctex
@pytest.mark.parametrize("line", INTRO_LINES)
def test_roundtrip_on_the_sample_paper(compiled_repo: Path, line: int) -> None:
    build = compiled_repo / ".rewrite" / "build"
    boxes = forward(compiled_repo, build, "main.tex", "sections/intro.tex", line)
    assert boxes, f"no box for intro.tex:{line}"
    box = boxes[0]
    assert box.page == 1 and box.w > 0 and box.h > 0
    found = reverse(
        compiled_repo, build, "main.tex", box.page, box.x + box.w / 2, box.y + box.h / 2
    )
    assert found is not None
    assert found.file == "sections/intro.tex"
    assert abs(found.line - line) <= 1


@requires_synctex
def test_coordinates_grow_downwards(compiled_repo: Path) -> None:
    """The documented convention: a line near the top of page 1 has a small y."""
    build = compiled_repo / ".rewrite" / "build"
    abstract = forward(compiled_repo, build, "main.tex", "main.tex", 22)[0]
    intro = forward(compiled_repo, build, "main.tex", "sections/intro.tex", 22)[0]
    design = forward(compiled_repo, build, "main.tex", "sections/method.tex", 5)[0]
    assert abstract.page == intro.page == design.page == 1
    assert 0 < abstract.y < intro.y < design.y < 792  # US letter is 792 pt tall
    # Within a point of what pdftotext -bbox reports for these lines (see app/synctex.py).
    assert abstract.y == pytest.approx(258.60, abs=1.0)
    assert intro.y == pytest.approx(552.59, abs=1.0)


# ------------------------------------------------------------------ API


@pytest.fixture
def compiled_client(compiled_repo: Path):  # type: ignore[no-untyped-def]
    with make_client(compiled_repo) as c:
        yield c


@requires_synctex
def test_api_forward_and_reverse(compiled_client: TestClient) -> None:
    body = compiled_client.get(
        "/api/synctex/forward", params={"file": "sections/intro.tex", "line": 4}
    ).json()
    assert set(body) == {"boxes"} and body["boxes"]
    box = body["boxes"][0]
    assert set(box) == {"page", "x", "y", "w", "h"} and box["page"] == 1
    found = compiled_client.get(
        "/api/synctex/reverse",
        params={"page": 1, "x": box["x"] + box["w"] / 2, "y": box["y"] + box["h"] / 2},
    ).json()
    assert found == {"file": "sections/intro.tex", "line": 4, "col": 0}

    # ./-prefixed and explicit col are accepted; a file that is not an input has no boxes
    again = compiled_client.get(
        "/api/synctex/forward", params={"file": "./sections/intro.tex", "line": 4, "col": 0}
    ).json()
    assert again == body
    none = compiled_client.get("/api/synctex/forward", params={"file": "refs.bib", "line": 1})
    assert none.status_code == 200 and none.json() == {"boxes": []}
    off = compiled_client.get("/api/synctex/reverse", params={"page": 99, "x": 10, "y": 10})
    assert off.status_code == 404 and "detail" in off.json()


@pytest.mark.parametrize(
    ("url", "params"),
    [
        ("/api/synctex/forward", {"line": 1}),
        ("/api/synctex/forward", {"file": "main.tex"}),
        ("/api/synctex/forward", {"file": "main.tex", "line": 0}),
        ("/api/synctex/forward", {"file": "main.tex", "line": "abc"}),
        ("/api/synctex/forward", {"file": "main.tex", "line": 1, "col": -1}),
        ("/api/synctex/forward", {"file": "../x.tex", "line": 1}),
        ("/api/synctex/forward", {"file": "/etc/passwd", "line": 1}),
        ("/api/synctex/reverse", {"page": 0, "x": 1, "y": 1}),
        ("/api/synctex/reverse", {"page": 1, "x": "nan", "y": 1}),
        ("/api/synctex/reverse", {"page": 1, "x": 1}),
        ("/api/synctex/reverse", {"page": 1, "x": "left", "y": 1}),
    ],
)
def test_api_bad_args_are_400(client: TestClient, url: str, params: dict[str, object]) -> None:
    # arguments are checked before anything else, so no compile is needed
    response = client.get(url, params=params)
    assert response.status_code == 400, response.json()
    assert isinstance(response.json()["detail"], str)


def test_api_404_before_compile(client: TestClient) -> None:
    forward_r = client.get("/api/synctex/forward", params={"file": "main.tex", "line": 1})
    reverse_r = client.get("/api/synctex/reverse", params={"page": 1, "x": 1, "y": 1})
    assert forward_r.status_code == 404 and "compile first" in forward_r.json()["detail"]
    assert reverse_r.status_code == 404


def test_api_409_without_repo(bare_client: TestClient) -> None:
    assert bare_client.get("/api/synctex/forward?file=a.tex&line=1").status_code == 409
    assert bare_client.get("/api/synctex/reverse?page=1&x=1&y=1").status_code == 409


def test_api_503_without_synctex(
    fake_build: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repo import init_repo

    repo, _ = fake_build
    (repo / "main.tex").write_text("\\documentclass{article}\n")
    init_repo(repo)
    real_which = shutil.which
    monkeypatch.setattr(
        "app.synctex.shutil.which",
        lambda name, *a, **k: None if name == "synctex" else real_which(name, *a, **k),
    )
    with make_client(repo) as c:
        response = c.get("/api/synctex/forward", params={"file": "main.tex", "line": 1})
    assert response.status_code == 503 and "synctex" in response.json()["detail"]
