# assent — AI 計畫格式 + 自動調度器

*[English version](README.md)*

Assent 是給長期專案使用的檔案式規劃格式與調度器。人類與 AI 先討論並
同意計畫，調度器在隔離的 Git worktree 執行任務，人類再依據報告與驗證證據
決定是否接受。管理面是 `.assent/`；專案本身仍是一般 Git 專案。

## 最短導覽

| 階段 | 內容 | 主要指令 |
| --- | --- | --- |
| 規劃 | 討論目標、寫入 Assent 格式任務檔、驗證計畫。 | `assent check` |
| 執行 | 跑每個任務的 focused verify、保存 WIP/檢查點，並可明示啟動有界的 folder review-and-repair loop。 | `assent run`、`assent run --auto-fix` |
| 審查 | 讀 `_report.md`、檢查 diff 與完整驗證證據，再決定接受、重做或駁回。 | `assent report`、`assent verify`、`assent accept` |

`DONE` 是執行 AI 的完成主張，不是人類批准。完整驗證 receipt 是證據；
`accept` 才是明示的人類決定。Assent 不丟棄已消耗 token 的成果：可處理的
中斷會成為 WIP 檢查點，失敗成果保留供重試或人工裁決。

## 需求與安裝

需要 Python 3.11+、Git，以及已登入的支援 AI CLI，例如 `claude` 或
`codex`。Assent 只使用 Python 標準函式庫。

安裝已發布套件：

```text
python -m pip install assent
```

移除已發布套件：

```text
python -m pip uninstall assent
```

解除安裝只會移除 Python 套件與 `assent` CLI 入口，不會刪除
`~/.assent`、任何專案的 `.assent/`、worktree、archive 或 Git branch；
資料清理仍是人類明確選擇。從原始碼以 editable 方式安裝請看
[設定指南](docs/zh-TW/CONFIGURATION.md)。

用 `assent --version` 或 `assent doctor` 檢查安裝。

## 快速開始

在既有 Git 專案根目錄執行：

```text
# 安裝每位使用者的契約/設定，以及專案的 .assent 骨架。
assent init --test unittest

# 檢查 ~/.assent/assent.toml、AGENTS.md 與 .assent/verify.py。
# 開規劃會議，在 .assent/<folder>/ 下寫入任務檔。
assent check

# 先試跑一個任務，再讓剩餘工作無人值守執行。
assent run --once
assent run

# 不開 AI session，刷新完整驗證證據。
assent verify <FOLDER>

# 人類在這個決定前先讀報告並檢查檢查點 diff。
assent report <FOLDER>
assent accept <FOLDER>

# 清理與封存都是另外的明確選擇。
assent clean <FOLDER>
assent archive --all
```

新專案的 `assent init` 會詢問真正的 verifier，可選平行 unittest、pytest、
npm test、Flutter test 或 custom argv。重跑 init 會保留現有 verifier、刷新兩份
使用者家目錄契約，只補入遺漏設定。使用者家目錄契約是
`~/.assent/instructions.md` 與 `~/.assent/format.md`；專案不會收到副本。
詳見[設定指南](docs/zh-TW/CONFIGURATION.md)。

可在另一個終端執行 `assent status`、`assent report`，或用 `git log`、
`git diff` 查看 worktree branch。`assent run --all --jobs 2` 會依資料夾依賴
安排獨立工作；`after` 只控制 readiness，只有明示的 `base` 會讓下游 worktree
從上游 commit 開始。詳見[工作流程](docs/zh-TW/WORKFLOW.md)與
[作業指南](docs/zh-TW/OPERATIONS.md)。

## 可選的有界 auto-fix

設定 `[auto_fix.review]` 會 override folder-level review-and-repair policy；沒有這個
table 時，明示的 `assent run --auto-fix` 會用第一個 effective worker adapter 的
`prime`/`heavy` 解析 reviewer，不需要重跑 `assent init` 或編輯
`~/.assent/assent.toml`。整個 loop 都是 invocation-level opt-in，只有明示的
`assent run --auto-fix` 才會在最後 focused checks 後啟動 completed-folder review，
或在有 durable blocker 證據的 quiescent blocked dependency 進入唯讀的
blocked-adjudication 入口，並授權該次 invocation 的有界 repair。沒有 flag 的普通 `assent run` 不會啟動
review，也不會 repair。這個 flag 與選取正交，可和明示 folder、`...`、`--all`、
`--once`、`--task`、`--verify` 合用；受限 run 若留下 incomplete folder，就延後
completed-folder loop。

Review 只看變更與直接互動的程式碼；既有 technical debt 只有在
`COMPLETED_FOLDER + INITIAL` 引入、修正局部於既有 task 的 declared scope、且 focused
test 能可靠驗證時才合格，不做全 repository debt audit。blocked adjudication 與
`RECHECK` 可以保留或解決該 ledger entry，但不能新增 debt。FAIL 只會自動重開 finding
所有權明確的既有 task，記錄帶理由且保留程式碼的 rework。reviewer 可以核准一個精確的
scope addition，但只有 scheduler 能修改 task file；worker 與 reviewer 都禁止編輯 task
file。completed-folder round 是合併的 reviewer-fixer session：發現真正的 blocker 時，
它可以直接在 finding 指名的那一個既有 task 的 declared scope 內修好，並以 `FIXED` 回報；
`PASS` 代表沒有 blocker 且該 round 完全沒有寫入任何檔案。
`[auto_fix.review].adapter` 接受單一 adapter 或有序 list，list 長度就是
round 的有限上界：每個 round 讓 durable round index 剛好前進一格。每個 reopen 的 task 都以它自己原本
的 task profile 修復，沒有 escalation ladder 也不消耗任何東西，所以中斷的 round 會以完全
相同的 identity 恢復，多 task finding 或 dependency cascade 也不會逐 task 讓 sibling
提前 escalation。recheck 會保留仍存在 finding 的 fingerprint，只有
有證據的 repair regression 或 newly exposed existing requirement 才能有新 finding；先前
集合清空後必須 PASS，optional improvement 與 speculation 不會讓 loop 繼續。它不會自動建立 task、
還原 source、刪 source、publish Git 或接受 folder。round list 若結束在一個沒有任何 round
確認過的修復上，folder 會沉澱為 `SELF-FIXED, UNREVIEWED`：每個 task 保留它自己 focused
gate 證明的狀態，run 仍然成功，而 `assent accept` 會在 publish 前要求一次明確的人類確認。
若 round list 結束在未修復的 blocker，state 與編輯會被保留交人類事後檢視；loop 內沒有
runtime human adjudication gate。
之後明示的 `run --auto-fix` recovery，必須讓決定該 pending state 的 identity 仍是目前
configured rounds 之一；移除或改變
policy 會拒絕 repair 與 closeout。`_auto_fix.toml` 與 report 是 derived evidence，不是
task status 或 acceptance evidence。完整 verification 仍依成功 run 的 receipt policy 或
明示 `--verify` 另行執行；缺 receipt 或尚未跑 full suite 絕不是 reviewer failure。
Report 也會顯示 exact scope-amendment transaction 與 durable review round index。Scheduler 在
rework、中斷、repair closeout 或 round 用盡時的 status-only transition 不會單獨讓 evidence
stale；真正的 task-contract structural edit 才會。

## 規劃會議 prompt

可把下面文字作為起點；討論由人類主導，任務 schema 以安裝的格式契約為準：

```text
請簡潔回答，不要用子代理。如果你看到源碼有任何bug、壞結構，或說明文件與程式行為不符合，就回報我。以下是本專案需要討論的問題，不要過度設計，先徵得人類的同意，依照 assent 格式產生相關的計畫書：
1. 需求描述。
2. 需求描述。
3. 需求描述。
```

每個達成共識的要求都應立即固化到任務檔；`assent check` 通過才可散會。
規範 schema 是使用者家目錄的 `~/.assent/format.md`，不是專案複製檔。

## 獨立驗收審查 prompt

需要第二意見時使用：

```text
請擔任獨立的驗收審查者。請簡潔回答，不要用子代理。任何變更前，先檢查工作資料夾的 _report.md；如果有 `TECHNICAL DEBT REVIEW REQUIRED`，讀取 _technical_debt.md，在建議 accept 前主動告訴人類，並列出每一項 debt，逐項取得「已完成的 local repair 足夠」、「追加/rework task 做具體追蹤」，或「提升成 AGENTS.md 的 durable project rule」的明確 disposition。接著檢查相關任務與 journal 檔、checkpoint commit/diff、實作，以及 focused/full verification 證據。先回報有證據支持的發現：bug、結構問題、過度設計、缺少測試，以及說明文件與程式行為漂移。建議使用與實作者不同 vendor 的高能力模型做獨立 cross-review，但不要要求或編碼第二模型或自動 gate。這個一般驗收審查由人類主導，不在該審查中自動 accept 或 rework；明示的 `run --auto-fix` 是另一個有界 review-and-repair 授權，且仍絕不自動接受 folder。等待人類決定；只有人類同意後，才寫 Assent 格式的 rework 任務或說明 acceptance 動作。
```

審查者整理發現時不修改 worktree。接受仍是人類執行
`assent accept <FOLDER>`（或明示 selected batch），重做仍是人類執行
`assent rework <FOLDER> <TASK>`。

## 主題導覽

README 是入口；詳細內容放在五組英中配對指南：

| 主題 | English canonical | 正體中文 reader guide |
| --- | --- | --- |
| 規劃、執行、審查、prompt、重做 | [WORKFLOW](docs/WORKFLOW.md) | [WORKFLOW 正體中文](docs/zh-TW/WORKFLOW.md) |
| 選取與 CLI 參考 | [COMMANDS](docs/COMMANDS.md) | [COMMANDS 正體中文](docs/zh-TW/COMMANDS.md) |
| init、設定、adapter、模型、effort | [CONFIGURATION](docs/CONFIGURATION.md) | [CONFIGURATION 正體中文](docs/zh-TW/CONFIGURATION.md) |
| focused/full verification、receipt、reconcile、accept 證據 | [VERIFICATION](docs/VERIFICATION.md) | [VERIFICATION 正體中文](docs/zh-TW/VERIFICATION.md) |
| worktree、鎖、並行、復原、清理、封存 | [OPERATIONS](docs/OPERATIONS.md) | [OPERATIONS 正體中文](docs/zh-TW/OPERATIONS.md) |

英文檔是 canonical，正體中文檔是讀者翻譯；內容不一致時以英文版為準。
[設計共識](docs/zh-TW/CONSENSUS.md)與[翻譯流程](docs/TRANSLATING.md)維持原有
專門角色。

## 必記的安全邊界

- Git 永遠啟用，沒有 Git-less 模式，也沒有手動維護的 current-folder pointer。
  請明示資料夾，或讓任務事實推導出唯一選取。
- 直接與 selected `accept` 不會啟動完整驗證；除了 ancestry 已證明的 no-op，
  都需要新鮮且相符的證據。`accept --all` 有自己的 batch replay 與 sequential
  fallback 模式。
- 完整 verification 使用臨時 integration candidate 與可刪除 receipt，只會
  鏡像 tracked content、已審閱的 ignored directory link，以及 tracked directory
  內的一般 ignored leaf file；不會複製 ignored tree。
- 清理前會先脫離 directory link，絕不穿越外部 target。不要手動刪除受管理的
  worktree 或 branch。
- worktree 是隔離與稽核邊界，不是安全 sandbox；無人值守 AI 仍擁有 OS identity
  可存取的 credentials、網路、外部 Git writer 與 worktree 外檔案權限。
- `[auto_fix.review]` 只提供有界 folder review policy；只有明示的
  `run --auto-fix` 會啟動唯讀 review 與有限、保留程式碼的 repair loop。沒有 flag 的
  普通 `run` 不會 review 或 repair；`_auto_fix.toml` 永遠不是 acceptance，`accept`
  仍是人類動作。

精確選取、receipt freshness、共享 ignored input 審閱、adapter 對應、復原與
完整 command options，請看上方五份指南。
