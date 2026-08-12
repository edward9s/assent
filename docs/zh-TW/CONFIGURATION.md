# 設定

*[English](../CONFIGURATION.md) · [README](../../README.zh-TW.md)*

> 本文是[英文版](../CONFIGURATION.md)的正體中文翻譯；若內容不同，以英文版為準。

Assent 需要 Python 3.11+、Git，以及至少一套已安裝並登入的 AI CLI。Python 套件
本身不使用第三方 runtime dependency。

## 檔案與優先順序

共用檔案放在 `~/.assent/`：

```text
assent.toml       scheduler 設定
adapter.toml      AI CLI 指令與 model 對應
instructions.md  AI session 規則
format.md         計畫檔契約
workflow.md       scheduler 與驗收契約
```

專案自己保有 `AGENTS.md`、`.assent/verify.py`、各份計畫，以及可選的
`.assent/assent.toml` override。

設定優先順序依次是內建預設、使用者設定、專案 override，以及該指令支援的命令列
override。Table 依 key 合併；scalar 與 array 整個取代較低層的值。省略才會繼承，
空 array 則是明確的空值。

`assent init` 會補入新版新增的設定，但保留現有值與註解。它不會擅自用新版預設
取代既有 workflow；請讀完本指南後自行決定是否採用。

## 從 workflow 理解設定

設定的核心關係是：

```text
ability：prompt + 權限
        ↓
role：一個或多個 ability + model/effort
        ↓
workflow：依序排列 role 與 scheduler action
```

Ability 說明一個 AI session 負責什麼；role 組合一個或多個 ability；workflow
決定 role 何時執行，以及哪個機械式結果會啟動下一次修復。`reviewer`、`fixer`
之類的名稱只是方便人類閱讀，engine 不會從名稱推斷權限。

### Ability

```toml
[abilities.task_review]
prompt = "Review only the current task's failure evidence."
writes = false
produces_verdict = true
```

- `prompt` 會附加到 session 指示中。它應明確限定這一層的責任；詳細協定與失敗
  證據由 scheduler 提供。
- `writes` 表示 role 能否在 scheduler 授權的範圍內修改 source。Reviewer 之所以
  唯讀，是因為這個能力值，而不是它的名稱。
- `produces_verdict = true` 表示 role 必須回傳結構化審查結果；預設為 `false`。

Prompt 不會擴張 scope、Git 權限或執行驗證的權限；scheduler 規則仍優先適用。

### Role

```toml
[roles.task_reviewer_fixer]
ability = ["task_review", "task_fix"]
model = "prime"
effort = "heavy"
```

`ability` 是不可為空的有序清單。只要其中一個 ability 可以寫入，role 就可以寫入；
只要其中一個會產生 verdict，role 就必須產生 verdict。一般 task role 的 `model`
與 `effort` 可以省略，屆時沿用 task 的設定。`workflow.plan` 與
`workflow.integration` 的 verdict role 必須明示兩者，讓審查責任可以重現。

### Scheduler action

Action 由 Assent 在 AI session 外執行：

| Workflow | Action | 檢查內容 |
| --- | --- | --- |
| `task` | `focused_test` | 目前 task 的 `verify` 指令 |
| `plan` | `focused_sweep` | 一份已完成 plan 中不重複的 focused command 聯集 |
| `integration` | `full_verify` | 依精確 plan 選集重建的整合候選版本 |

每種 action 只能放在對應的 array。AI role 不會自行執行這些 action，也不會執行
`.assent/verify.py`。

## Array 如何執行

Action 一旦通過，該層立刻完成，後面的項目全部略過。Action 失敗才會前進到下一個
修復 role，再由下一個 action 檢查修復結果。因此 array 同時代表執行順序與有限的
修復次數。用完仍無法解決時，Assent 會保留證據與修改，標記為
`REVIEW UNRESOLVED, HUMAN DECISION`，不會丟棄工作成果。

兩個 action 之間只能使用以下兩種形式：

```toml
# 同一個 session 完成審查與修復。
{ action = "focused_sweep" },
{ role = "plan_reviewer_fixer" }, # writes + produces_verdict
{ action = "focused_sweep" },

# 唯讀審查與可寫入修復分成兩個 session。
{ action = "focused_sweep" },
{ role = "plan_reviewer" },       # produces_verdict，不可寫入
{ role = "plan_fixer" },          # 可寫入，不產生 verdict
{ action = "focused_sweep" },
```

可寫入的 verdict role 必須是兩個 action 之間唯一的 role，由同一個 session 完成
診斷與修復。唯讀 verdict role 則可接一個可寫入、但不產生 verdict 的 fixer。
這些規則只看能力值與排列位置，不看 role 名稱。

## 三層修復責任不同

三層回答的是不同問題，因此應使用不同的 ability 與 prompt：

| 階段 | 修復範圍 |
| --- | --- |
| `workflow.task` | 處理目前 task 的 `BLOCKED` 或 `focused_test` 證據。它可以補救規劃時的小疏漏，例如漏列一條精確 scope path，並在同一個可寫入 verdict session 內完成修復；不會消耗 plan 的修復次數。 |
| `workflow.plan` | 檢查所有已完成 task 累積出的 worktree 是否符合既有 plan。它處理 `focused_sweep` 失敗與跨 task regression，並透過受影響的既有 task 修復。 |
| `workflow.integration` | 檢查同一份精確 plan 選集能否重建並通過 `full_verify`。它處理候選版本衝突與完整驗證失敗，不可刪掉某個 folder，也不可只接受能成功的前綴。 |

Integration workflow 只負責驗證與修復，不負責人類驗收。發布仍須稍後明確執行
`assent accept`。

### 預設 workflow

內建設定讓每個 task 先由一個實作 session 處理，三個修復層級各自使用專責的
reviewer/fixer：

```toml
[abilities.write_tests]
prompt = "Write or update tests that prove the supplied requirements."
writes = true

[abilities.implement_source]
prompt = "Implement the supplied requirements and satisfy the supplied focused checks."
writes = true

[abilities.task_review]
prompt = "Resolve only the current task's BLOCKED or focused_test evidence. Diagnose a task-local planning omission; when one exact scope path was omitted, identify it without inventing requirements."
writes = false
produces_verdict = true

[abilities.task_fix]
prompt = "In the same session, repair every authorized task-local finding, including an exact omitted scope path. Do not create tasks or requirements."
writes = true

[abilities.plan_review]
prompt = "Review only focused_sweep failure evidence to decide whether the cumulative worktree conforms to the existing plan, including cross-task interactions and concrete regressions."
writes = false
produces_verdict = true

[abilities.plan_fix]
prompt = "Repair every authorized plan-level finding through its implicated existing tasks. Do not create tasks or requirements."
writes = true

[abilities.integration_review]
prompt = "Review only the exact selection's candidate-conflict or full_verify failure evidence. Identify every integration blocker without shrinking the selection, accepting a prefix, or inventing requirements."
writes = false
produces_verdict = true

[abilities.integration_fix]
prompt = "Repair every authorized integration finding in the scheduler-provided workspaces while preserving the exact selection. Do not run Git, Assent, focused tests, full verification, or accept."
writes = true

[roles.implementer]
ability = ["write_tests", "implement_source"]

[roles.task_reviewer_fixer]
ability = ["task_review", "task_fix"]
model = "prime"
effort = "heavy"

[roles.plan_reviewer_fixer]
ability = ["plan_review", "plan_fix"]
model = "prime"
effort = "heavy"

[roles.integration_reviewer_fixer]
ability = ["integration_review", "integration_fix"]
model = "prime"
effort = "heavy"

[workflow]
task = [
  { role = "implementer" },
  { action = "focused_test" },
  { role = "task_reviewer_fixer" },
  { action = "focused_test" },
]
plan = [
  { action = "focused_sweep" },
  { role = "plan_reviewer_fixer", adapter = "codex" },
  { action = "focused_sweep" },
  { role = "plan_reviewer_fixer", adapter = "codex" },
  { action = "focused_sweep" },
]
integration = [
  { action = "full_verify" },
  { role = "integration_reviewer_fixer", adapter = "codex" },
  { action = "full_verify" },
]
```

第一個通過的 `focused_test`、`focused_sweep` 或 `full_verify` 會略過自己 array
中的後續項目。重複列出的 plan review 是不同修復回合，不代表每次都必須執行。

## 省略設定與 task override

- 省略 `workflow.task` 時，每個 task 會依自己的 model 與 effort 執行一個隱含
  session。
- 非空的 task workflow 可以先排列 worker role，再執行 `focused_test`；只要包含
  這個 action，最後一項就必須是它。Worker 回傳 `BLOCKED` 時，會帶著既有證據
  直接前進到下一個 verdict role。
- `workflow.task = []` 會停用逐 task session，改由 plan workflow 把整份 plan
  當成一個單位執行；此時 plan workflow 不可為空。
- 省略或設空 `workflow.plan`，表示不執行 plan review。
- 省略或設空 `workflow.integration`，表示停用自動 integration repair。

Task 檔可以只覆寫自己的 task sequence：

```toml
[roles.test_writer]
ability = ["write_tests"]

[roles.source_implementer]
ability = ["implement_source"]

workflow = [
  { role = "test_writer" },
  { role = "source_implementer" },
  { action = "focused_test" },
]
```

省略時繼承 `[workflow].task`；`workflow = []` 則把這個 task 交給 plan-wide
execution。Override 使用的 role 仍須定義在有效的 `[roles]` 設定中。

## Adapter、model 與 effort

`[adapter].name` 可指定一個 adapter，或指定依序輪替的清單。內建支援 Claude、
Codex 與 Antigravity；各自的指令、參數、可攜 model 對應、預設 effort，以及
vendor effort 轉換都放在 `adapter.toml`。無人值守執行前要先登入各 CLI；Assent
只使用既有認證，不管理 secrets。

Plan 使用可攜的 `prime`、`core`、`lite` model tier；effort 是另一個獨立選擇：
`heavy`、`normal`、`slight`。解析順序為 task 或 role 明示值、該 model tier 的
設定預設值、內建 tier 預設值。每次 invocation 都會取得轉換後的具體 effort。

Plan 與 integration workflow entry 可以指定 adapter：

```toml
{ role = "plan_reviewer_fixer", adapter = "codex" }
```

省略時使用設定清單中的第一個 adapter。Task workflow entry 不接受 `adapter`
欄位，而是沿用一般 task 的 adapter 選擇與輪替方式。

## 初始化與排錯

初始化專案時要選真正的完整 verifier，然後檢查產生的 script：

```text
assent init --test unittest
assent doctor
```

Task 檔應使用範圍較小的 focused command。設定有誤時，diagnostic 會指出錯誤的
key 與來源檔。也請確認 Git 可用、選定的 AI CLI 已登入，而且 model mapping
使用該 CLI 接受的名稱。

人類流程請看[工作流程](WORKFLOW.md)，CLI 用法請看[指令](COMMANDS.md)，執行期間
復原請看[作業](OPERATIONS.md)。
