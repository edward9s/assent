# agents — AI 計畫格式 + 自動調度器

一套讓 AI 在長期專案中以最小上下文正確工作的檔案體系,加上讀懂這套體系、
無人值守執行的調度器。

- **規劃**:人與 AI 開會議 session,共識即時固化為 `.agents/` 裡的任務檔,
  散會條件 = `agents check` 通過。
- **執行**:`agents run` 無人值守跑完全部任務——選任務、開 headless AI session、
  客觀驗收、git 檢查點、額度等待與續作,調度本身零 token。
- **驗收**:人先讀程式生成的 `.agents/<工作資料夾>/_report.md`(零 token),只對要裁決的任務開 session。

## 設計原則

1. **在確保輸出品質可信可靠的前提下,最小化 tokens 消耗。**
   調度、驗收、報告全部是純 Python 本地作業;每個 AI session 的必讀集只有
   專案 AGENTS.md + agents 工作指示 + 它自己的任務檔。
2. **保持靈活,少即是多。** 零第三方依賴(只用 Python 標準庫);
   狀態就是任務檔本身,沒有資料庫、沒有隱藏狀態。
3. **AI 能處理的全部自動處理,人類只做審查與裁決。**
   人不手改檔案;驗收不過就下指令叫 AI 改。
4. **執行 AI 燒過 tokens 的產出絕不丟棄。**
   額度中斷收 wip 檢查點續作;驗收失敗不還原、在現有成果上重試;
   重試用盡連同成果 commit 進 BLOCKED 檢查點交人類裁決。

## 運作原理

```
              ┌────────────────────────────────────────────┐
              │              主迴圈(零 token)               │
 .agents/     │  1. 掃工作資料夾,選任務:WIP 續作優先,        │
 工作資料夾 ──▶     否則第一個「TODO 且前置皆 DONE/SKIP」      │
 (tNNN_名稱   │  2. 讀該任務的檔位/effort,開 headless session │──▶ 執行 AI
  .e.toml)    │                                                   │
              │  3. session 結束後客觀驗收:                  │◀── 更新任務檔
              │     狀態 → 結構比對(防竄改)→ scope → verify │     + 同名 .r.toml 日誌
              │  4a. 通過 → auto(工作資料夾/tNNN) 檢查點     │
              │      → 回到 1                                  │
              │  4b. 失敗 → 保留成果帶原因重試 → 仍失敗則      │
              │      標 BLOCKED 連成果一起 commit → 回到 1    │
              │  4c. 額度耗盡 → wip 檢查點 → 倒數等重置        │
              │      → 帶「接續」提示續作                     │
              └────────────────────────────────────────────┘
```

- **任務檔即狀態**:每個任務一個 `tNNN_名稱.e.toml` 檔(狀態、依賴、檔位、
  scope、verify、驗收條件),日誌則是同主幹的 `tNNN_名稱.r.toml`
  (append-only、預設不讀)。斷電、當機、
  額度中斷,重新 `agents run` 就從現況接著跑。
- **格式契約**:`.agents/format.md`(`agents init` 會放進專案),
  規劃 AI 讀它產生任務檔,調度器解析器與它逐字對齊。
- **session 過程即時可見**:AI 說的話(`AI|`)、用的工具(`工具|`)、token
  用量(`--|`)同步印在終端,並留存於 `.agents/<工作資料夾>/_agents.log`。

## 安裝

Python 3.11+、git、已登入的 Claude Code CLI(`claude`)或 Codex CLI(`codex`)。

```
cd <agents 專案目錄>
pip install -e .
```

驗證:任何目錄執行 `agents --help`。零第三方依賴,不會下載任何外部套件。

## 快速開始

```
# 0. cd 到目標專案根目錄(需為 git repo)

# 1. 生成 .agents 骨架與 AGENTS.md
#    (既有 AGENTS.md 只補一行 agents 橋接,其他內容不覆蓋)
agents init

# 2. 填 AGENTS.md 的專案描述/硬限制、.agents/verify.py 的實際檢查命令
#    AGENTS.md 可自行決定是否提交;整個 .agents/ 留在主工作樹,不提交

# 3. 開 AI 會議產出任務檔(這一步是互動 session,見下方「使用循環」)

# 4. 驗證計畫與環境(零 token;通過 = 會議可以散會)
agents check

# 5. 試跑一個任務,確認無誤後全自動跑到底(可過夜)
agents run --once
agents run

# 也可以用位置參數指定工作資料夾(與 --config 正交)
agents run <資料夾>

# 依資料夾 after 依賴順序執行全部未完成資料夾,最多同時跑 2 個
agents run --all --jobs 2

# 6. 隨時查看(另開終端、零 token)
agents status
agents report
agents clean [FOLDER]
```

跑完後人類驗收:

```
git log --oneline <資料夾名>/<run-id>   # 一任務一 commit,逐一查看
git diff main...<資料夾名>/<run-id>     # 或看整體差異
# 接受 → merge;不接受 → 對著 _report.md 逐項裁決,叫 AI 改任務檔後續跑
```

## 平行執行

可在 N 個終端各自指定不同的工作資料夾執行，例如 `agents run parallel01`、
`agents run parallel02`;也可用 `agents run --all --jobs N` 由調度器依資料夾
依賴安排平行執行。各資料夾內的任務與日誌分別使用
`tNNN_名稱.e.toml`、`tNNN_名稱.r.toml`。每個工作資料夾都有自己的
`agents.lock`，同一資料夾
同時只允許一個 run；Git 永遠啟用,每個資料夾一律使用
`<專案名>.worktrees/<資料夾>/` 的獨立 worktree,這是安全平行處理的基礎。

版控邊界刻意簡單:`AGENTS.md` 是專案規則;有進 Git 時使用 worktree 內的
分支版本,未進 Git 時由提示詞提供主樹絕對路徑。整個 `.agents/` 是 agents
管理面,由 `.gitignore` 排除並只留在主工作樹。調度器同樣以絕對路徑提供
instructions、t/r 與預設驗收腳本;驗收腳本雖從主樹載入,執行 cwd 仍是
worktree。任何 `.agents/` 檔案已進 Git 時,調度器會在開 session 前
fail-closed 拒絕執行,避免 worktree 出現第二份真本。

AI 會議在主樹進行。從主樹可直接用 `git worktree list`、`git log <分支>` 與
`git diff main...<分支>` 審查各 worktree 的 checkpoint,不必進入其目錄。

平行執行的固有代價是額度共享，以及各分支 merge 回主線由人負責。

## 使用循環(三幕)

**第 1 幕:規劃會議**(互動 session)

```text
開始規劃。請讀 AGENTS.md、.agents/instructions.md 與 .agents/format.md,
然後跟我討論以下目標,把共識逐步寫成 .agents/<工作資料夾>/ 的任務檔:
<你的目標>
```

會議中每達成一項共識就落成任務檔;散會前跑 `agents check`,不過就是還沒開完。

**第 2 幕:無人值守執行**:`agents run`,去睡覺。

**第 3 幕:驗收小會議**(互動 session)

先自己讀 `_report.md`(它就是議程表:進度、BLOCKED 卡點、檢查點 hash),
再對要裁決的任務開 session:

```text
請讀 .agents/<資料夾>/t003_xxx.e.toml、t003_xxx.r.toml 與
auto(<資料夾>/t003) 對應 commit <hash> 的 diff,
說明卡點並提出修正方案。
```

裁決落實 = AI 改任務檔(status 改回 TODO、補說明、加任務、標 SKIP),
`agents check` 過了回第 2 幕。循環到全部 DONE → merge。
新一輪計畫 = 開新工作資料夾即可;舊資料夾可由 `_folder.toml` 的 `after`
繼續作為前置參與依賴判定。資料夾完成由任務檔推導,全部任務為 DONE/SKIP
才算完成。

## 指令參考

`run`、`status`、`check`、`report` 的完整形式都是
`agents <指令> [選項] [FOLDER]`。`FOLDER` 可明示工作資料夾；省略時 `run`
會依任務現況與 `_folder.toml` 的 `after` 前置推導唯一可執行資料夾,有歧義
就拒絕。`status`、`check`、`report` 省略時作用於全部資料夾。`--config PATH`
選擇設定檔,預設為 `.agents/agents.toml`;設定檔不再維護工作資料夾指標。
兩者彼此正交,可以只用其中一個,也可以同時使用,例如
`agents status --config configs/night.toml parallel01`。

`agents clean [FOLDER]` 只刪除已完全併入且乾淨的 worktree 與分支；證明不了就跳過，
不碰 `.agents/`，也沒有強制選項，且與 `git clean` 無關。

兩項舊設定已廢除:工作資料夾不再由設定檔中的手工指標維護,Git 也沒有停用
開關或無 Git 降級模式;工作資料夾由命令列明示或依任務事實推導,Git 永遠啟用。

| 指令與代表性命令 | 選項與作用 | token 消耗 |
|---|---|---|
| `agents run [FOLDER]`<br>`agents run parallel01` | 執行工作資料夾，直到任務全為 DONE/BLOCKED/SKIP。省略 `FOLDER` 時推導唯一可執行資料夾；`--once` 只執行下一個任務後停止；`--task ID` 指定單一任務且仍檢查前置，例如 `agents run --task t003 parallel01`。 | 僅執行 AI session 時消耗；`--once` 或 `--task` 最多執行單一任務 |
| `agents run --all`<br>`agents run --all --jobs 2` | 依 `_folder.toml` 的資料夾依賴順序執行全部未完成資料夾；`--jobs N` 限制同時執行的資料夾數(預設 1)。不可與 `FOLDER`、`--once` 或 `--task` 並用。 | 僅執行 AI session 時消耗 |
| `agents status [FOLDER]`<br>`agents status parallel01` | 顯示進度統計、下一個任務、分支與最後檢查點。接受 `--config PATH`。 | **零** |
| `agents check [FOLDER]`<br>`agents check --config .agents/agents.toml parallel01` | 驗證任務檔格式、依賴無循環、設定與環境，是規劃會議的散會條件。接受 `--config PATH`。 | **零** |
| `agents report [FOLDER]`<br>`agents report parallel01` | 生成並顯示工作資料夾內的人讀報告 `_report.md`。接受 `--config PATH`。 | **零** |
| `agents clean [FOLDER]`<br>`agents clean parallel01` | 只清理已完全併入且乾淨的 worktree 與同資料夾前綴分支；任何證明不足就跳過，不碰 `.agents/`，且沒有強制選項。省略 `FOLDER` 時作用於全部工作資料夾。 | **零** |
| `agents init`<br>`agents init --path C:\\work\\my-project` | 在目標專案生成 `.agents` 骨架與 `AGENTS.md`；`--path DIR` 預設為目前目錄。它不接受 `FOLDER` 或 `--config`。 | **零** |

各子命令的 `-h`／`--help` 會顯示該層實際語法；頂層沒有可套用到所有子命令的
`--config` 等全域選項。

## 計畫格式與設定檔

- 格式契約全文:[agents/templates/format.md](agents/templates/format.md)
  (`agents init` 會複製到專案的 `.agents/format.md`)。
- 工作指示範本:[agents/templates/instructions.md](agents/templates/instructions.md)
  ——agents session 行為與跨專案共通規則;專案規則留在 `AGENTS.md`。
- 設定檔範本:[agents/templates/agents.toml](agents/templates/agents.toml)
  ——adapter 選擇、抽象檔位(prime/core/lite)對照表、
  抽象 effort(low/medium/high)的預設與 CLI 值翻譯、watchdog 與重試參數。

## 常見問題

**Q:status / check / report 會消耗 tokens 嗎?**
不會。只有執行 AI 的 session 消耗 tokens;調度器從不把任何檔案內容塞給模型,
執行 AI 是自己用工具讀任務檔。

**Q:中途斷電/當機怎麼辦?**
重新 `agents run`。狀態在任務檔和 git 裡,沒有隱藏狀態;停在 WIP 的任務會
自動以「接續」提示續作。

**Q:執行 AI 亂改任務檔放寬自己的驗收怎麼辦?**
三層防禦:scope 豁免只有它自己的 `tNNN_名稱.e.toml` 任務檔與
`tNNN_名稱.r.toml` 日誌檔;任務檔除 status 外任何欄位被改動即驗收失敗
(逐欄位與檢查點版本比對);check 每輪驗 deps 完整性與循環。

**Q:BLOCKED 的任務會擋住全部進度嗎?**
只擋以它為前置的任務;其他任務照常繼續。_report.md 會列出所有卡點與最後日誌。

**Q:如何接 Claude / Codex 以外的 AI CLI?**
繼承 `Adapter` 並實作兩步介面。`resolve_model(model: str) -> str` 先把任務檔的
抽象檔位解析成這次實際傳給 AI CLI `--model` 的 `requested_model`；接著
engine 依設定檔把抽象 effort 翻成 `requested_effort`,再呼叫既有的
`run_task(prompt, requested_model, requested_effort, cwd) -> TaskResult`。Adapter
不另設 effort 翻譯方法,只使用收到的 CLI 實際值執行。`TaskResult` 包含
`exit_code`、`output`、`quota_exhausted`、
`reset_at`；額度偵測封裝在 adapter 內,主迴圈不感知廠牌差異。

## 專案狀態

核心完成:TOML 任務/日誌格式、五個子命令、claude 與 codex adapter、
完整 unittest 測試套件(無網路、無真實 CLI 也能跑)。設計共識見
[docs/CONSENSUS.md](docs/CONSENSUS.md)。
