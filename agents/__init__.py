"""agents - AI 專案的計畫格式 + 零 token 自動調度器。

格式契約見 agents/templates/format.md(agents init 會複製到專案的 .agents/)。
執行期零第三方依賴:本套件只准 import Python 標準庫。
"""


class AgentsError(Exception):
    """調度器可預期的錯誤;訊息直接呈現給使用者,不該以 traceback 收場。"""
