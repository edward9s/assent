"""gitops 測試:全部在 tempfile.mkdtemp() 建的臨時 repo 中進行(鐵則 4)。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agents import AgentsError
from agents.gitops import (
    changes_outside_scope, commit_all, commit_if_dirty, ensure_branch,
    ensure_clean, head_ref, restore)


def _run(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True,
                   encoding="utf-8", check=True)


class GitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        _run(self.root, "init")
        _run(self.root, "config", "user.name", "Test")
        _run(self.root, "config", "user.email", "test@example.com")
        (self.root / "README.md").write_text("init\n", encoding="utf-8")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "init")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class TestEnsureClean(GitTestCase):
    def test_clean_repo_passes(self):
        ensure_clean(self.root)  # 不應拋錯

    def test_dirty_repo_raises(self):
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(AgentsError):
            ensure_clean(self.root)

    def test_modified_tracked_file_raises(self):
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(AgentsError):
            ensure_clean(self.root)

    def test_excluded_runtime_artifacts_are_ignored(self):
        # .agents 內先有被追蹤的檔,porcelain 才會逐檔列出(整目錄未追蹤時只列目錄)
        (self.root / ".agents").mkdir()
        (self.root / ".agents" / "agents.toml").write_text("x", encoding="utf-8")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "track agents dir")
        (self.root / ".agents" / "agents.log").write_text("live", encoding="utf-8")
        ensure_clean(self.root, excludes=(".agents/agents.log",))

    def test_without_excludes_runtime_log_counts_as_dirty(self):
        (self.root / "agents.log").write_text("live", encoding="utf-8")
        with self.assertRaises(AgentsError):
            ensure_clean(self.root)


class TestEnsureBranch(GitTestCase):
    def test_creates_new_branch_from_main(self):
        branch = ensure_branch(self.root, "workflow/")
        self.assertTrue(branch.startswith("workflow/"))
        current = subprocess.run(
            ["git", "branch", "--show-current"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(current, branch)

    def test_reuses_existing_workflow_branch(self):
        first = ensure_branch(self.root, "workflow/")
        second = ensure_branch(self.root, "workflow/")
        self.assertEqual(first, second)

    def test_run_id_unique_across_prefixless_calls(self):
        # 從非 workflow 分支起始,兩次呼叫(先切回)應各自建立
        branch1 = ensure_branch(self.root, "workflow/")
        _run(self.root, "checkout", "master")
        branch2 = ensure_branch(self.root, "workflow/")
        self.assertNotEqual(branch1, branch2)


class TestChangesOutsideScope(GitTestCase):
    def test_excluded_paths_are_never_a_scope_violation(self):
        (self.root / "agents.log").write_text("AI output", encoding="utf-8")
        self.assertEqual(
            changes_outside_scope(self.root, [], excludes=("agents.log",)), [])

    def test_new_file_in_scope_not_flagged(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "t.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["tests/"])
        self.assertEqual(outside, [])

    def test_new_file_outside_scope_flagged(self):
        (self.root / "secret.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["tests/"])
        self.assertIn("secret.py", outside)

    def test_modified_file_outside_scope_flagged(self):
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["tests/"])
        self.assertIn("README.md", outside)

    def test_modified_file_inside_scope_not_flagged(self):
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["README.md"])
        self.assertEqual(outside, [])

    def test_windows_backslash_scope_normalized(self):
        (self.root / "workflow").mkdir()
        (self.root / "workflow" / "gitops.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["workflow\\"])
        self.assertEqual(outside, [])

    def test_empty_scope_denies_all(self):
        # 計畫檔未設定 workflow:scope 標記 → scope=[] → fail-closed,任何變更皆視為越界
        (self.root / "anything.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, [])
        self.assertIn("anything.py", outside)

    def test_non_ascii_filename_not_octal_escaped(self):
        # core.quotepath=false:中文檔名應直接以 UTF-8 顯示,不是 \NNN 八進位跳脫
        (self.root / "測試.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["tests/"])
        self.assertIn("測試.py", outside)

    def test_since_ref_covers_committed_wip_changes(self):
        # 額度中斷的 wip 檢查點把越界檔 commit 掉之後,工作樹是乾淨的;
        # 給 since_ref 才能把「任務起點以來」已 commit 的越界改動也抓出來
        start = head_ref(self.root)
        (self.root / "secret.py").write_text("x", encoding="utf-8")
        commit_all(self.root, "wip(T1): 額度中斷,保留進度")
        self.assertEqual(changes_outside_scope(self.root, ["tests/"]), [])
        outside = changes_outside_scope(self.root, ["tests/"], since_ref=start)
        self.assertIn("secret.py", outside)

    def test_since_ref_deduplicates_with_working_tree(self):
        start = head_ref(self.root)
        (self.root / "secret.py").write_text("x", encoding="utf-8")
        commit_all(self.root, "wip: 保留")
        (self.root / "secret.py").write_text("y", encoding="utf-8")  # 又改了同一檔
        outside = changes_outside_scope(self.root, ["tests/"], since_ref=start)
        self.assertEqual(outside.count("secret.py"), 1)


class TestCommitAll(GitTestCase):
    def test_commit_all_cleans_working_tree(self):
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        commit_all(self.root, "auto(W2): test commit")
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        self.assertEqual(status.strip(), "")

    def test_commit_message_recorded(self):
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        commit_all(self.root, "auto(W2): message check")
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(log, "auto(W2): message check")

    def test_excludes_inside_gitignored_dir_do_not_crash(self):
        # 回歸:整個 .agents/ 被 .gitignore 時,排除項 pathspec 點名 ignored
        # 路徑會讓 git add 退出碼 1(狗糧實測炸點);過濾後必須能正常 commit。
        (self.root / ".gitignore").write_text(".agents/\n", encoding="utf-8")
        agents_dir = self.root / ".agents"
        agents_dir.mkdir()
        (agents_dir / "agents.log").write_text("log", encoding="utf-8")
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        commit_all(self.root, "auto(t001): 不應因 ignored 排除項而失敗",
                   excludes=(".agents/agents.log", ".agents/plan01/report.md"))
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        self.assertEqual(status.strip(), "")

    def test_live_excludes_still_excluded_when_mixed_with_ignored(self):
        # 混合情境:一項被 gitignore(濾掉)、一項未被 ignore(仍要生效排除)
        (self.root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        (self.root / "ignored.log").write_text("x", encoding="utf-8")
        (self.root / "live.log").write_text("x", encoding="utf-8")
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        commit_all(self.root, "auto(t001): 混合排除",
                   excludes=("ignored.log", "live.log"))
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        self.assertIn("new.txt", tracked)
        self.assertNotIn("live.log", tracked)   # 未被 ignore 的排除項仍生效
        self.assertNotIn("ignored.log", tracked)


class TestCommitIfDirty(GitTestCase):
    def test_dirty_tree_commits_and_returns_true(self):
        (self.root / "wip.txt").write_text("x", encoding="utf-8")
        self.assertTrue(commit_if_dirty(self.root, "wip(T1): 額度中斷,保留進度"))
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(log, "wip(T1): 額度中斷,保留進度")

    def test_clean_tree_returns_false_without_commit(self):
        before = head_ref(self.root)
        self.assertFalse(commit_if_dirty(self.root, "不該出現"))
        self.assertEqual(head_ref(self.root), before)

    def test_excluded_artifact_alone_does_not_create_commit(self):
        before = head_ref(self.root)
        (self.root / "agents.log").write_text("AI output", encoding="utf-8")
        self.assertFalse(commit_if_dirty(self.root, "should not exist",
                                         excludes=("agents.log",)))
        self.assertEqual(head_ref(self.root), before)


class TestHeadRef(GitTestCase):
    def test_returns_current_head_hash(self):
        ref = head_ref(self.root)
        expect = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(ref, expect)

    def test_repo_without_commits_returns_none(self):
        import tempfile
        empty = Path(tempfile.mkdtemp())
        try:
            _run(empty, "init")
            self.assertIsNone(head_ref(empty))
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class TestRestore(GitTestCase):
    def test_restore_removes_untracked_and_modified(self):
        (self.root / "untracked.txt").write_text("x", encoding="utf-8")
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        restore(self.root)
        self.assertFalse((self.root / "untracked.txt").exists())
        self.assertEqual((self.root / "README.md").read_text(encoding="utf-8"), "init\n")
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        self.assertEqual(status.strip(), "")


class TestGitMissing(unittest.TestCase):
    def test_missing_git_raises_workflow_error(self):
        import agents.gitops as gitops
        original = gitops.subprocess.run

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("git not found")

        gitops.subprocess.run = fake_run
        try:
            with self.assertRaises(AgentsError):
                ensure_clean(Path("."))
        finally:
            gitops.subprocess.run = original


if __name__ == "__main__":
    unittest.main()
