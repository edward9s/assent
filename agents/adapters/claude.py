"""claude CLI adapter:組命令、stream-json、watchdog、額度偵測(W1 實作)。

技術事實見 workflow 專案 WORKFLOW_GUIDE.md 2.4;額度偵測策略見 2.5。

W1 真 CLI 探勘所得(fixture: tests/fixtures/stream_json_ok.txt,見工作日誌):
- `claude -p ... --output-format stream-json` **必須同時給 `--verbose`**,否則 CLI
  直接報 "requires --verbose" 並以非零退出。故本 adapter 一律注入 `--verbose`。
  (WORKFLOW_GUIDE 2.4 / README 命令形未含此旗標,已列為規格疑義交使用者。)
- stream-json 為每行一個 JSON 事件,實測出現的 type:`system`(init)、`assistant`、
  `rate_limit_event`、`result`(最後一筆,含 `subtype`/`is_error`/`result` 文字)。
- 額度資訊為**結構化事件** `rate_limit_event`,其 `rate_limit_info` 含
  `status`(成功時為 "allowed")、`resetsAt`(五小時窗重置的 Unix 秒數)、
  `rateLimitType`("five_hour")。這比 regex 掃文字可靠,故列為主要偵測來源;
  額度耗盡時的實際 status 值本次未取得樣本(成功案例是 "allowed"),
  `_BLOCKED_STATUSES` 與文字 regex 皆為 best-effort,待 W5 首次真實遇到時校正(鐵則允許)。

W5 實測校正(2026-07-15,Pro 訂閱、fable/high 真實撞限):
- 額度耗盡時 CLI 的人類可讀訊息實測為
  「You've hit your session limit · resets 4am (Asia/Taipei)」——不含
  "usage limit"/"rate limit"/"limit reached" 任何一種舊 regex 樣式,
  文字後備因此漏接。已把 "session limit" 與 "hit your ... limit" 補進 _QUOTA_TEXT_RE。
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from agents import AgentsError
from agents.adapters import Adapter, TaskResult

if TYPE_CHECKING:
    from agents.config import Config

# rate_limit_event.rate_limit_info.status 中代表「已被限流/耗盡」的值(best-effort;
# 成功時實測為 "allowed",耗盡時的實際值待 W5 校正)。
_BLOCKED_STATUSES = {"rejected", "blocked", "exhausted", "throttled", "limited", "reached"}
# 文字後備:僅比對「人類可讀字串」(result 文字、assistant 文字、非 JSON 的 stderr 行),
# **不**掃原始 JSON,否則成功輸出裡的 "rate_limit_event"/"rateLimitType" 字樣會誤觸。
_QUOTA_TEXT_RE = re.compile(
    r"usage\s+limit|rate\s+limit|session\s+limit|limit\s+reached"
    r"|hit\s+your\s+[\w'’ ]{0,40}limit|quota\s+(?:exceeded|exhausted)"
    r"|out\s+of\s+\w*\s*credit",
    re.IGNORECASE)

_SENTINEL = object()


def build_command(cfg: "Config", prompt: str, alias: str,
                  effort: str | None) -> list[str]:
    """依 2.4 組 claude CLI 命令;alias 已是 CLI --model 值,effort 已由 engine 解析。"""
    cmd = [cfg.claude_command, "-p", prompt, "--model", alias]
    if effort:
        cmd += ["--effort", effort]
    # 解析所需的固定旗標(--verbose 為探勘實證的硬性需求);extra_args 原樣附加於末。
    cmd += ["--output-format", "stream-json", "--verbose"]
    cmd += list(cfg.claude_extra_args)
    return cmd


def _tool_brief(inp) -> str:
    """從 tool_use 的 input 挑一個最有代表性的欄位,壓成單行短字串。"""
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "description", "skill"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            one = " ".join(val.split())
            return one[:100] + ("…" if len(one) > 100 else "")
    return ""


def format_stream_event(raw_line: str) -> str | None:
    """把一行 stream-json 事件轉成給終端看的即時進度文字;None = 這行不顯示。

    目的:讓使用者在 wflow run 期間同步看到執行 AI 在做什麼(說了什麼、用了哪些
    工具、燒了多少 tokens),而不是黑箱等到 session 結束。只做顯示,不影響
    parse_output_for_quota 的事後判定。
    """
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
        return f"  !| {s}"          # 非 JSON 行(多半是 stderr 錯誤文字),原樣顯示
    etype = evt.get("type")
    if etype == "system" and evt.get("subtype") == "init":
        return f"  --| session 開始(model={evt.get('model', '?')})"
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
                out.append(f"  工具| {block.get('name', '?')}"
                           + (f" {brief}" if brief else ""))
        return "\n".join(out) if out else None
    if etype == "rate_limit_event":
        info = evt.get("rate_limit_info") or {}
        status = str(info.get("status", "")).strip().lower()
        if status and status != "allowed":
            return f"  額度| rate_limit status={status}"
        return None
    if etype == "result":
        parts = [f"session 結束({evt.get('subtype', '?')})"]
        usage = evt.get("usage") or {}
        if isinstance(usage.get("output_tokens"), (int, float)):
            parts.append(f"輸出 {usage['output_tokens']} tokens")
        if isinstance(evt.get("duration_ms"), (int, float)):
            parts.append(f"{evt['duration_ms'] / 1000:.0f} 秒")
        return "  --| " + ",".join(parts)
    return None


def run_subprocess(command: list[str], cwd: Path, stall_seconds: float,
                   echo=None) -> tuple[int, str, bool]:
    """跑子程序,逐行收集輸出;reader thread + queue 做 watchdog(2.4 標準解法)。

    stall_seconds <= 0 -> 停用 watchdog(阻塞讀到 EOF)。
    echo:每收到一行就呼叫的回呼(即時顯示用);它拋錯不影響收集與判定。
    回傳 (returncode, 輸出全文, stalled)。stalled=True 表示逾時被殺。
    stderr 併入 stdout,確保額度/錯誤訊息不漏接。
    """
    proc = subprocess.Popen(
        command, cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)

    q: "queue.Queue" = queue.Queue()

    def _reader(stream) -> None:
        try:
            for line in stream:
                q.put(line)
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=_reader, args=(proc.stdout,), daemon=True)
    thread.start()

    lines: list[str] = []
    stalled = False
    try:
        while True:
            try:
                if stall_seconds and stall_seconds > 0:
                    item = q.get(timeout=stall_seconds)
                else:
                    item = q.get()
            except queue.Empty:
                stalled = True
                proc.kill()
                break
            if item is _SENTINEL:
                break
            lines.append(item)
            if echo is not None:
                try:
                    echo(item)
                except Exception:   # 顯示層故障不得影響輸出收集與額度判定
                    pass

        proc.wait()
        if stalled:  # 盡力收殘留已入列的輸出(不 join daemon thread,避免卡死)
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is not _SENTINEL:
                    lines.append(item)
    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    return proc.returncode, "".join(lines), stalled


def parse_output_for_quota(output: str) -> tuple[bool, datetime | None]:
    """從 stream-json 輸出判定是否額度耗盡並解析重置時間(2.5 策略)。

    主要來源:結構化 rate_limit_event(status 屬 _BLOCKED_STATUSES -> 耗盡;
    resetsAt Unix 秒 -> 重置時間)。後備:對人類可讀文字掃 _QUOTA_TEXT_RE。
    回傳 (quota_exhausted, reset_at);reset_at 為 UTC aware datetime 或 None。
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
            human_texts.append(s)  # 非 JSON 行(如 stderr 文字)

    if not exhausted:
        for text in human_texts:
            if _QUOTA_TEXT_RE.search(text):
                exhausted = True
                break

    reset_at = None
    if exhausted and reset_ts is not None:
        reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
    return exhausted, reset_at


class ClaudeAdapter(Adapter):
    """claude CLI adapter;設定由 get_adapter 注入。"""

    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg

    def resolve_model(self, model: str) -> str:
        """把抽象檔位解析成 Claude CLI 實際接收的模型參數。"""
        alias = self.cfg.claude_models.get(model)
        if alias is None:
            raise AgentsError(
                f"模型檔位 {model!r} 不在 [adapter.claude.models] 對照表中;"
                f"請檢查計畫檔的建議模型或設定檔對照表")
        return alias

    def run_task(self, prompt: str, requested_model: str, effort: str | None,
                 cwd: Path) -> TaskResult:
        cmd = build_command(self.cfg, prompt, requested_model, effort)
        stall_seconds = self.cfg.stall_minutes * 60 if self.cfg.stall_minutes else 0
        returncode, output, stalled = run_subprocess(
            cmd, cwd, stall_seconds, echo=self._echo_line)
        if stalled:  # 停滯是任務失敗,絕不誤判為額度(2.5)
            return TaskResult(exit_code=returncode, output=output,
                              quota_exhausted=False, reset_at=None)
        exhausted, reset_at = parse_output_for_quota(output)
        return TaskResult(exit_code=returncode, output=output,
                          quota_exhausted=exhausted, reset_at=reset_at)

    @staticmethod
    def _echo_line(raw_line: str) -> None:
        text = format_stream_event(raw_line)
        if text:
            print(text, flush=True)
