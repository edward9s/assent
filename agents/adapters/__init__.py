"""Adapter 介面與共用資料型別。

額度訊息的偵測與解析封裝在各 adapter 內,主迴圈不感知廠牌差異。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agents import AgentsError

if TYPE_CHECKING:
    from agents.config import Config


@dataclass
class TaskResult:
    exit_code: int
    output: str                    # 子程序輸出全文(逐行原文)
    quota_exhausted: bool          # True = 額度耗盡,本輪不計失敗
    reset_at: datetime | None      # 解析得到的重置時間;解析不到為 None


class Adapter:                     # 各廠牌 adapter 的基底
    def resolve_model(self, model: str) -> str:
        """把任務抽象檔位解析成這次傳給 CLI 的 ``--model`` 值。"""
        return model

    def run_task(self, prompt: str, requested_model: str,
                 requested_effort: str | None,
                 cwd: Path) -> TaskResult:
        """使用 engine 已解析的 CLI 模型與 effort 實際值執行任務。"""
        raise NotImplementedError


def get_adapter(name: str, cfg: "Config") -> Adapter:
    """依名稱取得 adapter 實例;cfg 於此注入(含檔位->型號對照表)。"""
    if name == "claude":
        from agents.adapters.claude import ClaudeAdapter  # 延遲載入避免循環匯入
        return ClaudeAdapter(cfg)
    if name == "codex":
        from agents.adapters.codex import CodexAdapter
        return CodexAdapter(cfg)
    raise AgentsError(f"未知的 adapter:{name!r}(目前內建 'claude' / 'codex')")
