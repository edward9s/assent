#!/usr/bin/env bash
# 將 starter kit 初始化到目標專案。只複製，絕不覆蓋既有檔案。
# 用法: ./init.sh /path/to/your/project
set -euo pipefail

TARGET="${1:?用法: ./init.sh /path/to/your/project}"
SRC="$(cd "$(dirname "$0")" && pwd)"

[ -d "$TARGET" ] || { echo "錯誤: 目錄不存在: $TARGET"; exit 1; }

copied=0
skipped=0

copy_one() {
  local rel="$1"
  local dest="$TARGET/$rel"
  if [ -e "$dest" ]; then
    echo "略過（已存在）: $rel"
    skipped=$((skipped+1))
  else
    mkdir -p "$(dirname "$dest")"
    cp "$SRC/$rel" "$dest"
    echo "已建立: $rel"
    copied=$((copied+1))
  fi
}

copy_one "AGENTS.md"
copy_one ".agents/CURRENT.md"
copy_one ".agents/tasks/ACTIVE.md"
copy_one ".agents/logs/TEMPLATE.md"
copy_one "scripts/verify.sh"

mkdir -p "$TARGET/.agents/tasks/completed"
chmod +x "$TARGET/scripts/verify.sh" 2>/dev/null || true

MONTH_LOG="$TARGET/.agents/logs/$(date +%Y-%m).md"
if [ ! -e "$MONTH_LOG" ]; then
  printf '# Work log %s\n' "$(date +%Y-%m)" > "$MONTH_LOG"
  echo "已建立: .agents/logs/$(date +%Y-%m).md"
  copied=$((copied+1))
fi

echo
echo "完成：新建 $copied 個檔案，略過 $skipped 個。"
echo
echo "接下來："
echo "  1. 填寫 AGENTS.md 與 scripts/verify.sh 中的 TODO"
echo "  2. 把專案目前真實狀態填入 .agents/CURRENT.md（最重要）"
echo "  3. 在 .agents/tasks/ACTIVE.md 定義第一個任務"
echo "  4. 開新 AI session 跑冷啟動測試（見 README.md）"
echo
echo "工具若讀 CLAUDE.md：cd $TARGET && ln -s AGENTS.md CLAUDE.md"
