"""主迴圈:選任務->執行->驗收->checkpoint/重試/額度等待 + report 生成。

繼承 workflow 專案 W5 實測後的鐵則:**執行 AI 燒過 tokens 的產出絕不丟棄。**
- 額度中斷 -> wip 檢查點保留進度,重置後帶「接續」提示續作,不是砍掉重跑。
- 驗收失敗 -> 不還原工作區;重試是在現有成果上修正(便宜),不是從零重做(昂貴)。
- 重試用盡 -> 連同未通過的工作一起 commit 進 BLOCKED 檢查點,交人類收尾裁決。
- scope 檢查 fail-closed:任務檔解析即強制 scope 非空,run 起點的整批解析
  等於零 token 的拒跑閘門。

agents 新增的驗收防禦(格式契約「防禦規則」):
- 執行 AI 對自己任務檔的合法修改只有 status 一行;驗收時把檢查點版本與磁碟版本
  逐欄位比對,其他欄位被動過(放寬 scope、換 verify、改 deps)即驗收失敗。
- scope/verify 一律取自檢查點版本的任務檔,不取磁碟版本。
- 任務自己的 t 檔與 r 檔自動豁免 scope(status 更新與日誌 append 是分內事)。
"""
from __future__ import annotations

import shlex
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, TextIO

from agents import AgentsError, gitops, lockfile
from agents.adapters import Adapter, get_adapter
from agents.config import Config
from agents.folderdeps import (find_unfinished_prerequisites,
                               parse_folder_dependencies)
from agents.plan import (Plan, Task, append_entry, parse_task_file,
                         read_entries, same_except_status, set_status)

# 預設提示詞模板(可用 [prompt] template 覆寫;變數以字面替換,容忍模板內其他大括號)。
_DEFAULT_PROMPT_TEMPLATE = (
    "你是 agents 的執行 AI。先讀專案規則 {agents_md_path},\n"
    "再讀 agents 工作指示 {instructions_path} 與任務檔 {task_path}。\n"
    "只執行任務 {task_id},不要碰其他任務檔。\n"
    "本次日誌身分為 by = \"{agent}\",requested_model = \"{requested_model}\"。\n"
    "requested_model 是本次傳給 AI CLI 的 --model 值。\n"
    "本次抽象 effort = \"{effort}\",實際 requested_effort = \"{requested_effort}\";\n"
    "requested_effort 是本次實際傳給 AI CLI 的值;空字串表示不傳、採 CLI 預設。\n"
    "自行驗證時在目前工作樹執行:{verify_command}\n"
    "完成後:\n"
    "1. 把 {task_path} 的 status 改為 DONE 或 BLOCKED——整份任務檔只准改這一行。\n"
    "2. 在 {journal_path} 檔尾 append 一筆 [[entry]] 日誌(TOML,含 time、\n"
    "   by = \"{agent}\"、requested_model = \"{requested_model}\"、event、summary、\n"
    "   detail;requested_effort 有值時也必須寫入;檔案不存在就建立)。\n"
    "不要執行 git commit,檢查點由調度器負責。"
)
_RETRY_SUFFIX = ("\n上一輪未通過驗收,原因:{failure_reason}。"
                 "上一輪的工作成果仍保留在工作區,請在現有基礎上檢視並修正,不要重做。")
_RESUME_SUFFIX = ("\n上次執行此任務時中斷(額度耗盡或使用者中斷),已完成的部分工作"
                  "保留在工作區(可能含 wip 檢查點)。請先檢視現況,接續完成剩餘部分,"
                  "不要重做已完成的。")

_QUOTA_BUFFER = timedelta(minutes=2)  # 重置時間 + 緩衝,避免剛好卡在邊界又被擋
_QUOTA_TICK = 1.0                     # 倒數的更新間隔(秒)
_DEFAULT_VERIFY_COMMAND = "python .agents/verify.py"
_GIT_REQUIRED_MESSAGE = "本專案尚未初始化 git,請先執行 git init"


@dataclass(frozen=True)
class _SessionIdentity:
    """一次任務執行共用的抽象選擇與實際 CLI 身分。"""

    agent: str
    requested_model: str
    effort: str | None
    requested_effort: str | None


@dataclass
class _SessionState:
    """讓外層中斷收尾取得目前這一輪 session 的解析身分。"""

    identity: _SessionIdentity | None = None


# --------------------------------------------------------------------------- #
# 提示詞 / 小工具
# --------------------------------------------------------------------------- #
def _build_prompt(cfg: Config, task: Task, failure_reason: str | None,
                  session: _SessionIdentity, resumed: bool = False) -> str:
    template = cfg.prompt_template or _DEFAULT_PROMPT_TEMPLATE
    text = (template
            .replace("{agents_md_path}", _agents_md_path_for_prompt(cfg))
            .replace("{instructions_path}",
                     cfg.rel(cfg.agents_dir / "instructions.md"))
            .replace("{task_path}", cfg.rel(task.path))
            .replace("{journal_path}", cfg.rel(task.journal_path))
            .replace("{verify_command}",
                     _verify_command_for_prompt(cfg, task.verify))
            .replace("{task_id}", task.id)
            .replace("{task_title}", task.title)
            .replace("{agent}", session.agent)
            .replace("{requested_model}", session.requested_model)
            .replace("{effort}", session.effort or "")
            .replace("{requested_effort}", session.requested_effort or ""))
    if resumed:
        text += _RESUME_SUFFIX
    if failure_reason:
        text += _RETRY_SUFFIX.replace("{failure_reason}", failure_reason)
    return text


def _resolve_session(cfg: Config, adapter: Adapter,
                     task: Task) -> _SessionIdentity:
    """在啟動 adapter 前解析身分;同一結果供提示、日誌與 CLI 命令共用。"""
    effort = _resolve_effort(cfg, task)
    return _SessionIdentity(
        agent=cfg.adapter_name,
        requested_model=adapter.resolve_model(task.model),
        effort=effort,
        requested_effort=_resolve_requested_effort(cfg, task.model, effort),
    )


def _short(text: str, limit: int = 60) -> str:
    """壓成單行、截斷,供 commit 訊息用。"""
    return " ".join(text.split())[:limit]


def _checkpoint_subject(cfg: Config, kind: str, task: Task, detail: str) -> str:
    """建立含工作資料夾命名空間的任務檢查點主旨。"""
    return f"{kind}({cfg.tasks_name}/{task.id}): {detail}"


def _resolve_effort(cfg: Config, task: Task) -> str | None:
    """任務檔標註優先;無則套設定檔 default_effort;都沒有 -> None(不傳 --effort)。"""
    if task.effort:
        return task.effort
    defaults = (cfg.codex_default_effort if cfg.adapter_name == "codex"
                else cfg.claude_default_effort)
    return defaults.get(task.model)


def _resolve_requested_effort(cfg: Config, model: str,
                              effort: str | None) -> str | None:
    """依「檔位分節 > 平面 > 等值」把抽象 effort 翻成 CLI 實際值。"""
    if effort is None:
        return None
    if cfg.adapter_name == "codex":
        flat, by_tier = cfg.codex_efforts, cfg.codex_tier_efforts
    else:
        flat, by_tier = cfg.claude_efforts, cfg.claude_tier_efforts
    return by_tier.get(model, {}).get(effort, flat.get(effort, effort))


def _task_excludes(cfg: Config, task: Task) -> list[str]:
    """該任務 scope 檢查的豁免清單:自己的 t 檔與 r 檔(status 更新與日誌是分內事)
    加上全域執行期產物。"""
    return [cfg.git_rel(task.path), cfg.git_rel(task.journal_path),
            *cfg.git_excludes]


def _git_read(root, *args: str) -> str | None:
    """唯讀 git 查詢;git 缺席或非零退出一律回 None(status/check 用,不拋 traceback)。"""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(root),
            capture_output=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _has_git_marker(root: Path) -> bool:
    """專案根目錄必須自行初始化 git,不可借用父目錄的 repo。"""
    return (root / ".git").exists()


def _verify_command_for_prompt(cfg: Config, command: str) -> str:
    """隔離執行時把預設驗收腳本展開成主樹絕對路徑。"""
    if cfg.source_root is None or command.strip() != _DEFAULT_VERIFY_COMMAND:
        return command
    parts = [sys.executable, str((cfg.agents_dir / "verify.py").resolve())]
    return (subprocess.list2cmdline(parts) if sys.platform == "win32"
            else shlex.join(parts))


def _agents_md_path_for_prompt(cfg: Config) -> str:
    """選擇專案規則:優先分支版本,沒有時回退主工作樹絕對路徑。"""
    candidate = cfg.root / "AGENTS.md"
    if candidate.is_file():
        return cfg.rel(candidate)
    if cfg.source_root is not None:
        source = cfg.source_root / "AGENTS.md"
        if source.is_file():
            return str(source.resolve())
    return "AGENTS.md(若存在;不存在就略過)"


def _worktree_configuration_errors(cfg: Config) -> list[str]:
    """.agents 管理面必須留在主樹,不得產生 worktree 內的第二份真本。"""
    errors: list[str] = []
    agents_path = cfg.git_rel(cfg.agents_dir)
    tracked = sorted(set(gitops.tracked_paths(cfg.root, agents_path))
                     | set(gitops.tracked_paths(cfg.root, agents_path,
                                                ref="HEAD")))
    if tracked:
        shown = ", ".join(tracked[:5]) + (" ..." if len(tracked) > 5 else "")
        errors.append(f".agents 已有 Git 追蹤檔案:{shown}"
                      "(Git 啟用時整個 .agents 必須留在主工作樹)")
    return errors


# --------------------------------------------------------------------------- #
# run:主迴圈
# --------------------------------------------------------------------------- #
def run(cfg: Config, once: bool = False, task_id: str | None = None, *,
        adapter: Adapter | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None) -> int:
    """執行任務直到全部 DONE/BLOCKED/SKIP(或 once/task_id 只做一個)。回傳進程退出碼。

    起手先檢查資料夾前置，再取工作資料夾檔案鎖；鎖涵蓋整個 run
    (含額度等待的長睡眠);同資料夾已有 run 在跑就印訊息、以退出碼 1 失敗,
    不碰工作區任何東西。status / check / report 唯讀,不取鎖,可在 run 進行中使用。
    """
    try:
        unfinished = find_unfinished_prerequisites(cfg.tasks_dir)
    except AgentsError as e:
        print(f"前置資料夾閘門:FAIL({e})")
        return 1
    if unfinished:
        print("前置資料夾尚未完成，拒絕執行:")
        for prerequisite in unfinished:
            print(f"  - {prerequisite.message()}")
        return 1

    if not _has_git_marker(cfg.root):
        print(_GIT_REQUIRED_MESSAGE)
        return 1

    if sleep is None:
        sleep = time.sleep
    if now is None:
        now = lambda: datetime.now(timezone.utc)  # noqa: E731

    try:
        with lockfile.hold_lock(cfg.tasks_dir, cfg.tasks_name):
            return _run_locked(cfg, once, task_id, adapter, sleep, now)
    except lockfile.LockBusy as e:
        print(str(e))
        return 1


def _run_locked(cfg: Config, once: bool, task_id: str | None,
                adapter: Adapter | None,
                sleep: Callable[[float], None],
                now: Callable[[], datetime]) -> int:
    """已持有工作資料夾鎖後的實際 run 主體。"""
    if cfg.source_root is None:
        try:
            errors = _worktree_configuration_errors(cfg)
            if errors:
                print("git worktree 版控分層錯誤:")
                for error in errors:
                    print(f"  - {error}")
                return 1
            root = gitops.ensure_worktree(cfg.root, cfg.tasks_name)
            cfg = cfg.for_worktree(root)
            print(f"隔離 worktree:{root}")
        except AgentsError as e:
            print(f"git worktree 準備失敗:{e}")
            return 1

    try:
        Plan.parse(cfg.tasks_dir)  # 早期驗證:任何任務檔壞格式,零 token 就拒跑
    except AgentsError as e:
        print(f"無法解析工作資料夾:{e}")
        return 1

    if adapter is None:
        try:
            adapter = get_adapter(cfg.adapter_name, cfg)
        except AgentsError as e:
            print(str(e))
            return 1

    try:
        gitops.ensure_clean(cfg.root, cfg.git_excludes)
        branch = gitops.ensure_branch(cfg.root, cfg.branch_prefix)
        print(f"工作分支:{branch}")
    except AgentsError as e:
        print(f"git 準備失敗:{e}")
        return 1

    current_task: Task | None = None
    current_session: _SessionState | None = None
    try:
        while True:
            plan = Plan.parse(cfg.tasks_dir)
            if task_id is not None:
                task = plan.get(task_id)
                if task is None:
                    print(f"工作資料夾中找不到任務 {task_id}")
                    return 1
                status_by_id = {t.id: t.status for t in plan.tasks}
                unmet = [d for d in task.deps
                         if status_by_id.get(d) not in ("DONE", "SKIP")]
                if unmet:
                    print(f"任務 {task_id} 的前置未完成:{', '.join(unmet)}")
                    return 1
                if task.status not in ("TODO", "WIP"):
                    print(f"任務 {task_id} 目前狀態為 {task.status},"
                          f"非 TODO/WIP,略過")
                    return 0
                resumed = task.status == "WIP"
            else:
                selected = plan.next_task()
                if selected is None:
                    break
                task, resumed = selected

            session = _SessionState()
            current_task = task
            current_session = session
            _process_task(cfg, task, adapter, sleep, now, session, resumed)
            current_task = None
            current_session = None

            if once or task_id is not None:
                break
    except KeyboardInterrupt:
        # Windows 主控台的 Ctrl+C 會同時送達子程序(AI session),故 session 由 OS
        # 訊號終止;engine 這裡把已產出的進度收進 wip 檢查點(絕不丟棄)後以 130 退出。
        print("\n收到中斷(Ctrl+C):session 已終止,保留目前進度...")
        if (current_task is not None and current_session is not None
                and current_session.identity is not None):
            _mark_interrupted_task(
                current_task, current_session.identity,
                "使用者中斷,保留進度供下次接續", now,
                detail="run 收到 Ctrl+C")
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", current_task, "使用者中斷,保留進度")
                if current_task is not None
                else f"wip({cfg.tasks_name}): 使用者中斷,保留進度")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("已把進度收進 wip 檢查點(不滿意可自行 git 回退)。")
        except AgentsError as e:
            print(f"wip 檢查點建立失敗:{e}(工作區保持原樣,未丟棄)")
        _try_write_report(cfg)
        print("已中斷。")
        return 130
    except AgentsError as e:
        print(f"執行中止(基礎設施錯誤):{e}")
        if (current_task is not None and current_session is not None
                and current_session.identity is not None):
            _mark_interrupted_task(
                current_task, current_session.identity,
                "基礎設施錯誤中止,保留進度供下次接續", now,
                detail=str(e))
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", current_task, "基礎設施錯誤中止,保留進度")
                if current_task is not None
                else f"wip({cfg.tasks_name}): 基礎設施錯誤中止,保留進度")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("已把進度收進 wip 檢查點。")
        except AgentsError:
            pass
        _try_write_report(cfg)
        return 1

    _print_summary(Plan.parse(cfg.tasks_dir))
    _try_write_report(cfg)
    return 0


def _mark_interrupted_task(task: Task, session: _SessionIdentity, summary: str,
                           now: Callable[[], datetime], *, detail: str) -> None:
    """中止時把目前任務持久化為 WIP 並寫機器日誌;二次錯誤只警告。

    BLOCKED 是執行 AI 可產生的合法終態,不可被中止處理覆寫。DONE 在調度器驗收
    通過前尚非可信終態,因此仍改回 WIP,讓下次 run 接續並重新通過閘門。
    """
    try:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "WIP")
    except Exception as e:  # 中止收尾不得用二次錯誤掩蓋原始退出碼
        print(f"中斷任務狀態寫回失敗:{e}(工作區保持原樣,未丟棄)")

    try:
        append_entry(
            task.journal_path, by="scheduler", event="interrupt",
            summary=summary, detail=detail,
            agent=session.agent, requested_model=session.requested_model,
            requested_effort=session.requested_effort,
            time_str=now().isoformat(timespec="seconds"))
    except Exception as e:  # 狀態與日誌各自嘗試,其中一項失敗不妨礙另一項
        print(f"中斷日誌寫入失敗:{e}(工作區保持原樣,未丟棄)")


def _process_task(cfg: Config, task: Task, adapter: Adapter,
                  sleep: Callable[[float], None],
                  now: Callable[[], datetime], session_state: _SessionState,
                  resumed: bool = False) -> None:
    """跑單一任務的完整生命週期;內部處理額度等待與重試,結束時該任務已 DONE/BLOCKED。

    `task` 是選任務當下(= 上一個檢查點)解析的可信版本:scope/verify 與全部欄位
    一律以它為準,執行 AI 對磁碟版本唯一的合法修改是 status 一行(_evaluate 比對)。
    """
    print(f"\n任務 {task.id}:{task.title}")
    if resumed:
        print("  (偵測到 WIP:上次中斷的任務,帶接續提示續作)")

    # 該任務起點的 HEAD:scope 檢查要涵蓋起點以來的全部改動(含 wip 檢查點)。
    start_ref = gitops.head_ref(cfg.root)

    attempts_used = 0
    failure_reason: str | None = None
    while True:
        session = _resolve_session(cfg, adapter, task)
        session_state.identity = session
        prompt = _build_prompt(cfg, task, failure_reason, session, resumed)
        print(f"  開 session(model={task.model} -> {session.requested_model}, "
              f"effort(抽象)={session.effort or '未指定'} -> "
              f"requested_effort(實際)={session.requested_effort or 'CLI 預設'})...")
        result = adapter.run_task(
            prompt, session.requested_model, session.requested_effort, cfg.root)

        if result.quota_exhausted:  # 額度耗盡不計失敗
            print("  額度耗盡 -> 保留進度(wip 檢查點)並等待重置後接續...")
            append_entry(task.journal_path, by="scheduler", event="quota",
                         summary="額度耗盡,保留進度等待重置後接續",
                         agent=session.agent,
                         requested_model=session.requested_model,
                         requested_effort=session.requested_effort,
                         time_str=now().isoformat(timespec="seconds"))
            if gitops.commit_if_dirty(
                    cfg.root, _checkpoint_subject(
                        cfg, "wip", task, "額度中斷,保留進度"),
                    cfg.git_excludes):
                print("  已建立 wip 檢查點。")
            _try_write_report(cfg)
            _wait_for_quota(cfg, result.reset_at, sleep, now)
            resumed = True
            continue  # 接續同一任務,不計入重試次數

        outcome, reason = _evaluate(cfg, task, start_ref)
        if outcome == "done":
            print("  驗收通過 -> 建立檢查點")
            if not gitops.commit_if_dirty(
                    cfg.root, _checkpoint_subject(
                        cfg, "auto", task, _short(task.title) or "完成"),
                    cfg.git_excludes):
                print("  (工作區無新變更,進度已在先前的 wip 檢查點內)")
            _try_write_report(cfg)
            return
        if outcome == "self_blocked":
            print("  執行 AI 自標 BLOCKED(合法產出,交人類裁決)-> 建立檢查點")
            gitops.commit_if_dirty(
                cfg.root, _checkpoint_subject(
                    cfg, "auto", task, "BLOCKED(執行 AI 自標)"),
                cfg.git_excludes)
            _try_write_report(cfg)
            return

        # outcome == "fail":不還原(產出保留),帶原因重試;次數用盡由調度器標 BLOCKED,
        # 未通過的工作連同 BLOCKED 標記一起 commit,交人類收尾裁決。
        print(f"  驗收未通過:{reason}")
        if attempts_used < cfg.retry_per_task:
            attempts_used += 1
            failure_reason = reason
            print(f"  保留現有成果,帶失敗原因重試(第 {attempts_used} 次)...")
            continue
        print("  重試次數用盡 -> 調度器標記 BLOCKED(未通過的工作一併保留)")
        _mark_blocked(cfg, task, session, reason or "驗收未通過", now,
                      attempts=attempts_used)
        _try_write_report(cfg)
        return


def _evaluate(cfg: Config, task: Task,
              start_ref: str | None = None) -> tuple[str, str | None]:
    """驗收:狀態 -> 結構比對(防竄改)-> scope -> verify。回傳 (outcome, reason)。

    outcome in {"done", "self_blocked", "fail"}。scope/verify 與全部欄位取自可信的
    檢查點版本 `task`;磁碟版本只被允許改 status 一行。
    """
    try:
        fresh = parse_task_file(task.path)
    except AgentsError as e:
        return "fail", f"重新解析任務檔失敗(執行 AI 可能改壞了它):{e}"

    # 狀態檢查
    if fresh.status == "BLOCKED":
        return "self_blocked", None
    if fresh.status != "DONE":
        return "fail", f"狀態未更新為 DONE/BLOCKED(目前為 {fresh.status})"

    # 結構比對:除 status 外任何欄位被改動 = 越權(放寬 scope、換 verify、改 deps)
    tampered = same_except_status(task, fresh)
    if tampered:
        return "fail", (f"任務檔除 status 外的欄位被修改:{', '.join(tampered)}"
                        "(執行 AI 只准改 status 一行)")

    # scope 檢查(以可信的檢查點 scope 為準,含 wip 檢查點內的改動;
    # 自己的 t 檔/r 檔與執行期產物豁免)
    outside = gitops.changes_outside_scope(
        cfg.root, task.scope, since_ref=start_ref,
        excludes=_task_excludes(cfg, task))
    if outside:
        shown = ", ".join(outside[:5]) + (" ..." if len(outside) > 5 else "")
        return "fail", f"出現 scope 外的變更:{shown}"

    # 驗收命令(以可信的檢查點 verify 為準)
    rc = _run_verify(cfg, task.verify)
    if rc != 0:
        return "fail", f"驗收命令退出碼非 0(={rc}):{task.verify}"

    return "done", None


def _run_verify(cfg: Config, command: str) -> int:
    """在目標工作樹執行 verify,退出碼 0 = 通過。

    隔離執行的預設腳本從主樹以絕對路徑載入,其餘命令維持原本的 shell
    語意;兩者的 cwd 都是目前目標工作樹。
    """
    if cfg.source_root is not None and command.strip() == _DEFAULT_VERIFY_COMMAND:
        result = subprocess.run(
            [sys.executable, str((cfg.agents_dir / "verify.py").resolve())],
            cwd=str(cfg.root), capture_output=True, encoding="utf-8",
            errors="replace")
    else:
        result = subprocess.run(
            command, shell=True, cwd=str(cfg.root),
            capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        if tail:
            print("  -- 驗收輸出(末段)--")
            for line in tail:
                print(f"  | {line}")
    return result.returncode


def _mark_blocked(cfg: Config, task: Task, session: _SessionIdentity, reason: str,
                  now: Callable[[], datetime], attempts: int | None = None) -> None:
    """調度器把任務標 BLOCKED + r 檔 append 機器記錄 + 建檢查點。

    工作樹此時可能還留著未通過驗收的成果(不還原):一併收進 BLOCKED 檢查點,
    人類收尾時直接在 git 歷史裡裁決要留要改,tokens 不白燒。
    """
    set_status(task.path, "BLOCKED")
    detail = (f"重試 {attempts} 次後仍未通過驗收" if attempts
              else "未經重試即判定失敗")
    append_entry(task.journal_path, by="scheduler", event="blocked",
                 summary=f"調度器標 BLOCKED:{reason}", detail=detail,
                 agent=session.agent,
                 requested_model=session.requested_model,
                 requested_effort=session.requested_effort,
                 time_str=now().isoformat(timespec="seconds"))
    gitops.commit_if_dirty(
        cfg.root, _checkpoint_subject(
            cfg, "auto", task, f"BLOCKED - {_short(reason, 50)}"),
        cfg.git_excludes)


def _quota_wait_seconds(cfg: Config, reset_at: datetime | None,
                        now: Callable[[], datetime]) -> float:
    """本輪額度該等多久(秒)。能解析重置時間 -> 睡到重置 + 緩衝(已過則 0);
    無法解析 -> 一個 poll 間隔。純函式,方便單獨測試。"""
    if reset_at is not None:
        return max(0.0, (reset_at + _QUOTA_BUFFER - now()).total_seconds())
    return float(cfg.quota_poll_minutes * 60)


def _wait_for_quota(cfg: Config, reset_at: datetime | None,
                    sleep: Callable[[float], None],
                    now: Callable[[], datetime]) -> None:
    """額度銜接:在 run 自己的終端原地倒數(unix 風格 \\r 覆寫同一行)。"""
    seconds = _quota_wait_seconds(cfg, reset_at, now)
    if reset_at is not None:
        label = f"額度重置 {reset_at.astimezone().strftime('%H:%M:%S')}"
    else:
        label = f"額度輪詢(每 {cfg.quota_poll_minutes} 分鐘)"
    _countdown(seconds, label, sleep)


def _countdown(seconds: float, label: str, sleep: Callable[[float], None], *,
               tick: float = _QUOTA_TICK, stream: TextIO | None = None) -> None:
    """倒數等待。終端機(tty)-> 用 \\r 原地更新一行讀秒,不逐行堆疊;非 tty(導向檔案
    /管線)-> 只印一行避免灌爆日誌。injected sleep 讓測試能不真的睡。"""
    if seconds <= 0:
        return
    stream = stream or sys.stdout
    interactive = hasattr(stream, "isatty") and stream.isatty()
    if not interactive:
        stream.write(f"  {label}:等待約 {int(seconds)} 秒後重跑。\n")
        stream.flush()
        sleep(seconds)
        return
    remaining = seconds

    # terminal_log.TeeTextIO 提供 terminal-only 通道:倒數是瞬時 UI,
    # 不應每秒寫入 _agents.log。測試用/一般 TextIO 則沿用 write。
    terminal_only = getattr(stream, "write_terminal_only", None)

    def transient_write(text: str) -> None:
        if terminal_only is not None:
            terminal_only(text)
        else:
            stream.write(text)
            stream.flush()
    while remaining > 0:
        h, rem = divmod(int(remaining + 0.999), 3600)
        m, s = divmod(rem, 60)
        transient_write(f"\r  {label}:倒數 {h:02d}:{m:02d}:{s:02d} 後重跑... ")
        step = tick if tick < remaining else remaining
        sleep(step)
        remaining -= step
    transient_write("\r" + " " * 48 + "\r")  # 清掉倒數行,交還游標


def _print_summary(plan: Plan) -> None:
    counts = Counter(t.status for t in plan.tasks)
    print("\n===== 執行總結 =====")
    print(f"DONE: {counts.get('DONE', 0)}  BLOCKED: {counts.get('BLOCKED', 0)}  "
          f"SKIP: {counts.get('SKIP', 0)}  TODO: {counts.get('TODO', 0)}  "
          f"WIP: {counts.get('WIP', 0)}  (共 {len(plan.tasks)} 個任務)")
    blocked = [t for t in plan.tasks if t.status == "BLOCKED"]
    if blocked:
        print("BLOCKED 任務(交人類裁決):")
        for t in blocked:
            print(f"  - {t.id}:{t.title}")
    if counts.get("TODO", 0) == 0 and counts.get("WIP", 0) == 0:
        print("全部任務已 DONE/BLOCKED/SKIP。")


# --------------------------------------------------------------------------- #
# report:零 token 的人讀報告(驗收會議的議程表)
# --------------------------------------------------------------------------- #
def render_report(cfg: Config, plan: Plan,
                  now: Callable[[], datetime] | None = None) -> str:
    """把 t/r 檔與 git 資訊彙整成一頁純文字報告。彙整是機械工作,零 token。"""
    if now is None:
        now = lambda: datetime.now(timezone.utc)  # noqa: E731
    counts = Counter(t.status for t in plan.tasks)

    git_root = _query_git_root(cfg)
    checkpoints: dict[str, str] = {}
    log = _git_read(git_root, "log", "--pretty=%h\t%s")
    if log:
        for line in log.splitlines():
            h, _, subject = line.partition("\t")
            for t in plan.tasks:
                prefix = f"auto({cfg.tasks_name}/{t.id}): "
                if t.id not in checkpoints and subject.startswith(prefix):
                    checkpoints[t.id] = h

    branch = _git_read(git_root, "branch", "--show-current") or "N/A"
    stamp = now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "執行報告(_report.md;agents 自動生成,勿手動編輯;重新生成:agents report)",
        "=" * 60,
        f"計畫資料夾:{cfg.tasks_name}",
        f"產生時間:{stamp}",
        f"分支:{branch}",
        f"進度:DONE {counts.get('DONE', 0)} / BLOCKED {counts.get('BLOCKED', 0)} / "
        f"WIP {counts.get('WIP', 0)} / TODO {counts.get('TODO', 0)} / "
        f"SKIP {counts.get('SKIP', 0)}(共 {len(plan.tasks)})",
        "",
    ]
    for t in plan.tasks:
        mark = checkpoints.get(t.id)
        lines.append(f"{t.id}  {t.status:<8} {t.title}"
                     + (f"  [{mark}]" if mark else ""))
        if t.status in ("BLOCKED", "WIP"):
            entries = read_entries(t.journal_path)
            if entries:
                last = entries[-1]
                summary = str(last.get("summary", "")).strip()
                by = last.get("by", "?")
                if summary:
                    lines.append(f"      └ 最後日誌({by}):{summary}")
    blocked = [t for t in plan.tasks if t.status == "BLOCKED"]
    if blocked:
        lines += ["", "待裁決:對照各 BLOCKED 任務的 r 檔與檢查點 commit,"
                      "改任務檔後把 status 改回 TODO 續跑,或標 SKIP 放棄。"]
    return "\n".join(lines) + "\n"


def write_report(cfg: Config, plan: Plan,
                 now: Callable[[], datetime] | None = None) -> Path:
    """把報告寫到工作資料夾的 _report.md(執行期產物,不進版控)。"""
    path = cfg.tasks_dir / "_report.md"
    path.write_text(render_report(cfg, plan, now), encoding="utf-8")
    return path


def _try_write_report(cfg: Config) -> None:
    """run 收尾時盡力更新 report;報告失敗絕不影響主流程的結果與退出碼。"""
    try:
        write_report(cfg, Plan.parse(cfg.tasks_dir))
    # 這是明確的盡力而為隔離邊界:包含權限、檔案鎖與內容解析等
    # 任何一般錯誤都不得掩蓋任務結果;KeyboardInterrupt/SystemExit 仍照常傳播。
    except Exception:
        pass


def report(cfg: Config) -> int:
    """子命令:生成 _report.md 並印到終端(零 token)。"""
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AgentsError as e:
        print(f"無法解析工作資料夾:{e}")
        return 1
    text = render_report(cfg, plan)
    path = write_report(cfg, plan)
    print(text, end="")
    print(f"(已寫入 {path})")
    return 0


# --------------------------------------------------------------------------- #
# status:零 token 進度查詢
# --------------------------------------------------------------------------- #
def status(cfg: Config) -> int:
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AgentsError as e:
        print(f"無法解析工作資料夾:{e}")
        return 1

    counts = Counter(t.status for t in plan.tasks)
    print(f"工作資料夾:{cfg.tasks_dir}")
    print(f"進度:DONE {counts.get('DONE', 0)} / BLOCKED {counts.get('BLOCKED', 0)} / "
          f"SKIP {counts.get('SKIP', 0)} / WIP {counts.get('WIP', 0)} / "
          f"TODO {counts.get('TODO', 0)}(共 {len(plan.tasks)})")

    git_root = _query_git_root(cfg)
    branch = _git_read(git_root, "branch", "--show-current")
    print(f"目前分支:{branch or 'N/A'}")
    last = _git_read(git_root, "log", "-1", "--grep=^auto(", "--pretty=%h %s")
    print(f"最後檢查點:{last or '(尚無 auto() commit)'}")

    selected = plan.next_task()
    if selected is not None:
        nxt, resumed = selected
        effort = _resolve_effort(cfg, nxt)
        requested_effort = _resolve_requested_effort(cfg, nxt.model, effort)
        effort_label = (f"{effort} -> {requested_effort}" if effort
                        else "CLI 預設")
        tag = "(WIP 續作)" if resumed else ""
        print(f"下一個任務:{nxt.id} [{nxt.model} /{effort_label}] "
              f"{nxt.title}{tag}")
    elif counts.get("TODO", 0):
        print("下一個任務:(尚有 TODO,但前置未完成或被 BLOCKED 擋住)")
    else:
        print("下一個任務:(無,全部 DONE/BLOCKED/SKIP)")
    return 0


def _query_git_root(cfg: Config) -> Path:
    """已有有效 worktree 時,改從隔離分支讀 git 資訊。"""
    if cfg.source_root is not None:
        return cfg.root
    candidate = gitops.worktree_path(cfg.root, cfg.tasks_name)
    top = _git_read(candidate, "rev-parse", "--show-toplevel")
    if top and Path(top).resolve() == candidate.resolve():
        return candidate
    return cfg.root


# --------------------------------------------------------------------------- #
# check:零 token 環境與格式驗證(會議的散會條件)
# --------------------------------------------------------------------------- #
def check(cfg: Config) -> int:
    if not _has_git_marker(cfg.root):
        print(_GIT_REQUIRED_MESSAGE)
        return 1

    ok = True
    print(f"設定檔:OK({cfg.agents_dir / 'agents.toml'} 已載入,"
          f"工作資料夾 = {cfg.tasks_name})")

    # 工作資料夾與任務檔格式(解析即完成:必填欄位、檔位、scope 非空、
    # deps 存在且無循環、id 不重複)
    try:
        plan = Plan.parse(cfg.tasks_dir)
        print(f"任務檔格式:OK({len(plan.tasks)} 個任務,依賴無循環)")
    except AgentsError as e:
        ok = False
        print(f"任務檔格式:FAIL({e})")

    # 指定資料夾的依賴宣告格式與引用完整性；全圖循環由 CLI 無參數 check 驗證。
    try:
        dependencies = parse_folder_dependencies(cfg.tasks_dir)
        after = "、".join(dependencies.after) or "無"
        print(f"資料夾依賴:OK(after = {after})")
    except AgentsError as e:
        ok = False
        print(f"資料夾依賴:FAIL({e})")

    # adapter 可解析
    try:
        get_adapter(cfg.adapter_name, cfg)
        print(f"adapter:OK({cfg.adapter_name})")
    except AgentsError as e:
        ok = False
        print(f"adapter:FAIL({e})")

    # git repo
    inside = _git_read(cfg.root, "rev-parse", "--is-inside-work-tree")
    if inside == "true":
        print("git repo:OK")
        try:
            errors = _worktree_configuration_errors(cfg)
        except AgentsError as e:
            errors = [str(e)]
        if errors:
            ok = False
            print("worktree 版控分層:FAIL")
            for error in errors:
                print(f"  - {error}")
        else:
            print("worktree 版控分層:OK")
    else:
        ok = False
        print("git repo:FAIL(專案根目錄不是 git 工作樹,或 git 未安裝/未在 PATH)")

    # 目前 adapter 的 CLI 可執行
    if cfg.adapter_name in ("claude", "codex"):
        command = (cfg.claude_command if cfg.adapter_name == "claude"
                   else cfg.codex_command)
        label = cfg.adapter_name
        try:
            result = subprocess.run(
                [command, "--version"],
                capture_output=True, encoding="utf-8", errors="replace")
            if result.returncode == 0:
                print(f"{label} CLI:OK({result.stdout.strip() or '可執行'})")
            else:
                ok = False
                print(f"{label} CLI:FAIL(--version 退出碼 {result.returncode})")
        except FileNotFoundError:
            ok = False
            print(f"{label} CLI:FAIL(找不到執行檔 {command!r})")

    print("結果:通過" if ok else "結果:有項目未通過")
    return 0 if ok else 1
