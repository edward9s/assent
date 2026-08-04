"""claude CLI adapter: builds the command, handles stream-json, the watchdog, and quota detection.

Technical background: see the workflow project's WORKFLOW_GUIDE.md 2.4; quota detection
strategy is covered in 2.5.

Findings from the first live CLI probe (fixture: tests/fixtures/stream_json_ok.txt, recorded
in the work log):
- `claude -p ... --output-format stream-json` **must be paired with `--verbose`**, otherwise the
  CLI exits non-zero with "requires --verbose". This adapter therefore always injects `--verbose`.
  (WORKFLOW_GUIDE 2.4 / the README's command form omits this flag; flagged as a spec question for
  the user.)
- stream-json emits one JSON event per line; the `type` values observed in practice are `system`
  (init), `assistant`, `rate_limit_event`, and `result` (the final line, carrying `subtype`/
  `is_error`/the `result` text).
- Quota information arrives as a **structured event**, `rate_limit_event`; its `rate_limit_info`
  carries `status` ("allowed" on success), `resetsAt` (Unix seconds for when the five-hour window
  resets), and `rateLimitType` ("five_hour"). This is more reliable than regex-scanning text, so
  it is the primary detection source; no sample of the actual status value on quota exhaustion
  was captured this round (the observed success case was "allowed"), so `_BLOCKED_STATUSES` and
  the text regex are both best-effort pending calibration against a real exhaustion event (see
  below; the ground rule permits this).

Calibrated from a real event (2026-07-15, Pro subscription, fable/high actually hit the limit):
- The CLI's human-readable message on quota exhaustion was observed to be
  "You've hit your session limit · resets 4am (Asia/Taipei)" — it matches none of the older regex
  patterns ("usage limit"/"rate limit"/"limit reached"), so the text fallback missed it. "session
  limit" and "hit your ... limit" have since been added to `_QUOTA_TEXT_RE`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from assent.adapters import (Adapter, TaskResult, is_checkpoint_resume_line,
                             parse_checkpoint_resume_output)
# The subprocess runner is shared adapter infrastructure, not claude knowledge; it is
# re-exported here because callers patch this module's name to fake a session.
from assent.adapters.process import run_subprocess

if TYPE_CHECKING:
    from assent.config import Config

# Values of rate_limit_event.rate_limit_info.status that mean "throttled/exhausted"
# (best-effort; the observed success value is "allowed", no sample of the structured status on
# exhaustion has been captured yet, so the text fallback below backs it up — see the
# 2026-07-15 calibration note at the top of this file).
_BLOCKED_STATUSES = {"rejected", "blocked", "exhausted", "throttled", "limited", "reached"}
# Text fallback: only matched against human-readable strings (result text, assistant text,
# non-JSON stderr lines) — **never** scanned against raw JSON, otherwise successful output
# containing the literal strings "rate_limit_event"/"rateLimitType" would false-positive.
_QUOTA_TEXT_RE = re.compile(
    r"usage\s+limit|rate\s+limit|session\s+limit|limit\s+reached"
    r"|hit\s+your\s+[\w'’ ]{0,40}limit|quota\s+(?:exceeded|exhausted)"
    r"|out\s+of\s+\w*\s*credit",
    re.IGNORECASE)
# Account-level billing/insufficient-balance text: distinct from quota, because a prepaid
# balance never refills on its own; the scheduler must fail fast, not wait for a reset that
# will never come.  Same scan discipline as _QUOTA_TEXT_RE: only matched against human-readable
# strings, never raw JSON key names.
_BILLING_TEXT_RE = re.compile(
    r"credit\s+balance|balance\s+is\s+too\s+low"
    r"|insufficient\s+(?:credit|funds|balance)|payment\s+required",
    re.IGNORECASE)
# HTTP statuses the CLI reports on a result event for an account-level billing/balance problem.
_BILLING_API_STATUSES = {400, 402}


def build_command(cfg: "Config", prompt: str, requested_model: str,
                  requested_effort: str | None) -> list[str]:
    """Build the claude CLI command whose prompt is supplied through stdin.

    Both model and effort have already been resolved to concrete values by the engine.
    The prompt is delivered via stdin (run_subprocess's input_text) to avoid exceeding
    Windows' CreateProcessW command-line length limit ([WinError 206]).
    """
    cmd = [cfg.claude_command, "-p", "--model", requested_model]
    if requested_effort:
        cmd += ["--effort", requested_effort]
    # Fixed flags required for parsing (--verbose is a hard requirement found by probing);
    # extra_args is appended verbatim at the end.
    cmd += ["--output-format", "stream-json", "--verbose"]
    cmd += list(cfg.claude_extra_args)
    return cmd


def _tool_brief(inp) -> str:
    """Pick the most representative field from a tool_use's input and compact it to one line."""
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "description", "skill"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            one = " ".join(val.split())
            return one[:100] + ("…" if len(one) > 100 else "")
    return ""


def format_stream_event(raw_line: str) -> str | None:
    """Turn one stream-json event line into live progress text for the terminal;
    None = don't show this line.

    Purpose: let the user watch what the executing AI is doing during `assent run` (what it
    said, which tools it used, how many tokens it burned) instead of waiting blind until the
    session ends. Display only — it doesn't affect parse_output_for_quota's after-the-fact
    verdict.
    """
    if is_checkpoint_resume_line(raw_line):
        return None
    s = raw_line.strip()
    if not s:
        return None
    evt = None
    if s.startswith("{"):
        try:
            evt = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            evt = None
    if not isinstance(evt, dict):
        return f"  !| {s}"          # Non-JSON line (usually stderr error text); shown verbatim
    etype = evt.get("type")
    if etype == "system" and evt.get("subtype") == "init":
        return f"  --| session started (model={evt.get('model', '?')})"
    if etype == "assistant":
        out: list[str] = []
        msg = evt.get("message") or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                out += [f"  AI| {ln}" for ln in text.splitlines() if ln.strip()]
            elif block.get("type") == "tool_use":
                brief = _tool_brief(block.get("input"))
                out.append(f"  Tool| {block.get('name', '?')}"
                           + (f" {brief}" if brief else ""))
        return "\n".join(out) if out else None
    if etype == "rate_limit_event":
        info = evt.get("rate_limit_info") or {}
        status = str(info.get("status", "")).strip().lower()
        if status and status != "allowed":
            return f"  Quota| rate_limit status={status}"
        return None
    if etype == "result":
        parts = [f"session ended ({evt.get('subtype', '?')})"]
        usage = evt.get("usage") or {}
        if isinstance(usage.get("output_tokens"), (int, float)):
            parts.append(f"output {usage['output_tokens']} tokens")
        if isinstance(evt.get("duration_ms"), (int, float)):
            parts.append(f"{evt['duration_ms'] / 1000:.0f} sec")
        return "  --| " + ",".join(parts)
    return None


def parse_output_for_quota(output: str) -> tuple[bool, datetime | None]:
    """Determine quota exhaustion and parse the reset time from stream-json output
    (the strategy from 2.5).

    Primary source: the structured rate_limit_event (status in _BLOCKED_STATUSES -> exhausted;
    resetsAt Unix seconds -> reset time). Fallback: scan human-readable text with
    _QUOTA_TEXT_RE.
    Returns (quota_exhausted, reset_at); reset_at is a UTC-aware datetime, or None.
    """
    exhausted = False
    reset_ts: float | None = None
    human_texts: list[str] = []

    for raw in output.splitlines():
        s = raw.strip()
        if not s:
            continue
        evt = None
        if s.startswith("{"):
            try:
                evt = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                evt = None
        if isinstance(evt, dict):
            etype = evt.get("type")
            if etype == "rate_limit_event":
                info = evt.get("rate_limit_info") or {}
                status = str(info.get("status", "")).strip().lower()
                if status in _BLOCKED_STATUSES:
                    exhausted = True
                ts = info.get("resetsAt")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    reset_ts = ts
            elif etype == "result":
                r = evt.get("result")
                if isinstance(r, str):
                    human_texts.append(r)
            elif etype == "assistant":
                msg = evt.get("message") or {}
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            human_texts.append(text)
        else:
            human_texts.append(s)  # Non-JSON line (e.g. stderr text)

    if not exhausted:
        for text in human_texts:
            if _QUOTA_TEXT_RE.search(text):
                exhausted = True
                break

    reset_at = None
    if exhausted and reset_ts is not None:
        reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
    return exhausted, reset_at


def parse_output_for_billing(output: str) -> bool:
    """Detect an account-level billing/insufficient-balance failure in stream-json output.

    Strongest source: a ``result`` event whose ``api_error_status`` is a billing status
    (400/402) carrying billing text in its ``result``/``error`` field -- the exact shape the
    real CLI emits once a prepaid balance hits zero. Fallback: billing phrasing in any
    human-readable text (result/error text, assistant text, a non-JSON stderr line). Never
    scanned against raw JSON key names, so ordinary output cannot false-positive.
    """
    human_texts: list[str] = []
    for raw in output.splitlines():
        s = raw.strip()
        if not s:
            continue
        evt = None
        if s.startswith("{"):
            try:
                evt = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                evt = None
        if isinstance(evt, dict):
            if evt.get("type") == "result":
                status = evt.get("api_error_status")
                texts = [evt.get(field) for field in ("result", "error")]
                texts = [t for t in texts if isinstance(t, str)]
                if (isinstance(status, int) and not isinstance(status, bool)
                        and status in _BILLING_API_STATUSES
                        and any(_BILLING_TEXT_RE.search(t) for t in texts)):
                    return True
                human_texts.extend(texts)
            elif evt.get("type") == "assistant":
                msg = evt.get("message") or {}
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            human_texts.append(text)
        else:
            human_texts.append(s)  # Non-JSON line (e.g. stderr text)

    return any(_BILLING_TEXT_RE.search(text) for text in human_texts)


class ClaudeAdapter(Adapter):
    """claude CLI adapter; config is injected by get_adapter."""

    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg
        # Model resolution and the effort contract come from the shared typed settings so this
        # adapter and the engine resolve invocations identically (base Adapter.resolve_model).
        self.settings = cfg.adapter_settings("claude")

    def run_task(self, prompt: str, requested_model: str,
                 requested_effort: str | None,
                 cwd: Path) -> TaskResult:
        cmd = build_command(
            self.cfg, prompt, requested_model, requested_effort)
        stall_seconds = self.cfg.stall_minutes * 60 if self.cfg.stall_minutes else 0
        returncode, output, stalled = run_subprocess(
            cmd, cwd, stall_seconds, echo=self._echo_line,
            input_text=prompt)
        if stalled:  # A stall is a task failure, never mistaken for quota exhaustion (2.5)
            return TaskResult(exit_code=returncode, output=output,
                              quota_exhausted=False, reset_at=None,
                              stalled=True)
        exhausted, reset_at = (
            parse_output_for_quota(output) if returncode != 0 else (False, None))
        billing = (returncode != 0 and parse_output_for_billing(output)
                   if not exhausted else False)
        terminal_record = parse_checkpoint_resume_output(output, returncode, stalled)
        # Quota evidence wins; otherwise the exact final control record wins over
        # unrelated billing-like prose that appeared earlier in the transcript.
        checkpoint_resume = terminal_record and not exhausted
        if checkpoint_resume:
            billing = False
        # Billing is a failure classification, so it is only meaningful for a failed session;
        # a successful run whose prose merely mentions "credit balance" must never be flagged.
        failure_kind = "billing" if billing else None
        return TaskResult(exit_code=returncode, output=output,
                          quota_exhausted=exhausted, reset_at=reset_at,
                          stalled=False, checkpoint_resume=checkpoint_resume,
                          failure_kind=failure_kind)

    @staticmethod
    def _echo_line(raw_line: str) -> None:
        text = format_stream_event(raw_line)
        if text:
            print(text, flush=True)
