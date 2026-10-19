"""The cross-runner merge rule (app/merge.py, phase-6 brief "Decisions fixed")."""

from __future__ import annotations

from typing import Any

from app.findings import Finding
from app.merge import (
    RunFindings,
    agree,
    agreements_count,
    longest_common_substring,
    merge_findings,
    quote_share,
    singletons,
)

ORDER = ["main.tex", "sections/intro.tex", "sections/method.tex"]


def finding(fid: str, file: str, start: int, end: int, quote: str, **extra: Any) -> Finding:
    values: dict[str, Any] = {
        "id": fid,
        "run_id": fid.split("-")[0],
        "file": file,
        "line_start": start,
        "line_end": end,
        "quote": quote,
        "level": "paragraph",
        "category": "function",
        "severity": "minor",
        "problem": "The paragraph does two jobs.",
        "why": "x",
        "json_ref": None,
        "state": "open",
        "anchor_state": "ok",
        "anchor_line_start": start,
        "anchor_line_end": end,
        "created_at": 0.0,
    }
    values.update(extra)
    return Finding(**values)


def test_quote_share_uses_the_longest_common_substring() -> None:
    assert longest_common_substring("abcdef", "zzcdez") == 3
    assert longest_common_substring("", "abc") == 0
    # whitespace is collapsed before comparing
    assert quote_share("warm  pools\n keep instances", "warm pools keep instances") == 1.0
    shorter = "the cold start"  # 14 characters, all inside the longer quote
    assert quote_share(shorter, "The price of scaling to zero is the cold start: the first") == 1.0
    assert quote_share("abcdefgh", "abcd____") == 0.5
    assert quote_share("abcdefgh", "abc_____") < 0.5
    assert quote_share("", "abc") == 0.0


def test_agreement_by_overlap_or_quote_in_the_same_file() -> None:
    a = finding("a-0", "sections/intro.tex", 10, 12, "first quote here")
    overlapping = finding("b-0", "sections/intro.tex", 12, 15, "completely different words")
    adjacent = finding("b-1", "sections/intro.tex", 13, 15, "completely different words")
    same_quote_elsewhere = finding("b-2", "sections/intro.tex", 40, 40, "the first quote here!")
    other_file = finding("b-3", "sections/method.tex", 10, 12, "first quote here")
    assert agree(a, overlapping)
    assert not agree(a, adjacent)
    assert agree(a, same_quote_elsewhere)  # lines far apart, quotes share >= 50 %
    assert not agree(a, other_file)  # never across files


def test_merge_pairs_agreements_first_then_file_and_line_order() -> None:
    claude = RunFindings(
        "a",
        "claude",
        [
            finding(
                "a-0", "sections/method.tex", 5, 5, "records the working set", severity="major"
            ),
            finding("a-1", "sections/intro.tex", 20, 21, "Warm pools trade memory for latency"),
            finding("a-2", "main.tex", 30, 30, "cuts median cold-start latency"),
        ],
    )
    codex = RunFindings(
        "b",
        "codex",
        [
            finding("b-0", "sections/intro.tex", 3, 3, "bill tenants per invocation"),
            finding(
                "b-1",
                "sections/intro.tex",
                19,
                19,
                "Warm pools trade memory for latency, and the",
                severity="critical",
                level="sentence",
            ),
            finding("b-2", "sections/method.tex", 2, 6, "something else entirely"),
        ],
    )
    merged = merge_findings([claude, codex], ORDER)
    assert [m.key for m in merged] == ["a-1+b-1", "a-0+b-2", "a-2", "b-0"]
    first = merged[0]
    assert first.agreement == 2 and first.runners == ["claude", "codex"]
    assert [f.id for f in first.findings] == ["a-1", "b-1"]
    assert (first.line_start, first.line_end) == (19, 21)  # the members' ranges joined
    assert first.severity == "critical"  # the highest
    assert first.level == "paragraph"  # from the first run's member
    assert merged[1].severity == "major"
    # singletons: main.tex comes before sections/intro.tex in include order
    assert [(m.agreement, m.file) for m in merged[2:]] == [
        (1, "main.tex"),
        (1, "sections/intro.tex"),
    ]
    assert agreements_count(merged) == 2


def test_no_self_agreement_within_one_run() -> None:
    claude = RunFindings(
        "a",
        "claude",
        [
            finding("a-0", "sections/intro.tex", 10, 12, "one quote"),
            finding("a-1", "sections/intro.tex", 11, 13, "one quote again"),
        ],
    )
    merged = merge_findings([claude], ORDER)
    assert [(m.key, m.agreement) for m in merged] == [("a-0", 1), ("a-1", 1)]
    # with a second run, each entry holds at most one finding per run
    codex = RunFindings("b", "codex", [finding("b-0", "sections/intro.tex", 12, 12, "zzz")])
    merged = merge_findings([claude, codex], ORDER)
    # b-0 overlaps both equally; the tie goes to the earlier entry, and a-1 stays alone
    assert [(m.key, m.agreement) for m in merged] == [("a-0+b-0", 2), ("a-1", 1)]
    assert all(len({f.run_id for f in m.findings}) == len(m.findings) for m in merged)
    assert agreements_count(merged) == 1


def test_best_agreement_wins_a_contested_finding() -> None:
    claude = RunFindings(
        "a",
        "claude",
        [
            finding(
                "a-0", "sections/intro.tex", 10, 14, "Snapshot restore is the main alternative"
            ),
            finding("a-1", "sections/intro.tex", 14, 16, "these page faults dominate restore"),
        ],
    )
    # b-0 overlaps both a-0 and a-1 by lines, but its quote matches a-1
    codex = RunFindings(
        "b",
        "codex",
        [
            finding(
                "b-0", "sections/intro.tex", 14, 14, "these page faults dominate restore latency"
            )
        ],
    )
    merged = merge_findings([claude, codex], ORDER)
    keys = {m.key for m in merged}
    assert keys == {"a-1+b-0", "a-0"}


def test_three_runs_and_ordering_ties() -> None:
    runs = [
        RunFindings(name, runner, [finding(f"{name}-0", "sections/intro.tex", 5, 6, "same span")])
        for name, runner in (("a", "claude"), ("b", "codex"), ("c", "claude"))
    ]
    runs[2] = RunFindings(
        "c",
        "claude",
        [
            finding("c-0", "sections/intro.tex", 6, 6, "same span"),
            finding("c-1", "sections/intro.tex", 1, 1, "an early line"),
        ],
    )
    merged = merge_findings(runs, ORDER)
    assert [(m.key, m.agreement) for m in merged] == [("a-0+b-0+c-0", 3), ("c-1", 1)]
    # files the include order does not list come last, alphabetically
    stray = RunFindings(
        "d", "codex", [finding("d-0", "zz.tex", 1, 1, "q"), finding("d-1", "aa.tex", 1, 1, "q")]
    )
    assert [m.file for m in merge_findings([stray], ORDER)] == ["aa.tex", "zz.tex"]


def test_singletons_for_fanout() -> None:
    runs = [
        RunFindings("a", "claude", [finding("a-0", "sections/method.tex", 3, 3, "x")]),
        RunFindings(
            "b",
            "claude",
            [
                finding("b-0", "sections/intro.tex", 9, 9, "x"),
                finding("b-1", "sections/intro.tex", 2, 2, "x"),
            ],
        ),
    ]
    merged = singletons(runs, ORDER)
    assert [m.key for m in merged] == ["b-1", "b-0", "a-0"]
    assert all(m.agreement == 1 and len(m.runners) == 1 for m in merged)
    assert agreements_count(merged) == 0
    assert merge_findings([], ORDER) == [] and singletons([], ORDER) == []
