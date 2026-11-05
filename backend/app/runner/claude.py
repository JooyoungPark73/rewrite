"""`claude -p` runner: parses `--output-format stream-json --verbose` events.

Verified on claude 2.1.302 (fixtures in tests/fixtures/runner/), tools re-verified on
2.1.310 (2026-10-26, real runs with `--tools Read,Glob,Grep`):
- `system/init` lists `model` and `tools` (`Glob`, `Grep`, `Read`, plus `StructuredOutput`
  with `--json-schema`).
- With `--include-partial-messages`, `stream_event` lines carry `content_block_delta`s:
  `text_delta` for text, `input_json_delta` for a tool call's input. Only the
  StructuredOutput call's JSON is streamed as the answer; other tool calls become status
  events.
- `assistant` lines carry whole messages: `text` blocks, `tool_use` blocks (name, input)
  and, for the answer, a `tool_use` block named `StructuredOutput`. `user` lines carry the
  `tool_result` blocks the CLI fed back.
- The final `result` line has `result` (text), `structured_output` (with --json-schema),
  `total_cost_usd`, `usage.{input_tokens, cache_creation_input_tokens,
  cache_read_input_tokens, output_tokens}`, `is_error` and `subtype`.

A run may read (Read, Glob, Grep: the paper's sources, figures and bibliography) but never
write: a tool call that edits or writes a file aborts the run (`abort_reason`), whatever
the config let through.
"""

from __future__ import annotations

import json
from typing import Any

from app.runner.base import (
    CliRunner,
    ParsedStream,
    RunEvent,
    RunUsage,
    StreamParser,
    as_int,
    load_event,
    status,
    text_event,
)

ANSWER_TOOL = "StructuredOutput"
TOOL_BLOCKS = frozenset({"tool_use", "server_tool_use", "mcp_tool_use"})
# Built-in tools that change files or run arbitrary commands: never allowed in a run.
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"})
INPUT_SUMMARY_KEYS = ("file_path", "path", "pattern", "url", "query", "command", "notebook_path")


def describe_tool_call(name: str, tool_input: Any) -> str:
    """One line for the live panel: `Read: sections/intro.tex`, `Grep: "\\\\cite"`."""
    if isinstance(tool_input, dict):
        for key in INPUT_SUMMARY_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return f"{name}: {value[:200]}"
    return name


class ClaudeStreamParser:
    def __init__(self) -> None:
        self.abort_reason: str | None = None
        self.model: str | None = None
        self.saw_deltas = False
        self.last_text = ""
        self.tool_output: Any | None = None
        self.final_text: str | None = None
        self.structured: Any | None = None
        self.usage: RunUsage | None = None
        self.error: str | None = None
        self.completed = False
        self.tool_calls = 0
        self._answer_ids: set[str] = set()  # tool_use ids of StructuredOutput calls
        # True while the streamed content block is the StructuredOutput call: its
        # `input_json_delta`s are the answer. Other tool calls' inputs are not streamed.
        self._streaming_answer = False

    def _forbid(self, name: str) -> None:
        if self.abort_reason is None:
            self.abort_reason = f"claude tried to write ({name}); runs are read-only"

    def _tool_call(self, name: str, tool_input: Any) -> list[RunEvent]:
        if name in WRITE_TOOLS:
            self._forbid(name)
            return [status(f"blocked: {describe_tool_call(name, tool_input)}")]
        self.tool_calls += 1
        return [status(describe_tool_call(name, tool_input))]

    def feed(self, line: str) -> list[RunEvent]:
        if not line.strip():
            return []
        event = load_event(line)
        if event is None:
            return [status(line.strip()[:500])]
        kind = event.get("type")
        if kind == "system":
            return self._system(event)
        if kind == "stream_event":
            return self._stream(event.get("event") or {})
        if kind == "assistant":
            return self._assistant(event.get("message") or {})
        if kind == "user":
            return self._user(event.get("message") or {})
        if kind == "result":
            return self._result(event)
        return []

    def _system(self, event: dict[str, Any]) -> list[RunEvent]:
        if event.get("subtype") != "init":
            return []
        self.model = event.get("model") if isinstance(event.get("model"), str) else None
        tools = [str(tool) for tool in event.get("tools") or []]
        version = event.get("claude_code_version") or "?"
        return [
            status(
                f"claude {version} started: model {self.model or '?'}, "
                f"tools: {', '.join(tools) or 'none'}"
            )
        ]

    def _stream(self, event: dict[str, Any]) -> list[RunEvent]:
        kind = event.get("type")
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            self._streaming_answer = False
            if block.get("type") in TOOL_BLOCKS:
                name = str(block.get("name"))
                if name == ANSWER_TOOL:
                    self._streaming_answer = True
                    return []
                # The input arrives as deltas; the whole call is reported from the
                # `assistant` line, which carries it complete.
                if name in WRITE_TOOLS:
                    self._forbid(name)
                return []
            if block.get("type") == "thinking":
                return [status("thinking…")]
            return []
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                self.saw_deltas = True
                return [text_event(delta["text"])] if delta["text"] else []
            if delta.get("type") == "input_json_delta" and self._streaming_answer:
                self.saw_deltas = True
                partial = delta.get("partial_json")
                return [text_event(partial)] if isinstance(partial, str) and partial else []
        return []

    def _assistant(self, message: dict[str, Any]) -> list[RunEvent]:
        events: list[RunEvent] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                self.last_text = block["text"]
                if not self.saw_deltas:
                    events.append(text_event(block["text"]))
            elif kind in TOOL_BLOCKS:
                name = str(block.get("name"))
                if name != ANSWER_TOOL:
                    events += self._tool_call(name, block.get("input"))
                    continue
                self.tool_output = block.get("input")
                if isinstance(block.get("id"), str):
                    self._answer_ids.add(block["id"])
                if not self.saw_deltas:
                    events.append(text_event(json.dumps(self.tool_output)))
        return events

    def _user(self, message: dict[str, Any]) -> list[RunEvent]:
        """`tool_result` blocks: what a Read/Glob/Grep returned, reported by size only."""
        events: list[RunEvent] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if block.get("tool_use_id") in self._answer_ids:
                continue  # the StructuredOutput acknowledgement is not a read
            content = block.get("content")
            size = len(content) if isinstance(content, str) else len(json.dumps(content or ""))
            if block.get("is_error"):
                text = content if isinstance(content, str) else json.dumps(content)
                events.append(status(f"tool error: {text[:200]}"))
            else:
                events.append(status(f"tool result: {size} chars"))
        return events

    def _result(self, event: dict[str, Any]) -> list[RunEvent]:
        self.completed = True
        result = event.get("result")
        self.final_text = result if isinstance(result, str) else None
        if event.get("structured_output") is not None:
            self.structured = event["structured_output"]
        usage = event.get("usage") or {}
        parts = [
            as_int(usage.get(key))
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ]
        known = [part for part in parts if part is not None]
        cost = event.get("total_cost_usd")
        self.usage = RunUsage(
            input_tokens=sum(known) if known else None,
            output_tokens=as_int(usage.get("output_tokens")),
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
        )
        subtype = event.get("subtype")
        if event.get("is_error") or (subtype not in (None, "success")):
            self.error = (
                self.final_text or ""
            ).strip() or f"claude reported {subtype or 'an error'}"
        cost_text = f"${self.usage.cost_usd:.4f}" if self.usage.cost_usd is not None else "?"
        reads = f", {self.tool_calls} tool calls" if self.tool_calls else ""
        return [status(f"finished ({subtype or 'done'}), cost {cost_text}{reads}")]

    def finish(self) -> ParsedStream:
        structured = self.structured if self.structured is not None else self.tool_output
        text = self.final_text if self.final_text else self.last_text
        if not text and structured is not None:
            text = json.dumps(structured)
        return ParsedStream(
            text=text,
            structured=structured,
            usage=self.usage,
            error=self.error,
            model=self.model,
            completed=self.completed,
        )


class ClaudeRunner(CliRunner):
    name = "claude"

    def make_parser(self) -> StreamParser:
        return ClaudeStreamParser()
