# AI 專案記憶管理 Starter Kit

讓 AI（Claude Code、Codex、Cursor 等）在長期專案中以**最小上下文**正確接手工作的檔案體系，
同時也是 [wflow](https://github.com/edward9s/workflow) 自動調度器可直接執行的計畫格式。

核心思想：不是讓 AI 從幾千行歷史中挑出相關內容，而是把上下文分層，
每次只載入「足以無歧義開工的最小工作集」。

- **規劃**：人與 AI 開會議 session，共識即時固化為任務檔，散會條件 = `wflow check` 通過。
- **執行**：`wflow run` 無人值守跑完全部任務，一任務一 git 檢查點。
- **驗收**：人只審查與下指令，改檔一律由 AI 執行。

```text
AGENTS.md                    永久規則（root，工具自動載入的入口）
.agents/
├── FORMAT.md                計畫格式契約（規劃 AI 必讀，唯一權威）
├── CURRENT.md               現況快照（規劃期人與 AI 維護；執行期 wflow 生成）
├── CONSENSUS.md             設計依據（預設不讀）
├── verify.py                完成的機器證明
├── workflow.toml.example    wflow 設定範本
├── tasks/                   一任務一檔（t01.md、t02.md…）
└── logs/                    歷史證據（append-only，預設不讀）
```

本 README 只在本 repo 存在，不複製進目標專案。

---

## 安裝（手動複製，兩個動作）

1. 把 `.agents/` 整個資料夾複製到目標專案根目錄。
2. `AGENTS.md`：
   - 專案還沒有 → 整檔複製到專案根目錄，填掉 TODO。
   - 專案已有自己的 AGENTS.md（或 CLAUDE.md）→ 把其中「AI 工作體系（.agents）」
     一節整段貼到既有檔尾即可，原有規則不動。
3. 要用 wflow 執行時：把 `.agents/workflow.toml.example` 複製為根目錄
   `workflow.toml` 並填寫。

之後把 `.agents/verify.py` 的 TODO 換成專案實際檢查命令、
把專案真實狀態填入 `.agents/CURRENT.md`（最重要的一步）。

---

## 使用循環

### 1. 規劃：AI 會議（互動 session）

```text
開始規劃。請讀 AGENTS.md、.agents/CURRENT.md、.agents/FORMAT.md，
然後跟我討論以下目標，把共識逐步寫成 .agents/tasks/ 的任務檔：
<你的目標>
```

會議中每達成一項共識就落成任務檔，不要累積在對話裡。
散會條件：`wflow check` 通過——通不過就是計畫還沒完成。

### 2. 執行：無人值守

```text
wflow run          # 跑到全部 DONE / BLOCKED，可過夜
wflow status       # 另開終端隨時查進度（零 token）
```

### 3. 驗收：小會議（互動 session）

```text
請讀 .agents/CURRENT.md 與 workflow 分支的 git log，跟我過一遍執行結果。
```

對 BLOCKED 與不滿意的產出逐項裁決，由 AI 落實（改任務檔、狀態改回 TODO、
加新任務、標 SKIP），`wflow check` 過了再回到步驟 2。循環到全部 DONE → merge。

### 冷啟動測試（初始化完成的驗收）

開一個全新 AI session，只給它：

```text
請讀 AGENTS.md、.agents/CURRENT.md 與 .agents/tasks/ 裡第一個 TODO 任務檔，
然後告訴我：目前目標、修改範圍、驗收條件、下一步。
```

能不追問就答對 → 初始化完成。需要追問 → 回頭補 CURRENT 與任務檔。

---

## 三條鐵律

1. **CURRENT 是快取，不是權威。**
   與程式碼衝突時，信程式碼和測試，然後修 CURRENT。

2. **狀態檔重寫，日誌追加。**
   混淆這兩者，CURRENT 最終會退化成另一份無法閱讀的日誌。

3. **文字說明意圖，測試證明正確。**
   完成 = verify 命令 exit 0 + 驗收條件逐項通過，不是「看起來完成了」。

---

## 人工抽查（每隔幾個 session，約 2 分鐘）

```text
□ CURRENT 的 commit hash 是否對應目前版本？
□ 已完成內容是否仍被寫成未完成？
□ 未驗證內容是否被誤寫成已通過？
□ 被否決的方案是否仍列為現行方案？
□ 任務檔的狀態是否與 git 歷史一致？
```

失真的快照比沒有快照更危險——這 2 分鐘是整套體系最重要的保險。

---

## 何時擴充（先有痛點，再加結構）

| 痛點 | 才加入 |
|---|---|
| 同一決策反覆討論、AI 重採已否決方案 | `.agents/decisions/`（ADR：Context / Decision / Consequences / Rejected，200–500 tokens） |
| 多份任務重複大量架構說明 | `.agents/architecture/` |
| 單月日誌過大 | `.agents/logs/archive/` |

不要為可能永遠不會出現的問題，預先建立文件官僚體系。

---

## FAQ

**Q：為什麼 AGENTS.md 不收進 .agents/？**
A：agent 工具自動在 project root 尋找指令檔，位置本身就是功能。
移進子目錄後工具不會自動載入，違反零記憶冷啟動的初衷。

**Q：logs 會不會無限膨脹？**
A：會，但沒關係——它按月分卷、預設不讀，成本只在磁碟不在 tokens。
過大時移入 archive/ 即可。

**Q：.agents/ 要不要進版本控制？**
A：要。任務檔與 CURRENT 的歷史 diff 本身就是可回溯的交接紀錄，
而且 wflow 的檢查點 commit 依賴它。

**Q：工具讀的是 CLAUDE.md 而非 AGENTS.md？**
A：建立符號連結，或直接複製一份 AGENTS.md 為 CLAUDE.md。
