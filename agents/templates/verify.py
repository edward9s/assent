#!/usr/bin/env python3
"""共用驗收腳本:任務不算完成,除非 verify 命令 exit 0。

任務檔的 verify 欄位預設指向本腳本(python .agents/verify.py);
個別任務可換更快或更嚴的命令。
TODO: 把下方「專案檢查」的示例換成你專案的實際檢查命令。
"""

import subprocess
import sys
from pathlib import Path

# Windows 下 stdout 導向管線/檔案時預設用系統 code page,中文會變亂碼
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 驗收目標一律是調度器指定的 cwd;隔離執行時腳本本體仍在主樹。
ROOT = Path.cwd().resolve()


def fail(message: str) -> None:
    print(f"verify: FAIL - {message}")
    sys.exit(1)


def run(*cmd: str) -> None:
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        fail(f"命令失敗(退出碼 {result.returncode}): {' '.join(cmd)}")


# --- 工作樹完整性檢查(保留) ---
run("git", "diff", "--check")

# --- 專案檢查(TODO: 依技術棧擇一或自行替換) ---

# Flutter / Dart:
# run("dart", "format", "--output=none", "--set-exit-if-changed", ".")
# run("flutter", "analyze")
# run("flutter", "test")

# Node / TypeScript:
# run("npx", "prettier", "--check", ".")
# run("npx", "eslint", ".")
# run("npm", "test")

# Python:
# run("ruff", "check", ".")
# run("ruff", "format", "--check", ".")
# run("pytest")

print("verify: OK")
