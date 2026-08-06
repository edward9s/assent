# 作業

*[English version](../OPERATIONS.md) · [README](../../README.zh-TW.md)*

> 本檔是 [../OPERATIONS.md](../OPERATIONS.md) 的正體中文(台灣用語)翻譯；內容如與
> 英文版不同，以英文版為準。涵蓋 worktree、lock、並行、復原、清理、封存與安全。

## Worktree 與 branch

Git 永遠必須啟用，每個 work folder 都有自己的 worktree：

```text
<project name>.worktrees/<FOLDER>/
```

它是 change isolation、衝突管理、稽核與復原邊界。人類 acceptance 與 integration
被證明完成前，source branch 與 task files 都保留供審查。整個 `.assent/` management
plane 被 ignore，只在 primary worktree；進入 Git 時 scheduler 會 fail closed，避免
worktree 產生第二份 source of truth。

Scheduler 提供 task/journal 與 verifier 的 main-tree absolute path。tracked 的
`AGENTS.md` 使用 branch 版本；untracked 時使用 prompt 提供的 main-tree path。verifier
從 main tree 載入，但 cwd 仍是 candidate 或 worktree。共用契約永遠是 user-home 的
`~/.assent/instructions.md` 與 `~/.assent/format.md`。

AI meeting 在 primary worktree。可用 `git worktree list`、`git log <branch>`、
`git diff main...<branch>` 從外部審查，不必進入每個 worktree。

## 平行執行

不同 terminal 可跑不同資料夾：

```text
assent run parallel01
assent run parallel02
```

也可用 `assent run --all --jobs N` 讓 scheduler 安排。parent process 保持 foreground，
即時 child output 以 `[work-folder] message` 加前綴。root `.assent/_assent.log` 只留
startup 與 per-folder scheduling summary；各 folder 的 `_assent.log` 會附加自己的
rendered terminal session output，不含 parent scheduler 的 `[work-folder]` prefix。

並行會共享 adapter quota，branch 整合回 main line 是人類責任。speculative content
由明示 `base` 決定，不由 `after` 推導；詳見[工作流程](WORKFLOW.md)。

## Lock 是診斷資料

每個 work folder 有 `assent.lock`。檔案只記錄上次 run 的 PID、開始時間與 folder；它的
存在不代表現在仍有 run。真正的 ownership 是 open handle 上的 OS exclusive lock：Windows
用 `msvcrt`，POSIX 用 `fcntl`。正常結束、Ctrl+C、crash 與 force termination 都會在 handle
關閉時釋放。

- 不要把檔案存在當成「正在執行」；每次 run 後它仍會留著。
- 不要刪它來復原；刪除會引入 race，下一次 run 會重用，archive 也能建立遺失的檔案。
- folder 真忙時，下一次 run 取不到真正 lock 會拒絕；那才是訊號。

`run --all` 在所有 exit path（含 refusal 與 scheduling error）都會等待並 reap 自己
擁有的 child。記錄的 PID 若仍活著，可能是真正運行中的 process。這個 lock 保證主要
針對 local filesystem；某些 network filesystem 的 `flock`/`msvcrt.locking` 不可靠。

## 中斷與復原

Assent 有處理的中斷會寫 `WIP` checkpoint，`assent run` 以 continue prompt 恢復。在
run startup，如果每個未提交變更都能證明屬於要恢復的 task（或一個尚未被 checkpoint
的 `DONE` task），Assent 會將 task 標成 `WIP`、記錄 scope-verified recovery、把編輯
收進 `WIP` checkpoint，並在不開 AI session 的情況下繼續 recovery path。若 ownership
有 ambiguity，或任何 dirt 超出 task scope，Assent 會保留 dirty worktree 供人類檢查，
fail closed 而不猜；重新執行前請先檢查並建立 checkpoint。`assent.lock` 不是復原狀態，
不要處理它。

Scheduler 不會在失敗時 revert workspace。失敗 review 的程式碼保留並在其上重試；重試
用盡後成果進入 `BLOCKED` checkpoint 供人類裁決。journal 保存 structured events、
有界的 summary 與 adapter classification，不保存完整 raw adapter stream；各 folder 的
`_assent.log` 保存 rendered terminal session output，且沒有 parent scheduler prefix。

### Auto-fix recovery 與寫入邊界

`[auto_fix.review]` 是可選的 policy override；沒有 table 時，`run --auto-fix` 用第一個
effective worker adapter 的 `prime`/`heavy` 解析 reviewer，不需重跑 `assent init`。只有
`run --auto-fix` invocation 才會啟動 completed-folder final review，或有 durable blocker
證據的 quiescent blocked-adjudication review，並授權 repair；review 仍是唯讀。沒有 flag 的
普通 `run` 不會 review，也不會 repair。FAIL review 以帶理由的 automatic rework 重開既有
scope 內 task；不會建立 task、還原 source、刪 source 或 accept。reviewer 可核准一個精確
scope addition，但只有 scheduler 能寫 task file；worker 與 reviewer 都禁止 task-file edit。
每個 repair round 都在第一個 write-capable session 開始前把 fixer-profile assignments 寫入
`_auto_fix.toml`，因此 process failure 不會讓 consumed profile 悄悄恢復可用，也不會因先跑
一個 task 就讓同 round sibling 提前 escalation。
Finding ledger、consumed profiles、WIP checkpoint 與編輯會在中斷、quota、adapter failure
及 focused gate 失敗後保留。

之後的 `run --auto-fix` 只有在目前 resolved reviewer identity 相同時，才會讀取既有 FAIL
state、跳過已消耗 profile 並恢復 WIP；policy 漂移會拒絕 repair 與 closeout。Profile 用盡是
有限的 human handoff，不是持續重試或撤銷程式碼的指令；loop 內沒有 runtime human
adjudication gate。Report 只以 derived runtime 資訊顯示 `NOT RUN`、`PASSED`、`FAILED` 或
`STALE`，並附 phase、blocker、findings、scope decision、acknowledgement、profiles 與
terminal reason；不改 task status 或 acceptance。完整 verification 依成功 run 的 receipt
policy 或明示 `--verify` 另行執行，缺 receipt 或未跑 full suite 絕不是 reviewer failure。
Report 的 scope-amendment transaction 與 repair-round assignment 是 zero-token evidence；
scheduler 的 status-only lifecycle transition 也不會單獨讓 evidence stale。
Reviewer 的 prompt-plus-detection 寫入拒絕是在 `danger-full-access` 預設下的 cooperative
rule，不是 security sandbox 或預防性的 OS permission boundary。

### 臨時 integration candidate

完整 verification 會建立 sibling candidate，例如：

```text
<project>.integration/target-<uuid>
branch assent-integration/<folder>/<uuid>
```

它在 verifier 全程存在，成功、Python exception 與 Ctrl-C 都由 `finally` 清理。要觀察
時以 candidate 作 cwd，執行 main-tree verifier；不要把 source worktree 當 candidate。

只有 `taskkill /F` 等 hard kill 或斷電可能留下 residue。不要對 residue 使用 raw Git
worktree remove 或 recursive deletion；保留 exact path/branch，使用 Assent 所有者的
recovery/retry path。它會 inventory directory link/reparse point、先 detach link object、
再重驗 ownership 後刪 managed resource。證明不完整時，path、branch 與外部 target 都保留。

## Link-safe cleanup

`clean`、`archive`、`reject`、reconcile、setup failure 與 temporary candidate 都遵守同一
規則：directory junction、directory symlink 或其他 directory reparse point 會先以 link
object 脫離，再做 recursive Git/filesystem removal；絕不穿越 resolved target。外部 target
在成功、拒絕、失敗、中斷與重試後都保留。

如果無法證明 inventory、ownership 或 detachment，cleanup 會拒絕並保留 managed path，
等待 Assent 自己 retry。不要把含 directory link 的 tree 傳給 Git 或 recursive remover，
也不要手動刪 source worktree/branch。

## `clean`

`assent clean` 只刪除 fully merged 且 clean 的 worktree 與同 folder-prefix branch；不碰
`.assent/`、沒有 force option，也和 `git clean` 無關。

清理是 upstream-first 且依 evidence。direct dependent 尚未完成、未接受、dirty、遺失或
無法證明整合時，source evidence 必須保留；`assent clean A` 會拒絕並說明原因。所有
dependent 已接受、可證明整合且 clean 後，才先 clean upstream，再 clean dependent。

`assent clean A B` 與 `assent clean A ...` 會在一次 upstream-first pass 中處理選取；裸的
`assent clean` 仍 discovery 全部。`...` 規則見[指令](COMMANDS.md)。

`assent-integration/<folder>/<suffix>` 與 `assent-reconcile/<folder>` 是兩個 Assent 自有的
暫存 branch namespace，人類不可 checkout 或在其上建置。兩者各自由建立它的 transaction
在完成後移除，殘留下來的只因為那個 transaction 在完成前就中斷，才算 orphan。
`assent clean --all` 在每次 per-folder cleanup 之後、每次呼叫掃一次所有這類 orphan，因為
repository-wide integration lock 在該 branch 仍存在時被持有，才是它是 orphan 的完整
證明——不是 branch 的內容，也不是它的 tree 是 published 還是 superseded，那只是回報資訊。
明示單一 `assent clean FOLDER` 刻意不掃，讓指名部分 folder 時不會意外刪掉
repository-global 的 ref。

## `archive`

Archive 是 retirement，不是普通 cleanup。它先遵守 clean contract，再把合格 work folder
壓到 `.assent/_archive/`，並更新 `.assent/_archived.toml`（或目前 roster）。明示多個
folder 時採 single-folder contract：逐一嘗試，不合格者令 command nonzero；`archive --all`
則跳過不合格者而不使 dynamic request 失敗。

`archive --restore FOLDER` 只還原一個 archive，不接受 `--all` 或 `...`。Archive recovery
可能暫時沒有 live directory；已辨識的 restore 狀態不會被一般 explicit-selection audit
錯報成遺失 folder。

`archive --all` 繼承 `clean --all` 每次呼叫一次的暫存 branch 清理，做法是委派同一個
實作在自己的 per-folder loop 之後執行，而非重新實作：每個 per-folder archive 步驟本身
已持有清理所需的 integration lock。

## `doctor`

`assent doctor` 診斷 Python、Git、adapter CLI 與 temporary directory 是否可寫，不需要
現有 project，另外也會用和 `clean --all` 相同的 lock-based 證明，回報找到的 orphaned
Assent-owned 暫存 branch。找到 orphan、人類拒絕移除、或移除被拒絕，都不會讓 `doctor`
失敗或影響它的 exit code——這只是不整潔，不是壞掉。

跟不問人的自動 sweep 不同，`doctor` 會提出確認式的 `[y/N]` 移除：先列出找到的項目，
詢問是否移除，並在真正刪除前於同一次 integration lock 內重新讀取 branch 清單，讓
「回報之後、確認之前」剛好不再是 orphan 的 branch 被保留而不是被刪。`doctor` 的提案是
復原路徑；`clean --all` 與 `archive --all` 仍是例行、不問人的路徑，且 `doctor` 本身不跑
任何 folder 操作，也不碰任何 work folder、task file 或 receipt。

## `reject`

Reject 是明示丟棄 folder 實作的人類決定，不是 cleanup shortcut。它要求指定 folder，run
中會拒絕。刪 branch 前先記錄每個完整 tip hash、以 WIP commit 保存未提交變更，經 link-safe
boundary 移除 folder worktree，再強制刪同 prefix branch；`DONE`、`WIP`、`BLOCKED` 重設
`TODO`，`SKIP` 保留，journal 附加 `rejected` 與完整 Git evidence。hash 只在 Git 一般
garbage-collection grace period 內可復原。

## Acceptance 與外部 writer

Acceptance 是人類明示動作。它使用 integration lock，但 lock 不能阻止外部 Git writer；
acceptance 期間不要在同一 primary worktree 執行寫入型 Git command。Assent 不會在 acceptance
中連線 remote、pull、rebase、force-push、push、自動解衝突或刪 source。local decision 與
證據完成後，才由人類自行選擇普通 Git 同步。

direct/selected accept 不會驗證；`accept --all` 的例外與 receipt freshness 規則在
[驗證](VERIFICATION.md)。dependent 尚未接受與 clean proof 尚未完成前，保留 accepted
source evidence。

## 作業安全邊界

Worktree 不是 security sandbox。`danger-full-access` 與 `bypassPermissions` 仍讓 AI 取得
其 OS identity 可用的 credentials、network、外部 Git writer 與 worktree 外檔案。只在信任
的 project 與 account 使用 unattended execution；Assent 不建立 container/VM，也不攔截
外部效果。

## 相關指南

- [工作流程](WORKFLOW.md)：規劃、執行、審查與裁決。
- [指令](COMMANDS.md)：選取、`...` 與 command syntax。
- [設定](CONFIGURATION.md)：init 與 adapter 設定。
- [驗證](VERIFICATION.md)：candidate、receipt、ignored input、reconcile、accept evidence。
