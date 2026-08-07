# 設定

*[English version](../CONFIGURATION.md) · [README](../../README.zh-TW.md)*

> 本檔是 [../CONFIGURATION.md](../CONFIGURATION.md) 的正體中文(台灣用語)翻譯；
> 內容如與英文版不同，以英文版為準。涵蓋 init、設定、adapter、model tier、
> effort 與故障排除。

## 需求與檔案位置

Assent 支援 Python 3.11+，需要 Git，且只用標準函式庫。設定的 AI adapter 必須
先安裝並登入 CLI，才能無人值守執行。

```text
python -m pip install assent
python -m pip install -e .
```

第一行是已發布套件；第二行是 source checkout 的 editable 安裝。解除安裝只移除
Python 套件與 `assent` CLI entry point，不會刪除 `~/.assent`、專案 `.assent/`、
worktree、archive 或 Git branch；資料清理是另外的人類選擇。

每台機器只有一份 Assent 共用檔：

```text
~/.assent/
├── assent.toml       # 共用設定
├── instructions.md   # session rules 契約
└── format.md         # task-format 契約

<project>/
├── AGENTS.md         # 專案規則與 Assent bridge line
└── .assent/          # ignored，只在 main worktree
    ├── verify.py
    ├── assent.toml   # optional project override
    └── <work folder>/
```

專案不會收到 `instructions.md` 或 `format.md` 副本；`assent init` 只在 user home
安裝/刷新它們。專案自己保有 `AGENTS.md`、verifier、task folder、report、log、
receipt、archive 與 optional project override。

## 設定優先序

由低到高：

1. Assent 內建預設值。
2. `~/.assent/assent.toml` 的 user settings。
3. `.assent/assent.toml` 的 optional project override。
4. 支援時的明示 CLI 選擇，例如 `--config PATH`、`--jobs`。

Table 依 key merge；scalar 與 array 整體取代。project override 只 shadow 它明示的
key，不會搬進 user home，也會 byte-for-byte 保留。`--config PATH` 選 project-level
override 並從 `.assent` parent 定位 project，不是 current-folder pointer。

省略 key 才會繼承：

- `key =` 是無效 TOML，不是空值。
- 空 table 不提供 leaf override。
- 允許該欄位時，空 array 是明確取代。
- 需要有意義文字的設定（command、adapter name、effort）若為空或全空白會拒絕。

無效 TOML 或值會在 managed files 寫入前拒絕。

## 初始化

在 Git project root 執行 fresh init，會安裝 user-home 契約與設定、建立
`.assent/verify.py`、保留 `.assent/` ignored，並刷新 `AGENTS.md` bridge line。它會
要求選一個真正的 project verifier：parallel unittest、pytest、npm test、Flutter
test、dotnet test、Maven test、Gradle test、CMake/CTest、Make test 或 custom
argv：

```text
assent init --test unittest
assent init --test "custom:python -m unittest"
```

產生的 verifier 會啟用所選 command，不會留下沒有測試卻回報成功的空骨架。

重跑 init 時保留既有 verifier，既有 verifier 存在就拒絕新的 `--test`；刷新
`~/.assent/instructions.md`、`~/.assent/format.md`，只補入遺漏的 active settings key。
現有 `.assent/assent.toml` 會保留並標示為 override。所有讀取、解析與 merge 在第一次
寫入前完成，失敗不會留下半套升級。專案內的 shared contract 副本只有在和 packaged
text 完全相同時才移除，不同內容會保留並提醒人類搬遷。

任何 AI session 開始前，兩份 user-home 契約都必須存在、可讀，且和 packaged text
byte-identical；缺少或過期會指出路徑並建議 `assent init`，不會在 run 中偷偷修補。
Universal-newline 比對讓 editor 改成 CRLF 仍算相同契約。

## Adapter

Adapter 把 task 的 portable 設定翻成 vendor CLI argument。task 使用抽象 model tier
`prime`、`core`、`lite`，可寫抽象 effort `heavy`、`normal`、`slight`。Vendor model
名稱與 effort value 屬於設定表，不可硬寫在 adapter code；task 明示的 effort 絕不能
被靜默忽略或升降級。

### Claude

```toml
[adapter]
name = "claude"

[adapter.claude]
command = "claude"
extra_args = ["--permission-mode", "bypassPermissions"]

[adapter.claude.models]
prime = "fable"
core = "opus"
lite = "sonnet"
```

### Codex

```toml
[adapter]
name = "codex"

[adapter.codex]
command = "codex"
extra_args = ["--sandbox", "danger-full-access"]

[adapter.codex.models]
prime = "gpt-5.6-sol"
core = "gpt-5.6-terra"
lite = "gpt-5.6-luna"
```

### Antigravity

Antigravity 使用本機 `agy` CLI 執行 Gemini。每台機器第一次需互動登入，之後以
print mode headless 執行；開 session 前會先驗證 model/effort 組合。

```toml
[adapter]
name = "antigravity"

[adapter.antigravity]
command = "agy"
extra_args = ["--dangerously-skip-permissions"]

[adapter.antigravity.models]
prime = "gemini-3.1-pro"
core = "gemini-3.6-flash"
lite = "gemini-3.5-flash"

[adapter.antigravity.default_effort]
prime = "heavy"
core = "heavy"
lite = "heavy"

[adapter.antigravity.efforts.prime]
normal = "high"

[adapter.antigravity.efforts.lite]
heavy = "medium"
```

第一次設定：

1. 依[Google 官方 CLI 安裝與驗證文件](https://antigravity.google/docs/cli/install)安裝 `agy`。
2. 執行互動式 `agy`，完成 browser sign-in；若輸出 authorization URL，開啟它完成流程。
3. 確認 `agy --version` 至少為 1.1.5，並用 `agy models` 查看模型。

Assent 只使用 AGY 已持有的 credentials，不會開 login browser、讀寫 credentials、
切換 Google account 或改 workspace trust。登出請在互動式 `agy` prompt 輸入 `/logout`；
它不是 shell subcommand。

## Model 與 effort 解析

model 與 effort 正交。effort 固定依序解析：

1. task file 明示的 `effort`；
2. 該 tier 的設定 `default_effort` override；
3. 該 tier 的 built-in default。

partial `default_effort` 只取代它寫出的 tier，其他 tier 保留內建值；每次受支援的
呼叫都傳入 concrete effort。

effort 翻譯依序查：

1. `[adapter.<name>.efforts.<tier>]`；
2. `[adapter.<name>.efforts]`；
3. 內建 `heavy -> high`、`normal -> medium`、`slight -> low`。

每個 key 各自 fallback。若新版 Gemini 支援 `medium`，可這樣覆寫：

```toml
[adapter.antigravity.efforts.prime]
normal = "medium"
```

有效矩陣的 effort 值如下：

| effort | Claude prime/core/lite | Codex prime/core/lite | Antigravity prime/core/lite |
| --- | --- | --- | --- |
| slight | `low` / `low` / `low` | `low` / `low` / `low` | `low` / `low` / `low` |
| normal | `medium` / `medium` / `medium` | `medium` / `medium` / `medium` | `high` / `medium` / `medium` |
| heavy | `high` / `high` / `high` | `high` / `high` / `high` | `high` / `high` / `medium` |

Antigravity prime 的 Gemini 3.1 Pro 沒有 `medium`，所以 normal 可見地映射到
`high`；lite 的 Gemini 3.5 Flash 沒有 `high`，所以 heavy 映射到 family ceiling
`medium`。1.1.5+ 才支援 `--effort`、穩定 model slug 與 unattended 修正。

Session 行會一次顯示四個稽核事實：

```text
Session: codex | core->gpt-5.6-terra | heavy->high
```

左邊是 task 的抽象值，右邊是實際傳給 CLI 的 argument。

## 可選的 folder review 與 auto-fix

可選的 `[auto_fix.review]` table override folder-level reviewer。沒有 table 時，會用第一個
effective worker adapter 的 `prime`/`heavy` 自動解析；不需重跑 `assent init` 或編輯
`~/.assent/assent.toml`。`adapter` 必須是已註冊
的 adapter；`model` 只能用抽象 `prime`/`core`/`lite`，`effort` 只能用抽象
`heavy`/`normal`/`slight`。Reviewer 會重用該 adapter 自己的 model/effort mapping，
因此明示的 reviewer 可以選 worker rotation 以外的 vendor：

```toml
[adapter]
name = ["claude", "codex"]

[auto_fix.review]
adapter = ["antigravity"]     # 每個 entry 一輪 review；不在 worker rotation
model = "prime"               # 抽象 tier
effort = "heavy"              # 抽象 effort
```

設定 table 或 built-in fallback 只提供 bounded read-only review-and-repair loop 的 policy；只有明示
`assent run --auto-fix` 才啟動該次 invocation 的 review 並授權 repair。沒有 flag 的普通
run 不會 review，也不會 repair。這個 flag 與 folder selection 正交，可和明示、remainder、
`--all`、`--once`、`--task`、`--verify` 的 run 形式合用。`_auto_fix.toml` 保存的是
resolved adapter 與實際 CLI model/effort，加上 finding ledger 與 consumed profile，不
接受 reviewer 自己聲稱的 identity。`assent check` 會顯示 resolved reviewer adapter、抽象
model/effort、實際 CLI 值，以及每個設定與 mapping 來自 built-in fallback 或 user/project
settings layer。唯讀 review 的 prompt-plus-detection write refusal 仍不是 security sandbox。

Repair 使用 worker rotation 的有限 abstract profile；每個 repair round 在第一個
write-capable session 前先依 round 開始時的 consumed history 記錄整輪 assignments，所以
多 task finding 或 dependency cascade 不會逐 task 消耗 normal profile。它只會以理由
`Automatic repair of durable folder-review findings` 重開既有 scope 內的 task。變更或直接
互動程式碼遇到的既有 technical debt，只有 `COMPLETED_FOLDER + INITIAL` 引入、局部且
focused tests 能可靠驗證時才可修正；blocked adjudication 與 `RECHECK` 可以保留或解決，
但不能新增，也不是全 repository debt audit。reviewer 可核准一個精確 scope addition，
但只有 scheduler 能修改 task file。recheck 保留仍存在 finding 的 fingerprint，只有有證據
的 repair regression 或 newly exposed existing requirement 才能新增；原集合清空後必須 PASS，
optional improvement 與 speculation 不會讓 loop 繼續。它不會自動建立 task、還原 source、
刪 source 或 accept。profile 用盡、中斷或 gate 失敗會保留 state 與編輯供 recovery；loop 內
沒有 runtime human adjudication gate。pending `FAIL` 只有在目前 resolved reviewer identity
相同時才能恢復，policy 漂移會拒絕 repair 與 closeout。完整 verification 依成功 run 的
receipt policy 或明示 `--verify` 另行執行；缺 receipt 或未跑 full suite 絕不是 reviewer failure。

## Antigravity timeout 與排錯

`print_timeout_minutes` 限制一次 AGY print；Assent watchdog 限制 session 沒有輸出的
時間，兩者獨立：

```toml
[adapter.antigravity]
print_timeout_minutes = 120
```

值必須為正，且不應短於最長 task。`preflight failed: invalid model selection` 時，
檢查 `agy models`，用 `agy --print --model <MODEL> ...` 測試，修正 model table 或
tier-specific effort。authentication error 時互動執行 `agy` 完成登入；`command
not found: agy` 則安裝 CLI 並檢查 `agy --version`。

quota 中斷會記錄 WIP。單一 adapter 有 reset 時間就等到該時間，否則等 quota poll；
adapter list 會立即切到下一個，全部用盡才等待。用 `assent run <FOLDER>` 恢復。

Adapter 若要立即續跑，只能用這個 exact final non-empty line：

```text
{"type":"assent.checkpoint_resume"}
```

Assent 隱藏 live output 的控制列、保留 raw diagnostics、建立 WIP，再用 continue
prompt 重開同一 adapter。它沒有 account/quota/reset/capability-probe 語意。
Wrapper 只有先安排立即續跑，才可把 provider quota result 換成這個 record；若轉送
provider quota，Assent 仍負責普通 wait 或 rotation。若 quota evidence 與這個 record
同時存在，普通 quota path 優先。

## Media 與 custom adapter

圖片、PDF、audio 等是一般 project context。task schema 不加 `inputs`、image、audio、
video 或 attachment 欄位。既有 media 的 project-relative path 與用途寫在 `behavior`
或 `notes`；只有 task 可能建立或修改的 media 才放入 `scope`。可重現 media 放在
worktree，不放 generated `.assent/`；感知判斷留給人類 `accept`，`verify` 保持機器可檢查。

要加入其他 AI CLI，subclass `Adapter`：`resolve_model(model: str) -> str` 將抽象 tier
映射成 `requested_model`，`run_task(prompt, requested_model, requested_effort, cwd) ->
TaskResult` 接收已翻譯值。`TaskResult` 有 `exit_code`、`output`、`quota_exhausted`、
`reset_at` 與獨立 checkpoint-resume outcome；vendor detection 留在 adapter。

## 相關設定

`[verification] receipt_refresh` 控制 folder-level complete evidence 是在 closeout
自動刷新（`"auto"`），還是等顯式 `assent verify`（預設 `"manual"`）。它不改變
`run --verify` 的 invocation request 或 acceptance 規則；詳見[驗證](VERIFICATION.md)。
