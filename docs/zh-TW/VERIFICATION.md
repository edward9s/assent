# 驗證

*[English](../VERIFICATION.md) · [README](../../README.zh-TW.md)*

> 本文是 [英文版](../VERIFICATION.md) 的正體中文翻譯；若內容不同，以英文版為準。

Assent 把快速的 task 檢查與完整 candidate verification 分開。這樣可以節省執行
成本，也不會把 focused pass 誤當成最終要發布的結果。

## Focused check

每個 task 都有一個小而明確的 `verify` command。`focused_test` 執行單一 task
的 command；`focused_sweep` 執行完成 plan 內不重複的所有 command。兩者都不寫
receipt。

可以執行單一 task command，或重跑完成 plan 內不重複的 commands：

```text
assent verify <PLAN> --focus t003  # focused_test
assent verify <PLAN> --focus       # focused_sweep
```

明示 task 時不限制目前 status；sweep 只包含 `DONE` tasks，且相同 command 只執行
一次。

Focused pass 只證明受測的 source worktree，不能授權 `accept`。

## 完整驗證

`full_verify` 與 `assent verify` 會從選定的 source commit 建立臨時 integration
candidate，再於其中執行專案的 `.assent/verify.py`。它們不會變更 target ref，
也不會接受成果。

所有明確的 `assent verify` 用法都只執行機械驗證：不進入設定的 workflow role、
不啟動 AI session，也不自動修復失敗。

```text
assent verify <PLAN>     # 單一 plan receipt
assent verify A B        # 一份精確 batch receipt
assent verify --batch    # 動態發現的 batch
```

明確選取必須整組成功。若要求 `A B C`，Assent 不能只驗證 `A C`，再宣稱
`A B C` 通過。前半段通過可以協助診斷，但不能授權原本的 request。

動態 `--batch` 不同，因為 plan 是由指令自己發現。建立 candidate 遇到衝突時，
它會列出所有衝突與受影響的 dependent，再詢問是否只驗證其餘互不衝突的部分。
最後 receipt 只會記錄實際驗證的 plan。

## Receipt

Receipt 是可刪除的證據，不是 source of truth。它記錄重現結果所需的身分：選取的
source commit、重建過程的 tree、verifier digest 與 reviewed ignored-directory input digest。
任何相關 source、candidate、verifier 或 ignored-directory input 改變，都會讓 receipt 過期。

完整 plan verification 會在 receipt operation 與所有 verification lock 結束後，
恰好刷新一次該 plan 的 `_report.md`。這是 best-effort 動作，不會改變驗證結果。
Focused 與 batch verification 不刷新個別 plan report。

直接或明確選取的 accept 需要新鮮且完全相符的 PASS receipt，只有 ancestry 已證明
整合的 no-op 例外。`accept --all` 可以原子重播新鮮 batch receipt；沒有可用 batch
證據時，則逐一驗證並接受。Malformed batch receipt 會直接拒絕，不會忽略後 fallback。

## 自動修復

機械檢查通過，就完成該 workflow 層，不會再啟動 repair role。失敗才會進入下一個
已設定的 repair role，並由後續 action 重新檢查。預設 repair role 在一個 session
內合併審查與修復；自訂 workflow 仍可將兩者分離。設定陣列就是有限的全部次數。

Task role 處理單一 task 的失敗；plan role 處理累積 focused sweep 失敗；
integration role 處理 exact-selection evidence。Conflict evidence 已經指出衝突的
plan 與 path，因此不需要猜測 ownership。沒有機械式 source attribution 的 multi-plan
verifier failure 才交由人類決定。

若有限次數用完仍未通過，Assent 會保留修改與證據，回報
`REVIEW UNRESOLVED, HUMAN DECISION`。未通過的機械證據仍會阻擋 acceptance。

## 衝突與 reconcile

Candidate conflict 發生在 verifier 之前，不會產生 PASS receipt。`run` 會把精確
conflict evidence 交給設定的 integration workflow。Target-only conflict 使用受管理的
source-first reconcile worktree；peer-only conflict 使用衝突 plan 的 persistent source
worktree，並提供 compatible-prefix evidence。AI role 只修改內容；Git staging、commit、
source transition、candidate 重建與下一次 `full_verify` 都由 Assent 負責。

明確的手動替代方式是：

```text
assent reconcile <PLAN>
# 只編輯回報的衝突路徑
assent reconcile <PLAN> --continue
assent verify <PLAN>     # 或重跑原本的 exact/batch verification
```

Reconcile 會建立受管理的 source-first worktree。人類只編輯內容；staging、commit、
ref 更新、驗證與清理由 Assent 負責。`--continue` 會拒絕尚未解決的路徑、conflict
marker、whitespace error 或無關修改。它只推進 source，不改 target，所以仍要重新
完整驗證。`--abort` 只移除乾淨且重新證明 ownership 的資源。

手動的單一 plan reconcile 處理 source 與目前 target 的衝突。Integration workflow
也能修復 peer-only conflict，不會接受 prefix 或改變 exact selection。

## Ignored-directory input

新的 Git worktree 不會有 ignored directory，但專案可能需要大型本機目錄（例如
`assets/` 或 `pkg/`）才能編譯或測試。`ignored-dirs` 記錄哪些 ignored directory
是 source 必要輸入；Assent 只連結這些目錄，不會複製所有 ignored tree。Tracked
source 旁的一般 ignored leaf file 則會自動處理。

各位置的責任不同：

- 主要 worktree 保存真實目錄，以及未納入 Git 的審查快取
  `.assent/_ignored-dirs.toml`。
- 受管理的 source worktree 在相同相對路徑建立 Windows junction 或 POSIX
  directory symlink，指向主要 worktree 的真實目錄。
- AI 專用的 `ignored-dirs declare` operation 把自己的受管理 source worktree 當成
  宣告所描述的 snapshot，也只同步該 worktree 的鏈結。

一般流程不需要人介入。`run` 找到匹配的審查結果時，會在 AI session 開始前自動
建立鏈結。若狀態是 `UNKNOWN` 或 `STALE`，AI 會審查完整 inventory，再於自己的
受管理 source worktree 執行 `declare`。這個操作會驗證宣告、把 profile 寫入主要
worktree 的 manifest，同時在 source worktree 建立鏈結，讓下一個 focused action
可以開始。在主要 worktree 執行 `declare` 也是合法的，但只會快取該
主要 snapshot 的 profile，不會建立指向
自己的鏈結。Verification 與 reconcile 也會把同一份 profile 套用到各自的受管理
worktree，不會依賴先前 `run` 遺留下來的鏈結。

可在任一 worktree 查看狀態，不做任何變更：

```text
assent ignored-dirs status
```

輸出會列出目前與主要 worktree、manifest、狀態、匹配的 profile、必要目錄、watch
files，以及鏈結是否一致。在主要 worktree 中，鏈結會顯示為不適用，因為其中的一般
目錄就是 target。這個指令不會修復任何東西；無法讀取契約或已確定 profile 的鏈結
損壞時，會回傳非零狀態。

審查列出的 inventory 後，active source role 會在自己的受管理 worktree 提交宣告，
並指定哪些 tracked dependency 或 build file 改變後，原決定應該失效：

```text
assent ignored-dirs declare --required assets --required pkg --not-required build "generated output" --watch package.lock
```

宣告指示列出的每個 ordinary ignored directory，都必須由 `--required` 或
`--not-required DIR REASON` 覆蓋一次；兩者都可以涵蓋 subtree。沒有目錄是 source
必要輸入時使用 `--none-required`。只有 `--required` 目錄會在 worktree 建立鏈結。
Ignored leaf file 仍由 verifier 自動處理，不需要分類。這不是一般 junction 管理
指令，也不會複製目錄。它不是人類復原指令；人類使用 `assent rework`，讓下一次
`run` 把決定重新交給 AI workflow。

決定會快取在主要 worktree 未追蹤的 `.assent/_ignored-dirs.toml`。Watch file、directory
inventory 或 target 改變後會過期。若成功查詢沒有發現 ordinary ignored directory，
狀態是 `NO-IGNORED-DIRECTORY-CANDIDATE`；它只描述目前檔案系統，不是「專案永遠
不需要 ignored-directory input」的語意保證。
前導底線表示這是 Assent-owned 本機狀態；只能透過 `ignored-dirs declare` 修改。

不能把所有 ignored directory 都建立成鏈結。Ignore rule 還可能包含可寫入的 build
output、cache、virtual environment、editor state 與 credential；全部連結會共用
可變狀態、暴露無關的本機資料，也會讓驗證依賴過期產物。不要手動建立 source-
worktree link，也不要把 ignored directory 複製進去。未宣告的 link 會讓
verification、report、reconcile 與 acceptance 失效。清理時 Assent 只會移除鏈結
本身，不會進入或刪除 target。

若 verifier output 指向既有 ignored directory 內的遺漏路徑，Assent 會附加
`Ignored input diagnosis:`，指出 `ignored-dirs declare` 的處理方式，但保留原始 exit
code。

選取方式請看[指令](COMMANDS.md)，復原安全請看[作業](OPERATIONS.md)。
