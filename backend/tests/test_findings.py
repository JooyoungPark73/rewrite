"""app/findings.py: schema, output parsing, the rule validator, anchoring, re-anchoring."""

from __future__ import annotations

from typing import Any

import pytest

from app.findings import (
    FINDING_KEYS,
    FindingIn,
    OutputParseError,
    Rejected,
    anchor_new,
    extract_json,
    findings_schema,
    has_rewrite_text,
    locate,
    parse_output,
    reanchor,
    resolve_file,
    validate_findings,
    validate_item,
)


def finding(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "file": "sections/intro.tex",
        "line_start": 5,
        "line_end": 6,
        "quote": "platforms bill tenants per invocation",
        "level": "paragraph",
        "category": "topic-sentence",
        "severity": "major",
        "problem": "The first sentence describes billing, not the paragraph's point.",
        "why": "Readers expect the claim first.",
        "json_ref": "p-1",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- schema


def test_schema_has_exactly_the_finding_keys() -> None:
    schema = findings_schema()
    assert schema["required"] == ["findings"] and schema["additionalProperties"] is False
    item = schema["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    assert tuple(item["required"]) == FINDING_KEYS
    assert set(item["properties"]) == set(FINDING_KEYS)
    assert not {"replacement", "rewrite", "suggestion", "fix"} & set(item["properties"])
    assert item["properties"]["json_ref"]["type"] == ["string", "null"]
    assert item["properties"]["severity"]["enum"] == ["minor", "major", "critical"]


# --------------------------------------------------------------------------- parsing


def test_plain_json() -> None:
    parsed = parse_output('{"findings": [{"a": 1}]}')
    assert parsed.method == "json" and parsed.items == [{"a": 1}] and parsed.rejected == []


def test_preamble_and_fenced_json() -> None:
    text = (
        "Great paper overall! Here is what I found:\n"
        '```json\n{"findings": [{"first": true}]}\n```\n'
        "And a corrected version:\n"
        '```json\n{"findings": [{"last": true}]}\n```\n'
        "Hope this helps."
    )
    parsed = parse_output(text)
    assert parsed.method == "fence" and parsed.items == [{"last": True}]


def test_untagged_fence_and_brace_fallback() -> None:
    assert extract_json('Sure.\n```\n{"findings": []}\n```')[1] == "fence"
    value, method = extract_json('Preamble text {"findings": [{"x": "y"}]} trailing words')
    assert method == "braces" and value == {"findings": [{"x": "y"}]}


def test_structured_output_wins_over_text() -> None:
    parsed = parse_output('{"findings": [{"from": "text"}]}', {"findings": [{"from": "schema"}]})
    assert parsed.method == "structured" and parsed.items == [{"from": "schema"}]


def test_bare_list_and_extra_top_level_keys() -> None:
    assert parse_output("[1, 2]").items == [1, 2]
    parsed = parse_output('{"findings": [], "summary": "Excellent work", "score": 9}')
    assert parsed.items == []
    assert parsed.rejected == [
        Rejected(index=-1, reason="unknown_field:summary", raw="Excellent work"),
        Rejected(index=-1, reason="unknown_field:score", raw=9),
    ]


@pytest.mark.parametrize(
    "text",
    ["", "   ", "no json here", '{"issues": []}', '{"findings": "none"}', "42", "{broken"],
)
def test_unparseable_outputs(text: str) -> None:
    with pytest.raises(OutputParseError):
        parse_output(text)


# --------------------------------------------------------------------------- validation


def test_valid_finding() -> None:
    result = validate_item(finding(severity="Major", level="Paragraph", json_ref=""), 0, False)
    assert isinstance(result, FindingIn)
    assert result.severity == "major" and result.level == "paragraph" and result.json_ref is None


def test_swapped_line_range_is_ordered() -> None:
    result = validate_item(finding(line_start=9, line_end=4), 0, False)
    assert isinstance(result, FindingIn) and (result.line_start, result.line_end) == (4, 9)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"replacement": "Use this sentence instead."}, "unknown_field:replacement"),
        ({"suggestion": "x"}, "unknown_field:suggestion"),
        ({"severity": "blocker"}, "invalid:severity"),
        ({"level": "chapter"}, "invalid:level"),
        ({"line_start": 0}, "invalid:line_start"),
        ({"line_end": "6"}, "invalid:line_end"),
        ({"line_start": True}, "invalid:line_start"),
        ({"category": "  "}, "invalid:category"),
        ({"file": ""}, "invalid:file"),
        ({"json_ref": 3}, "invalid:json_ref"),
        ({"quote": None}, "invalid:quote"),
        ({"problem": ""}, "praise"),
        ({"problem": "Good use of examples in this paragraph."}, "praise"),
        ({"problem": "  excellent framing"}, "praise"),
        ({"problem": "Well written and clear."}, "praise"),
    ],
)
def test_rejections(overrides: dict[str, Any], reason: str) -> None:
    result = validate_item(finding(**overrides), 3, False)
    assert isinstance(result, Rejected)
    assert result.reason == reason and result.index == 3
    assert result.raw == finding(**overrides)


def test_missing_field_and_non_object() -> None:
    raw = finding()
    del raw["why"]
    result = validate_item(raw, 0, False)
    assert isinstance(result, Rejected) and result.reason == "missing_field:why"
    result = validate_item("just a string", 1, False)
    assert isinstance(result, Rejected) and result.reason == "invalid:finding"


REWRITES = [
    'Replace with "the platform boots a sandbox and loads the runtime lazily on demand"',
    "The sentence should read: “Warm pools waste memory on functions that are rarely invoked.”",
    "Rewrite it as 'Snapshot restore skips initialization but pays for every page fault.'",
    'Change to "We observe that page sets are stable and small across invocations."',
    'Instead write "This paper shows that predictive prefetching beats warm pools at the tail."',
    'The opening → "Cold starts are frequent because most functions run rarely in production."',
]


@pytest.mark.parametrize("text", REWRITES)
def test_rewrite_text_rejected_unless_allowed(text: str) -> None:
    assert has_rewrite_text(text)
    for field in ("problem", "why"):
        raw = finding(**{field: "The sentence is vague. " + text})
        rejected = validate_item(raw, 0, False)
        assert isinstance(rejected, Rejected) and rejected.reason == "rewrite_text"
        assert isinstance(validate_item(raw, 0, True), FindingIn)  # allow_rewrites: true


def test_short_quotes_are_not_rewrites() -> None:
    assert not has_rewrite_text('Replace with "Figure~\\ref{fig:a}"')  # < 8 words
    assert not has_rewrite_text("The claim in “we always win” is stronger than the evidence.")
    assert not has_rewrite_text("It should read more like a result than a hope.")


def test_validate_findings_splits() -> None:
    accepted, rejected = validate_findings(
        [finding(), finding(rewrite="x"), finding(problem="Great section.")], False
    )
    assert [index for index, _ in accepted] == [0]
    assert [(r.index, r.reason) for r in rejected] == [
        (1, "unknown_field:rewrite"),
        (2, "praise"),
    ]


# --------------------------------------------------------------------------- anchoring

LINES = [
    "\\section{Introduction}",
    "",
    "Function-as-a-Service (FaaS) platforms bill tenants per invocation and scale",
    "each function down to zero instances when it is idle.",
    "",
    "Providers mitigate cold starts. Providers mitigate cold starts.",
    "Later, providers mitigate cold starts again.",
]


def test_locate_collapses_whitespace_across_lines() -> None:
    assert locate("bill tenants per invocation and   scale\n each function", LINES, 1) == (3, 4)


def test_locate_prefers_occurrence_nearest_line_start() -> None:
    assert locate("mitigate cold starts", LINES, 1) == (6, 6)
    assert locate("mitigate cold starts", LINES, 7) == (7, 7)


def test_locate_fallbacks() -> None:
    assert locate("   3 | Function-as-a-Service (FaaS) platforms", LINES, 1) == (3, 3)
    assert locate('"each function down to zero"', LINES, 1) == (4, 4)
    assert locate("Function-as-a-Service (FaaS) ... when it is idle.", LINES, 1) == (3, 4)
    assert locate("not in the file", LINES, 1) is None


def test_anchor_new_ok_and_unverified() -> None:
    ok = anchor_new("each function down to zero", LINES, 40, 41)
    assert (ok.state, ok.line_start, ok.line_end) == ("ok", 4, 4)
    missing = anchor_new("paraphrased text", LINES, 6, 99)
    assert (missing.state, missing.line_start, missing.line_end) == ("unverified", 6, 7)
    empty = anchor_new("", LINES, 2, 3)
    assert empty.state == "unverified"


def test_reanchor_transitions() -> None:
    quote = "each function down to zero"
    home = (4, 4)
    same = reanchor(quote, LINES, "ok", 4, 4, home)
    assert (same.state, same.line_start) == ("ok", 4)
    shifted = ["", "", *LINES]
    moved = reanchor(quote, shifted, "ok", 4, 4, home)
    assert (moved.state, moved.line_start, moved.line_end) == ("moved", 6, 6)
    assert reanchor(quote, shifted, "moved", 6, 6, home).state == "moved"
    back = reanchor(quote, LINES, "moved", 6, 6, home)  # the text is back where it was
    assert (back.state, back.line_start) == ("ok", 4)
    edited = [line.replace("down to zero", "to nothing") for line in LINES]
    stale = reanchor(quote, edited, "ok", 4, 4, home)
    assert (stale.state, stale.line_start, stale.line_end) == ("stale", 4, 4)
    assert reanchor(quote, LINES, "stale", 4, 4, home).state == "ok"  # text restored
    assert reanchor(quote, None, "ok", 4, 4, home).state == "stale"  # file deleted
    assert reanchor("never there", LINES, "unverified", 2, 3, (2, 3)).state == "unverified"
    found_later = reanchor(quote, LINES, "unverified", 2, 3, (2, 3))
    assert (found_later.state, found_later.line_start) == ("moved", 4)


def test_reanchor_without_origin_uses_the_current_anchor() -> None:
    quote = "each function down to zero"
    assert reanchor(quote, LINES, "ok", 4, 4).state == "ok"
    assert reanchor(quote, LINES, "moved", 4, 4).state == "moved"
    assert reanchor(quote, ["", *LINES], "ok", 4, 4).state == "moved"


def test_resolve_file() -> None:
    files = {"main.tex": ["\\input{sections/intro}"], "sections/intro.tex": LINES}
    assert resolve_file("sections/intro.tex", "", 1, files) == "sections/intro.tex"
    assert resolve_file("./sections/intro.tex", "", 1, files) == "sections/intro.tex"
    assert resolve_file("wrong.tex", "each function down", 1, files) == "sections/intro.tex"
    assert resolve_file("intro.tex", "nope", 1, files) == "sections/intro.tex"
    assert resolve_file("other.tex", "nope", 1, files) is None
    assert resolve_file("other.tex", "nope", 1, {"a.tex": ["x"]}) == "a.tex"
    assert resolve_file("paper.json", "nope", 1, {"a.tex": ["x"]}) is None
