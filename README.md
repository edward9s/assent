# agents — AI 計畫格式 + 零 token 自動調度器

一套讓 AI 在長期專案中以最小上下文正確工作的檔案體系,加上讀懂這套體系、
無人值守執行的調度器。前身:計畫檔驅動的 [workflow](https://github.com/edward9s/workflow)
調度器與 .agents 記憶管理 kit,本專案把兩者合為一體。

- **規劃**:人與 AI 開會議 session,共識即時固化為 `.agents/` 裡的任務檔,
  散會條件 = `agents check` 通過。
- **執行**:`agents run` 無人值守跑完全部任務——選任務、開 headless AI session、
  客觀驗收、git 檢查點、額度等待與續作,調度本身零 token。
- **驗收**:人先讀程式生成的 `report.md`(零 token),只對要裁決的任務開 session。

## 設計原則

1. **在確保輸出品質可信可靠的前提下,最小化 tokens 消耗。**
   調度、驗收、報告全部是純 Python 本地作業;每個 AI session 的必讀集只有
   AGENTS.md + 它自己的任務檔。
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
 (tNNN.toml)  │  2. 讀該任務的檔位/effort,開 headless session │──▶ 執行 AI
              │  3. session 結束後客觀驗收:                  │◀── 更新任務檔
              │     狀態 → 結構比對(防竄改)→ scope → verify │     + r 檔日誌
              │  4a. 通過 → auto(tNNN) 檢查點 → 回到 1       │
              │  4b. 失敗 → 保留成果帶原因重試 → 仍失敗則      │
              │      標 BLOCKED 連成果一起 commit → 回到 1    │
              │  4c. 額度耗盡 → wip 檢查點 → 倒數等重置        │
              │      → 帶「接續」提示續作                     │
              └────────────────────────────────────────────┘
```

- **任務檔即狀態**:每個任務一個 TOML 檔(狀態、依賴、檔位、scope、verify、
  驗收條件),日誌一任務一檔(r 檔,append-only、預設不讀)。斷電、當機、
  額度中斷,重新 `agents run` 就從現況接著跑。
- **格式契約**:`.agents/format.md`(`agents init` 會放進專案),
  規劃 AI 讀它產生任務檔,調度器解析器與它逐字對齊。
- **session 過程即時可見**:AI 說的話(`AI|`)、用的工具(`工具|`)、token
  用量(`--|`)同步印在終端,並留存於 `.agents/agents.log`。

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

# 1. 生成 .agents 骨架與 AGENTS.md(已存在的檔案一律不覆蓋)
agents init

# 2. 填 AGENTS.md 的專案描述/硬限制、.agents/verify.py 的實際檢查命令

# 3. 開 AI 會議產出任務檔(這一步是互動 session,見下方「使用循環」)

# 4. 驗證計畫與環境(零 token;通過 = 會議可以散會)
agents check

# 5. 試跑一個任務,確認無誤後全自動跑到底(可過夜)
agents run --once
agents run

# 6. 隨時查看(另開終端、零 token)
agents status
agents report
```

跑完後人類驗收:

```
git log --oneline <資料夾名>/<run-id>   # 一任務一 commit,逐一查看
git diff main...<資料夾名>/<run-id>     # 或看整體差異
# 接受 → merge;不接受 → 對著 report.md 逐項裁決,叫 AI 改任務檔後續跑
```

## 使用循環(三幕)

**第 1 幕:規劃會議**(互動 session)

```text
開始規劃。請讀 AGENTS.md 與 .agents/format.md,
然後跟我討論以下目標,把共識逐步寫成 .agents/<工作資料夾>/ 的任務檔:
<你的目標>
```

會議中每達成一項共識就落成任務檔;散會前跑 `agents check`,不過就是還沒開完。

**第 2 幕:無人值守執行**:`agents run`,去睡覺。

**第 3 幕:驗收小會議**(互動 session)

先自己讀 `report.md`(它就是議程表:進度、BLOCKED 卡點、檢查點 hash),
再對要裁決的任務開 session:

```text
請讀 .agents/<資料夾>/t003_xxx.toml、r003_xxx.toml 與 commit <hash> 的 diff,
說明卡點並提出修正方案。
```

裁決落實 = AI 改任務檔(status 改回 TODO、補說明、加任務、標 SKIP),
`agents check` 過了回第 2 幕。循環到全部 DONE → merge。
新一輪計畫 = 開新工作資料夾 + 改 agents.toml,舊資料夾原地即歸檔。

## 指令參考

| 指令 | 作用 | token 消耗 |
|---|---|---|
| `agents run` | 主命令:跑到全部 DONE/BLOCKED/SKIP | 僅執行 AI 的 session 本身 |
| `agents run --once` | 只執行下一個任務後停止(試跑用) | 同上,單一任務 |
| `agents run --task t003` | 指定執行單一任務(仍檢查前置) | 同上,單一任務 |
| `agents status` | 進度統計、下一個任務、分支與最後檢查點 | **零** |
| `agents check` | 驗任務檔格式、依賴無循環、設定與環境 | **零** |
| `agents report` | 生成並顯示人讀報告 report.md | **零** |
| `agents init` | 在專案生成 .agents 骨架與 AGENTS.md | **零** |

所有指令(init 除外)接受 `--config <path>`(預設 `.agents/agents.toml`)。

## 計畫格式與設定檔

- 格式契約全文:[agents/templates/format.md](agents/templates/format.md)
  (`agents init` 會複製到專案的 `.agents/format.md`)。
- 設定檔範本:[agents/templates/agents.toml](agents/templates/agents.toml)
  ——工作資料夾名稱、adapter 選擇、抽象檔位(prime/core/lite)對照表、
  watchdog 與重試參數。

## 常見問題

**Q:status / check / report 會消耗 tokens 嗎?**
不會。只有執行 AI 的 session 消耗 tokens;調度器從不把任何檔案內容塞給模型,
執行 AI 是自己用工具讀任務檔。

**Q:中途斷電/當機怎麼辦?**
重新 `agents run`。狀態在任務檔和 git 裡,沒有隱藏狀態;停在 WIP 的任務會
自動以「接續」提示續作。

**Q:執行 AI 亂改任務檔放寬自己的驗收怎麼辦?**
三層防禦:scope 豁免只有它自己 t 檔與 r 檔;t 檔除 status 外任何欄位被改動
即驗收失敗(逐欄位與檢查點版本比對);check 每輪驗 deps 完整性與循環。

**Q:BLOCKED 的任務會擋住全部進度嗎?**
只擋以它為前置的任務;其他任務照常繼續。report.md 會列出所有卡點與最後日誌。

**Q:如何接 Claude / Codex 以外的 AI CLI?**
實作一個 adapter:`run_task(prompt, model, effort, cwd) -> TaskResult`
(含 exit_code / output / quota_exhausted / reset_at);額度偵測封裝在 adapter
內,主迴圈不感知廠牌差異。任務檔寫抽象檔位,對照表翻譯成自家型號。

## 專案狀態

核心完成:TOML 任務/日誌格式、五個子命令、claude 與 codex adapter、
160 個 unittest(無網路、無真實 CLI 也能跑)。設計依據的歷史紀錄見
[docs/CONSENSUS.md](docs/CONSENSUS.md)。尚未實戰:真實計畫的端到端試跑。
