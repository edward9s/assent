# 工作流程

*[English version](../WORKFLOW.md) · [README](../../README.zh-TW.md)*

> 本檔是 [../WORKFLOW.md](../WORKFLOW.md) 的正體中文(台灣用語)翻譯；內容如與
> 英文版不同，以英文版為準。它涵蓋規劃、執行、審查、重做、駁回與 prompt。

## 三幕流程

### 第一幕：規劃會議

互動式規劃 session 會讀取專案 `AGENTS.md`、`~/.assent/instructions.md` 與
`~/.assent/format.md`。和人類討論目標，只檢查任務直接碰到的 source 與 tests。
如果發現 source bug、壞結構或說明文件/程式行為不一致，必須回報，不要默默繞開。
不要使用子代理，也不要過度設計。

共識一邊討論一邊寫入 `.assent/<work folder>/`。每個任務是正式的
`tNNN_name.e.toml`，並有同 stem 的 append-only `tNNN_name.r.toml` journal。
欄位與檔名契約以 `~/.assent/format.md` 為準，不要把契約複製進專案。

散會前執行：

```text
assent check
```

`assent check` 通過才算可以散會；它不開 AI session，會檢查任務格式、依賴
完整性、環境與設定。沒有通過的計畫不是完成品。

### 第二幕：無人值守執行

`assent run` 選取可執行任務，開啟 headless adapter session，提供專案規則、
共用 session instructions 與指定任務。session 執行任務的 focused `verify`。
結束後，Assent 檢查 task-file structural diff、scope 與 focused 結果，才更新
status 並寫入對應 journal。

成功工作會有一個終端 `auto(work-folder/task)` checkpoint。失敗會保留編輯並附理由重試；
重試用盡後成為保留成果的 `BLOCKED` checkpoint。quota 中斷會成為具備進度的 `WIP`
checkpoint，並在等待或輪換 adapter 後以 continue prompt 恢復；除非 session 明確寫入
`BLOCKED`，否則會先把 task status 寫回 `WIP`。唯一的 provider-neutral 立即續跑控制
記錄是 `{"type":"assent.checkpoint_resume"}`，也遵循相同的 WIP 規則，不攜帶帳號、quota
或 reset 語意。恢復的 task 後來通過所有 gate 時，即使 WIP checkpoint 已保存全部檔案變更、
目前 tree 因而乾淨，Assent 仍只建立一個終端 auto checkpoint；這是只攜帶 ownership evidence
的刻意空 commit。一般有變更的成功仍只有一個包含內容的 auto checkpoint。只由舊 WIP
checkpoint 支撐的乾淨 legacy `DONE` task 會原樣保留；新規則不會改寫或捏造歷史證據。

focused verify 不等於完整 candidate verification。`[verification]
receipt_refresh` 預設為 `"manual"`，需要之後顯式 `assent verify`；設為
`"auto"` 才會在資料夾完成時刷新 receipt。`assent run --verify` 是本次呼叫的
明確要求，只在 run 成功後接續相符的完整驗證。候選樹、receipt 與報告規則見
[驗證指南](VERIFICATION.md)。

#### 可選的有界 auto-fix

`[auto_fix.review]` 是可選的 reviewer override；若沒有 table，`run --auto-fix` 會用第一個
effective worker adapter 的 `prime`/`heavy` 自動解析 reviewer，不需重跑 `assent init` 或
編輯 `~/.assent/assent.toml`。它的 `adapter` 可以是單一名稱，也可以是有序 list，list 中的
每個項目（含重複）都是一個共用該 table 單一 `model` 與 `effort` 的 review round，list 長度
就是這個 loop 的有限上限。整個 loop 是 invocation-level opt-in，只有明示 `run --auto-fix`
的 invocation 才會在 folder 完成後做 folder-level review。順序固定為：普通
task-focused verification、每個 distinct `DONE` task 的最後一次 focused sweep，然後才啟動
completed-folder reviewer round；`--once`/`--task` 若留下 incomplete folder，就延後這個 loop 且
不消耗 review token。有 durable worker `BLOCKED` 或 focused-gate 證據的 quiescent blocked
dependency，會走另一個 blocked-adjudication entry point。focused failure 不會啟動
completed-folder reviewer。沒有 `--auto-fix` 的普通 `run` 不會做這次 final sweep、review 或
repair。

`assent run --auto-fix` 是該次 invocation 進入 repair 的唯一授權，與選取正交，可和自動
選取、明示 folder、`...`、`--all`、`--once`、`--task`、`--verify` 合用。`--all` 會把
同一 policy 傳給每個 child folder；`--verify` 仍只在 run 與 auto-fix loop 成功後做
完整 verification。auto-fix 自己不建立 full candidate，也不 publish 或 accept。

completed-folder round 是 reviewer 與 fixer 合併的 session，不是嚴格唯讀的 gate：它可以直接
修好真正的 blocker，但只能寫入 finding 指名的那一個既有 task 的 declared scope，並以 verdict
`FIXED` 回報。其他任何寫入 —— management-plane 或 task 檔案、別的 task 的 scope、commit，或
寫入 primary worktree —— 都會讓該 verdict 不可用，同時保留原編輯。`PASS` 代表沒有 blocking
問題殘留且該 round 完全沒有寫入；`FAIL` 仍代表 round 自己不能修的 blocker（例如精確的
scope omission），以及維持唯讀的 blocked adjudication。

`FAIL` review 會寫入 folder 的 derived `_auto_fix.toml` 與 report。只有能對應到一個既有
task 且位於其 declared scope 的 finding，才可自動 code-preserving rework；scheduler
記錄理由 `Automatic repair of durable folder-review findings` 與
`authorization: run --auto-fix`。每個被 reopen 的 task 都以它自己原本的 task profile 修復：
沒有 escalation ladder，也沒有 consumed-profile ledger，所以中斷的 round 會以完全相同的
identity 恢復，多 task finding 與 dependency cascade 也不會逐 task 提前 escalation。
變更或直接互動程式碼中遇到的既有
technical debt 只有在 `COMPLETED_FOLDER + INITIAL` 引入、修正局部且 focused test 可可靠
驗證時才合格；blocked adjudication 與 `RECHECK` 可以保留或解決 debt，但不能新增。review
不做全 repository debt audit。reviewer 可以核准一個精確 scope addition，但只有 scheduler
能修改 task file；worker 與 reviewer 都禁止 task-file edits。未知、含糊或越界 finding 交
scheduler 作決定。不會自動建立 task、還原 source、刪 source，絕不自動接受 folder。round
用盡、quota、中斷或 gate 失敗都保留 state 與編輯，之後可用 `run --auto-fix` recovery 或交
人類事後檢視；loop 內沒有 runtime human adjudication gate。若決定 pending state 的 identity
已不在設定的 round 之中，repair 與 closeout 會 fail closed。

每個 round 讓 durable `review_round_index` 剛好前進一，走到設定 list 結尾就有限地結束自動化。
在 `FIXED` round 上用完不會馬上結算：scheduler 會先在修復後的 source 上重跑 implicated
task 自己的 focused gate 一次——沿用同一份會跳過本次 invocation 已證明過 command 的
de-duplicating ledger——所以最終「每個 task 都通過自己 focused gate」這個結算聲明，證明的
是這次修復本身，不只是修復之前的狀態。通過後才結算為 SELF-FIXED, UNREVIEWED 且 exit code
為零：不還原、不 reopen、不重新標記，被修好的 task 保留它自己 focused gate 證明的 `DONE`，
不會被改成 `BLOCKED`。這個結果是終局而非可續跑的 phase —— 之後的 `run --auto-fix` 只會再次
回報它、不再開新 round，只有人類 `rework` 能重新開啟該 folder。唯一缺的是獨立的 review
確認，而那只有人類的 `accept` 決定能提供。

若這道 settling gate 失敗，則是另一個獨立結果，與 SELF-FIXED, UNREVIEWED 及一般 `BLOCKED`
task 都不同：folder 不會結算、沒有 task 被標成 `BLOCKED`，修復、finding 與每一筆編輯都
保留 —— 而 run 以非零 exit code 結束。

在未修好的 blocker 上用完 round，不再以非零 exit code 結束：而是結算為同樣獨立的
REVIEW UNRESOLVED, HUMAN DECISION 結果，保留每一項 finding、編輯與 journal，每個 task
保留它自己 closeout 給的 status，exit code 為零。這是刻意設計：因為 folder scheduler 的
`--all` launch loop 只在沒有 folder 失敗時才持續啟動下一個，這裡的非零 exit 過去會靜默取消
同一次 invocation 中排在後面的所有無關 folder。未解決的 review finding 是 scheduler
無法決定的問題，不是 infrastructure failure，因此改由人類 acceptance meeting 決定，而不是
讓 run failure 取消其他佇列中的工作。

一個已經寫入修復的 round 若在其 verdict 記錄前被中斷，保留該編輯且不前進 round index。
下一次 run 的 startup recovery 會利用 durable state 的 current finding 所指名的 task 來
歸屬這份 dirt，證明方式與其他 recovery owner 相同的 scope-containment 判斷，並收進 `wip`
checkpoint，不開任何 AI session；若無法以此方式證明 ownership，recovery 仍會 fail-closed
拒絕，不會用猜的。

Worker 必須在 repair task journal detail 以每個 current fingerprint 一行回覆下列 exact
provider-neutral acknowledgement；`still_blocked` 只能搭配 `BLOCKED` task，scheduler 會驗證
JSON 與 fingerprint，且這不是改 task file、改 scope 或接受 folder 的授權：

```text
ASSENT_REPAIR_DISPOSITION {"fingerprint":"<64 lowercase hex>","disposition":"fixed|not_reproducible|still_blocked","detail":"concrete bounded evidence"}
```

Recheck 先處理之前的 current findings；仍存在的 blocker 保留原 fingerprint，新的 blocker
必須有 repair regression 或 newly exposed existing requirement 的證據。之前集合清空後必須
PASS；optional improvement、speculation 與重複 debt discovery 不會讓 loop 繼續。完整
verification 仍依成功 run 的 receipt policy 或明示 `--verify` 另行執行；缺 receipt、未跑
full suite 或沒有 complete verification 都不是 reviewer failure，只有與 task requirement
直接相關的具體 local focused-test gap 才能是 review finding。

### 第三幕：人類審查與裁決

先讀產生的 `.assent/<work folder>/_report.md`；它是零 token 的議程，包含進度、阻塞、
checkpoint hash 與驗證狀態。如果有 `TECHNICAL DEBT REVIEW REQUIRED`，讀同層的
`_technical_debt.md`，在建議 accept 前主動告訴人類並列舉每一項 debt；每一項都要取得
「完成的 local repair 足夠」、「追加/rework task 做具體追蹤」，或「提升成 `AGENTS.md`
durable project rule」的明確 disposition。只默讀檔案不算完成程序。接著才檢查相關任務與
journal、checkpoint commit 與 diff、實作，以及 focused/full verification 證據。

Report 的 `Folder auto-fix` 是零 token 的 derived evidence：沒有 state file 是
`NOT RUN`，新鮮的 review pass/fail 分別是 `PASSED (fresh)` 與 `FAILED (fresh)`，
`SELF-FIXED, UNREVIEWED (fresh)` 會指出 self-fixed round 的位置、用掉的 round 數、該 round
的 adapter/model/effort，以及證明這次修復的 settling-gate evidence，
`REVIEW UNRESOLVED, HUMAN DECISION (fresh)` 會指出 round 位置、用掉的 round 數、該 round 的
adapter/model/effort，以及沒有任何 round 解決的 finding —— 這與 `SELF-FIXED, UNREVIEWED`
及一般 `BLOCKED` task 都是不同的獨立結果 —— 而
malformed state 或 source/task binding 改變則是 `STALE`。settling gate 失敗的 `FIXED` round
不會進入這兩種結算 state，仍停在 `FAILED (fresh)` 並附上失敗 gate 的 command 與 evidence，
產生它的 run 以非零 exit code 結束。它也會列出 phase、context、stage、
original blocker、current findings/recommendations、scope decision 與 exact scope-amendment
transaction、repair acknowledgement 與 brief，以及 review round index 對設定 round 數的
比例。Scheduler 在 rework、中斷、repair closeout 或 round 用盡時的 status-only
transition 不會單獨讓 evidence stale；真正的 task-contract structural edit 才會。若曾有
`COMPLETED_FOLDER + INITIAL` 引入的 eligible debt，
`_report.md` 會指向 generated `_technical_debt.md`。`FAIL` 的 current findings 會列出，
但 state 或 review `PASS` 都不是 acceptance evidence。

`DONE` 代表執行 AI 主張任務完成，不是第二個 review state，也不是人類批准。
人類批准是明示的 `assent accept` 加上受保護的 Git integration。直接與 selected
accept 會重播新鮮且相符的 receipt，不會自行啟動完整 verifier；詳見
[驗證](VERIFICATION.md)。

人類可以：

- 用 `assent accept <FOLDER>` 或 exact selected batch 接受完成資料夾；
- 用 `assent rework <FOLDER> <TASK>` 重開一個任務；或
- 用 `assent reject <FOLDER>` 駁回整個資料夾。

若單一 folder 的 derived auto-fix state 是 SELF-FIXED, UNREVIEWED，accept 會在 merge 前多加
一道互動式確認。所有 receipt-based check 都已通過，缺的只是有限 round list 沒能產生的獨立
確認。Assent 會指出 self-fixed round、它的 adapter/model/effort，以及記載被修復 finding 的
`_report.md`，然後詢問 `Publish it anyway? [y/N]`。只有精確的 `y`/`Y` 會 publish；其他任何
輸入，包含非互動 stdin 的 EOF，都視為拒絕，不改任何 Git state。

若單一 folder 的 derived auto-fix state 是 REVIEW UNRESOLVED, HUMAN DECISION，accept 會以
同樣方式 gate，同樣在所有 receipt-based check 都已通過之後：Assent 會指出產生這些未解決
finding 的 round 位置與 identity，以及每個 finding 的 task、path 與 summary，然後詢問
`Publish it anyway? [y/N]`。拒絕規則相同，且不留下任何 Git side effect。同時帶有
self-fixed gate 條件與此結果的 folder，只會被問一次，並同時指出兩個原因。

`git push` 等遠端同步是另外的人類 Git 決定。只有 source 不再需要且 Assent
能證明安全時才用 `assent clean`；詳見[作業](OPERATIONS.md)。

## 規劃 prompt

下列是指定的繁體中文 prompt，保留原文：

```text
請簡潔回答，不要用子代理。如果你看到源碼有任何bug、壞結構，或說明文件與程式行為不符合，就回報我。以下是本專案需要討論的問題，不要過度設計，先徵得人類的同意，依照 assent 格式產生相關的計畫書：
1. 需求描述。
2. 需求描述。
3. 需求描述。
```

數字 placeholder 可由人類換成實際需求，但不是新的 task schema 欄位。

## 獨立驗收審查 prompt

```text
請擔任獨立的驗收審查者。請簡潔回答，不要用子代理。任何變更前，先檢查工作資料夾的 _report.md、相關任務與 journal 檔、checkpoint commit/diff、實作，以及 focused/full verification 證據。先回報有證據支持的發現：bug、結構問題、過度設計、缺少測試，以及說明文件與程式行為漂移。建議使用與實作者不同 vendor 的高能力模型做獨立 cross-review，但不要要求或編碼第二模型或自動 gate。這個一般驗收審查由人類主導，不在審查中自動 accept 或 rework；等待人類決定，只有人類同意後，才寫 Assent 格式的 rework 任務或說明 acceptance 動作。明示的 `run --auto-fix` 是另一個有界修正授權，但仍絕不自動接受 folder。
```

這個建議只是人類 workflow 指引，不會新增 model 欄位、adapter capability、
scheduler state，也不會強制使用多模型。auto-fix reviewer 仍受唯讀與寫入偵測規則
限制，`danger-full-access` 是執行權限預設，不是 security sandbox。

## 資料夾依賴與堆疊

在 `_folder.toml` 用 `after = ["A"]` 宣告 A 是順序前置條件；它不把 A 的檔案
放進 B，也不提供同檔衝突保護。只有 `base = "A"` 宣告 lineage，讓 B 的 source
worktree 從 A 的 commit 開始。沒有 `base` 時，資料夾從目前 integration target
開始；多個 `after` 或多個尚未接受的 upstream 不會造成 base ambiguity。

典型順序：

```text
assent run A
assent run B                 # B has base = "A"
assent verify A B
assent accept A
assent accept B
```

B 可在 A 接受前產生 receipt；只有 source tip、重建的 integration tree、verifier
digest 都沒變時才能重用。A 若前進，B 變 stale 但成果保留；用 rework/reject，
或開新資料夾重新規劃。Assent 不重寫 stack history、不自動 rebase/解衝突/push。

Git 能自動合併時，exact-tree verification 會證明結果；衝突則保留 target 不動，
交給人類。source-versus-target 衝突用 `assent reconcile <FOLDER>`；peer-only
batch conflict 走 `verify --batch` 的略過決定。

## 明示選取

`assent run A B` 只依寫出的順序跑 A、B；`assent run A B --all` 先完成前綴，再
交給依賴排序的 scheduler。兩者都不暗中驗證或接受。

所有明示的 live folder 都會在 dispatch 前完整稽核：包括 `...` 前的每一個名字，
都必須是現存 `.assent/` 目錄，且含正式 `tNNN_name.e.toml`。只要有 unresolved，
就完整列出並阻止所有選取操作啟動。readiness、lock、receipt 與 Git eligibility
仍由各指令自己的 gate 處理。

最後一個位置參數的 ASCII `...` 是 `run`、`verify`、`accept`、`clean`、`archive`
共用的 remainder selector。它把指令原本會找到的其餘資料夾接在明示前綴後，並在
任何 mutation 前 snapshot；不是 `--all`。不可重複、不可放在最後以外，也不可和
`--all` 合用。`verify`/`accept` 只加入已完成資料夾，`run`/`clean`/`archive` 先
考慮所有 work folder 再各自判斷資格；排序規則見[指令](COMMANDS.md)。

cardinality 決定 path：一個資料夾是 folder receipt/direct accept/單一 archive；
兩個以上是 exact selected batch。`...` 展開後仍是 exact selection，因此 selected
accept 需要恰好那一組 evidence，而且不會驗證。它不能和 `verify --batch`、
`verify --focus`、`run --once`、`run --task`、`archive --restore` 合用。

## 重做與駁回

`assent rework <FOLDER> <TASK>` 是一般的非破壞性審查處理：預設只把目標 status
重設為 `TODO` 並保留程式碼。下游任務若已開始或完成，必須明示 `--cascade`；
`--reason TEXT` 保存裁決理由。`--revert-code` 只有在 checkpoint 是連續 branch
tail 時才新增 reverse commit，絕不重寫 history。成功後更新 report，但不自動 run。

`assent reject <FOLDER>` 是丟棄整個實作的另一個人類決定：先以 WIP commit 保存
未提交變更、記錄各 branch 完整 tip hash，再走 link-safe cleanup 刪除 worktree、
強制刪除同 prefix branch，將 `DONE`/`WIP`/`BLOCKED` 重設成 `TODO`，並在 journal
留完整 Git 證據；`SKIP` 不會被翻轉。run 進行中會拒絕。

如果審查或驗證發現的是同一個仍存活資料夾目標的缺項，請附加新的 task，不要重寫
或重編舊 task。真正不同的目標、已接受/封存/駁回的資料夾，或需要新的 dependency
與 `base` lineage 時，才開新資料夾。

## 語言與契約

英文是 tracked technical documentation 與 scheduler-generated text 的 canonical；
`README.zh-TW.md` 與 `docs/zh-TW/` 是正體中文讀者翻譯。指令、路徑、task ID、
JSON、設定與使用者資料保持原樣。共用契約只在 `~/.assent/instructions.md` 與
`~/.assent/format.md`；請參考[翻譯流程](../TRANSLATING.md)與[設計共識](../CONSENSUS.md)。
