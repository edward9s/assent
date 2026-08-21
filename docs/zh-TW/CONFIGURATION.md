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
instructions.md   AI session 規則
format.md         計畫檔契約
workflow.md       scheduler 與驗收契約
```

專案自己保有 `AGENTS.md`、`.assent/verify.py`、各份計畫，以及可選的
`.assent/assent.toml` 與 `.assent/adapter.toml` override。

設定優先順序依次是內建預設、使用者設定、專案 override，以及該指令支援的命令列
override。Table 依 key 合併；scalar 與 array 整個取代較低層的值。省略才會繼承，
空 array 是明確的空值，空字串也是；需要文字的設定寫成空字串會被拒絕。

## 從 workflow 理解設定

設定的核心關係是：

```text
ability：prompt + 權限
        ↓
role：一個或多個 ability + model
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
```

`ability` 是不可為空的有序清單。只要其中一個 ability 可以寫入，role 就可以寫入；
只要其中一個會產生 verdict，role 就必須產生 verdict。一般 task role 的 `model`
可以省略；workflow role entry 可以覆寫它，一般 task step 再沿用 task 的設定。
`workflow.plan` 與 `workflow.integration` 的每一個 role step 都必須由 role 或
workflow entry 形成明確的有效 model——這種 session 負責的是一整個單位，
沒有單一 task 可以繼承。

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
| `workflow.plan` | 檢查所有已完成 task 累積出的 worktree 是否符合既有 plan。它會在第一次 `focused_sweep` 前執行一次常態品質審查，再處理 sweep 失敗與跨 task regression，並透過受影響的既有 task 修復。 |
| `workflow.integration` | 檢查同一份精確 plan 選集能否重建並通過 `full_verify`。它處理候選版本衝突與完整驗證失敗，不可刪掉某個 plan，也不可只接受能成功的前綴。 |

Integration workflow 只負責驗證與修復，不負責人類驗收。發布仍須稍後明確執行
`assent accept`。

### 預設 workflow

內建設定讓每個 task 先由一個實作 session 處理，三個修復層級各自使用專責的
reviewer/fixer。下方引用的 ability prompt 已經精簡，完整內容在
`~/.assent/assent.toml`；該檔也定義了每一層唯讀的 reviewer 與只寫入的
fixer，並在內建 workflow array 旁以註解附上對應的拆分版本：

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

[abilities.plan_quality_review]
prompt = "Review the completed cumulative worktree once for conformance to the existing plan before focused_sweep. Inspect cross-task interactions and whether changed tests prove cited requirements through observable semantics. Do not accept tests that merely mirror implementation constants, template examples, or incidental representation instead of proving the cited requirement. Report only blocking correctness, safety, unmet-requirement, or focused-test-gap findings tied to an existing task. Do not invent requirements or conduct a repository-wide debt search."
writes = false
produces_verdict = true

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

[roles.plan_quality_reviewer_fixer]
ability = ["plan_quality_review", "plan_fix"]
model = "prime"

[roles.plan_reviewer_fixer]
ability = ["plan_review", "plan_fix"]
model = "prime"

[roles.integration_reviewer_fixer]
ability = ["integration_review", "integration_fix"]
model = "prime"

[workflow]
task = [
  { role = "implementer" },
  { action = "focused_test" },
  { role = "task_reviewer_fixer" },
  { action = "focused_test" },
]
plan = [
  { role = "plan_quality_reviewer_fixer", adapter = "codex" },
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

第一次 action 前的 plan role 是唯一無條件執行的累積品質審查。它通過或完成修復後，
第一個通過的 `focused_test`、`focused_sweep` 或 `full_verify` 會略過自己 array 中剩餘的
失敗處理者。後續重複列出的 plan review 是不同修復回合，不是額外的常態審查。

## 省略設定與 task override

- 省略 `workflow.task` 時，每個 task 會依自己的 model 執行一個隱含
  session。
- 非空的 task workflow 可以先排列 worker role，再執行 `focused_test`；只要包含
  這個 action，最後一項就必須是它。Worker 回傳 `BLOCKED` 時，會帶著既有證據
  直接前進到下一個 verdict role。
- `workflow.task = []` 會停用逐 task session，改由 plan workflow 把整份 plan
  當成一個單位執行；此時 plan workflow 不可為空。
- 省略或設空 `workflow.plan`，表示不執行 plan review。
- 省略或設空 `workflow.integration`，表示停用自動 integration repair。

Task 檔只能覆寫自己的 task sequence。若要把一個 task 拆給 test writer 與 source
implementer，直接使用內建設定已定義的 `tests_writer` 與 `source_implementer`；
若你的設定檔沒有它們，就在有效的設定檔（例如 `~/.assent/assent.toml`）
補上：

```toml
[roles.tests_writer]
ability = ["write_tests"]

[roles.source_implementer]
ability = ["implement_source"]
```

再把 sequence override 單獨放進該 task 的 `.e.toml` 檔：

```toml
workflow = [
  { role = "tests_writer" },
  { role = "source_implementer" },
  { action = "focused_test" },
]
```

這會依序開啟兩個獨立的 AI session，最後以 `focused_test` 執行該 task 的
`verify` 指令。

省略時繼承 `[workflow].task`；`workflow = []` 則把這個 task 交給 plan-wide
execution。Override 使用的 role 仍須定義在有效的 `[roles]` 設定中。

## Adapter 與 model

`[adapter].name` 可指定一個 adapter，或指定依序輪替的清單。內建支援 Claude、
Codex 與 Antigravity；各自的指令、參數與可攜 model 對應都放在 `adapter.toml`。
無人值守執行前要先登入各 CLI；Assent 只使用既有認證，不管理 secrets。

Plan 只使用可攜的 `prime`、`core`、`lite` model tier。Effort 不是另一個可攜選擇，
也不是 task 欄位：每個 adapter 把一個 tier 對應到一次完整的 invocation。設定實際
會用到的每個 adapter——`[adapter].name` rotation，以及任何被 workflow entry
綁定的 adapter——都必須列出三個 tier，缺一個會在載入設定時被拒絕；完全
用不到的 adapter 則不需要對應表。

```toml
[adapter.codex.models]
prime = "gpt-5.6-sol/high"
core  = "gpt-5.6-terra/high"
lite  = "gpt-5.6-luna/max"
```

第一個 `/` 之前是 vendor model，之後是 vendor effort，兩者都會原封不動傳給該 CLI。
因此 model 名稱不能含 `/`；出現第二個分隔符會在載入設定時被拒絕。完全省略分隔符
就不會傳 effort argument，改用該 CLI 自己的預設值：

```toml
lite = "gpt-5.6-luna"
```

因為 tier 本身已經帶著它的推理投入，模型家族的實際限制就寫在值裡，人看得到。
Antigravity 的 `lite` 預設是 `gemini-3.5-flash/medium`，因為該家族沒有 `high`；
執行期不會有任何靜默的升降檔。如果某個 tier 常常需要「但這題比較難」，那是這個
tier 設得太低——改那一行，而不是去標註個別 task。

### Workflow model 優先順序

在同一個 role session 中，workflow role entry 會覆寫其 `[roles]` 定義；
其餘 fallback 依 workflow 層而定：

| Workflow role | Model fallback |
| --- | --- |
| `workflow.task` | 目前的 task |
| `workflow.plan` 或 `workflow.integration` | 無；workflow entry 或 role 必須指定 model |

Plan 與 integration 的 role 負責的是一整個單位，沒有可以繼承 model 的 task。
在這兩層省略 model 是設定錯誤，`assent check` 會回報，與該 role 是否產出
verdict 無關。省略 `workflow.task` 時，會使用 task 自己的 model 開啟一個隱含
session。

### 直接指定 vendor 模型

Task 檔案只接受 `prime`、`core`、`lite`，其他任何值都會在讀取計畫時被拒絕。
Vendor model id 只代表一次發布，而 task 檔案的壽命比它長，所以那個 id 屬於
設定檔，不屬於計畫本身。

`[roles]` 或 workflow entry 則可以直接寫 vendor 選擇，文法跟 `models` table
同一套。沒有標記語法：任何不是 tier 的值都會被讀成 vendor 選擇。

```toml
plan = [
  # codex 三個 tier 以外的 vendor 選擇
  { role = "plan_reviewer_fixer", adapter = "codex", model = "gpt-5.6-sol/xhigh" },
  { action = "focused_sweep" },
]
```

這會完全略過該 adapter 的 `models` table，也不會修改 `adapter.toml`。因為
vendor 字串對別的 vendor 沒有意義，使用它的 workflow step 必須只解析到一個
adapter。

每一層 workflow 的 role entry 都可以指定一個 adapter，或依序 fallback 的清單：

```toml
{ role = "implementer", adapter = "codex" }
{ role = "task_fixer", adapter = "codex", model = "gpt-5.6-terra/low" }
{ role = "task_reviewer_fixer", adapter = ["claude", "codex"] }
```

省略時，該 role 沿用全域 `[adapter].name` rotation。字串會把該 role 固定在
一個 adapter；quota 用完便等待它。清單只會依宣告順序使用其中的 adapter：
quota 或 adapter availability failure 會先保留進度，再切換且不消耗 task retry；
整份清單都 unavailable 或 quota-exhausted 後才等待。測試失敗、`BLOCKED` 與無效 verdict 會照
workflow 或 retry policy 處理，不會因此更換 adapter。每個 workflow step 都從
自己清單的第一個 adapter 開始。
Authentication failure 會保留進度並略過該候選。若所有候選都需要登入，Assent
會以 `AUTHENTICATION REQUIRED` 停止；登入後重新執行指令即可。

## 初始化與排錯

初始化專案時要選真正的完整 verifier，然後檢查產生的 script：

```text
assent init --test unittest
assent doctor
```

既有 `assent.toml` 或 `adapter.toml` 與模板不同時，init 會逐檔詢問，預設保留原檔。
對 verifier 而言，只有標記的專案測試命令區以外才屬於框架；區內命令由專案擁有，
不參與比對，缺少標記或標記無效則視為框架不同。選擇取代會先建立 byte-exact
同層備份；若是專案設定 override，取代代表移除 override，改用共享設定。未指定
`--test CHOICE` 而選擇取代 verifier 時，接著會顯示 0–9 測試選單。所有結果設定
都會在實際寫入前完成驗證。

Task 檔應使用範圍較小的 focused command。設定有誤時，diagnostic 會指出錯誤的
key 與來源檔。也請確認 Git 可用、選定的 AI CLI 已登入，而且 model mapping
使用該 CLI 接受的名稱。

人類流程請看[工作流程](WORKFLOW.md)，CLI 用法請看[指令](COMMANDS.md)，執行期間
復原請看[作業](OPERATIONS.md)。
