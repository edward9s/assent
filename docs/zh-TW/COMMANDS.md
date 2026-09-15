# 指令

*[English](../COMMANDS.md) · [README](../../README.zh-TW.md)*

> 本文是 [英文版](../COMMANDS.md) 的正體中文翻譯；若內容不同，以英文版為準。

完整 option 請直接執行 `assent <command> --help`。本文先把一般人類流程與
planning AI、檢查、復原指令分開，再說明 plan selection。

## 人類正常流程

一般情況下，人類真正需要操作的是：

```text
assent init
# 開規劃會議；planning AI 建立 plan，並在結束會議前反覆執行
# assent check 直到通過。
assent run <PLAN>
assent test <PLAN>      # runtime execution 為 explicit 時
assent accept <PLAN>
assent archive <PLAN>
```

`assent check` 主要是 planning contract 的 validation gate。人類可以拿它診斷，
但正常由 planning AI 負責。`assent report` 是可選的驗收輔助，不是必走步驟。
`assent archive` 已經包含與 `clean` 相同的安全清理，所以完成 plan 時不需要先
另外執行 `clean`。

使用 whole-project scheduler 時，`assent run` 可以省略 plan 名稱。

## Plan selection

`PLAN` 是專案 `.assent/` 直下的目錄名稱，不是路徑；例如 `demo` 代表
`.assent/demo/`。其中必須至少包含一份正式 `.e.toml` task。Assent 會在任何動作
開始前檢查所有 plan 名稱；若有錯，會一次列出完整清單，不會執行其中一部分。

選到一個 plan 就走單一 plan 流程；兩個以上就是一組精確 batch。明確選取的
accept 仍需要與整組完全相符的證據，而且不會啟動驗證。

`run`、`status`、`check`、`report`、`verify`、`clean`、`archive`、`accept`、
`reconcile`、`reject`、`rework` 支援各自的 `--config PATH` option。它會選擇專案
設定並定位專案，不是 top-level global option。`init`、`doctor` 與 `ignored-inputs`
各有自己的專案位置規則。

## 指令角色

| 指令 | 角色 |
| --- | --- |
| `init` | **人類正常流程。** 安裝共用契約與設定，建立專案骨架。 |
| `run` | **人類正常流程。** 執行 task、plan 與 integration workflow。 |
| `test` | **需要時的人類正常流程。** 判定或執行 main runtime contract，或執行 plan 宣告的 command。 |
| `accept` | **人類正常流程。** 人類依相符證據發布成果。 |
| `archive` | **人類正常流程。** 安全清理並封存完成的管理紀錄。 |
| `check` | **Planning AI／診斷。** 不開 AI，檢查計畫、設定與相依關係。 |
| `report` | **可選檢查。** 重新產生人類驗收用的報告。 |
| `status` | **可選檢查。** 查看一個或全部計畫的簡要狀態。 |
| `verify` | **手動驗證／復原。** 執行指定的機械驗證，不啟動 AI review、repair 或 accept。 |
| `reconcile` | **衝突解決。** 準備並完成由人編輯的 Git 衝突修復。 |
| `rework` | **重新開啟 task。** 保留程式碼，重新開啟既有 task。 |
| `reject` | **破壞性重設。** 將人工 Git 復原證據記錄到 `_reject.toml` 後，拋棄該 plan 的實作。 |
| `clean` | **可選維護。** 不封存 live plan，只移除已證明多餘的 worktree/branch。 |
| `doctor` | **診斷。** 診斷安裝並復原孤兒暫存 branch。 |
| `ignored-inputs status` | **診斷。** 查看目前 worktree 的 ignored-input 決定與鏈結，不做任何變更。 |
| `ignored-inputs declare` | **AI source-role operation。** 記錄審查結果，只為必要檔案或目錄建立鏈結。 |

## 初始化專案

`assent init` 會安裝共用契約與設定、建立 fail-closed 的 `.assent/verify.py`，並建立
內容為 `execution = "pending"` 的 `.assent/_runtime_test.toml`；它不詢問 command。
更新 framework 時會保留既有 project-owned verifier command block、main runtime
decision 與 `.assent/assent.toml`。Planning meeting 負責配置 verifier 與各 plan 的
runtime decision；planning AI 必須在結束會議前反覆執行 `assent check` 直到通過。

## 執行

排程所有找到且 ready 的 plan：

```text
assent run
assent run --jobs 2
```

省略 `PLAN` 時，`run` 使用整個專案的 dependency scheduler。`--jobs` 設定並行
上限，而且只能用於這種 whole-project 執行方式。

執行精確指定的 plans：

```text
assent run <PLAN>
assent run A B
```

具名 plans 依輸入順序執行。每次成功的 `run` 都會針對其完成 selection 繼續設定的
plan 與 integration workflow，但不會 accept。

## Runtime test

執行一個 live plan 的獨立 runtime-test workflow：

```text
assent test <PLAN>
```

Plan 形式讀取 `.assent/<PLAN>/_runtime_test.toml`，在 plan candidate worktree
執行其中宣告的 `command`；其值可以是單一 string 或有序 string array。Array 在
第一個失敗 command 停止，後續項目記為 not run。`execution = "disabled"` 時會拒絕
這個 plan command。`execution = "after_plan"` 會在 `assent run` 中自動執行，因此
不需要人類另外 test；`execution = "explicit"` 才是 `run` 後執行
`assent test <PLAN>` 的一般理由。

省略 `PLAN` 時，讀取 main runtime contract `.assent/_runtime_test.toml`，直接處理
目前的 primary working tree：

```text
assent test
```

`assent init` 會把這份 contract 建立為 `execution = "pending"`。第一次執行
`assent test` 時不會猜測或執行 command；下一個已設定的可寫 runtime role 會檢查
提供給操作人員的文件與已實作的 production entrypoint。只有能唯一判定時，才提出
`explicit` 與使用正常持久化 production 設定及資料的精確、有限 production
operation。Unit test、test runner、mock、fixture、專用 probe、暫存或 in-memory
resource、sample invocation、刻意限縮範圍的 invocation 都不符合。Assent 先還原
role 對 control file 的直接修改、驗證提案、顯示每一條完整 command，再詢問一次
是否寫入並執行。只有 `y` 或 `yes` 會寫入；拒絕或 stdin EOF 會維持 `pending`，也
不執行任何 command。一條 shell command 必須是一個包含 executable 與所有參數的
完整字串；array 表示多條依序執行的完整 command line，不是 argv token array。
寫入後，之後的 `assent test` 直接執行已保存的 command，
不再詢問。提案無效時直接拒絕；缺少或無法唯一判定 operation 時，有限 workflow
以 unresolved 結束。這些結果都回傳非 0。

`test` 只啟動獨立的 `runtime_test` workflow，不會執行 task、plan、integration、
`full_verify` 或 `accept`。完整的 mode、state、repair、quota 與 source-bound
evidence 規則見[工作流程](WORKFLOW.md)；repair role 的設定與 adapter 範例見
[設定](CONFIGURATION.md)。

## 可選檢查與手動驗證

需要結構化的人類驗收摘要時再執行：

```text
assent report <PLAN>
```

手動更新單一 receipt 或驗證明確選取：

```text
assent verify <PLAN>
assent verify A B
```

只跑單一 task check 或 plan 內 `DONE` tasks 的 focused sweep，不寫 receipt：

```text
assent verify <PLAN> --focus t003
assent verify <PLAN> --focus
```

明確執行的 `verify` 不會進入設定的 workflow role 或自動修復；失敗會直接回傳給
呼叫者。

動態驗證目前所有符合條件的計畫：

```text
assent verify --batch
```

明確選取必須整組成功，遇到衝突就拒絕。動態 batch 回報衝突後，可以詢問是否只
驗證其餘互不衝突的計畫。

## 接受、復原與封存

正常成功流程是：

```text
assent accept <PLAN>
assent archive <PLAN>
```

直接或明確選取的 `accept` 不會執行驗證。`accept --all` 可以重播一份新鮮 batch
receipt；若沒有可用 batch 證據，則逐一驗證並接受，遇到第一個失敗就停止。

如果結果需要介入，再使用：

```text
assent rework <PLAN> <TASK>
assent reject <PLAN>
assent reconcile <PLAN>
```

要保留並修改實作時使用 `rework`。`reject` 先把 worktree HEAD 與 branch tips 記錄
到 `_reject.toml`，再拋棄該 plan 的實作。這份 journal 只在 commit objects 尚存時
支援人工、best-effort 的 Git 復原；重新執行 `reject` 只處理當下仍存在的狀態，
不會重建已刪除的 branch。

`archive` 嚴格包含安全清理：如果 source branch/worktree 仍存在，它會重用 `clean`
的證明與移除流程，再壓縮並封存 live plan。因此 archive 前不需要另跑 `clean`。
只有想清理但不封存時才使用：

```text
assent clean
assent clean <PLAN>
```

明確指定的 archive 若不符合條件會回報錯誤；`--all` 則略過不符合者。兩者都沒有
強制刪除模式。

完整流程請看[工作流程](WORKFLOW.md)，receipt 與衝突請看[驗證](VERIFICATION.md)，
復原與清理安全請看[作業](OPERATIONS.md)。
