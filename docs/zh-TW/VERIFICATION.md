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

若設定 `[auto_fix.review]`，且 invocation 明示 `run --auto-fix`，completed folder 才會在
最後 focused gate 後做 folder-level 唯讀 review。Scheduler 先再跑每個 distinct `DONE` task
的 `verify` 一次；任何 failure 都只寫 focused finding evidence，不啟動 reviewer。`--once`
或 `--task` 若留下 incomplete folder，會延後整個 loop 且不消耗 token；只有 `SKIP` 的
folder 不需要 implementation review。沒有 `--auto-fix` 的普通 run 不會做這次 final
sweep/review，也不會 repair，即使已設定 policy。

`run --auto-fix` 是該次 invocation 修正 FAIL review 的授權，與 run selection 正交，可和
明示 folder、`...`、`--all`、`--once`、`--task`、`--verify` 合用。它不跑 full candidate、
不 publish ref。沒有 flag 時 FAIL 是人類裁決 evidence；有 flag 時每個 finding 都必須對應
一個既有 task 與 declared scope，才會記錄 code-preserving reason-bearing rework，並在
每個 repair session 前消耗有限 fixer profile。

Review 依循 changed 與 directly interacting code；既有 technical debt 只有在修正局部於
既有 scope 且 focused gate 能可靠測試時才合格，不做全 repository audit。未知、含糊或越界
finding 交人類。不會自動建立 task、改 task requirement/scope、還原 source、刪 source、
accept，也不把 `_auto_fix.toml` 當 task status。Reviewer 不得寫 project 或 management file；
Assent 以 before/after surface snapshot 偵測並拒絕寫入、保留原編輯。這是在
`danger-full-access` 預設下的 cooperative rule，不是 security sandbox。

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
`STALE`；新鮮 review PASS 是 `PASSED (fresh)`；新鮮 FAIL 是 `FAILED (fresh)` 並列出
current blocking findings。State 綁定 source tree、task-plan digest、review-prompt digest、
resolved reviewer adapter/model/effort，保留 finding ledger、observed states 與 consumed
fixer profiles。它可刪除重建，不是 receipt、task status 或 acceptance gate。

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

`assent accept --all` 有兩種模式：

1. fresh `PASSED` batch 只針對 receipt 自己的 folder 做 atomic replay，不新增 verification；
2. 缺少或過期/non-`PASSED` evidence 時，在 dependency order 對每個尚未整合 folder 先跑
   `verify_folder_if_needed` 再 publish；第一個真正失敗就停止，保留之前的 publication；
3. malformed batch receipt 直接拒絕，不 fallback。

Acceptance 會保留 source evidence 供審查與清理。integration lock 無法阻止外部 Git writer，
所以 acceptance 期間不要在同一 main worktree 執行寫入型 Git command。完整 verification
是 evidence，人類 review 與明示 accept 仍然分開。
