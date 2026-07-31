# 設計共識

*[English](../CONSENSUS.md)*

> 本檔為 [../CONSENSUS.md](../CONSENSUS.md) 的正體中文(台灣用語)翻譯。內容若
> 與英文版不一致,以英文版為準。

> 源自三輪討論(Claude Fable × GPT-5.6)的共識,隨架構演進持續更新。
> 目標:在「輸出品質可信可靠」與「極致節省 tokens」之間取得最穩健的平衡。
> 現行格式的唯一契約是 `~/.assent/format.md`(源碼在
> `assent/templates/format.md`);本檔記錄格式背後的設計原則。本檔是專案的
> 非規範性設計理念說明,不是可執行的任務格式契約本身——那份契約僅為
> `assent/templates/format.md`。

## 核心思想

產品命名空間是 `assent`,管理面是 `.assent/`。

不是讓 AI 聰明地從幾千行中挑出相關內容,而是把上下文分層,
讓每次任務只需載入「足以無歧義開工的最小上下文」。

```text
專案規則   → AGENTS.md(root,工具自動載入的入口,是否進版控由專案決定)
工作指示   → ~/.assent/instructions.md(assent session 行為與跨專案共通規則,
             每台機器一份)
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

調度器分開 focused task verification 與完整 candidate verification。在 AI task
session 中,以及 `assent verify FOLDER --focus` 中,distinct task-level `verify`
命令都在該資料夾的 source worktree 執行。focused verification 不寫 receipt、
不建立 integration candidate,即使通過也不能授權接受。資料夾完成後,無人值守的
完整驗證建立臨時 integration candidate 並執行完整 `.assent/verify.py`,結果寫成
可刪除重建的 `_verification.toml` derived receipt。`assent verify <FOLDER>` 是
零 token 的單一資料夾 refresh。

明示選取必須精確。`assent run A B` 只依寫出的順序執行 A、再執行 B,第一個
設定或執行失敗就停止;`assent run A B --all` 先完成這個明示順序,再依依賴順序
執行其餘未完成資料夾。`assent verify A B` 將恰好 A、B 正規化為依賴順序,合併
成一個 integration candidate,只執行一次完整 verifier。selected conflict 會拒絕,
不縮小集合。成功的 PASSED batch receipt 精確記錄選取的 source identity、中間
tree、最終 tree 與 verifier digest;失敗要求即使 bisect 留下 PASSED prefix,命令
仍回傳失敗,不能授權原本的 selected acceptance。這些 run 或 verify 命令都不改
target ref,也不接受任何資料夾。

選取語法在各個吃資料夾的命令之間是對稱的。`run`、`verify`、`accept`、`clean`、
`archive` 都接受字面 token `...` 作為最後一個位置參數,意思是「再加上這道命令
自己會發現的其餘每個資料夾」——`verify` 與 `accept` 只發現已完成的資料夾,其餘
三者則是每個工作資料夾。`...` 是 remainder operator,不是 `--all` 的別名:它產生
一個 exact selection,並在動任何東西之前先定格;與 `--all`(或
`verify --batch`/`--focus`、`run --once`/`--task`,以及只處理單一資料夾的
`archive --restore`)併用是用法錯誤。remainder 接在明示前綴之後,各命令再套用
自己的排序:`run` 保持前綴寫下的順序、remainder 依資料夾依賴順序,`verify` 與
`accept` 把整個選擇正規化為依賴順序,`clean` 則正規化為 upstream-first。
決定路徑的仍是數量,所以展開後的選擇就是一般的
exact selection:一個資料夾走單一資料夾路徑,兩個以上走 exact selected batch,而
selected acceptance 仍要求恰好涵蓋展開後集合的證據,且不驗證任何東西。

identity boundary 在進入命令自己的資格判斷以前就 fail-closed。每個明示的 live 資料夾,
包括最後 `...` 前的每個名稱,都必須符合既有發現規則:它是 `.assent/` 下的現存目錄,
而且至少含有一份正式 `tNNN_name.e.toml` 任務檔。只要有任一名稱 unresolved,Assent
就一次列出完整未解析集合,不 dispatch 任何被選資料夾,所以不會建立缺少的資料夾,
也不會讓較早的選擇先執行、驗證、發布、清理或封存。這項檢查不預先判斷狀態、依賴、
lock、receipt 或 Git 資格;省略資料夾、`--all`、`--batch` 與單獨的 `...` 仍是動態
發現路徑,而 archive restore 與已辨識的 archive recovery 可以在沒有 live 目錄時續作。

`assent run --verify` 只在 run 成功時接上完整驗證。失敗的 run 原樣回傳、不背書
任何東西;成功時驗證範圍與選擇一致 —— 一個資料夾寫 folder receipt,明示的多
資料夾選擇寫該 selected batch,`--all` 或單獨的 `...` 則是全專案的動態 batch ——
其 exit code 就是這道命令的 exit code。`--once`、`--task` 同樣可以併用:它們恰好
只選出一個資料夾,因此只有在該次受限執行讓所選資料夾變成完成時才驗證,而資料夾
未完成則此請求失敗且不寫下 receipt。這道拒絕來自 `verify_folder` 自己在建立
candidate 之前的關卡,會指出未完成的任務 id 與狀態,並且發生在任何整合 candidate
或完整驗證器之前。作為呼叫層級的請求,`--verify` 不理會設定中的 receipt 刷新政策。

在預設的 manual receipt-refresh 政策下,run 收尾延後的是 per-folder receipt;若這次
呼叫要求了 `--verify`,收尾會指出同一次呼叫接下來的 run-level verification,不會
叫使用者重新啟動那道已經要執行的命令。這樣交接仍然只有一個 invocation 與一個
選擇集合。

多資料夾的 `clean A B` 在一趟 upstream-first 流程裡清理,每個資料夾的證據規則
不變。多資料夾的 `archive A B` 遵守的是單一資料夾 `archive` 的契約而非 `--all`
的:每個被指名的資料夾都會嘗試,只是不合格也算被拒絕、以非零 exit code 結束,
而 `archive --all` 遇到這種資料夾只是略過,不算失敗。

`DONE` 是執行 AI 的主張,receipt 是 scheduler 的完整驗證證據,`accept` 則是明示
的人類批准。直接 `assent accept <FOLDER>` 從不執行 verifier:若 source tip 已由
ancestry 證明在 target 中,就是具冪等性的 no-op;否則必須有精確匹配的 fresh
per-folder PASSED receipt,並重播其重建的 candidate。selected
`assent accept A B` 同樣從不驗證,必須有恰好涵蓋依賴排序後 A、B 的 fresh batch
receipt;它重播每個記錄的 merge,一次原子發佈全部 selected 資料夾或一個也不發佈。
這兩種形式都不擴大集合,也不靜默接受剩餘資料夾。

`assent accept --all` 刻意有兩種模式。fresh PASSED batch receipt 會在不新增驗證
的情況下重播並原子發佈,只發佈 receipt 記錄的資料夾,其餘已完成資料夾只回報。
沒有 receipt,或 batch receipt 已過期/不是 PASSED 時,改走逐資料夾路徑:依依賴順序,
對每個尚未整合的資料夾先呼叫 `verify_folder_if_needed`,再執行一般 receipt-backed
accept。已整合資料夾是 ancestry no-op;已完成且 source branch 與 worktree 都在
已證明整合後被清除的資料夾,只有這條路徑會跳過。malformed batch receipt 會拒絕,
不會 fallback。逐資料夾路徑第一次真正的驗證或接受失敗就停止,但先前已發佈的成果
保留不撤回。

被忽略的輸入是交接問題,不是破口。候選由被追蹤內容加上剛好兩種鏡射產物——已佈建
的被忽略目錄連結,以及一般被忽略葉節點檔案——組成,因此「必要的被忽略目錄必須以
junction 或目錄符號連結佈建、絕不複製」這條規則,也寫在執行 session 真正會讀的
打包排程任務指示中,而不只寫在 format 契約裡。若完整 verifier 仍然失敗在某個
contributing source worktree 實體持有的被忽略目錄底下的路徑,證據會保留 verifier
輸出與 exit code,並附上一則 `Ignored input diagnosis:` 註記,指名該目錄、說明它是
刻意不放進候選的,並給出目錄連結的修法。它只回報 verifier 輸出自己指名的目錄,
分隔符號先正規化,且不列舉任何被忽略的樹。不新增複製 fallback、`local_inputs`
設定或 force 旗標。

哪些被忽略目錄是共享的,是審閱出來的決定,不是推論。沒有任何檔案系統規則能證明
某個被忽略目錄在語意上是必要的,因此這個答案只審閱一次,快取在主 worktree 那份
未被追蹤的 `.assent/manifest.toml`——它是本機執行記憶,不是專案來源,永不提交,
也不複製進任何 worktree。`[shared_paths]` 以指紋為鍵保留整份 profile(宣告路徑、
精確的被追蹤 `watch` 檔,以及那些檔案加上被追蹤 Git-ignore 規則的摘要),使並行
分支不會讓快取來回擺盪。source 快照為 `UNKNOWN`、`REVIEWED-NONE`(相符且
`paths = []` 的 profile 就是答案,絕不因為它是空的而再次觸發審閱)、
`REVIEWED-PATHS`(Assent 自行佈建精確的 junction 或目錄符號連結)或 `STALE`;
相符但互相矛盾的 profile 一律 fail closed。`assent shared-paths review` 是唯一的
寫入者,先驗證再變更,持有一個專案本地鎖,並以原子方式取代檔案。`UNKNOWN` 與
`STALE` 會為下一個已排程 session 附加一則有界的審閱指示,在 settled 之前拒絕其
closeout。每一條驗證入口與 `assent reconcile` 都在候選、verifier 或受管 worktree
出現之前先分類並調和,folder 與 batch receipt 綁定一個在 verifier 前後各取一次
快照的 `shared_inputs_sha256`,acceptance 在推進 ref 之前再次核對,且絕不為了讓它
通過而修復任何連結。

`NO-IGNORED-DIRECTORY-CANDIDATE` 是與這些狀態並列、確定性的零 token 快捷路徑。
它只主張:一次成功的 Git ignored-entry 查詢在主 worktree 找不到任何位於 `.git/`
與 `.assent/` 之外、實際存在的一般被忽略目錄,而不是主張這個專案在語意上不需要
共享輸入。它不需要 profile、junction 或 AI 審閱即告 settled,在 receipt 摘要中帶有
與 `REVIEWED-NONE` 不同的身分,並在每一道適用的關卡廉價地重新計算。它 fail
closed:Git 查詢失敗是可行動的拒絕,而不是空的候選集合;被忽略的葉節點檔案不算,
任何一般被忽略目錄都算,即使之後審閱成 `paths = []`;出現候選時下一次分類為
`UNKNOWN`,除非已有相符的快取 profile 回答它;完整 verifier 的 `required_evidence`
指名缺少的目錄時,主 worktree 有合法目標就轉為審閱,否則以精確的「目標缺少或未被
忽略」問題拒絕。候選列舉之所以問主 worktree,是因為每個被允許的連結目標都必須是
該主 worktree 同一相對路徑上實際存在、被 Git 忽略的一般目錄,而全新的 source
checkout 本來就不會有;只存在於尚未接受之 source 分支上的目錄或 ignore 規則尚不可
佈建,會發出可行動的拒絕,而不是宣稱不需要。

receipt 是 derived artifact,不凌駕 Git。target tip 改變但重建後 integration tree
完全相同仍可接受;內容改變就 stale。直接與 selected acceptance 遇到 missing、
malformed、stale 或 mismatch evidence 時會拒絕,不自行啟動驗證。passive merge
metadata 只供人讀稽核,不是 clean 後的狀態資料庫;dependent 尚未接受時保留
upstream source。所有接受路徑都維持本地人類批准,conflict 不推進 target,也沒有
remote、pull、rebase、force、自動解衝突或刪除 source 的行為。

流程是 `run` -> focused checks -> 明示的完整 `verify`(單一、selected 或 dynamic
batch) -> 人類審查 -> `accept` -> 可選的一般 Git 同步 -> `clean`。單有
verification receipt 從不會發布任何東西。

打包 verifier 會檢查 working tree,也會檢查 candidate 的 `HEAD` 相對第一父提交的
committed delta 是否殘留衝突標記; root commit 沒有父提交時安全略過第二項。只有
空白差異時不阻擋驗證,包括換行格式、行尾空格或 tab,以及檔尾空白行;需要格式政策的
專案應加入明確的 formatter 檢查。新的 `assent init` 必須明示選擇平行 unittest、
pytest、npm test、Flutter test 或 custom argv 命令。打包 template 的每個專案測試
範例都維持註解,產生的副本只啟用一個選項,因此空專案在選定測試不存在時不能回報
`verify: OK`。

重跑 `assent init` 永不取代既有 `.assent/verify.py`,已有 verifier 時提供 `--test`
會拒絕。它會從打包契約刷新 `~/.assent/format.md` 與 `~/.assent/instructions.md`,
並把打包組態中遺漏的 active table/key path 補進 `~/.assent/assent.toml`,保留既有
及自訂值。專案內舊的契約副本只有與打包文字完全相同時才會被移除;不同的會保留並
發出警告,因為 session 讀的都是使用者家目錄的契約。輸入與 TOML 驗證都完成後才會
改動任何受管檔案。開任何 session 之前,兩份使用者家目錄契約都必須存在、可讀且與
這套安裝的打包文字逐位元組相同,否則 run 會 fail-closed 並指向 `assent init`,而不
是在執行中途修補它們。verifier digest 改變會使舊 receipt stale,因此接受前必須在
無人值守階段以 `assent verify <FOLDER>` refresh 證據。

worktree 是變更隔離、衝突管理、稽核與 Git 復原邊界,不是安全 sandbox。`danger-full-access`
與 `bypassPermissions` 等完整權限模式仍讓 AI 接觸其 OS 身分可用的 network、credential、
外部 Git 寫入者與 worktree 外檔案。使用者必須選擇可信任的專案與帳號環境;產品不另建
container 或 VM sandbox。

## 位置慣例

- `AGENTS.md` 必須留在 project root——agent 工具自動在 root 尋找指令檔,
  位置本身就是功能。它只放專案規則與一行 assent 橋接;進版控時使用
  worktree 內的分支版本,未進版控時由調度器提示主樹絕對路徑。
- assent session 行為與跨專案共通規則放在 `~/.assent/instructions.md`,不混入
  專案 AGENTS.md。它與同目錄的 `~/.assent/format.md` 描述的是工具而非任何單一
  專案,所以每台機器只安裝一份,專案不會拿到副本;共用的
  `~/.assent/assent.toml` 設定也放在旁邊。其餘專案專屬管理檔則全部收進專案自己
  的 `.assent/`,root 保持乾淨。
- 設定解析順序是內建預設值、`~/.assent/assent.toml`、選用的
  `.assent/assent.toml` 專案覆寫,最後是指令有提供時的顯式 CLI 選擇。table 依
  key 合併,scalar 與 array 整個取代,因此覆寫會就它寫出的那些 key 遮蔽日後對
  共用設定的修改;`assent init` 逐位元組保留這種檔案,絕不搬移它。只有省略才
  代表繼承:`key =` 是無效 TOML,空 table 表示不覆寫任何葉節點,空 array 在該
  欄位允許時是顯式取代,空字串對需要有意義文字的設定一律拒絕,而不是安靜地把
  下層值放回來。
- 專案保有真正屬於自己的東西:`AGENTS.md`、`.assent/verify.py`、工作資料夾與
  其中的執行期產物。`assent init` 只刷新前者裡那一行 bridge,也絕不覆寫後者。
- 整個 `.assent/` 由 `.gitignore` 排除,只留在主工作樹;調度器用絕對路徑
  把 t/r 與預設驗收腳本(主樹路徑)以及兩份契約(`~/.assent` 路徑)交給
  worktree session,不製造第二份真本。
- 驗收腳本預設在主樹 `.assent/verify.py`,內容是專案自己的檢查命令;
  從主樹載入腳本,但以 worktree 為 cwd 驗收隔離後的成果。
- Git 永遠啟用並一律使用 worktree,不得以切換開關或無 Git 降級模式取代;
  這是安全平行處理多個工作資料夾的必要條件。任何已追蹤的 `.assent/` 檔案
  都會 fail-closed,避免第二份真本。
- 工作資料夾內的 `assent.lock` 保證同一資料夾一個 run;worktree 路徑為
  `<專案名>.worktrees/<資料夾>/`,可用位置參數指定工作資料夾。所有權是綁在
  開啟檔案 handle 上的 OS 層級鎖,正常結束、Ctrl+C、當機與強制終止都會釋放;
  檔案本身刻意留下作為診斷資料,從來不是需要清理的 stale lock 問題。
  `run --all` 在任何離開路徑結束前都會回收它擁有的工作資料夾子行程,因此紀錄
  的 PID 若仍存活,代表真的有行程在跑,而不是殘留檔案。
- 工作資料夾名稱採可攜的 Windows/Git-ref 契約:不可為空,不可含空白、路徑
  分隔符、控制字元、Git-ref 禁用字元(`~`、`^`、`:`、`?`、`*`、`[`),或
  Windows 禁用字元(`<`、`>`、`"`、`|`);不可 `-` 或 `.` 開頭,不可含 `..`
  或 `@{`,不可用 `.` 或 `.lock` 結尾,也不可使用保留的 Windows 裝置名稱。
  它同時是 Git branch prefix,所以建立 worktree 或 branch 前就會套用這項驗證。

## 資料夾依賴共識

依賴跟著工作資料夾走:資料夾可用 `_folder.toml` 的 `after` 宣告直接前置,
沒有該檔案即代表沒有前置。資料夾完成不靠手工狀態,而是由其中全部正式任務
檔現場推導,只有全為 `DONE` 或 `SKIP` 才算完成。`run` 的前置閘門與 `check`
的完整依賴圖驗證都採 fail-closed:前置未完成、引用不存在、解析失敗或循環
一律拒絕繼續。

`after` 控制 readiness,只有明示的 `base` 才選出可重現的堆疊檔案內容。下游
只能透過 `base` 堆疊在零個或恰一個尚未接受的 upstream 上;其他 `after` upstream
只提供順序,沒有 `base` 時從目前 integration target 開始,不會變成隱含的
integration engine。操作順序是 `run A`、`run B` 堆疊在 A 上、combined verification、
人類 `accept A`,再人類 `accept B`。若 source tip、integration tree、verifier digest
仍相同,receipt 可重用,因此 direct 與 selected accept 都是快速證據檢查,不重跑
完整 suite。A 前進後,B 會 stale 但成果保留;應 rework/reject B 或開新資料夾,
不重寫 stack history。同檔案修改也採一般 Git 整合:能自動合併時由 exact-tree
verification 覆蓋,conflict 則 target 不變交由人工作裁決。Assent 不自動 rebase、
解衝突或 push。清理採 upstream-first:直接 dependent 在接受且有機械證據證明
整合並乾淨前都保留 source evidence;之後才可清除多餘成果,不另設狀態資料庫。

## 批次衝突略過共識(2026-07-26)

Exact selected verification 的輸出標籤是 `verify selected`;`verify --batch` 只保留
給動態發現與互動式衝突略過決策。Exact selected 的 candidate 建置若發生衝突,
會在任何恢復建議前說明完整 verifier 尚未執行,列出衝突資料夾與路徑,並說明沒有
寫入 receipt、target 與 selected source ref 都維持不變。若資料夾單獨就與 target
衝突,直接指向 `assent reconcile <FOLDER>`;若只是 peer-only conflict,會列出衝突
資料夾前方相容的 selected prefix,建議先驗證並接受該 prefix,等 target 前進後再
針對衝突資料夾 reconcile。`rework` 與 `reject` 仍是明確替代方案。Exact
selection 絕不提問略過,也不縮小集合。

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

略過刻意不是任何形式的解決:它不改變 target 或任何 source(不論被略過或已合併)。
若是 peer-only conflict,可先驗證並接受衝突資料夾前方相容的工作,讓 target 前進
後再處理該資料夾;`rework` 與 `reject` 仍是重新開啟或捨棄它的明確替代方案。
`accept --all` 有兩種不同模式:fresh PASSED
batch receipt 的 release 路徑只在一次原子 ref 更新中發佈 receipt 涵蓋的確切
資料夾,其餘被排除的已完成資料夾只回報,同一次執行不驗證或接受它們。沒有 receipt,
或 evidence 已過期/不是 PASSED 時,刻意的逐資料夾路徑會逐一驗證並接受,第一次
真正失敗就停止但保留先前發佈的成果。malformed receipt 會拒絕,不選擇這個 fallback。
`archive --all` 延伸 `clean` 已在強制的 upstream-first 規則:只封存獨立符合資格的
資料夾,並持續保留尚未被接受的 dependent 仍需要的 source evidence。

## 人工調解共識(2026-07-27)

Conflict 在內容上仍是人類的決定,但圍繞該決定的 Git 機制歸 Assent 掌管。
`assent reconcile FOLDER` 把兩者分開:人類只編輯衝突檔案,Assent 執行每一個
Git 操作。start 會在專屬 worktree `<project>.reconcile/<FOLDER>` 內、於臨時
分支 `assent-reconcile/<FOLDER>` 上,把擷取到的 target tip 合併進確切的
資料夾 source;這個 merge 以 source 為先建立,因此 source 可以被 fast-forward
到它上面。主 worktree 與 source worktree 維持乾淨,整合 target 從不被改變。
`--continue` 把解決加入索引、驗證、提交 merge、推進 source 分支並清理;
`--abort` 只移除它重新證明過自己所管理的資源。沒有狀態檔——worktree、臨時
分支、`HEAD`、`MERGE_HEAD` 與 merge parents 就是可續行的狀態,因此任何中斷
與拒絕都會保留 worktree、分支與每一筆編輯。

驗證邊界並未移動。`--continue` 既不執行聚焦任務測試也不執行完整驗證,且不寫
receipt;由於 source 確實前進,它會刪除依舊 source 身分寫成的 receipt——那是
derived artifact,重建的代價只是一次 `assent verify`。`assent verify FOLDER`
仍是由人控制的昂貴步驟,`assent accept FOLDER` 仍是明確批准,並且仍要求一份
fresh、可重現的 `PASSED` 完整驗證 receipt。Reconcile 不是整合引擎:只處理單一
資料夾對當前整合 target、不自動解決內容;兩個未被接受 source 之間、只在批次中
出現的 conflict 仍留給動態 `verify --batch` 的略過決定。若 peer-only conflict
前方已有相容工作,先驗證並接受該工作,再對 target 前進後的衝突資料夾 reconcile;
`rework` 或 `reject` 仍是明確替代方案。

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

由於身分在 session 開啟前就已完整解析,`run` 會用一行精簡訊息表示:

```
  Session: codex | core->gpt-5.6-terra | heavy->high
```

讀法是先看 adapter,再看兩組對應。每個箭頭都把左側任務檔寫的可攜抽象值,
對應到右側實際傳給該 adapter CLI 的引數;因此 `core->gpt-5.6-terra` 是這次的
`--model` 值,`heavy->high` 是這次的 `--effort` 值。adapter、檔位、模型、effort
四項稽核事實都保留在單行,不再展開成冗長標籤。

## 媒體是一般的專案脈絡

任務使用的媒體——圖片、PDF、音訊檔、影片——是由文字任務契約引用的一般專案
脈絡,不是 schema 功能。固定任務欄位因此不變:assent 不新增 `inputs`、image、
audio 或 video 欄位,不提供 adapter 附件協定,不推測模型的媒體能力,也不新增第二個
審查狀態。

任務若使用專案中已存在的媒體檔,必須在 `behavior` 或 `notes` 以專案相對路徑寫明
檔案與用途。只讀取的參考路徑不必放進 `scope`;任務可能建立或修改的每個媒體檔都
必須由 `scope` 涵蓋,與原始碼相同。媒體應放在已納入版控的工作樹檔案中以確保可重現,
不要把來源媒體放進產生的 `.assent/` 管理面。`verify` 仍承載可由機器檢查的要求,
視覺或感知判斷仍是人類明確執行 `accept` 的一部分,不會成為第二個審查狀態。

在具體的 adapter 附件需求證明 schema 變更確有必要以前,文字契約在足夠時就是較簡單
的做法。

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
  `~/.assent/instructions.md`,且不靠自覺:調度器的結構比對讓「放寬自己的驗收」
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
| 確實不同的一輪目標與現行計畫混在一起 | 開新工作資料夾;舊資料夾作為 `after` 前置繼續參與依賴判定。若是對某個仍存活、尚未被接受的資料夾自身目標所做的審查或驗證後續,則改以新編號的任務附加到該資料夾 |
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
