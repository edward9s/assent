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
    ├── agents.toml              # 調度器設定:工作資料夾、adapter、檔位對照表
    ├── instructions.md          # agents session 指示與跨專案共通規則
    ├── format.md                # 本檔
    ├── verify.py                # 共用驗收腳本(任務檔 verify 欄位的預設選擇)
    └── plan01/                  # 工作資料夾(名稱由會議決定,= git 分支前綴)
        ├── agents.lock          # 該資料夾一 run 的檔案鎖
        ├── _agents.log          # 該資料夾的執行期終端輸出(不進版控)
        ├── t001_骨架與測試基建.e.toml  # 任務檔(會議產出)
        ├── t001_骨架與測試基建.r.toml  # 日誌檔(執行後產生,與任務檔成對)
        ├── t002_額度偵測.e.toml
        └── _report.md           # 人讀報告(agents 自動生成,不進版控)
```

## 工作資料夾

- 位於 `.agents/` 內,名稱寫在 agents.toml 的 `[plan] tasks`。
- 名稱規則:不含空白與路徑分隔符,不以 `-` 或 `.` 開頭(它同時是 git 分支前綴,
  分支形如 `plan01/<UTC 時間戳>`)。
- **計畫輪替 = 開新資料夾**:新一輪計畫在會議中決定新資料夾名、改 agents.toml
  指過去;舊資料夾原地保留即是歸檔,預設不讀。
- 任務編號在資料夾內**只增不改**:插入新任務用新號碼,不重編既有任務,
  deps 引用才不會斷。
- **平行執行**:一個工作資料夾同一時間只允許一個 `run`,由該資料夾內的
  `agents.lock` 鎖定；不同資料夾可在不同終端平行執行。Git 啟用時一律使用
  `<專案名>.worktrees/<資料夾>/` 的專屬 worktree，位置參數可用
  `agents run <資料夾>` 覆寫設定檔中的工作資料夾，並與 `--config` 正交。

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

`.agents/` 以主樹磁碟為唯一副本,備份由專案自行負責。Git 啟用時一律隔離到
worktree,沒有切換開關;這是安全平行處理多個工作資料夾所必需的。

### adapter 沙箱的硬需求

執行 AI 必須能寫入主樹 `.agents/`,因為改自己任務檔的 status 與 append r 檔是
分內事。若整個 `.agents/` 被 gitignore 的專案使用 codex `workspace-write`,它會被劃成
「專案外」唯讀,任務會全數假性 BLOCKED；此組合需使用 `danger-full-access`。
執行 AI 也必須保持整潔:臨時探針或墊片用完即刪,尤其不得留下內嵌 git repo。

## 任務檔(tNNN_名稱.e.toml)

檔名 = `t` + 三位數編號 + `_` + 簡短名稱 + `.e.toml`。**任務 id = 檔名前綴
tNNN**(id 只存在於檔名,檔內不重複存)。執行順序 = 檔名字典序。

目前只為本輪格式搬移保留短期過渡:在 t002 搬移完成前,調度器亦可讀舊
`tNNN_名稱.toml`;這不是長期相容承諾。同一個 tNNN id 若同時存在正式與舊任務檔,
視為衝突並拒絕解析,不得任選其一。

```toml
title = "骨架與測試基建"
deps = []                        # 前置任務 id 陣列;無前置也要明寫 []
model = "prime"                  # prime | core | lite(絕不寫廠牌型號)
effort = "high"                  # low | medium | high;可省略,由 agents.toml 預設
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
`t001_骨架.e.r.toml`;兩者依名稱排序時由 `.e.toml` 在前且固定相鄰。短期過渡的
舊 `t001_骨架.toml` 也對應同一個 `t001_骨架.r.toml`。
日誌採 append-only:只在檔尾追加 `[[entry]]` 區塊,不修改既有條目;
檔案不存在就建立。

```toml
[[entry]]
time = "2026-07-17T02:03:04+00:00"
by = "codex"                     # 執行者:codex | claude
requested_model = "gpt-5.6-sol" # 本次傳給 AI CLI 的 --model 值
event = "done"                   # 建議值:done | blocked | quota | interrupt | note
summary = "完成骨架,37 測試全綠"
detail = '''
較長的過程記錄、卡點、驗證輸出摘要。
'''                              # 可省略
```

- 執行 AI 每次 session 收尾使用 `by = "codex"` 或 `by = "claude"`,並寫入
  `requested_model`。它的 summary 會被 report 直接引用,請寫可驗證事實,
  不寫自我敘事。
- `requested_model` 精確表示 agents.toml 對照後、本次傳給 AI CLI 的
  `--model` 值;不保證是服務端最終採用或回報的模型。任務檔的 `model` 仍只寫
  `prime` / `core` / `lite`。
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
event = "quota"
summary = "額度耗盡,保留進度等待重置後接續"
```

## 任務挑選規則(調度器執行語意)

1. 依**檔名字典序**掃描工作資料夾的正式任務檔 `tNNN_名稱.e.toml` 與短期
   過渡舊任務檔 `tNNN_名稱.toml`,先明確排除 `*.r.toml`(三位數編號保證順序)。
2. 有 `WIP` 任務 -> 優先選它,帶「接續」提示續作(它是上次中斷的任務)。
3. 否則取第一個 `TODO` 且所有 `deps` 皆為 `DONE` / `SKIP` 的任務。
4. `BLOCKED` 只擋以它為前置的任務,其他任務照常執行。
5. 全部任務皆 `DONE` / `BLOCKED` / `SKIP` 時結束,印總結並更新工作資料夾內的 `_report.md`。

## 生命週期與驗收(客觀閘門)

每個任務:開 headless session -> session 結束後調度器依序檢查,全部通過才 commit:

1. **狀態檢查**:該任務 status 已被執行 AI 更新為 `DONE` 或 `BLOCKED`。
2. **結構比對**:任務檔除 status 外任何欄位與檢查點版本不一致 = 驗收失敗
   (防執行 AI 放寬自己的 scope/verify/deps)。
3. **scope 檢查**:任務起點以來的全部變更(含 wip 檢查點)都落在該任務的
   `scope` 內;自己的 t 檔/r 檔與執行期產物豁免。
4. **驗收命令**:執行該任務的 `verify`,退出碼 0 = 通過。

- 通過 -> `auto(tNNN): <title>` 檢查點 commit。自標 BLOCKED -> 免驗直接 commit
  (BLOCKED 也是合法產出,交人類裁決)。
- 失敗 -> **不還原工作區**,帶失敗原因重試;重試用盡 -> 調度器標 BLOCKED +
  r 檔機器記錄 + 連同未通過的成果 commit。**燒過 tokens 的產出絕不丟棄。**
- 額度耗盡 -> 不計失敗:r 檔記 `quota`、進度收進 `wip(tNNN)` 檢查點、
  倒數等待重置、帶「接續」提示重跑同一任務。
- 執行 AI 永不執行 git commit——檢查點由調度器建立。

## 防禦規則(總結)

1. scope fail-closed + 豁免最小化:執行 AI 在 `.agents/` 的合法改動只有
   自己 t 檔的 status 與自己的 r 檔。
2. 結構比對:t 檔其他欄位被動過即失敗;scope/verify 永遠取檢查點版本。
3. `agents check` 驗引用完整性:deps 指向存在的任務、無循環、id 不重複、
   欄位齊全——run 主迴圈每輪重新解析,壞檔 fail-loud。

## _report.md(驗收會議的議程表)

`agents report`(或 run 收尾)把 t/r 檔與 git 資訊彙整成一頁純文字報告:
進度統計、每任務一行(狀態 + 檢查點 commit)、BLOCKED/WIP 任務附最後一筆
r 檔 summary。**彙整是機械工作,零 token**;AI 永遠不做「幫我總結整輪」這種事。
_report.md 是執行期產物:不進版控、不參與乾淨/scope 檢查,每次生成整檔重寫。

## 會議規範

- 開局會議:讀 AGENTS.md + instructions.md + 本檔,把共識逐步落成任務檔
  (草稿可以是散文,
  定稿必須是任務檔),散會條件 = `agents check` 通過。
- 驗收會議:人先讀 `_report.md`(零 token),只對要裁決的任務開 AI session,
  指名「讀 tNNN 任務檔、同名 .r.toml 日誌與對應 commit」;裁決落實 =
  改任務檔(status 改回 TODO、
  補充說明、加新任務、標 SKIP),若裁決回退某任務的檢查點(git revert),同一次會議
  必須把該任務 status 改回 TODO 或標 SKIP,並檢視以它為前置的下游任務是否需要
  連動重作；狀態與程式碼事實不一致即未完成,散會仍須 `agents check` 通過。
- 專案特有且跨計畫仍有效的決策沉澱進 AGENTS.md,不靠舊任務檔傳承;
  跨專案共通的 agents 規則則更新 instructions 範本。

## 冷啟動測試(計畫合格的品質標準)

一個零記憶的新 AI session,只給它 AGENTS.md、instructions.md 與任一 `TODO`
任務檔,必須能不追問就說出:目標、可改動範圍、驗收條件、下一步。做不到 =
任務檔資訊不足,計畫還沒定稿。機器側等價物:`agents check` 通過。
