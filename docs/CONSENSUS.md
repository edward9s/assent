# 設計共識

> 源自三輪討論(Claude Fable × GPT-5.6)的共識,隨架構演進持續更新。
> 目標:在「輸出品質可信可靠」與「極致節省 tokens」之間取得最穩健的平衡。
> 現行格式的唯一契約是 `.agents/format.md`(源碼在
> `agents/templates/format.md`);本檔記錄格式背後的設計原則。

## 核心思想

不是讓 AI 聰明地從幾千行中挑出相關內容,而是把上下文分層,
讓每次任務只需載入「足以無歧義開工的最小上下文」。

```text
專案規則   → AGENTS.md(root,工具自動載入的入口,是否進版控由專案決定)
工作指示   → .agents/instructions.md(agents session 行為與跨專案共通規則)
本次任務   → .agents/<工作資料夾>/tNNN_名稱.toml(任務檔,執行上自包含)
目前狀態   → 任務檔的 status + git(任務檔即狀態,沒有另外的狀態檔)
歷史證據   → rNNN_名稱.toml(一任務一檔日誌,append-only,預設不讀)
正確性證明 → 任務檔的 verify 命令(預設 .agents/verify.py)
```

## 四條核心原則

1. **分層**
   規則、任務、狀態、歷史不混檔。
   執行 session 的必讀集只有專案 AGENTS.md + instructions.md + 被指派的
   那一個任務檔;會議 session 加讀 format.md,驗收會議加讀工作資料夾內的
   `_report.md`。

2. **生成而非快照**
   早期設計有一份手寫的 CURRENT.md 導航快照,而「失真的權威快照比沒有
   快照更危險」。現行架構直接取消手寫快照:狀態就在任務檔與 git 裡,
   人讀的 `_report.md` 由程式機械彙整、每次整檔重寫,不可能與事實分歧。
   事實優先序:程式行為與測試結果 → 原始碼與 Git → 任務檔 → r 檔日誌。

3. **重寫而非追加**
   任務檔記現在式:status 由調度器精準寫回,其餘位元組不動。
   過程細節 append 進 r 檔,永不回改既有條目。
   任務檔「執行上自包含」(目標、範圍、驗收直接寫),但共用知識引用而非
   複製,避免版本分歧;專案特有且跨計畫仍有效的決策沉澱進 AGENTS.md。

4. **測試證明正確**
   文字說明意圖,verify 證明正確。「執行 AI 自稱 DONE」只是宣稱,
   完成與否由調度器客觀驗收:狀態 → 結構比對(防竄改)→ scope →
   verify exit 0,全部通過才 commit 檢查點。
   摘要只寫可驗證事實;pending 不得包裝成 completed。

## 位置慣例

- `AGENTS.md` 必須留在 project root——agent 工具自動在 root 尋找指令檔,
  位置本身就是功能。它只放專案規則與一行 agents 橋接;進版控時使用
  worktree 內的分支版本,未進版控時由調度器提示主樹絕對路徑。
- agents session 行為與跨專案共通規則放在 `.agents/instructions.md`,不混入
  專案 AGENTS.md。其餘管理檔也全部收進 `.agents/`,root 保持乾淨。
- 整個 `.agents/` 由 `.gitignore` 排除,只留在主工作樹;調度器用絕對路徑
  把 instructions、t/r 與預設驗收腳本交給 worktree session,不製造第二份真本。
- 驗收腳本預設在主樹 `.agents/verify.py`,內容是專案自己的檢查命令;
  從主樹載入腳本,但以 worktree 為 cwd 驗收隔離後的成果。
- Git 永遠啟用並一律使用 worktree,不得以切換開關或無 Git 降級模式取代;
  這是安全平行處理多個工作資料夾的必要條件。任何已追蹤的 `.agents/` 檔案
  都會 fail-closed,避免第二份真本。
- 工作資料夾內的 `agents.lock` 保證同一資料夾一個 run；worktree 路徑為
  `<專案名>.worktrees/<資料夾>/`，可用位置參數指定工作資料夾。

## 資料夾依賴共識

依賴跟著工作資料夾走:資料夾可用 `_folder.toml` 的 `after` 宣告直接前置,
沒有該檔案即代表沒有前置。資料夾完成不靠手工狀態,而是由其中全部正式任務
檔現場推導,只有全為 `DONE` 或 `SKIP` 才算完成。`run` 的前置閘門與 `check`
的完整依賴圖驗證都採 fail-closed:前置未完成、引用不存在、解析失敗或循環
一律拒絕繼續。

## 模型與推理投入共識

`model` 與 `effort` 是正交的抽象檔位。任務的 model 固定使用
`prime` / `core` / `lite`;選填 effort 固定使用 `low` / `medium` / `high`,
通常省略,只有刻意偏離 adapter 對該 model 的預設時才明寫。三個 effort 值
描述可攜的相對投入,不是精確預算;`high` 也不宣稱等於廠牌原生最高檔。

effort 分成選擇與翻譯兩步:任務明寫值優先於 `default_effort[model]`,兩者皆無
就不傳值、採 CLI 預設;選出抽象值後,engine 依「檔位分節 > 平面 > 等值」查
`efforts` 設定。平面層表達 adapter 的通例,model 檔位分節只寫少數例外格。
廠牌特有 effort 是與 models 對照表同級的設定資料,不得進入任務格式、
`default_effort` 或 Adapter 程式碼;Adapter 介面只接收翻譯後的實際值。

## 品質標準(取代 token 數字 KPI)

**冷啟動測試**:一個零記憶的新 AI 只讀 AGENTS.md + instructions.md +
任一 `TODO` 任務檔,
能否不問問題就正確說出目標、可改動範圍、驗收條件、下一步?
能 → 計畫定稿;不能 → 任務檔資訊不足。
機器側等價物:`agents check` 通過——這也是規劃會議的散會條件。

這套架構消除的是「每次重讀全部歷史」的 O(n) 成長:調度、驗收、報告
全部是純 Python 本地作業,零 token;實際成本只剩每個任務 session
需要檢查的程式碼與驗證輸出。

## 維護紀律

- AI 交接最容易在 session 尾聲、上下文快滿時鬆掉 → 收尾協定寫死在
  `.agents/instructions.md`,且不靠自覺:調度器的結構比對讓「放寬自己的驗收」
  直接判失敗,scope 豁免只有任務自己的 t 檔與 r 檔。
- 人的角色只剩審查與裁決:讀 `_report.md`(零 token),只對要裁決的任務
  開 session 下指令;人不手改檔案,改檔一律由 AI 依指示執行。
- 執行 AI 燒過 tokens 的產出絕不丟棄:額度中斷收 wip 檢查點續作;
  驗收失敗不還原、帶原因重試;重試用盡連同成果 commit 進 BLOCKED
  檢查點交人類裁決。
- 合併後的 worktree 與分支清理由 `agents clean` 機械執行；安全條件必須由機器
  證明，人不手動執行 Git 清理。駁回整個資料夾的實作亦由 `agents reject`
  機械執行(封存、強刪、任務改回 TODO、r 檔留痕),同樣不手動操作 Git。

## 升級路徑(先有痛點,再加結構)

| 痛點 | 才加入 |
|------|--------|
| 新一輪目標與現行計畫混在一起 | 開新工作資料夾;舊資料夾作為 `after` 前置繼續參與依賴判定 |
| 同一決策反覆被推翻、AI 重採已否決方案 | 沉澱進 AGENTS.md 的 Permanent constraints |
| 多任務重複大量共用說明 | 抽成引用(檔案或錨點),任務檔只留指標 |

不要為可能永遠不會出現的問題,預先建立文件官僚體系。

## 一句話定案

> 用 AGENTS 管專案規則、instructions 管 agents 行為、任務檔管本次與現在、
> r 檔管歷史、verify 管真假;執行 session 預設只讀 AGENTS + instructions +
> 自己的任務檔,結束時客觀驗收、
> 精準寫回、細節歸檔。
>
> 文件負責讓 AI 快速接手,Git 與調度器的客觀閘門負責保證事實,
> 人類負責裁決——省下 tokens 的前提,永遠是輸出品質可信可靠。
