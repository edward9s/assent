# AI 專案記憶管理 Starter Kit

讓 AI（Claude Code、Codex、Cursor 等）在長期專案中，
以**最小上下文**正確接手工作的檔案體系。

核心思想：不是讓 AI 從幾千行歷史中挑出相關內容，
而是把上下文分層，每次只載入「足以無歧義開工的最小工作集」。

```text
AGENTS.md            永久規則（root，工具自動載入的入口）
.agents/CURRENT.md   現況快照（每次結束重寫）
.agents/tasks/       當前任務契約
.agents/logs/        歷史證據（append-only，預設不讀）
scripts/verify.py    完成的機器證明
```

設計原理見 `docs/CONSENSUS.md`，完整範例見 `docs/MANUAL.md`。

---

## 初始化（三步）

### 1. 複製檔案到你的專案

把本 kit 的以下內容複製到專案根目錄：

```text
AGENTS.md
.agents/
scripts/verify.py
```

或使用附帶腳本：

```bash
python init.py /path/to/your/project
```

（腳本只複製，不覆蓋既有檔案；`docs/` 與本 README 不會被複製。）

### 2. 填入你的專案實況

所有範本中的 `<!-- TODO -->` 都需要替換：

| 檔案 | 要填的內容 |
|---|---|
| `AGENTS.md` | 專案一句話描述、永久硬限制 |
| `scripts/verify.py` | 你的實際檢查命令（lint、test、format） |
| `.agents/CURRENT.md` | 專案**目前的真實狀態**（最重要的一步） |
| `.agents/tasks/ACTIVE.md` | 第一個要做的任務 |

如果你的工具讀的是 `CLAUDE.md` 而非 `AGENTS.md`，
建立符號連結即可：`ln -s AGENTS.md CLAUDE.md`。

### 3. 跑一次冷啟動測試

開一個全新的 AI session，只給它這個指令：

```text
請讀 AGENTS.md、.agents/CURRENT.md、.agents/tasks/ACTIVE.md，
然後告訴我：目前目標、修改範圍、驗收條件、下一步。
```

AI 若能不追問就答對 → 初始化完成。
若它需要問「這功能要做什麼」「哪些檔案能改」→ 回頭補 CURRENT 和 ACTIVE。

---

## 日常使用

### 每次 session 開始（開工提示詞）

```text
開始工作。請依序：

1. 讀 AGENTS.md
2. 讀 .agents/CURRENT.md
3. 讀 .agents/tasks/ACTIVE.md
4. 查看 git status / git log -5 / git diff
5. 查看任務直接相關的程式碼與測試
6. 核對 CURRENT 是否仍與程式碼一致（commit hash 是否對應）

動手前先回報：目前目標、修改範圍、驗收條件、預計驗證命令。
不要讀取 .agents/logs/，除非符合 AGENTS.md 列出的例外。
```

### 每次 session 結束

AI 依 `AGENTS.md` 的 Completion protocol 執行七步，關鍵是：

- 跑 `python scripts/verify.py`，逐項核對驗收條件
- **重寫** `.agents/CURRENT.md`（現在式，刪過時內容），不是追加
- 詳細過程追加到 `.agents/logs/YYYY-MM.md`
- 未驗證項目如實標註 pending，不得寫成完成

### 任務切換

ACTIVE 完成 → 重點結論回寫 CURRENT，再以新任務重寫 ACTIVE.md。
舊任務的完整記錄已在 logs/ 與 git 歷史，不另做歸檔。

### 人工抽查（每隔幾個 session，約 2 分鐘）

只查「當前真相層」：

```text
□ CURRENT 的 commit hash 是否對應目前版本？
□ 已完成內容是否仍被寫成未完成？
□ 未驗證內容是否被誤寫成已通過？
□ 被否決的方案是否仍列為現行方案？
□ ACTIVE 是否真的是目前優先任務？
```

失真的快照比沒有快照更危險——這 2 分鐘是整套體系最重要的保險。

---

## 三條鐵律

1. **CURRENT 是快取，不是權威。**
   與程式碼衝突時，信程式碼和測試，然後修 CURRENT。

2. **狀態檔重寫，日誌追加。**
   混淆這兩者，CURRENT 最終會退化成另一份無法閱讀的日誌。

3. **文字說明意圖，測試證明正確。**
   完成 = verify.py exit 0 + 驗收條件逐項通過，不是「看起來完成了」。

---

## 何時擴充（先有痛點，再加結構）

| 痛點 | 才加入 |
|---|---|
| 同一決策反覆討論、AI 重採已否決方案 | `.agents/decisions/`（ADR） |
| 多份任務重複大量架構說明 | `.agents/architecture/` |
| 路線圖與短期狀態更新頻率差異太大 | 拆出 `.agents/PLAN.md` |
| 單月日誌過大 | `.agents/logs/archive/` |

不要為可能永遠不會出現的問題，預先建立文件官僚體系。

---

## FAQ

**Q：為什麼 AGENTS.md 不收進 .agents/？**
A：agent 工具自動在 project root 尋找指令檔，位置本身就是功能。
移進子目錄後工具不會自動載入，違反零記憶冷啟動的初衷。

**Q：verify.py 為什麼放 scripts/ 而非 .agents/？**
A：它驗證的是專案正確性，不是 AI 專屬資源，CI 和你自己也會用。

**Q：logs 會不會無限膨脹？**
A：會，但沒關係——它按月分卷、預設不讀，成本只在磁碟不在 tokens。
過大時移入 archive/ 即可。

**Q：.agents/ 要不要進版本控制？**
A：要。CURRENT 和 ACTIVE 的歷史 diff 本身就是可回溯的交接紀錄，
而且多台機器或多 agent 協作時需要同步。
