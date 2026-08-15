# 工作流程

*[English](../WORKFLOW.md) · [README](../../README.zh-TW.md)*

> 本文是 [英文版](../WORKFLOW.md) 的正體中文翻譯；若內容不同，以英文版為準。

Assent 的核心流程只有三個階段：談妥計畫、讓它自動執行、驗收完成結果。每個
階段只讓 AI 讀取當下需要的資料，不必在每個任務都帶著整套系統手冊。

## 1. 規劃會議

在主要 worktree 開始。AI 先讀：

1. 專案的 `AGENTS.md`；
2. `~/.assent/instructions.md`，了解共同的 session 規則；
3. `~/.assent/format.md`，了解計畫檔格式。

只有在會議要調整 workflow，或必須確認 scheduler 細節時，才需要讀
`~/.assent/workflow.md`。AI 可按需要查看原始碼與測試，確認真正的檔案責任與
完整 scope。

先與 AI 確認需求；討論期間不要建立檔案。取得人類明確同意後，再請 AI：
「將上述討論的共識，建立成 `.assent/<PLAN>/` 下的 Assent 格式計畫。」計畫由
`tNNN_name.e.toml` task 組成；每份 task 應讓全新的 AI 清楚知道要完成什麼、
可以改哪些路徑，以及 focused verification 如何判定完成。

會議最後執行：

```text
assent check
```

通過後，計畫才算完成。

### 規劃 prompt

```text
請和我一起規劃這項變更。先讀 AGENTS.md、~/.assent/instructions.md 與
~/.assent/format.md。請簡潔回答，不要使用子代理。按需要檢查相關原始碼與
測試；若發現原始碼錯誤、結構問題或文件與實作不符，請直接指出。不要過度
設計。先和我確認需求；在我明確同意前不要建立檔案。我同意後，將上述討論的
共識建立成 .assent/<PLAN>/ 下的 Assent 格式計畫，最後執行 assent check。
```

## 2. 自動執行

`assent run` 只會開啟目前 workflow 所需的 AI session。一般 task session
會讀專案規則、簡短的共同 instructions 與自己負責的 `.e.toml`，再自行查看
相關原始碼。Scheduler 會提供這次 role 的責任、權限與當前證據；AI 不需要讀
原始 `assent.toml` 來猜工作內容。

三層 workflow 各有不同責任：

- **Task：**完成一項任務。Focused test 失敗或 worker 回報 `BLOCKED` 時，
  才會進入 task reviewer/fixer；第一次測試通過就略過修復。
- **Plan：**所有 task 都 `DONE` 或 `SKIP` 後，檢查累積 worktree 是否符合
  整份計畫，包含不同 task 之間的互動。
- **Integration：**重建這次明確選取的完整結果並執行完整驗證。若建立
  candidate 時衝突，先修復，再重建同一組選取。

機械檢查是決策點，不是 AI 意見。通過就完成該層；失敗才使用下一個已設定的
修復 role，之後再檢查。設定陣列就是全部回合，Assent 不會自行增加次數。

若 shared ignored-directory 證據為 unknown 或 stale，同一個 plan reviewer 會在
verdict 中附上精確的 shared paths、附理由的 non-shared dispositions 與 watched
files。Assent 先驗證並套用該決定，再接受 verdict；不需要另外由人執行指令，也
不需要增加 AI session。既有且精確指向 primary 同路徑的 directory link 會列入
review 證據；若 verdict 漏列，會在 manifest 變更前交回同一個 bounded reviewer
修正。

若另一個 cached profile 漏列 application 已記錄的 link，worktree preparation 會
保留 link 並把 contract 標成 stale。Integration repair 會在 focused checks 前，讓
已設定的 integration verdict role 執行一次唯讀 shared-input recovery。若既有受損
profile 直到 focused output 指出 omitted ordinary ignored directory 之下的檔案才被
發現，則以同一條 recovery 作為安全網；scheduler 驗證並套用
paths/dispositions/watch 決定後重試一次。一般 focused failure 仍照常失敗，不會
因此增加 source-repair 回合。

Task 的 `BLOCKED` 證據留在 task 層，不會消耗 plan review。若只是少列一個
scope 路徑，具備寫入能力的 task reviewer 應在同一個 session 修好；scheduler
再驗證修改並更新 task。Role 名稱由使用者自訂，真正的權限與 verdict 行為來自
ability。

若預算用完仍無法通過，Assent 會保留成果與證據。需要人判斷的問題成為
`REVIEW UNRESOLVED, HUMAN DECISION`，讓其他排隊計畫仍可繼續；基礎設施或安全
檢查失敗仍會回傳非零結果。

中斷後，只要 ownership 明確，成果會成為可恢復的 WIP checkpoint。乾淨的舊版
`DONE` task 會保留原有歷史，不會事後補造 terminal auto checkpoint。

未完成的 plan review 會依目前的 workflow 設定恢復。若儲存的 reviewer 已移到
其他位置或 identity 已改變，Assent 會保留 commits 與耐久 findings，只重設
workflow cursor，再把既有證據交給目前流程重新判斷。只有目前已沒有
plan-review sequence 可以處理待決證據時，才會拒絕恢復。

## 3. 驗收會議

先執行：

```text
assent report <PLAN>
```

接著閱讀 `_report.md`、相關 task 與 journal、checkpoint diff、實作，以及
focused/full verification 證據。若報告標示 `TECHNICAL DEBT REVIEW REQUIRED`，
還要讀 `_technical_debt.md`，逐項決定如何處理後才能接受。

需要第二意見時，可請獨立 AI 審查。它應讀 `AGENTS.md`、三份 Assent 契約與
上述證據，再按需要查看相關原始碼。人類尚未決定前，審查本身不修改或接受成果。

### 驗收 prompt

```text
請擔任獨立驗收者。簡潔回答，不要使用子代理。先讀 AGENTS.md 與 Assent
契約，再檢查這個 plan 的 _report.md、相關 task/journal、checkpoint diff、
實作與驗證證據。優先回報有證據支持的 bug、未完成需求、缺少測試、不必要的
複雜度，以及文件與實作不符。若有 technical debt 標記，列出每一項並請人類
決定。這是人類主導的審查；不要自行 accept、rework 或修改檔案，等待人類決定。
```

人類再明確選擇：

- `assent accept <PLAN>`：依 receipt 發布成果；
- `assent rework <PLAN> <TASK>`：保留程式碼並重開既有 task；
- `assent reject <PLAN>`：經人確認後破壞性重設；先把 dirty 修改存成
  checkpoint、記錄 branch tip，再移除受管理的 worktree 與同前綴 branch，並把
  已開始的 task 重設為 `TODO`。

Workflow 不會自行接受計畫。驗證提供證據，accept 才是決定。

## 相依與堆疊

`after` 只控制執行順序；只有 `base` 表示下游 worktree 要從某個尚未接受的
上游 commit 開始。沒有 `base` 時，從目前 integration target 開始。若上游在
下游建立後又前進，保留下游成果，使用 rework、reject 或新計畫處理，不要改寫
歷史。

選取規則請看[指令](COMMANDS.md)，驗證與衝突請看[驗證](VERIFICATION.md)，
復原與清理請看[作業](OPERATIONS.md)。
