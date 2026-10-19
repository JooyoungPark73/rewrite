"""Cross-runner merge of findings (phase-6 brief "Decisions fixed": merge rule). Pure
functions over stored findings; nothing here touches the database or the disk.

Two findings from different runs agree when they are in the same file and either
- their anchor line ranges overlap, or
- their whitespace-collapsed quotes share a common substring at least half as long as the
  shorter quote (`QUOTE_SHARE`).

A merged entry holds at most one finding per run, so its `agreement` is the number of runs
that reported it and two findings of one run never agree with each other. Runs are merged
in the order given: each finding of the next run joins the best-agreeing entry that has no
finding from that run yet (strongest pairs first: quote share, then overlapping lines, then
list order), or starts an entry of its own.

Entries are sorted by agreement (descending), then file (include order; files the order
does not list come after, alphabetically), then first line, then last line. An entry spans
its members' anchor ranges; its severity is the highest of its members; its level and
category are those of its first member (the first run's finding).
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from app.findings import SEVERITIES, Finding, Severity, collapse

QUOTE_SHARE = 0.5


class MergedFinding(BaseModel):
    """One entry of a group's merged list (phase-6 API contract)."""

    key: str  # the member finding ids joined by "+": stable while the findings exist
    file: str
    line_start: int  # the members' anchor ranges, joined
    line_end: int
    severity: Severity  # the highest of the members
    level: str
    category: str
    agreement: int  # how many runs reported it
    runners: list[str]  # the runner of each member, in run order
    findings: list[Finding]  # the members, in run order


@dataclass(frozen=True)
class RunFindings:
    """The findings of one run, with the runner that produced them."""

    run_id: str
    runner: str
    findings: Sequence[Finding]


@dataclass(frozen=True)
class _Member:
    position: int  # the run's position in the merge
    index: int  # the finding's position in its run's list
    runner: str
    finding: Finding


def longest_common_substring(a: str, b: str) -> int:
    """Length of the longest string that occurs in both `a` and `b`."""
    if not a or not b:
        return 0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return matcher.find_longest_match(0, len(a), 0, len(b)).size


def quote_share(a: str, b: str) -> float:
    """The longest common substring of the two whitespace-collapsed quotes, as a fraction of
    the shorter one (0.0 when either is empty)."""
    left, right = collapse(a), collapse(b)
    if not left or not right:
        return 0.0
    return longest_common_substring(left, right) / min(len(left), len(right))


def overlap_lines(a: Finding, b: Finding) -> int:
    """How many lines the two anchor ranges share (0 when they do not overlap)."""
    first = max(a.anchor_line_start, b.anchor_line_start)
    last = min(a.anchor_line_end, b.anchor_line_end)
    return max(0, last - first + 1)


def agreement_strength(a: Finding, b: Finding) -> tuple[float, int] | None:
    """(quote share, overlapping lines) when the two findings agree, else None."""
    if a.file != b.file:
        return None
    share = quote_share(a.quote, b.quote)
    lines = overlap_lines(a, b)
    if lines > 0 or share >= QUOTE_SHARE:
        return share, lines
    return None


def agree(a: Finding, b: Finding) -> bool:
    return agreement_strength(a, b) is not None


def _file_rank(file: str, order: dict[str, int]) -> tuple[int, str]:
    return (order.get(file, len(order)), file)


def _entry(members: list[_Member]) -> MergedFinding:
    members = sorted(members, key=lambda m: (m.position, m.index))
    findings = [m.finding for m in members]
    first = findings[0]
    severity = max((f.severity for f in findings), key=SEVERITIES.index)
    return MergedFinding(
        key="+".join(f.id for f in findings),
        file=first.file,
        line_start=min(f.anchor_line_start for f in findings),
        line_end=max(f.anchor_line_end for f in findings),
        severity=severity,
        level=first.level,
        category=first.category,
        agreement=len(members),
        runners=[m.runner for m in members],
        findings=findings,
    )


def _sorted(
    entries: list[tuple[MergedFinding, tuple[int, int]]], file_order: Sequence[str]
) -> list[MergedFinding]:
    order = {file: rank for rank, file in reversed(list(enumerate(file_order)))}
    entries.sort(
        key=lambda item: (
            -item[0].agreement,
            _file_rank(item[0].file, order),
            item[0].line_start,
            item[0].line_end,
            item[1],
        )
    )
    return [entry for entry, _ in entries]


def merge_findings(
    runs: Sequence[RunFindings], file_order: Sequence[str] = ()
) -> list[MergedFinding]:
    """Merge the findings of several runs by agreement (see the module docstring)."""
    clusters: list[list[_Member]] = []
    for position, run in enumerate(runs):
        candidates: list[tuple[tuple[float, int], int, int]] = []
        for index, finding in enumerate(run.findings):
            for cluster_index, cluster in enumerate(clusters):
                strengths = [
                    strength
                    for member in cluster
                    if member.position != position
                    and (strength := agreement_strength(finding, member.finding)) is not None
                ]
                if strengths:
                    candidates.append((max(strengths), index, cluster_index))
        candidates.sort(key=lambda c: (-c[0][0], -c[0][1], c[1], c[2]))
        used_findings: set[int] = set()
        used_clusters: set[int] = set()
        for _, index, cluster_index in candidates:
            if index in used_findings or cluster_index in used_clusters:
                continue
            used_findings.add(index)
            used_clusters.add(cluster_index)
            clusters[cluster_index].append(
                _Member(position, index, run.runner, run.findings[index])
            )
        for index, finding in enumerate(run.findings):
            if index not in used_findings:
                clusters.append([_Member(position, index, run.runner, finding)])
    entries = []
    for cluster in clusters:
        first = min(cluster, key=lambda m: (m.position, m.index))
        entries.append((_entry(cluster), (first.position, first.index)))
    return _sorted(entries, file_order)


def singletons(runs: Sequence[RunFindings], file_order: Sequence[str] = ()) -> list[MergedFinding]:
    """Every finding as its own entry (agreement 1), in file and line order: the merged view
    of a fan-out group, whose runs cover different scopes."""
    entries = [
        (_entry([_Member(position, index, run.runner, finding)]), (position, index))
        for position, run in enumerate(runs)
        for index, finding in enumerate(run.findings)
    ]
    return _sorted(entries, file_order)


def agreements_count(merged: Sequence[MergedFinding]) -> int:
    """How many merged entries more than one run reported."""
    return sum(1 for entry in merged if entry.agreement >= 2)
