# assent — AI 計畫格式 + 自動調度器

*[English](README.md)*

> 本檔為 [README.md](README.md) 的正體中文(台灣用語)翻譯。內容若與英文版
> 不一致,以英文版為準。翻譯所依版本:`92e1e39` (2026-07-20)。

一套讓 AI 在長期專案中以最小上下文正確工作的檔案體系,加上讀懂這套體系、
無人值守執行的調度器。

- **規劃**:人與 AI 開會議 session,共識即時固化為 `.assent/` 裡的任務檔,
  散會條件 = `assent check` 通過。
- **執行**:`assent run` 無人值守跑完全部任務——選任務、開 headless AI session、
  執行任務 focused verify、建立 git 檢查點、額度等待與續作。資料夾完成後,
  調度器在 AI session 外執行一次完整 candidate verify;調度本身零 token。
- **驗收**:人先讀程式生成的 `.assent/<工作資料夾>/_report.md`(零 token),只對要裁決的任務開 session。

## 設計原則

1. **在確保輸出品質可信可靠的前提下,最小化 tokens 消耗。**
   調度、驗收、報告全部是純 Python 本地作業;每個 AI session 的必讀集只有
   專案 AGENTS.md + assent 工作指示 + 它自己的任務檔。
2. **保持靈活,少即是多。** 零第三方依賴(只用 Python 標準庫);
   狀態就是任務檔本身,沒有資料庫、沒有隱藏狀態。
3. **AI 能處理的全部自動處理,人類只做審查與裁決。**
   人不手改檔案;驗收不過就下指令叫 AI 改。
4. **執行 AI 燒過 tokens 的產出絕不丟棄。**
   額度中斷收 wip 檢查點續作;驗收失敗不還原、在現有成果上重試;
   重試用盡連同成果 commit 進 BLOCKED 檢查點交人類裁決。

## 運作原理

```text
              ┌────────────────────────────────────────────┐
              │              主迴圈(零 token)               │
 .assent/     │  1. 掃工作資料夾,選任務:WIP 續作優先,        │
 工作資料夾 ──▶     否則第一個「TODO 且前置皆 DONE/SKIP」      │
 (tNNN_name   │  2. 讀該任務的檔位/effort,開 headless session │──▶ 執行 AI
  .toml)      │                                                   │
              │  3. session 結束後客觀驗收:                  │◀── 更新任務檔
              │     狀態 → 結構比對(防竄改)→ scope → verify │     + 同名 rNNN_name.toml 日誌
              │  4a. 通過 → auto(工作資料夾/tNNN) 檢查點     │
              │      → 回到 1                                  │
              │  4b. 失敗 → 保留成果帶原因重試 → 仍失敗則      │
              │      標 BLOCKED 連成果一起 commit → 回到 1    │
              │  4c. 額度耗盡 → wip 檢查點 → 倒數等重置        │
              │      → 帶「接續」提示續作                     │
              └────────────────────────────────────────────┘
```

- **任務檔即狀態**:每個任務一個 `tNNN_name.toml` 檔(狀態、依賴、檔位、
  scope、verify、驗收條件),日誌則是同主幹的 `rNNN_name.toml`
  (append-only、預設不讀)。妥善處理的中斷會留下 WIP,重新 `assent run` 即可
  續作;若程序或主機突然中斷而留下未提交變更,調度器會拒絕猜測,等候人工檢查
  並建立檢查點。
- **格式契約**:`.assent/format.md`(`assent init` 會放進專案),
  規劃 AI 讀它產生任務檔,調度器解析器與它逐字對齊。
- **session 過程即時可見**:AI 說的話(`AI|`)、用的工具(`Tool|`)、token
  用量(`--|`)同步印在終端,並留存於 `.assent/<工作資料夾>/_assent.log`。

## 安裝

Python 3.11+、git、已登入的 Claude Code CLI(`claude`)或 Codex CLI(`codex`)。

```
cd <assent 專案目錄>
pip install -e .
```

驗證:任何目錄執行 `assent --help`。零第三方依賴,不會下載任何外部套件。

## 快速開始

```
# 0. cd 到目標專案根目錄(需為 git repo)

# 1. 生成 .assent 骨架與 AGENTS.md
#    (既有 AGENTS.md 只補一行 assent 橋接,其他內容不覆蓋)
assent init

# 2. 填 AGENTS.md 的專案描述/硬限制、.assent/verify.py 的實際檢查命令
#    AGENTS.md 可自行決定是否提交;整個 .assent/ 留在主工作樹,不提交

# 3. 開 AI 會議產出任務檔(這一步是互動 session,見下方「使用循環」)

# 4. 驗證計畫與環境(零 token;通過 = 會議可以散會)
assent check

# 5. 試跑一個任務,確認無誤後全自動跑到底(可過夜)
assent run --once
assent run

# 也可以用位置參數指定工作資料夾(與 --config 正交)
assent run <資料夾>

# 依資料夾 after 依賴順序執行全部未完成資料夾,最多同時跑 2 個
assent run --all --jobs 2

# 6. 預設 [verification] receipt_refresh = "manual" 下,run 收尾不會留下
#    receipt,直接 accept 會被拒絕並提示先 verify。離席時顯式刷新(零
#    token):一次驗證多個已完成資料夾,成本等同驗證一個
assent verify --batch
# 或只刷新單一資料夾的 receipt
assent verify <FOLDER>
# 想要 run 收尾就自動刷新 receipt,改設 receipt_refresh = "auto"

# 7. 隨時查看(另開終端、零 token),再進行審查
assent status
assent report
# 人類審查後,依資料夾依賴順序接受全部已完成資料夾
assent accept --all
# 或只接受一個已完成的資料夾併入目前目標分支
assent accept <資料夾>
# 接受後,用一般 Git(或自行委任的 AI 流程)獨立同步
git push
# 接受與所需同步完成後,移除多餘成果
assent clean <資料夾>
# 不再需要時,把已接受資料夾的計畫封存進 _archive/
assent archive --all

# 驗收會議要求單一任務重做(預設保留程式碼;不自動 run)
assent rework <FOLDER> <TASK> [--cascade] [--reason TEXT]

# 驗收會議裁決駁回整個資料夾的實作時(封存、強刪、任務改回 TODO)
assent reject <FOLDER>
```

跑完後人類驗收:

```
git log --oneline <資料夾名>/<run-id>   # 一任務一 commit,逐一查看
git diff main...<資料夾名>/<run-id>     # 或看整體差異
# 人類做決定;Assent 執行受保護的本地整合
assent accept <資料夾>
# 再自行選擇一般 Git 同步,例如 `git push`,或委任你自己的 AI 流程
# 單一任務不接受 → assent rework <資料夾> <任務>
# 有已開始的下游 → 加 --cascade;確認要反向程式碼 → 加 --revert-code
# 整個資料夾的實作都不要 → assent reject <資料夾>
```

`rework` 成功後會立即更新 `_report.md`,但不印整份報告、也不啟動 AI;人確認
TODO 與連動範圍正確後,再明示執行 `assent run <FOLDER>`。

`DONE` 是執行 AI 的完成主張,不是人類批准。人類必須先讀 `_report.md`、
檢查報告與 checkpoint 存證,再呼叫 `assent accept <FOLDER>` 做出接受決定。
receipt 是 scheduler 的完整驗證證據;呼叫 `accept` 才是人類批准。
`assent verify <FOLDER>` 是零 token、可離席刷新 missing/stale receipt 的命令,
不改 target 也不開 AI session。
`FOLDER` 必填:`accept` 沒有 `--all`、`--push` 或 `push` subcommand,不會連線
遠端 hosting、不會 pull、rebase,也不會刪除 source worktree。成功本地接受後,
用一般 Git 命令,或你自行操作的 AI 流程獨立同步;這不是 Assent 的內建功能。
只有在接受與所需同步完成、且清理證明成立後,才執行 `assent clean <FOLDER>`。
具有未接受 dependent 時,不應先 clean upstream source branch;source worktree 或
branch 消失後再次 accept 會直接拒絕,不把 passive merge metadata 當狀態資料庫。

接受要求主工作樹目前位於 target branch,且 source 已完成、乾淨、唯一可辨識並通過
依賴安全檢查。Assent 會驗證 source 與整合後結果,以可稽核的 `--no-ff` merge
留下證據,成功重跑具冪等性。完成、lock、乾淨、branch、依賴或歧義證明不足就拒絕;
verify failure 或 conflict 不會推進 target。Assent 不自動解衝突、pull、rebase、
force push,也不宣稱 integration lock 能阻止外部 Git 寫入;accept 期間不要在同一
主工作樹執行會寫入的 Git 命令。lock 只保證 Assent accept 彼此串行。

### 有界的樂觀堆疊

下游資料夾的 `_folder.toml` 設為 `after = ["A"]` 後,表示 A 是排程前提。
`base = "A"` 宣告下游檔案建立在 A 的 commit 上,其 worktree 會是該 commit
的完整簽出;非 `base` 的 `after` upstream 只保證順序,不提供檔案內容或同檔
衝突保護。沒有宣告 `base` 時,下游從目前的整合目標建立;`after` 成員的數量
與接受狀態不影響基底選擇,多個未接受 upstream 也不會因此造成基底歧義或拒絕。

例如:`run A` -> `run B` 堆疊在 A 上 -> combined verification -> 人類
`accept A` -> 人類 `accept B`。B 可在 A 尚未接受時建立 receipt;A 進入 target
後,若 source tip、integration tree、verifier digest 仍相同即可重用,`accept`
不重跑完整 suite。若 A 前進,B 會 stale 但成果保留;可 rework/reject B,或開
新資料夾重新規劃,Assent 不重寫 stack history。

A 與 B 修改同一檔案也遵守同一規則。Git 能自動合併時由 exact-tree verification
證明結果;Git conflict 則 target 不變,交由人工作裁決。Assent 不自動 rebase、
解衝突或 push。

清理採 upstream-first 且以證據為準。直接 dependent 尚未完成、接受、乾淨、存在,
或無法證明已整合時都保留 source;`assent clean A` 會拒絕並說明原因。全部
dependent 都接受且可證明整合並乾淨後,再用 `assent clean` 先清 upstream、後清
dependent;不要手動刪 worktree 或 branch。

### `verify --batch` 的互動式衝突略過

沒有 conflict 的 `assent verify --batch` 維持完全無人值守。建置批次候選時
才會發現 source conflict,而這從不算作驗證失敗:每個排入佇列的資料夾仍會
被嘗試合併,因此一個資料夾發生 conflict 不會阻止之後、彼此獨立的資料夾
也被嘗試。一旦有一個以上資料夾 conflict,`verify --batch` 會回報每個
conflict 資料夾及其衝突路徑,並回報每個排在某個 conflict 資料夾 `after`
之後而被一併排除的資料夾(遞移計算,而非在缺少其宣告 upstream 的情況下
仍驗證它),接著只問一次 `[Y/n]`:是否略過整組被排除者,改為只驗證其餘
仍可合併的資料夾。

- **是**(空白回答或 `y`/`yes`):對較小子集執行一次完整驗證,批次 receipt
  只記錄這些已驗證的資料夾;每個被略過的資料夾完全不會被嘗試。
- **否、無法辨識的回答,或 EOF**(無人可回答的非互動呼叫者):
  `verify --batch` 會在執行完整驗證前停止,且不寫入 receipt,與其他任何
  拒絕情形相同。
- **整批全部 conflict**:已沒有獨立可提供的資料夾,批次會直接拒絕,
  不會提問。

略過不是解決、rebase、接受或刪除任何東西——target 與每個 source
資料夾,不論被略過或已合併,都維持原樣不變。conflict 資料夾自身的
source 仍需經過明確的人工 `assent rework` 或 `assent reject`,才能
重新加入未來的批次。

`assent accept --all` 只在一次原子 ref 更新中發佈 receipt 涵蓋的確切
資料夾,並在同一次執行內回報 receipt 未涵蓋的其餘已完成資料夾(可能是
太晚完成而沒被納入批次,也可能是建置批次時就沒包含它),既不驗證也不
接受它們:不會有第二次提問,也不會有同一次執行內轉回逐資料夾路徑的
fallback。之後可再次執行 `assent verify --batch`,對剩餘部分建置下一個
批次。

`assent archive --all` 只封存獨立符合資格的資料夾(已完成,且其 source
已經不存在,或 `clean` 本身的機械證明可以移除它);對於任何 source 仍被
未接受 dependent 需要的資料夾,它會保留該證據並跳過封存,與 `clean` 所
強制的 upstream-first 規則相同。

### 用 `assent reconcile` 解決單一資料夾的衝突

`assent verify --batch` 只能略過發生 conflict 的資料夾,無法解決它。
`assent reconcile FOLDER` 是對應的單一資料夾指令:人類只編輯衝突檔案,
Assent 則負責這些編輯周圍的每一個 Git 操作。完整順序是:

```text
assent reconcile parallel01              # 在 worktree 中準備好衝突
                                         # (人工編輯被回報的檔案)
assent reconcile --continue parallel01   # 加入索引、提交、推進 source
assent verify parallel01                 # 選用、明確、昂貴
assent accept parallel01                 # 明確的人類批准
```

**start** 要求資料夾已完成(每個任務為 `DONE` 或 `SKIP`)、主 worktree 乾淨,
且 source 有自己的分支與 worktree。它會擷取整合 target 目前的 tip,在主
worktree 旁建立 worktree `<project>.reconcile/<FOLDER>`,置於從 source tip
起始的臨時分支 `assent-reconcile/<FOLDER>`,並把擷取到的 target tip 合併
進來但不提交。由於這個 merge 以 source 為先建立,其第一 parent 就是原本的
source,所以之後 source 分支可以被 fast-forward 到它上面——source 從不被
改寫,而**整合 target 從不被改變**。過程中主 worktree 與該資料夾自己的
source worktree 都維持乾淨。若兩邊其實可以自動合併,start 會說明、撤銷該
merge、移除它建立的資源,並讓 source 維持原狀。若 source 已包含於 target,
就沒有需要 reconcile 的東西。

**你只編輯,不執行任何 Git 指令。** start 會印出 worktree 路徑、分支、
雙方 tip 與每個衝突檔案;只在那個 worktree 內解決這些檔案。

**`--continue`** 只把 Git 仍回報為 unmerged 的路徑加入索引,並驗證結果
(沒有殘留 unmerged 路徑、依 `git diff --cached --check` 沒有殘留衝突標記
或空白錯誤、也沒有衝突解決範圍以外的編輯),接著建立 merge commit、在
source 自己的 worktree 內 fast-forward source 分支,然後移除臨時 worktree
與分支。刪除前它會重新證明每個受管資源的身分——是本 repository 的
worktree、附著於受管分支、`HEAD` 為已證明的 commit、且乾淨——因此絕不會
擴大刪除範圍。由於 source 確實前進了,`--continue` 會刪除依舊 source 身分
寫成的 receipt:資料夾 receipt,以及當批次 receipt 記錄的任一 source 身分
已不再成立時的批次 receipt(批次 receipt 本質上是全有全無)。連讀都讀不了
的批次 receipt 會原地保留供檢查,而不是被抹除。

**Reconcile 不是證據,也不是批准。** `--continue` 不執行聚焦任務測試,也
不執行完整驗證,更不寫 receipt。證明解決後的 source 是之後由人明確啟動的
`assent verify FOLDER`——那個昂貴步驟,對當下的 target 執行;批准則是之後的
`assent accept FOLDER`,它仍要求一份 fresh、可重現的 `PASSED` 完整驗證
receipt。若 target 在 start 之後前進,擷取到的 merge 不會被改寫;drift 會被
回報,而之後那次 `verify` 才是權威。

**中斷與拒絕**都可復原,且從不具破壞性。沒有狀態檔:之後的執行會讀取
worktree、臨時分支、`HEAD`、`MERGE_HEAD` 與 merge parents,判斷前一次執行
走到哪裡,因此 `--continue` 能接續某次中斷執行已提交的 merge,或補完只差
的 fast-forward。一旦有不相符之處——source 分支獨立移動、受管路徑不是
worktree 或位於別的分支、已加入索引的解決有驗證問題——該次執行會拒絕並保留
worktree、分支與每一筆編輯;不提交任何東西,也不刪除任何東西。

**`--abort`** 放棄這次嘗試:它只移除受管的 worktree 與臨時分支,且只在證明
各自確實是它所管理的資源之後才移除;當 worktree 仍有未提交變更時它會拒絕,
而不是丟棄成果。source 與整合 target 都維持不變。

Reconcile 刻意不是整合引擎。它只處理單一資料夾對當前整合 target;它從不替
你解決檔案內容、從不合併投機性的同儕資料夾、從不執行 AI adapter,也從不改
任務狀態。只在建置批次 candidate 時、於兩個未被接受的 source 之間出現的
conflict 不屬於本指令——那組仍走 `verify --batch` 的略過決定,再由
`assent rework` 或 `assent reject` 處理。

## 平行執行

可在 N 個終端各自指定不同的工作資料夾執行,例如 `assent run parallel01`、
`assent run parallel02`;也可用 `assent run --all --jobs N` 由調度器依資料夾
依賴安排平行執行。`run --all` 維持單一前景終端,並把各子行程訊息即時顯示為
`[工作資料夾] 訊息`;平行執行時可由前綴辨識每一列的來源。

家長終端會顯示上述帶前綴訊息,根層 `.assent/_assent.log` 只保存啟動標頭與
工作資料夾啟動、完成或失敗等調度摘要。各工作資料夾自己的 `_assent.log`
則由子行程保存完整原始輸出,不含家長前綴且不會重複寫入。各資料夾內的任務
與日誌分別使用 `tNNN_name.toml`、`rNNN_name.toml`。每個工作資料夾都有自己的
`assent.lock`,同一資料夾
同時只允許一個 run;Git 永遠啟用,每個資料夾一律使用
`<專案名>.worktrees/<資料夾>/` 的獨立 worktree,這是安全平行處理的基礎。

版控邊界刻意簡單:`AGENTS.md` 是專案規則;有進 Git 時使用 worktree 內的
分支版本,未進 Git 時由提示詞提供主樹絕對路徑。整個 `.assent/` 是 assent
管理面,由 `.gitignore` 排除並只留在主工作樹。調度器同樣以絕對路徑提供
instructions、t/r 與預設驗收腳本;驗收腳本雖從主樹載入,執行 cwd 仍是
worktree。任何 `.assent/` 檔案已進 Git 時,調度器會在開 session 前
fail-closed 拒絕執行,避免 worktree 出現第二份真本。

AI 會議在主樹進行。從主樹可直接用 `git worktree list`、`git log <分支>` 與
`git diff main...<分支>` 審查各 worktree 的 checkpoint,不必進入其目錄。

平行執行的固有代價是額度共享,以及各分支 merge 回主線由人負責。

## 使用循環(三幕)

**第 1 幕:規劃會議**(互動 session)

```text
開始規劃。請讀 AGENTS.md、.assent/instructions.md 與 .assent/format.md,
然後跟我討論以下目標,把共識逐步寫成 .assent/<工作資料夾>/ 的任務檔:
<你的目標>
```

會議中每達成一項共識就落成任務檔;散會前跑 `assent check`,不過就是還沒開完。

**第 2 幕:無人值守執行**:`assent run`,去睡覺。每個 task session 只跑該任務的
focused verify;資料夾完成後是否還在 AI session 外建立臨時 integration candidate
並執行完整 `.assent/verify.py`,取決於 `assent.toml`「[verification]」的
`receipt_refresh`:預設 `"manual"` 把這一步留給之後顯式的
`assent verify [--batch]`;`"auto"` 則在資料夾全部任務完成時的 run 收尾就執行。

`assent verify <FOLDER>` 是零 token、可離席執行的完整驗證 receipt refresh,不改
target、不開 AI session;`assent verify --batch` 則對每個已完成、尚未整合的資料夾
一次做同樣的事。兩者的報告都會顯示 `PASSED`/`FAILED` 與 `fresh`/`stale`,stale
時可在無人值守階段重新 refresh;沒有新鮮的 `PASSED` receipt,`assent accept` 會
拒絕並提示先 verify。

打包的 `.assent/verify.py` 同時檢查 candidate working tree 與 candidate `HEAD` 相對
第一父提交的 committed delta,因此能抓到單純 `git diff --check` 看不到的已提交尾端
空白。`assent init` 不會覆寫既有 verifier;要同步時請人工把 template 的檢查移植到
既有檔案。verifier digest 改變會使舊 receipt stale,應在無人值守驗證時執行
`assent verify <FOLDER>` 後再請人接受。

**自己重跑驗證**:任務的 focused `verify` 命令記錄在該任務
`tNNN_name.e.toml` 的 `verify` 欄位,可在該工作資料夾的隔離 worktree
`<專案>.worktrees/<資料夾>/` 內直接執行同一命令。`assent run` 的執行輸出
會把同一段文字印成 `verify: <command>` 這一列,緊接著印出 `verify passed
(exit 0)` 或 `verify failed (exit N)`,因此這一列印出的就是可手動重跑的
原文命令。完整階段由 `assent verify <FOLDER>` 在臨時整合候選中執行；候選
的路徑形狀是 `<project>.integration/target-<uuid>`,它和
`<project>.worktrees/` 同層,使用分支
`assent-integration/<folder>/<uuid>`。這是合併後、由完整
`.assent/verify.py` 驗證且由 receipt 認證的樹;它在整套測試執行期間都存在,
測試結束後移除。要手動重現或觀看逐測試輸出,須在候選仍存在時以它作為
cwd,並從主工作樹執行 verifier,例如 `python <main-worktree>/.assent/verify.py`;
不要把 source worktree 當成整合候選。清理在 `finally` 中進行,所以正常結束、
Python 例外與 Ctrl-C 都會清除;只有硬殺(例如 `taskkill /F`)或斷電可能留下
殘留。assent 沒有自動回收殘留的機制;請自行執行
`git worktree remove --force <path>` 與 `git branch -D <branch>` 清除。

**平行執行測試**:打包的 `.assent/verify.py` template 提供
`run_unittest_parallel()`,預設以註解停用;啟用後會把 `tests/test_*.py`
底下每個模組各自丟進獨立 subprocess 平行執行,而非單一行程依序跑完整套件,
因此總耗時約等於最慢那個模組,而非全部加總。之所以用行程隔離而非執行緒,
是因為 unittest 模組會改動行程層級的全域狀態(`os.chdir`、`os.environ`),
共用同一個直譯器會讓模組間互相汙染。並發數預設是 `min(模組數, CPU 數)`,
可用 `ASSENT_VERIFY_JOBS` 覆寫。修改 `.assent/verify.py` 啟用它會改變
verifier digest,使既有 receipt 過期一次;重跑 `assent verify <FOLDER>`
即可換發。

worktree 是變更隔離、衝突管理、稽核與復原邊界,不是安全 sandbox。`danger-full-access`
或 `bypassPermissions` 下,AI 仍可使用其 OS 身分可存取的 network、credential、外部
Git 寫入者與 worktree 外檔案。只有在可信任的專案與帳號環境才應啟用無人值守執行;
Assent 不提供 container/VM sandbox,也不攔截這些外部效果。

**第 3 幕:驗收小會議**(互動 session)

先自己讀 `_report.md`(它就是議程表:進度、BLOCKED 卡點、檢查點 hash),
再對要裁決的任務開 session:

```text
請讀 .assent/<資料夾>/t003_xxx.toml、r003_xxx.toml 與
auto(<資料夾>/t003) 對應 commit <hash> 的 diff,
說明卡點並提出修正方案。
```

裁決落實 = AI 改任務檔(status 改回 TODO、補說明、加任務、標 SKIP),
`assent check` 過了回第 2 幕。`DONE` 仍是執行主張;receipt 是 scheduler 證據,
不是批准。人類讀報告後呼叫 `assent accept <FOLDER>`;它快速重建 candidate,
比對 source tip、integration tree、verifier digest,只有完全重現 fresh `PASSED`
receipt 才發布,不執行完整測試。missing/stale receipt 要先 `assent verify <FOLDER>`。
沒有 task `review` 欄位。遠端同步仍是獨立的一般 Git 決定,最後可執行
`assent clean <FOLDER>`。循環到需要重做的任務
都完成並由人類接受。
新一輪計畫 = 開新工作資料夾即可;舊資料夾可由 `_folder.toml` 的 `after`
繼續作為前置參與依賴判定。資料夾完成由任務檔推導,全部任務為 DONE/SKIP
才算完成。

## 指令參考

`run`、`status`、`check`、`report` 的完整形式都是
`assent <指令> [選項] [FOLDER]`。`FOLDER` 可明示工作資料夾;省略時 `run`
會依任務現況與 `_folder.toml` 的 `after` 前置推導唯一可執行資料夾,有歧義
就拒絕。`status`、`check`、`report` 省略時作用於全部資料夾。`--config PATH`
選擇設定檔,預設為 `.assent/assent.toml`;設定檔不再維護工作資料夾指標。
兩者彼此正交,可以只用其中一個,也可以同時使用,例如
`assent status --config configs/night.toml parallel01`。

`assent verify <FOLDER>` 是單一資料夾的零 token receipt refresh,不改 target、不開 AI。
`assent accept <FOLDER>` 是人類批准,只快速重建 candidate 並比對 fresh `PASSED`
receipt,不執行完整測試。receipt 是可刪除重建的 derived evidence;內容變更會 stale,
target commit 改變但重建後 integration tree 相同仍可接受。source worktree/branch
消失就拒絕。它不連線 remote、沒有 `--all`/`--push`、不 pull、rebase、force push、
自動解衝突或刪 source;integration lock 不能阻止外部 Git 寫入。接受期間不要在同一
主工作樹執行寫入 Git 命令。成功重跑具冪等性。

`assent clean [FOLDER]` 只刪除已完全併入且乾淨的 worktree 與分支;證明不了就跳過,
不碰 `.assent/`,也沒有強制選項,且與 `git clean` 無關。

`assent reject <FOLDER>` 是人工裁決的明示駁回動作,與常規 clean 分流:先把
未提交變更封存為 wip commit,印出各分支完整 tip hash 存證後強制刪除該
資料夾的 worktree 與同前綴分支(僅 gc 期限內可用 hash 救回),再把 DONE/
WIP/BLOCKED 任務改回 TODO 並在 r 檔留下含完整 Git 存證的 `rejected`
記錄(SKIP 不推翻)。`FOLDER` 必填,不可作用於全部資料夾;run 進行中拒絕執行。

`assent rework <FOLDER> <TASK>` 是單一任務的非破壞性重開。預設保留所有程式碼,
只把目標狀態改回 TODO;有已開始或已完成的下游時必須明示 `--cascade` 才連動。
`--reason TEXT` 保存裁決理由。`--revert-code` 採 fail-closed:只有目標範圍的
checkpoints 構成目前分支的連續尾段才會建立新的反向 commit,絕不改寫 Git 歷史。
命令成功後重生報告,但不自動執行 `run`;預檢、狀態或報告更新失敗皆回傳失敗。

兩項舊設定已廢除:工作資料夾不再由設定檔中的手工指標維護,Git 也沒有停用
開關或無 Git 降級模式;工作資料夾由命令列明示或依任務事實推導,Git 永遠啟用。

| 指令與代表性命令 | 選項與作用 | token 消耗 |
|---|---|---|
| `assent run [FOLDER]`<br>`assent run parallel01` | 執行工作資料夾,直到任務全為 DONE/BLOCKED/SKIP。省略 `FOLDER` 時推導唯一可執行資料夾;`--once` 只執行下一個任務後停止;`--task ID` 指定單一任務且仍檢查前置,例如 `assent run --task t003 parallel01`。 | 僅執行 AI session 時消耗;`--once` 或 `--task` 最多執行單一任務 |
| `assent run --all`<br>`assent run --all --jobs 2` | 依 `_folder.toml` 的資料夾依賴順序執行全部未完成資料夾;`--jobs N` 限制同時執行的資料夾數(預設 1),家長終端以 `[工作資料夾] 訊息` 即時標示各子行程輸出。不可與 `FOLDER`、`--once` 或 `--task` 並用。 | 僅執行 AI session 時消耗 |
| `assent status [FOLDER]`<br>`assent status parallel01` | 顯示進度統計、下一個任務、分支與最後檢查點。接受 `--config PATH`。 | **零** |
| `assent check [FOLDER]`<br>`assent check --config .assent/assent.toml parallel01` | 驗證任務檔格式、依賴無循環、設定與環境,是規劃會議的散會條件。接受 `--config PATH`。 | **零** |
| `assent report [FOLDER]`<br>`assent report parallel01` | 生成並顯示工作資料夾內的人讀報告 `_report.md`。接受 `--config PATH`。 | **零** |
| `assent verify <FOLDER>`<br>`assent verify parallel01` | 對單一資料夾的臨時 integration candidate 執行一次完整 verifier 並刷新 derived receipt;不改 target、不開 AI session。報告顯示 `PASSED`/`FAILED`、`fresh`/`stale`;沒有 `--all`。 | **零** |
| `assent accept <FOLDER>`<br>`assent accept parallel01` | 人類批准單一已完成資料夾。快速重建 candidate,只在完全比對 fresh `PASSED` receipt 時發布;不執行完整驗證。missing/stale receipt 要先 `assent verify`;沒有 `--all`、`--push`、remote、pull、rebase、force、自動解衝突或刪 source。 | **零** |
| `assent reconcile <FOLDER>`<br>`assent reconcile --continue parallel01` | 在隔離 worktree `<project>.reconcile/<FOLDER>` 內準備單一已完成資料夾的 source 對 target conflict,讓人工編輯被回報的檔案;`--continue` 把該解決加入索引並驗證、提交 merge、fast-forward source 分支;`--abort` 只丟棄已證明的受管 worktree 與分支。從不改 target、不解決內容、不執行聚焦或完整驗證、不寫 receipt、也不接受。`FOLDER` 必填;沒有 `--all`。 | **零** |
| `assent clean [FOLDER]`<br>`assent clean parallel01` | 只清理已完全併入且乾淨的 worktree 與同資料夾前綴分支;任何證明不足就跳過,不碰 `.assent/`,且沒有強制選項。省略 `FOLDER` 時作用於全部工作資料夾。 | **零** |
| `assent reject <FOLDER>`<br>`assent reject parallel01` | 人工裁決駁回:封存未提交變更後強制刪除該資料夾的 worktree 與同前綴分支(刪除前以完整 tip hash 存證),並把 DONE/WIP/BLOCKED 任務改回 TODO、r 檔保存 Git 存證。`FOLDER` 必填;run 進行中拒絕。 | **零** |
| `assent rework <FOLDER> <TASK>`<br>`assent rework parallel01 t003 --cascade --reason "驗收不符"` | 非破壞性重開單一任務;預設保留程式碼,`--cascade` 明示連動下游。`--revert-code` 僅在 checkpoints 是連續分支尾段時建立新反向 commit。成功後更新報告,不自動執行 run。接受 `--config PATH`。 | **零** |
| `assent init`<br>`assent init --path C:\work\my-project` | 在目標專案生成 `.assent` 骨架與 `AGENTS.md`;`--path DIR` 預設為目前目錄。它不接受 `FOLDER` 或 `--config`。 | **零** |
| `assent doctor`<br>`assent doctor` | 診斷機器環境(Python 版本、git、adapter CLIs、temp 目錄可寫性);不需要 `FOLDER` 或 `--config`,也不需要既有的 `.assent/` 專案就能執行。 | **零** |

各子命令的 `-h`/`--help` 會顯示該層實際語法;頂層沒有可套用到所有子命令的
`--config` 等全域選項。

## Adapter、模型檔位與 effort 等級

Assent 透過可插式 adapter 支援不同的 AI CLI 工具。每份任務檔用抽象**檔位**
(`prime`、`core` 或 `lite`) 替代具體模型名稱;adapter 的組態表會把該檔位轉成
這次執行的實際 CLI 模型。同樣地,任務可要求抽象 **effort** 等級(`heavy`、
`normal` 或 `slight`),adapter 會轉成廠商的具體 CLI 值(如果支援的話)。

### 支援的 adapter

**Claude** (`adapter.name = "claude"`)

```toml
[adapter.claude]
command = "claude"
extra_args = ["--permission-mode", "bypassPermissions"]

[adapter.claude.models]
prime = "fable"      # Fable 5 — 最快檔位
core  = "opus"       # Opus 4.8 — 平衡檔位
lite  = "sonnet"     # Sonnet 5 — 高效檔位
```

**Codex** (`adapter.name = "codex"`)

```toml
[adapter.codex]
command = "codex"
extra_args = ["--sandbox", "danger-full-access"]

[adapter.codex.models]
prime = "gpt-5.6-sol"    # 最大模型
core  = "gpt-5.6-terra"  # 平衡模型
lite  = "gpt-5.6-luna"   # 高效模型
```

**Antigravity** (`adapter.name = "antigravity"`)

Antigravity adapter 透過 `agy`(Antigravity CLI) 執行 Google 的 Gemini 模型,
是一份自由安裝的本地 CLI,每台機器需互動登入一次。本 adapter 用 print mode
(純文字輸出、無 JSON 事件) 通訊,在開啟 session 前有 model/effort 組合的
preflight 驗證。

```toml
[adapter.antigravity]
command = "agy"
extra_args = ["--dangerously-skip-permissions"]

[adapter.antigravity.models]
prime = "gemini-3.1-pro"   # Gemini 3.1 Pro — 最高品質
core  = "gemini-3.6-flash" # Gemini 3.6 Flash — 平衡(新)
lite  = "gemini-3.5-flash" # Gemini 3.5 Flash — 高效

# Antigravity 各檔位的 effort 翻譯。下面說明每個。
[adapter.antigravity.default_effort]
prime = "heavy"
core  = "heavy"
lite  = "heavy"

# Gemini 3.1 Pro 只支援 low 和 high effort,沒有 medium。
# 為了品質,normal 翻譯上升為 high(絕不無聲降級)。
[adapter.antigravity.efforts.prime]
normal = "high"

# Gemini 3.5 Flash 只支援 low 和 medium,沒有 high。Lite 檔位的 heavy
# 翻譯為 medium(這個家族的上限),在組態表裡可見,可覆寫。
[adapter.antigravity.efforts.lite]
heavy = "medium"
```

### 模型/effort 矩陣

任務檔指定抽象檔位和可選的 effort。Adapter 把它轉成具體 CLI 呼叫。
完整 9 宮格如下,顯示每份任務檔 (檔位, effort) 配對在各 adapter 裡的轉譯:

#### Claude adapter

| Effort | prime<br/>(Fable) | core<br/>(Opus) | lite<br/>(Sonnet) |
|--------|---|---|---|
| slight | `--model fable --effort low` | `--model opus --effort low` | `--model sonnet --effort low` |
| normal | `--model fable --effort medium` | `--model opus --effort medium` | `--model sonnet --effort medium` |
| heavy | `--model fable --effort high` | `--model opus --effort high` | `--model sonnet --effort high` |

#### Codex adapter

| Effort | prime<br/>(gpt-5.6-sol) | core<br/>(gpt-5.6-terra) | lite<br/>(gpt-5.6-luna) |
|--------|---|---|---|
| slight | `--model gpt-5.6-sol --effort low` | `--model gpt-5.6-terra --effort low` | `--model gpt-5.6-luna --effort low` |
| normal | `--model gpt-5.6-sol --effort medium` | `--model gpt-5.6-terra --effort medium` | `--model gpt-5.6-luna --effort medium` |
| heavy | `--model gpt-5.6-sol --effort high` | `--model gpt-5.6-terra --effort high` | `--model gpt-5.6-luna --effort high` |

#### Antigravity adapter (1.1.5+)

| Effort | prime<br/>(3.1 Pro) | core<br/>(3.6 Flash) | lite<br/>(3.5 Flash) |
|--------|---|---|---|
| slight | `--model gemini-3.1-pro --effort low` | `--model gemini-3.6-flash --effort low` | `--model gemini-3.5-flash --effort low` |
| normal | `--model gemini-3.1-pro --effort high` | `--model gemini-3.6-flash --effort medium` | `--model gemini-3.5-flash --effort medium` |
| heavy | `--model gemini-3.1-pro --effort high` | `--model gemini-3.6-flash --effort high` | `--model gemini-3.5-flash --effort medium` |

說明:
- **Antigravity prime/normal**: Gemini 3.1 Pro 不支援 `medium`,故 assent 改選
  `high`(品質優先對應)。這不是無聲回落—組態表裡清楚可見、可審計。
- **Antigravity lite/heavy**: Gemini 3.5 Flash 沒有 `high` effort 等級,故 `heavy`
  轉成 `medium`(該家族的最大可用)。
- **Antigravity 1.1.5 最低版**: 這是支援 `--effort`、穩定 model slug 及無人值班
  執行所需 headless 修正的版本。舊版在開啟 session 前會被拒。

### 使用 Antigravity adapter

**首次設定**

1. 在你的機器上安裝 `agy`(Antigravity CLI)(如無則裝)。
2. 執行 `agy auth login` 在本機進行一次互動登入。
3. 用 `agy --version` 驗證版本(必須 1.1.5 或更新)與 `agy models`(顯可用模型)。

Assent **不會**修改 `~/.gemini/antigravity-cli/settings.json`、執行登入瀏覽器、
或和認證互動。你的登入認證與 workspace 信任完全由你管。

**使用 Antigravity 的任務檔範例**

```toml
title = "用高品質推理分析程式碼"
model = "prime"
effort = "heavy"
status = "TODO"
scope = ["src/", "tests/"]
verify = "python -m pytest"

goal = "用 Gemini 3.1 Pro(最高品質)審查程式碼庫。"
```

執行 `assent run` 時,會:
1. 驗證 Antigravity 1.1.5+ 已安裝且能觸及 `gemini-3.1-pro --effort high`。
2. 用 `agy --print --model gemini-3.1-pro --effort high --mode accept-edits ...`
   開啟無標題 session。
3. 執行驗證命令並紀錄結果。

**在既有專案中切換 adapter**

改 `[adapter]` name 只需一行。既有任務檔無需改動;它們仍用 `model = "prime"` 和
`effort = "heavy"`,新 adapter 的組態表照樣轉譯。切換後,下一次 `assent check`
會在任何 session 啟動前驗證新 adapter。

### 設定模型與 effort 翻譯

`.assent/assent.toml` 的組態範本顯示如何自訂檔位到模型的對應與抽象到 CLI
effort 的翻譯。查找順序永遠是:

1. 任務檔明示的 `effort` 註記(如有)
2. Adapter 的 `default_effort` 為此檔位(如有)
3. 無 effort flag(某些 adapter/檔位可能不支援)

與 effort 翻譯:

1. 檔位特定區段: `[adapter.<name>.efforts.<tier>]`
2. 平面區段: `[adapter.<name>.efforts]`
3. 內建基準表:`heavy` → `high`、`normal` → `medium`、`slight` → `low`
   (較高優先層缺少某個鍵時,每個抽象鍵都各自逐層退回平面表,再退回基準表)。

範例:如果你的 Antigravity 設定有更新的 3.1 Pro 支援 medium,可移除品質優先對應:

```toml
# 移除這行:
# [adapter.antigravity.efforts.prime]
# normal = "high"

# 或設為實際值:
[adapter.antigravity.efforts.prime]
normal = "medium"
```

### 設定 Antigravity print timeout

Antigravity 的 `--print-timeout` 獨立於 Assent 的 watchdog timeout。Print
timeout 限 CLI 等待單一 print 呼叫完成的時間;watchdog 限 Assent 等待任何
輸出(殺掉 session 前)的時間。

在 `.assent/assent.toml`:

```toml
[adapter.antigravity]
print_timeout_minutes = 120  # AGY 最多等 2 小時得答案
```

別設低於你最長任務的預期時間;`assent check` 會驗證 print timeout 是正數。

### Antigravity 組態故障排除

**問題: `preflight failed: invalid model selection`**

Antigravity 在 preflight 拒了 model/effort 組合。檢查:

```bash
agy models                         # 看有什麼模型
agy --print --model <MODEL> ...    # 試試你的 model/effort 選擇
```

常見原因:
- **未對應的 model tier**: 加到 `[adapter.antigravity.models]`。
- **不支援的 effort**: 模型不支援那個 effort 等級。例如 Gemini 3.1 Pro
  不支援 `medium`。在 `[adapter.antigravity.efforts.prime]` 裡修正對應。

**問題: `authentication required` 或 `permission denied`**

得在本機登入過一次:

```bash
agy auth login          # 開瀏覽器 Google 登入
```

如果你無人值班執行 `assent run`(例如晚上),登入必須在執行前完成。Assent
無法開瀏覽器、幫你登入或察覺你是否離開;它只用你既有登入認證。

**問題: `command not found: agy`**

Antigravity CLI 未裝或不在 PATH。見 [Antigravity CLI 安裝文件]
(https://google-antigravity.github.io/install) 並用 `agy --version` 確認。

**問題: session 中途額度耗盡**

Antigravity 達配額限時,`assent run` 紀錄 `WIP` checkpoint 保留部分成果。
你的配額重設時(Google 通常日或小時級重設,視計劃),能續該任務:

```bash
assent run <FOLDER>  # 自動從 WIP 恢復
```

任務日誌紀錄確切配額重設時間(如可得)與調度器會輪詢等候的方式。
同時要跑另一份資料夾,可在第二終端跑(只要它不依賴被配額限的那份)。
若 `[adapter].name` 設為名單,額度耗盡會依序輪替到下一個 adapter;
只有整輪 adapter 都耗盡時,調度器才等待輪詢間隔。

**組態 preflight 錯誤後修正**

別改任務檔的抽象檔位或 effort。只改 adapter 組態。例如 prime/normal 對應
到 high 但想改:

```toml
# 之前
[adapter.antigravity.efforts.prime]
normal = "high"

# 之後(如果 normal 現在支援)
[adapter.antigravity.efforts.prime]
normal = "medium"
```

修正組態後,無需改 `.assent/` 管理檔;`assent check` 會重新驗證,
`assent run` 會重試。

## 計畫格式與設定檔

- 格式契約全文:[assent/templates/format.md](assent/templates/format.md)
  (`assent init` 會複製到專案的 `.assent/format.md`)。
- 工作指示範本:[assent/templates/instructions.md](assent/templates/instructions.md)
  ——assent session 行為與跨專案共通規則;專案規則留在 `AGENTS.md`。
- 設定檔範本:[assent/templates/assent.toml](assent/templates/assent.toml)
  ——adapter 選擇、抽象檔位(prime/core/lite)對照表、
  抽象 effort(heavy/normal/slight)的預設與 CLI 值翻譯、watchdog 與重試參數。

## 常見問題

**Q:status / check / report 會消耗 tokens 嗎?**
不會。只有執行 AI 的 session 消耗 tokens;調度器從不把任何檔案內容塞給模型,
執行 AI 是自己用工具讀任務檔。

**Q:中途斷電/當機怎麼辦?**
先檢查隔離 worktree。若中斷已妥善處理且任務停在 WIP,`assent run` 會以
「接續」提示續作;若突然中斷留下未提交變更,調度器會拒絕 dirty worktree,
而不是自行猜測。檢查並建立檢查點後再重新執行。

**Q:執行 AI 亂改任務檔放寬自己的驗收怎麼辦?**
三層防禦:scope 豁免只有它自己的 `tNNN_name.toml` 任務檔與
`rNNN_name.toml` 日誌檔;任務檔除 status 外任何欄位被改動即驗收失敗
(逐欄位與檢查點版本比對);check 每輪驗 deps 完整性與循環。

**Q:BLOCKED 的任務會擋住全部進度嗎?**
只擋以它為前置的任務;其他任務照常繼續。_report.md 會列出所有卡點與最後日誌。

**Q:如何接 Claude / Codex 以外的 AI CLI?**
繼承 `Adapter` 並實作兩步介面。`resolve_model(model: str) -> str` 先把任務檔的
抽象檔位解析成這次實際傳給 AI CLI `--model` 的 `requested_model`;接著
engine 依設定檔把抽象 effort 翻成 `requested_effort`,再呼叫既有的
`run_task(prompt, requested_model, requested_effort, cwd) -> TaskResult`。Adapter
不另設 effort 翻譯方法,只使用收到的 CLI 實際值執行。`TaskResult` 包含
`exit_code`、`output`、`quota_exhausted`、
`reset_at`;額度偵測封裝在 adapter 內,主迴圈不感知廠牌差異。

## 專案狀態

核心完成:TOML 任務/日誌格式、九個子命令、claude 與 codex adapter、
完整 unittest 測試套件(無網路、無真實 CLI 也能跑)。設計共識見
[docs/CONSENSUS.md](docs/CONSENSUS.md)(正體中文翻譯:
[docs/zh-TW/CONSENSUS.md](docs/zh-TW/CONSENSUS.md))。
