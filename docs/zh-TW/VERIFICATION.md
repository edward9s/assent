# 驗證

*[English version](../VERIFICATION.md) · [README](../../README.zh-TW.md)*

> 本檔是 [../VERIFICATION.md](../VERIFICATION.md) 的正體中文(台灣用語)翻譯；內容
> 如與英文版不同，以英文版為準。涵蓋 focused/full/batch verification、receipt、
> shared ignored input、reconcile 與 acceptance evidence。

## 兩層驗證

### Focused task verification

每個 task 的 `verify` 欄位是 AI 修改後在 source worktree 執行的 command。輸出會
印出 `verify: <command>`，接著是 `verify passed (exit 0)` 或
`verify failed (exit N)`；可以在 `<project>.worktrees/<FOLDER>/` 重跑同一行。

`assent verify <FOLDER> --focus` 在 source worktree 重跑不同的 `DONE` task verify；
不建立 integration candidate 或 receipt，通過也不能授權 accept。focused verification
仍會在 check 開始前分類 shared ignored input 並同步 reviewed link。

### Folder auto-fix review gate

`[auto_fix.review]` 是可選的 reviewer override；若沒有 table，會用第一個 effective worker
adapter 的 `prime`/`heavy` 自動解析。它的 `adapter` 可以是單一名稱，也可以是有序的名稱
list，每個項目就是一個 review round；重複的名稱是有意義的，代表再跑一個相同 identity 的
round。單一的 `model` 與 `effort` 套用到每個項目，因此 list 會依序解析成每 round 一組
adapter/model/effort identity，設定的項目數就是這個 loop 的有限上限。
Invocation 明示 `run --auto-fix` 時，completed folder
才會在最後 focused gate 後做 folder-level review。Scheduler 先再跑每個 distinct `DONE`
task 的 `verify` 一次；任何 failure 都只寫 focused finding evidence，不啟動 completed-folder
reviewer。`--once` 或 `--task` 若留下 incomplete folder，會延後這個 loop 且不消耗 token；有
durable worker `BLOCKED` 或 focused-gate 證據的 quiescent blocked dependency 走另一個
blocked-adjudication entry point；只有 `SKIP` 的 folder 不需要 implementation review。沒有
`--auto-fix` 的普通 run 不會做這次 final sweep/review，也不會 repair，即使已有 policy。

`run --auto-fix` 是該次 invocation 修正 FAIL review 的授權，與 run selection 正交，可和
明示 folder、`...`、`--all`、`--once`、`--task`、`--verify` 合用。它不跑 full candidate、
不 publish ref。沒有 flag 時 FAIL 是人類裁決 evidence；有 flag 時每個 finding 都必須對應
一個既有 task 與 declared scope，才會記錄 code-preserving reason-bearing rework。每個被
reopen 的 task 都以它自己原本的 task profile 修復：沒有 escalation ladder，也不會消耗任何
profile，所以中斷的 round 會以完全相同的 identity 恢復，多 task finding 與 dependency
cascade 也不會逐 task 提前 escalation。

completed-folder round 是 reviewer 與 fixer 合併的 session，不是嚴格唯讀的 gate：發現真正
的 blocking problem 時，它可以直接修，但只能寫入 finding 指名的那一個既有 task 的 declared
scope，並以 verdict `FIXED` 回報。其他任何寫入 —— management-plane 檔案、task 檔案、別的
task 的 scope、commit，或任何寫入 primary worktree —— 都會被普通 worker session 面對的同一
道結構性安全 gate 擋下，使該 verdict 不可用，同時保留原編輯。只有在沒有任何 blocking 問題
殘留、且該 round 完全沒有寫入時才回 `PASS`；`FAIL` 仍代表該 round 自己不能修的 blocker
（例如精確的 scope omission），以及維持唯讀的 blocked adjudication。

Loop 依設定的 round list 終止：每個 round 讓 durable round index 剛好前進一，走到 list
結尾就有限地結束自動化。在未修好的 blocker 上用完 round，會保留每一項 finding、編輯與
journal，不再開新 round，並以非零 exit code 結束；在 `FIXED` round 上用完，則結算為下述
獨立的 SELF-FIXED, UNREVIEWED 結果，exit code 為零。

Review 依循 changed 與 directly interacting code；既有 technical debt 只有在
`COMPLETED_FOLDER + INITIAL` 引入、修正局部於既有 scope 且 focused gate 能可靠測試時才
合格，不做全 repository audit。blocked adjudication 與 `RECHECK` 可以保留或解決 debt，
但不能新增。未知、含糊或越界 finding 交 scheduler 作決定。reviewer 可核准一個精確 scope
addition，但只有 scheduler 修改 task file；worker 與 reviewer 都禁止 task-file edits。
不會自動建立 task、改 task requirement、還原 source、刪 source、accept，也不把
`_auto_fix.toml` 當 task status。任何 round 都不得寫 management-plane 檔案；Assent 以
before/after surface snapshot 偵測並拒絕這類寫入、保留原編輯，唯讀的 blocked adjudication
則完全不得寫入。這是在 `danger-full-access` 預設
下的 cooperative rule，不是 security sandbox，loop 內沒有 runtime human adjudication gate。

Round list 在 `FIXED` round 上結束的 folder 屬於 SELF-FIXED, UNREVIEWED：durable state 記下
結算結果 —— round 位置、用掉的 round 總數、該 round 的 adapter/model/effort，以及沒有人確認
過的 finding fingerprint —— run 以零 exit code 結束。不還原、不 reopen、不重新標記：每個 task
保留它自己 focused gate 證明的 status，被修好的 task 維持 `DONE` 而不會被改成 `BLOCKED`。
Scheduler 為每個 implicated task 寫一筆 journal entry 並刷新 report。這個結果是終局，不是
可續跑的 phase，之後的 `run --auto-fix` 只會再次回報它、不再開新 round，只有人類 `rework`
能重新開啟該 folder。唯一缺的是獨立的 review 確認，而那只有人類的 `accept` 決定能提供。

Review context 是 `COMPLETED_FOLDER` 或 `BLOCKED_ADJUDICATION`，stage 是 `INITIAL` 或
`RECHECK`。Recheck 先處理 prior current findings；仍存在 blocker 保留 fingerprint，新
blocker 只能有 repair regression 或 newly exposed existing requirement 的證據；prior set
清空後必須 PASS。Optional improvement、speculation 與重複 debt discovery 不會讓 loop 繼續。
與 task requirement 直接相關的具體 local focused-test gap 才能是 review evidence；缺完整
verification、缺 receipt 或未跑 full suite 都不是 reviewer failure。

### 完整 candidate verification

`assent verify <FOLDER>` 建立臨時 integration candidate，把 source 結果放入其中，
以 main-tree `.assent/verify.py` 執行完整 verifier，寫入或刷新 folder receipt。不耗
AI token、不改 target ref、不接受任何 folder。candidate 建立衝突會在 verifier 與
`PASSED` evidence 之前拒絕。

`assent verify A B` 是一個 exact selected batch：A、B 正規化成 dependency order，
建立一個 candidate、跑一次 verifier，receipt 只記這一組。它不改 target。
`assent verify --batch` 則 dynamic 處理已完成且尚未整合的 folder。selected/dynamic
batch 寫 batch receipt，不會為了對稱而刷新 folder report。

完整 verifier 也檢查 candidate tree，以及 `HEAD` 到 first parent 的 committed delta
是否殘留 conflict marker。只有 whitespace 的差異（line ending、trailing space/tab、
EOF 空行）不會阻擋，除非 project 另加 formatter check。`assent init` 選的真正 project
test 會執行，不是空的成功骨架。

## Receipt 與 report

Receipt 是可刪除的 derived evidence，不是 source of truth。它必須由 source commit
identity、重建的 integration tree、verifier-script digest 與 shared-input identity
重現；任何 drift 會使它 stale。Malformed receipt 直接拒絕，不會在 acceptance 中偷偷
換新。

每個 production folder-level complete verification（含 `verify_folder_if_needed`）
都在 receipt operation settle 且所有 verification lock 釋放後，恰好刷新一次該 folder
的 `_report.md`。best-effort report refresh 會觀察 `PASSED`、`FAILED`、stale replacement、
fresh reuse、malformed refusal、incomplete no-op 與 interrupt，但不改變或遮蔽 verification
結果。

Folder report 也會以零 token 顯示 derived `_auto_fix.toml`：沒有檔案是
`Folder auto-fix: NOT RUN (no review state)`；malformed 或 source/task binding 改變是
`STALE`；新鮮 review PASS 是 `PASSED (fresh)`；新鮮的非 `PASS` verdict 是 `FAILED (fresh)`
並列出 phase、context、stage、original blocker、current findings/recommendations、scope
decision、acknowledgement、terminal reason、exact scope-amendment transaction，以及 review
round index 對設定 round 數的比例。已結算的 self-fixed folder 會顯示獨立的
`SELF-FIXED, UNREVIEWED (fresh)`，指出 self-fixed round 的位置、用掉的 round 數與該 round
的 adapter/model/effort。Version 6 的 state 必須有 `phase`、
`review_context`、`review_stage`、`failure_trigger`，綁定 source tree、task-plan digest、
review-prompt digest 與 resolved reviewer adapter/model/effort，保留 finding ledger、
recommendations、scope additions、exact scope-amendment transactions、repair briefs、
dispositions、transitions、observed states、`review_round_index`，以及至多一筆
`self_fixed_unreviewed` 結果；`NEEDS_REPAIR`、`REPAIRING`、`AWAITING_REVIEW`、`COMPLETE` 明確
表示 restart boundary。它可刪除重建，不是 receipt、task status 或 acceptance gate。Pending
的非 `PASS` state 若沒有目前 reviewer policy，或決定它的 identity 已不在設定的 round 之中，
repair 與 closeout 會拒絕。

若 eligible debt 曾由 `COMPLETED_FOLDER + INITIAL` 引入，report generation 會建立同層
`_technical_debt.md`，並在 `_report.md` 標示 `TECHNICAL DEBT REVIEW REQUIRED`；它保留
recheck 後已解決的 finding 及 task、path、evidence、recommendation、repair disposition、
current/resolved outcome、scope decision。blocked adjudication 與 recheck 不能新增 entry。
Acceptance 前 meeting 必須主動告訴人類、列舉每項，逐項取得完成 repair 足夠、follow-up
task/rework，或 durable `AGENTS.md` rule 的 disposition；這不是第二個 approval state。

Scheduler 在 automatic rework、中斷、repair closeout 或有限 round 用盡時造成的
status-only transition 是正常 lifecycle evidence，不會單獨讓 auto-fix report stale。
真正改動 task requirement、scope、verification 或其他 contract structure 才會讓 binding
stale；兩者都只是 zero-token report evidence。

`[verification] receipt_refresh = "manual"`（預設）讓普通 run closeout 延後 folder
receipt；`"auto"` 在 folder 所有 task 完成時刷新。`assent run --verify` 不受這個設定
影響，只在 run 成功後按選取範圍驗證；run 失敗不驗證。

## Batch conflict 與 reconcile

沒有 conflict 時，`assent verify --batch` 完全無人值守。建立 batch candidate 時若
發現 source conflict，Assent 仍嘗試所有 queued folder，列出每個衝突 folder/path，並
transitively 排除所有排在衝突之後的 folder。接著只問一次 `[Y/n]`：

- 空回答或 `y`/`yes`：對剩下可 merge 的較小集合跑一次完整驗證，receipt 只記該集合；
- no、無法辨識的回答或 EOF：在 verifier 前拒絕，不寫 receipt；
- 全部 queued folder 都衝突：沒有獨立子集，直接拒絕、不提問。

略過不是解決、rebase、accept 或 delete。明示的 `assent verify A B` 發生 conflict
時不會縮小集合；若 peer-only conflict 前有可相容 prefix，可先 verify/accept prefix，
讓 target 前進，再針對衝突 folder reconcile。`rework` 與 `reject` 仍是明示替代方案。

### `assent reconcile`

`assent reconcile <FOLDER>` 是人類處理 source-versus-target conflict 的 path。它要求
folder 已完成、main worktree clean，且 source branch/worktree 存在。它先擷取當前 target
tip，在 main worktree 旁建立 `<project>.reconcile/<FOLDER>` 與暫時 branch
`assent-reconcile/<FOLDER>`，從 exact source tip 開始，再 merge 擷取的 target 而不 commit。
merge 以 source 為 first parent，因此 source branch 之後可前進到它；target 與 source
worktree 全程不變。

如果其實沒有 conflict，start 會回報、undo merge、移除剛建立的資源並保持 source 不變。
如果 source 已包含在 target，也沒有要 reconcile 的內容。

人類只在印出的 worktree 編輯衝突檔案，不執行 Git command。
`assent reconcile --continue <FOLDER>` 只 stage Git 仍標為 unmerged 的 path，檢查沒有
unmerged path、conflict marker、`git diff --cached --check` whitespace error，也沒有
編輯 conflict scene 以外的內容；接著 commit merge、在自己的 worktree fast-forward
source branch，並重驗 ownership 後移除 temporary worktree/branch。source tip 改變後，
舊 folder evidence 與記錄舊 source 的 batch receipt 會刪除；無法 parse 的 batch receipt
會保留供檢查。

Reconcile 不寫 receipt、不跑 focused task test 或完整 suite，也不 accept。之後人類必須
顯式執行 `assent verify <FOLDER>`，再執行 `assent accept <FOLDER>`。若 target 在 start
後前進，不會重寫已擷取的 merge；之後的 verify 才是權威。

沒有 reconcile state file。復原依 worktree、temporary branch、`HEAD`、`MERGE_HEAD` 與
merge parents 判斷；`--continue` 可續接已 commit 的 merge 或完成剩下的 fast-forward。
若 source branch、managed path、branch 或 staged resolution 不符，拒絕並保留所有編輯。
`--abort` 只移除已證明的 managed worktree/branch，有未提交編輯時拒絕。

## Candidate 建立

Candidate 由 source worktree 的 tracked content 建立。額外只 mirror 兩種 artifact（可在
root 或 tracked parent 下）：

1. Assent provision 的 reviewed ignored directory link：Windows junction/directory
   symlink，POSIX directory symlink；
2. 位於 otherwise tracked directory 內的一般 ignored leaf file，例如 tracked source
   旁生成的 `*.g.dart`。

Directory mirror 是指向相同 resolved target 的 link；file mirror 是指向 source file 的
candidate-side link（Windows 同 volume hard link，POSIX file symlink）。不會 copy，不會
手動準備 hardlink twin。

Git ignore walk 會 prune 整棵 ignored tree、`.git`、`.assent`、build output、cache、
editor state、credentials、discovered link target 內的一切，以及 parent chain 不在
candidate tracked tree 內的檔案。每個 destination 必須在 candidate 缺席且被 Git ignore；
mirror 不會取代或遮蔽 tracked content。

多個 source 的 artifact 取 union。相同 path 若解析到同一 directory target，或 file 的
content digest 相同，就 deduplicate。若 target 衝突、file content 不同、kind mismatch、
ancestor/descendant overlap、dangling/unsupported link、destination 已佔用、parent 不安全
或無法建立 link，會在 verifier 前拒絕，也不寫 `PASSED` receipt。

Mirror 只存在 verifier 期間，會 deepest-first 移除，再移除 temporary candidate worktree；
只清理由 provisioning 建立且已空的 parent。任何建立或清理都不穿越、修改或刪除 linked
target；source link、file、外部 target 在成功、失敗與中斷後都保留。沒有 force flag、
blanket ignore overlay、copy fallback 或 project `local_inputs` 設定。

## Shared ignored directory

isolated worktree 只有 tracked content；若完整 check 確實需要 ignored directory，必須
使用 reviewed handoff。排程 session instructions 要執行：

```text
assent shared-paths review --path DIR --watch FILE
```

需要多個目錄就重複；沒有共享目錄時用
`assent shared-paths review --none --watch FILE`。這是唯一可寫 primary worktree 未追蹤
`.assent/manifest.toml` 的操作。絕不 copy ignored tree，也不要手動建 source link；Assent
會把同 relative path 的 primary target 建成 junction 或 directory symlink。

Manifest 的 `[shared_paths]` 以 fingerprint 保存整份 profile：normalized project-relative
`paths`、精確 tracked `watch` files，以及 tracked Git-ignore rules 的 digest。source snapshot
可能是：

- `UNKNOWN`：沒有 matching answer；
- `REVIEWED-NONE`：matching profile 有 `paths = []`，這是真正答案，不會再觸發 review；
- `REVIEWED-PATHS`：reviewed directory 已指向 exact primary target；
- `STALE`：watched file/target 改變、消失、變 type、不再 ignored，或 diagnosis 指到未宣告目錄；
- `NO-IGNORED-DIRECTORY-CANDIDATE`：primary worktree 的成功 Git query 在 `.git/`、`.assent/`
  以外找不到現存的一般 ignored directory。

最後一個是 deterministic zero-token 結果，不表示語意上永遠不需要 shared input；不用
manifest profile、link 或 AI review，且 digest identity 和 `REVIEWED-NONE` 不同，每個 gate
都便宜重算。ignored leaf file 不算；任何 ordinary ignored directory 都算，即使 review
後回答 `paths = []`。之後新出現 directory 會使下一次成為 `UNKNOWN`，除非已有 matching
profile。Git ignored-entry query 失敗會拒絕，不會假裝空集合。

完整 verifier 證據若指名 required directory，絕不以
`NO-IGNORED-DIRECTORY-CANDIDATE` 結案：有有效 primary target 就進 review，缺少或未被
ignore 的 target 就以 actionable refusal 結束。只存在未接受 source branch 的 directory
或 ignore rule 還不能 provision。

`UNKNOWN` 與 `STALE` 會在下一個已排程 session 加一個有界 review clause，未 settled 前
拒絕 closeout；fingerprint 沒變不耗 review token。review 會先驗證所有值、取 project-local
lock，再 atomic replace manifest；中斷只會留下舊的完整 profile 或新的完整 profile。

每個 verification entry point（single、selected、dynamic batch、localization prefix、
`run --verify`、`--focus`）與 `assent reconcile` 都會在 candidate、verifier 或 managed
reconcile worktree 之前分類並同步 shared input。每個 source 的 ignored directory link
必須等於 active profile 且指向 exact primary target；未宣告 manual link 是 unreviewed
evidence，會讓 verification、reconcile、receipt freshness、report、acceptance 拒絕。
ordinary ignored leaf file 仍走自己的 automatic candidate-link 行為。

Folder/batch receipt 綁定一個 `shared_inputs_sha256`，在完整 verifier 前後各 snapshot；
acceptance 發布前重查它，不修 link，也不呼叫 AI。

## Ignored-input diagnosis

完整 verifier 若在 physically present 的 ordinary ignored source directory 內失敗，會
保留原 verifier output 與 exit code，再附加一則：

```text
Ignored input diagnosis: <directory> is omitted from the candidate; place the
required ordinary Git-ignored target at the primary path and record it with
assent shared-paths review rather than copying it or hand-creating a link.
```

只報 verifier output 自己指名的 directory，並正規化 separator；不列舉或穿越 ignored tree。
它會放在記錄 failure summary 的 receipt，適用 single-folder、exact selected、dynamic batch
與 localization-prefix。

## Acceptance evidence

直接 `assent accept <FOLDER>` 是人類明示批准。除非 source 已因 ancestry 成為 target 的
idempotent no-op，否則需要 fresh `PASSED` folder evidence，且 source tip、integration tree、
verifier digest 與 shared-input digest 都必須精確重現。`assent accept A B` 同樣需要恰好
dependency-ordered selected set 的 fresh `PASSED` batch receipt。兩者都不開始完整 verifier、
不擴大選取、不連線 remote、不自動解衝突。

若單一 folder 的 derived auto-fix state 是 SELF-FIXED, UNREVIEWED，accept 會在 merge 前多加
一道互動式確認。所有 receipt-based check 都已通過，所以這不是拒絕：缺的只是有限 round list
沒能產生的獨立確認，而那只有人類能提供。Assent 會指出 self-fixed round、它的
adapter/model/effort，以及記載被修復 finding 的 `_report.md`，然後詢問
`Publish it anyway? [y/N]`。只有精確的 `y`/`Y` 會 publish；其他任何輸入，包含已關閉或
非互動 stdin 的 EOF，都視為拒絕，不 merge、不改任何 Git state。state 檔是可刪除的 derived
memory、不是 acceptance evidence，因此 malformed record 無法在 receipt evidence 完整的
folder 上憑空製造這道 gate。

`assent accept --all` 有兩種模式：

1. fresh `PASSED` batch 只針對 receipt 自己的 folder 做 atomic replay，不新增 verification；
2. 缺少或過期/non-`PASSED` evidence 時，在 dependency order 對每個尚未整合 folder 先跑
   `verify_folder_if_needed` 再 publish；第一個真正失敗就停止，保留之前的 publication；
3. malformed batch receipt 直接拒絕，不 fallback。

Acceptance 會保留 source evidence 供審查與清理。integration lock 無法阻止外部 Git writer，
所以 acceptance 期間不要在同一 main worktree 執行寫入型 Git command。完整 verification
是 evidence，人類 review 與明示 accept 仍然分開。
