"""Black-box CLI integration tests for transactional local acceptance.

Every fixture is a disposable local repository.  The tests invoke ``python -m
assent accept`` rather than its implementation helpers, and make assertions on
Git's observable refs, parents, messages, worktrees, and command exit codes.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assent import auto_fix, gitops
from assent.accept import accept_folder
from assent.config import load_config
from assent.lockfile import hold_integration_lock, hold_lock


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_VERIFY = "python -c pass"


class AcceptCliCase(unittest.TestCase):
    """A Git repository fixture isolated from global and system Git settings."""

    autocrlf = "false"
    eol = "lf"

    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent accept cli "))
        self.root = self.parent / "repository with spaces and Unicode 測試"
        self.root.mkdir()
        self.addCleanup(self._cleanup)
        self.env = dict(os.environ)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["PYTHONPATH"] = os.pathsep.join(
            (str(_PROJECT_ROOT), self.env.get("PYTHONPATH", "")))
        self._git("init")
        self._git("config", "user.name", "Assent CLI Test")
        self._git("config", "user.email", "assent-cli@example.invalid")
        self._git("config", "core.autocrlf", self.autocrlf)
        self._git("checkout", "-b", "trunk")
        (self.root / ".gitattributes").write_text(
            f"*.txt text eol={self.eol}\n", encoding="utf-8", newline="\n")
        (self.root / ".gitignore").write_text(
            ".assent/\n", encoding="utf-8", newline="\n")
        (self.root / "README.txt").write_text(
            "baseline\n", encoding="utf-8", newline="\n")
        self._git("add", "-A")
        self._git("commit", "-m", "baseline")

        self.folder = "計畫01"
        self.assent_dir = self.root / ".assent"
        self.tasks_dir = self.assent_dir / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config = self.assent_dir / "assent.toml"
        self.config.write_text("", encoding="utf-8", newline="\n")
        self._write_verify("raise SystemExit(0)\n")
        self._write_task()

    def _cleanup(self) -> None:
        if self.root.exists():
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=self.root,
                capture_output=True, encoding="utf-8", errors="replace", env=self.env)
            for line in result.stdout.splitlines():
                if line.startswith("worktree "):
                    path = Path(line.removeprefix("worktree "))
                    if path.resolve() != self.root.resolve():
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", str(path)],
                            cwd=self.root, capture_output=True, env=self.env)
        shutil.rmtree(self.parent, ignore_errors=True)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.root, capture_output=True,
            encoding="utf-8", errors="replace", env=self.env)
        if result.returncode:
            self.fail("git command failed: " + " ".join(args) + "\n" +
                      result.stdout + result.stderr)
        return result.stdout.strip()

    def _write_task(self, *, folder: str | None = None,
                    status: str = "DONE") -> Path:
        name = folder or self.folder
        tasks_dir = self.assent_dir / name
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / "t001_任務.e.toml"
        path.write_text(
            'title = "CLI acceptance task"\n'
            'deps = []\n'
            'model = "core"\n'
            f'status = "{status}"\n'
            'scope = ["src/"]\n'
            f'verify = "{_VERIFY}"\n'
            'goal = "Verify local acceptance."\n'
            'acceptance = "The verification command passes."\n',
            encoding="utf-8", newline="\n")
        return path

    def _write_verify(self, text: str) -> None:
        path = self.assent_dir / "verify.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def _cli(self, command: str, folder: str | None = None, *,
             stdin: str | None = None) -> subprocess.CompletedProcess:
        args = [sys.executable, "-m", "assent", command]
        if folder is not None:
            args.append(folder)
        args.extend(("--config", str(self.config)))
        return subprocess.run(
            args, cwd=self.root, capture_output=True, encoding="utf-8",
            errors="replace", env=self.env,
            # No text at all means a genuinely closed stdin, the way an
            # unattended or piped invocation really runs.
            input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)

    def _make_source(self, *, folder: str | None = None,
                     filename: str = "result with space 空白.txt",
                     content: str = "accepted\n") -> tuple[Path, str, str]:
        name = folder or self.folder
        branch = f"{name}/run"
        path = self.parent / f"{self.root.name}.worktrees" / name
        self._git("worktree", "add", "-b", branch, str(path))
        (path / filename).write_text(content, encoding="utf-8", newline="\n")
        self._git("add", "-A", cwd=path)
        self._git("commit", "-m", f"finish {name}", cwd=path)
        return path, branch, self._git("rev-parse", branch)

    def _head(self, ref: str = "HEAD") -> str:
        return self._git("rev-parse", ref)

    def _assert_no_temporary_integration(self) -> None:
        container = self.parent / f"{self.root.name}.integration"
        self.assertFalse(container.exists() and list(container.iterdir()))
        branches = self._git(
            "for-each-ref", "--format=%(refname:short)",
            "refs/heads/assent-integration/").splitlines()
        self.assertEqual(branches, [])
        worktrees = self._git("worktree", "list", "--porcelain")
        self.assertNotIn(str(container), worktrees)

    def _assert_failed_preserves(self, before: str, source: Path,
                                 branch: str, tip: str) -> None:
        self.assertEqual(self._head(), before)
        self.assertTrue(source.is_dir())
        self.assertEqual(self._git("rev-parse", branch), tip)
        self._assert_no_temporary_integration()


class AcceptCliLineEndingTests:
    def test_accepts_non_main_target_with_evidence_and_clean_recovery(self) -> None:
        self._git("checkout", "-b", "release")
        self._write_verify(
            "from pathlib import Path\n"
            "raise SystemExit(0 if Path('result with space 空白.txt').is_file() else 1)\n")
        source, branch, tip = self._make_source()
        before = self._head()
        verified = self._cli("verify", self.folder)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

        accepted = self._cli("accept", self.folder)

        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("retain it while a dependent may still need its source evidence",
                      accepted.stdout)
        self.assertIn(f"clean {self.folder}", accepted.stdout)
        after = self._head()
        parents = self._git("rev-list", "--parents", "-n", "1", after).split()
        self.assertEqual(parents[1:], [before, tip])
        message = self._git("log", "-1", "--format=%B", "release")
        self.assertIn(f"Assent-Folder: {self.folder}", message)
        self.assertIn(f"Assent-Source-Branch: {branch}", message)
        self.assertIn(f"Assent-Source-Tip: {tip}", message)
        self.assertIn("Assent-Verified-Tree:", message)
        self.assertIn("Assent-Verifier-SHA256:", message)
        self.assertEqual(self._git("branch", "--show-current"), "release")
        self.assertTrue(source.is_dir())
        self._assert_no_temporary_integration()

        rerun = self._cli("accept", self.folder)
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertEqual(self._head(), after)
        self.assertIn("already accepted", rerun.stdout)

        cleaned = self._cli("clean", self.folder)
        self.assertEqual(cleaned.returncode, 0, cleaned.stdout + cleaned.stderr)
        self.assertFalse(source.exists())
        self.assertEqual(self._git("branch", "--list", branch), "")
        evidence_only = self._cli("accept", self.folder)
        self.assertEqual(evidence_only.returncode, 1,
                         evidence_only.stdout + evidence_only.stderr)
        self.assertEqual(self._head(), after)
        self.assertIn("no source worktree", evidence_only.stdout)


class TestAcceptCliLf(AcceptCliLineEndingTests, AcceptCliCase):
    """The fixture sets LF attributes before the baseline commit."""


class TestAcceptCliCrlf(AcceptCliLineEndingTests, AcceptCliCase):
    """The fixture sets CRLF attributes before the baseline commit."""

    autocrlf = "true"
    eol = "crlf"


class TestAcceptCliFailures(AcceptCliCase):
    def test_clean_preserves_upstream_until_dependent_is_accepted(self) -> None:
        upstream = "base"
        self._write_task(folder=upstream)
        upstream_source, upstream_branch, _ = self._make_source(
            folder=upstream, filename="base.txt")
        self._git("merge", "--no-ff", "-m", "accept base", upstream_branch)

        dependent = "dependent"
        self._write_task(folder=dependent)
        (self.assent_dir / dependent / "_folder.toml").write_text(
            f'after = ["{upstream}"]\n', encoding="utf-8", newline="\n")
        dependent_source, _dependent_branch, _ = self._make_source(
            folder=dependent, filename="dependent.txt")
        with hold_lock(self.assent_dir / upstream, upstream):
            pass
        with hold_lock(self.assent_dir / dependent, dependent):
            pass

        premature = self._cli("clean", upstream)
        self.assertEqual(premature.returncode, 0,
                         premature.stdout + premature.stderr)
        self.assertTrue(upstream_source.exists())
        self.assertIn("dependent source evidence is still required",
                      premature.stdout)
        self.assertIn("dependent dependent: current source tip", premature.stdout)

        verified = self._cli("verify", dependent)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        accepted = self._cli("accept", dependent)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertTrue(dependent_source.exists())

        upstream_clean = self._cli("clean", upstream)
        self.assertEqual(upstream_clean.returncode, 0,
                         upstream_clean.stdout + upstream_clean.stderr)
        self.assertFalse(upstream_source.exists())

        dependent_clean = self._cli("clean", dependent)
        self.assertEqual(dependent_clean.returncode, 0,
                         dependent_clean.stdout + dependent_clean.stderr)
        self.assertFalse(dependent_source.exists())

    def test_locks_and_source_states_refuse_without_mutating_target(self) -> None:
        source, branch, tip = self._make_source()
        before = self._head()
        with hold_lock(self.tasks_dir, self.folder):
            result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("already processing", result.stdout)
        self._assert_failed_preserves(before, source, branch, tip)

        with hold_integration_lock(self.assent_dir):
            result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("integration is already running", result.stdout)
        self._assert_failed_preserves(before, source, branch, tip)

        (source / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("source worktree", result.stdout)
        self._assert_failed_preserves(before, source, branch, tip)
        (source / "dirty.txt").unlink()

        self._git("checkout", "--detach", cwd=source)
        result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("detached HEAD", result.stdout)
        self._assert_failed_preserves(before, source, branch, tip)
        self._git("checkout", branch, cwd=source)

        self._git("checkout", "-b", "foreign/run", cwd=source)
        result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"not a {self.folder}/* branch", result.stdout)
        self._assert_failed_preserves(before, source, branch, tip)

    def test_target_and_source_ambiguity_refuse_fail_closed(self) -> None:
        source, branch, tip = self._make_source()
        before = self._head()
        (self.root / "dirty-target.txt").write_text("dirty\n", encoding="utf-8")
        result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("main worktree", result.stdout)
        self.assertEqual(self._head(), before)
        (self.root / "dirty-target.txt").unlink()

        self._git("checkout", "--detach")
        result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("detached HEAD", result.stdout)
        self._git("checkout", "trunk")
        self._assert_failed_preserves(before, source, branch, tip)

        self._git("worktree", "remove", "--force", str(source))
        self._git("branch", f"{self.folder}/second", "HEAD")
        result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("multiple candidate", result.stdout)
        self.assertEqual(self._head(), before)
        self._assert_no_temporary_integration()

    def test_conflict_failure_keeps_recoverable_state(self) -> None:
        source, branch, tip = self._make_source()
        (source / "README.txt").write_text("source\n", encoding="utf-8")
        self._git("add", "README.txt", cwd=source)
        self._git("commit", "-m", "source conflict", cwd=source)
        tip = self._git("rev-parse", branch)
        verified = self._cli("verify", self.folder)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        (self.root / "README.txt").write_text("target\n", encoding="utf-8")
        self._git("add", "README.txt")
        self._git("commit", "-m", "target conflict")
        before = self._head()
        result = self._cli("accept", self.folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Conflicting file(s)", result.stdout)
        self.assertIn("README.txt", result.stdout)
        self._assert_failed_preserves(before, source, branch, tip)

    def test_prerequisite_ancestry_and_forged_evidence_are_distinguished(self) -> None:
        base = "base"
        self._write_task(folder=base)
        _, base_branch, _ = self._make_source(folder=base, filename="base.txt")
        self._git("merge", "--no-ff", "-m", "manual base merge", base_branch)
        self._write_task(folder="dependent")
        (self.assent_dir / "dependent" / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8", newline="\n")
        source, _, _ = self._make_source(folder="dependent", filename="dependent.txt")
        verified = self._cli("verify", "dependent")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        accepted = self._cli("accept", "dependent")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertTrue(source.exists())

        absent = "absent"
        self._write_task(folder=absent)
        result = self._cli("accept", absent)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no source worktree", result.stdout)

        forged = (
            "forged evidence\n\n"
            f"Assent-Folder: {absent}\n"
            f"Assent-Source-Branch: {absent}/run\n"
            f"Assent-Source-Tip: {'0' * 40}\n")
        (self.root / "forged.txt").write_text("not a merge\n", encoding="utf-8")
        self._git("add", "forged.txt")
        message_path = self.parent / "forged-message.txt"
        message_path.write_text(forged, encoding="utf-8", newline="\r\n")
        self._git("commit", "-F", str(message_path))
        result = self._cli("accept", absent)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no source worktree", result.stdout)

    def test_self_fixed_folder_needs_a_real_typed_confirmation(self) -> None:
        source, branch, tip = self._make_source()
        before = self._head()
        verified = self._cli("verify", self.folder)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        cfg = load_config(self.config, self.folder)
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FIXED", (auto_fix.ReviewFinding(
                "t001", "src/main.py", "Blocking implementation issue",
                "The round repaired the task's own declared scope."),)),
            source_tree=gitops.tree_of(self.root, "HEAD"),
            task_plan_sha256=auto_fix.sha256_files(
                [self.tasks_dir / "t001_任務.e.toml"]),
            review_prompt_sha256="5" * 64,
            reviewer_adapter="codex", reviewer_model="prime",
            reviewer_effort="heavy", review_round_index=1)
        auto_fix.write_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg),
            auto_fix.with_self_fixed_unreviewed(state))

        # Closed stdin: it must decline immediately rather than hang or default
        # to publishing.
        closed = self._cli("accept", self.folder)
        self.assertEqual(closed.returncode, 1, closed.stdout + closed.stderr)
        self.assertIn("SELF-FIXED, UNREVIEWED", closed.stdout)
        self.assertIn("self-fixed round: 1 of 1 (codex/prime/heavy)", closed.stdout)
        self.assertIn("was not confirmed", closed.stdout)
        self._assert_failed_preserves(before, source, branch, tip)

        declined = self._cli("accept", self.folder, stdin="n\n")
        self.assertEqual(declined.returncode, 1, declined.stdout + declined.stderr)
        self.assertIn("was not confirmed", declined.stdout)
        self._assert_failed_preserves(before, source, branch, tip)

        confirmed = self._cli("accept", self.folder, stdin="y\n")
        self.assertEqual(confirmed.returncode, 0,
                         confirmed.stdout + confirmed.stderr)
        after = self._head()
        parents = self._git("rev-list", "--parents", "-n", "1", after).split()
        self.assertEqual(parents[1:], [before, tip])
        self.assertNotIn(
            "SELF-FIXED", self._git("log", "-1", "--format=%B", "trunk"))

    def test_last_gate_branch_head_and_cleanliness_changes_refuse(self) -> None:
        source, branch, tip = self._make_source()
        before = self._head()
        self._git("branch", "other", before)
        verified = self._cli("verify", self.folder)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        cfg = load_config(self.config, self.folder)
        real_commit_parents = gitops.commit_parents

        def run_with_action(action: str) -> tuple[int, str]:
            def mutate(candidate: Path, ref: str = "HEAD") -> tuple[str, ...]:
                parents = real_commit_parents(candidate, ref)
                if action == "switch":
                    self._git("switch", "other")
                elif action == "move":
                    (self.root / "concurrent.txt").write_text(
                        "move\n", encoding="utf-8")
                    self._git("add", "concurrent.txt")
                    self._git("commit", "-m", "concurrent move")
                else:
                    (self.root / "concurrent-dirty.txt").write_text(
                        "dirty\n", encoding="utf-8")
                return parents

            output = io.StringIO()
            with patch("assent.accept.gitops.commit_parents", side_effect=mutate):
                with contextlib.redirect_stdout(output):
                    code = accept_folder(cfg)
            return code, output.getvalue()

        code, output = run_with_action("switch")
        self.assertEqual(code, 1)
        self.assertIn("no longer on trunk", output)
        self.assertEqual(self._head("trunk"), before)
        self.assertEqual(self._head("other"), before)
        self.assertTrue(source.exists())
        self._assert_no_temporary_integration()
        self._git("switch", "trunk")

        code, output = run_with_action("dirty")
        self.assertEqual(code, 1)
        self.assertIn("became dirty", output)
        self.assertEqual(self._head(), before)
        self.assertTrue(source.exists())
        self._assert_no_temporary_integration()
        (self.root / "concurrent-dirty.txt").unlink()

        code, output = run_with_action("move")
        self.assertEqual(code, 1)
        self.assertIn("moved during accept", output)
        self.assertNotEqual(self._head(), before)
        self.assertEqual(self._git("rev-parse", f"{self._head()}^"), before)
        self.assertEqual(self._git("rev-parse", branch), tip)
        self.assertTrue(source.exists())
        self._assert_no_temporary_integration()


class TestAcceptAllCli(AcceptCliCase):
    """CLI argument-combination and end-to-end coverage for ``accept --all``.

    Ordering, verify-then-accept interleaving, fail-closed chain stop, and
    idempotent rerun are covered directly against ``accept_all`` in
    tests/test_accept_all.py; this class only proves the CLI wiring: the
    three FOLDER/--all combinations, and one real subprocess round trip.
    """

    def _cli_all(self) -> subprocess.CompletedProcess:
        args = [sys.executable, "-m", "assent", "accept", "--all",
                "--config", str(self.config)]
        return subprocess.run(args, cwd=self.root, capture_output=True,
                              encoding="utf-8", errors="replace", env=self.env)

    def test_folder_and_all_combinations_match_behavior_contract(self) -> None:
        neither = self._cli("accept")
        self.assertEqual(neither.returncode, 2, neither.stdout + neither.stderr)

        both = subprocess.run(
            [sys.executable, "-m", "assent", "accept", self.folder, "--all",
             "--config", str(self.config)],
            cwd=self.root, capture_output=True, encoding="utf-8",
            errors="replace", env=self.env)
        self.assertEqual(both.returncode, 2, both.stdout + both.stderr)

        folder_only = self._cli("accept", self.folder)
        self.assertEqual(folder_only.returncode, 1,
                         folder_only.stdout + folder_only.stderr)
        self.assertIn("no source worktree", folder_only.stdout)

        # self.folder is already DONE (setUp) but has no source branch or
        # worktree, so --all dispatches to the same folder instead of being
        # rejected by argument parsing, then skips it (no source remains) --
        # unlike a directly named FOLDER, --all never fails closed on this.
        all_only = self._cli_all()
        self.assertEqual(all_only.returncode, 0, all_only.stdout + all_only.stderr)
        self.assertIn(f"skip {self.folder} (no source branch remains", all_only.stdout)

    def test_accept_all_publishes_every_finished_folder_via_real_cli(self) -> None:
        second = "second"
        self._write_task(folder=second)
        self._make_source()
        self._make_source(folder=second)

        result = self._cli_all()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        subjects = self._git("log", "--format=%s", "--reverse").splitlines()
        accept_subjects = [s for s in subjects if s.startswith("accept(")]
        self.assertEqual(accept_subjects, [
            f"accept({second}): integrate into trunk",
            f"accept({self.folder}): integrate into trunk",
        ])
        self.assertIn("accept --all: summary", result.stdout)


if __name__ == "__main__":
    unittest.main()
