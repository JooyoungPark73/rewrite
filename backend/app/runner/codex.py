"""`codex exec` runner: parses `--json` events.

Verified on codex-cli 0.162.1 (fixtures in tests/fixtures/runner/), tools re-verified on
0.163.0 (2026-10-26, real runs with the shell tool on under `--sandbox read-only`):
`thread.started`, `turn.started`, `item.started|updated|completed` with `item.type`
`agent_message` (`text`), `reasoning`, `command_execution` (`command`, `exit_code`,
`aggregated_output`), `file_change`, `mcp_tool_call`, `web_search`, ...; `turn.completed`
with `usage.{input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}`;
`turn.failed {error: {message}}` and `error {message}`. stderr says "Reading prompt from
stdin...". There is no cost field (cost stays null) and no model name in the events.

The answer is the last `agent_message` (with `--output-schema` it is the JSON object).
A run may read (shell commands under the read-only sandbox: the paper's sources, figures
and bibliography) but never write: a `file_change` item (apply_patch) aborts the run.
"""

from __future__ import annotations

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

# Items that edit files; never allowed in a run (the sandbox would block the write too).
WRITE_ITEMS = frozenset({"file_change", "patch", "apply_patch"})
COMMAND_MAX = 200


class CodexStreamParser:
    def __init__(self) -> None:
        self.abort_reason: str | None = None
        self.messages: list[str] = []
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.error: str | None = None
        self.last_error: str | None = None
        self.completed = False
        self.tool_calls = 0

    def feed(self, line: str) -> list[RunEvent]:
        if not line.strip():
            return []
        event = load_event(line)
        if event is None:
            return [status(line.strip()[:500])]
        kind = event.get("type")
        if kind == "thread.started":
            return [status("codex thread started")]
        if kind == "turn.started":
            return [status("turn started")]
        if kind in ("item.started", "item.updated", "item.completed"):
            return self._item(kind, event.get("item") or {})
        if kind == "turn.completed":
            self.completed = True
            usage = event.get("usage") or {}
            self.input_tokens = _add(self.input_tokens, as_int(usage.get("input_tokens")))
            self.output_tokens = _add(self.output_tokens, as_int(usage.get("output_tokens")))
            calls = f", {self.tool_calls} tool calls" if self.tool_calls else ""
            return [status(f"turn completed{calls}")]
        if kind == "turn.failed":
            error = event.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            self.error = str(message or error or "codex reported a failed turn")
            return [status(f"turn failed: {self.error}")]
        if kind == "error":
            self.last_error = str(event.get("message") or "error")
            return [status(f"error: {self.last_error}")]
        return []

    def _item(self, kind: str, item: dict[str, Any]) -> list[RunEvent]:
        item_type = str(item.get("type") or "")
        if item_type in WRITE_ITEMS:
            if self.abort_reason is None:
                self.abort_reason = f"codex tried to write ({item_type}); runs are read-only"
            return [status(f"blocked: {item_type}")]
        if item_type == "command_execution":
            command = str(item.get("command") or "")[:COMMAND_MAX]
            if kind == "item.started":
                self.tool_calls += 1
                return [status(f"command: {command}")]
            if kind == "item.completed":
                code = item.get("exit_code")
                output = str(item.get("aggregated_output") or "")
                return [status(f"command exited {code}, {len(output)} chars: {command}")]
            return []
        if item_type not in ("agent_message", "reasoning", "todo_list", "error"):
            # mcp_tool_call, web_search, ...: reported, allowed (nothing here writes)
            if kind == "item.started":
                self.tool_calls += 1
            return [status(f"{kind}: {item_type}")] if kind != "item.updated" else []
        if kind != "item.completed":
            return []
        if item_type == "agent_message" and isinstance(item.get("text"), str):
            self.messages.append(item["text"])
            return [text_event(item["text"])]
        if item_type == "reasoning":
            summary = str(item.get("text") or "").strip()
            return [status(f"reasoning: {summary[:200]}" if summary else "reasoning…")]
        if item_type == "error":
            return [status(f"error: {item.get('message') or ''}")]
        return []

    def finish(self) -> ParsedStream:
        text = self.messages[-1] if self.messages else ""
        error = self.error
        if error is None and not self.completed and self.last_error and not text:
            error = self.last_error
        usage = RunUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens)
        return ParsedStream(
            text=text, structured=None, usage=usage, error=error, completed=self.completed
        )


def _add(total: int | None, value: int | None) -> int | None:
    if value is None:
        return total
    return value if total is None else total + value


class CodexRunner(CliRunner):
    name = "codex"

    def make_parser(self) -> StreamParser:
        return CodexStreamParser()
