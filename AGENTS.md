# Project instructions

## Project

<!-- TODO: 一句話描述專案：技術棧、平台、用途 -->
<!-- 例：Flutter application using Riverpod and SQLite. -->

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
- CURRENT.md 是導航快照；程式碼、Git 與測試結果才是最終事實來源。
  兩者衝突時以程式碼為準，並修正 CURRENT.md。

## Permanent constraints

<!-- TODO: 放每次都成立的硬限制，例： -->
<!-- - Support Android 11 and later. -->
<!-- - 不引入新依賴，除非記錄理由。 -->
<!-- - 保持既有資料庫相容性。 -->

## Completion protocol

任務結束前，依序執行：

1. `git diff --stat` 檢視修改範圍。
2. 執行 `scripts/verify.sh`。
3. 逐項對照 `.agents/tasks/ACTIVE.md` 的驗收條件。
4. 更新 `.agents/tasks/ACTIVE.md`。
5. 重寫 `.agents/CURRENT.md`：只反映現在有效狀態，刪除過時內容，
   記錄最後驗證的 commit hash，不得只追加。
6. 詳細過程追加到 `.agents/logs/` 當月檔（YYYY-MM.md）。
7. 未驗證或失敗項目如實標註 pending，不得包裝成完成。
