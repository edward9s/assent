#!/usr/bin/env python3
"""將 starter kit 初始化到目標專案。只複製，絕不覆蓋既有檔案。

用法: python init.py /path/to/your/project
"""

import shutil
import sys
from datetime import date
from pathlib import Path

# Windows 下 stdout 導向管線/檔案時預設用系統 code page，中文會變亂碼
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FILES = [
    "AGENTS.md",
    ".agents/CURRENT.md",
    ".agents/tasks/ACTIVE.md",
    ".agents/logs/TEMPLATE.md",
    "scripts/verify.py",
]


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python init.py /path/to/your/project")
        return 1
    target = Path(sys.argv[1])
    if not target.is_dir():
        print(f"錯誤: 目錄不存在: {target}")
        return 1
    src = Path(__file__).resolve().parent

    copied = 0
    skipped = 0
    for rel in FILES:
        dest = target / rel
        if dest.exists():
            print(f"略過（已存在）: {rel}")
            skipped += 1
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dest)
            print(f"已建立: {rel}")
            copied += 1

    month = f"{date.today():%Y-%m}"
    month_log = target / ".agents" / "logs" / f"{month}.md"
    if not month_log.exists():
        month_log.write_text(f"# Work log {month}\n", encoding="utf-8")
        print(f"已建立: .agents/logs/{month}.md")
        copied += 1

    print()
    print(f"完成：新建 {copied} 個檔案，略過 {skipped} 個。")
    print()
    print("接下來：")
    print("  1. 填寫 AGENTS.md 與 scripts/verify.py 中的 TODO")
    print("  2. 把專案目前真實狀態填入 .agents/CURRENT.md（最重要）")
    print("  3. 在 .agents/tasks/ACTIVE.md 定義第一個任務")
    print("  4. 開新 AI session 跑冷啟動測試（見 README.md）")
    print()
    print("工具若讀 CLAUDE.md：建立符號連結，或直接複製 AGENTS.md 為 CLAUDE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
