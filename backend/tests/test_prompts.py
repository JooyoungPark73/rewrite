"""The default prompt set: front matter per PLAN.md, bodies render with the documented vars."""

from __future__ import annotations

import re

import jinja2
import pytest
import yaml

from app.repo import default_prompts_dir

PROMPTS = default_prompts_dir()
PASSES = sorted((PROMPTS / "passes").glob("*.md"))
EXPECTED = {
    "message-drift": "message",
    "claims-evidence": "claim",
    "section-promises": "section",
    "paragraph-function": "paragraph",
    "clarity": "sentence",
    "epistemic-calibration": "sentence",
    "reviewer-2": "message",
    "latex-hygiene": "sentence",
    "ab-compare": "paragraph",
    "triage": "message",
}
# Passes that are not review passes (front matter `kind`, phase 6); every other pass has no
# `kind` key and is a review pass.
KINDS = {"ab-compare": "compare", "triage": "triage"}
VARS = {
    "spec_outline": "- message: ...",
    "spec_json": "{}",
    "source": "1  \\section{Intro}",
    "scope_name": "Introduction",
    "claims": "- c1: ...",
    "findings_schema": '{"type": "object"}',
}
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def split(path):  # type: ignore[no-untyped-def]
    match = FRONT_MATTER.match(path.read_text(encoding="utf-8"))
    assert match, f"{path.name}: missing YAML front matter"
    return yaml.safe_load(match.group(1)), match.group(2)


def test_all_passes_present() -> None:
    assert {p.stem for p in PASSES} == set(EXPECTED)


@pytest.mark.parametrize("path", PASSES, ids=lambda p: p.stem)
def test_front_matter(path) -> None:  # type: ignore[no-untyped-def]
    meta, _ = split(path)
    assert set(meta) - {"kind"} == {"name", "level", "scope", "allow_rewrites", "model", "thinking"}
    assert meta.get("kind", "review") == KINDS.get(path.stem, "review")
    assert meta["name"] == path.stem
    assert meta["level"] == EXPECTED[path.stem]
    assert meta["scope"] in {"paper", "section", "file", "selection", "ends"}
    assert meta["allow_rewrites"] is (path.stem == "latex-hygiene")
    assert meta["model"] is None and meta["thinking"] is None


@pytest.mark.parametrize("path", PASSES, ids=lambda p: p.stem)
def test_body_renders(path) -> None:  # type: ignore[no-untyped-def]
    _, body = split(path)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    extra = {
        "ab-compare": {"version_a": "A text", "version_b": "B text"},
        "triage": {"findings": "1. sections/intro.tex, line 3"},
    }.get(path.stem, {})
    rendered = env.from_string(body).render(**VARS, **extra)
    assert "Introduction" in rendered
    if path.stem != "ab-compare":
        assert VARS["source"] in rendered and VARS["findings_schema"] in rendered


def test_system_prompt_is_plain_markdown() -> None:
    text = (PROMPTS / "system.md").read_text(encoding="utf-8")
    assert "{{" not in text and "{%" not in text  # copied verbatim into the sandbox
    assert "submitted" in text.lower()
    assert "praise" in text.lower() and "rewrite" in text.lower()


@pytest.mark.parametrize("path", [p for p in PASSES if p.stem not in KINDS], ids=lambda p: p.stem)
def test_review_passes_ask_for_the_findings_object(path) -> None:  # type: ignore[no-untyped-def]
    meta, body = split(path)
    assert body.lstrip().startswith("{#"), "first line is a {# description #} comment"
    assert "{{ findings_schema }}" in body and "{{ source }}" in body
    assert "{{ spec_outline }}" in body or path.stem == "latex-hygiene"
    assert '`{"findings": []}`' in body
    assert f'`"level": "{meta["level"]}"`' in body


def test_system_prompt_states_the_finding_rules() -> None:
    text = (PROMPTS / "system.md").read_text(encoding="utf-8")
    for key in (
        "file",
        "line_start",
        "line_end",
        "quote",
        "severity",
        "problem",
        "why",
        "json_ref",
    ):
        assert f"`{key}`" in text
    assert "`=== file: <path> ===`" in text and "% @p <id>" in text


def test_triage_pass_shows_findings_source_and_verdict_schema() -> None:
    meta, body = split(PROMPTS / "passes" / "triage.md")
    assert meta["kind"] == "triage" and meta["scope"] == "paper"
    for var in ("findings", "source", "spec_outline", "findings_schema", "scope_name"):
        assert "{{ " + var + " }}" in body
    for verdict in ("`keep`", "`wrong`", "`trivial`"):
        assert verdict in body
    assert '`{"findings": []}`' not in body  # it asks for verdicts, not findings
