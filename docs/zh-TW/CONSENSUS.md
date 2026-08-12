# 設計原則

*[English](../CONSENSUS.md) · [README](../../README.zh-TW.md)*

> 本文是 [英文版](../CONSENSUS.md) 的正體中文翻譯；若內容不同，以英文版為準。

本文說明 Assent 為何採用目前的結構，不是可執行契約。實際行為以安裝的
`format.md`、`workflow.md`、原始碼與測試為準。

## 最少脈絡，明確責任

每個 AI 只應取得足以正確工作的最少資料：

- `AGENTS.md` 保存長期有效的專案規則，並指向 Assent 契約；
- `instructions.md` 保存所有 AI session 共用的簡短規則；
- `format.md` 告訴 planning/review AI 計畫檔如何運作；
- 一份 `.e.toml` task 是具體的執行契約；
- role prompt 說明當次責任與權限；
- report、journal、diff 與測試輸出提供當前證據。

人類指南負責解釋如何使用，不應成為 AI 執行時的隱藏依賴。

## 先做機械檢查，再使用判斷

Scope、dependency、Git ownership、focused test、candidate construction 與完整驗證
都由程式檢查。只有設定好的機械失敗才交給 AI 判斷，而且回合有限。檢查通過後，
不會再浪費 reviewer session 重複確認。

## 保留成果，無法證明就停止

Assent 在失敗或中斷後保留修改與證據。無法證明 scope、ownership 或安全 transition
時，會停止，不猜測，也不自動還原。真正需要人類判斷的問題會明確回報，而且不會
取消其他無關的排隊工作。

## 由人接受

Task `DONE`、AI review 與通過的 verification receipt 都只是證據。只有人類明確
執行 `assent accept` 才會發布成果。

## 底層仍是一般 Git

Worktree 用來隔離平行計畫並保留復原證據，但不是安全 sandbox。Assent 不隱藏 Git
lineage、不建立 current-plan pointer、不推測 speculative base，也不自動 push。
Cleanup 是另外一個有保護、且不穿越 link target 的操作。

## 維護方式

每條規則只保留一個 canonical owner。行為改變時，source、契約、測試與 reader
documentation 必須一起更新。歷史細節一旦不再幫助現在的使用者或維護者做決定，
就應移除。
