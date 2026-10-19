"""The claims x sections coverage matrix (PLAN.md "Spec editor", right rail; phase-5 brief).

The count for (claim, section) is the number of nodes in that section's subtree (the section
node itself and its paragraphs) whose `claims` contain the claim id. An unreferenced claim
is a row of zeros; an empty section is a column of zeros. Pure: reads a spec, writes nothing.
"""

from __future__ import annotations

from pydantic import BaseModel

from app import skeleton
from app.skeleton import DocScan
from app.spec import Node, PaperSpec


class CoverageClaim(BaseModel):
    id: str
    says: str


class CoverageSection(BaseModel):
    id: str
    says: str
    heading: str | None  # the LaTeX heading text; null for a planned section


class Coverage(BaseModel):
    claims: list[CoverageClaim]
    sections: list[CoverageSection]
    cells: dict[str, dict[str, int]]  # claim id → section id → count (zeros included)
    unreferenced_claims: list[str]
    empty_sections: list[str]


def _subtree(node: Node) -> list[Node]:
    out = [node]
    for child in node.children:
        out.extend(_subtree(child))
    return out


def section_heading(node: Node, scan: DocScan | None) -> str | None:
    """The heading text of a section anchor: from `\\section{...}` directly, or for a
    `\\label{...}` anchor the heading carrying that label in the scanned source."""
    if node.anchor is None:
        return None
    marker = node.anchor.marker.strip()
    match = skeleton.SECTION_MARKER_RE.match(marker)
    if match:
        return skeleton.collapse(match.group("text"))
    label = skeleton.LABEL_MARKER_RE.match(marker)
    if label is None or scan is None:
        return None
    for heading in scan.headings:
        if heading.label == label.group("label").strip():
            return heading.heading
    return None


def needs_scan(spec: PaperSpec) -> bool:
    """Only label-anchored sections need the source to name their heading."""
    return any(
        section.anchor is not None and section.anchor.marker.strip().startswith("\\label")
        for section in spec.tree.children
    )


def compute_coverage(spec: PaperSpec, scan: DocScan | None = None) -> Coverage:
    sections = list(spec.tree.children)
    cells: dict[str, dict[str, int]] = {claim.id: {} for claim in spec.claims}
    for section in sections:
        nodes = _subtree(section)
        for claim in spec.claims:
            cells[claim.id][section.id] = sum(1 for node in nodes if claim.id in node.claims)
    return Coverage(
        claims=[CoverageClaim(id=claim.id, says=claim.says) for claim in spec.claims],
        sections=[
            CoverageSection(id=s.id, says=s.says, heading=section_heading(s, scan))
            for s in sections
        ],
        cells=cells,
        unreferenced_claims=[c.id for c in spec.claims if not any(cells[c.id].values())],
        empty_sections=[s.id for s in sections if not any(cells[c.id][s.id] for c in spec.claims)],
    )
