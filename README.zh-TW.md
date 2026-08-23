# assent — 與 AI 規劃、自動執行、由人驗收

*[English](README.md)*

> 本文是英文版的正體中文翻譯；若內容不同，以英文版為準。

Assent 把人類與 AI 談妥的設計，轉成可以隔離執行、重複驗證的工作。你先與
AI 確認需求，再請 AI 將討論共識建立成 Assent 格式計畫。接著讓 `assent run`
實作與測試，最後閱讀證據，親自決定是否接受。

原始碼仍是一般 Git 專案；Assent 的計畫與執行紀錄放在專案內被忽略的
`.assent/` 目錄。

## 核心流程

| 階段 | 人類要做的事 | 主要指令 |
| --- | --- | --- |
| 規劃 | 與 AI 確認需求後，明確要求：「將上述討論的共識，建立成 `.assent/<PLAN>/` 下的 Assent 格式計畫。」 | `assent check` |
| 執行 | 讓 task、plan、integration 三層流程在有限次數內實作、測試與修復。 | `assent run` |
| 驗收 | 閱讀報告與 diff，再接受、重做或駁回。 | `assent report`、`assent accept` |

`DONE` 只表示執行 AI 認為任務完成；通過的 receipt 只表示重建後的結果通過
完整驗證。兩者都不是人類批准，只有 `assent accept` 會正式整合成果。

## 安裝

需要 Python 3.11+、Git，以及已安裝並登入的 AI CLI，例如 Claude 或 Codex。
Assent 只使用 Python 標準函式庫。

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

在既有 Git 專案根目錄執行：

```text
assent init --test unittest

# 先與 AI 確認需求；取得共識後，再請 AI 將討論共識建立成
# .assent/<PLAN>/ 下的 Assent 格式計畫。
assent check

# 自動執行所有找到的計畫。
assent run

# 閱讀證據後，再由人決定是否接受。
assent report <PLAN>
assent accept <PLAN>

# 需要時才清除多餘 worktree 或封存已完成計畫。
assent clean <PLAN>
assent archive --all
```

`assent init` 會把共用設定與三份 AI 契約安裝到 `~/.assent/`，建立專案骨架，
並詢問專案的完整驗證方式。第一次執行前，請檢查
`~/.assent/assent.toml`、`AGENTS.md` 與 `.assent/verify.py`。

## `run` 會做什麼

`[workflow]` 分成三層：

- `task` 處理單一任務，並以 `focused_test` 作為機械檢查；
- `plan` 等所有任務完成或略過後，以 `focused_sweep` 檢查整體成果；
- `integration` 重建本次明確選取的結果，再執行 `full_verify`。

檢查一通過，該層就立即結束，不會再開 repair role。失敗時，才會進入下一個
已設定的 repair role，並由後續 action 重新檢查。預設 repair role 會在一個
session 內合併審查與修復；自訂 workflow 仍可使用分離的 role。設定陣列就是全部
修復次數；Assent 不會自行追加回合。若自動流程無法安全判斷，會保留所有成果，
並回報 `REVIEW UNRESOLVED, HUMAN DECISION`，交給驗收會議決定。

Task action 失敗時仍留在 task 層，並依設定的有限 steps 前進。Plan role 的工作
不同：它負責確認累積實作是否符合整份計畫。

Integration 會維持原本選取的完整計畫集合。Typed Git conflict evidence 會指出
衝突的 plan 與 paths，因此已設定的 integration role 可以在 scheduler 提供的
reconcile 或 source worktree 中修復，再由 `full_verify` 重建 candidate。沒有機械式
source attribution 的 multi-plan verifier failure 才交由人類決定。Assent 不會移除
某個 plan、先接受已通過的前半段，也不會自行執行 `accept`。

## 文件

- [工作流程](docs/zh-TW/WORKFLOW.md)：規劃、自動執行與驗收會議。
- [指令](docs/zh-TW/COMMANDS.md)：選取規則與常用指令。
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
