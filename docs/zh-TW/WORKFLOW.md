# 工作流程

*[English](../WORKFLOW.md) · [README](../../README.zh-TW.md)*

> 本文是 [英文版](../WORKFLOW.md) 的正體中文翻譯；若內容不同，以英文版為準。

Assent 有三個人類可見階段：談妥計畫、自動執行、驗收結果。每個 AI session
只讀取該階段需要的資料。

## 1. 規劃會議

在主要 worktree 開始。讀取 `AGENTS.md`、`~/.assent/instructions.md` 與
`~/.assent/format.md`。只有修改 workflow 設定或確認 scheduler 精確行為時，
才讀 `~/.assent/workflow.md`；相關原始碼與測試則按需要檢查。

先確認需求，再寫 plan file。取得人類明確同意後，建立
`.assent/<PLAN>/tNNN_name.e.toml` tasks。每份 task 描述行為與 focused
verification command，不預測 write scope。

規劃 prompt：

```text
請和我一起規劃這項變更。先讀 AGENTS.md、~/.assent/instructions.md 與
~/.assent/format.md。請簡潔回答，不要使用子代理。按需要檢查相關原始碼與
測試；若發現原始碼錯誤、結構問題或文件與實作不符，請直接指出。不要過度
設計。先和我確認需求；在我明確同意前不要建立檔案。我同意後，將上述討論的
共識建立成 .assent/<PLAN>/ 下的 Assent 格式計畫，最後執行 assent check。
```

`assent check` 通過後，計畫才可以執行。

## 2. 自動執行

`assent run` 執行 task、plan 與 integration array；若 plan 需要，會在指定位置
插入獨立的 runtime-test array：

- `task` 處理一個 task；`focused_test` 執行該 task 的 command。
- `plan` 處理累積 candidate；`focused_sweep` 執行不重複的 task commands。
- `integration` 重建精確選集；`full_verify` 在 AI session 外執行完整 verifier。

Role session 成功就前進一格。Action 通過就完成該層並略過後續 roles；失敗則
記錄證據並前進。設定的 array 就是全部自動化預算，Assent 不會自行新增審查或
修復回合。

### 獨立的 runtime-test workflow

已安裝的 `~/.assent/workflow.md` 擁有這份 runtime-test contract；本指南摘要說明
使用方式。

`assent test [PLAN]` 與 task、plan、integration layer 分開。有 plan argument 時，
讀取該 live plan 的 `_runtime_test.toml`，在 plan candidate worktree 執行其 command。
沒有 plan argument 時，使用 project layer 的 `[runtime_test].command` 與獨立的 main
repair candidate；不修改主要 worktree。它不會執行 `full_verify`、寫入 verification
receipt 或接受任何成果。

Plan contract 選擇一個精確的 `execution` mode：`disabled` 沒有 runtime gate；
`explicit` 只有在明確執行 `assent test PLAN` 時才執行；`after_plan` 則在 `run` 的
plan workflow 後、selection 的 integration `full_verify` 前自動執行。每個
`after_plan` source 都必須通過自己的最新 runtime gate，full verification 才會開始。
Acceptance 會重新檢查相同的 source-bound runtime evidence；`accept` 絕不執行 runtime
testing。

`[workflow].runtime_test` 是由 `{ action = "runtime_test" }` step 與可寫 repair role
組成的有限 linear array。Shipped configuration 嚴格交替 action、`runtime_repairer`、
action。Runtime action 才是裁決者：exit 0 記為 `PASSED`，非 0 記為 `FAILED`，
source 或 command 漂移則記為 `STALE`。Role output 不能宣告 pass。可寫 role 若成功
結束但沒有修改 candidate source，workflow 會成為 unresolved，不會自行增加 action。

Runtime role session 可以在 candidate 中修改一般 source、test、fixture、project
configuration 與 documentation；不能執行 command，也不能修改 task contract、journal、
scheduler state、receipt、Git 或 acceptance state。Runtime state 保存 workflow cursor、
有限 evidence、candidate identity 與 quota wait。Quota 中斷會 checkpoint candidate，
restart 時恢復該 state，不會還原已消耗 token 的成果。Array 耗盡時回報
`REVIEW UNRESOLVED, HUMAN DECISION`，exit 0 並保留 evidence。

Plan runtime state 是 plan contract 旁的 `.assent/<PLAN>/_runtime_test_workflow.toml`。
Main runtime state 是 `.assent/_runtime_test_workflow.toml`，candidate 是以主要
`HEAD` 精確建立的 `<project>.runtime-test/main`。修復過的 main candidate 等待人類整合。
Runtime evidence 不是 verification receipt：`full_verify` 與 receipt 仍是獨立證據，
acceptance 需要新鮮的 receipt 與任何必需的 current runtime gate。

Ignored-directory 決定未完成時，action 並未啟動；Assent 會把這項 gate 證據與
測試結果分開。FAILED 之後的下一個已設定 action 會重新執行；只有匹配的 PASSED
證據可在中斷復原時重用。

Role 與 ability 名稱對 scheduler 沒有特殊意義。Ability 提供 prompt 與寫入
權限；可寫 role 能修改滿足既有需求所需的一般 candidate file。Task contract、
journal、scheduler state、Git、receipt 與 acceptance 都由 scheduler 控制。

Sessions 依序執行，不互相對話。Scheduler 只把先前 role 的有限輸出與精確的
機械 action 證據交給下一個 session。系統沒有 structured verdict、finding
ledger、owner routing、path-scope amendment 或第二套修復引擎。

Ignored-directory 證據為 unknown 或 stale 時，source role 會收到一項
有限的宣告指示。Session 審查完整 inventory，再透過
`assent ignored-dirs declare` 提交決定；Assent 負責驗證、記錄並套用。只有這個
operation 能寫入本機 manifest；決定完成前，下一個 action 不會開始。AI 不會
複製目錄或手動建立 link。

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

## 3. 驗收

先執行：

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

人類再明確選擇：

- `assent accept <PLAN>`：依 receipt 發布成果。
- `assent rework <PLAN> <TASK>`：保留程式碼並重開既有 task。
- `assent reject <PLAN>`：經確認的破壞性重設；先 checkpoint dirty edits、記錄
  branch tips、移除受管理的 worktrees 與同前綴 branches，再把已開始的 task
  重設為 `TODO`。

Workflow 不會自行接受 plan。Verification 提供證據；`accept` 才是人類發布決定。

## 相依與堆疊

`after` 控制 readiness。只有 `base` 允許 downstream stack 包含一個尚未接受的
upstream tip。沒有 `base` 時，plan 從目前 integration target 開始。Upstream
改變時，保留下游成果，使用 rework、reject 或新 plan，而不是改寫歷史。

另見[指令](COMMANDS.md)、[設定](CONFIGURATION.md)、
[驗證](VERIFICATION.md)與[作業](OPERATIONS.md)。
