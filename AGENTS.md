# Project instructions

## Project

<!-- TODO: 一句話描述專案：技術棧、平台、用途 -->
<!-- 例：Flutter application using Riverpod and SQLite. -->

## Permanent constraints

<!-- TODO: 放每次都成立的硬限制，例： -->
<!-- - Support Android 11 and later. -->
<!-- - 不引入新依賴，除非記錄理由。 -->

## AI 工作體系（.agents）

> 本節自成一體。專案已有自己的 AGENTS.md 時，把本節整段貼到該檔即可。
> 計畫格式的唯一契約是 `.agents/FORMAT.md`；本節只管 session 行為。

### Default reading

**會議／互動 session** 開工只讀：

1. `AGENTS.md`（本檔）
2. `.agents/CURRENT.md`
3. `.agents/FORMAT.md`（要建立或修改任務檔時必讀）
4. 任務直接涉及的原始碼與測試

**wflow 調度的任務 session** 只讀：

1. `AGENTS.md`（本檔）
2. 被指派的那一個任務檔 `.agents/tasks/<id>.md`
3. 任務直接涉及的原始碼與測試

預設不讀：`.agents/logs/`、`.agents/CONSENSUS.md`、已歸檔文件。

例外（才可讀歷史）：

- 正在除錯且問題反覆發生
- 追查 regression 需要歷史比對
- 任務檔或 CURRENT 明確引用某份歷史紀錄
- 懷疑目前方案過去已測試過

### Working rules

- 不修改與目前任務無關的檔案。
- 共用規格用標題錨點引用，不複製到各任務檔。
- 不以行號作為引用；用標題錨點或穩定識別碼。
- 推測、已修改、已驗證、未驗證必須分開記錄。
- 未通過驗證不得宣告完成；pending 不得包裝成 completed。
- CURRENT.md 是導航快照；程式碼、git 與測試結果才是最終事實來源。
  兩者衝突時以程式碼為準。

### 任務 session 收尾（wflow 調度時）

1. 逐項對照任務檔的 Acceptance criteria 自檢。
2. 執行任務檔表頭的 `verify` 命令，確認退出碼 0。
3. 更新**自己任務檔**的 `status`（DONE 或 BLOCKED），不碰其他任務檔。
4. 在 `.agents/logs/` 當月檔（YYYY-MM.md，不存在就建立）append 一筆。
5. **不執行 git commit**（檢查點由調度器負責）；
   **不編輯 CURRENT.md**（執行期由 wflow 自動生成）。

### 會議 session 收尾（互動時）

1. `git diff --stat` 檢視修改範圍；執行 `python .agents/verify.py` 確認全綠。
2. 重寫 `.agents/CURRENT.md`：只反映現在有效狀態，刪除過時內容，
   記錄最後驗證的 commit hash，不得只追加。
3. 詳細過程 append 到 `.agents/logs/` 當月檔。
4. 未驗證或失敗項目如實標註 pending。
