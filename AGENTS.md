# Project instructions

## Project

agents — AI 計畫格式 + 自動調度器。純 Python 3.11+(僅標準庫,tomllib),
Windows 優先、跨平台。CLI 子命令:run / status / check / report / init。
原始碼在 `agents/`,測試在 `tests/`(unittest,不用 pytest)。

## Permanent constraints

- 只用 Python 標準庫,不引入第三方相依。
- Windows 相容優先:路徑用 pathlib、輸出強制 utf-8、鎖用 msvcrt(POSIX 用 fcntl)。
- 測試命令:`python -m unittest discover -s tests`,改動必須讓全部測試通過。
- 註解、docstring、使用者訊息一律正體中文(台灣用語),風格比照現有模組(先讀再寫);
  不用未經解釋的英語行話直譯。
- 燒過 tokens 的產出絕不丟棄:任何流程改動不得引入「失敗即還原工作區」的行為。
- scope 檢查 fail-closed 是安全底線,不得放寬其語意。
- git 永遠必須,不得引入停用開關或無 git 的降級模式。
- 不得引入手工維護的「目前資料夾」指標;工作資料夾由參數明示,或由任務檔
  事實推導,歧義一律拒絕。
- `build/lib/` 是舊建置產物,永遠不要改它。
- `model` 與 `effort` 是正交的抽象檔位:`model` 使用 prime/core/lite;
  選填的 `effort` 使用 low/medium/high,只在任務需偏離模型預設時明寫。
  `high` 表示可攜的高推理投入,不等於廠牌原生最高檔;adapter 不得靜默忽略
  或升降任務明寫的 effort。
- 使用 agents 時,請先讀專案主工作樹的 `.agents/instructions.md`;worktree session 以調度器提示的絕對路徑為準。 <!-- agents-instructions -->
