"""Ignored-directory decisions, provisioning, and verification evidence.

Every case here runs against a disposable Git repository with real worktrees and
real directory links -- a junction on Windows, a directory symlink elsewhere --
because the whole point of the feature is what happens on a filesystem: which
link is created, which is refused, which is detached, and what is never
traversed.  A test that only exercised the parser would prove nothing about the
one guarantee that matters, that no target content is ever read, moved, or
destroyed.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import unittest
from pathlib import Path

from assent import AssentError, gitops, ignored_dirs
from assent.ignored_dirs_cli import ignored_dirs_declare, ignored_dirs_status
from tests.link_support import make_directory_link, safe_rmtree

import tempfile


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def settle_ignored_dirs(main: Path, worktree: Path, *required: str) -> None:
    """Record one reviewed ignored-directory profile so verification may start.

    Most fixtures elsewhere are not about the reviewed cache at all: they
    exercise low-level mirroring separately and only have to get past the
    ignored-directory gate. With no required directory this records the reviewed empty answer,
    which is what those repositories honestly are. A complete verification
    consumer still refuses any directory link those low-level cases create by
    hand, because REVIEWED-NONE declares none. It lives here, beside the cache
    it exercises, so the other suites import one helper instead of restating it.
    """
    main = Path(main)
    tracked = [entry for entry in gitops.tracked_paths(Path(worktree), ".")
               if not entry.startswith(".assent/")]
    ignored_dirs.declare(main, Path(worktree), required=required,
                        watch=tracked[:1], none_required=not required,
                        not_required=excluded_inventory(main, required))


def excluded_inventory(
        main: Path, required: tuple[str, ...] | list[str] = ()
        ) -> tuple[ignored_dirs.NonRequiredDirectory, ...]:
    """Keep unrelated fixtures explicit without repeating their inventory."""
    selected = set(required)
    return tuple(
        ignored_dirs.NonRequiredDirectory(
            relative, "not required by this focused test")
        for relative in ignored_dirs.ignored_inventory(Path(main))
        if not any(relative == root or relative.startswith(f"{root}/")
                   for root in selected))


class IgnoredDirsCase(unittest.TestCase):
    """One repository, one source worktree, and real ignored directories."""

    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent ignored dirs test "))
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

        # Required inputs that Git cannot carry into another worktree.
        self._make_required("pkg", "vendored.txt", "vendored\n")
        self._make_required("assets", "logo.bin", "asset\n")
        self._make_required("lib/l10n/arb", "app_en.arb", "{}\n")

        self.worktree = self.parent / "worktrees" / "plan測試"
        _git(self.root, "branch", "plan測試/run", self.tip)
        _git(self.root, "worktree", "add", str(self.worktree), "plan測試/run")
        self.addCleanup(self._cleanup)

    def _make_required(self, relative: str, name: str, text: str) -> Path:
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

    def _review(self, *required: str, none_required: bool = False,
                watch: tuple[str, ...] = ("pubspec.yaml",)):
        return ignored_dirs.declare(self.root, self.worktree, required=required,
                                   watch=watch, none_required=none_required,
                                   not_required=excluded_inventory(
                                       self.root, required))

    def _classify(self, worktree: Path | None = None, **kwargs):
        return ignored_dirs.classify(
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


class TestDecisionStates(IgnoredDirsCase):
    def test_workflow_declaration_refuses_the_primary_worktree(self):
        with self.assertRaisesRegex(
                AssentError, "must run in its managed source worktree"):
            ignored_dirs_declare(
                [], ["pubspec.yaml"], True,
                [["pkg", "not required"], ["assets", "not required"],
                 ["lib/l10n/arb", "not required"],
                 ["build", "not required"]], cwd=self.root)

    def test_review_accepts_a_wholly_ignored_tree_when_its_name_is_not_ignored(self):
        ignore = self.root / ".gitignore"
        ignore.write_text(
            ignore.read_text(encoding="utf-8").replace(
                "pkg/\n", "pkg/*.txt\n"),
            encoding="utf-8")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "commit", "-m", "ignore generated package contents")
        _git(self.worktree, "merge", "trunk")

        self.assertFalse(gitops.is_path_ignored(
            self.root, "pkg", directory=True))
        self.assertIn("pkg/", gitops.ignored_entries(self.root))

        contract = self._review("pkg")
        self.assertEqual(contract.state, ignored_dirs.REVIEWED_REQUIRED)
        self.assertTrue(os.path.islink(self.worktree / "pkg")
                        or os.path.isdir(self.worktree / "pkg"))
        ignored_dirs.require_directory_link_agreement(
            self.root, self.worktree, contract)



    def test_manifest_has_one_unversioned_feature_specific_format(self):
        self._review("pkg")
        manifest_path = ignored_dirs.manifest_path(self.root)
        self.assertEqual(manifest_path.name, "_ignored-dirs.toml")
        text = manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("version", text)
        self.assertIn("[[ignored_dirs.profile]]", text)


    def test_settled_is_pure_when_an_undeclared_link_appears(self):
        self._review(none_required=True)
        contract = self._classify()
        external = self.parent / "external build"
        external.mkdir()
        (external / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        make_directory_link(self.worktree / "build", external)

        # State inspection neither inventories links nor turns an agreement
        # failure into an exception. The relying operation owns that gate.
        self.assertTrue(contract.settled)
        self.assertEqual(ignored_dirs.closeout_refusal(contract), "")
        with self.assertRaisesRegex(AssentError, "outside its active REVIEWED-NONE"):
            ignored_dirs.require_directory_link_agreement(
                self.root, self.worktree, contract)
        self.assertEqual(
            (external / "sentinel.txt").read_text(encoding="utf-8"), "keep\n")

    def test_same_primary_orphan_is_reviewed_before_manifest_mutation(self):
        self._review("pkg")
        make_directory_link(
            self.worktree / "lib/l10n/arb",
            self.root / "lib/l10n/arb")
        settled = self._classify()
        self.assertEqual(settled.state, ignored_dirs.REVIEWED_REQUIRED)

        reviewable = ignored_dirs.review_decision_with_source_links(
            self.root, self.worktree, settled)
        self.assertEqual(reviewable.state, ignored_dirs.STALE)
        self.assertTrue(reviewable.needs_review)
        self.assertIn("lib/l10n/arb", "\n".join(reviewable.evidence))
        before = ignored_dirs.manifest_path(self.root).read_bytes()

        prepared = ignored_dirs.prepare_worktree(self.root, self.worktree)
        self.assertEqual(prepared.state, ignored_dirs.STALE)
        with self.assertRaisesRegex(
                AssentError, "omitted existing ignored directory link.*lib/l10n/arb"):
            ignored_dirs.validate_declaration(
                self.root, self.worktree, ("pkg",), ("pubspec.yaml",),
                excluded_inventory(self.root, ("pkg",)))
        self.assertEqual(
            ignored_dirs.manifest_path(self.root).read_bytes(), before)

        validated = ignored_dirs.validate_declaration(
            self.root, self.worktree, ("pkg", "lib/l10n/arb"),
            ("pubspec.yaml",),
            excluded_inventory(self.root, ("pkg", "lib/l10n/arb")))
        self.assertEqual(validated.required, ("lib/l10n/arb", "pkg"))
        self.assertEqual(validated.watch, ("pubspec.yaml",))




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
        self.assertEqual(second.required, ("assets", "pkg"))

        # Going back to the first shape reuses the first answer, unchanged, and
        # the second profile is still there for the next switch.
        _git(self.worktree, "checkout", "-b", "plan測試/back", self.tip)
        back = self._classify()
        self.assertEqual(back.state, ignored_dirs.REVIEWED_REQUIRED)
        self.assertEqual(back.required, ("pkg",))
        self.assertNotEqual(first.profile.fingerprint, second.profile.fingerprint)
        manifest = ignored_dirs.read_manifest(self.root)
        self.assertEqual(
            {profile.fingerprint for profile in manifest.profiles},
            {first.profile.fingerprint, second.profile.fingerprint})

    def test_conflicting_matching_profiles_fail_closed(self):
        self._review("pkg")
        manifest = ignored_dirs.read_manifest(self.root)
        base = manifest.profiles[0]
        clone = ignored_dirs.Profile(
            base.fingerprint, ("assets",), base.watch, dict(base.digests),
            base.inventory, excluded_inventory(self.root, ("assets",)))
        manifest.profiles = manifest.profiles + (clone,)
        ignored_dirs.write_manifest(self.root, manifest)
        with self.assertRaisesRegex(AssentError, "conflicting matching profiles"):
            self._classify()

    def test_one_review_replaces_all_matching_answers_only(self):
        self._review("pkg", watch=("pubspec.yaml",))
        manifest = ignored_dirs.read_manifest(self.root)
        readme_digests = ignored_dirs.snapshot_digests(
            self.worktree, ("README.md",))
        conflicting = ignored_dirs.Profile(
            ignored_dirs.fingerprint_of(readme_digests), ("assets",),
            ("README.md",), readme_digests,
            manifest.profiles[0].inventory,
            excluded_inventory(self.root, ("assets",)))
        stale_digests = dict(readme_digests)
        stale_digests["README.md"] = "0" * 64
        retained = ignored_dirs.Profile(
            ignored_dirs.fingerprint_of(stale_digests), ("lib/l10n/arb",),
            ("README.md",), stale_digests,
            manifest.profiles[0].inventory,
            excluded_inventory(self.root, ("lib/l10n/arb",)))
        manifest.profiles += (conflicting, retained)
        ignored_dirs.write_manifest(self.root, manifest)
        with self.assertRaisesRegex(AssentError, "conflicting matching profiles"):
            self._classify()

        resolved = self._review("pkg", watch=("pubspec.yaml",))

        self.assertEqual(resolved.required, ("pkg",))
        profiles = ignored_dirs.read_manifest(self.root).profiles
        self.assertEqual(len(profiles), 2)
        self.assertIn(retained, profiles)
        self.assertEqual(sum(profile.required == ("pkg",) for profile in profiles), 1)


class TestNoIgnoredDirectoryCandidate(IgnoredDirsCase):
    """The deterministic zero-token fast path, and every way it must not apply.

    The state says exactly one thing: a *successful* Git ignored-entry query of
    the primary worktree found no existing ordinary ignored directory outside
    `.git/` and `.assent/`.  It is never a claim that the project semantically
    needs no ignored-directory input, so each case pins one boundary of that claim.
    """

    def test_only_a_successful_empty_discovery_settles_the_fast_path(self):
        bare = self._plain_repository()
        self.assertFalse(ignored_dirs.has_ignored_directory_candidate(bare))
        # `.git` is ignored-by-definition and `.assent` is assent's own; neither
        # is a candidate anyone could declare.
        (bare / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (bare / ".assent" / "plan01").mkdir(parents=True)
        (bare / ".assent" / "plan01" / "t001.e.toml").write_text(
            "x = 1\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "assent dir")
        self.assertFalse(ignored_dirs.has_ignored_directory_candidate(bare))
        self.assertEqual(ignored_dirs.classify(bare, bare).state,
                         ignored_dirs.NO_IGNORED_DIRECTORY_CANDIDATE)

    def test_an_ignored_leaf_file_is_not_a_directory_candidate(self):
        bare = self._plain_repository()
        (bare / ".gitignore").write_text("*.g.dart\n", encoding="utf-8")
        (bare / "model.g.dart").write_text("generated\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "generated leaf")
        self.assertFalse(ignored_dirs.has_ignored_directory_candidate(bare))
        contract = ignored_dirs.classify(bare, bare)
        self.assertEqual(
            contract.state, ignored_dirs.NO_IGNORED_DIRECTORY_CANDIDATE)
        self.assertEqual(contract.inventory, ())

    def test_an_appearing_ignored_directory_turns_the_next_answer_unknown(self):
        bare = self._plain_repository()
        (bare / ".gitignore").write_text("cache/\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "ignore rule")
        # A rule alone declares nothing: the directory has to be there.
        self.assertEqual(ignored_dirs.classify(bare, bare).state,
                         ignored_dirs.NO_IGNORED_DIRECTORY_CANDIDATE)

        (bare / "cache").mkdir()
        (bare / "cache" / "entry.bin").write_text("cached\n", encoding="utf-8")
        contract = ignored_dirs.classify(bare, bare)
        self.assertEqual(contract.state, ignored_dirs.UNKNOWN)
        self.assertTrue(contract.needs_review)
        self.assertFalse(contract.settled)

    def test_an_existing_candidate_still_counts_when_it_reviews_to_none(self):
        """`required = []` is a reviewed answer, not evidence of no candidate."""
        self.assertTrue(ignored_dirs.has_ignored_directory_candidate(self.root))
        self._review(none_required=True)
        contract = self._classify()
        self.assertEqual(contract.state, ignored_dirs.REVIEWED_NONE)
        # And the matching profile, not the fast path, is what answers it.
        self.assertIsNotNone(contract.profile)

    def test_a_failed_ignored_entry_query_refuses_instead_of_answering_none(self):
        """Being unable to look is not evidence that there is nothing to see."""
        broken = self.parent / "not a repository"
        broken.mkdir()
        with self.assertRaises(AssentError):
            ignored_dirs.has_ignored_directory_candidate(broken)
        with self.assertRaises(AssentError):
            ignored_dirs.classify(broken, broken)


    def test_required_evidence_without_a_primary_target_refuses_precisely(self):
        bare = self._plain_repository()
        with self.assertRaises(AssentError) as missing:
            ignored_dirs.classify(bare, bare, required_evidence=("pkg",))
        self.assertIn("does not exist in the primary worktree",
                      str(missing.exception))
        self.assertIn("ignored-dirs declare", str(missing.exception))

        # Present but not ignored is the other half: a link there would change
        # what the worktree tracks, so it is refused with that exact problem.
        (bare / "pkg").mkdir()
        (bare / "pkg" / "kept.txt").write_text("tracked\n", encoding="utf-8")
        _git(bare, "add", "-A")
        _git(bare, "-c", "commit.gpgsign=false", "commit", "-m", "tracked pkg")
        with self.assertRaises(AssentError) as tracked:
            ignored_dirs.classify(bare, bare, required_evidence=("pkg",))
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
            ignored_dirs.has_ignored_directory_candidate(self.worktree))
        with self.assertRaises(AssentError) as ctx:
            self._classify(required_evidence=("build",))
        self.assertIn("build does not exist in the primary worktree",
                      str(ctx.exception))

    def test_the_fast_path_settles_every_gate_without_a_manifest(self):
        bare = self._plain_repository()
        contract = ignored_dirs.prepare_worktree(bare, bare)
        self.assertEqual(contract.state,
                         ignored_dirs.NO_IGNORED_DIRECTORY_CANDIDATE)
        self.assertEqual(ignored_dirs.closeout_refusal(contract), "")
        prepared = ignored_dirs.prepare_sources(bare, [("plan01", bare)])
        self.assertEqual(prepared[0][1].state,
                         ignored_dirs.NO_IGNORED_DIRECTORY_CANDIDATE)
        # A digest is available (verification may proceed) and no profile was
        # ever cached to produce it.
        self.assertTrue(ignored_dirs.ignored_directory_inputs_digest(bare, prepared))
        self.assertEqual(ignored_dirs.read_manifest(bare).profiles, ())


class TestDeclareOperation(IgnoredDirsCase):
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
        self.assertFalse(ignored_dirs.manifest_path(self.root).exists())

    def test_a_review_requires_a_decision_and_a_tracked_watch(self):
        with self.assertRaisesRegex(AssentError, "at least one --required"):
            self._review()
        with self.assertRaisesRegex(AssentError, "not both"):
            self._review("pkg", none_required=True)
        with self.assertRaisesRegex(AssentError, "at least one --watch"):
            self._review("pkg", watch=())
        with self.assertRaisesRegex(AssentError, "not tracked"):
            self._review("pkg", watch=("untracked.yaml",))

    def test_overlapping_declarations_are_refused(self):
        self._make_required("pkg/inner", "x.txt", "x\n")
        with self.assertRaisesRegex(AssentError, "overlapping required directories"):
            self._review("pkg", "pkg/inner")

    def test_an_omitted_inventory_entry_cannot_be_recorded(self):
        with self.assertRaisesRegex(AssentError, "unclassified: assets"):
            ignored_dirs.validate_declaration(
                self.root, self.worktree, ("pkg",), ("pubspec.yaml",))

        not_required = excluded_inventory(self.root, ("pkg",))
        contract = ignored_dirs.declare(
            self.root, self.worktree, required=("pkg",),
            watch=("pubspec.yaml",), not_required=not_required)
        reread = ignored_dirs.read_manifest(self.root).profiles[-1]
        self.assertEqual(reread.inventory, contract.profile.inventory)
        self.assertEqual(reread.not_required, not_required)

    def test_a_primary_inventory_change_is_reconsidered_before_the_next_session(self):
        self._review("pkg")
        build = self.root / "build"
        build.mkdir()
        (build / "output.bin").write_bytes(b"output")

        contract = self._classify()
        self.assertEqual(contract.state, ignored_dirs.STALE)
        self.assertIn("ignored directory added: build",
                      contract.evidence)

    def test_a_second_concurrent_review_is_refused_not_interleaved(self):
        self._review("pkg")
        before = ignored_dirs.manifest_path(self.root).read_bytes()
        with ignored_dirs.hold_manifest_lock(self.root):
            with self.assertRaisesRegex(AssentError, "already updating"):
                self._review("assets")
        self.assertEqual(ignored_dirs.manifest_path(self.root).read_bytes(),
                         before)

    def test_a_malformed_manifest_refuses_rather_than_reading_as_empty(self):
        ignored_dirs.manifest_path(self.root).parent.mkdir(
            parents=True, exist_ok=True)
        ignored_dirs.manifest_path(self.root).write_text(
            "not = [toml\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "not valid TOML"):
            ignored_dirs.read_manifest(self.root)
        ignored_dirs.manifest_path(self.root).write_text(
            "unexpected = true\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "only top-level table"):
            ignored_dirs.read_manifest(self.root)
        ignored_dirs.manifest_path(self.root).write_text(
            '[[ignored_dirs.profile]]\nfingerprint = "a"\n'
            'required = ["../escape"]\n', encoding="utf-8")
        with self.assertRaises(AssentError):
            ignored_dirs.read_manifest(self.root)

    def test_extra_top_level_tables_are_refused(self):
        ignored_dirs.manifest_path(self.root).parent.mkdir(
            parents=True, exist_ok=True)
        ignored_dirs.manifest_path(self.root).write_text(
            '[ignored_dirs]\n[extra]\nkept = "no"\n',
            encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "only top-level table"):
            ignored_dirs.read_manifest(self.root)


class TestProvisioning(IgnoredDirsCase):
    def _link(self, relative: str) -> Path:
        return self.worktree / relative

    def test_directory_symlink_is_ignored_as_a_link_object(self):
        exclude = gitops.git_common_dir(self.root) / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("# keep this user rule\n/local-only\n")
        self._review("pkg")
        link = self._link("pkg")
        if not os.path.islink(link):
            ignored_dirs.pathops.detach_directory_link(link)
            try:
                os.symlink(
                    self.root / "pkg", link, target_is_directory=True)
            except OSError as e:
                self.skipTest(f"directory symlinks are unavailable: {e}")

        self.assertTrue(gitops.is_path_ignored(self.worktree, "pkg"))
        self.assertTrue(gitops.working_tree_status(self.worktree).is_clean)
        ignored_dirs.require_directory_link_agreement(
            self.root, self.worktree, self._classify())

        ignored_dirs.release(self.root, self.worktree)
        text = exclude.read_text(encoding="utf-8")
        self.assertIn("# keep this user rule\n/local-only\n", text)
        self.assertNotIn("# --- Assent directory links", text)
        self.assertFalse(gitops.is_path_ignored(self.worktree, "pkg"))

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
        self.assertEqual(ignored_dirs.applied_required_directories(
            ignored_dirs.read_manifest(self.root), self.worktree),
            contract.required)

    def test_an_existing_exact_link_is_accepted_and_a_foreign_one_refuses(self):
        self._review("pkg")
        # Re-running creates nothing new: the exact link is already there.
        contract = self._classify()
        created, detached = ignored_dirs.reconcile(
            self.root, self.worktree, contract)
        self.assertEqual((created, detached), ((), ()))

        elsewhere = self.parent / "external pkg"
        elsewhere.mkdir()
        (elsewhere / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        pathops_link = self._link("assets")
        make_directory_link(pathops_link, elsewhere)
        manifest = ignored_dirs.read_manifest(self.root)
        with self.assertRaisesRegex(AssentError, "is not a link to"):
            ignored_dirs.reconcile(
                self.root, self.worktree,
                ignored_dirs.Decision(ignored_dirs.REVIEWED_REQUIRED,
                                      ignored_dirs.Profile("f", ("assets",)),
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

    def test_profile_switch_preserves_a_recorded_link_until_review(self):
        original = (self.worktree / "pubspec.yaml").read_text(encoding="utf-8")
        changed = original + "  extra: any\n"

        self._review("pkg", "assets")
        (self.worktree / "pubspec.yaml").write_text(changed, encoding="utf-8")
        self._review("pkg")

        (self.worktree / "pubspec.yaml").write_text(original, encoding="utf-8")
        ignored_dirs.prepare_worktree(self.root, self.worktree)
        self.assertTrue(os.path.lexists(self._link("assets")))

        (self.worktree / "pubspec.yaml").write_text(changed, encoding="utf-8")
        prepared = ignored_dirs.prepare_worktree(self.root, self.worktree)
        self.assertEqual(prepared.state, ignored_dirs.STALE)
        self.assertIn("assets", "\n".join(prepared.evidence))
        self.assertTrue(os.path.lexists(self._link("assets")))
        self.assertEqual(
            ignored_dirs.applied_required_directories(
                ignored_dirs.read_manifest(self.root), self.worktree),
            ("assets", "pkg"))
        with self.assertRaisesRegex(AssentError, "outside its active"):
            ignored_dirs.prepare_sources(
                self.root, (("plan\u6e2c\u8a66", self.worktree),))

        self._review("pkg")
        self.assertFalse(os.path.lexists(self._link("assets")))

    def test_a_declared_target_that_disappears_or_changes_type_fails_closed(self):
        self._review("pkg", "assets")
        safe_rmtree(self.root / "assets")
        contract = self._classify()
        self.assertEqual(contract.state, ignored_dirs.STALE)
        self.assertTrue(any("assets does not exist" in line
                            for line in contract.evidence))
        with self.assertRaises(AssentError):
            ignored_dirs.reconcile(self.root, self.worktree, contract)

    def test_running_the_review_in_the_primary_worktree_links_nothing(self):
        contract = ignored_dirs.declare(
            self.root, self.root, required=("pkg",), watch=("pubspec.yaml",),
            not_required=excluded_inventory(self.root, ("pkg",)))
        self.assertEqual(contract.state, ignored_dirs.REVIEWED_REQUIRED)
        # pkg is still the ordinary directory it was; nothing tried to link it
        # to itself.
        self.assertFalse(
            ignored_dirs.pathops.is_link(self.root / "pkg"))

    def test_status_distinguishes_primary_targets_from_source_links(self):
        self._review("pkg", "assets")
        manifest = ignored_dirs.manifest_path(self.root)
        before = manifest.read_bytes()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = ignored_dirs_status(self.worktree)
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn(f"Current worktree: {self.worktree.resolve()}", text)
        self.assertIn(f"Primary worktree: {self.root.resolve()}", text)
        self.assertIn("State: REVIEWED-REQUIRED", text)
        self.assertIn("Required directories: assets, pkg", text)
        self.assertIn("Watch files: pubspec.yaml", text)
        self.assertIn("Links: OK", text)
        self.assertEqual(manifest.read_bytes(), before)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = ignored_dirs_status(self.root)
        self.assertEqual(code, 0)
        self.assertIn(
            "Links: not applicable (the primary worktree contains the targets)",
            output.getvalue())
        self.assertEqual(manifest.read_bytes(), before)

    def test_status_reports_broken_settled_links_without_repairing_them(self):
        self._review("pkg")
        ignored_dirs.release(self.root, self.worktree)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = ignored_dirs_status(self.worktree)
        self.assertEqual(code, 1)
        self.assertIn("Links: INVALID", output.getvalue())
        self.assertFalse(os.path.lexists(self.worktree / "pkg"))
        self.assertTrue((self.root / "pkg" / "vendored.txt").exists())

    def test_release_detaches_recorded_links_and_forgets_the_worktree(self):
        self._review("pkg", "assets")
        detached = ignored_dirs.release(self.root, self.worktree)
        self.assertEqual(sorted(detached), ["assets", "pkg"])
        self.assertFalse(os.path.lexists(self._link("pkg")))
        self.assertEqual(ignored_dirs.applied_required_directories(
            ignored_dirs.read_manifest(self.root), self.worktree), ())
        self.assertEqual(
            (self.root / "pkg" / "vendored.txt").read_text(encoding="utf-8"),
            "vendored\n")

    def test_application_problem_reports_an_altered_link_without_repairing_it(self):
        self._review("pkg")
        self.assertEqual(
            ignored_dirs.application_problem(self.root, self.worktree), "")
        ignored_dirs.pathops.detach_directory_link(self._link("pkg"))
        problem = ignored_dirs.application_problem(self.root, self.worktree)
        self.assertIn("pkg", problem)
        self.assertFalse(os.path.lexists(self._link("pkg")))


class TestIgnoredDirectoryInputEvidence(IgnoredDirsCase):
    def test_the_digest_covers_declared_target_content(self):
        contract = self._review("pkg")
        before = ignored_dirs.ignored_directory_inputs_digest(
            self.root, [("plan測試", contract)])
        self.assertEqual(
            before,
            ignored_dirs.ignored_directory_inputs_digest(self.root, [("plan測試", contract)]))
        (self.root / "pkg" / "vendored.txt").write_text(
            "changed\n", encoding="utf-8")
        self.assertNotEqual(
            before,
            ignored_dirs.ignored_directory_inputs_digest(self.root, [("plan測試", contract)]))

    def test_reviewed_none_is_its_own_identity_and_unknown_has_none(self):
        none_contract = self._review(none_required=True)
        empty = ignored_dirs.ignored_directory_inputs_digest(
            self.root, [("plan測試", none_contract)])
        nothing = ignored_dirs.ignored_directory_inputs_digest(self.root, [])
        self.assertNotEqual(empty, nothing)
        # "Reviewed to need nothing" and "there was nothing to review" are
        # different evidence and must not share a digest identity.
        no_candidate = ignored_dirs.Decision(
            ignored_dirs.NO_IGNORED_DIRECTORY_CANDIDATE)
        self.assertNotEqual(
            empty,
            ignored_dirs.ignored_directory_inputs_digest(
                self.root, [("plan測試", no_candidate)]))
        with self.assertRaisesRegex(AssentError, "not a reviewed answer"):
            ignored_dirs.ignored_directory_inputs_digest(
                self.root,
                [("plan測試", ignored_dirs.Decision(ignored_dirs.UNKNOWN))])

    def test_a_nested_link_is_recorded_by_identity_and_never_followed(self):
        outside = self.parent / "outside target"
        outside.mkdir()
        (outside / "secret.txt").write_text("never read\n", encoding="utf-8")
        make_directory_link(self.root / "pkg" / "escape", outside)
        contract = self._review("pkg")
        first = ignored_dirs.ignored_directory_inputs_digest(
            self.root, [("plan測試", contract)])
        # Content *inside* the link's target is not part of the digest: only the
        # link's own identity is, so changing the target's content changes
        # nothing and nothing outside the declared tree was ever read.
        (outside / "secret.txt").write_text("still never read\n",
                                            encoding="utf-8")
        self.assertEqual(
            first,
            ignored_dirs.ignored_directory_inputs_digest(self.root, [("plan測試", contract)]))


class TestDeclarationClause(unittest.TestCase):
    def test_unknown_contract_gets_one_validated_cli_instruction(self):
        contract = ignored_dirs.Decision(
            ignored_dirs.UNKNOWN, needs_review=True,
            inventory=("assets", "build"))

        clause = ignored_dirs.declaration_clause(contract)

        self.assertIn("assent ignored-dirs declare", clause)
        self.assertIn("--not-required DIR REASON", clause)
        self.assertIn("  - assets", clause)
        self.assertNotIn("assent.auto_fix_review", clause)


if __name__ == "__main__":              # pragma: no cover - manual runs
    unittest.main()
