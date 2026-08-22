# 作業與復原

*[English](../OPERATIONS.md) · [README](../../README.zh-TW.md)*

> 本文是 [英文版](../OPERATIONS.md) 的正體中文翻譯；若內容不同，以英文版為準。

Assent 使用 Git worktree 隔離修改，也讓失敗成果能被檢查與恢復。Worktree 是稽核
與復原邊界，不是安全 sandbox。

## Worktree 與 lock

每個 plan 使用自己的 worktree：

```text
<project>.worktrees/<PLAN>/
```

專案內被忽略的 `.assent/` 留在主要 worktree。若 branch 內有 tracked
`AGENTS.md` 就使用該版本，否則 scheduler 提供主要檔案的絕對路徑。

同一時間只有一個 `run` 可以擁有某個 plan。持續存在的 `assent.lock` 只是診斷檔；
真正的 ownership 來自 OS lock，不能因為檔案存在就判定卡死，也不要刪檔「解鎖」。
相依條件允許時，不同 plan 可以平行執行。

`accept` 執行期間，不要讓其他 Git 程式修改主要 worktree。Assent 的 integration
lock 只能序列化自己的發布動作，無法阻止外部 writer。

## 中斷與復原

Adapter failure、quota 中斷、Ctrl+C 或 crash 後，Assent 都會保留成果。Role 或
scheduler action 開始前會 checkpoint dirty candidate work。下次 run 會把 dirty
managed plan worktree 收進 `WIP` checkpoint，再從已保存的 workflow cursor 繼續，
不另開復原 AI session。

若 AI 誤寫主要 worktree，before/after boundary check 會拒絕該 role。Assent 保留
兩邊現況交給人類處理，不猜測如何轉移或丟棄修改。

Journal 保存 structured event 與有界摘要，不保存完整 raw adapter stream。Terminal
log 保存畫面上的 session output，也不會在每行再重複 scheduler prefix。

不要終止不屬於自己的 process，也不要手動刪除受管理的 worktree 或暫存 branch。
保留完整路徑與診斷，重跑原本的 Assent 指令，或執行 `assent doctor`。

## Link-safe cleanup

在任何 recursive Git 或 filesystem removal 之前，Assent 會盤點 directory junction、
directory symlink 與其他 directory reparse point，再先脫離 link object 本身。Remover
絕不穿越 resolved target；外部 target 在成功、拒絕、失敗、中斷與重試後都會保留。

若無法證明 inventory、ownership 或安全脫離，清理會停止並保留 managed path。

## Clean

```text
assent clean <PLAN>
assent clean              # 所有 plan
```

`clean` 只移除已證明乾淨、ownership 正確且已整合的 worktree/branch。只要 direct
dependent 尚未完成、未接受、dirty、遺失或缺乏證明，上游就會保留。多個 plan
依 upstream-first 順序處理。沒有 force-delete，`.assent/<PLAN>/` 也不會被刪除。

## Archive

```text
assent archive <PLAN>
assent archive --all
assent archive <PLAN> --restore
```

Archive 先要求同一套安全 cleanup 證明，再把管理資料存到 `.assent/_archive/`、更新
roster，並移除 live plan directory。明示的 plan 不符合條件時會回報錯誤；`--all` 會略過。
Restore 一次只處理一個 plan，先驗證 archive，也不會覆蓋現有 live plan directory。

## 暫存 branch 與 doctor

`assent-integration/<PLAN>/<suffix>` 和 `assent-reconcile/<PLAN>` 屬於建立它們的
transaction。只有 repository-wide integration lock 證明沒有 transaction 持有時，
殘留 branch 才算 orphan。它的 tree 是已發布或已被取代，只是回報資訊，不是刪除條件。

未指定 plan 的 `clean` 每次會掃描這兩種 namespace 一次；`archive --all` 使用相同流程。
明示的 `clean <PLAN>` 刻意不動 repository-wide 暫存 branch。`assent doctor` 會回報
殘留項目，重新確認 ownership 後才提供 `[y/N]` 移除。

## 安全邊界

Adapter 若使用廣泛權限，AI 就能接觸 OS identity 可使用的 credential、網路服務、
外部 Git writer 與 worktree 外檔案。Assent 只能事後檢查專案修改，不提供 container
或 VM。無人值守執行只適合可信任的 repository、instructions、adapter 與帳號。

Candidate link 與 receipt 請看[驗證](VERIFICATION.md)，accept 與 rework 決定請看
[工作流程](WORKFLOW.md)。
