# Project instructions

## Project

<!-- TODO: 一句話描述專案:技術棧、平台、用途 -->

## Permanent constraints

<!-- TODO: 放每次都成立的硬限制;跨計畫仍有效的決策也沉澱到這裡 -->

## AI 工作體系(.agents)

> 本節自成一體,由 agents init 生成;專案已有自己的 AGENTS.md 時會整段 append。
> 計畫格式的唯一契約是 `.agents/format.md`;本節只管 session 行為。

### Default reading

**會議/互動 session** 開工只讀:

1. `AGENTS.md`(本檔)
2. `.agents/format.md`(要建立或修改任務檔時必讀)
3. 目前工作資料夾的任務檔與 `report.md`(驗收會議時)
4. 任務直接涉及的原始碼與測試

**agents 調度的任務 session** 只讀:

1. `AGENTS.md`(本檔)
2. 被指派的那一個任務檔
3. 任務直接涉及的原始碼與測試

預設不讀:舊工作資料夾、r 檔(日誌;除錯或被明確引用才讀)、`.agents/agents.log`。

### Working rules

- 不修改與目前任務無關的檔案。
- 共用規格用引用,不複製到各任務檔。
- 推測、已修改、已驗證、未驗證必須分開記錄。
- 未通過驗證不得宣告完成;pending 不得包裝成 completed。
- 程式碼、git 與測試結果才是最終事實來源。

### 任務 session 收尾(agents 調度時)

1. 逐項對照任務檔的 acceptance 自檢,並執行任務檔的 verify 命令確認退出碼 0。
2. 把**自己任務檔**的 status 改為 DONE 或 BLOCKED——整份任務檔只准改這一行,
   不碰其他任務檔。
3. 在對應的 r 檔(t 換 r,不存在就建立)檔尾 append 一筆 [[entry]]:
   time、by = "ai"、event、summary(可驗證事實,一句話)、detail(過程細節)。
4. 不執行 git commit——檢查點由調度器負責。

### 會議 session 收尾(互動時)

1. 共識即時落成任務檔,不留在對話裡;格式依 `.agents/format.md`。
2. 執行 `agents check`——通過才算散會,不通過就是計畫還沒完成。
3. 跨計畫仍有效的決策寫進本檔的 Permanent constraints。
