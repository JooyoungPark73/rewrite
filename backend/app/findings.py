"""Findings: the JSON schema the model must follow, output parsing, the rule validator and
anchoring (PLAN.md "Review pass" and "Rule enforcement"; phase-4 brief "Decisions").

The rules are enforced here, in code, so editing a prompt cannot weaken them:

- No rewrites. The schema has no replacement field and the validator rejects any finding
  with a key outside the schema (`unknown_field:<key>`, which catches `replacement`,
  `rewrite`, `suggestion`, ...). Unless the pass sets `allow_rewrites: true`, a `problem`
  or `why` that quotes a span of 8 or more words after "replace with", "rewrite as",
  "change to", "instead write", "should read" or an arrow is rejected (`rewrite_text`).
- No praise. A finding whose `problem` is empty or opens with good/great/nice/well/
  excellent/clear/strong is rejected (`praise`). Text around the JSON (a preamble or
  closing remarks) is never parsed.
- Rejected findings are kept with their reason and raw value, so a misbehaving prompt is
  visible in the run detail rather than quietly dropped.

Nothing here writes files; the orchestrator (app/runs.py) stores the results in state.db.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel

Severity = Literal["minor", "major", "critical"]
SEVERITIES: tuple[str, ...] = ("minor", "major", "critical")
FINDING_LEVELS: tuple[str, ...] = ("message", "claim", "section", "paragraph", "sentence")
FINDING_KEYS: tuple[str, ...] = (
    "file",
    "line_start",
    "line_end",
    "quote",
    "level",
    "category",
    "severity",
    "problem",
    "why",
    "json_ref",
)
FindingState = Literal["open", "addressed", "dismissed"]
FINDING_STATES: tuple[str, ...] = ("open", "addressed", "dismissed")
AnchorState = Literal["ok", "unverified", "moved", "stale"]
TriageVerdict = Literal["keep", "wrong", "trivial"]
TRIAGE_VERDICTS: tuple[str, ...] = ("keep", "wrong", "trivial")

REWRITE_MIN_WORDS = 8


# --------------------------------------------------------------------------- schema


def findings_schema() -> dict[str, Any]:
    """The JSON schema for the model's whole answer: `{"findings": [Finding]}`.

    Strict-mode compatible (codex `--output-schema` uses OpenAI structured outputs): every
    object lists all its properties as required and forbids additional ones; the nullable
    `json_ref` is typed `["string", "null"]`. There is deliberately no field for replacement
    text."""
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": list(FINDING_KEYS),
        "properties": {
            "file": {
                "type": "string",
                "description": "Repo-relative path from the `=== file: ... ===` header.",
            },
            "line_start": {
                "type": "integer",
                "description": "First source line of the quoted span (the number before ` | `).",
            },
            "line_end": {
                "type": "integer",
                "description": "Last source line of the quoted span.",
            },
            "quote": {
                "type": "string",
                "description": "A short span copied verbatim from the source, without the "
                "line-number prefix, without ellipses.",
            },
            "level": {"type": "string", "enum": list(FINDING_LEVELS)},
            "category": {
                "type": "string",
                "description": "One of the categories the pass lists.",
            },
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "problem": {
                "type": "string",
                "description": "What is wrong, stated as a problem. No praise, no rewritten text.",
            },
            "why": {
                "type": "string",
                "description": "Why it matters to a reader or reviewer.",
            },
            "json_ref": {
                "type": ["string", "null"],
                "description": "Id of the spec entry the finding concerns, or null.",
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings"],
        "properties": {"findings": {"type": "array", "items": finding}},
    }


def findings_schema_text() -> str:
    return json.dumps(findings_schema(), indent=2)


# --------------------------------------------------------------------------- models


class FindingIn(BaseModel):
    """One validated finding as the model returned it (before anchoring)."""

    file: str
    line_start: int
    line_end: int
    quote: str
    level: str
    category: str
    severity: Severity
    problem: str
    why: str
    json_ref: str | None


class Finding(BaseModel):
    """A stored finding (API shape)."""

    id: str
    run_id: str
    file: str
    line_start: int
    line_end: int
    quote: str
    level: str
    category: str
    severity: Severity
    problem: str
    why: str
    json_ref: str | None
    state: FindingState
    anchor_state: AnchorState
    anchor_line_start: int
    anchor_line_end: int
    created_at: float
    # Phase 6: the verdict of the latest triage that judged this finding (app/triage.py).
    # A verdict never changes `state` by itself; POST /api/findings/dismiss-flagged does.
    triage_verdict: TriageVerdict | None = None
    triage_reason: str | None = None


class Rejected(BaseModel):
    """A finding the validator refused: its index in the model's list, the reason, and the
    raw value. Index -1 marks a problem with the answer as a whole (e.g. extra top-level
    keys next to `findings`)."""

    index: int
    reason: str
    raw: Any


# --------------------------------------------------------------------------- parsing


class OutputParseError(ValueError):
    """The output holds no findings JSON object."""


_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


@dataclass
class ParsedOutput:
    items: list[Any]  # the raw `findings` entries
    method: Literal["structured", "json", "fence", "braces"]
    rejected: list[Rejected]  # top-level problems (index -1)


def _loads(text: str) -> Any:
    return json.loads(text)


def extract_json(text: str) -> tuple[Any, Literal["json", "fence", "braces"]]:
    """The JSON value in a model's text answer.

    In order: the whole text; the last ```json fenced block (then the last fenced block of
    any language that parses); the span from the first `{` to the last `}`. Any preamble or
    closing remark outside the chosen span is ignored. Raises OutputParseError."""
    stripped = text.strip()
    if not stripped:
        raise OutputParseError("the output is empty")
    try:
        return _loads(stripped), "json"
    except json.JSONDecodeError:
        pass
    fences = _FENCE_RE.findall(text)
    tagged = [body for lang, body in fences if lang.lower() == "json"]
    for body in [*reversed(tagged), *reversed([b for lang, b in fences if lang.lower() != "json"])]:
        try:
            return _loads(body.strip()), "fence"
        except json.JSONDecodeError:
            continue
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        try:
            return _loads(text[first : last + 1]), "braces"
        except json.JSONDecodeError as exc:
            raise OutputParseError(f"no parseable JSON object in the output ({exc.msg})") from exc
    raise OutputParseError("no JSON object in the output")


def parse_output(text: str, structured: Any | None = None) -> ParsedOutput:
    """The raw findings list from a run's output.

    Structured output (claude `--json-schema` puts it in the result event) wins when present;
    otherwise the text is parsed with `extract_json`. A bare list is taken as the findings
    list. Other top-level keys are dropped and reported as rejected (index -1)."""
    method: Literal["structured", "json", "fence", "braces"]
    if structured is not None:
        value, method = structured, "structured"
    else:
        value, method = extract_json(text)
    rejected: list[Rejected] = []
    if isinstance(value, list):
        return ParsedOutput(items=value, method=method, rejected=rejected)
    if not isinstance(value, dict):
        raise OutputParseError(f"the output JSON is a {type(value).__name__}, not an object")
    if "findings" not in value:
        raise OutputParseError("the output JSON has no `findings` list")
    items = value["findings"]
    if not isinstance(items, list):
        raise OutputParseError("`findings` is not a list")
    for key in value:
        if key != "findings":
            rejected.append(Rejected(index=-1, reason=f"unknown_field:{key}", raw=value[key]))
    return ParsedOutput(items=items, method=method, rejected=rejected)


# --------------------------------------------------------------------------- validation

PRAISE_RE = re.compile(r"^(good|great|nice|well|excellent|clear|strong)\b", re.IGNORECASE)

_TRIGGERS = (
    r"replace(?:\s+(?:it|this|that|them))?\s+with",
    r"rewrite(?:\s+(?:it|this|that|them))?\s+as",
    r"change(?:\s+(?:it|this|that|them))?\s+to",
    r"instead\s+write",
    r"should\s+read",
    r"→",
    r"->",
)
_QUOTES = (
    r'"(?P<q1>[^"]+)"',
    r"“(?P<q2>[^”]+)”",
    r"‘(?P<q3>[^’]+)’",
    r"``(?P<q4>.+?)''",
    r"«(?P<q5>[^»]+)»",
    r"'(?P<q6>[^']+)'",
    r"`(?P<q7>[^`]+)`",
)
REWRITE_RE = re.compile(
    r"(?:" + "|".join(_TRIGGERS) + r")\s*[:,]?\s*(?:" + "|".join(_QUOTES) + r")",
    re.IGNORECASE | re.DOTALL,
)


def has_rewrite_text(text: str) -> bool:
    """A quoted span of ≥ 8 words introduced by a rewrite phrase ("replace with", "should
    read", "→", ...)."""
    for match in REWRITE_RE.finditer(text):
        span = next((g for g in match.groups() if g is not None), "")
        if len(span.split()) >= REWRITE_MIN_WORDS:
            return True
    return False


def is_praise(problem: str) -> bool:
    stripped = problem.strip()
    return not stripped or PRAISE_RE.match(stripped) is not None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_item(item: Any, index: int, allow_rewrites: bool) -> FindingIn | Rejected:
    """Check one raw finding against the schema and the rules (first failure wins)."""

    def reject(reason: str) -> Rejected:
        return Rejected(index=index, reason=reason, raw=item)

    if not isinstance(item, dict):
        return reject("invalid:finding")
    for key in item:
        if key not in FINDING_KEYS:
            return reject(f"unknown_field:{key}")
    for key in FINDING_KEYS:
        if key not in item:
            return reject(f"missing_field:{key}")

    file = item["file"]
    if not isinstance(file, str) or not file.strip():
        return reject("invalid:file")
    for key in ("line_start", "line_end"):
        if not _is_int(item[key]) or item[key] < 1:
            return reject(f"invalid:{key}")
    line_start, line_end = int(item["line_start"]), int(item["line_end"])
    if line_end < line_start:  # models miscount; keep the range, ordered
        line_start, line_end = line_end, line_start
    for key in ("quote", "problem", "why"):
        if not isinstance(item[key], str):
            return reject(f"invalid:{key}")
    level = item["level"]
    if not isinstance(level, str) or level.strip().lower() not in FINDING_LEVELS:
        return reject("invalid:level")
    category = item["category"]
    if not isinstance(category, str) or not category.strip():
        return reject("invalid:category")
    severity = item["severity"]
    if not isinstance(severity, str) or severity.strip().lower() not in SEVERITIES:
        return reject("invalid:severity")
    json_ref = item["json_ref"]
    if json_ref is not None and not isinstance(json_ref, str):
        return reject("invalid:json_ref")

    problem, why = item["problem"], item["why"]
    if is_praise(problem):
        return reject("praise")
    if not allow_rewrites and (has_rewrite_text(problem) or has_rewrite_text(why)):
        return reject("rewrite_text")

    return FindingIn(
        file=file.strip(),
        line_start=line_start,
        line_end=line_end,
        quote=item["quote"],
        level=level.strip().lower(),
        category=category.strip(),
        severity=severity.strip().lower(),  # type: ignore[arg-type]
        problem=problem.strip(),
        why=why.strip(),
        json_ref=(json_ref.strip() or None) if isinstance(json_ref, str) else None,
    )


def validate_findings(
    items: Sequence[Any], allow_rewrites: bool
) -> tuple[list[tuple[int, FindingIn]], list[Rejected]]:
    """(accepted (index, finding) pairs, rejected) for a raw findings list."""
    accepted: list[tuple[int, FindingIn]] = []
    rejected: list[Rejected] = []
    for index, item in enumerate(items):
        result = validate_item(item, index, allow_rewrites)
        if isinstance(result, Rejected):
            rejected.append(result)
        else:
            accepted.append((index, result))
    return accepted, rejected


# --------------------------------------------------------------------------- anchoring

_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\s*\|\s?", re.MULTILINE)
_ELLIPSIS_RE = re.compile(r"\.\.\.|…")
_EDGE_QUOTES = "\"'“”‘’`"


def collapse(text: str) -> str:
    return " ".join(text.split())


def _collapsed_with_lines(lines: Sequence[str]) -> tuple[str, list[int]]:
    """The file text with whitespace runs collapsed to one space, and for each character of
    it the 1-based line it came from."""
    chars: list[str] = []
    line_of: list[int] = []
    pending_space = False
    for number, line in enumerate(lines, start=1):
        for ch in line:
            if ch.isspace():
                pending_space = bool(chars)
                continue
            if pending_space:
                chars.append(" ")
                line_of.append(number)
                pending_space = False
            chars.append(ch)
            line_of.append(number)
        pending_space = bool(chars)  # the line break
    return "".join(chars), line_of


def _occurrences(haystack: str, needle: str) -> list[int]:
    found: list[int] = []
    start = haystack.find(needle)
    while start != -1:
        found.append(start)
        start = haystack.find(needle, start + 1)
    return found


def _nearest(
    starts: list[int], length: int, line_of: list[int], near: int
) -> tuple[int, int] | None:
    if not starts:
        return None
    best = min(starts, key=lambda s: (abs(line_of[s] - near), s))
    return line_of[best], line_of[best + length - 1]


def locate(quote: str, lines: Sequence[str], near: int) -> tuple[int, int] | None:
    """(line_start, line_end) of `quote` in the file, matching with whitespace collapsed;
    the occurrence nearest line `near` wins. Falls back to the quote without line-number
    prefixes, without surrounding quote marks, and (for a quote with an ellipsis) to its
    first and last fragments. None when the quote is not in the file."""
    text, line_of = _collapsed_with_lines(lines)
    candidates = [quote, _NUMBER_PREFIX_RE.sub("", quote)]
    candidates.append(candidates[-1].strip().strip(_EDGE_QUOTES))
    for candidate in candidates:
        needle = collapse(candidate)
        if not needle:
            continue
        hit = _nearest(_occurrences(text, needle), len(needle), line_of, near)
        if hit is not None:
            return hit
    fragments = [collapse(f) for f in _ELLIPSIS_RE.split(candidates[-1])]
    fragments = [f for f in fragments if len(f) >= 8]
    if len(fragments) >= 2:
        first, last = fragments[0], fragments[-1]
        firsts = _occurrences(text, first)
        hit = _nearest(firsts, len(first), line_of, near)
        if hit is not None:
            start_pos = min(firsts, key=lambda s: (abs(line_of[s] - near), s))
            tail = text.find(last, start_pos + len(first))
            if tail != -1:
                return line_of[start_pos], line_of[tail + len(last) - 1]
            return hit
    return None


@dataclass(frozen=True)
class AnchorResult:
    state: AnchorState
    line_start: int
    line_end: int


def _clamp(line_start: int, line_end: int, length: int) -> tuple[int, int]:
    if length < 1:
        return max(1, line_start), max(1, line_end)
    start = min(max(1, line_start), length)
    end = min(max(start, line_end), length)
    return start, end


def anchor_new(quote: str, lines: Sequence[str], line_start: int, line_end: int) -> AnchorResult:
    """Anchor a new finding: by exact quote ("ok"), else by the line range as given
    ("unverified", clamped to the file)."""
    hit = locate(quote, lines, line_start) if quote.strip() else None
    if hit is not None:
        return AnchorResult("ok", hit[0], hit[1])
    start, end = _clamp(line_start, line_end, len(lines))
    return AnchorResult("unverified", start, end)


def reanchor(
    quote: str,
    lines: Sequence[str] | None,
    state: AnchorState,
    anchor_start: int,
    anchor_end: int,
    origin: tuple[int, int] | None = None,
) -> AnchorResult:
    """Re-anchor after the file changed (`lines` None: the file is gone).

    The quote is searched near the current anchor. Found → "ok" when it sits where the
    finding was first anchored (`origin`; without one, the current anchor), else "moved",
    with the new lines. Not found → "stale" keeping the last lines (a finding that was never
    verified stays "unverified")."""
    hit = locate(quote, lines, anchor_start) if lines is not None and quote.strip() else None
    if hit is None:
        if state == "unverified":
            return AnchorResult("unverified", anchor_start, anchor_end)
        return AnchorResult("stale", anchor_start, anchor_end)
    home = origin if origin is not None else (anchor_start, anchor_end)
    if origin is None and hit == home and state == "moved":
        return AnchorResult("moved", hit[0], hit[1])
    return AnchorResult("ok" if hit == home else "moved", hit[0], hit[1])


def resolve_file(
    given: str, quote: str, line_start: int, files: dict[str, Sequence[str]]
) -> str | None:
    """Which scope file a finding refers to. `files` maps repo-relative path → lines, in
    document order. The given path if it is in scope; else the first file containing the
    quote; else a unique path-suffix or basename match; else the only file when the given
    path has its extension; else None (the finding is rejected as `invalid:file`)."""
    path = given.strip()
    while path.startswith("./"):
        path = path[2:]
    if path in files:
        return path
    if quote.strip():
        for name, lines in files.items():
            if locate(quote, lines, line_start) is not None:
                return name
    suffix = [name for name in files if name.endswith("/" + path) or path.endswith("/" + name)]
    if len(suffix) == 1:
        return suffix[0]
    base = path.rsplit("/", 1)[-1]
    same = [name for name in files if name.rsplit("/", 1)[-1] == base]
    if len(same) == 1:
        return same[0]
    if len(files) == 1:  # a mangled path in a one-file scope, if it names the same kind of file
        only = next(iter(files))
        if PurePosixPath(path).suffix.lower() == PurePosixPath(only).suffix.lower():
            return only
    return None
