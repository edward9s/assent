# assent — 與 AI 規劃、自動執行、由人驗收

*[English](README.md)*

> 本文是英文版的正體中文翻譯；若內容不同，以英文版為準。

Assent 把人類與 AI 談妥的設計，轉成可以隔離執行、重複驗證的工作。規劃會議
可以使用任何 AI 或其他方式；只要討論最後產生 Assent 格式計畫，Assent 才接手
後續工作。接著讓 `assent run` 自動實作與驗證，按 plan 需要執行 runtime test，
最後由人決定是否接受。

原始碼仍是一般 Git 專案；Assent 的計畫與執行紀錄放在專案內被忽略的
`.assent/` 目錄。

## 人類正常流程

一般使用者真正需要操作的流程很小：

| 階段 | 會發生什麼 | 主要指令 |
| --- | --- | --- |
| 初始化 | 安裝 Assent 共用契約與設定，建立專案骨架。 | `assent init` |
| 規劃 | 使用你自己選擇的 AI 討論需求。Planning AI 建立 `.assent/<PLAN>/`，並在結束會議前自行執行 `assent check` 直到通過。 | AI 負責 `assent check` |
| 執行 | 讓 Assent 在有限流程內實作、測試、修復與驗證計畫。 | `assent run` |
| Runtime test | Plan 為 `explicit` 時執行宣告的 runtime workflow；`after_plan` 會在 `run` 中自動執行。 | `assent test <PLAN>` |
| 接受 | 由人依相符證據決定是否發布成果。 | `assent accept <PLAN>` |
| 封存 | 結束這份 plan；archive 會先執行與 `clean` 相同的安全清理，再封存紀錄。 | `assent archive <PLAN>` |

正常情況下，人類**不需要自己執行 `assent check`**；它主要是 planning contract
要求 AI 完成的 validation gate。`archive` 前也不需要另外執行 `assent clean`。

`DONE` 只表示執行 AI 認為任務完成；通過的 receipt 只表示重建後的結果通過
完整驗證。兩者都不是人類批准。`assent accept` 會把人類批准的精確驗證結果
發布到目前 target branch；它不會替你 push 到 GitHub。

## 安裝

需要 Python 3.11+、Git，以及供自動執行使用、已安裝並登入的受支援 AI CLI，
例如 Claude 或 Codex。規劃階段本身不綁定這個選擇：你可以使用任何 AI 或流程
談妥需求。Assent 只使用 Python 標準函式庫。

```text
python -m pip install assent
```

解除安裝：

```text
python -m pip uninstall assent
```

解除安裝只會移除套件與 CLI，不會刪除 `~/.assent`、專案的 `.assent/`、
worktree、archive 或 Git branch。清理資料必須由人明確執行。

## 快速開始

先在既有 Git 專案根目錄執行一次：

```text
assent init
```

接著使用你自己選擇的 AI 開規劃會議。Assent 不會替你啟動或指定 planning AI。
取得明確需求共識後，由 AI 建立 Assent plan，並在結束會議前自行驗證。例如：

```text
請和我一起規劃這項變更。先讀 AGENTS.md、~/.assent/instructions.md 與
~/.assent/format.md。在我明確同意前不要建立 plan file。
將上述討論的共識，建立成 `.assent/<PLAN>/` 下的 Assent 格式計畫。
配置完整 verification 與 runtime decisions，最後反覆執行 assent check
直到通過後才能結束會議。
```

會議結束後，人類正常流程是：

```text
assent run my-plan
assent test my-plan
assent accept my-plan
assent archive my-plan
```

把 `my-plan` 換成 `.assent/` 下實際建立的 plan 目錄名稱。
`assent test my-plan` 用於 plan 宣告 `execution = "explicit"` 的情況；如果是
`after_plan`，runtime testing 已由 `assent run` 自動執行；如果是 `disabled`，則
沒有 runtime gate。

`assent archive my-plan` 會先執行與 `assent clean` 相同的機械式安全清理證明，
移除可安全刪除的 source worktree/branch，再壓縮並封存 live plan 紀錄。因此只有
在「想先清 source、但暫時不封存 plan」時，才需要單獨使用 `clean`。

要使用 whole-project scheduler 時，可以省略 plan 名稱：

```text
assent run
assent run --jobs 2
```

`assent report`、`status`、`verify`、`rework`、`reject`、`reconcile`、`clean` 與
`ignored-inputs` 仍提供檢查、手動驗證、復原與進階流程使用；它們不是正常 happy
path 額外必走的步驟。

`assent init` 會把共用設定與三份 AI 契約安裝到 `~/.assent/`，並在不詢問 command
的情況下建立專案骨架；新建的 `.assent/verify.py` 預設 fail-closed。第一次 live
plan 結束規劃前，planning AI 會配置完整的 project-test block、選擇該 plan 的
`_runtime_test.toml`、按需要加入 project runtime-test workflow，並反覆執行
`assent check` 直到通過。

## `run` 會做什麼

概念上，`assent run` 會讓已設定的 AI roles 處理指定 plans，在修復嘗試之間執行
機械檢查，驗證重建後的結果，最後停下來交給人驗收，不會自行接受成果。

`[workflow]` 有 preflight repair layer、三個核心 layer，另有獨立的
runtime-test workflow：

- `preflight` 會執行完整的唯讀 check，只有失敗時才啟動 AI repairer；
- `task` 處理單一任務，並以 `focused_test` 作為機械檢查；
- `plan` 等所有任務完成或略過後，以 `focused_sweep` 檢查整體成果；
- `integration` 重建本次明確選取的結果，再執行 `full_verify`。

檢查一通過，該層就立即結束，不會再開 repair role。失敗時，才會進入下一個
已設定的 repair role，並由後續 action 重新檢查。預設 repair role 會在一個
session 內合併審查與修復；自訂 workflow 仍可使用分離的 role。設定陣列就是全部
修復次數；Assent 不會自行追加回合。若自動流程無法安全判斷，會保留所有成果，
並回報 `REVIEW UNRESOLVED, HUMAN DECISION`，交給人類決定。
明確執行 `assent check` 仍為唯讀；只有 `assent run` 會進入已設定的 preflight
repair workflow。

Task action 失敗時仍留在 task 層，並依設定的有限 steps 前進。Plan role 的工作
不同：它負責確認累積實作是否符合整份計畫。

`assent test [PLAN]` 是獨立的 runtime-test workflow。有 `PLAN` 時，使用該 live
plan 的 `_runtime_test.toml` 單一 command 或有序 command array，在 plan candidate
中執行；省略 `PLAN` 時，使用 project layer 的 `[runtime_test].command`，直接在
目前 primary working tree 執行。`execution = "after_plan"` 的 plan 會在 plan layer
完成後、integration `full_verify` 前執行這個 workflow；`accept` 絕不執行 runtime
testing。Array 會在第一個失敗 command 停止；repair evidence 會指出該 command，
下一個 runtime action 從頭重跑。

Integration 會維持原本選取的完整計畫集合。Typed Git conflict evidence 會指出
衝突的 plan 與 paths，因此已設定的 integration role 可以在 scheduler 提供的
reconcile 或 source worktree 中修復，再由 `full_verify` 重建 candidate。沒有機械式
source attribution 的 multi-plan verifier failure 才交由人類決定。Assent 不會移除
某個 plan、先接受已通過的前半段，也不會自行執行 `accept`。

## 文件

- [工作流程](docs/zh-TW/WORKFLOW.md)：規劃、自動執行、runtime testing、驗收與封存。
- [指令](docs/zh-TW/COMMANDS.md)：人類正常流程、各指令角色與 selection 規則。
- [設定](docs/zh-TW/CONFIGURATION.md)：初始化、adapter、model 與 workflow。
- [驗證](docs/zh-TW/VERIFICATION.md)：focused/full test、receipt、衝突與共用輸入。
- [作業](docs/zh-TW/OPERATIONS.md)：worktree、復原、清理與封存。

英文文件是正式版本，正體中文是讀者翻譯。AI 使用的契約與人類指南刻意分開：
`instructions.md` 是 session 規則，`format.md` 定義計畫檔，`workflow.md`
定義 scheduler 與驗收行為。

## 安全邊界

- Assent 會保留失敗或中斷的成果，不會自動還原。
- AI role 不得修改 task contract、scheduler state、Git state、receipt 或
  acceptance state。
- 完整驗證使用臨時 integration candidate，不會變更 target ref。
- 清理 junction 或 directory symlink 時，絕不穿越外部 target。
- Worktree 用來隔離與記錄變更，不是安全 sandbox。
- `reject` 是需要確認的破壞性動作；若要保留現有程式碼，請用 `rework`。
- 驗證通過不等於接受；最後決定永遠由人做。
