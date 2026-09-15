# 工作流程

*[English](../WORKFLOW.md) · [README](../../README.zh-TW.md)*

> 本文是 [英文版](../WORKFLOW.md) 的正體中文翻譯；若內容不同，以英文版為準。

一般人類流程是：初始化一次、使用自己選擇的任何 AI 開規劃會議、執行產生的
plan、按需要執行 explicit runtime test、接受成果，最後封存。最後一次
`assent check` 由 planning AI 負責；人類正常情況不需要在會議後再執行一次。

每個 execution AI session 只讀取該階段需要的資料。

## 1. 規劃會議

在主要 worktree 開始。Planning AI 讀取 `AGENTS.md`、
`~/.assent/instructions.md` 與 `~/.assent/format.md`。只有修改 workflow 設定或確認
scheduler 精確行為時，才讀 `~/.assent/workflow.md`；相關原始碼與測試則按需要
檢查。

先確認需求，再寫 plan file。取得人類明確同意後，由 planning AI 建立
`.assent/<PLAN>/tNNN_name.e.toml` tasks。每份 task 描述行為與 focused verification
command，不預測 write scope。會議結束前，AI 依完成後的專案共識配置
`.assent/verify.py`，使用 test runner 支援的最大安全平行度，並建立 plan 的
`_runtime_test.toml`。Commands 可以指向 plan 將建立的 test 或 probe；規劃階段
不執行它們。只有 plan 不需要 runtime gate 時才使用 `disabled`，不能用它表示
command 尚未確定。

規劃 prompt：

```text
請和我一起規劃這項變更。先讀 AGENTS.md、~/.assent/instructions.md 與
~/.assent/format.md。請簡潔回答，不要使用子代理。按需要檢查相關原始碼與
測試；若發現原始碼錯誤、結構問題或文件與實作不符，請直接指出。不要過度
設計。先和我確認需求；在我明確同意前不要建立檔案。我同意後，將上述討論的
共識建立成 .assent/<PLAN>/ 下的 Assent 格式計畫，配置完整 verification 與
runtime decisions，最後反覆執行 assent check 直到通過後才能結束會議。
```

`assent check` 通過後，計畫才可以執行。在正常流程裡，這是 planning AI 的最後
validation gate。人類仍可手動執行它來診斷問題，但它不是會議結束後額外必走的
人工步驟。

## 2. 自動執行

規劃結束後，人類通常直接使用 `assent run`，或以 `assent run <PLAN>` 明確選取。

`assent run` 先執行 plan 的 preflight array，再執行 task、plan 與 integration
array；若 plan 需要，會在指定位置插入獨立的 runtime-test array：

- `preflight` 執行完整的唯讀 `check`；失敗時可進入宣告式 repair role，再重新
  check。
- `task` 處理一個 task；`focused_test` 執行該 task 的 command。
- `plan` 處理累積 candidate；`focused_sweep` 執行不重複的 task commands。
- `integration` 重建精確選集；`full_verify` 在 AI session 外執行完整 verifier。

Role session 成功就前進一格。Action 通過就完成該層並略過後續 roles；失敗則
記錄證據並前進。設定的 array 就是全部自動化預算，Assent 不會自行新增審查或
修復回合。

### Preflight repair workflow

已安裝的 `~/.assent/assent.toml` 嚴格交替 `check`、
`preflight_repairer` 與 `check`。第一個 action 通過時不會啟動 AI。失敗時，
repairer 會收到完整診斷；只有下一個唯讀 check 通過，才接受其宣告式修改。
Repairer 不得修改 task status、workflow cursor、evidence、receipt、Git、
candidate source 或 acceptance。成功的 repair evidence 會寫入 plan journal 與
report。

明確執行 `assent check` 仍為唯讀，不會進入 workflow。若設定錯誤使 Assent
無法解析 preflight role 或任何可傳送的 adapter，仍屬於需要人工處理的 bootstrap
failure。

### 獨立的 runtime-test workflow

已安裝的 `~/.assent/workflow.md` 擁有這份 runtime-test contract；本指南摘要說明
使用方式。

`assent test [PLAN]` 與 task、plan、integration、`full_verify`、`accept` 分開。

Main discovery 從 `assent init` 建立的檔案開始：

```toml
# .assent/_runtime_test.toml
execution = "pending"
```

執行 `assent test` 後，有限流程如下：

| Step | 可觀察結果 |
| --- | --- |
| `runtime_test` action | 不啟動，也不記成 `FAILED`。 |
| 可寫 runtime role | 檢查操作文件與 production entrypoint，不修改一般 project file；只有能唯一判定時，才提出精確、有限的 production operation，否則維持 `pending`。 |
| scheduler | 還原 role 直接修改的 control file、驗證並顯示每一條完整 command，只在一次 `[y/N]` 確認後寫入。 |
| 下一個 `runtime_test` action | 從頭執行已寫入的 command。 |

有效提案如下：

```toml
# .assent/_runtime_test.toml
execution = "explicit"
command = "python run.py sync"
```

Command 必須使用正常持久化 production 設定與資料；test、probe、暫存 resource、
sample 或刻意限縮範圍的替代指令都不符合。一條 shell command 是包含 executable
與所有參數的完整字串；array 表示多條依序執行的完整 command line，不是 argv
token array。只有 `y` 或 `yes` 會寫入並執行；拒絕
或 stdin EOF 會維持 `pending` 且不執行。寫入後，之後的 `assent test` 不再詢問。
無效提案直接拒絕；所有 role 都未提出時，有限 workflow 以 unresolved 結束。
`assent check` 維持唯讀，不會啟動 discovery。

Plan directory 內使用同名 contract，但有三種不同 mode：

| `.assent/<PLAN>/_runtime_test.toml` | 效果 |
| --- | --- |
| `execution = "disabled"` | 沒有 `command`，也沒有 runtime gate。 |
| `execution = "explicit"` 加 `command` | 使用 `assent test <PLAN>` 執行。 |
| `execution = "after_plan"` 加 `command` | `assent run` 在 plan workflow 後、integration `full_verify` 前執行。 |

例如 explicit plan 的操作順序是：

```text
assent run demo
assent test demo
assent accept demo
```

`after_plan` 省略中間那條指令。`accept` 絕不執行 runtime testing；發布前只重新檢查
必要且 source-bound 的 runtime evidence。

共用設定直接列出預設 repair loop：

```toml
runtime_test = [
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
]
```

Command array 失敗時的流程是：

```text
runtime_test       第一個非 0 command -> FAILED；後續 command 不執行
runtime_repairer   修改一般 source、test、fixture、config 或文件
runtime_test       從第一個 command 重新執行整個 array
```

只有 action 能記錄 `PASSED`、`FAILED` 或 `STALE`；role 的文字不能宣告通過。真正的
command 失敗後，role 若未修改 source，結果就是 unresolved。Pending main contract
提案與完成 ignored-input decision，是可以不修改 tracked source 而前進的兩種情況。
逐 step adapter 範例見[設定](CONFIGURATION.md)。

| Target | Contract | Workflow state | Working tree |
| --- | --- | --- | --- |
| current main | `.assent/_runtime_test.toml` | `.assent/_runtime_test_workflow.toml` | primary tree |
| live plan | `.assent/<PLAN>/_runtime_test.toml` | `.assent/<PLAN>/_runtime_test_workflow.toml` | plan candidate |

Runtime role 不執行 command，也不修改 Git、journal、receipt、task contract、scheduler
state 或 acceptance state。Quota interruption 會保留 edits，並從已儲存的位置繼續。
流程耗盡時回報 `REVIEW UNRESOLVED, HUMAN DECISION`：獨立的
`assent test [PLAN]` 回傳 1；unattended `run` 回傳 0，讓其他 plan 繼續。Runtime
evidence 不是 verification receipt；`full_verify` 仍是另一份證據。

Role 與 ability 名稱對 scheduler 沒有特殊意義。Ability 提供 prompt 與寫入
權限；可寫 role 能修改滿足既有需求所需的一般 candidate file。Task contract、
journal、scheduler state、Git、receipt 與 acceptance 都由 scheduler 控制。

Sessions 依序執行，不互相對話。Scheduler 只把先前 role 的有限輸出與精確的
機械 action 證據交給下一個 session。系統沒有 structured verdict、finding
ledger、owner routing、path-scope amendment 或第二套修復引擎。

Ignored-directory 證據為 unknown 或 stale 時，source role 會收到一項有限的宣告
指示。Session 審查完整 inventory，再透過 `assent ignored-inputs declare` 提交決定；
Assent 負責驗證、記錄並套用。只有這個 operation 能寫入本機 manifest；決定完成
前，下一個 action 不會開始。AI 不會複製目錄或手動建立 link。

Integration failure 可以前進到已設定的 integration role。Typed Git conflict
evidence 會指出衝突的 plan 與 paths；target-only conflict 使用受管理的 reconcile
worktree，peer-only conflict 使用該 plan 的 persistent source worktree，之後再重建
exact candidate。沒有機械式 source attribution 的 multi-plan verifier failure 才交由
人類決定。

有限 array 走完仍未通過時，所有修改與證據都保留，結果是
`REVIEW UNRESOLVED, HUMAN DECISION`，exit zero，讓其他排隊計畫繼續。基礎設施
錯誤、被拒絕的 precondition 或損壞的 safety gate 才是 nonzero。

中斷與 quota 等待前會 checkpoint dirty candidate。下次 run 從已保存的 cursor
與 worktree 繼續，不丟棄已花 token 產生的成果。

## 3. 人類驗收

檢查實作、diff、verification evidence，以及任何必要的 runtime-test 結果。
`assent report <PLAN>` 是可選的輔助工具，用來重新產生結構化驗收摘要；它不是
accept 前必須執行的步驟。

需要結構化驗收資料時再執行：

```text
assent report <PLAN>
```

檢查 `_report.md`、task requirements、相關 journal、source diff 與 verification
證據。需要第二意見時可使用獨立 AI，但決定仍屬於人類。

驗收 prompt：

```text
請擔任獨立驗收者。簡潔回答，不要使用子代理。先讀 AGENTS.md 與 Assent
契約，再檢查這個 plan 的 _report.md、相關 task/journal、source diff、實作與
驗證證據。優先回報有證據支持的 bug、未完成需求、缺少測試、不必要的複雜度，
以及文件與實作不符。這是人類主導的審查；不要自行 accept、rework 或修改檔案，
等待人類決定。
```

正常成功流程最後是：

```text
assent accept <PLAN>
assent archive <PLAN>
```

`accept` 依 receipt 發布成果到目前 target branch，本身不執行 verification 或
runtime testing。接受完成而且不再需要 live plan 後，`archive` 是正常的最後維護
動作。

如果結果需要人工介入，才使用：

- `assent rework <PLAN> <TASK>`：保留程式碼並重開既有 task。
- `assent reject <PLAN>`：經確認的破壞性重設；先 checkpoint dirty edits、記錄
  worktree HEAD 與所有 branch tip 到 plan-level `_reject.toml`、移除受管理的
  worktrees 與同前綴 branches，再把已開始的 task 重設為 `TODO`。這些 hash 只在
  Git 尚未清除 commit object 時支援人工復原；重新執行 `reject` 不會讀取這份
  journal，也不會重建已刪除的 branch。
- `assent reconcile <PLAN>`：處理需要人工編輯的 Git conflict。

Workflow 不會自行接受 plan。Verification 提供證據；`accept` 才是人類發布決定。

## 4. 封存完成工作

`assent archive <PLAN>` 嚴格包含 `assent clean` 的安全清理：它會先證明並移除
已多餘的 managed source worktree/branch，再壓縮 `.assent/<PLAN>/`、記錄 archive，
最後移除 live plan 目錄。

因此正常流程不需要在 `archive` 前先執行 `clean`。只有想移除已證明多餘的
source worktree/branch、但刻意保留 live plan 紀錄時，才單獨使用 `assent clean`。

## 相依與堆疊

`after` 控制 readiness。只有 `base` 允許 downstream stack 包含一個尚未接受的
upstream tip。沒有 `base` 時，plan 從目前 integration target 開始。Upstream
改變時，保留下游成果，使用 rework、reject 或新 plan，而不是改寫歷史。

另見[指令](COMMANDS.md)、[設定](CONFIGURATION.md)、
[驗證](VERIFICATION.md)與[作業](OPERATIONS.md)。
