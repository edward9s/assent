# AI 專案記憶管理：操作手冊

> 可直接複製使用。範本中的 Flutter 指令與任務內容為示例，請換成你的專案實況。

## 0. 目錄結構

```text
project/
├── AGENTS.md                  # 唯一留在 root，工具自動載入的入口
├── .agents/
│   ├── CURRENT.md             # 現況快照，每次結束重寫
│   ├── tasks/
│   │   ├── ACTIVE.md          # 當前任務契約
│   │   └── completed/         # 完成任務歸檔
│   └── logs/
│       └── 2026-07.md         # 按月分卷，append-only，預設不讀
└── scripts/
    └── verify.sh              # 完成的機器證明（CI 共用）
```

按痛點才增加：`.agents/decisions/`（ADR）、`.agents/architecture/`。

---

## 1. `AGENTS.md` 範本（root）

```markdown
# Project instructions

## Project

（一句話描述：技術棧、平台。例：Flutter app using Riverpod and SQLite.）

## Default reading

開始任務時只讀：

1. `AGENTS.md`（本檔）
2. `.agents/CURRENT.md`
3. `.agents/tasks/ACTIVE.md`
4. 任務直接涉及的原始碼與測試

預設不讀：`.agents/logs/`、`.agents/tasks/completed/`、已歸檔文件。

例外（才可讀歷史）：
- 正在除錯且問題反覆發生
- 追查 regression 需要歷史比對
- ACTIVE 或 CURRENT 明確引用某份歷史紀錄
- 懷疑目前方案過去已測試過

## Working rules

- 不修改與目前任務無關的檔案。
- 共用規格用標題錨點引用，不複製到各任務檔。
- 不以行號作為引用；用標題錨點或穩定識別碼。
- 推測、已修改、已驗證、未驗證必須分開記錄。
- 未通過驗證不得宣告完成。

## Permanent constraints

（放每次都成立的硬限制。例：）
- Support Android 11 and later.
- 不引入新依賴，除非記錄理由。
- 保持既有資料庫相容性。

## Completion protocol

任務結束前，依序執行：

1. `git diff --stat` 檢視修改範圍。
2. 執行 `scripts/verify.sh`。
3. 逐項對照 ACTIVE 的驗收條件。
4. 更新 `.agents/tasks/ACTIVE.md`。
5. 重寫 `.agents/CURRENT.md`（只反映現在有效狀態，不得追加堆積）。
6. 詳細過程追加到 `.agents/logs/` 當月檔。
7. 未驗證或失敗項目如實標註，不得包裝成完成。
```

---

## 2. `.agents/CURRENT.md` 範本

```markdown
# Current state

Updated: 2026-07-16
Active task: `.agents/tasks/ACTIVE.md`
Last verified commit: `a13c9e2`

## Current objective

（一兩句話：現在整體在解什麼問題。）

## Verified facts

- `flutter analyze`: passed
- `flutter test`: 184 passed
- （其他已核實的狀態）

## Unverified or failing

- （尚未驗證或已知失敗的項目）

## Decisions in force

- （目前有效的關鍵決策，每條一行；被推翻的不留在這裡）

## Next action

（下一個具體動作，一句話）
```

規則：
- 永遠現在式；每次 session 結束**重寫**，刪除過時內容。
- 記錄 commit hash——新 session 發現 commit 已前進，先核對再信任。
- 不放嘗試流水帳、終端輸出、已作廢方案的細節。
- 與程式碼衝突時：信程式碼與測試，修正本檔。

---

## 3. `.agents/tasks/ACTIVE.md` 範本

```markdown
# Active task: （任務名）

## Goal

（要達成什麼，一兩句）

## Scope

可以修改：
- `lib/...`
- 對應測試

不可修改：
- （明確列出禁區）

## Required behavior

1. （具體行為要求，逐條）

## Acceptance criteria

- `scripts/verify.sh` exit 0
- （新增測試要求）
- （人工驗證項目，逐項列出）

## Current findings

- （目前假設與已確認的發現）

## Next action

（下一個具體動作）

## References

- `AGENTS.md#permanent-constraints`
- `.agents/CURRENT.md#decisions-in-force`
（共用知識用引用，不複製）
```

任務完成後：移入 `.agents/tasks/completed/`，重點結論回寫 CURRENT，
再建立新的 ACTIVE.md。

---

## 4. `.agents/logs/` 日誌條目格式

檔名按月分卷：`2026-07.md`。內部 append-only，只追加不修改。

```markdown
## 2026-07-16 — （任務名）

Attempt:
（做了什麼）

Result:
（發生什麼，含失敗）

Verification:
- flutter test: 184 passed
- Android 15 phone landscape: not verified

Conclusion:
（一句話結論，供未來回溯）
```

要求：寫可驗證事實，不寫「已成功改善並完成優化」這類無交接價值的自我敘事。

---

## 5. `scripts/verify.sh` 範本

```bash
#!/usr/bin/env bash
set -euo pipefail

test -f .agents/CURRENT.md
test -f .agents/tasks/ACTIVE.md

git diff --check
dart format --output=none --set-exit-if-changed .
flutter analyze
flutter test

# 專案有其他硬限制時逐步加入：
# ./scripts/check_database_migrations.sh
```

無法自動化的人工測試，在 ACTIVE 或 CURRENT 中逐項記錄
passed / pending，pending 不得寫成 completed。

---

## 6. Session 標準流程

### 開始時（可直接作為開工提示詞）

```text
開始工作。請依序：

1. 讀 AGENTS.md
2. 讀 .agents/CURRENT.md
3. 讀 .agents/tasks/ACTIVE.md
4. 查看 git status / git log -5 / git diff
5. 查看任務直接相關的程式碼與測試
6. 核對 CURRENT 是否仍與程式碼一致（commit hash 是否對應）

動手前先回報：目前目標、修改範圍、驗收條件、預計驗證命令。
不要讀取 .agents/logs/，除非符合 AGENTS.md 列出的例外。
```

### 結束時

依 AGENTS.md 的 Completion protocol 七步執行。
關鍵：**重寫 CURRENT，不是追加**；未驗證項目如實標註。

### 人工抽查（每隔幾個 session，只查「當前真相層」）

```text
□ CURRENT 的 commit hash 是否對應目前版本？
□ 已完成內容是否仍被寫成未完成？
□ 未驗證內容是否被誤寫成已通過？
□ 被否決的方案是否仍列為現行方案？
□ ACTIVE 是否真的是目前優先任務？
```

---

## 7. 升級觸發條件

| 出現這個痛點 | 才加入 |
|---|---|
| 同一決策反覆討論、AI 重採已否決方案 | `.agents/decisions/ADR-xxx.md` |
| 多份任務重複大量架構說明 | `.agents/architecture/` |
| 路線圖與短期狀態更新頻率差異太大 | 拆出 `.agents/PLAN.md` |
| 單月日誌過大 | 移入 `.agents/logs/archive/` |

ADR 最小格式：Context / Decision / Consequences / Rejected alternatives，
200–500 tokens 足夠。

---

## 8. 品質自檢：冷啟動測試

一個零記憶的新 AI 只讀 AGENTS + CURRENT + ACTIVE，若還需要問——

- 這功能到底要做什麼？
- 哪些檔案可以改？
- 怎樣才算完成？
- 現在卡在哪？

→ 工作集資訊不足或摘要品質不夠。
若要讀幾千行才能回答 → 沒分層好。
兩者都不是 → 這套體系運作正常。
