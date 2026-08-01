"""The reviewed shared-ignored-path cache: states, review, provisioning, evidence.

Every case here runs against a disposable Git repository with real worktrees and
real directory links -- a junction on Windows, a directory symlink elsewhere --
because the whole point of the feature is what happens on a filesystem: which
link is created, which is refused, which is detached, and what is never
traversed.  A test that only exercised the parser would prove nothing about the
one guarantee that matters, that no target content is ever read, moved, or
destroyed.
"""
from __future__ import annotations

import os
import subprocess
import tomllib
import unittest
from datetime import date, datetime, time
from pathlib import Path

from assent import AssentError, gitops, shared_paths
from tests.link_support import make_directory_link, safe_rmtree

import tempfile


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def settle_shared_paths(main: Path, worktree: Path, *paths: str) -> None:
    """Record one reviewed shared-path profile so verification may start.

    Most fixtures elsewhere are not about the reviewed cache at all: they
    exercise low-level mirroring separately and only have to get past the
    shared-path gate. With no ``--path`` this records the reviewed empty answer,
    which is what those repositories honestly are. A complete verification
    consumer still refuses any directory link those low-level cases create by
    hand, because REVIEWED-NONE declares none. It lives here, beside the cache
    it exercises, so the other suites import one helper instead of restating it.
    """
    main = Path(main)
    tracked = [entry for entry in gitops.tracked_paths(Path(worktree), ".")
               if not entry.startswith(".assent/")]
    shared_paths.review(main, Path(worktree), paths=paths,
                        watch=tracked[:1], none=not paths)


class SharedPathsCase(unittest.TestCase):
    """One repository, one source worktree, and real ignored directories."""

    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent shared paths 測試 "))
        self.root = self.parent / "repository with spaces"
        self.root.mkdir()
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Assent Test")
        _git(self.root, "config", "user.email", "assent@example.invalid")
        _git(self.root, "checkout", "-b", "trunk")
        (self.root / ".gitignore").write_text(
            "pkg/\nassets/\nlib/l10n/arb/\nbuild/\n", encoding="utf-8")
        (self.root / "pubspec.yaml").write_text(
            "name: demo\ndependencies:\n  shared: any\n", encoding="utf-8")
        (self.root / "README.md").write_text("initial\n", encoding="utf-8")
        (self.root / "lib" / "l10n").mkdir(parents=True)
        (self.root / "lib" / "l10n" / "app_en.arb").write_text(
            "{}\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "initial")
        self.tip = _git(self.root, "rev-parse", "HEAD")

        # The real shared inputs: ordinary ignored directories in the primary
        # worktree that Git cannot carry but the project needs.
        self._make_shared("pkg", "vendored.txt", "vendored\n")
        self._make_shared("assets", "logo.bin", "asset\n")
        self._make_shared("lib/l10n/arb", "app_en.arb", "{}\n")

        self.worktree = self.parent / "worktrees" / "plan測試"
        _git(self.root, "branch", "plan測試/run", self.tip)
        _git(self.root, "worktree", "add", str(self.worktree), "plan測試/run")
        self.addCleanup(self._cleanup)

    def _make_shared(self, relative: str, name: str, text: str) -> Path:
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
        return path

    def _cleanup(self) -> None:
        if self.root.exists():
            listing = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=self.root,
                capture_output=True, encoding="utf-8", errors="replace")
            for line in listing.stdout.splitlines():
                if line.startswith("worktree "):
                    path = Path(line.removeprefix("worktree "))
                    if path.resolve() != self.root.resolve():
                        safe_rmtree(path)
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", str(path)],
                            cwd=self.root, capture_output=True)
        safe_rmtree(self.parent)

    def _review(self, *paths: str, none: bool = False,
                watch: tuple[str, ...] = ("pubspec.yaml",)):
        return shared_paths.review(self.root, self.worktree, paths=paths,
                                   watch=watch, none=none)

    def _classify(self, worktree: Path | None = None, **kwargs):
        return shared_paths.classify(
            self.root, self.worktree if worktree is None else worktree, **kwargs)

    def _plain_repository(self, name: str = "plain") -> Path:
        """A committed repository that ignores no directory at all."""
        bare = self.parent / name
        bare.mkdir()
        _git(bare, "init")
        _git(bare, "config", "user.name", "Assent Test")
        _git(bare, "config", "user.email", "assent@example.invalid")
        (bare / "README.md").write_text("plain\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "initial")
        return bare


class TestThreeStateContract(SharedPathsCase):
    def test_unknown_becomes_reviewed_paths_and_is_cached_atomically(self):
        contract = self._classify()
        self.assertEqual(contract.state, shared_paths.UNKNOWN)
        self.assertTrue(contract.needs_review)
        self.assertIn("shared-paths review", shared_paths.review_clause(contract))
        self.assertFalse(shared_paths.manifest_path(self.root).exists())

        self._review("pkg", "lib/l10n/arb")
        manifest = shared_paths.manifest_path(self.root)
        self.assertTrue(manifest.exists())
        # Local execution memory, never project content: Git must not see it.
        status = _git(self.root, "status", "--porcelain", "--ignored=no")
        self.assertNotIn("manifest", status)
        self.assertNotIn("manifest", _git(self.root, "ls-files"))

        settled = self._classify()
        self.assertEqual(settled.state, shared_paths.REVIEWED_PATHS)
        self.assertEqual(settled.paths, ("lib/l10n/arb", "pkg"))
        self.assertEqual(shared_paths.review_clause(settled), "")
        self.assertEqual(shared_paths.closeout_refusal(settled), "")

    def test_a_reviewed_empty_answer_never_asks_again(self):
        self._review(none=True)
        contract = self._classify()
        self.assertEqual(contract.state, shared_paths.REVIEWED_NONE)
        self.assertEqual(contract.paths, ())
        self.assertEqual(shared_paths.review_clause(contract), "")
        # REVIEWED-NONE is an answer, so it provisions nothing and asks nothing.
        created, detached = shared_paths.reconcile(
            self.root, self.worktree, contract)
        self.assertEqual((created, detached), ((), ()))

    def test_settled_is_pure_when_an_undeclared_link_appears(self):
        self._review(none=True)
        contract = self._classify()
        external = self.parent / "external build"
        external.mkdir()
        (external / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        make_directory_link(self.worktree / "build", external)

        # State inspection neither inventories links nor turns an agreement
        # failure into an exception. The relying operation owns that gate.
        self.assertTrue(contract.settled)
        self.assertEqual(shared_paths.closeout_refusal(contract), "")
        with self.assertRaisesRegex(AssentError, "outside its active REVIEWED-NONE"):
            shared_paths.require_directory_link_agreement(
                self.root, self.worktree, contract)
        self.assertEqual(
            (external / "sentinel.txt").read_text(encoding="utf-8"), "keep\n")

    def test_a_repository_with_no_ignored_directory_has_nothing_to_review(self):
        bare = self._plain_repository()
        contract = shared_paths.classify(bare, bare)
        self.assertEqual(contract.state,
                         shared_paths.NO_IGNORED_DIRECTORY_CANDIDATE)
        self.assertFalse(contract.needs_review)
        self.assertTrue(contract.settled)
        self.assertEqual(shared_paths.review_clause(contract), "")
        self.assertIn("NO-IGNORED-DIRECTORY-CANDIDATE",
                      shared_paths.describe(contract))
        # A settled fast path costs no manifest at all.
        self.assertFalse(shared_paths.manifest_path(bare).exists())

    def test_a_changed_watch_file_goes_stale_with_only_the_changed_evidence(self):
        self._review("pkg")
        (self.root / "pubspec.yaml").write_text(
            "name: demo\ndependencies:\n  shared: any\n  extra: any\n",
            encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "new dependency")
        _git(self.worktree, "merge", "--ff-only", "trunk")

        contract = self._classify()
        self.assertEqual(contract.state, shared_paths.STALE)
        self.assertEqual(contract.prior_paths, ("pkg",))
        self.assertEqual(contract.evidence, ("pubspec.yaml (changed)",))
        clause = shared_paths.review_clause(contract)
        self.assertIn("Previously reviewed shared paths: pkg", clause)
        self.assertIn("pubspec.yaml (changed)", clause)
        self.assertNotIn("assets", clause)

    def test_a_watch_left_untracked_goes_stale_even_when_ignored_bytes_remain(self):
        (self.root / ".gitignore").write_text(
            "pkg/\nassets/\nlib/l10n/arb/\nbuild/\npubspec.yaml\n",
            encoding="utf-8")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "commit", "-m", "ignore the tracked watch")
        before = (self.root / "pubspec.yaml").read_bytes()
        shared_paths.review(
            self.root, self.root, paths=("pkg",), watch=("pubspec.yaml",))

        _git(self.root, "rm", "--cached", "pubspec.yaml")
        _git(self.root, "commit", "-m", "remove the watched file from Git")

        contract = shared_paths.classify(self.root, self.root)

        self.assertEqual(contract.state, shared_paths.STALE)
        self.assertIn("pubspec.yaml", " ".join(contract.evidence))
        self.assertIn("no longer Git-tracked", " ".join(contract.evidence))
        self.assertIn("assent shared-paths review", shared_paths.review_clause(contract))
        self.assertEqual((self.root / "pubspec.yaml").read_bytes(), before)

    def test_two_branch_fingerprints_reuse_their_own_profiles_without_oscillation(
            self):
        self._review("pkg")
        first = self._classify()
        (self.root / "pubspec.yaml").write_text(
            "name: demo\ndependencies:\n  shared: any\n  assets: any\n",
            encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "second dependency shape")
        _git(self.worktree, "merge", "--ff-only", "trunk")
        self._review("pkg", "assets")
        second = self._classify()
        self.assertEqual(second.paths, ("assets", "pkg"))

        # Going back to the first shape reuses the first answer, unchanged, and
        # the second profile is still there for the next switch.
        _git(self.worktree, "checkout", "-b", "plan測試/back", self.tip)
        back = self._classify()
        self.assertEqual(back.state, shared_paths.REVIEWED_PATHS)
        self.assertEqual(back.paths, ("pkg",))
        self.assertNotEqual(first.profile.fingerprint, second.profile.fingerprint)
        manifest = shared_paths.read_manifest(self.root)
        self.assertEqual(
            {profile.fingerprint for profile in manifest.profiles},
            {first.profile.fingerprint, second.profile.fingerprint})

    def test_conflicting_matching_profiles_fail_closed(self):
        self._review("pkg")
        manifest = shared_paths.read_manifest(self.root)
        clone = shared_paths.Profile(
            manifest.profiles[0].fingerprint, ("assets",),
            manifest.profiles[0].watch, dict(manifest.profiles[0].digests))
        manifest.profiles = manifest.profiles + (clone,)
        shared_paths.write_manifest(self.root, manifest)
        with self.assertRaisesRegex(AssentError, "conflicting matching profiles"):
            self._classify()

    def test_one_review_replaces_all_matching_answers_only(self):
        self._review("pkg", watch=("pubspec.yaml",))
        manifest = shared_paths.read_manifest(self.root)
        readme_digests = shared_paths.snapshot_digests(
            self.worktree, ("README.md",))
        conflicting = shared_paths.Profile(
            shared_paths.fingerprint_of(readme_digests), ("assets",),
            ("README.md",), readme_digests)
        stale_digests = dict(readme_digests)
        stale_digests["README.md"] = "0" * 64
        retained = shared_paths.Profile(
            shared_paths.fingerprint_of(stale_digests), ("lib/l10n/arb",),
            ("README.md",), stale_digests)
        manifest.profiles += (conflicting, retained)
        shared_paths.write_manifest(self.root, manifest)
        with self.assertRaisesRegex(AssentError, "conflicting matching profiles"):
            self._classify()

        resolved = self._review("pkg", watch=("pubspec.yaml",))

        self.assertEqual(resolved.paths, ("pkg",))
        profiles = shared_paths.read_manifest(self.root).profiles
        self.assertEqual(len(profiles), 2)
        self.assertIn(retained, profiles)
        self.assertEqual(sum(profile.paths == ("pkg",) for profile in profiles), 1)


class TestNoIgnoredDirectoryCandidate(SharedPathsCase):
    """The deterministic zero-token fast path, and every way it must not apply.

    The state says exactly one thing: a *successful* Git ignored-entry query of
    the primary worktree found no existing ordinary ignored directory outside
    `.git/` and `.assent/`.  It is never a claim that the project semantically
    needs no shared input, so each case below pins one boundary of that claim.
    """

    def test_only_a_successful_empty_discovery_settles_the_fast_path(self):
        bare = self._plain_repository()
        self.assertFalse(shared_paths.has_ignored_directory_candidate(bare))
        # `.git` is ignored-by-definition and `.assent` is assent's own; neither
        # is a candidate anyone could declare.
        (bare / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (bare / ".assent" / "plan01").mkdir(parents=True)
        (bare / ".assent" / "plan01" / "t001.e.toml").write_text(
            "x = 1\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "assent dir")
        self.assertFalse(shared_paths.has_ignored_directory_candidate(bare))
        self.assertEqual(shared_paths.classify(bare, bare).state,
                         shared_paths.NO_IGNORED_DIRECTORY_CANDIDATE)

    def test_an_ignored_leaf_file_is_not_a_candidate(self):
        bare = self._plain_repository()
        (bare / ".gitignore").write_text("*.g.dart\n", encoding="utf-8")
        (bare / "model.g.dart").write_text("generated\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "generated leaf")
        self.assertFalse(shared_paths.has_ignored_directory_candidate(bare))
        self.assertEqual(shared_paths.classify(bare, bare).state,
                         shared_paths.NO_IGNORED_DIRECTORY_CANDIDATE)

    def test_an_appearing_ignored_directory_turns_the_next_answer_unknown(self):
        bare = self._plain_repository()
        (bare / ".gitignore").write_text("cache/\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "ignore rule")
        # A rule alone declares nothing: the directory has to be there.
        self.assertEqual(shared_paths.classify(bare, bare).state,
                         shared_paths.NO_IGNORED_DIRECTORY_CANDIDATE)

        (bare / "cache").mkdir()
        (bare / "cache" / "entry.bin").write_text("cached\n", encoding="utf-8")
        contract = shared_paths.classify(bare, bare)
        self.assertEqual(contract.state, shared_paths.UNKNOWN)
        self.assertTrue(contract.needs_review)
        self.assertFalse(contract.settled)

    def test_an_existing_candidate_still_counts_when_it_reviews_to_none(self):
        """`paths = []` is a reviewed answer, not evidence of no candidate."""
        self.assertTrue(shared_paths.has_ignored_directory_candidate(self.root))
        self._review(none=True)
        contract = self._classify()
        self.assertEqual(contract.state, shared_paths.REVIEWED_NONE)
        # And the matching profile, not the fast path, is what answers it.
        self.assertIsNotNone(contract.profile)

    def test_a_failed_ignored_entry_query_refuses_instead_of_answering_none(self):
        """Being unable to look is not evidence that there is nothing to see."""
        broken = self.parent / "not a repository"
        broken.mkdir()
        with self.assertRaises(AssentError):
            shared_paths.has_ignored_directory_candidate(broken)
        with self.assertRaises(AssentError):
            shared_paths.classify(broken, broken)

    def test_verifier_required_evidence_is_never_settled_as_no_candidate(self):
        """A verifier that proved a directory is needed created a subject."""
        bare = self._plain_repository()
        (bare / ".gitignore").write_text("pkg/\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "ignore pkg")
        (bare / "pkg").mkdir()
        (bare / "pkg" / "vendored.txt").write_text("v\n", encoding="utf-8")
        # A directory the primary worktree can genuinely serve becomes a review
        # naming it, never a settled "nothing to declare".
        contract = shared_paths.classify(bare, bare, required_evidence=("pkg",))
        self.assertEqual(contract.state, shared_paths.UNKNOWN)
        self.assertIn("pkg is required by complete-verifier evidence",
                      " ".join(contract.evidence))
        self.assertIn("pkg", shared_paths.review_clause(contract))

    def test_required_evidence_without_a_primary_target_refuses_precisely(self):
        bare = self._plain_repository()
        with self.assertRaises(AssentError) as missing:
            shared_paths.classify(bare, bare, required_evidence=("pkg",))
        self.assertIn("does not exist in the primary worktree",
                      str(missing.exception))
        self.assertIn("shared-paths review", str(missing.exception))

        # Present but not ignored is the other half: a link there would change
        # what the worktree tracks, so it is refused with that exact problem.
        (bare / "pkg").mkdir()
        (bare / "pkg" / "kept.txt").write_text("tracked\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "tracked pkg")
        with self.assertRaises(AssentError) as tracked:
            shared_paths.classify(bare, bare, required_evidence=("pkg",))
        self.assertIn("no longer Git-ignored", str(tracked.exception))

    def test_a_source_only_directory_is_not_a_provisionable_primary_target(self):
        """Enumeration asks the primary worktree, by design and not by accident."""
        # The source worktree holds an ignored directory the primary one does
        # not; it is not a target anyone may link to, so requiring it refuses
        # with the primary-target prerequisite rather than claiming "none".
        source_only = self.worktree / "build"
        source_only.mkdir()
        (source_only / "out.bin").write_text("built\n", encoding="utf-8")
        self.assertTrue(
            shared_paths.has_ignored_directory_candidate(self.worktree))
        with self.assertRaises(AssentError) as ctx:
            self._classify(required_evidence=("build",))
        self.assertIn("build does not exist in the primary worktree",
                      str(ctx.exception))

    def test_the_fast_path_settles_every_gate_without_a_manifest(self):
        bare = self._plain_repository()
        contract = shared_paths.prepare_worktree(bare, bare)
        self.assertEqual(contract.state,
                         shared_paths.NO_IGNORED_DIRECTORY_CANDIDATE)
        self.assertEqual(shared_paths.closeout_refusal(contract), "")
        prepared = shared_paths.prepare_sources(bare, [("plan01", bare)])
        self.assertEqual(prepared[0][1].state,
                         shared_paths.NO_IGNORED_DIRECTORY_CANDIDATE)
        # A digest is available (verification may proceed) and no profile was
        # ever cached to produce it.
        self.assertTrue(shared_paths.shared_inputs_digest(bare, prepared))
        self.assertEqual(shared_paths.read_manifest(bare).profiles, ())


class TestReviewOperation(SharedPathsCase):
    def test_every_unsafe_value_is_refused_before_any_mutation(self):
        for bad in ("/etc", "..", "../outside", ".git", ".assent/plan",
                    "pkg/../..", ""):
            with self.subTest(path=bad):
                with self.assertRaises(AssentError):
                    self._review(bad)
        # A path that is not an ignored ordinary directory is refused too.
        with self.assertRaisesRegex(AssentError, "does not exist"):
            self._review("nowhere")
        with self.assertRaisesRegex(AssentError, "not an ordinary directory"):
            self._review("README.md")
        (self.root / "tracked-dir").mkdir()
        (self.root / "tracked-dir" / "kept.txt").write_text("x", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "tracked directory")
        with self.assertRaisesRegex(AssentError, "no longer Git-ignored"):
            self._review("tracked-dir")
        self.assertFalse(shared_paths.manifest_path(self.root).exists())

    def test_a_review_requires_a_decision_and_a_tracked_watch(self):
        with self.assertRaisesRegex(AssentError, "at least one --path"):
            self._review()
        with self.assertRaisesRegex(AssentError, "not both"):
            self._review("pkg", none=True)
        with self.assertRaisesRegex(AssentError, "at least one --watch"):
            self._review("pkg", watch=())
        with self.assertRaisesRegex(AssentError, "not tracked"):
            self._review("pkg", watch=("untracked.yaml",))

    def test_overlapping_declarations_are_refused(self):
        self._make_shared("pkg/inner", "x.txt", "x\n")
        with self.assertRaisesRegex(AssentError, "overlapping shared paths"):
            self._review("pkg", "pkg/inner")

    def test_a_second_concurrent_review_is_refused_not_interleaved(self):
        self._review("pkg")
        before = shared_paths.manifest_path(self.root).read_bytes()
        with shared_paths.hold_manifest_lock(self.root):
            with self.assertRaisesRegex(AssentError, "already updating"):
                self._review("assets")
        self.assertEqual(shared_paths.manifest_path(self.root).read_bytes(),
                         before)

    def test_a_malformed_manifest_refuses_rather_than_reading_as_empty(self):
        shared_paths.manifest_path(self.root).parent.mkdir(
            parents=True, exist_ok=True)
        shared_paths.manifest_path(self.root).write_text(
            "not = [toml\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "not valid TOML"):
            shared_paths.read_manifest(self.root)
        shared_paths.manifest_path(self.root).write_text(
            "version = 99\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "newer assent"):
            shared_paths.read_manifest(self.root)
        shared_paths.manifest_path(self.root).write_text(
            'version = 1\n[[shared_paths.profile]]\nfingerprint = "a"\n'
            'paths = ["../escape"]\n', encoding="utf-8")
        with self.assertRaises(AssentError):
            shared_paths.read_manifest(self.root)

    def test_unknown_top_level_tables_survive_a_rewrite(self):
        shared_paths.manifest_path(self.root).parent.mkdir(
            parents=True, exist_ok=True)
        shared_paths.manifest_path(self.root).write_text(
            'version = 1\n\n[future]\nkept = "yes"\n', encoding="utf-8")
        self._review("pkg")
        text = shared_paths.manifest_path(self.root).read_text(encoding="utf-8")
        self.assertIn("[future]", text)
        self.assertIn('kept = "yes"', text)

    def test_quoted_unknown_table_paths_and_dates_keep_parsed_meaning(self):
        path = shared_paths.manifest_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'version = 1\n\n["a.b"]\n'
            'day = 2026-08-01\nclock = 12:34:56\n'
            'stamp = 2026-08-01T12:34:56+08:00\n'
            '["a.b"."nested space"]\n"key.with.dot" = "kept"\n'
            '[["a.b"."array.key"]]\n"space key" = 1\n',
            encoding="utf-8")
        before = shared_paths.read_manifest(self.root).other
        self.assertIsInstance(before["a.b"]["day"], date)
        self.assertIsInstance(before["a.b"]["clock"], time)
        self.assertIsInstance(before["a.b"]["stamp"], datetime)

        self._review("pkg")

        after = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["a.b"], before["a.b"])


class TestProvisioning(SharedPathsCase):
    def _link(self, relative: str) -> Path:
        return self.worktree / relative

    def test_declared_paths_are_provisioned_including_a_nested_one(self):
        contract = self._review("pkg", "assets", "lib/l10n/arb")
        for relative in ("pkg", "assets", "lib/l10n/arb"):
            link = self._link(relative)
            self.assertTrue(os.path.lexists(link), relative)
            self.assertEqual(
                Path(os.path.realpath(link)), (self.root / relative).resolve())
        # Reading through the link reaches the primary worktree's own content.
        self.assertEqual(
            (self._link("pkg") / "vendored.txt").read_text(encoding="utf-8"),
            "vendored\n")
        # Nothing was copied: the source worktree tracks no new content.
        self.assertEqual(_git(self.worktree, "status", "--porcelain"), "")
        self.assertEqual(shared_paths.applied_paths(
            shared_paths.read_manifest(self.root), self.worktree),
            contract.paths)

    def test_an_existing_exact_link_is_accepted_and_a_foreign_one_refuses(self):
        self._review("pkg")
        # Re-running creates nothing new: the exact link is already there.
        contract = self._classify()
        created, detached = shared_paths.reconcile(
            self.root, self.worktree, contract)
        self.assertEqual((created, detached), ((), ()))

        elsewhere = self.parent / "external pkg"
        elsewhere.mkdir()
        (elsewhere / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        pathops_link = self._link("assets")
        make_directory_link(pathops_link, elsewhere)
        manifest = shared_paths.read_manifest(self.root)
        with self.assertRaisesRegex(AssentError, "is not a link to"):
            shared_paths.reconcile(
                self.root, self.worktree,
                shared_paths.Contract(shared_paths.REVIEWED_PATHS,
                                      shared_paths.Profile("f", ("assets",)),
                                      {}, ("assets",)),
                manifest=manifest)
        self.assertEqual(
            (elsewhere / "foreign.txt").read_text(encoding="utf-8"), "foreign\n")

    def test_an_ordinary_destination_refuses_and_is_left_untouched(self):
        occupied = self._link("pkg")
        occupied.mkdir(parents=True)
        (occupied / "mine.txt").write_text("mine\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "already exists"):
            self._review("pkg")
        self.assertEqual((occupied / "mine.txt").read_text(encoding="utf-8"),
                         "mine\n")

    def test_a_removed_declaration_detaches_only_the_recorded_link(self):
        self._review("pkg", "assets")
        # An ordinary directory and a foreign link the review never declared.
        ordinary = self.worktree / "build"
        ordinary.mkdir()
        (ordinary / "kept.txt").write_text("kept\n", encoding="utf-8")
        external = self.parent / "external other"
        external.mkdir()
        (external / "outside.txt").write_text("outside\n", encoding="utf-8")

        self._review("pkg")
        self.assertTrue(os.path.lexists(self._link("pkg")))
        self.assertFalse(os.path.lexists(self._link("assets")))
        # The detached link's target survives untouched, as does everything the
        # profile never declared.
        self.assertEqual(
            (self.root / "assets" / "logo.bin").read_text(encoding="utf-8"),
            "asset\n")
        self.assertEqual((ordinary / "kept.txt").read_text(encoding="utf-8"),
                         "kept\n")
        self.assertEqual((external / "outside.txt").read_text(encoding="utf-8"),
                         "outside\n")

    def test_a_declared_target_that_disappears_or_changes_type_fails_closed(self):
        self._review("pkg", "assets")
        safe_rmtree(self.root / "assets")
        contract = self._classify()
        self.assertEqual(contract.state, shared_paths.STALE)
        self.assertTrue(any("assets does not exist" in line
                            for line in contract.evidence))
        with self.assertRaises(AssentError):
            shared_paths.reconcile(self.root, self.worktree, contract)

    def test_running_the_review_in_the_primary_worktree_links_nothing(self):
        contract = shared_paths.review(
            self.root, self.root, paths=("pkg",), watch=("pubspec.yaml",))
        self.assertEqual(contract.state, shared_paths.REVIEWED_PATHS)
        # pkg is still the ordinary directory it was; nothing tried to link it
        # to itself.
        self.assertFalse(
            shared_paths.pathops.is_link(self.root / "pkg"))

    def test_release_detaches_recorded_links_and_forgets_the_worktree(self):
        self._review("pkg", "assets")
        detached = shared_paths.release(self.root, self.worktree)
        self.assertEqual(sorted(detached), ["assets", "pkg"])
        self.assertFalse(os.path.lexists(self._link("pkg")))
        self.assertEqual(shared_paths.applied_paths(
            shared_paths.read_manifest(self.root), self.worktree), ())
        self.assertEqual(
            (self.root / "pkg" / "vendored.txt").read_text(encoding="utf-8"),
            "vendored\n")

    def test_application_problem_reports_an_altered_link_without_repairing_it(self):
        self._review("pkg")
        self.assertEqual(
            shared_paths.application_problem(self.root, self.worktree), "")
        shared_paths.pathops.detach_directory_link(self._link("pkg"))
        problem = shared_paths.application_problem(self.root, self.worktree)
        self.assertIn("pkg", problem)
        self.assertFalse(os.path.lexists(self._link("pkg")))


class TestSharedInputEvidence(SharedPathsCase):
    def test_the_digest_covers_declared_target_content(self):
        contract = self._review("pkg")
        before = shared_paths.shared_inputs_digest(
            self.root, [("plan測試", contract)])
        self.assertEqual(
            before,
            shared_paths.shared_inputs_digest(self.root, [("plan測試", contract)]))
        (self.root / "pkg" / "vendored.txt").write_text(
            "changed\n", encoding="utf-8")
        self.assertNotEqual(
            before,
            shared_paths.shared_inputs_digest(self.root, [("plan測試", contract)]))

    def test_reviewed_none_is_its_own_identity_and_unknown_has_none(self):
        none_contract = self._review(none=True)
        empty = shared_paths.shared_inputs_digest(
            self.root, [("plan測試", none_contract)])
        nothing = shared_paths.shared_inputs_digest(self.root, [])
        self.assertNotEqual(empty, nothing)
        # "Reviewed to need nothing" and "there was nothing to review" are
        # different evidence and must not share a digest identity.
        no_candidate = shared_paths.Contract(
            shared_paths.NO_IGNORED_DIRECTORY_CANDIDATE)
        self.assertNotEqual(
            empty,
            shared_paths.shared_inputs_digest(
                self.root, [("plan測試", no_candidate)]))
        with self.assertRaisesRegex(AssentError, "not a reviewed answer"):
            shared_paths.shared_inputs_digest(
                self.root,
                [("plan測試", shared_paths.Contract(shared_paths.UNKNOWN))])

    def test_a_nested_link_is_recorded_by_identity_and_never_followed(self):
        outside = self.parent / "outside target"
        outside.mkdir()
        (outside / "secret.txt").write_text("never read\n", encoding="utf-8")
        make_directory_link(self.root / "pkg" / "escape", outside)
        contract = self._review("pkg")
        first = shared_paths.shared_inputs_digest(
            self.root, [("plan測試", contract)])
        # Content *inside* the link's target is not part of the digest: only the
        # link's own identity is, so changing the target's content changes
        # nothing and nothing outside the declared tree was ever read.
        (outside / "secret.txt").write_text("still never read\n",
                                            encoding="utf-8")
        self.assertEqual(
            first,
            shared_paths.shared_inputs_digest(self.root, [("plan測試", contract)]))


if __name__ == "__main__":              # pragma: no cover - manual runs
    unittest.main()
