# 設計共識

*[English](../CONSENSUS.md)*

> 本檔為 [../CONSENSUS.md](../CONSENSUS.md) 的正體中文(台灣用語)翻譯。內容若
> 與英文版不一致,以英文版為準。翻譯所依版本:`92e1e39` (2026-07-20)。

> 源自三輪討論(Claude Fable × GPT-5.6)的共識,隨架構演進持續更新。
> 目標:在「輸出品質可信可靠」與「極致節省 tokens」之間取得最穩健的平衡。
> 現行格式的唯一契約是 `.assent/format.md`(源碼在
> `assent/templates/format.md`);本檔記錄格式背後的設計原則。本檔是專案的
> 非規範性設計理念說明,不是可執行的任務格式契約本身——那份契約僅為
> `assent/templates/format.md`。

## 核心思想

產品命名空間是 `assent`,管理面是 `.assent/`。

不是讓 AI 聰明地從幾千行中挑出相關內容,而是把上下文分層,
讓每次任務只需載入「足以無歧義開工的最小上下文」。

```text
專案規則   → AGENTS.md(root,工具自動載入的入口,是否進版控由專案決定)
工作指示   → .assent/instructions.md(assent session 行為與跨專案共通規則)
本次任務   → .assent/<工作資料夾>/tNNN_name.toml(任務檔,執行上自包含)
目前狀態   → 任務檔的 status + git(任務檔即狀態,沒有另外的狀態檔)
歷史證據   → rNNN_name.toml(一任務一檔日誌,append-only,預設不讀)
正確性證明 → 任務檔的 verify 命令(預設 .assent/verify.py)
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
   task focused verify exit 0,全部通過才 commit 檢查點。
   摘要只寫可驗證事實;pending 不得包裝成 completed。

## 驗證、receipt 與人類接受

調度器分開 focused task verification 與完整 candidate verification。AI session
只執行任務的 `verify`;資料夾全部完成後,AI session 外的 scheduler 建立一次臨時
integration candidate,執行完整 `.assent/verify.py`,結果寫成可刪除重建的
`_verification.toml` derived receipt。`assent verify <FOLDER>` 是零 token 的
receipt refresh,報告會顯示 `PASSED`/`FAILED` 以及 `fresh`/`stale`。

`DONE` 是執行 AI 的主張,receipt 是 scheduler 的完整驗證證據,呼叫
`assent accept <FOLDER>` 才是人類批准。accept 必須明示單一資料夾,只快速重建
candidate 並比對 source tip、integration tree、verifier digest 與 fresh `PASSED`
receipt,不重跑完整 verifier。missing/stale receipt 必須先執行
`assent verify`;task format 沒有 `review` 欄位。

receipt 是 derived artifact,不凌駕 Git。target tip 改變但重建後 integration tree
完全相同仍可接受;內容改變就 stale。source worktree 或 branch 消失後拒絕;passive
merge metadata 只供人讀稽核,不是 clean 後的狀態資料庫。dependent 尚未接受時,
保留 upstream source。

流程是 `run` -> 無人值守完整驗證 receipt -> 人類審查 -> `accept` -> 可選的一般
Git 同步 -> `clean`。accept 僅限本地、單一資料夾,沒有 `--all`、`--push`、remote/PR、
pull、rebase、force、自動解衝突或刪 source。

打包 verifier 會檢查 working tree,也會檢查 candidate 的 `HEAD` 相對第一父提交的
committed delta; root commit 沒有父提交時安全略過第二項。這能抓到已提交的尾端空白,
不讀專案私有 verifier,也不呼叫 shell。`assent init` 永不取代既有的
`.assent/verify.py`,專案要自行人工同步。verifier digest 改變會使舊 receipt stale,
因此接受前必須在無人值守階段以 `assent verify <FOLDER>` refresh 證據。

worktree 是變更隔離、衝突管理、稽核與 Git 復原邊界,不是安全 sandbox。`danger-full-access`
與 `bypassPermissions` 等完整權限模式仍讓 AI 接觸其 OS 身分可用的 network、credential、
外部 Git 寫入者與 worktree 外檔案。使用者必須選擇可信任的專案與帳號環境;產品不另建
container 或 VM sandbox。

## 位置慣例

- `AGENTS.md` 必須留在 project root——agent 工具自動在 root 尋找指令檔,
  位置本身就是功能。它只放專案規則與一行 assent 橋接;進版控時使用
  worktree 內的分支版本,未進版控時由調度器提示主樹絕對路徑。
- assent session 行為與跨專案共通規則放在 `.assent/instructions.md`,不混入
  專案 AGENTS.md。其餘管理檔也全部收進 `.assent/`,root 保持乾淨。
- 整個 `.assent/` 由 `.gitignore` 排除,只留在主工作樹;調度器用絕對路徑
  把 instructions、t/r 與預設驗收腳本交給 worktree session,不製造第二份真本。
- 驗收腳本預設在主樹 `.assent/verify.py`,內容是專案自己的檢查命令;
  從主樹載入腳本,但以 worktree 為 cwd 驗收隔離後的成果。
- Git 永遠啟用並一律使用 worktree,不得以切換開關或無 Git 降級模式取代;
  這是安全平行處理多個工作資料夾的必要條件。任何已追蹤的 `.assent/` 檔案
  都會 fail-closed,避免第二份真本。
- 工作資料夾內的 `assent.lock` 保證同一資料夾一個 run;worktree 路徑為
  `<專案名>.worktrees/<資料夾>/`,可用位置參數指定工作資料夾。

## 資料夾依賴共識

依賴跟著工作資料夾走:資料夾可用 `_folder.toml` 的 `after` 宣告直接前置,
沒有該檔案即代表沒有前置。資料夾完成不靠手工狀態,而是由其中全部正式任務
檔現場推導,只有全為 `DONE` 或 `SKIP` 才算完成。`run` 的前置閘門與 `check`
的完整依賴圖驗證都採 fail-closed:前置未完成、引用不存在、解析失敗或循環
一律拒絕繼續。

`after` 也選出可重現的 worktree base。下游最多只能堆疊在零個或恰一個
尚未接受的 upstream 上;多個時拒絕,不把它變成隱含的 integration engine。
操作順序是 `run A` -> `run B` 堆疊在 A 上 -> combined verification -> 人類
`accept A` -> 人類 `accept B`。若 source tip、integration tree、verifier digest
仍相同,combined candidate 的 receipt 可重用,因此 accept 是快速證據檢查,
不重跑完整 suite。A 前進後,B 會 stale 但成果保留;應 rework/reject B 或開新
資料夾,不重寫 stack history。同檔案修改也採一般 Git 整合:能自動合併時由
exact-tree verification 覆蓋,conflict 則 target 不變交由人工作裁決。Assent
不自動 rebase、解衝突或 push。清理採 upstream-first:直接 dependent 在接受且
有機械證據證明整合並乾淨前都保留 source evidence;之後才可清除多餘成果,
不另設狀態資料庫。

## 批次衝突略過共識(2026-07-26)

`verify --batch` 從不自行解決 source conflict;它只做一次決定:是否改為
證明一個較小的批次,而非完全不證明。建置批次候選時,無論較早出現的
conflict 為何,都仍會依序嘗試合併每個排入佇列的資料夾,因此一個資料夾
conflict 不會阻止之後、彼此獨立的資料夾也被嘗試。沒有 conflict 的批次
維持完全無人值守。一旦有一個以上資料夾發生 conflict,系統會把每個
conflict 的資料夾與其遞移排在其 `after` 之後的下游一併蒐集、回報,然後
只問一次 `[Y/n]`:是否略過整組被排除者,改為驗證其餘仍可合併的資料夾。
明確的「是」會對這個較小子集執行一次完整驗證,receipt 只記錄這個子集;
「否」、無法辨識的回答、或 EOF 一律 fail-closed,不證明任何東西。若整批
都沒有獨立可提供的資料夾,批次會直接拒絕,不會提問。

略過刻意不是任何形式的解決:它不改變 target 或任何 source(不論被略過
或已合併),conflict 資料夾自身的 source 仍須經過明確的人工 `rework` 或
`reject` 才能重新加入未來的批次。`accept --all` 的批次發佈路徑在發佈端
呼應同一紀律:它只在一次原子 ref 更新中發佈 receipt 涵蓋的確切資料夾,
並在同一次執行內,只回報 receipt 未涵蓋的其餘已完成資料夾——不會有第二次
提問,也不會有同一次執行內自行驗證或接受那批剩餘項目的 fallback。
`archive --all` 延伸 `clean` 已在強制的 upstream-first 規則:只封存
獨立符合資格的資料夾,並持續保留尚未被接受的 dependent 仍需要的
source evidence。

## 模型與推理投入共識

`model` 與 `effort` 是正交的抽象檔位。任務的 model 固定使用
`prime` / `core` / `lite`;選填 effort 固定使用 `heavy` / `normal` / `slight`,
通常省略,只有刻意偏離 adapter 對該 model 的預設時才明寫。三個 effort 值
描述可攜的相對投入,不是精確預算;`heavy` 也不宣稱等於廠牌原生最高檔。

effort 分成選擇與翻譯兩步。選擇是決定性的,依序有三個來源:任務明寫值、組態中
該檔位的 `default_effort` 覆寫、該檔位的內建預設值。寫出來的 `default_effort`
表是逐檔位覆寫,不是整張取代內建表,所以該表缺席、為空或只寫一部分時,每個已知
檔位仍然都有值。由此得到本次定案的結論:每一次受支援的呼叫都會傳入具體的
requested effort,assent 絕不省略該旗標去沿用廠商 CLI 自己的預設。
選出抽象值後,engine 依「檔位分節 > 平面 > 內建基準」查
`efforts` 設定。內建基準把 `heavy` 對應 `high`,`normal` 對應 `medium`,把
`slight` 對應 `low`;每個抽象鍵都會獨立地從檔位分節退回平面表,再退回基準表。
抽象詞與廠商 effort 詞刻意不同字,因此抽象值不能原值直通。平面層表達 adapter
的通例,model 檔位分節只寫少數例外格。
廠牌特有 effort 是與 models 對照表同級的設定資料,不得進入任務格式、
`default_effort` 或 Adapter 程式碼;Adapter 介面只接收翻譯後的實際值。

## 品質標準(取代 token 數字 KPI)

**冷啟動測試**:一個零記憶的新 AI 只讀 AGENTS.md + instructions.md +
任一 `TODO` 任務檔,
能否不問問題就正確說出目標、可改動範圍、驗收條件、下一步?
能 → 計畫定稿;不能 → 任務檔資訊不足。
機器側等價物:`assent check` 通過——這也是規劃會議的散會條件。

這套架構消除的是「每次重讀全部歷史」的 O(n) 成長:調度、驗收、報告
全部是純 Python 本地作業,零 token;實際成本只剩每個任務 session
需要檢查的程式碼與驗證輸出。

## 維護紀律

- AI 交接最容易在 session 尾聲、上下文快滿時鬆掉 → 收尾協定寫死在
  `.assent/instructions.md`,且不靠自覺:調度器的結構比對讓「放寬自己的驗收」
  直接判失敗,scope 豁免只有任務自己的 t 檔與 r 檔。
- 人的角色只剩審查與裁決:讀 `_report.md`(零 token),只對要裁決的任務
  開 session 下指令;人不手改檔案,改檔一律由 AI 依指示執行。
- 執行 AI 燒過 tokens 的產出絕不丟棄:額度中斷收 wip 檢查點續作;
  驗收失敗不還原、帶原因重試;重試用盡連同成果 commit 進 BLOCKED
  檢查點交人類裁決。
- 合併後的 worktree 與分支清理由 `assent clean` 機械執行;安全條件必須由機器
  證明,人不手動執行 Git 清理。駁回整個資料夾的實作亦由 `assent reject`
  機械執行(封存、強刪、任務改回 TODO、r 檔留痕),同樣不手動操作 Git。
- 單一任務的驗收重做由 `assent rework <FOLDER> <TASK>` 機械執行。預設保留
  程式碼,下游連動必須明示;反向程式碼只接受可證明為連續分支尾段的 checkpoints,
  並建立新 commit 而不改寫歷史。操作只更新狀態與報告,不自動啟動 AI。

## 升級路徑(先有痛點,再加結構)

| 痛點 | 才加入 |
|------|--------|
| 新一輪目標與現行計畫混在一起 | 開新工作資料夾;舊資料夾作為 `after` 前置繼續參與依賴判定 |
| 同一決策反覆被推翻、AI 重採已否決方案 | 沉澱進 AGENTS.md 的 Permanent constraints |
| 多任務重複大量共用說明 | 抽成引用(檔案或錨點),任務檔只留指標 |

不要為可能永遠不會出現的問題,預先建立文件官僚體系。

## 一句話定案

> 用 AGENTS 管專案規則、instructions 管 assent 行為、任務檔管本次與現在、
> r 檔管歷史、verify 管真假;執行 session 預設只讀 AGENTS + instructions +
> 自己的任務檔,結束時客觀驗收、
> 精準寫回、細節歸檔。
>
> 文件負責讓 AI 快速接手,Git 與調度器的客觀閘門負責保證事實,
> 人類負責裁決——省下 tokens 的前提,永遠是輸出品質可信可靠。
