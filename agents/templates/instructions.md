# agents 工作指示

> 本檔位於專案主工作樹的 `.agents/instructions.md`,只定義 agents session 行為。
> 計畫格式的唯一契約是同目錄的 `format.md`。

## 跨專案共通規則

- git commit 訊息禁止任何 AI 署名與廣告字樣(`Co-Authored-By`、
  `Generated with` 等),一行都不准出現。

## 預設讀取範圍

**會議/互動 session** 開工只讀:

1. 專案根目錄的 `AGENTS.md`(若存在)
2. 本檔
3. `.agents/format.md`(要建立或修改任務檔時必讀)
4. 目前工作資料夾的任務檔與 `_report.md`(驗收會議時)
5. 任務直接涉及的原始碼與測試

**agents 調度的任務 session** 只讀:

1. 調度器提示的 `AGENTS.md` 路徑(分支版本優先;未追蹤時是主樹絕對路徑;
   不存在就略過)
2. 調度器提示的本檔絕對路徑
3. 調度器提示的那一個任務檔絕對路徑
4. 任務直接涉及的原始碼與測試

worktree 不含 `.agents/`;任務 session 不得自行以相對路徑推測管理檔位置,
一律以調度器提示的主工作樹絕對路徑為準。預設不讀:舊工作資料夾、
r 檔(日誌;除錯或被明確引用才讀)、工作資料夾內的 `_agents.log`。

## 工作規則

- 不修改與目前任務無關的檔案。
- 共用規格用引用,不複製到各任務檔。
- 推測、已修改、已驗證、未驗證必須分開記錄。
- 未通過驗證不得宣告完成;pending 不得包裝成 completed。
- 程式碼、git 與測試結果才是最終事實來源。
- 禁止 kill / Stop-Process 任何非自己啟動的進程——你的父進程鏈上就是調度器,
  殺錯一個,整個 run 無聲死亡。
- 命令逾時的正確處置是調高逾時或分批重跑,不是獵殺「看起來卡住」的進程。

## 任務 session 收尾(agents 調度時)

1. 逐項對照任務檔的 acceptance 自檢,並執行調度器提示的驗收命令確認退出碼 0。
2. 把**自己任務檔**的 status 改為 DONE 或 BLOCKED——整份任務檔只准改這一行,
   不碰其他任務檔。
3. 在調度器提示的 r 檔絕對路徑檔尾 append 一筆 [[entry]]:
   time、提示詞指定的 by = "codex" 或 "claude"、requested_model、event、
   summary(可驗證事實,一句話)、detail(過程細節);提示詞的 requested_effort
   有值時也必須寫入。requested_model 與 requested_effort 是本次實際傳給 AI
   CLI 的值,不代表服務端最終採用或回報的模型與推理投入。
4. 不執行 git commit——檢查點由調度器負責。

## 會議 session 收尾(互動時)

1. 共識即時落成任務檔,不留在對話裡;格式依 `.agents/format.md`。
2. 執行 `agents check`——通過才算散會,不通過就是計畫還沒完成。
3. 跨計畫仍有效的決策寫進專案 `AGENTS.md` 的 Permanent constraints。
