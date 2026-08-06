# 指令

*[English version](../COMMANDS.md) · [README](../../README.zh-TW.md)*

> 本檔是 [../COMMANDS.md](../COMMANDS.md) 的正體中文(台灣用語)翻譯；內容如與
> 英文版不同，以英文版為準。涵蓋 CLI syntax、選取與 acceptance 邊界。

## 一般 syntax

```text
assent <command> [options] [FOLDER ...]
```

`run`、`status`、`check`、`report`、`verify`、`clean`、`archive`、`accept`、
`reconcile`、`reject`、`rework` 支援 `--config PATH`。它指定 optional project-level
config（預設 `.assent/assent.toml`），也從該 path 找到 project；這是每個
subcommand 自己的 option，不是 top-level global option。`--config` 與 folder
argument 正交。`init`、`doctor`、`shared-paths` 各有自己的 project-location
contract。

不寫 folder 時，`run` 從 task state 與 `_folder.toml` 的 `after` prerequisite
推導唯一可執行資料夾；有 ambiguity 就拒絕。`status`、`check`、`report` 不寫 folder
時處理所有資料夾。其他指令依自己的 discovery contract 處理。

資料夾名稱必須是可攜的 Windows/Git-ref 名稱：不可空白、含 path separator、控制
字元、Git-ref 禁用字元（`~`、`^`、`:`、`?`、`*`、`[`）或 Windows 禁用字元
（`<`、`>`、`"`、`|`）。不可用 `-` 或 `.` 開頭、含 `..` 或 `@{`、以 `.` 或
`.lock` 結尾，也不可為 Windows reserved device name。這些檢查在建立 worktree
或 branch 前完成。

## 選取稽核與 `...`

所有明示的 live folder 都在 dispatch 前完整稽核。包括 `...` 前綴的每個名字，都
必須對應現有 `.assent/` 目錄，且至少有一個正式的 `tNNN_name.e.toml`。任何
unresolved 都會完整列出，並在 run、verify、publish、clean 或 archive 前阻止所有
選取操作；不會建立遺失的 folder、lock 或 log。這項 identity check 不取代 readiness、
receipt 與 Git eligibility gate。

ASCII token `...` 必須只出現一次、且是最後的位置參數；它共用於 `run`、`verify`、
`accept`、`clean`、`archive`：

```text
assent run A B ...
assent verify A ...
assent accept A ...
assent clean A ...
assent archive A ...
```

意思是「附加這個 command 自己會找到的所有剩餘 folder」。它在 mutation 前一次
snapshot，不是 `--all` 的別名。和 `--all` 合用、重複、或不在最後都算 usage error。
`verify`/`accept` 只找已完成 folder；`run`/`clean`/`archive` 考慮所有 work folder，
再套用各自 eligibility。前綴順序保留；`run` 對 remainder 做 dependency order，
`verify`/`accept` 對全選取做 dependency order，`clean` 用 upstream-first。

選取結果會在開始前印出；沒有 folder 就拒絕。`...` 不會切換 mode，因此裸的
`assent run ...` 仍是 exact selection path，不是 `--all` scheduler；`--jobs` 仍是
`--all` option。cardinality 決定 path：一個 folder 是 folder receipt、direct accept
或單一 archive；兩個以上是 exact selected batch。`assent verify A ...` 只寫恰好
展開集合的 receipt，`assent accept A ...` 需要同一集合的 fresh evidence，且不會驗證。

`...` 不可與 `verify --batch`、`verify --focus`、`run --once`、`run --task`、
`archive --restore` 合用。

## `run --verify`

`--verify` 只在 run exit code 為零後接完整 verification；run 失敗時原樣回傳且不
驗證。verification 的 exit code 成為整道 command 的 exit code。

| 呼叫 | 完整驗證範圍 |
| --- | --- |
| `assent run --verify` | 自動選出的 folder receipt。 |
| `assent run A --verify` | A 的 folder receipt。 |
| `assent run A B --verify` | A、B 作為一個 exact selected batch。 |
| `assent run A ... --verify` | 明示前綴加 remainder 的 exact selection。 |
| `assent run --all --verify` | 全專案 dynamic batch。 |
| `assent run ... --verify` | 全專案 dynamic batch。 |
| `assent run A --once --verify` | 只有 A 的所有 task 完成時才驗證。 |
| `assent run A --task t003 --verify` | 同一個 single-folder 完成條件。 |

`--once` 或 `--task` 留下未完成 folder 時，會在 candidate/verifier 建立前拒絕且不
寫 receipt；錯誤會列出 incomplete task ID 與 status。這是 invocation-level request，
不受 `receipt_refresh` 設定影響。

## `run --auto-fix`

`--auto-fix` 是唯一的 invocation-level、與選取正交的 review/repair 授權。它可和所有 `run` 選取
形式合用：自動選取、明示一個或多個 folder、prefix 加 `...`、`--all`、`--once`、
`--task` 與 `--verify`。它不代表 `--all`、不改變 cardinality 或 remainder 規則，會
依 command 原本順序傳給每個選取 folder；`--all` 的每個 child folder 都收到同一 policy。

`[auto_fix.review]` 是可選的 policy override；沒有 table 時，會用第一個 effective worker
adapter 的 `prime`/`heavy` 自動解析 reviewer，不需重跑 `assent init` 或編輯
`~/.assent/assent.toml`。整個 loop 是 invocation-level opt-in，只有明示 `--auto-fix` 的
run 才會在所有 task-focused gate 與最後 distinct focused sweep 通過後做 completed-folder
review。`adapter` 接受單一名稱或有序 list，list 長度即 loop 的上界；completed-folder round
可以在 finding 指名 task 自己的 declared scope 內修好 blocker 並回報 `FIXED`。有 durable
worker `BLOCKED` 或 focused-gate 證據的 quiescent blocked dependency
則走唯讀的 blocked-adjudication entry point；同一次 invocation 的 FAIL 才能進入 automatic repair。
沒有 `--auto-fix` 的普通 run 不會做 final sweep、review 或 repair；`--once`/`--task` 受限執行
不完整會延後 completed-folder loop，focused failure 不會開 completed-folder reviewer。

自動修正只重開 finding 所有權明確且位於 declared scope 的既有 task，記錄帶理由且
保留程式碼的 rework；每個 round 讓 durable review round index 剛好前進一格，每個 reopen
的 task 都以它自己原本的 task profile 修復：沒有 escalation ladder，也不消耗任何東西，
所以中斷的 round 會以相同 identity 恢復，多 task finding 與 dependency
cascade 不會逐 task escalation。一個已經寫入修復的 round 若在其 verdict 記錄前被中斷，
保留該編輯；下一次 run 的 startup recovery 在 durable state 的 current finding 能證明
ownership 時，會把這份 dirt 歸屬給 implicated task，收進 `wip` checkpoint，否則 fail-closed
拒絕。

round list 若結束在 `FIXED` round，會先在修復後的 source 上重跑 implicated task 自己的
focused gate 一次 —— 沿用同一份會跳過本次 invocation 已證明過 command 的 de-duplicating
ledger —— 通過後才沉澱為 `SELF-FIXED, UNREVIEWED`：保留每個 task 自己的狀態、exit code
為 0，並讓 `accept` 要求一次明確確認。若這道 settling gate 失敗，則是另一個獨立結果，
與 `SELF-FIXED, UNREVIEWED` 及一般 `BLOCKED` task 都不同：folder 不會沉澱、沒有 task 被標成
`BLOCKED`、每一項編輯與 finding 都保留，run 以非零 exit code 結束。round list 若結束在
未修好的 blocker，則沉澱為同樣獨立的 `REVIEW UNRESOLVED, HUMAN DECISION` 結果：每個 task
保留它自己 closeout 給的狀態、沒有 task 被標成 `BLOCKED`，exit code 為 0 —— 這是刻意取代
先前的非零 exit，讓同一次 `--all` invocation 中排在後面的 folder 仍能繼續啟動；未解決的
review finding 是留給人類 acceptance meeting 決定的問題，不是 infrastructure failure。在
`accept` 時，這個結果會要求同樣一次明確的 `[y/N]` 確認，指出未解決 finding 的 task、path
與 summary；同時帶有兩種結果的 folder 只會被問一次，並同時指出兩個原因。既有 technical
debt 只有 `COMPLETED_FOLDER + INITIAL`
引入、局部且 focused test 可可靠驗證時才合格；blocked adjudication 與 `RECHECK` 可以保留
或解決，但不能新增。review 不做全 repository debt audit。reviewer 可核准一個精確 scope
addition，但只有 scheduler 修改 task file；worker 與 reviewer 都禁止 task-file edits。
不會自動建立 task、還原 source、刪 source、做完整 candidate acceptance 或 publish Git。
recheck 會保留仍存在 finding 的 fingerprint，新的 finding 只接受有證據的 repair regression
或 newly exposed existing requirement；原集合清空後必須 PASS，optional improvement 與
speculation 不會讓 loop 繼續。round 用盡、中斷或 gate 失敗會保留 evidence，loop 內沒有
runtime human adjudication gate。`_auto_fix.toml` 與 report 都是 derived evidence；`accept`
仍是人類明示動作。Pending state 的 recovery 若決定它的 reviewer identity 已不在 configured
rounds 之中，repair 與 closeout 會拒絕。完整 verification 仍依成功 run 的 receipt policy 或明示 `--verify`，缺
receipt 或未跑 full suite 絕不是 reviewer failure。

Review 或 acceptance meeting 先讀 `_report.md`。若有 `TECHNICAL DEBT REVIEW REQUIRED`，
必須讀 `_technical_debt.md`，在建議 accept 前主動告訴人類並列舉每一項，逐項取得完成
local repair 足夠、追加/rework task，或提升成 `AGENTS.md` durable project rule 的明確
disposition；默讀檔案不算完成，也不是新增的 approval state。

## 指令速查

| 指令 | 效果與邊界 | token cost |
| --- | --- | --- |
| `assent run [FOLDER]` | 跑到 task 為 `DONE`、`BLOCKED` 或 `SKIP`；`--once` 停在下一個 task，`--task ID` 只跑一個但仍檢查 upstream。 | 只有 AI session |
| `assent run A B` | 依寫出順序跑 A、B，第一個失敗即停止；不暗中驗證或接受。 | 只有 AI session |
| `assent run A B --all` | 先跑明示前綴，再依 dependency order 跑剩餘 incomplete folder。 | 只有 AI session |
| `assent run --all [--jobs N]` | 用 dependency scheduler 跑所有 incomplete folder，`--jobs` 限制並行數。 | 只有 AI session |
| `assent run [selection] --auto-fix` | 在 completed folder 的最後 focused sweep 後，或有 quiescent blocked-review evidence 時，授權設定好的有界 review-and-repair loop；與 run selectors 相容，絕不 accept。 | AI session 加設定好的 review/repair |
| `assent status [FOLDER]` | 顯示進度、下一個 task、branch 與最後 checkpoint。 | 零 |
| `assent check [FOLDER]` | 檢查 task format、dependency cycle、設定與環境；是規劃散會 gate。 | 零 |
| `assent report [FOLDER]` | 產生並顯示 `_report.md`。 | 零 |
| `assent verify <FOLDER>` | 建立臨時 integration candidate，跑一次完整 verifier，刷新 folder receipt；不改 target、不開 AI。 | 零 |
| `assent verify A B` | 對 exact A、B 做 dependency-order selected batch；candidate conflict 直接拒絕。 | 零 |
| `assent verify --batch` | Dynamic 驗證已完成、尚未整合的 folder；conflict 可有一次互動式 skip decision。 | 零 |
| `assent verify <FOLDER> --focus` | 在 source worktree 重跑不同的 `DONE` task verify；不寫 receipt，不能授權 accept。 | 零 |
| `assent accept <FOLDER>` | 明示人類批准；不跑完整 verifier，除 ancestry no-op 外需 fresh matching `PASSED` evidence。 | 零 |
| `assent accept A B` | 只重播恰好 A、B 的 fresh batch receipt，不驗證，all-or-none publish。 | 零 |
| `assent accept --all` | Fresh `PASSED` batch 做 atomic replay；缺少或過期時逐 folder verify-then-accept；malformed 拒絕。 | 零加 sequential verifier |
| `assent reconcile <FOLDER>` | 為一個 source-versus-target conflict 準備人類編輯的 managed worktree；不改 target/status、不寫 receipt。 | 零 |
| `assent reconcile --continue <FOLDER>` | 驗證 staged resolution、commit merge、前進 source branch、移除已證明的 managed resource；不驗證。 | 零 |
| `assent reconcile --abort <FOLDER>` | 只移除已證明的 managed reconcile worktree 與 branch；有未提交編輯會拒絕。 | 零 |
| `assent clean [FOLDER ...]` | 只移除 fully merged 且 clean 的 worktree/branch；不碰 `.assent/`。無 folder 時處理所有，選取多個時 upstream-first。裸的 `assent clean --all` 還會每次呼叫掃一次 orphaned Assent-owned 暫存 branch；明示單一 `assent clean FOLDER` 刻意不掃。 | 零 |
| `assent archive <FOLDER ...>` | 先走 clean，再把合格計畫壓入 archive；明示但不合格者令 request 失敗。 | 零 |
| `assent archive --all` | 封存獨立合格的 folder；dynamic 模式略過不合格者而不使整體失敗；它繼承 `clean --all` 同一次每次呼叫的暫存 branch 清理。 | 零 |
| `assent archive --restore FOLDER` | 只還原一個 archive，不接受 `--all` 或 `...`。 | 零 |
| `assent reject <FOLDER>` | 人類駁回：保存 tips/WIP、link-safe 移除 worktree、刪 branch、將 `DONE`/`WIP`/`BLOCKED` 重設 `TODO`。 | 零 |
| `assent rework <FOLDER> <TASK>` | 非破壞性重開 task，預設保留程式碼；`--cascade`、`--reason`、`--revert-code` 都是明示選項。 | 零 |
| `assent shared-paths review ...` | 唯一可寫 primary worktree shared ignored-directory manifest 的操作；詳見[驗證](VERIFICATION.md)。 | 零 |
| `assent init [--test CHOICE]` | 安裝 user-home 契約/設定、project verifier、AGENTS bridge 與 ignore；fresh init 選一個真正 verifier。 | 零 |
| `assent doctor` | 診斷 Python、Git、adapter CLI 與 temporary directory，不需現有 project。 | 零 |
| `assent --version` | 顯示安裝的 distribution version，不需 project 或 subcommand。 | 零 |

每個 subcommand 的 `-h`/`--help` 才是實際 syntax。Assent 不會在 acceptance 中
連線 remote、pull、rebase、force-push、刪 source 或自動解衝突。

## 孤兒暫存 branch 清理

`assent-integration/<folder>/<suffix>` 與 `assent-reconcile/<folder>` 是
Assent 自有的暫存 branch，人類不可直接使用：兩者各自由建立它的 transaction
在完成後移除，唯有該 transaction 在完成前就中斷，殘留的 branch 才算 orphan。
repository-wide integration lock 在該 branch 仍存在時被持有，才是它是 orphan
的完整證明，而不是它的內容，也不是它的 tree 是 published 還是 superseded
——那只是回報資訊。`assent clean --all` 每次呼叫掃一次所有這類 orphan，
`assent archive --all` 繼承同一次掃描而非重新實作；明示單一 `assent clean
FOLDER` 刻意不掃。`assent doctor` 的 `[y/N]` 復原提案見[作業](OPERATIONS.md)。

## Acceptance 模式

直接 `accept <FOLDER>` 與 selected `accept A B` 絕不啟動完整 verifier。已被 target
包含的 direct folder 是 ancestry-proven idempotent no-op；其他 direct 形式需要
source tip、重建 integration tree、verifier digest 都相符的 fresh receipt。selected
形式需要恰好 dependency-ordered set 的 fresh `PASSED` batch receipt；缺少、malformed、
stale 或 drifted evidence 都拒絕。

`accept --all` 有刻意的兩種 mode：fresh `PASSED` batch 只 atomic replay 自己記錄的
folder；沒有或過期 evidence 時，依序對每個尚未整合 folder 執行
`verify_folder_if_needed` 再 accept，遇到第一個真正失敗就停止並保留之前 publication。
malformed batch receipt 不會 fallback；已整合 folder 是 ancestry no-op，source 已在
證明整合後清理的 folder 才可略過。

## 有色 help

Python 3.14+ 且標準 argparse 啟用 color 時，Assent 只重新設計 `usage:` 前綴與
section heading。`NO_COLOR`、`FORCE_COLOR`、`PYTHON_COLORS`、redirect 與不支援的
stream 仍決定是否輸出 escape；Python 3.11–3.13 是純文字。Assent 不保證有色 help。

## 相關指南

- [工作流程](WORKFLOW.md)：規劃、執行、審查、重做與駁回。
- [設定](CONFIGURATION.md)：init、設定優先序、adapter、模型與 effort。
- [驗證](VERIFICATION.md)：candidate、receipt、ignored input、reconcile、accept 證據。
- [作業](OPERATIONS.md)：worktree、lock、並行、復原、清理與封存。
