# .agents 計畫格式規格（wflow 相容）

> 本檔是「AI 會議產出計畫」與「wflow 自動執行」共同遵守的唯一格式契約。
> 規劃 AI：產生或修改任務檔前，必須先讀本檔。
> 執行 AI：不需要讀本檔，只讀 `AGENTS.md` 與被指派的那一個任務檔。
> 格式合格的客觀標準：`wflow check` 通過。

## 目錄佈局

```text
project/
├── AGENTS.md                # 永久規則（root，工具自動載入）
├── workflow.toml            # wflow 設定（從 .agents/workflow.toml.example 複製填寫）
└── .agents/
    ├── FORMAT.md            # 本檔
    ├── CURRENT.md           # 見「CURRENT.md 的兩種模式」
    ├── CONSENSUS.md         # 設計依據（預設不讀）
    ├── verify.py            # 完成的機器證明
    ├── workflow.toml.example
    ├── tasks/               # 一任務一檔，檔名 = <id>.md
    │   ├── t01.md
    │   └── t02.md
    └── logs/
        └── 2026-07.md       # append-only，預設不讀
```

## 任務檔（.agents/tasks/<id>.md）

一個任務一個檔案。檔案分兩部分：**表頭**（機器解析，嚴格格式）與**內文**（給執行 AI 讀的散文，結構建議但不強制）。

### 表頭（嚴格格式，逐字遵守）

檔案第 1 行必須是 `---`，表頭到下一個 `---` 為止。每行 `key: value`（半形冒號 + 一個空白）。出現未定義的 key 即格式錯誤。

```markdown
---
id: t01
title: 抽出 UndoProvider 介面
deps: none
model: lite
effort: medium
status: TODO
scope: lib/undo/ test/undo/
verify: python .agents/verify.py
---
```

| key | 必填 | 值域與規則 |
|---|---|---|
| `id` | 是 | 小寫英數，`t` + 兩位數零填充（`t01`～`t99`）；**必須與檔名一致**（`t01.md`） |
| `title` | 是 | 一句話任務名；驗收通過後成為 commit 訊息 `auto(t01): <title>` |
| `deps` | 是 | 前置任務 id，逗號分隔（`t01, t02`）；無前置寫 `none` |
| `model` | 是 | 三檔位之一：`prime` / `core` / `lite`，見下表；**絕不寫廠牌型號** |
| `effort` | 否 | `low` / `medium` / `high`；省略時用 workflow.toml 的 `default_effort` |
| `status` | 是 | `TODO` / `WIP` / `DONE` / `BLOCKED` / `SKIP`，見「狀態語意」 |
| `scope` | 是 | 該任務允許改動的路徑前綴，空白分隔；**fail-closed**：缺少或留空 = 拒絕執行。`.agents/` 內的任務檔 status 更新與日誌 append 由 wflow 自動豁免，不必列入 |
| `verify` | 是 | 該任務的驗收命令，在專案根目錄執行，退出碼 0 = 通過；通常是 `python .agents/verify.py`，個別任務可換更快或更嚴的命令 |

### 三檔位定義（model）

任務檔只寫抽象檔位，實際廠牌型號由 workflow.toml 的 `[adapter.<name>.models]` 對照表翻譯——同一份計畫換 adapter 即換廠牌，不改任務檔。

| 檔位 | 定位 | 適用 |
|---|---|---|
| `prime` | 最強旗艦 | 架構設計、跨模組契約、最難的任務 |
| `core` | 中堅主力 | 一般實作、需要推理的除錯 |
| `lite` | 快速便宜 | 機械性修改、樣板、文件同步 |

### 狀態語意

| 狀態 | 意義 |
|---|---|
| `TODO` | 未開始（新任務；或人類驗收不過、指示重做時改回） |
| `WIP` | 進行中 |
| `DONE` | 執行 AI 自認完成，待調度器客觀驗收 |
| `BLOCKED` | 卡住，交人類裁決（執行 AI 自標，或 wflow 在重試用盡時標） |
| `SKIP` | 本輪不做 |

狀態不設寫入權限：人類只負責審查與下指令，改檔一律由 AI 依指示執行
（驗收不過就叫 AI 把狀態改回 TODO 重做）。唯一的機器規則：
wflow 對任務檔的寫入僅限 status 一行，位元組級精準替換，其餘內容不動。

### 內文（建議結構）

```markdown
## Goal
（要達成什麼，一兩句）

## Required behavior
（具體行為要求，逐條編號）

## Acceptance criteria
（可逐項核對的驗收條件；機器可驗的寫進 .agents/verify.py，
 無法自動化的逐項列出，由人類收尾時複核）

## Notes
（已知事實、引用、風險；共用知識用標題錨點引用，不複製進來）
```

內文只給執行 AI 與人類閱讀，機器不解析。任務檔必須「執行上自包含」：
一個零記憶的 AI 只讀 AGENTS.md + 本任務檔就能無歧義開工。

## 任務挑選規則（wflow 執行語意）

1. 依**檔名字典序**掃描 `.agents/tasks/*.md`（零填充編號保證順序）。
2. 取第一個 `status: TODO` 且所有 `deps` 皆為 `DONE` 或 `SKIP` 的任務。
3. `BLOCKED` 只擋以它為前置的任務，其他任務照常執行。
4. 全部任務皆 `DONE` / `BLOCKED` / `SKIP` 時結束並印總結報告。

## 計畫輪替

一輪計畫結束（全部 DONE/SKIP、人類驗收並 merge）後，下一場會議開場時：

1. 刪除全部舊任務檔——git 歷史即歸檔，tasks/ 不留屍體，編號從 t01 重用。
2. 上一輪遺留的 BLOCKED 任務由會議裁決：改寫後編入新計畫，或放棄。
3. 仍有效的結論收進 CURRENT.md 或 AGENTS.md，不靠舊任務檔傳承。

## CURRENT.md 的兩種模式

- **規劃期**（AI 會議進行中）：由會議維護的現況快照，現在式、每次重寫。
- **執行期**（`wflow run` 之後）：**由 wflow 自動生成的唯讀報告**（進度統計、
  目前/下一個任務、BLOCKED 清單、最後檢查點）。人和會議 AI 讀它；
  **任何 AI 不得手動編輯**，調度器永不讀它——它壞了不影響執行。

規劃期結束前，CURRENT.md 裡仍有效的資訊必須收進任務檔或 AGENTS.md，
因為 run 之後它會被覆寫。

## 日誌（.agents/logs/YYYY-MM.md）

- append-only：只在檔尾追加，不修改既有條目；當月檔不存在就建立。
- 執行 AI 每完成一個任務 append 一筆；wflow 在標 BLOCKED 時 append 機器記錄。
- **預設不讀**。例外：除錯反覆發生的問題、追 regression、任務檔明確引用。

## 驗收（客觀閘門）

任務 session 結束後由 wflow 依序檢查，全部通過才 commit：

1. 狀態檢查：該任務 status 已被更新為 `DONE` 或 `BLOCKED`。
2. scope 檢查：任務起點以來的全部變更都落在該任務的 `scope` 內。
3. 驗收命令：執行該任務表頭的 `verify` 命令，退出碼 0 = 通過。

執行 AI **永不執行 git commit**——檢查點由調度器建立。
文字說明意圖，測試證明正確；pending 不得包裝成 completed。

## 冷啟動測試（計畫合格的品質標準）

一個零記憶的新 AI session，只給它 AGENTS.md 與任一 `TODO` 任務檔，
它必須能不追問就說出：目標、可改動範圍、驗收條件、下一步。
做不到 → 任務檔資訊不足，計畫還沒定稿。機器側等價物：`wflow check` 通過。
