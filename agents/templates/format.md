# .agents 計畫格式(agents 格式契約)

> 本檔是「AI 會議產出計畫」與「agents 調度器自動執行」共同遵守的唯一格式契約。
> 規劃 AI:產生或修改任務檔前,必須先讀本檔。
> 執行 AI:不需要讀本檔,只讀專案 AGENTS.md、instructions.md 與被指派的任務檔。
> 格式合格的客觀標準:`agents check` 通過——這也是會議的散會條件。

## 目錄佈局

```text
project/
├── AGENTS.md                    # 專案規則(root;版控由專案決定;留一行 agents 橋接)
└── .agents/
    ├── agents.toml              # 調度器設定:adapter、檔位對照表
    ├── instructions.md          # agents session 指示與跨專案共通規則
    ├── format.md                # 本檔
    ├── verify.py                # 共用驗收腳本(任務檔 verify 欄位的預設選擇)
    └── bootstrap01/             # 工作資料夾(依任務性質命名,由會議決定,= git 分支前綴)
        ├── agents.lock          # 該資料夾一 run 的檔案鎖
        ├── _agents.log          # 該資料夾的執行期終端輸出(不進版控)
        ├── t001_骨架與測試基建.e.toml  # 任務檔(會議產出)
        ├── t001_骨架與測試基建.r.toml  # 日誌檔(執行後產生,與任務檔成對)
        ├── _folder.toml             # 工作資料夾前置依賴宣告(可省略)
        ├── t002_額度偵測.e.toml
        └── _report.md           # 人讀報告(agents 自動生成,不進版控)
```

## 工作資料夾

- 位於 `.agents/` 內,含至少一個 `tNNN_名稱.e.toml` 正式任務檔。
- 名稱依任務性質命名(如 `bootstrap01`、`loginfix01`),沒有保留字或慣例名;
  規則:不含空白與路徑分隔符,不以 `-` 或 `.` 開頭(它同時是 git 分支前綴,
  分支形如 `bootstrap01/<UTC 時間戳>`)。
- **計畫輪替 = 開新資料夾**:新一輪計畫直接開新資料夾即可;舊資料夾作為
  `after` 前置繼續參與依賴判定。`run` 省略資料夾時依任務現況與前置完成狀態
  推導選定唯一可執行資料夾,有歧義就拒絕;`status`、`check`、`report` 省略時
  作用於全部資料夾。
- 任務編號在資料夾內**只增不改**:插入新任務用新號碼,不重編既有任務,
  deps 引用才不會斷。
- **平行執行**:一個工作資料夾同一時間只允許一個 `run`,由該資料夾內的
  `agents.lock` 鎖定；不同資料夾可在不同終端平行執行。Git 永遠啟用並一律使用
  `<專案名>.worktrees/<資料夾>/` 的專屬 worktree；位置參數可用
  `agents run <資料夾>` 明示工作資料夾，並與 `--config` 正交。
- **清理**:`agents clean [FOLDER]` 只處理固定位置的冗餘 worktree 與
  `<資料夾>/*` 分支；只有同時證明該資料夾未被鎖定、worktree 乾淨、且 worktree
  與分支成果已完全併入主樹目前 HEAD，才可移除。證明不了就跳過並說明原因；
  不碰 `.agents/`（其中的 t/r 檔是歸檔本體），沒有 `--force`，也與 `git clean`
  無關，不刪未追蹤或未合併的內容。
- **駁回**:`agents reject <FOLDER>` 是人工裁決的明示駁回,與常規清理分流:
  封存未提交變更後強制刪除該資料夾的 worktree 與同前綴分支(刪除前以
  完整 tip hash 存證),並把 DONE/WIP/BLOCKED 任務改回 TODO、r 檔留下含
  完整 Git 存證的 `rejected` 記錄(SKIP 不推翻)。`FOLDER` 必填;run 進行中
  拒絕。

### 專案規則與 agents 管理面

`AGENTS.md` 是專案規則,其中只保留一行橋接,要求使用 agents 時讀取主工作樹的
`.agents/instructions.md`。它若進版控,執行 AI 讀 worktree 內的分支版本;若未
進版控,調度器改為提示主樹絕對路徑。專案特有限制不與 agents session 流程
混在同一個受工具管理的區塊。

整個 `.agents/` 是調度器管理面,預設由 `.gitignore` 排除且永遠留在主工作樹;
worktree 不含 `.agents/`。調度器提示詞會把 instructions、t/r 與預設驗收腳本
展開成主樹絕對路徑。`.agents/verify.py` 的腳本本體從主樹載入,但執行 cwd
是 worktree,因此驗收目標仍是隔離工作樹。任何已進 Git 的 `.agents/` 檔案
都會 fail-closed,避免 worktree 產生第二份真本。

`.agents/` 以主樹磁碟為唯一副本,備份由專案自行負責。Git 永遠啟用並一律隔離到
worktree,不得以切換開關或無 Git 降級模式取代;這是安全平行處理多個工作資料夾
所必需的。

### 工作資料夾依賴與完成

每個含正式任務檔的工作資料夾可放置 `_folder.toml`,內容只允許宣告直接前置
資料夾的 `after` 字串陣列:

```toml
after = ["bootstrap01"]
```

沒有 `_folder.toml` 就視為 `after = []`;若檔案存在,則必須明寫 `after`,且
每個名稱都必須是同一 `.agents/` 下含正式任務檔的資料夾,不可依賴自己。資料夾
完成狀態不另存檔,每次由其中所有正式任務檔現場推導:全部為 `DONE` 或 `SKIP`
才算完成。`run` 執行資料夾前會先檢查所有直接 `after` 前置皆已完成;未完成、
缺檔或解析錯誤都拒絕執行。`check` 會驗證完整資料夾依賴圖,偵測並拒絕循環;
任何驗證或依賴解析錯誤都維持 fail-closed。

### adapter 沙箱的硬需求

執行 AI 必須能寫入主樹 `.agents/`,因為改自己任務檔的 status 與 append r 檔是
分內事。若整個 `.agents/` 被 gitignore 的專案使用 codex `workspace-write`,主樹
`.agents/` 會是唯讀,不符合任務收尾需求。執行 AI 也必須能寫入系統暫存目錄——
tempfile 型測試會寫在那裡,`workspace-write` 同樣會拒絕。這兩項是預設設定改用
`danger-full-access` 而非 `workspace-write` 的原因；收緊沙箱時兩者都須放行。
執行 AI 也必須保持整潔:臨時探針或墊片用完即刪,尤其不得留下內嵌 git repo。

## 任務檔(tNNN_名稱.e.toml)

檔名 = `t` + 三位數編號 + `_` + 簡短名稱 + `.e.toml`。**任務 id = 檔名前綴
tNNN**(id 只存在於檔名,檔內不重複存)。執行順序 = 檔名字典序。

`.e.toml` 是唯一任務檔副檔名。工作資料夾若殘留已停用的
`tNNN_名稱.toml`,`check` 與 `run` 都會 fail-closed 並要求先搬移；不會忽略、
讀取、回退或自動搬移。直接把舊檔或 `.r.toml` 傳給任務解析器也會得到明確的
格式錯誤。

```toml
title = "骨架與測試基建"
deps = []                        # 前置任務 id 陣列;無前置也要明寫 []
model = "prime"                  # prime | core | lite(絕不寫廠牌型號)
effort = "high"                  # low | medium | high;通常省略,刻意偏離預設才明寫
status = "TODO"                  # TODO | WIP | DONE | BLOCKED | SKIP
scope = ["agents/", "tests/"]    # 允許改動的路徑前綴;fail-closed,不可為空
verify = "python .agents/verify.py"   # 驗收命令,exit 0 = 通過

goal = """
要達成什麼,一兩句。
"""

behavior = """
1. 具體行為要求,逐條。
"""                              # 可省略

acceptance = """
- 可逐項核對的驗收條件;機器可驗的納入 verify 命令,無法自動化的逐項列出。
"""

notes = """
已知事實、引用、風險;共用知識用引用,不複製。
"""                              # 可省略
```

規則:

- 欄位固定這 11 個,多寫即格式錯誤;`title / deps / model / status / scope /
  verify / goal / acceptance` 必填。
- **結構欄位寫在前、多行散文寫在後**(如上例順序)——status 行必須出現在任何
  多行字串之前,調度器靠這一點做精準寫回。
- scope 是 **fail-closed**:空陣列或缺欄位 = 格式錯誤,`run` 起點直接拒跑。
  任務自己的 t 檔(status 行)與 r 檔由調度器自動豁免,不必列入 scope。
- 工作資料夾只允許 `tNNN_名稱.e.toml` 作為任務；`*.r.toml` 永遠是日誌,
  不得進入任務集合。
- 任務檔必須「執行上自包含」:零記憶的 AI 只讀 AGENTS.md + instructions.md +
  本檔就能無歧義開工。

### 三檔位(model)

任務檔只寫抽象檔位,實際廠牌型號由 agents.toml 的 `[adapter.<name>.models]`
對照表翻譯——同一份計畫換 adapter 即換廠牌,不改任務檔。

| 檔位 | 定位 | 適用 |
|---|---|---|
| `prime` | 最強旗艦 | 架構設計、跨模組契約、最難的任務 |
| `core` | 中堅主力 | 一般實作、需要推理的除錯 |
| `lite` | 快速便宜 | 機械性修改、樣板、文件同步 |

### 三檔推理投入(effort)

`model` 與 `effort` 是正交的兩個抽象選擇。任務檔的 `effort` 選填且只接受
`low` / `medium` / `high`;它們表示相對推理投入,不是精確 token 或金額預算。
`high` 是跨廠牌可攜的高檔,不等於任一廠牌原生最高檔。通常省略 `effort`,只有
任務刻意偏離目前 adapter 對該 model 的預設時才明寫。

engine 先依「任務明寫值 > `[adapter.<名>.default_effort]` 的 model 預設 >
未指定」選出抽象 effort;未指定時不傳 effort,明確採用 CLI 預設。選出後再查
`[adapter.<名>.efforts]`:檔位分節鍵優先於平面鍵,平面鍵優先於等值直通。
例如 `[adapter.codex.efforts] high = "xhigh"` 是三個 model 檔位的通例,
`[adapter.codex.efforts.lite] high = "max"` 則只覆寫 `lite/high`。兩層與每個鍵
都可省略;什麼都不寫即全部等值,既有設定不需遷移。廠牌特有值只放在翻譯表,
不可寫進任務檔或 `default_effort`。

### 狀態語意

| 狀態 | 意義 |
|---|---|
| `TODO` | 未開始(新任務;或人類驗收不過、指示重做時改回) |
| `WIP` | 有 session 動過但未完結 = **中斷續作的訊號**:`run` 啟動時優先接續 WIP 任務 |
| `DONE` | 執行 AI 自認完成,待調度器客觀驗收 |
| `BLOCKED` | 卡住,交人類裁決(執行 AI 自標,或調度器在重試用盡時標) |
| `SKIP` | 本輪不做 |

狀態不設寫入權限:人類只負責審查與下指令,改檔一律由 AI 依指示執行。
調度器對任務檔的機器寫入僅限 status 一行,精準替換,其餘位元組不動。

## 日誌檔(tNNN_名稱.r.toml)

由任務檔去掉 `.e.toml` 後加上 `.r.toml` 產生,一任務一檔、**有需要才讀**。
例如 `t001_骨架.e.toml` 唯一對應 `t001_骨架.r.toml`,絕不產生
`t001_骨架.e.r.toml`;兩者依名稱排序時由 `.e.toml` 在前且固定相鄰。
日誌採 append-only:只在檔尾追加 `[[entry]]` 區塊,不修改既有條目;
檔案不存在就建立。

```toml
[[entry]]
time = "2026-07-17T02:03:04+00:00"
by = "codex"                     # 執行者:codex | claude
requested_model = "gpt-5.6-sol" # 本次傳給 AI CLI 的 --model 值
requested_effort = "high"       # 有傳 effort 時才寫;本次送入 CLI 的實際值
event = "done"                   # 建議值:done | blocked | quota | interrupt | note
summary = "完成骨架,37 測試全綠"
detail = '''
較長的過程記錄、卡點、驗證輸出摘要。
'''                              # 可省略
```

- 執行 AI 每次 session 收尾使用 `by = "codex"` 或 `by = "claude"`,並寫入
  `requested_model`;該次有傳 effort 時另寫 `requested_effort`。它的 summary
  會被 report 直接引用,請寫可驗證事實,不寫自我敘事。
- `requested_model` 精確表示 agents.toml 對照後、本次傳給 AI CLI 的
  `--model` 值;不保證是服務端最終採用或回報的模型。任務檔的 `model` 仍只寫
  `prime` / `core` / `lite`。
- `requested_effort` 精確表示 efforts 對照後、本次實際傳給 AI CLI 的值;
  沒有選出抽象 effort 時省略。任務檔與 `default_effort` 仍只接受抽象三值。
- 調度器的機器事件使用 `by = "scheduler"`,另寫 `agent = "codex"` 或
  `agent = "claude"` 與同一次的 `requested_model`。既有事件包括額度中斷
  `quota`、使用者或基礎設施中斷 `interrupt`、標記 `blocked`;不另寫 session
  啟動事件。
- 舊日誌的 `by = "ai"` 與缺少新欄位的條目維持可讀,不遷移、不覆寫。

調度器事件範例:

```toml
[[entry]]
time = "2026-07-17T02:05:06+00:00"
by = "scheduler"
agent = "claude"
requested_model = "fable"
requested_effort = "high"
event = "quota"
summary = "額度耗盡,保留進度等待重置後接續"
```

## CLI 與任務挑選規則(調度器執行語意)

1. 先檢查工作資料夾是否殘留已停用的 `tNNN_名稱.toml`,若有就要求搬移並
   fail-closed；否則只依**檔名字典序**掃描 `tNNN_名稱.e.toml`,明確排除
   `*.r.toml`(三位數編號保證順序)。
`agents run [FOLDER]` 明示資料夾時只執行該資料夾。省略 `FOLDER` 時,依任務
現況與資料夾 `after` 前置推導唯一「有 `TODO`/`WIP` 且前置皆完成」的資料夾;
沒有或超過一個候選,或任何資料夾解析失敗,一律拒絕猜測並要求明示資料夾。
`agents run --all` 則依資料夾依賴順序執行全部未完成資料夾;可用 `--jobs N`
限制同時執行的資料夾數,但不可與 `FOLDER`、`--once` 或 `--task` 並用。

`status`、`check`、`report` 明示 `FOLDER` 時只作用於該資料夾,省略時作用於
全部資料夾。`check` 額外驗證完整依賴圖與循環;唯讀命令遇任一資料夾錯誤都以
失敗結束。

`agents clean [FOLDER]` 清除固定位置中可證明冗餘的 worktree 與同資料夾前綴
分支;省略 `FOLDER` 時作用於全部工作資料夾。每個資料夾都必須能取得既有
`agents.lock`、worktree 完全乾淨,且所有同前綴分支與 detached HEAD 都已併入
主樹目前 HEAD,才會先以一般保護移除 worktree,再以 `git branch -d` 刪除分支。
任何證明不足都保留並明講原因;不提供強制刪除選項,也絕不改動 `.agents/`。

`agents reject <FOLDER>` 供驗收會議駁回整個資料夾的實作:取得同一把
`agents.lock` 後,先完整解析任務檔,再把未提交變更封存為 wip commit,以各
分支完整 tip hash 存證,再強制移除 worktree、以 `git branch -D` 刪除同前綴
分支,最後把 DONE/WIP/BLOCKED 任務改回 TODO 並在 r 檔 append 含完整 Git
存證的 `rejected` 記錄。狀態重置是駁回的本分,不是常規清理的例外;任何
Git 步驟失敗都不進入任務檔重置,重跑同一命令即可續完。

任一資料夾內:有 `WIP` 任務 -> 優先選它,帶「接續」提示續作;否則取第一個
`TODO` 且所有 `deps` 皆為 `DONE` / `SKIP` 的任務。`BLOCKED` 只擋以它為前置
的任務,其他任務照常執行。全部任務皆 `DONE` / `BLOCKED` / `SKIP` 時結束,
印總結並更新工作資料夾內的 `_report.md`。

## 生命週期與驗收(客觀閘門)

每個任務:開 headless session -> session 結束後調度器依序檢查,全部通過才 commit:

1. **狀態檢查**:該任務 status 已被執行 AI 更新為 `DONE` 或 `BLOCKED`。
2. **結構比對**:任務檔除 status 外任何欄位與檢查點版本不一致 = 驗收失敗
   (防執行 AI 放寬自己的 scope/verify/deps)。
3. **scope 檢查**:任務起點以來的全部變更(含 wip 檢查點)都落在該任務的
   `scope` 內;自己的 t 檔/r 檔與執行期產物豁免。
4. **驗收命令**:執行該任務的 `verify`,退出碼 0 = 通過。

- 通過 -> `auto(<工作資料夾>/tNNN): <title>` 檢查點 commit。自標 BLOCKED ->
  免驗直接使用同一命名空間 commit(BLOCKED 也是合法產出,交人類裁決)。
- 失敗 -> **不還原工作區**,帶失敗原因重試;重試用盡 -> 調度器標 BLOCKED +
  r 檔機器記錄 + 連同未通過的成果 commit。**燒過 tokens 的產出絕不丟棄。**
- 額度耗盡 -> 不計失敗:r 檔記 `quota`、進度收進
  `wip(<工作資料夾>/tNNN)` 檢查點、
  倒數等待重置、帶「接續」提示重跑同一任務。
- 執行 AI 永不執行 git commit——檢查點由調度器建立。

## 防禦規則(總結)

1. scope fail-closed + 豁免最小化:執行 AI 在 `.agents/` 的合法改動只有
   自己 t 檔的 status 與自己的 r 檔。
2. 結構比對:t 檔其他欄位被動過即失敗;scope/verify 永遠取檢查點版本。
3. `agents check` 驗引用完整性:deps 指向存在的任務、無循環、id 不重複、
   欄位齊全——run 主迴圈每輪重新解析,壞檔或已停用任務檔一律 fail-closed。

## _report.md(驗收會議的議程表)

`agents report`(或 run 收尾)把 t/r 檔與 git 資訊彙整成一頁純文字報告:
進度統計、每任務一行(狀態 + 檢查點 commit)、BLOCKED/WIP 任務附最後一筆
r 檔 summary。**彙整是機械工作,零 token**;AI 永遠不做「幫我總結整輪」這種事。
檢查點 commit 只接受與目前工作資料夾及任務 id 完整相符的
`auto(<工作資料夾>/tNNN)`；舊 `auto(tNNN)` 無法安全判定歸屬,不顯示其 hash。
_report.md 是執行期產物:不進版控、不參與乾淨/scope 檢查,每次生成整檔重寫。

## 會議規範

- 開局會議:讀 AGENTS.md + instructions.md + 本檔,把共識逐步落成任務檔
  (草稿可以是散文,
  定稿必須是任務檔),散會條件 = `agents check` 通過。
- 驗收會議:人先讀 `_report.md`(零 token),只對要裁決的任務開 AI session,
  指名「讀 tNNN 的 `.e.toml` 任務檔、共同主幹 `.r.toml` 日誌與
  `auto(<工作資料夾>/tNNN)` 對應 commit」;
  裁決落實 =
  改任務檔(status 改回 TODO、
  補充說明、加新任務、標 SKIP),若裁決回退某任務的檢查點(git revert),同一次會議
  必須把該任務 status 改回 TODO 或標 SKIP,並檢視以它為前置的下游任務是否需要
  連動重作；狀態與程式碼事實不一致即未完成,散會仍須 `agents check` 通過。
- 專案特有且跨計畫仍有效的決策沉澱進 AGENTS.md,不靠已結案任務檔傳承;
  跨專案共通的 agents 規則則更新 instructions 範本。

## 冷啟動測試(計畫合格的品質標準)

一個零記憶的新 AI session,只給它 AGENTS.md、instructions.md 與任一 `TODO`
任務檔,必須能不追問就說出:目標、可改動範圍、驗收條件、下一步。做不到 =
任務檔資訊不足,計畫還沒定稿。機器側等價物:`agents check` 通過。
