"""Codex CLI adapter using the official ``codex exec --json`` JSONL stream."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents import AgentsError
from agents.adapters import Adapter, TaskResult
from agents.adapters.claude import run_subprocess

if TYPE_CHECKING:
    from workflow.config import Config

_QUOTA_TEXT_RE = re.compile(
    r"usage\s+limit|rate\s+limit|session\s+limit|limit\s+reached"
    r"|hit\s+your\s+[\w'’ ]{0,40}limit|quota\s+(?:exceeded|exhausted)"
    r"|out\s+of\s+\w*\s*credit|insufficient[_ ]quota",
    re.IGNORECASE,
)


def build_command(cfg: "Config", prompt: str, alias: str,
                  effort: str | None) -> list[str]:
    """Build a non-interactive, machine-readable Codex invocation."""
    cmd = [cfg.codex_command, "exec", "--json", "--color", "never",
           "--model", alias]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd += list(cfg.codex_extra_args)
    cmd.append(prompt)
    return cmd


def _one_line(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _item_brief(item: dict) -> str | None:
    kind = item.get("type")
    if kind == "agent_message":
        text = str(item.get("text") or "").strip()
        lines = [f"  AI| {line}" for line in text.splitlines() if line.strip()]
        return "\n".join(lines) or None
    if kind == "reasoning":
        text = item.get("text") or item.get("summary")
        brief = _one_line(text)
        return f"  思考| {brief}" if brief else None
    if kind == "command_execution":
        brief = _one_line(item.get("command"))
        status = _one_line(item.get("status"), 30)
        return "  工具| command" + (f" {brief}" if brief else "") + (
            f" [{status}]" if status else "")
    if kind == "file_change":
        paths: list[str] = []
        for change in item.get("changes") or []:
            if isinstance(change, dict) and isinstance(change.get("path"), str):
                paths.append(change["path"])
        return "  工具| file_change" + (f" {', '.join(paths[:4])}" if paths else "")
    if kind == "mcp_tool_call":
        server = item.get("server") or item.get("server_name") or "?"
        tool = item.get("tool") or item.get("tool_name") or "?"
        return f"  工具| MCP {server}/{tool}"
    if kind == "web_search":
        brief = _one_line(item.get("query"))
        return "  工具| web_search" + (f" {brief}" if brief else "")
    if kind == "plan_update":
        brief = _one_line(item.get("text") or item.get("plan"))
        return f"  計畫| {brief}" if brief else "  計畫| 已更新"
    return None


def format_stream_event(raw_line: str) -> str | None:
    """Translate one Codex JSONL event into concise live terminal text."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return f"  !| {line}"
    if not isinstance(event, dict):
        return None

    kind = event.get("type")
    if kind == "thread.started":
        return f"  --| session 開始(thread={event.get('thread_id', '?')})"
    if kind in ("item.started", "item.completed", "item.updated"):
        item = event.get("item")
        return _item_brief(item) if isinstance(item, dict) else None
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        parts = ["session 結束"]
        if isinstance(usage.get("output_tokens"), (int, float)):
            parts.append(f"輸出 {usage['output_tokens']} tokens")
        if isinstance(usage.get("reasoning_output_tokens"), (int, float)):
            parts.append(f"推理 {usage['reasoning_output_tokens']} tokens")
        return "  --| " + ",".join(parts)
    if kind == "turn.failed":
        error = event.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else error
        return f"  !| session 失敗:{_one_line(message) or '未知錯誤'}"
    if kind == "error":
        message = event.get("message") or event.get("error")
        if isinstance(message, dict):
            message = message.get("message")
        return f"  !| {_one_line(message) or line}"
    return None


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def parse_output_for_quota(output: str) -> bool:
    """Best-effort quota detection from Codex error/failure/agent events."""
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if _QUOTA_TEXT_RE.search(line):
                return True
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        item = event.get("item") or {}
        is_agent_message = (kind in ("item.started", "item.completed", "item.updated")
                            and isinstance(item, dict)
                            and item.get("type") == "agent_message")
        if kind in ("error", "turn.failed") or is_agent_message:
            if any(_QUOTA_TEXT_RE.search(text) for text in _strings(event)):
                return True
    return False


class CodexAdapter(Adapter):
    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg

    def run_task(self, prompt: str, model: str, effort: str | None,
                 cwd: Path) -> TaskResult:
        alias = self.cfg.codex_models.get(model)
        if alias is None:
            raise AgentsError(
                f"模型檔位 {model!r} 不在 [adapter.codex.models] 對照表中;"
                f"請檢查計畫檔的建議模型或設定檔對照表")
        command = build_command(self.cfg, prompt, alias, effort)
        stall_seconds = self.cfg.stall_minutes * 60 if self.cfg.stall_minutes else 0
        returncode, output, stalled = run_subprocess(
            command, cwd, stall_seconds, echo=self._echo_line)
        exhausted = False if stalled else parse_output_for_quota(output)
        return TaskResult(exit_code=returncode, output=output,
                          quota_exhausted=exhausted, reset_at=None)

    @staticmethod
    def _echo_line(raw_line: str) -> None:
        text = format_stream_event(raw_line)
        if text:
            print(text, flush=True)
