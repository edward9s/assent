#!/usr/bin/env bash
# 完成的機器證明：任務不算完成，除非本腳本 exit 0。
# TODO: 把下方示例換成你專案的實際檢查命令。
set -euo pipefail

# --- 體系完整性檢查（保留） ---
test -f AGENTS.md
test -f .agents/CURRENT.md
test -f .agents/tasks/ACTIVE.md
git diff --check

# --- 專案檢查（TODO: 依技術棧擇一或自行替換） ---

# Flutter / Dart:
# dart format --output=none --set-exit-if-changed .
# flutter analyze
# flutter test

# Node / TypeScript:
# npx prettier --check .
# npx eslint .
# npm test

# Python:
# ruff check .
# ruff format --check .
# pytest

# --- 專案特有的硬限制檢查（有痛點再加） ---
# ./scripts/check_database_migrations.sh

echo "verify: OK"
