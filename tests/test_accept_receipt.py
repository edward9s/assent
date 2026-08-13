"""Black-box tests for receipt-gated fast local acceptance."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from assent import AssentError, gitops
from assent.accept import accept_folder
from assent.config import load_config

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AcceptReceiptCase(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent receipt accept "))
        self.root = self.parent / "repository with spaces"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.parent, True)
        self.env = dict(os.environ)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["PYTHONPATH"] = os.pathsep.join(
            (str(_PROJECT_ROOT), self.env.get("PYTHONPATH", "")))
        self._git("init")
        self._git("config", "user.name", "Assent Receipt Test")
        self._git("config", "user.email", "receipt@example.invalid")
        self._git("checkout", "-b", "trunk")
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "baseline")

        self.folder = "plan01"
        self.assent_dir = self.root / ".assent"
        self.tasks_dir = self.assent_dir / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config = self.assent_dir / "assent.toml"
        self.config.write_text("", encoding="utf-8")
        self.counter = self.parent / "verifier-count.txt"
        self._write_verifier(True)
        self._write_task(self.folder)
        self.source, self.branch = self._make_source(self.folder)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.root, env=self.env,
            capture_output=True, encoding="utf-8", errors="replace")
        if result.returncode:
            self.fail("git failed: " + " ".join(args) + "\n" +
                      result.stdout + result.stderr)
        return result.stdout.strip()

    def _cli(self, command: str, folder: str | None = None
             ) -> subprocess.CompletedProcess[str]:
        args = [sys.executable, "-m", "assent", command]
        if folder is not None:
            args.append(folder)
        args.extend(("--config", str(self.config)))
        return subprocess.run(
            args, cwd=self.root, env=self.env, capture_output=True,
            encoding="utf-8", errors="replace")

    def _write_task(self, folder: str) -> Path:
        tasks = self.assent_dir / folder
        tasks.mkdir(parents=True, exist_ok=True)
        path = tasks / "t001_task.e.toml"
        path.write_text(
            'title = "Task"\n'
            'deps = []\n'
            'model = "core"\n'
            'status = "DONE"\n'
            'scope = ["src/"]\n'
            'verify = "python --version"\n'
            'goal = "Finish."\n'
            'acceptance = "Pass."\n',
            encoding="utf-8")
        return path

    def _write_verifier(self, passes: bool) -> None:
        counter = json.dumps(str(self.counter))
        (self.assent_dir / "verify.py").write_text(
            "from pathlib import Path\n"
            f"counter = Path({counter})\n"
            "value = int(counter.read_text() if counter.exists() else '0')\n"
            "counter.write_text(str(value + 1), encoding='utf-8')\n"
            f"raise SystemExit({0 if passes else 7})\n",
            encoding="utf-8")

    def _make_source(self, folder: str, filename: str | None = None,
                     base_ref: str | None = None
                     ) -> tuple[Path, str]:
        branch = f"{folder}/run"
        path = self.parent / f"{self.root.name}.worktrees" / folder
        args = ["worktree", "add", "-b", branch, str(path)]
        if base_ref is not None:
            args.append(base_ref)
        self._git(*args)
        (path / (filename or f"{folder}.txt")).write_text(
            f"{folder}\n", encoding="utf-8")
        self._git("add", "-A", cwd=path)
        self._git("commit", "-m", f"finish {folder}", cwd=path)
        return path, branch

    def _head(self, ref: str = "HEAD") -> str:
        return self._git("rev-parse", ref)

    def _verify_passes(self, folder: str | None = None) -> None:
        result = self._cli("verify", folder or self.folder)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _assert_refused_unchanged(self, before: str,
                                  result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self._head(), before)
        self.assertIn(f"assent verify {self.folder}", result.stdout)


class TestReceiptGate(AcceptReceiptCase):
    def test_exact_receipt_publishes_two_parent_merge_without_rerunning_verifier(self) -> None:
        before = self._head()
        tip = self._head(self.branch)
        self._verify_passes()
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")

        accepted = self._cli("accept", self.folder)

        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")
        after = self._head()
        parents = self._git("rev-list", "--parents", "-n", "1", after).split()[1:]
        self.assertEqual(parents, [before, tip])
        message = self._git("show", "-s", "--format=%B", after)
        self.assertIn(f"Assent-Folder: {self.folder}", message)
        self.assertIn(f"Assent-Source-Branch: {self.branch}", message)
        self.assertIn(f"Assent-Source-Tip: {tip}", message)
        self.assertIn("Assent-Verified-Tree:", message)
        self.assertIn("Assent-Verifier-SHA256:", message)
        self.assertTrue(self.source.is_dir())

        repeated = self._cli("accept", self.folder)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self._head(), after)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")
        self.assertIn("already accepted", repeated.stdout)

    def test_missing_failed_and_malformed_receipts_fail_closed(self) -> None:
        before = self._head()
        missing = self._cli("accept", self.folder)
        self._assert_refused_unchanged(before, missing)
        self.assertIn("receipt not found", missing.stdout.lower())

        receipt = self.tasks_dir / "_verification.toml"
        receipt.write_text("not valid = [\n", encoding="utf-8")
        malformed = self._cli("accept", self.folder)
        self._assert_refused_unchanged(before, malformed)
        self.assertIn("not valid TOML", malformed.stdout)

        receipt.unlink()
        self._write_verifier(False)
        failed_verify = self._cli("verify", self.folder)
        self.assertEqual(failed_verify.returncode, 1)
        self.assertNotIn(
            "REVIEW UNRESOLVED, HUMAN DECISION", failed_verify.stdout)
        failed = self._cli("accept", self.folder)
        self._assert_refused_unchanged(before, failed)
        self.assertIn("status is FAILED", failed.stdout)

    def test_source_verifier_and_candidate_changes_are_stale(self) -> None:
        self._verify_passes()
        before = self._head()
        (self.source / "later.txt").write_text("later\n", encoding="utf-8")
        self._git("add", "later.txt", cwd=self.source)
        self._git("commit", "-m", "advance source", cwd=self.source)
        source_changed = self._cli("accept", self.folder)
        self._assert_refused_unchanged(before, source_changed)
        self.assertIn("source tip changed", source_changed.stdout)

        self._verify_passes()
        self._write_verifier(True)
        with (self.assent_dir / "verify.py").open("a", encoding="utf-8") as handle:
            handle.write("# changed\n")
        verifier_changed = self._cli("accept", self.folder)
        self._assert_refused_unchanged(before, verifier_changed)
        self.assertIn("verification script changed", verifier_changed.stdout)

        self._verify_passes()
        (self.root / "target-only.txt").write_text("different\n", encoding="utf-8")
        self._git("add", "target-only.txt")
        self._git("commit", "-m", "change target tree")
        moved = self._head()
        candidate_changed = self._cli("accept", self.folder)
        self._assert_refused_unchanged(moved, candidate_changed)
        self.assertIn("candidate tree differs", candidate_changed.stdout)

    def test_tree_identical_target_commit_does_not_stale_receipt(self) -> None:
        self._verify_passes()
        before_metadata = self._head()
        self._git("commit", "--allow-empty", "-m", "target metadata only")
        target_before = self._head()
        self.assertNotEqual(before_metadata, target_before)

        accepted = self._cli("accept", self.folder)

        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        parents = self._git("rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        self.assertEqual(parents, [target_before, self._head(self.branch)])

    def test_cleaned_source_is_not_reauthorized_from_history(self) -> None:
        self._verify_passes()
        accepted = self._cli("accept", self.folder)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        after = self._head()
        self._git("worktree", "remove", str(self.source))
        self._git("branch", "-D", self.branch)

        repeated = self._cli("accept", self.folder)

        self.assertEqual(repeated.returncode, 1)
        self.assertEqual(self._head(), after)
        self.assertIn("no source worktree", repeated.stdout)
        self.assertIn("does not infer authorization", repeated.stdout)

    def test_cleanup_diagnostic_does_not_turn_published_accept_into_failure(self) -> None:
        self._verify_passes()
        before = self._head()
        cfg = load_config(self.config, self.folder)
        original_cleanup = gitops._cleanup_temporary_worktree

        def cleanup_then_report(*args, **kwargs) -> None:
            original_cleanup(*args, **kwargs)
            raise AssentError("simulated cleanup diagnostic")

        output = io.StringIO()
        with patch.object(
                gitops, "_cleanup_temporary_worktree",
                side_effect=cleanup_then_report):
            with contextlib.redirect_stdout(output):
                code = accept_folder(cfg)

        self.assertEqual(code, 0, output.getvalue())
        self.assertNotEqual(self._head(), before)
        self.assertIn("warning: simulated cleanup diagnostic", output.getvalue())
        self.assertIn("temporary ref:", output.getvalue())
        self.assertIn("temporary path:", output.getvalue())


class TestDependencyGate(AcceptReceiptCase):
    def test_current_upstream_tip_must_be_in_target_and_unrelated_bad_folder_is_ignored(
            self) -> None:
        base = "base"
        self._write_task(base)
        base_source, base_branch = self._make_source(base)
        self._git("merge", "--no-ff", "-m", "accept base manually", base_branch)
        self._git("worktree", "remove", str(self.source))
        self._git("branch", "-D", self.branch)
        self.source, self.branch = self._make_source(self.folder)
        (self.tasks_dir / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")

        bad = self.assent_dir / "unrelated"
        bad.mkdir()
        (bad / "t001_bad.e.toml").write_text("not valid = [\n", encoding="utf-8")
        self._verify_passes()
        accepted = self._cli("accept", self.folder)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

        (base_source / "base-later.txt").write_text("later\n", encoding="utf-8")
        self._git("add", "base-later.txt", cwd=base_source)
        self._git("commit", "-m", "advance base", cwd=base_source)

        dependent = "dependent"
        self._write_task(dependent)
        dependent_dir = self.assent_dir / dependent
        (dependent_dir / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")
        self._make_source(dependent)
        refused = self._cli("accept", dependent)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("current tip", refused.stdout)
        self.assertIn("not in target", refused.stdout)


class TestStackedReceiptLifecycle(AcceptReceiptCase):
    upstream = "upstream"
    downstream = "downstream"

    def _make_stack(self) -> tuple[Path, str, Path, str]:
        shared = self.root / "shared.txt"
        shared.write_text(
            "upstream section\nneutral section\ndownstream section\n",
            encoding="utf-8")
        self._git("add", "shared.txt")
        self._git("commit", "-m", "add shared baseline")

        self._write_task(self.upstream)
        upstream_source, upstream_branch = self._make_source(self.upstream)
        (upstream_source / "shared.txt").write_text(
            "upstream result\nneutral section\ndownstream section\n",
            encoding="utf-8")
        self._git("add", "shared.txt", cwd=upstream_source)
        self._git("commit", "-m", "finish upstream shared section",
                  cwd=upstream_source)

        self._write_task(self.downstream)
        downstream_tasks = self.assent_dir / self.downstream
        (downstream_tasks / "_folder.toml").write_text(
            f'after = ["{self.upstream}"]\n'
            f'base = "{self.upstream}"\n', encoding="utf-8")
        downstream_source, downstream_branch = self._make_source(
            self.downstream, base_ref=upstream_branch)
        (downstream_source / "shared.txt").write_text(
            "upstream result\nneutral section\ndownstream result\n",
            encoding="utf-8")
        self._git("add", "shared.txt", cwd=downstream_source)
        self._git("commit", "-m", "finish downstream shared section",
                  cwd=downstream_source)
        return (upstream_source, upstream_branch,
                downstream_source, downstream_branch)

    def _receipt(self) -> tuple[bytes, dict[str, object]]:
        path = self.assent_dir / self.downstream / "_verification.toml"
        raw = path.read_bytes()
        return raw, tomllib.loads(raw.decode("utf-8"))

    def test_combined_receipt_precedes_upstream_accept_and_is_reused_in_order(
            self) -> None:
        (_upstream_source, upstream_branch,
         downstream_source, downstream_branch) = self._make_stack()
        target_before = self._head()

        self._verify_passes(self.downstream)
        receipt_raw, receipt = self._receipt()
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")
        self.assertEqual(receipt["status"], "PASSED")
        self.assertEqual(receipt["source_tip"], self._head(downstream_branch))
        self.assertNotEqual(receipt["target_tip"], self._head(upstream_branch))

        refused = self._cli("accept", self.downstream)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("not in target", refused.stdout)
        self.assertEqual(self._head(), target_before)
        self.assertEqual(self._receipt()[0], receipt_raw)
        self.assertTrue(downstream_source.is_dir())

        self._verify_passes(self.upstream)
        accepted_upstream = self._cli("accept", self.upstream)
        self.assertEqual(
            accepted_upstream.returncode, 0,
            accepted_upstream.stdout + accepted_upstream.stderr)
        target_with_upstream = self._head()
        self.assertTrue(gitops.is_ancestor(
            self.root, self._head(upstream_branch), target_with_upstream))

        accepted_downstream = self._cli("accept", self.downstream)
        self.assertEqual(
            accepted_downstream.returncode, 0,
            accepted_downstream.stdout + accepted_downstream.stderr)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "2")
        self.assertEqual(
            self._git("rev-parse", "HEAD^{tree}"), receipt["integration_tree"])
        self.assertEqual(
            (self.root / "shared.txt").read_text(encoding="utf-8"),
            "upstream result\nneutral section\ndownstream result\n")

    def test_upstream_tip_drift_refuses_refresh_and_preserves_old_receipt(self) -> None:
        upstream_source, _upstream_branch, downstream_source, _branch = (
            self._make_stack())
        self._verify_passes(self.downstream)
        receipt_raw, _receipt = self._receipt()
        target_before = self._head()

        (upstream_source / "later.txt").write_text("later\n", encoding="utf-8")
        self._git("add", "later.txt", cwd=upstream_source)
        self._git("commit", "-m", "advance upstream after downstream verification",
                  cwd=upstream_source)

        refreshed = self._cli("verify", self.downstream)
        self.assertEqual(refreshed.returncode, 1, refreshed.stdout + refreshed.stderr)
        self.assertIn("stale stack", refreshed.stdout)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")
        self.assertEqual(self._receipt()[0], receipt_raw)
        self.assertEqual(self._head(), target_before)
        self.assertTrue(downstream_source.is_dir())

        accepted = self._cli("accept", self.downstream)
        self.assertEqual(accepted.returncode, 1, accepted.stdout + accepted.stderr)
        self.assertIn("not in target", accepted.stdout)
        self.assertEqual(self._head(), target_before)
        self.assertEqual(self._receipt()[0], receipt_raw)

    def test_same_line_target_conflict_lists_file_and_preserves_everything(self) -> None:
        (_upstream_source, _upstream_branch,
         downstream_source, _downstream_branch) = self._make_stack()
        self._verify_passes(self.downstream)
        receipt_raw, _receipt = self._receipt()
        self._verify_passes(self.upstream)
        accepted_upstream = self._cli("accept", self.upstream)
        self.assertEqual(accepted_upstream.returncode, 0)

        (self.root / "shared.txt").write_text(
            "upstream result\nneutral section\ntarget concurrent result\n",
            encoding="utf-8")
        self._git("add", "shared.txt")
        self._git("commit", "-m", "concurrent target edits downstream line")
        target_before = self._head()

        accepted = self._cli("accept", self.downstream)
        self.assertEqual(accepted.returncode, 1, accepted.stdout + accepted.stderr)
        self.assertIn("Conflicting file(s)", accepted.stdout)
        self.assertIn("shared.txt", accepted.stdout)
        self.assertEqual(self._head(), target_before)
        self.assertEqual(self._receipt()[0], receipt_raw)
        self.assertTrue(downstream_source.is_dir())
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "2")


if __name__ == "__main__":
    unittest.main()
