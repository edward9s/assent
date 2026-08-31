# 設定

> 本文是 [英文版](../CONFIGURATION.md)（English）的正體中文翻譯；若內容不同，以英文版為準。

Assent 依序讀取內建預設值、`~/.assent/assent.toml` 與專案的
`.assent/assent.toml`。Table 依 key 合併；scalar 與 array 會整體取代繼承值。

## Model 與 adapter

Task file 只使用 `prime`、`core`、`lite` 三種可攜 tier。每個 adapter 把 tier
對應到一個完整的 `model/effort`：

```toml
[adapter.codex.models]
prime = "gpt-5.6-sol/high"
core = "gpt-5.6-luna/max"
lite = "gpt-5.6-luna/high"
```

第一個 `/` 分隔 model 與 effort。未包含 `/` 時不傳 effort，沿用供應商 CLI
預設值。專案自訂 mapping 會整體取代該 adapter 的內建 mapping，因此三個 tier
都必須存在。

Role 可以填 tier，也能直接填供應商 selection。供應商 selection 只適用於該
workflow step 明確解析到單一 adapter 的情況。

## Ability

Ability 只有 prompt 與寫入能力：

```toml
[abilities.plan_review]
prompt = "Inspect the cumulative candidate for correctness and simplicity."
writes = false

[abilities.plan_fix]
prompt = "Use the requirements to determine whether the defect is in tests or implementation, then correct whichever is wrong without weakening correct tests."
writes = true
```

`prompt` 描述責任；`writes` 決定 role 能否修改一般 candidate file。Ability 名稱
對 engine 沒有特殊意義。Prompt 不會授予 Git、task contract、journal、receipt、
scheduler action 或 acceptance 權限。

## Role

Role 組合一個或多個 ability，也可以選擇 model：

```toml
[roles.plan_repairer]
ability = ["plan_review", "plan_fix"]
model = "core"
```

任一組成 ability 若有 `writes = true`，整個 role 就能寫入。Adapter selection
屬於 workflow entry：`adapter = "codex"` 指定單一 adapter，`adapter = [...]`
指定依序嘗試的可用性清單；省略時使用全域 adapter rotation。

Task session 的 model 優先序是 workflow entry model > role model > task file model。Task-local
workflow entry 只能包含 role 或 action，因此其具名 role 會直接 fallback 到 task
tier。Plan 與 integration session 沒有 task fallback；role 或 workflow entry 必須
指定 model。

Workflow entry 指定可攜 tier 但省略 `adapter` 時，全域 rotation 中的每個 adapter
會各自解析該 tier。若指定供應商 `model/effort` 卻省略 `adapter`，只有全域 rotation
恰好包含一個 adapter 時才有效；否則設定載入會因語意不明而拒絕。

## Workflow

`[workflow]` 包含 preflight repair array、三個核心且長度有限的 step array，
另有獨立的 runtime-test array：

```toml
[workflow]
preflight = [
  { action = "check" },
  { role = "preflight_repairer" },
  { action = "check" },
]
task = [
  { role = "implementer" },
  { action = "focused_test" },
  { role = "task_repairer" },
  { action = "focused_test" },
]
plan = [
  { role = "plan_quality_repairer" },
  { action = "focused_sweep" },
  { role = "plan_repairer" },
  { action = "focused_sweep" },
]
integration = [
  { action = "full_verify" },
  { role = "integration_repairer" },
  { action = "full_verify" },
]
runtime_test = [
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
]
```

每個 entry 只能包含一個 `role` 或 `action`：

| Array | Action | 意義 |
| --- | --- | --- |
| `preflight` | `check` | 執行完整的唯讀 plan 與環境檢查。 |
| `task` | `focused_test` | 執行目前 task 的 `verify` command。 |
| `plan` | `focused_sweep` | 在累積 candidate 上執行各個不重複的 task command。 |
| `integration` | `full_verify` | 重建並驗證精確選集。 |
| `runtime_test` | `runtime_test` | 在其 candidate 執行宣告的 runtime command。 |

Role session 成功就前進一格。Action 通過就完成該層並略過後續 step；action
失敗則前進。走完仍未通過時，結果是 `REVIEW UNRESOLVED, HUMAN DECISION`，
exit zero，並保留所有證據。

`preflight` 嚴格交替 `check` action 與可寫 repair role，並以 action 開始和結束。
`assent run` 會在 task 執行前進入此層；第一個 check 通過就略過所有 repair role。
Repairer 只能修改 check evidence 指名的宣告式 Assent input，不得修改 status、
workflow cursor、scheduler evidence、receipt、Git、candidate source 或 acceptance。
最後仍失敗時，`run` 以 nonzero 停止。明確執行 `assent check` 仍為唯讀，而且
不會進入這個 workflow。

設定中沒有 structured verdict。Reviewer、fixer 或合併責任完全由 ability 決定。
Sessions 依序執行且不互相對話；scheduler 把有限長度的輸出與 action evidence
交給下一個 step。因此，相鄰的 reviewer 與 fixer role 會在 reviewer 正常結束後
依序執行。Scheduler 將 role 輸出存進 `.assent/<PLAN>/_workflow.toml`，再注入下一個
role 的 prompt；它不會依這段文字分支。

Effective `task` array 必須存在且不可為空。Project override 只有在繼承較低設定層
時才能省略它。Action-only task array 只執行機械驗證，不開啟 AI session。省略或
清空 `plan` 不會建立 plan session。省略或清空 `integration` 不會建立 integration
role session；完成的 run 選集仍會執行 `full_verify`。非空 layer 若以 role 結尾，
Assent 會在尾端補一次該層 action。

可寫入的 task 與 plan role 能修改任何為滿足需求所需的一般 candidate source、
test、configuration 或 documentation file。沒有 task path scope，也沒有 finding
ownership。單一 plan 的 integration repair 使用該 plan 的 source worktree；多 plan
candidate 失敗時，因為沒有唯一可修改的 source branch，交由人類決定。

## Task workflow override

Task 可以用只含 `role` 或 `action` 的 task-local entry 取代專案 task array：

```toml
workflow = [
  { role = "tests_writer" },
  { role = "source_implementer" },
  { action = "focused_test" },
]
```

省略欄位會繼承 `[workflow].task`；空 task workflow 無效。不需要 AI session 時，
明確寫成 `workflow = [{ action = "focused_test" }]`。

## Runtime-test 設定

Runtime testing 有自己的 workflow layer，不會重用 task、plan 或 integration
action。共用設定提供可寫入的 `runtime_repairer` role；project template 提供由
`runtime_test` action 與該 repair role 嚴格交替組成的 `[workflow].runtime_test`
array。自訂 runtime role 必須可寫入並明確指定 model；array 頭尾都是 action。

Main candidate 的 command 是 project-specific，必須寫在 project 的
`.assent/assent.toml`：

```toml
[runtime_test]
command = "python -m unittest tests.test_runtime"
```

多個有序 command 仍使用同一個單數 key，值改用 array：

```toml
[runtime_test]
command = ["python tools/probe_a.py", "python tools/probe_b.py"]
```

`assent init` 會先詢問是否建立這份 project file，再詢問各 command，並從 packaged
project template 產生 command 值與 runtime-test workflow。Ability 與 role 定義
仍繼承自 `~/.assent/assent.toml`。

這個 `[runtime_test].command` 接受一個非空 string，或由非空 string 組成的非空
array；只由不帶 `PLAN` 的 `assent test` 使用。每個 plan
則自行寫入 `_runtime_test.toml` contract；精確的 `execution` mode 與 `command`
存在規則見 `format.md`。Plan command 不會 fallback 到 `task.verify` 或 project
command。

Runtime repair ability 可以在 candidate 中修改為滿足需求所需的一般 source、test、
fixture、project configuration 與 documentation；不能執行 command 或修改
Assent/Git control state，文字也不能宣告 runtime pass。完整 runtime workflow 與
candidate lifecycle 見[工作流程](WORKFLOW.md)。

## Usage limit

供應商 usage 是衍生的觀察證據，不會裁決 task 正確性。Adapter quota 與
authentication handling 會保留 WIP checkpoint，並依序切換設定的候選 adapter。
供應商中立的立即續跑 control record 是：

```json
{"type":"assent.checkpoint_resume"}
```

## 安全邊界

預設 adapter 可能具有廣泛的 OS 權限。Assent 使用 worktree、prompt、control-file
snapshot、primary-worktree 比對與 Git HEAD 檢查來偵測越界；這不是預防式
sandbox。只在可信任的 repository 與環境中執行無人值守 workflow。
