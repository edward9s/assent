# 指令

*[English](../COMMANDS.md) · [README](../../README.zh-TW.md)*

> 本文是 [英文版](../COMMANDS.md) 的正體中文翻譯；若內容不同，以英文版為準。

完整 option 請直接執行 `assent <command> --help`。本文只說明如何選指令，以及
plan selection 的重要規則。

## Plan selection

`PLAN` 是專案 `.assent/` 直下的目錄名稱，不是路徑；例如 `demo` 代表
`.assent/demo/`。其中必須至少包含一份正式 `.e.toml` task。Assent 會在任何動作
開始前檢查所有 plan 名稱；若有錯，會一次列出完整清單，不會執行其中一部分。

選到一個 plan 就走單一 plan 流程；兩個以上就是一組精確 batch。明確選取的
accept 仍需要與整組完全相符的證據，而且不會啟動驗證。

`run`、`status`、`check`、`report`、`verify`、`clean`、`archive`、`accept`、
`reconcile`、`reject`、`rework` 支援各自的 `--config PATH` option。它會選擇專案
設定並定位專案，不是 top-level global option。`init`、`doctor` 與 `ignored-dirs`
各有自己的專案位置規則。

## 指令用途

| 指令 | 用途 |
| --- | --- |
| `init` | 安裝共用契約與設定，建立專案骨架。 |
| `check` | 不開 AI，檢查計畫、設定與相依關係。 |
| `run` | 執行 task、plan 與 integration workflow。 |
| `test` | 執行 plan 宣告的 runtime command，或在目前 main candidate 執行 project command。 |
| `status` | 查看一個或全部計畫的簡要狀態。 |
| `report` | 重新產生人類驗收用的報告。 |
| `verify` | 執行指定的機械驗證，不啟動 AI review、repair 或 accept。 |
| `accept` | 人類依相符證據發布成果。 |
| `reconcile` | 準備並完成由人編輯的 Git 衝突修復。 |
| `rework` | 保留程式碼，重新開啟既有 task。 |
| `reject` | 記錄可復原的 Git 證據後，經人確認執行破壞性重設。 |
| `clean` | 只移除已證明多餘的 worktree/branch。 |
| `archive` | 安全清理後封存完成的管理紀錄。 |
| `doctor` | 診斷安裝並復原孤兒暫存 branch。 |
| `ignored-dirs status` | 查看目前 worktree 的 ignored-directory 決定與鏈結，不做任何變更。 |
| `ignored-dirs declare` | AI source role 記錄審查結果的 operation；只為必要目錄建立鏈結。 |

## 初始化專案

`assent init` 會安裝共用契約與設定，並建立 fail-closed 的
`.assent/verify.py` 骨架，不詢問 verification 或 runtime command。更新 framework
時會保留既有 project-owned verifier command block，並原樣保留
`.assent/assent.toml`。Planning meeting 必須在最後一次 `assent check` 前配置
verifier 與 plan runtime decision。

## 常見用法

排程所有找到的 plan：

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
第一個失敗 command 停止，後續項目記為 not run。`execution = "disabled"` 時，這個 plan command
會被拒絕。省略 `PLAN` 時，則使用 `.assent/assent.toml` project layer 的
`[runtime_test].command`，直接測試目前的 primary working tree：

```text
assent test
```

`test` 只啟動獨立的 `runtime_test` workflow，不會執行 task、plan、integration、
`full_verify` 或 `accept`。完整的 mode、state、repair、quota 與 source-bound
evidence 規則見[工作流程](WORKFLOW.md)；main command 與 repair role 的設定見
[設定](CONFIGURATION.md)。

更新單一 receipt 或驗證明確選取：

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

驗收與後續決定：

```text
assent report <PLAN>
assent accept <PLAN>
assent rework <PLAN> <TASK>
assent reject <PLAN>
```

直接或明確選取的 `accept` 不會執行驗證。`accept --all` 可以重播一份新鮮 batch
receipt；若沒有可用 batch 證據，則逐一驗證並接受，遇到第一個失敗就停止。

需要時才清理或封存：

```text
assent clean
assent clean <PLAN>
assent archive <PLAN>
assent archive --all
```

明確指定的 archive 若不符合條件會回報錯誤；`--all` 則略過不符合者。兩者都沒有
強制刪除模式。

完整流程請看[工作流程](WORKFLOW.md)，receipt 與衝突請看[驗證](VERIFICATION.md)，
復原與清理安全請看[作業](OPERATIONS.md)。
