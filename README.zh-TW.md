# assent — AI 計畫格式 + 自動調度器

*[English](README.md)*

> 本檔為 [README.md](README.md) 的正體中文(台灣用語)翻譯。內容若與英文版不一致,以英文版為準。

一套讓 AI 在長期專案中以最小上下文正確工作的檔案體系,加上讀懂這套體系、
無人值守執行的調度器。

- **規劃**:人與 AI 開會議 session,共識即時固化為 `.assent/` 裡的任務檔,
  散會條件 = `assent check` 通過。
- **執行**:`assent run` 無人值守跑完全部任務——選任務、開 headless AI session、
  執行任務 focused verify、建立 git 檢查點、額度等待與續作。資料夾完成後,
  調度器在 AI session 外執行一次完整 candidate verify;調度本身零 token。
- **驗收**:人先讀程式生成的 `.assent/<工作資料夾>/_report.md`(零 token),只對要裁決的任務開 session。

## 設計原則

1. **在確保輸出品質可信可靠的前提下,最小化 tokens 消耗。**
   調度、驗收、報告全部是純 Python 本地作業;每個 AI session 的必讀集只有
   專案 AGENTS.md + assent 工作指示 + 它自己的任務檔。
2. **保持靈活,少即是多。** 零第三方依賴(只用 Python 標準庫);
   狀態就是任務檔本身,沒有資料庫、沒有隱藏狀態。
3. **AI 能處理的全部自動處理,人類只做審查與裁決。**
   人不手改檔案;驗收不過就下指令叫 AI 改。
4. **執行 AI 燒過 tokens 的產出絕不丟棄。**
   額度中斷收 wip 檢查點續作;驗收失敗不還原、在現有成果上重試;
   重試用盡連同成果 commit 進 BLOCKED 檢查點交人類裁決。

## 運作原理

```text
              ┌────────────────────────────────────────────┐
              │              主迴圈(零 token)               │
 .assent/     │  1. 掃工作資料夾,選任務:WIP 續作優先,        │
 工作資料夾 ──▶     否則第一個「TODO 且前置皆 DONE/SKIP」      │
 (tNNN_name   │  2. 讀該任務的檔位/effort,開 headless session │──▶ 執行 AI
  .toml)      │                                                   │
              │  3. session 結束後客觀驗收:                  │◀── 更新任務檔
              │     狀態 → 結構比對(防竄改)→ scope → verify │     + 同名 rNNN_name.toml 日誌
              │  4a. 通過 → auto(工作資料夾/tNNN) 檢查點     │
              │      → 回到 1                                  │
              │  4b. 失敗 → 保留成果帶原因重試 → 仍失敗則      │
              │      標 BLOCKED 連成果一起 commit → 回到 1    │
              │  4c. 額度耗盡 → wip 檢查點 → 倒數等重置        │
              │      → 帶「接續」提示續作                     │
              └────────────────────────────────────────────┘
```

- **任務檔即狀態**:每個任務一個 `tNNN_name.toml` 檔(狀態、依賴、檔位、
  scope、verify、驗收條件),日誌則是同主幹的 `rNNN_name.toml`
  (append-only、預設不讀)。妥善處理的中斷會留下 WIP,重新 `assent run` 即可
  續作;若程序或主機突然中斷而留下未提交變更,調度器會拒絕猜測,等候人工檢查
  並建立檢查點。
- **格式契約**:`~/.assent/format.md`(`assent init` 安裝到每台機器共用的
  使用者家目錄,一台機器只有一份),規劃 AI 讀它產生任務檔,調度器解析器與它
  逐字對齊。同目錄的 `~/.assent/instructions.md` 則是 session 規則;兩者都不會
  複製進任何專案。
- **session 過程即時可見**:AI 說的話(`AI|`)、用的工具(`Tool|`)、token
  用量(`--|`)同步印在終端,並留存於 `.assent/<工作資料夾>/_assent.log`。

## 安裝

Python 3.11+、git、已登入的 Claude Code CLI(`claude`)或 Codex CLI(`codex`)。

```
python -m pip install assent
```

驗證:任何目錄執行 `assent --version`;它會印出已安裝套件的版本。
`assent --help` 會顯示頂層 CLI 說明。零第三方依賴,不會下載任何外部套件。

若要從原始碼 checkout 進行開發:

```
python -m pip install -e .
```

## 檔案放在哪裡

描述 assent 本身的檔案,一台機器只有一份;描述你的專案的檔案,留在你的專案裡。

```text
~/.assent/                  # 每位使用者的 assent 家目錄,所有專案共用
├── assent.toml             # 你的設定:adapter、檔位對應表、watchdog、重試
├── instructions.md         # session 規則契約     (assent 擁有這個檔)
└── format.md               # 計畫格式契約         (assent 擁有這個檔)

<專案>/
├── AGENTS.md               # 你的專案規則 + 一行 assent bridge
└── .assent/                # 被 git 忽略,只存在於主工作樹
    ├── verify.py           # 你這個專案的驗收腳本
    ├── assent.toml         # 選用:舊版遺留或刻意設定的專案覆寫
    ├── <工作資料夾>/        # 任務檔、r 檔、_report.md、_assent.log、
    │                       #   assent.lock,以及該資料夾的驗證 receipt
    ├── _batch_verification.toml   # batch 驗證 receipt(衍生物)
    ├── _archived.toml      # 已退役工作資料夾的名冊
    └── _archive/           # 那些資料夾各壓成一個 zip
```

`instructions.md` 與 `format.md` 描述的是工具本身,所以每台機器各一份,專案永遠
不會拿到副本。`AGENTS.md` 與 `.assent/verify.py` 則是你的:`assent init` 只會刷新
前者裡那一行 bridge,也絕不覆寫後者。

### 設定優先序

由低到高:

1. assent 的內建預設值
2. 你的使用者設定 `~/.assent/assent.toml`
3. 選用的專案覆寫 `.assent/assent.toml`
4. 指令有提供時的顯式 CLI 選擇(`--config PATH` 決定由哪個專案層檔案擔任第 3 層;
   `--jobs` 之類的旗標則對該次執行覆寫對應設定)

table 依 key 合併,scalar 與 array 整個取代。因此專案覆寫會就它所寫出的那些 key,
遮蔽你日後對共用設定的修改;`assent init` 不會把它搬進使用者家目錄,也不會改它
——只逐位元組保留並回報它是一個覆寫。

只有「省略某個 key」才會繼承下層:

- `key =` 不是「沒有值」,那是無效 TOML,整個檔案載入失敗。
- 空 table 表示不覆寫任何葉節點,裡面每個 key 仍由下層解析。
- 空 array 在該欄位允許時是一次顯式取代,不是要求回退。
- 空字串或只有空白的字串,對任何需要有意義文字的設定(命令、adapter 名稱、
  effort 值)一律拒絕。錯誤訊息會指出那個 dotted key 與寫出它的檔案,而不是
  安靜地把下層值放回來。

### `assent init` 重跑時做什麼

它會把兩份使用者家目錄契約刷新成這套安裝的打包版本,並只補上你的 `assent.toml`
尚未寫出的打包設定 key,絕不取代你寫過的值。在專案端,它保留既有的 `verify.py`
(已存在時提供 `--test` 會拒絕),維持 `AGENTS.md` 的 bridge 那一行為最新,並讓
`.gitignore` 保有 `.assent/` 條目。所有讀取、解析與合併都在第一次寫入前完成,
因此無效 TOML 或無效的 `--test` 選擇會直接拒絕,不會留下半升級狀態。

升級舊專案時,它印出警告而不替你決定。專案內的 `instructions.md` 或 `format.md`
副本,只有與打包文字完全相同時才會被移除;不同的會保留並回報——反正 session 讀的
是使用者家目錄的契約,請自行把還想保留的內容搬出來後再刪掉。既有的
`.assent/assent.toml` 一樣保留,並回報為優先於你的使用者設定的相容性覆寫。

開任何 session 之前,除非兩份使用者家目錄契約都存在、可讀,且與這套安裝的打包
文字逐位元組相同,否則 assent 會 fail-closed。缺少、無法讀取或過期的契約會指出
路徑並要你執行 `assent init`,絕不會在執行中途自行修補或重新生成。比對以
universal newlines 讀取文字,因此被編輯器改成 CRLF 的檔案仍算同一份契約。

## 快速開始

```
# 0. cd 到目標專案根目錄(需為 git repo)

# 1. 安裝使用者家目錄(~/.assent:共用設定 + 兩份契約),以及專案的 .assent
#    骨架與 AGENTS.md,先選擇真正的專案測試
#    (可互動選平行 unittest、pytest、npm test、Flutter test 或 custom argv;
#     script 可用 --test,例如)
assent init --test unittest
#    custom 命令可寫成: assent init --test "custom:python -m unittest"
#    重跑 init 不提問:保留專案的 verify.py,刷新兩份使用者家目錄契約,
#    只補入遺漏的設定 key

# 2. 檢視 ~/.assent/assent.toml 的共用設定(本機每個專案都會讀),再填
#    AGENTS.md 的專案描述/硬限制、.assent/verify.py 的實際檢查命令
#    AGENTS.md 可自行決定是否提交;整個 .assent/ 留在主工作樹,不提交

# 3. 開 AI 會議產出任務檔(這一步是互動 session,見下方「使用循環」)

# 4. 驗證計畫與環境(零 token;通過 = 會議可以散會)
assent check

# 5. 試跑一個任務,確認無誤後全自動跑到底(可過夜)
assent run --once
assent run

# 也可以用位置參數指定工作資料夾(與 --config 正交)
assent run <資料夾>

# 只依寫出的順序執行 A、再執行 B
assent run A B
# 先依序執行 A、B,再把其餘未完成資料夾交給 --all scheduler
assent run A B --all
# 字面 `...` 是 remainder selector:先 A、再 B,再加上其餘每個工作資料夾,
# 整批在動任何東西之前就先定格成一個明確選擇
assent run A B ...

# 依資料夾 after 依賴順序執行全部未完成資料夾,最多同時跑 2 個
assent run --all --jobs 2

# 在 exit code 為零的 run 之後接上完整驗證(失敗的 run 不驗證任何東西);
# 驗證的 exit code 就是這道命令的 exit code
assent run --all --verify

# 6. 預設 [verification] receipt_refresh = "manual" 下,run 收尾不會留下
#    receipt,直接/selected accept 會被拒絕並提示先 verify。離席時顯式刷新(零
#    token):一次驗證多個已完成資料夾,成本等同驗證一個
assent verify --batch
# 或只刷新單一資料夾的 receipt
assent verify <FOLDER>
# 把 A、B 當成一個依賴排序的 selected batch 做完整驗證
assent verify A B
# 把 A 加上其餘每個已完成資料夾,當成一個 exact selected batch 驗證
assent verify A ...
# 在 FOLDER 的 source worktree 重跑 DONE task 的 focused checks(不寫 receipt)
assent verify <FOLDER> --focus
# 想要 run 收尾就自動刷新 receipt,改設 receipt_refresh = "auto"

# 7. 隨時查看(另開終端、零 token),再進行審查
assent status
assent report
# 人類審查後,依資料夾依賴順序接受全部已完成資料夾
assent accept --all
# 或只接受一個已完成的資料夾併入目前目標分支
assent accept <資料夾>
# 只接受有相符 batch receipt 的 A、B
assent accept A B
# 接受 A 加上其餘每個已完成資料夾;這仍是 exact selection,
# 需要恰好涵蓋展開後集合的 receipt,且一律不驗證
assent accept A ...
# 接受後,用一般 Git(或自行委任的 AI 流程)獨立同步
git push
# 接受與所需同步完成後,移除多餘成果
assent clean <資料夾>
# 一次清理多個資料夾,upstream-first(單獨的 assent clean 仍代表全部)
assent clean A B
assent clean A ...
# 不再需要時,把已接受資料夾的計畫封存進 _archive/
assent archive --all
# 也可以指名要退場的資料夾;被指名卻不合格的資料夾算失敗,
# 與會直接略過的 --all 不同
assent archive A B

# 驗收會議要求單一任務重做(預設保留程式碼;不自動 run)
assent rework <FOLDER> <TASK> [--cascade] [--reason TEXT]

# 驗收會議裁決駁回整個資料夾的實作時(封存、強刪、任務改回 TODO)
assent reject <FOLDER>
```

跑完後人類驗收:

```
git log --oneline <資料夾名>/<run-id>   # 一任務一 commit,逐一查看
git diff main...<資料夾名>/<run-id>     # 或看整體差異
# 人類做決定;Assent 執行受保護的本地整合
assent accept <資料夾>
# 再自行選擇一般 Git 同步,例如 `git push`,或委任你自己的 AI 流程
# 單一任務不接受 → assent rework <資料夾> <任務>
# 有已開始的下游 → 加 --cascade;確認要反向程式碼 → 加 --revert-code
# 整個資料夾的實作都不要 → assent reject <資料夾>
```

`rework` 成功後會立即更新 `_report.md`,但不印整份報告、也不啟動 AI;人確認
TODO 與連動範圍正確後,再明示執行 `assent run <FOLDER>`。

`DONE` 是執行 AI 的完成主張,不是人類批准。人類必須先讀 `_report.md`、
檢查報告與 checkpoint 存證,再明示做出接受決定。receipt 是 scheduler 的完整
驗證證據;呼叫 `accept` 才是人類批准。

直接 `assent accept <FOLDER>` 與 selected `assent accept A B` 從不執行完整 verifier。
直接資料夾若已由 ancestry 證明包含在 target 中,就是具冪等性的 no-op;否則直接
形式需要 source tip、重建 integration tree 與 verifier digest 完全相符的 fresh
PASSED per-folder receipt。selected 形式需要恰好涵蓋依賴排序後 A、B 的 fresh
PASSED batch receipt。missing、malformed、stale、mismatch 或 drift evidence 都會
拒絕,並提示相應的 `assent verify`;兩者都不會靜默驗證或接受明示集合以外的資料夾。

`assent accept --all` 是刻意保留的例外,有兩種模式。fresh PASSED batch receipt 會
在不新增完整驗證的情況下重播並原子發佈,只發布 receipt 記錄的確切資料夾;沒有
receipt,或 batch receipt 已過期/不是 PASSED 時,改走逐資料夾路徑:依依賴順序對每個
尚未整合的資料夾先執行 `verify_folder_if_needed`,再做一般 receipt-backed accept。
malformed batch receipt 會拒絕,不會 fallback。逐資料夾路徑把已整合資料夾當作
ancestry no-op;已完成且 source branch 與 worktree 都在已證明整合後被清除的資料夾,
只有此 `--all` 路徑會跳過。第一次真正的驗證或接受失敗就停止,但先前發布的成果
保留;fresh batch 路徑對 receipt 以外的已完成資料夾只回報,同一次不驗證也不接受。

所有接受路徑都要求明示的人類決定、完整且依賴安全的 source,以及乾淨、唯一可辨識
的 Git 狀態。conflict 不會自動解決,失敗閘門不會推進 target;不連線 remote、
不 `pull`、`rebase`、force push、刪 source 或提供自動衝突處理。integration lock
只能串行 Assent accept,不能阻擋外部 Git 寫入;接受期間不要在同一主工作樹執行會
寫入的 Git 命令。只有在接受與所需同步完成、且清理證明成立後,才執行
`assent clean <FOLDER>`。

### 有界的樂觀堆疊

下游資料夾的 `_folder.toml` 設為 `after = ["A"]` 後,表示 A 是排程前提。
`base = "A"` 宣告下游檔案建立在 A 的 commit 上,其 worktree 會是該 commit
的完整簽出;非 `base` 的 `after` upstream 只保證順序,不提供檔案內容或同檔
衝突保護。沒有宣告 `base` 時,下游從目前的整合目標建立;`after` 成員的數量
與接受狀態不影響基底選擇,多個未接受 upstream 也不會因此造成基底歧義或拒絕。

例如:`run A` -> `run B` 堆疊在 A 上 -> combined verification -> 人類
`accept A` -> 人類 `accept B`。B 可在 A 尚未接受時建立 receipt;A 進入 target
後,若 source tip、integration tree、verifier digest 仍相同即可重用,`accept`
不重跑完整 suite。若 A 前進,B 會 stale 但成果保留;可 rework/reject B,或開
新資料夾重新規劃,Assent 不重寫 stack history。

A 與 B 修改同一檔案也遵守同一規則。Git 能自動合併時由 exact-tree verification
證明結果;Git conflict 則 target 不變,交由人工作裁決。Assent 不自動 rebase、
解衝突或 push。

### 明示的 selected workflow

`assent run A B` 只依寫出的順序執行 A、再執行 B。每個資料夾仍會檢查自己的
前置,第一個設定或執行失敗就停止。`assent run A B --all` 先完成這個明示順序,
再把其餘未完成資料夾交給正常的依賴排序 `--all` scheduler;兩者都不會暗中
建立完整 integration candidate 或接受任何東西。

每次明示的資料夾選擇都會在 dispatch 前完整稽核。每個寫出的名稱,包括
`...` 前的明示前綴,都必須解析為現存 `.assent/` 目錄,且其中至少有一份正式的
`tNNN_name.e.toml` 任務檔。只要有任何名稱 unresolved,Assent 會一次列出完整的
未解析集合並以非零結束,在第一個被選資料夾執行、驗證、發布、清理或封存以前就
停止;也不會建立缺少的資料夾、lock 或資料夾 log。這只是 identity 檢查:任務完成度、
依賴是否 ready、lock、receipt、Git 狀態與其他資格仍由各命令自己的 gate 決定。
省略資料夾、`--all`、`--batch` 與單獨的 `...` 保留原本的發現契約。`archive
--restore FOLDER` 與已辨識的 archive crash-resume 狀態則可以刻意沒有 live 目錄。

#### `...` remainder selector

字面 ASCII token `...` 是 `run`、`verify`、`accept`、`clean`、`archive` 共用的
最後一個位置參數,意思是「再加上這道命令自己會發現的其餘每個資料夾」,所以
`assent run A B ...` 就是「先 A、再 B、然後其他全部」。它不是 `--all` 的另一種
寫法:`...` 產生的是一個 exact folder selection,而且在動任何東西之前就先定格;
`--all` 則保留自己的動態全專案模式。兩者併用是用法錯誤,`...` 出現一次以上、
或不放在最後,同樣是用法錯誤。資料夾名稱永遠不能含 `..`,所以這個 token 不會
和真實資料夾相撞。

每道命令用自己的發現規則展開 remainder:`verify` 與 `accept` 只加入已完成的
資料夾(那正是它們全專案模式處理的集合),`run`、`clean`、`archive` 則納入每個
工作資料夾,之後再照常做各自的逐資料夾判斷。remainder 接在明示前綴之後,各命令
再套用自己的排序:`run` 保持前綴寫下的順序、remainder 依資料夾依賴順序,
`verify` 與 `accept` 把整個選擇正規化為依賴順序,`clean` 則正規化為
upstream-first。展開結果會在開工前印出;展開後選不到任何資料夾則拒絕執行。
`...` 只負責選資料夾,不會把命令切換成另一種模式:即使是單獨的
`assent run ...`,也是照一般的明示資料夾路徑逐一走完展開後的選擇,而不是交給
`--all` scheduler,`--jobs` 仍然只是 `--all` 的選項。

決定走哪條路徑的仍是數量,不是這個 token:一個資料夾走單一資料夾路徑(folder
receipt、direct accept、單一 `archive`),兩個以上走 exact selected batch。因此
`assent verify A ...` 寫出的 batch receipt 恰好涵蓋展開後的集合,而
`assent accept A ...` 也要求恰好同一集合的 fresh receipt,並且一律不驗證。
`...` 不可與 `verify --batch`、`verify --focus`、`run --once`、`run --task`,以及
只還原單一資料夾的 `archive --restore` 併用。

#### `run --verify`

`assent run --verify` 只在 run 的 exit code 為零時接上一次完整驗證;失敗的 run
會原樣回傳、不驗證任何東西,因為根本沒有完成的計畫可背書。驗證範圍與選擇一致:

| 呼叫方式 | 驗證什麼 |
| --- | --- |
| `assent run --verify`(自動選出的資料夾) | 該資料夾的 receipt |
| `assent run A --verify` | A 的 folder receipt |
| `assent run A B --verify` | A 與 B 當成一個 selected batch |
| `assent run A ... --verify` | 展開後的 exact selection 當成一個 batch |
| `assent run --all --verify` | 全專案的動態 batch |
| `assent run ... --verify` | 全專案的動態 batch |
| `assent run A --once --verify`<br>`assent run A --task t003 --verify` | A 的 folder receipt,但僅限該次受限執行讓 A 變成完成時 |

單獨的 `...` 是全專案請求,因此剛好落在 `--all` 使用的同一個動態 batch,而不是
把 scheduler 之後可能還會擴充的集合凍結起來;帶明示前綴的 `...` 則仍是 exact
expanded selection,驗證的正是它所執行的那些資料夾。驗證的 exit code 就是這道
命令的 exit code。

在預設的 manual receipt-refresh 政策下,完成資料夾的 run 收尾會先說明 per-folder
receipt 延後。若這次呼叫有 `--verify`,收尾訊息會說明同一次呼叫接下來要做的是
run-level verification,不會再叫使用者重新啟動已經正在進行的驗證命令;表格中的
exact selected 或動態路徑就是這個交接。

表格中的單一資料夾路徑會在 folder verification 更新、使 receipt 失效或留下
沒有 receipt 之後 refresh `_report.md`,因此報告會反映這次呼叫最新的 receipt
結果。這個 best-effort 寫入不會改變 verification 的 exit code 或中斷處理;
selected 或動態 batch 驗證以及 `--focus` 保留原有契約,不會只為了對稱而刷新
folder report。

`--once`、`--task` 可以與 `--verify` 併用。它們恰好只選出一個資料夾,receipt 的
範圍因此毫無歧義,但它們在單一任務後就停止:所以只有在該次受限執行讓所選資料夾
變成完成(每個任務都是 `DONE` 或 `SKIP`)時才驗證。資料夾未完成則此請求失敗且不
寫下 receipt;這道拒絕會指出未完成的任務 id 與狀態,並且發生在建立任何整合
candidate、啟動任何完整驗證器之前,所以它是失敗,而不是安靜地跳過。`--verify`
屬於呼叫層級的請求,不理會設定中的 `receipt_refresh` 政策。

#### 多資料夾的 `clean` 與 `archive`

`assent clean A B` 與 `assent clean A ...` 在一趟 upstream-first 的流程裡清理多個
資料夾,每個資料夾自己的證據規則不變;單獨的 `assent clean` 仍代表全部資料夾。
`assent archive A B` 封存每個被指名的資料夾,遵守的是單一資料夾 `archive` 的
契約而非 `--all` 的:既然是人類指名的,只是不合格也算被拒絕的請求。每個被指名的
資料夾都會嘗試,結尾以一行摘要報告封存了幾個,只要有任何一個沒封存就以非零
exit code 結束 —— 而 `assent archive --all` 遇到不合格的資料夾只是略過,不算失敗。
`archive --restore FOLDER` 只還原一份封存,不接受 `--all`,也不接受 `...`。

#### 有顏色的 help

在標準函式庫會為 `--help` 上色的環境(Python 3.14 以後),assent 只換掉
`usage:` 前綴與段落標題的顏色,讓它們不再是預設佈景那種難以辨識的深藍;其餘
選項與標籤顏色都維持標準值。這個替換發生在 argparse 自己的顏色判斷之內,所以
`NO_COLOR`、`FORCE_COLOR`、`PYTHON_COLORS`,以及被重導或不支援的輸出串流,仍
決定究竟要不要輸出任何 escape sequence。在 argparse 尚無顏色支援的
Python 3.11-3.13,help 是純文字。因此顏色從來不是承諾 —— 沒有顏色時 help 一樣
好讀。

`assent verify A B` 只選取 A、B,正規化成依賴順序,建立一個 integration candidate,
只執行一次完整 verifier,並寫一份記錄這些 source identity 與中間 tree 的 batch
receipt。selected merge conflict 會拒絕,不跳過也不縮小集合;它不改 target ref、
也不接受資料夾。若失敗要求被 bisect 成通過 prefix,命令仍回傳失敗,該 prefix
不能授權原本的 selected acceptance。

這條 exact selected 路徑的標籤是 `verify selected`,不是 `verify --batch`;
後者保留給動態發現及其互動式衝突略過政策。若 candidate 建置發生衝突,訊息會
先說明完整 verifier 尚未執行,列出衝突資料夾與路徑,並說明沒有寫入 receipt、
target 與 selected source ref 都維持不變。若資料夾單獨對 target 就衝突,直接
指向 `assent reconcile <FOLDER>`。若只是 peer-only conflict,則列出衝突資料夾
前方相容的 selected prefix,建議先對該 prefix 執行 `assent verify` 再
`assent accept`,讓 target 前進後再以 `assent reconcile <FOLDER>` 對進階後的
target 處理;`assent rework <FOLDER> <TASK>` 與 `assent reject <FOLDER>` 仍是
明確的替代方案。

`assent verify <FOLDER> --focus` 則不同:它在該資料夾的 source worktree 執行
distinct DONE-task verification commands,不建立 integration candidate、不寫 receipt,
即使通過也不能授權接受。成功的 exact selected verification 之後,人類審查可執行
`assent accept A B`;它要求恰好 A、B 的 fresh receipt,不重跑驗證,一次原子發布
全部 selected 資料夾或一個也不發布。

清理採 upstream-first 且以證據為準。直接 dependent 尚未完成、接受、乾淨、存在,
或無法證明已整合時都保留 source;`assent clean A` 會拒絕並說明原因。全部
dependent 都接受且可證明整合並乾淨後,再用 `assent clean` 先清 upstream、後清
dependent;不要手動刪 worktree 或 branch。

### `verify --batch` 的互動式衝突略過

沒有 conflict 的 `assent verify --batch` 維持完全無人值守。建置批次候選時
才會發現 source conflict,而這從不算作驗證失敗:每個排入佇列的資料夾仍會
被嘗試合併,因此一個資料夾發生 conflict 不會阻止之後、彼此獨立的資料夾
也被嘗試。一旦有一個以上資料夾 conflict,`verify --batch` 會回報每個
conflict 資料夾及其衝突路徑,並回報每個排在某個 conflict 資料夾 `after`
之後而被一併排除的資料夾(遞移計算,而非在缺少其宣告 upstream 的情況下
仍驗證它),接著只問一次 `[Y/n]`:是否略過整組被排除者,改為只驗證其餘
仍可合併的資料夾。

- **是**(空白回答或 `y`/`yes`):對較小子集執行一次完整驗證,批次 receipt
  只記錄這些已驗證的資料夾;每個被略過的資料夾完全不會被嘗試。
- **否、無法辨識的回答,或 EOF**(無人可回答的非互動呼叫者):
  `verify --batch` 會在執行完整驗證前停止,且不寫入 receipt,與其他任何
  拒絕情形相同。
- **整批全部 conflict**:已沒有獨立可提供的資料夾,批次會直接拒絕,
  不會提問。

略過不是解決、rebase、接受或刪除任何東西——target 與每個 source
資料夾,不論被略過或已合併,都維持原樣不變。若是 peer-only conflict,可先驗證並
接受衝突資料夾前方相容的工作,讓 target 前進後再處理該資料夾;`assent rework`
與 `assent reject` 仍是重新開啟或捨棄它的明確替代方案。

`assent accept --all` 有兩種刻意區分的模式。fresh PASSED batch receipt 時,只在
一次原子 ref 更新中發佈 receipt 涵蓋的確切資料夾,並在同一次執行內回報 receipt
未涵蓋的其餘已完成資料夾;不會驗證或接受這些剩餘項目,也沒有第二次提問或隱藏的
集合擴張。沒有 batch receipt,或 evidence 已過期/不是 PASSED 時,改走逐資料夾
路徑,在每個尚未整合的 accept 前執行 `verify_folder_if_needed`。malformed receipt
會拒絕而不 fallback;逐資料夾路徑在第一次真正失敗時停止,保留先前已發布成果。
之後可再次執行 `assent verify --batch`,對剩餘部分建置下一個明示批次。

`assent archive --all` 只封存獨立符合資格的資料夾(已完成,且其 source
已經不存在,或 `clean` 本身的機械證明可以移除它);對於任何 source 仍被
未接受 dependent 需要的資料夾,它會保留該證據並跳過封存,與 `clean` 所
強制的 upstream-first 規則相同。

### 用 `assent reconcile` 解決單一資料夾的衝突

`assent verify --batch` 只能略過發生 conflict 的資料夾,無法解決它。
`assent reconcile FOLDER` 是對應的單一資料夾指令:人類只編輯衝突檔案,
Assent 則負責這些編輯周圍的每一個 Git 操作。完整順序是:

```text
assent reconcile parallel01              # 在 worktree 中準備好衝突
                                         # (人工編輯被回報的檔案)
assent reconcile --continue parallel01   # 加入索引、提交、推進 source
assent verify parallel01                 # 接受前必須執行、明確、昂貴
assent accept parallel01                 # 明確的人類批准
```

**start** 要求資料夾已完成(每個任務為 `DONE` 或 `SKIP`)、主 worktree 乾淨,
且 source 有自己的分支與 worktree。它會擷取整合 target 目前的 tip,在主
worktree 旁建立 worktree `<project>.reconcile/<FOLDER>`,置於從 source tip
起始的臨時分支 `assent-reconcile/<FOLDER>`,並把擷取到的 target tip 合併
進來但不提交。由於這個 merge 以 source 為先建立,其第一 parent 就是原本的
source,所以之後 source 分支可以被 fast-forward 到它上面——source 從不被
改寫,而**整合 target 從不被改變**。過程中主 worktree 與該資料夾自己的
source worktree 都維持乾淨。若兩邊其實可以自動合併,start 會說明、撤銷該
merge、移除它建立的資源,並讓 source 維持原狀。若 source 已包含於 target,
就沒有需要 reconcile 的東西。

**你只編輯,不執行任何 Git 指令。** start 會印出 worktree 路徑、分支、
雙方 tip 與每個衝突檔案;只在那個 worktree 內解決這些檔案。

**`--continue`** 只把 Git 仍回報為 unmerged 的路徑加入索引,並驗證結果
(沒有殘留 unmerged 路徑、依 `git diff --cached --check` 沒有殘留衝突標記
或空白錯誤、也沒有衝突解決範圍以外的編輯),接著建立 merge commit、在
source 自己的 worktree 內 fast-forward source 分支,然後移除臨時 worktree
與分支。刪除前它會重新證明每個受管資源的身分——是本 repository 的
worktree、附著於受管分支、`HEAD` 為已證明的 commit、且乾淨——因此絕不會
擴大刪除範圍。由於 source 確實前進了,`--continue` 會刪除依舊 source 身分
寫成的 receipt:資料夾 receipt,以及當批次 receipt 記錄的任一 source 身分
已不再成立時的批次 receipt(批次 receipt 本質上是全有全無)。連讀都讀不了
的批次 receipt 會原地保留供檢查,而不是被抹除。

**Reconcile 不是證據,也不是批准。** `--continue` 不執行聚焦任務測試,也
不執行完整驗證,更不寫 receipt。證明解決後的 source 是之後由人明確啟動的
`assent verify FOLDER`——那個昂貴步驟,對當下的 target 執行;批准則是之後的
`assent accept FOLDER`,它仍要求一份 fresh、可重現的 `PASSED` 完整驗證
receipt。若 target 在 start 之後前進,擷取到的 merge 不會被改寫;drift 會被
回報,而之後那次 `verify` 才是權威。

**中斷與拒絕**都可復原,且從不具破壞性。沒有狀態檔:之後的執行會讀取
worktree、臨時分支、`HEAD`、`MERGE_HEAD` 與 merge parents,判斷前一次執行
走到哪裡,因此 `--continue` 能接續某次中斷執行已提交的 merge,或補完只差
的 fast-forward。一旦有不相符之處——source 分支獨立移動、受管路徑不是
worktree 或位於別的分支、已加入索引的解決有驗證問題——該次執行會拒絕並保留
worktree、分支與每一筆編輯;不提交任何東西,也不刪除任何東西。

**`--abort`** 放棄這次嘗試:它只移除受管的 worktree 與臨時分支,且只在證明
各自確實是它所管理的資源之後才移除;當 worktree 仍有未提交變更時它會拒絕,
而不是丟棄成果。source 與整合 target 都維持不變。

Reconcile 刻意不是整合引擎。它只處理單一資料夾對當前整合 target;它從不替
你解決檔案內容、從不合併投機性的同儕資料夾、從不執行 AI adapter,也從不改
任務狀態。只在建置批次 candidate 時、於兩個未被接受的 source 之間出現的
conflict 不屬於本指令——那組仍走 `verify --batch` 的略過決定。若 peer-only
conflict 前方已有相容工作,先驗證並接受該工作,再對 target 前進後的衝突資料夾
reconcile;`assent rework` 與 `assent reject` 仍是明確替代方案。

## 平行執行

可在 N 個終端各自指定不同的工作資料夾執行,例如 `assent run parallel01`、
`assent run parallel02`;也可用 `assent run --all --jobs N` 由調度器依資料夾
依賴安排平行執行。`run --all` 維持單一前景終端,並把各子行程訊息即時顯示為
`[工作資料夾] 訊息`;平行執行時可由前綴辨識每一列的來源。

家長終端會顯示上述帶前綴訊息,根層 `.assent/_assent.log` 只保存啟動標頭與
工作資料夾啟動、完成或失敗等調度摘要。各工作資料夾自己的 `_assent.log`
則由子行程保存完整原始輸出,不含家長前綴且不會重複寫入。各資料夾內的任務
與日誌分別使用 `tNNN_name.toml`、`rNNN_name.toml`。每個工作資料夾都有自己的
`assent.lock`,同一資料夾
同時只允許一個 run;Git 永遠啟用,每個資料夾一律使用
`<專案名>.worktrees/<資料夾>/` 的獨立 worktree,這是安全平行處理的基礎。

### 鎖檔是診斷資料,不是鎖本身

`assent.lock` 在 run 結束後刻意留在磁碟上。它只是一份診斷紀錄——上一次 run 的
PID、開始時間與資料夾名稱——沒有任何判斷會依據它的內容或存在與否。

真正的所有權是綁在開啟檔案 handle 上的 OS 層級互斥鎖(Windows 用 msvcrt,
POSIX 用 fcntl),在行程存活期間持有。正常結束、Ctrl+C、當機與強制終止都會由
OS 關閉 handle 而自動釋放。因此這裡不存在 stale lock,沒有 PID 重用問題,也沒有
任何清理程序:

- 不要把檔案存在解讀成「有 run 正在進行」——只要該資料夾跑過一次,它就會一直在。
- 不要為了「復原」而刪除它。刪除只會引入 race 而修不好任何事;下一次 run 會重用
  它,`assent archive` 甚至會在它不存在時建立,因為缺少鎖檔正是「沒有人持有這個
  資料夾」的證明。
- 資料夾若真的忙碌,下一次 `assent run` 會在取鎖失敗時明講。那個拒絕才是訊號,
  檔案不是。

紀錄的 PID 唯一能告訴你的,是關於一個「還活著」的行程:`assent run --all` 在
任何離開路徑上——包含拒絕與調度錯誤——都要等它擁有的工作資料夾子行程全部結束
並被回收,才會結束自己的中斷處理。所以紀錄的 PID 若仍存活,那就是一個真正還在
跑的行程,值得等待,而不是需要手動清理的殘留檔案。

值得知道的限制與 stale 無關:`flock` 與 `msvcrt.locking` 的語意在部分網路檔案
系統上並不可靠,因此這個鎖只保證本機檔案系統上的互斥。

版控邊界刻意簡單:`AGENTS.md` 是專案規則;有進 Git 時使用 worktree 內的
分支版本,未進 Git 時由提示詞提供主樹絕對路徑。整個 `.assent/` 是 assent
管理面,由 `.gitignore` 排除並只留在主工作樹。調度器同樣以絕對路徑提供
t/r 與預設驗收腳本(主樹路徑)與兩份契約(`~/.assent` 路徑);驗收腳本雖從主樹
載入,執行 cwd 仍是 worktree。任何 `.assent/` 檔案已進 Git 時,調度器會在開
session 前 fail-closed 拒絕執行,避免 worktree 出現第二份真本。

AI 會議在主樹進行。從主樹可直接用 `git worktree list`、`git log <分支>` 與
`git diff main...<分支>` 審查各 worktree 的 checkpoint,不必進入其目錄。

平行執行的固有代價是額度共享,以及各分支 merge 回主線由人負責。

## 使用循環(三幕)

**第 1 幕:規劃會議**(互動 session)

```text
開始規劃。請讀 AGENTS.md、~/.assent/instructions.md 與 ~/.assent/format.md,
然後跟我討論以下目標,把共識逐步寫成 .assent/<工作資料夾>/ 的任務檔:
<你的目標>
```

會議中每達成一項共識就落成任務檔;散會前跑 `assent check`,不過就是還沒開完。

**第 2 幕:無人值守執行**:`assent run`,去睡覺。每個 task session 只跑該任務的
focused verify;資料夾完成後是否還在 AI session 外建立臨時 integration candidate
並執行完整 `.assent/verify.py`,取決於 `assent.toml`「[verification]」的
`receipt_refresh`:預設 `"manual"` 把 per-folder receipt 留給之後顯式的
`assent verify` 路徑;`run --verify` 會把 selected 或動態的 run-level verification
當成同一次呼叫的立即交接;`"auto"` 則在資料夾全部任務完成時的 run 收尾就執行。

`assent verify <FOLDER>` 是零 token、可離席執行的完整驗證 receipt refresh,不改
target、不開 AI session;單一資料夾路徑會在 receipt 更新後 refresh `_report.md`,
所以報告與 receipt 描述同一個最新的資料夾驗證結果,而且這個 best-effort 寫入
不會改變 verification 結果。`assent verify --batch` 則對每個已完成、尚未整合的
資料夾一次做同樣的事,但不會順便刷新 folder report。兩者的報告都會顯示
`PASSED`/`FAILED` 與 `fresh`/`stale`,stale 時可在無人值守階段重新 refresh。直接
`assent accept <FOLDER>` 與 selected
`assent accept A B` 沒有相符的新鮮 `PASSED` receipt 就拒絕,且從不自行啟動
verifier;`assent accept --all` 則依 fresh batch release,或 batch evidence 缺少/
過期時的刻意逐資料夾 verify-then-accept 模式執行。

打包的 `.assent/verify.py` 同時檢查 candidate working tree 與 candidate `HEAD` 相對
第一父提交的 committed delta 是否殘留合併衝突標記。只有空白差異時不阻擋驗證,
包括換行格式、行尾空格或 tab,以及檔尾空白行;需要格式政策的專案可選用或加入明確
的 formatter 檢查。新的 `assent init` 會選擇真正的專案測試:平行 unittest、
pytest、npm test、Flutter test 或 custom 命令,並把它安全地渲染成 argv。打包
template 內的所有專案測試範例都維持註解,只有新專案副本啟用選中的一個;測試尚不
存在時,新的 verifier 會失敗而不會從空骨架回報 `verify: OK`。

重跑初始化時,`assent init` 永不覆寫既有 verifier,已有 verifier 時提供 `--test`
會拒絕。它會用打包版本取代 `~/.assent/format.md` 與 `~/.assent/instructions.md`,
並把打包 `assent.toml` 中遺漏的 active 設定補進 `~/.assent/assent.toml`,不改既有
或自訂值。無效 TOML 或輸入會在任何受管檔案改動前拒絕。verifier digest 改變會使舊
receipt stale,應在無人值守驗證時執行 `assent verify <FOLDER>` 後再請人接受。

**自己重跑驗證**:任務的 focused `verify` 命令記錄在該任務
`tNNN_name.e.toml` 的 `verify` 欄位,可在該工作資料夾的隔離 worktree
`<專案>.worktrees/<資料夾>/` 內直接執行同一命令。`assent run` 的執行輸出
會把同一段文字印成 `verify: <command>` 這一列,緊接著印出 `verify passed
(exit 0)` 或 `verify failed (exit N)`,因此這一列印出的就是可手動重跑的
原文命令。完整階段由 `assent verify <FOLDER>` 在臨時整合候選中執行；候選
的路徑形狀是 `<project>.integration/target-<uuid>`,它和
`<project>.worktrees/` 同層,使用分支
`assent-integration/<folder>/<uuid>`。這是合併後、由完整
`.assent/verify.py` 驗證且由 receipt 認證的樹;它在整套測試執行期間都存在,
測試結束後移除。要手動重現或觀看逐測試輸出,須在候選仍存在時以它作為
cwd,並從主工作樹執行 verifier,例如 `python <main-worktree>/.assent/verify.py`;
不要把 source worktree 當成整合候選。清理在 `finally` 中進行,所以正常結束、
Python 例外與 Ctrl-C 都會清除;只有硬殺(例如 `taskkill /F`)或斷電可能留下
殘留。assent 沒有自動回收殘留的機制。
不要手動對殘留執行 raw Git worktree 移除或遞迴刪除;保留確切的 candidate
路徑與分支作為 recovery 證據,交由 Assent 的 recovery/retry 路徑重新證明
ownership、盤點目錄連結與其他目錄 reparse point,並先解除每個連結物件再進行
遞迴移除。若無法完成證明,路徑、分支與外部目標都留在原處。

**連結目標清理警告:** Assent 在任何遞迴 Git 或檔案系統移除前,都會先解除每個
目錄連結物件,絕不沿解析後的目標路徑走訪。外部連結目標在成功、拒絕、失敗、
中斷與重試後都存活。這適用於 `clean`、`archive`、`reject`、reconcile、設定失敗
清理與臨時 verification candidate;刪除連結物件不等於沿解析後的路徑刪除內容。

**候選仍需要的被忽略輸入**:`git worktree add` 只用被追蹤的內容建出候選,
被忽略的路徑因此不在其中。完整驗證只會從每個進入候選的 source worktree
鏡射兩種產物,位置可以在根層,也可以巢狀在被追蹤的上層目錄底下:一是 Assent
依已審閱 profile 佈建的被忽略目錄連結(Windows junction 與目錄符號連結、POSIX 目錄符號連結),
例如巢狀的 `lib/l10n/arb`;二是夾在被追蹤目錄之中的一般被忽略葉節點檔案,
例如生成在被追蹤原始碼旁邊的 `lib/models/task.g.dart`。目錄會鏡射成指向同一個
已解析目標的連結,檔案則鏡射成候選端指向 source 檔案的連結(Windows 用同磁碟區
硬連結,POSIX 用檔案符號連結)。過程不複製內容,也不需要你手動準備硬連結分身或
把生成檔改成符號連結。

其餘一律排除。整棵被忽略的目錄樹會被剪除而不是走訪,因此 `.git`、`.assent`、
建置產物、快取、憑證、編輯器狀態,以及被鏡射連結目標內部的所有路徑都不會被列舉;
上層路徑不屬於候選被追蹤樹的檔案同樣不會。目的地必須在候選中不存在且被 Git 忽略;
被鏡射的產物永遠不會取代或遮蔽被追蹤的內容。多個資料夾合成一個候選驗證時,
它們的產物取聯集——同一路徑對應同一目錄目標、或同一檔案內容摘要,視為單一產物;
而目標衝突、檔案內容不同、種類不一致、彼此重疊、連結懸空或目的地已被占用,
都會在 verifier 開始執行之前、也在任何 `PASSED` receipt 產生之前拒絕。這些鏡射
只在該次 verifier 執行期間存在,並在臨時 worktree 移除之前先移除,因此你的
source worktree 連結、生成檔與外部目標,在通過、失敗與 Ctrl-C 之下都同樣存活。
沒有任何 force 旗標或專案設定可以放寬這一點。

同一條規則也寫在 `assent init` 安裝的排程任務指示裡,因此需要被忽略輸入的無人
值守 session 會被告知用 `assent shared-paths review` 記錄需求,由 Assent 連到主
worktree 的精確相同相對路徑目標,而不是複製整棵樹——複製只能通過
focused 檢查,之後就會在候選中被剪除。若完整驗證真的失敗在某個 source worktree
實體持有的一般被忽略目錄底下的路徑,例如 `pkg/fl_chart`,該次失敗會保留原本的
輸出與 exit code,並附上一則 `Ignored input diagnosis:` 註記:`pkg/` 是刻意不放進
候選的,修法是在主路徑放置一般且被 Git 忽略的必要目標,再用 review 命令記錄;
不是複製內容,也不是手動建立 source 連結。分隔
符號會先正規化,只回報 verifier 輸出自己指名的目錄,而單一資料夾、selected、
動態 batch 與定位執行都會把它記進各自存放 failure summary 的 receipt。

**已審閱的共享被忽略目錄**:手動建立 source 連結不是備援。一個專案真正共享哪些被忽略
目錄,是 Assent 只審閱一次、之後就快取在主 worktree 那份未被追蹤、永不提交的
`.assent/manifest.toml` 裡的決定。`[shared_paths]` 以整份 profile 保存——宣告的
目錄、判定何時該重新考慮的精確被追蹤相依/建置檔 `watch`,以及那些檔案加上倉庫
被追蹤 Git-ignore 規則的指紋——並以指紋為鍵保留,因此相依結構不同的兩個分支各自
保有自己的答案,不會互相覆蓋。source 快照因而處於 `UNKNOWN`(尚無答案)、
`REVIEWED-NONE`(相符且 `paths = []` 的 profile:這是真正的答案,之後的 session
不建立任何連結,也不附加 AI 探查指示)、`REVIEWED-PATHS`(Assent 在你的任務開始前
自行把每個宣告目錄佈建為指向主 worktree 相同相對路徑的 junction 或目錄符號連結),
或 `STALE`——被 watch 的檔案變動、宣告目標消失、型別改變或不再被忽略,或某則
`Ignored input diagnosis:` 指名了 profile 未宣告的目錄。相符但互相矛盾的 profile
一律 fail closed;一次受控 review 會取代所有與目前 source 快照相符的 profile,
即使 watch 集合不同,並保留真正不相符的分支 profile。

`assent shared-paths review --path DIR --watch FILE`(或 `--none --watch FILE`)
是唯一的寫入者。它先驗證每個值,取得一個專案本地鎖,再以原子方式取代整個檔案,
因此並行嘗試會被拒絕,中斷後留下的要嘛是先前完整的 manifest,要嘛是完整的新版。
`UNKNOWN` 與 `STALE` 會在下一個已排程 session 附加一則有界的審閱指示,並在契約
settled 之前擋住該任務 closeout;指紋沒變則完全不花成本。每一條 verify 入口——單一
資料夾、selected 與動態 batch、定位前綴、`run --verify` 與 `--focus`——都會在任何
verifier 開始前分類 source 並調和其連結,`assent reconcile` 在建立受管資源前也一樣。
folder 與 batch receipt 會記錄 `shared_inputs_sha256`,涵蓋選定 profile 與每個宣告
目標在 verifier 前後的有界快照。每個 source 的被忽略目錄連結必須精確等於 active
profile 並指向那些主目標;未宣告的手動連結會在驗證前拒絕,並使既有 folder/batch
證據及 folder report freshness 過期或無效。acceptance 在推進 ref 之前再次核對;
一般被忽略葉節點檔案仍自動處理,不成為 manifest path。

沒有東西可宣告的倉庫則完全不花成本。`NO-IGNORED-DIRECTORY-CANDIDATE` 只表示:
一次成功的 Git ignored-entry 查詢在主 worktree 找不到任何位於 `.git/` 與
`.assent/` 之外、實際存在的一般被忽略目錄;它不是「這個專案在語意上不需要共享
輸入」的主張。這個狀態不需要 manifest profile、不建立 junction、也不動用 AI 審閱
即告 settled,並在 receipt 摘要中帶有一個與 `REVIEWED-NONE` 不同的身分,且在每一
道適用的關卡都廉價地重新計算。它在各個方向都 fail closed:Git 查詢失敗是可行動的
拒絕,絕不被轉換成空的候選集合;被忽略的葉節點檔案不算候選,而任何一般被忽略
目錄都算——即使之後審閱的答案是 `paths = []`;若之後出現候選目錄,下一次分類就是
`UNKNOWN`;完整 verifier 的 `required_evidence` 指名缺少的目錄時也絕不以此狀態
結案——主 worktree 有合法目標時它轉為審閱,否則以精確的「目標缺少或未被忽略」
問題拒絕。這個查詢之所以問主 worktree,是因為每個被允許的連結目標都必須是主
worktree 同一相對路徑上實際存在、被 Git 忽略的一般目錄;只存在於尚未接受之 source
分支上的目錄尚不可佈建,必要時會發出可行動的拒絕,而不是宣稱「不需要」。

**平行執行測試**:在 `assent init` 選 `unittest` 會啟用打包的
`run_unittest_parallel()`,把 `tests/test_*.py` 底下每個模組各自丟進獨立
subprocess 平行執行,而非單一行程依序跑完整套件。打包 template 會把這個以及
pytest、npm、Flutter 範例都留在註解中,直到選定一個。之所以用行程隔離而非執行緒,
是因為 unittest 模組會改動行程層級的全域狀態(`os.chdir`、`os.environ`),
共用同一個直譯器會讓模組間互相汙染。並發數預設是 `min(模組數, CPU 數)`,
可用 `ASSENT_VERIFY_JOBS` 覆寫。選擇命令會改變產生的 verifier digest,使既有 receipt
過期一次;重跑 `assent verify <FOLDER>`
即可換發。

worktree 是變更隔離、衝突管理、稽核與復原邊界,不是安全 sandbox。`danger-full-access`
或 `bypassPermissions` 下,AI 仍可使用其 OS 身分可存取的 network、credential、外部
Git 寫入者與 worktree 外檔案。只有在可信任的專案與帳號環境才應啟用無人值守執行;
Assent 不提供 container/VM sandbox,也不攔截這些外部效果。

**第 3 幕:驗收小會議**(互動 session)

先自己讀 `_report.md`(它就是議程表:進度、BLOCKED 卡點、檢查點 hash),
再對要裁決的任務開 session:

```text
請讀 .assent/<資料夾>/t003_xxx.toml、r003_xxx.toml 與
auto(<資料夾>/t003) 對應 commit <hash> 的 diff,
說明卡點並提出修正方案。
```

裁決落實 = AI 改任務檔(status 改回 TODO、補說明、加任務、標 SKIP),
`assent check` 過了回第 2 幕。`DONE` 仍是執行主張;receipt 是 scheduler 證據,
不是批准。人類讀報告後,直接 `assent accept <FOLDER>` 只快速重建 candidate,
比對 source tip、integration tree、verifier digest,需要 fresh `PASSED` receipt
才發布且不執行完整測試;selected `assent accept A B` 也只發布恰好相符的
batch receipt,不驗證。`assent accept --all` 是例外:fresh batch receipt 會原子重播,
batch evidence 缺少/過期時才走逐資料夾驗證 fallback。沒有 task `review` 欄位。
遠端同步仍是獨立的一般 Git 決定,最後可執行 `assent clean <FOLDER>`。循環到需要重做的任務
都完成並由人類接受。
審查或驗證某個仍存活、尚未被接受的資料夾時,若發現的是該資料夾本身目標的
修正、補齊或驗證,優先以新編號的任務附加到同一個資料夾,不要另開資料夾;
既有任務一律不改寫、不重新編號。目標確實不同、相關資料夾已被接受/封存/駁回,
或依賴與 `base` 隔離需要獨立的 source lineage 時,才開新工作資料夾;
舊資料夾可由 `_folder.toml` 的 `after`
繼續作為前置參與依賴判定。資料夾完成由任務檔推導,全部任務為 DONE/SKIP
才算完成。

## 指令參考

`run`、`status`、`check`、`report` 的完整形式都是
`assent <指令> [選項] [FOLDER]`。`FOLDER` 可明示工作資料夾;省略時 `run`
會依任務現況與 `_folder.toml` 的 `after` 前置推導唯一可執行資料夾,有歧義
就拒絕。`status`、`check`、`report` 省略時作用於全部資料夾。`--config PATH`
選擇專案層設定檔,預設為 `.assent/assent.toml`。該檔是疊在
`~/.assent/assent.toml` 之上的選用覆寫層,同時也用來定位專案(專案根目錄就是
該路徑所在 `.assent` 目錄的上一層),因此即使檔案不存在,這個路徑仍有意義。
設定檔不再維護工作資料夾指標。`--config` 與 `FOLDER` 彼此正交,可以只用其中一個,
也可以同時使用,例如
`assent status --config configs/night.toml parallel01`。

工作資料夾名稱必須是可攜的 Windows/Git-ref 名稱:不可為空,不可含空白、路徑
分隔符、控制字元、Git-ref 禁用字元(`~`、`^`、`:`、`?`、`*`、`[`),或 Windows
禁用字元(`<`、`>`、`"`、`|`);不可 `-` 或 `.` 開頭,不可含 `..` 或 `@{`,不可用
`.` 或 `.lock` 結尾,也不可使用保留的 Windows 裝置名稱。它會成為 Git branch
prefix,所以建立 worktree 或 branch 前就會先驗證。

`assent verify <FOLDER>` 是單一資料夾的零 token 完整驗證 receipt refresh,不改
target、不開 AI。`assent verify A B` 是精確 selected batch:將 A、B 依賴排序,
只建一個 candidate、只跑一次完整 verifier,並寫一份只涵蓋該集合的 batch receipt。
`assent verify <FOLDER> --focus` 則在 source worktree 重跑 distinct DONE-task checks,
不寫 receipt,不能授權接受。

`assent accept <FOLDER>` 是人類明示批准,從不執行完整測試;除了 ancestry no-op,
必須有 fresh 且完全匹配的 `PASSED` receipt,才快速重建 candidate 並記錄受保護的
`--no-ff` merge。`assent accept A B` 必須有恰好 A、B 的 fresh batch receipt,
不驗證,一次發布全部 selected 或一個也不發布。`assent accept --all` 是刻意的兩種
模式例外:fresh PASSED batch receipt 原子重播;batch evidence 缺少或過期則逐資料夾
verify-then-accept。malformed batch evidence 會拒絕,不 fallback。receipt 是可刪除
重建的 derived evidence;內容變更會 stale。direct 與 selected 不會擴大集合或啟動
verifier,也不連線 remote、`--push`、pull、rebase、force push、自動解衝突或刪 source;
integration lock 不能阻止外部 Git 寫入。接受期間不要在同一主工作樹執行寫入 Git
命令。source 已整合時重跑是冪等 no-op。

`assent clean [FOLDER ...]` 只刪除已完全併入且乾淨的 worktree 與分支;證明不了就跳過,
不碰 `.assent/`,也沒有強制選項,且與 `git clean` 無關。

`assent reject <FOLDER>` 是人工裁決的明示駁回動作,與常規 clean 分流:先把
未提交變更封存為 wip commit,印出各分支完整 tip hash 存證後強制刪除該
資料夾的 worktree 與同前綴分支(僅 gc 期限內可用 hash 救回),再把 DONE/
WIP/BLOCKED 任務改回 TODO 並在 r 檔留下含完整 Git 存證的 `rejected`
記錄(SKIP 不推翻)。`FOLDER` 必填,不可作用於全部資料夾;run 進行中拒絕執行。

`assent rework <FOLDER> <TASK>` 是單一任務的非破壞性重開。預設保留所有程式碼,
只把目標狀態改回 TODO;有已開始或已完成的下游時必須明示 `--cascade` 才連動。
`--reason TEXT` 保存裁決理由。`--revert-code` 採 fail-closed:只有目標範圍的
checkpoints 構成目前分支的連續尾段才會建立新的反向 commit,絕不改寫 Git 歷史。
命令成功後重生報告,但不自動執行 `run`;預檢、狀態或報告更新失敗皆回傳失敗。

兩項舊設定已廢除:工作資料夾不再由設定檔中的手工指標維護,Git 也沒有停用
開關或無 Git 降級模式;工作資料夾由命令列明示或依任務事實推導,Git 永遠啟用。

| 指令與代表性命令 | 選項與作用 | token 消耗 |
|---|---|---|
| `assent run [FOLDER]`<br>`assent run parallel01` | 執行工作資料夾,直到任務全為 DONE/BLOCKED/SKIP。省略 `FOLDER` 時推導唯一可執行資料夾;`--once` 只執行下一個任務後停止;`--task ID` 指定單一任務且仍檢查前置,例如 `assent run --task t003 parallel01`。 | 僅執行 AI session 時消耗;`--once` 或 `--task` 最多執行單一任務 |
| `assent run A B`<br>`assent run A B --all`<br>`assent run A B ...` | 只依序執行 A、B,第一個失敗就停止。加 `--all` 時,再依依賴順序執行其餘未完成資料夾;字面 `...` 則是把其餘每個資料夾接在後面,整批在開跑前先定格成一個選擇。`...` 與 `--all` 不可併用,兩種形式也都不暗中驗證或接受。 | 僅執行 AI session 時消耗 |
| `assent run --all --verify`<br>`assent run A B --verify` | 先執行,只有在 exit code 為零時,才執行與選擇一致的完整驗證:一個資料夾寫 folder receipt,明示的多資料夾選擇寫該 selected batch,`--all` 或單獨的 `...` 則是全專案 batch。驗證的 exit code 就是這道命令的 exit code。與 `--once`、`--task` 併用時,只有在該次受限執行讓所選資料夾變成完成時才驗證;資料夾未完成則此請求失敗且不寫下 receipt。 | 僅執行 AI session 時消耗;驗證本身為**零** |
| `assent run --all`<br>`assent run --all --jobs 2` | 依 `_folder.toml` 的資料夾依賴順序執行全部未完成資料夾;`--jobs N` 限制同時執行的資料夾數(預設 1),家長終端以 `[工作資料夾] 訊息` 即時標示各子行程輸出。 | 僅執行 AI session 時消耗 |
| `assent status [FOLDER]`<br>`assent status parallel01` | 顯示進度統計、下一個任務、分支與最後檢查點。接受 `--config PATH`。 | **零** |
| `assent check [FOLDER]`<br>`assent check --config .assent/assent.toml parallel01` | 驗證任務檔格式、依賴無循環、設定與環境,是規劃會議的散會條件。接受 `--config PATH`。 | **零** |
| `assent report [FOLDER]`<br>`assent report parallel01` | 生成並顯示工作資料夾內的人讀報告 `_report.md`。接受 `--config PATH`。 | **零** |
| `assent verify <FOLDER>`<br>`assent verify parallel01` | 對單一資料夾的臨時 integration candidate 執行一次完整 verifier 並刷新 derived receipt;不改 target、不開 AI session。報告顯示 `PASSED`/`FAILED`、`fresh`/`stale`。 | **零** |
| `assent verify A B`<br>`assent verify A ...` | 依依賴順序只驗證 A、B,一個 candidate、一次完整 verifier,寫一份只涵蓋該集合的 batch receipt;`--no-bisect` 只適用於 batch。selected conflict 會拒絕,不跳過。`A ...` 把其餘已完成資料夾一併展開進來,仍是 exact selection。 | **零** |
| `assent verify <FOLDER> --focus`<br>`assent verify parallel01 --focus` | 在 source worktree 重跑 distinct DONE-task verify 命令;不寫 receipt,不能授權接受。 | **零** |
| `assent accept <FOLDER>`<br>`assent accept parallel01` | 人類明示批准單一資料夾。從不執行完整驗證;除了 ancestry no-op,需要 fresh 且精確的 `PASSED` receipt 才快速重建 candidate。 | **零** |
| `assent accept A B`<br>`assent accept A ...` | 人類明示批准恰好 A、B,只接受相符的 fresh batch receipt;依賴排序後重播、不驗證,一次發布全部或一個也不發布,不擴大也不 fallback。`A ...` 也選入其餘已完成資料夾,並且同樣要求恰好涵蓋展開後集合的證據。 | **零** |
| `assent accept --all` | Fresh PASSED batch receipt:不新增驗證、原子重播。Evidence 缺少/過期:依賴順序逐資料夾 `verify_folder_if_needed` 再 accept,失敗即停但保留先前成果。Malformed evidence 會拒絕;已整合 no-op、source 已清除則跳過。 | **零** |
| `assent reconcile <FOLDER>`<br>`assent reconcile --continue parallel01` | 在隔離 worktree `<project>.reconcile/<FOLDER>` 內準備單一已完成資料夾的 source 對 target conflict,讓人工編輯被回報的檔案;`--continue` 把該解決加入索引並驗證、提交 merge、fast-forward source 分支;`--abort` 只丟棄已證明的受管 worktree 與分支。從不改 target、不解決內容、不執行聚焦或完整驗證、不寫 receipt、也不接受。`FOLDER` 必填;沒有 `--all`。 | **零** |
| `assent clean [FOLDER ...]`<br>`assent clean A B`<br>`assent clean A ...` | 只清理已完全併入且乾淨的 worktree 與同資料夾前綴分支;任何證明不足就跳過,不碰 `.assent/`,且沒有強制選項。可指定一個、多個或字面 `...` remainder,都不指名時作用於全部工作資料夾;多個資料夾在一趟 upstream-first 流程裡清理。 | **零** |
| `assent archive <FOLDER ...>`<br>`assent archive --all`<br>`assent archive --restore FOLDER` | 讓已完成資料夾退場:內含 `clean`,再把計畫壓縮進 `_archive/` 並登錄到 roster。被指名的資料夾遵守單一資料夾 `archive` 的契約 —— 每個都會嘗試,只要有不合格的就以非零 exit code 結束 —— 而 `--all` 遇到不合格的資料夾只是略過,不算失敗。`--restore` 只還原一份封存,不接受 `--all`,也不接受 `...`。 | **零** |
| `assent reject <FOLDER>`<br>`assent reject parallel01` | 人工裁決駁回:封存未提交變更後強制刪除該資料夾的 worktree 與同前綴分支(刪除前以完整 tip hash 存證),並把 DONE/WIP/BLOCKED 任務改回 TODO、r 檔保存 Git 存證。`FOLDER` 必填;run 進行中拒絕。 | **零** |
| `assent rework <FOLDER> <TASK>`<br>`assent rework parallel01 t003 --cascade --reason "驗收不符"` | 非破壞性重開單一任務;預設保留程式碼,`--cascade` 明示連動下游。`--revert-code` 僅在 checkpoints 是連續分支尾段時建立新反向 commit。成功後更新報告,不自動執行 run。接受 `--config PATH`。 | **零** |
| `assent init --test CHOICE`<br>`assent init --path C:\work\my-project --test pytest` | 安裝使用者家目錄 `~/.assent`(共用設定與 `instructions.md`、`format.md` 兩份契約),以及專案的 `.assent/verify.py`、`AGENTS.md` bridge 那一行與 `.gitignore` 條目,並精確選一個真正的專案測試:平行 unittest、pytest、npm test、Flutter test 或 custom argv。新鮮 init 省略 `--test` 時顯示編號選單;重跑不提問、保留既有 verifier、刷新兩份使用者家目錄契約,並只補入遺漏的 active 設定。專案內舊契約副本只有與打包文字完全相同時才移除;專案 `assent.toml` 保留為覆寫並回報。無效輸入/TOML 會在任何受管檔案改動前拒絕。 | **零** |
| `assent doctor`<br>`assent doctor` | 診斷機器環境(Python 版本、git、adapter CLIs、temp 目錄可寫性);不需要 `FOLDER` 或 `--config`,也不需要既有的 `.assent/` 專案就能執行。 | **零** |
| `assent --version` | 印出 `assent` 加上已安裝的 distribution 版本後離開;不需要專案或子命令即可執行。 | **零** |

各子命令的 `-h`/`--help` 會顯示該層實際語法;頂層沒有可套用到所有子命令的
`--config` 等全域選項。

## Adapter、模型檔位與 effort 等級

Assent 透過可插式 adapter 支援不同的 AI CLI 工具。每份任務檔用抽象**檔位**
(`prime`、`core` 或 `lite`) 替代具體模型名稱;adapter 的組態表會把該檔位轉成
這次執行的實際 CLI 模型。同樣地,任務可要求抽象 **effort** 等級(`heavy`、
`normal` 或 `slight`),adapter 會轉成廠商的具體 CLI 值(如果支援的話)。

### 支援的 adapter

**Claude** (`adapter.name = "claude"`)

```toml
[adapter.claude]
command = "claude"
extra_args = ["--permission-mode", "bypassPermissions"]

[adapter.claude.models]
prime = "fable"      # Fable 5 — 最快檔位
core  = "opus"       # Opus 4.8 — 平衡檔位
lite  = "sonnet"     # Sonnet 5 — 高效檔位
```

**Codex** (`adapter.name = "codex"`)

```toml
[adapter.codex]
command = "codex"
extra_args = ["--sandbox", "danger-full-access"]

[adapter.codex.models]
prime = "gpt-5.6-sol"    # 最大模型
core  = "gpt-5.6-terra"  # 平衡模型
lite  = "gpt-5.6-luna"   # 高效模型
```

**Antigravity** (`adapter.name = "antigravity"`)

Antigravity adapter 透過 `agy`(Antigravity CLI) 執行 Google 的 Gemini 模型,
是一份自由安裝的本地 CLI,每台機器需互動登入一次。本 adapter 用 print mode
(純文字輸出、無 JSON 事件) 通訊,在開啟 session 前有 model/effort 組合的
preflight 驗證。

```toml
[adapter.antigravity]
command = "agy"
extra_args = ["--dangerously-skip-permissions"]

[adapter.antigravity.models]
prime = "gemini-3.1-pro"   # Gemini 3.1 Pro — 最高品質
core  = "gemini-3.6-flash" # Gemini 3.6 Flash — 平衡(新)
lite  = "gemini-3.5-flash" # Gemini 3.5 Flash — 高效

# Antigravity 各檔位的 effort 翻譯。下面說明每個。
[adapter.antigravity.default_effort]
prime = "heavy"
core  = "heavy"
lite  = "heavy"

# Gemini 3.1 Pro 只支援 low 和 high effort,沒有 medium。
# 為了品質,normal 翻譯上升為 high(絕不無聲降級)。
[adapter.antigravity.efforts.prime]
normal = "high"

# Gemini 3.5 Flash 只支援 low 和 medium,沒有 high。Lite 檔位的 heavy
# 翻譯為 medium(這個家族的上限),在組態表裡可見,可覆寫。
[adapter.antigravity.efforts.lite]
heavy = "medium"
```

### 模型/effort 矩陣

任務檔指定抽象檔位和可選的 effort。Adapter 把它轉成具體 CLI 呼叫。
完整 9 宮格如下,顯示每份任務檔 (檔位, effort) 配對在各 adapter 裡的轉譯:

#### Claude adapter

| Effort | prime<br/>(Fable) | core<br/>(Opus) | lite<br/>(Sonnet) |
|--------|---|---|---|
| slight | `--model fable --effort low` | `--model opus --effort low` | `--model sonnet --effort low` |
| normal | `--model fable --effort medium` | `--model opus --effort medium` | `--model sonnet --effort medium` |
| heavy | `--model fable --effort high` | `--model opus --effort high` | `--model sonnet --effort high` |

#### Codex adapter

| Effort | prime<br/>(gpt-5.6-sol) | core<br/>(gpt-5.6-terra) | lite<br/>(gpt-5.6-luna) |
|--------|---|---|---|
| slight | `--model gpt-5.6-sol --effort low` | `--model gpt-5.6-terra --effort low` | `--model gpt-5.6-luna --effort low` |
| normal | `--model gpt-5.6-sol --effort medium` | `--model gpt-5.6-terra --effort medium` | `--model gpt-5.6-luna --effort medium` |
| heavy | `--model gpt-5.6-sol --effort high` | `--model gpt-5.6-terra --effort high` | `--model gpt-5.6-luna --effort high` |

#### Antigravity adapter (1.1.5+)

| Effort | prime<br/>(3.1 Pro) | core<br/>(3.6 Flash) | lite<br/>(3.5 Flash) |
|--------|---|---|---|
| slight | `--model gemini-3.1-pro --effort low` | `--model gemini-3.6-flash --effort low` | `--model gemini-3.5-flash --effort low` |
| normal | `--model gemini-3.1-pro --effort high` | `--model gemini-3.6-flash --effort medium` | `--model gemini-3.5-flash --effort medium` |
| heavy | `--model gemini-3.1-pro --effort high` | `--model gemini-3.6-flash --effort high` | `--model gemini-3.5-flash --effort medium` |

說明:
- **Antigravity prime/normal**: Gemini 3.1 Pro 不支援 `medium`,故 assent 改選
  `high`(品質優先對應)。這不是無聲回落—組態表裡清楚可見、可審計。
- **Antigravity lite/heavy**: Gemini 3.5 Flash 沒有 `high` effort 等級,故 `heavy`
  轉成 `medium`(該家族的最大可用)。
- **Antigravity 1.1.5 最低版**: 這是支援 `--effort`、穩定 model slug 及無人值班
  執行所需 headless 修正的版本。舊版在開啟 session 前會被拒。

### 使用 Antigravity adapter

**首次設定**

1. 在你的機器上安裝 `agy`(Antigravity CLI)(如無則裝);請參考 [Google 官方
   Antigravity CLI 安裝與認證文件](https://antigravity.google/docs/cli/install)。
2. 啟動互動式 CLI:

   ```bash
   agy
   ```

   在瀏覽器完成 Google 登入。如果 `agy` 改為印出授權 URL,請在瀏覽器開啟
   該 URL,完成提示的登入流程。
3. 在啟動無人值守的 `assent run` 前完成這次互動登入。用 `agy --version`
   驗證版本(必須 1.1.5 或更新)與 `agy models`(顯示可用模型)。

Assent 只使用 AGY 已持有的認證。它**不會**開啟登入瀏覽器、讀取或修改認證、
切換 Google 帳戶,或改變 workspace trust。若要登出,啟動互動式 `agy` 並在
CLI 提示中輸入 `/logout`;`/logout` 不是 shell 子命令。

**使用 Antigravity 的任務檔範例**

```toml
title = "用高品質推理分析程式碼"
model = "prime"
effort = "heavy"
status = "TODO"
scope = ["src/", "tests/"]
verify = "python -m pytest"

goal = "用 Gemini 3.1 Pro(最高品質)審查程式碼庫。"
```

執行 `assent run` 時,會:
1. 驗證 Antigravity 1.1.5+ 已安裝且能觸及 `gemini-3.1-pro --effort high`。
2. 用 `agy --print --model gemini-3.1-pro --effort high --mode accept-edits ...`
   開啟無標題 session。
3. 執行驗證命令並紀錄結果。

**在既有專案中切換 adapter**

改 `[adapter]` name 只需一行。既有任務檔無需改動;它們仍用 `model = "prime"` 和
`effort = "heavy"`,新 adapter 的組態表照樣轉譯。切換後,下一次 `assent check`
會在任何 session 啟動前驗證新 adapter。

### 設定模型與 effort 翻譯

`~/.assent/assent.toml` 的設定顯示如何自訂檔位到模型的對應與抽象到 CLI
effort 的翻譯。查找順序永遠是:

1. 任務檔明示的 `effort` 註記(如有)
2. 組態中此檔位的 `default_effort` 覆寫(如有)
3. 此檔位的內建預設值

寫出來的 `[adapter.<name>.default_effort]` 表是「逐檔位覆寫」,不是整張取代內建
表。因此該表缺席、為空或只寫一部分時,每個檔位仍然都有值——只寫 `lite`,
`prime`/`core` 就沿用各自的內建預設。結果是:每一次受支援的呼叫都會傳入具體的
effort 值;assent 不會省略該旗標而去沿用廠商 CLI 自己的預設。

與 effort 翻譯:

1. 檔位特定區段: `[adapter.<name>.efforts.<tier>]`
2. 平面區段: `[adapter.<name>.efforts]`
3. 內建基準表:`heavy` → `high`、`normal` → `medium`、`slight` → `low`
   (較高優先層缺少某個鍵時,每個抽象鍵都各自逐層退回平面表,再退回基準表)。

範例:如果你的 Antigravity 設定有更新的 3.1 Pro 支援 medium,可移除品質優先對應:

```toml
# 移除這行:
# [adapter.antigravity.efforts.prime]
# normal = "high"

# 或設為實際值:
[adapter.antigravity.efforts.prime]
normal = "medium"
```

### 讀懂 session 行

`run` 開啟 session 時會印出一行精簡訊息,說明整個已解析的身分:

```
  Session: codex | core->gpt-5.6-terra | heavy->high
```

讀法是:先看 adapter,再看兩組對應。每個箭頭都由左邊「任務檔寫的可攜抽象值」
指向右邊「實際傳給該 adapter CLI 的引數」——所以 `core->gpt-5.6-terra` 是這次
的 `--model` 值,`heavy->high` 是這次的 `--effort` 值。四項稽核事實
(adapter、檔位、模型、effort)都在這一行上;它維持單行,不會再展開回冗長標籤。

### 設定 Antigravity print timeout

Antigravity 的 `--print-timeout` 獨立於 Assent 的 watchdog timeout。Print
timeout 限 CLI 等待單一 print 呼叫完成的時間;watchdog 限 Assent 等待任何
輸出(殺掉 session 前)的時間。

在 `~/.assent/assent.toml`(或專案覆寫):

```toml
[adapter.antigravity]
print_timeout_minutes = 120  # AGY 最多等 2 小時得答案
```

別設低於你最長任務的預期時間;`assent check` 會驗證 print timeout 是正數。

### Antigravity 組態故障排除

**問題: `preflight failed: invalid model selection`**

Antigravity 在 preflight 拒了 model/effort 組合。檢查:

```bash
agy models                         # 看有什麼模型
agy --print --model <MODEL> ...    # 試試你的 model/effort 選擇
```

常見原因:
- **未對應的 model tier**: 加到 `[adapter.antigravity.models]`。
- **不支援的 effort**: 模型不支援那個 effort 等級。例如 Gemini 3.1 Pro
  不支援 `medium`。在 `[adapter.antigravity.efforts.prime]` 裡修正對應。

**問題: `authentication required` 或 `permission denied`**

必須先完成 AGY 的互動式 Google 登入,才能執行無人值守的任務:

```bash
agy
```

請完成瀏覽器登入;如果 `agy` 印出授權 URL,請開啟該 URL 並完成登入流程。
如果你無人值班執行 `assent run`(例如晚上),登入必須在執行前完成。Assent
無法開啟瀏覽器、幫你登入、讀取或修改認證、切換 Google 帳戶,或改變 workspace
trust;它只使用 AGY 已持有的認證。

**問題: `command not found: agy`**

Antigravity CLI 未裝或不在 PATH。見 [Google 官方 Antigravity CLI 安裝與認證文件]
(https://antigravity.google/docs/cli/install) 並用 `agy --version` 確認。

**問題: session 中途額度耗盡**

Antigravity 達配額限時,`assent run` 紀錄 `WIP` checkpoint 保留部分成果。
你的配額重設時(Google 通常日或小時級重設,視計劃),能續該任務:

```bash
assent run <FOLDER>  # 自動從 WIP 恢復
```

任務日誌紀錄確切配額重設時間(如可得)與調度器會輪詢等候的方式。
同時要跑另一份資料夾,可在第二終端跑(只要它不依賴被配額限的那份)。
若 `[adapter].name` 設為名單,額度耗盡會依序輪替到下一個 adapter;
只有整輪 adapter 都耗盡時,調度器才等待輪詢間隔。

**組態 preflight 錯誤後修正**

別改任務檔的抽象檔位或 effort。只改 adapter 組態。例如 prime/normal 對應
到 high 但想改:

```toml
# 之前
[adapter.antigravity.efforts.prime]
normal = "high"

# 之後(如果 normal 現在支援)
[adapter.antigravity.efforts.prime]
normal = "medium"
```

修正組態後,無需改 `.assent/` 管理檔;`assent check` 會重新驗證,
`assent run` 會重試。

## 計畫格式與設定檔

- 格式契約全文:[assent/templates/format.md](assent/templates/format.md)
  ——安裝到 `~/.assent/format.md`,每次成功 `assent init` 都在那裡刷新。
- 工作指示範本:[assent/templates/instructions.md](assent/templates/instructions.md)
  ——assent session 行為與跨專案共通規則;安裝到 `~/.assent/instructions.md`,
  每次成功 `assent init` 都在那裡刷新;專案規則留在 `AGENTS.md`。
- 設定檔範本:[assent/templates/assent.toml](assent/templates/assent.toml)
  ——adapter 選擇、抽象檔位(prime/core/lite)對照表、
  抽象 effort(heavy/normal/slight)的預設與 CLI 值翻譯、watchdog 與重試參數。
  第一次 init 用它建立 `~/.assent/assent.toml`;之後的 init 只在那裡補遺漏的
  active table/key path,保留既有及自訂值。

這三份是工具自己的檔案,因此每台機器各一份。專案只持有自己的 `AGENTS.md`、
`.assent/verify.py`、工作資料夾,以及——僅在舊版佈局或刻意決定放置時——一份專案
`.assent/assent.toml` 覆寫。

### 使用專案媒體檔的任務

任務需要的圖片、PDF、音訊或其他媒體都是一般的專案脈絡,所以計畫 schema 維持不
變——沒有 `inputs`、image、audio 或 video 欄位,assent 也不會替你把檔案附加給
adapter,或推測某個模型能讀哪種媒體。

- 在任務的 `behavior` 或 `notes` 裡,以專案相對路徑寫出既有媒體檔並說明用途。
  只讀取的參考路徑不必進入 `scope`。
- 任務可能建立或修改的每一個媒體檔,都必須被 `scope` 涵蓋。
- 優先使用工作樹中已納入版控的檔案以確保可重現;不要把來源媒體放進產生出來的
  `.assent/` 管理層。
- `verify` 仍然只承載客觀檢查;視覺或感受性的判斷留給人在 `accept` 決定,不會
  變成第二個審查狀態。

格式契約中附有完整範例。

## 常見問題

**Q:status / check / report 會消耗 tokens 嗎?**
不會。只有執行 AI 的 session 消耗 tokens;調度器從不把任何檔案內容塞給模型,
執行 AI 是自己用工具讀任務檔。

**Q:中途斷電/當機怎麼辦?**
先檢查隔離 worktree。若中斷已妥善處理且任務停在 WIP,`assent run` 會以
「接續」提示續作;若突然中斷留下未提交變更,調度器會拒絕 dirty worktree,
而不是自行猜測。檢查並建立檢查點後再重新執行。資料夾裡看到的 `assent.lock`
不屬於這個復原流程:行程死亡時 OS 已釋放真正的鎖,檔案只是上一次 run 的診斷
紀錄,不要動它。

**Q:執行 AI 亂改任務檔放寬自己的驗收怎麼辦?**
三層防禦:scope 豁免只有它自己的 `tNNN_name.toml` 任務檔與
`rNNN_name.toml` 日誌檔;任務檔除 status 外任何欄位被改動即驗收失敗
(逐欄位與檢查點版本比對);check 每輪驗 deps 完整性與循環。

**Q:BLOCKED 的任務會擋住全部進度嗎?**
只擋以它為前置的任務;其他任務照常繼續。_report.md 會列出所有卡點與最後日誌。

**Q:如何接 Claude / Codex 以外的 AI CLI?**
繼承 `Adapter` 並實作兩步介面。`resolve_model(model: str) -> str` 先把任務檔的
抽象檔位解析成這次實際傳給 AI CLI `--model` 的 `requested_model`;接著
engine 依設定檔把抽象 effort 翻成 `requested_effort`,再呼叫既有的
`run_task(prompt, requested_model, requested_effort, cwd) -> TaskResult`。Adapter
不另設 effort 翻譯方法,只使用收到的 CLI 實際值執行。`TaskResult` 包含
`exit_code`、`output`、`quota_exhausted`、
`reset_at`;額度偵測封裝在 adapter 內,主迴圈不感知廠牌差異。

## 專案狀態

核心完成:TOML 任務/日誌格式、九個子命令、claude 與 codex adapter、
完整 unittest 測試套件(無網路、無真實 CLI 也能跑)。設計共識見
[docs/CONSENSUS.md](docs/CONSENSUS.md)(正體中文翻譯:
[docs/zh-TW/CONSENSUS.md](docs/zh-TW/CONSENSUS.md))。
