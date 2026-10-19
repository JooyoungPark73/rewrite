r"""`scan_labels` and GET /api/spec/labels: figure/table/equation labels with captions."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.skeleton import scan_document, scan_labels


def test_sample_paper_labels(initialized_repo: Path) -> None:
    labels = {i.label: i for i in scan_labels(scan_document(initialized_repo, "main.tex"))}
    assert set(labels) == {
        "sec:intro",
        "sec:method",
        "eq:working-set",
        "fig:pipeline",
        "sec:results",
        "tab:latency",
        "sec:conclusion",
    }
    fig = labels["fig:pipeline"]
    assert fig.kind == "figure" and fig.file == "sections/method.tex"
    assert fig.caption is not None and fig.caption.startswith("Restore path in \\textsc{Prefetch}")
    tab = labels["tab:latency"]
    assert tab.kind == "table" and tab.caption == "Cold-start latency in milliseconds."
    eq = labels["eq:working-set"]
    assert eq.kind == "equation" and eq.caption is None
    assert labels["sec:intro"].kind == "section" and labels["sec:intro"].line == 2
    # Document order: intro before method before results.
    order = [i.label for i in scan_labels(scan_document(initialized_repo, "main.tex"))]
    assert order.index("sec:intro") < order.index("fig:pipeline") < order.index("tab:latency")


def test_label_before_caption_and_commented_label(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        "\\begin{document}\n"
        "\\begin{figure}\n"
        "  \\label{fig:early}\n"
        "  \\caption[short]{A caption\n  over two lines}\n"
        "\\end{figure}\n"
        "% \\label{fig:commented}\n"
        "\\begin{table}\\caption{T}\\label{tab:t}\\end{table}\n"
        "\\label{loose}\n"
        "\\label{fig:early}\n"
        "\\end{document}\n"
    )
    labels = scan_labels(scan_document(tmp_path, "main.tex"))
    by = {i.label: i for i in labels}
    assert [i.label for i in labels] == ["fig:early", "tab:t", "loose"]
    assert by["fig:early"].caption == "A caption over two lines"  # caption after the label
    assert by["fig:early"].line == 3
    assert by["tab:t"].kind == "table" and by["tab:t"].caption == "T"
    assert by["loose"].kind == "other" and by["loose"].caption is None


def test_labels_endpoint(client: TestClient) -> None:
    res = client.get("/api/spec/labels")
    assert res.status_code == 200, res.text
    labels = {i["label"]: i for i in res.json()["labels"]}
    assert labels["fig:pipeline"]["kind"] == "figure"
    assert labels["tab:latency"]["file"] == "sections/results.tex"
    assert set(labels["fig:pipeline"]) == {"label", "kind", "file", "line", "caption"}


def test_labels_endpoint_without_repo(bare_client: TestClient) -> None:
    assert bare_client.get("/api/spec/labels").status_code == 409
