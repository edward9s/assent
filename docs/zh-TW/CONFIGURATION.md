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

## Workflow

`[workflow]` 包含三個可任意排列、但長度有限的 step array：

```toml
[workflow]
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
```

每個 entry 只能包含一個 `role` 或 `action`：

| Array | Action | 意義 |
| --- | --- | --- |
| `task` | `focused_test` | 執行目前 task 的 `verify` command。 |
| `plan` | `focused_sweep` | 在累積 candidate 上執行各個不重複的 task command。 |
| `integration` | `full_verify` | 重建並驗證精確選集。 |

Role session 成功就前進一格。Action 通過就完成該層並略過後續 step；action
失敗則前進。走完仍未通過時，結果是 `REVIEW UNRESOLVED, HUMAN DECISION`，
exit zero，並保留所有證據。

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
