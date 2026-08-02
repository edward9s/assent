"""contracts tests: the global ~/.assent/instructions.md and format.md gate.

Every case redirects the user home with ASSENT_HOME, so the developer's real
~/.assent is never read or written.  ``install_global_contracts`` is the fixture
the CLI, engine and inspection test modules share to put a temporary user home
with current contracts in place.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError, contracts
from assent.user_home import ASSENT_HOME_ENV

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def install_global_contracts(case: unittest.TestCase) -> Path:
    """Point ASSENT_HOME at a temporary user home holding the current contracts.

    Returns that directory so a test can then delete or edit one contract to
    produce the missing/stale cases.  Cleanup is registered on the test case.
    """
    home = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, home, ignore_errors=True)
    environment = mock.patch.dict(os.environ, {ASSENT_HOME_ENV: str(home)})
    environment.start()
    case.addCleanup(environment.stop)
    for name in contracts.CONTRACT_NAMES:
        (home / name).write_text(contracts.installed_contract_text(name),
                                 encoding="utf-8", newline="\n")
    return home


class GlobalContractsMixin:
    """Give a test case current global contracts under a temporary user home.

    ``run`` and ``check`` now fail closed without them, so every case that drives
    either one mixes this in ahead of its own base class.  It deliberately stays a
    plain mixin -- it must compose with the git-backed engine fixtures without this
    module having to import them.
    """

    def setUp(self):
        super().setUp()
        self.user_home = install_global_contracts(self)


class TestContractPaths(unittest.TestCase):
    def test_paths_resolve_under_the_redirected_user_home(self):
        home = install_global_contracts(self)
        self.assertEqual(contracts.contract_dir(), home)
        self.assertEqual(contracts.instructions_path(),
                         home / "instructions.md")
        self.assertEqual(contracts.contract_path("format.md"),
                         home / "format.md")

    def test_an_unknown_contract_name_is_refused(self):
        install_global_contracts(self)
        with self.assertRaises(AssentError) as ctx:
            contracts.contract_path("AGENTS.md")
        self.assertIn("unknown global contract", str(ctx.exception))

    def test_the_installed_text_comes_from_the_packaged_templates(self):
        install_global_contracts(self)
        packaged = Path(contracts.__file__).resolve().parent / "templates"
        for name in contracts.CONTRACT_NAMES:
            with self.subTest(contract=name):
                self.assertEqual(
                    contracts.installed_contract_text(name),
                    (packaged / name).read_text(encoding="utf-8"))


class TestContractContent(unittest.TestCase):
    """Durable rules a reader must be able to find in the shipped contract."""

    def test_quota_examples_describe_rotation_action(self):
        install_global_contracts(self)
        format_text = contracts.installed_contract_text("format.md")
        self.assertIn(
            'summary = "quota exhausted; progress kept, switching to codex '
            'immediately"', format_text)
        self.assertNotIn("waiting for reset to continue", format_text)

        english = (_PROJECT_ROOT / "docs/CONFIGURATION.md").read_text(
            encoding="utf-8")
        for phrase in (
                "Quota exhaustion records a WIP checkpoint",
                "rotates immediately to the next configured adapter",
                "waits only after all are exhausted"):
            with self.subTest(document="docs/CONFIGURATION.md", phrase=phrase):
                self.assertIn(phrase, english)
        chinese = (_PROJECT_ROOT / "docs/zh-TW/CONFIGURATION.md").read_text(
            encoding="utf-8")
        for phrase in ("quota 中斷會記錄 WIP", "adapter list 會立即切到下一個",
                       "全部用盡才等待"):
            with self.subTest(document="docs/zh-TW/CONFIGURATION.md",
                              phrase=phrase):
                self.assertIn(phrase, chinese)

    def test_checkpoint_resume_control_is_documented_consistently(self):
        install_global_contracts(self)
        record = '{"type":"assent.checkpoint_resume"}'
        format_text = contracts.installed_contract_text("format.md")
        english = (_PROJECT_ROOT / "docs/CONFIGURATION.md").read_text(
            encoding="utf-8")
        for name, text in (("format.md", format_text),
                           ("docs/CONFIGURATION.md", english)):
            with self.subTest(document=name):
                self.assertIn(record, text)
                self.assertIn("checkpoint_resume", text)
                compact = " ".join(text.split())
                self.assertIn(
                    "A wrapper may replace a provider quota result with it only "
                    "after arranging an immediate continuation; if it forwards "
                    "provider quota, Assent performs the normal wait or rotation. "
                    "When quota evidence and this record are both present, the "
                    "ordinary quota path wins.",
                    compact)

        chinese = (_PROJECT_ROOT / "docs/zh-TW/CONFIGURATION.md").read_text(
            encoding="utf-8")
        compact = "".join(chinese.split())
        self.assertIn(record, chinese)
        self.assertIn("checkpoint_resume", chinese)
        for phrase in (
                "先安排立即續跑，才可把providerquotaresult換成這個record",
                "若轉送providerquota，Assent仍負責普通wait或rotation",
                "若quotaevidence與這個record同時存在，普通quotapath優先"):
            with self.subTest(document="docs/zh-TW/CONFIGURATION.md",
                              phrase=phrase):
                self.assertIn(phrase, compact)

    def test_wip_progress_and_terminal_auto_boundary_are_documented(self):
        install_global_contracts(self)
        format_text = contracts.installed_contract_text("format.md")
        english = (_PROJECT_ROOT / "docs/WORKFLOW.md").read_text(
            encoding="utf-8")
        for name, text in (("format.md", format_text),
                           ("docs/WORKFLOW.md", english)):
            compact = " ".join(text.split())
            for phrase in (
                    "progress-bearing WIP checkpoint",
                    "terminal auto",
                    "clean legacy `DONE` task",
                    "does not retroactively synthesize"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        chinese = (_PROJECT_ROOT / "docs/zh-TW/WORKFLOW.md").read_text(
            encoding="utf-8")
        for phrase in ("具備進度的 `WIP`", "終端 auto checkpoint",
                       "乾淨 legacy `DONE` task", "不會改寫或捏造歷史證據"):
            with self.subTest(document="docs/zh-TW/WORKFLOW.md", phrase=phrase):
                self.assertIn(phrase, chinese)

    def test_command_guides_state_the_per_subcommand_config_contract(self):
        commands = (
            "run", "status", "check", "report", "verify", "clean", "archive",
            "accept", "reconcile", "reject", "rework",
        )
        english = (_PROJECT_ROOT / "docs/COMMANDS.md").read_text(
            encoding="utf-8")
        chinese = (_PROJECT_ROOT / "docs/zh-TW/COMMANDS.md").read_text(
            encoding="utf-8")
        for name, text in (("docs/COMMANDS.md", english),
                           ("docs/zh-TW/COMMANDS.md", chinese)):
            compact = " ".join(text.split())
            with self.subTest(document=name):
                clause = (
                    "`run`, `status`, `check`, `report`, `verify`, `clean`, "
                    "`archive`, `accept`, `reconcile`, `reject`, and `rework` "
                    "accept `--config PATH`"
                    if name == "docs/COMMANDS.md" else
                    "`run`、`status`、`check`、`report`、`verify`、`clean`、"
                    "`archive`、`accept`、`reconcile`、`reject`、`rework` "
                    "支援 `--config PATH`")
                self.assertIn(
                    clause if name == "docs/COMMANDS.md"
                    else "".join(clause.split()),
                    compact if name == "docs/COMMANDS.md"
                    else "".join(compact.split()))
                for command in commands:
                    self.assertIn(f"`{command}`", compact)
                self.assertIn(
                    "per-subcommand" if name == "docs/COMMANDS.md"
                    else "每個 subcommand 自己的 option", compact)
                self.assertIn("top-level global option", compact)
                self.assertIn(
                    "have their own project-location contracts"
                    if name == "docs/COMMANDS.md"
                    else "各有自己的 project-location contract", compact)

    def test_operations_guides_separate_recovery_and_persistent_evidence(self):
        english = (_PROJECT_ROOT / "docs/OPERATIONS.md").read_text(
            encoding="utf-8")
        chinese = (_PROJECT_ROOT / "docs/zh-TW/OPERATIONS.md").read_text(
            encoding="utf-8")
        english_compact = " ".join(english.split())
        for phrase in (
                "every uncommitted change is provably attributable",
                "marks that task `WIP`",
                "gathers the edits into a `WIP` checkpoint",
                "without opening an AI session",
                "keeps the dirty worktree for human inspection",
                "journal carries structured events plus bounded summaries and adapter classifications",
                "not the full raw adapter stream",
                "rendered terminal session output",
                "without a parent scheduler prefix"):
            with self.subTest(document="docs/OPERATIONS.md", phrase=phrase):
                self.assertIn(phrase, english_compact)
        chinese_compact = " ".join(chinese.split())
        for phrase in (
                "每個未提交變更都能證明屬於要恢復的 task",
                "將 task 標成 `WIP`",
                "收進 `WIP` checkpoint",
                "不開 AI session",
                "保留 dirty worktree 供人類檢查",
                "journal 保存 structured events",
                "不保存完整 raw adapter stream",
                "rendered terminal session output",
                "沒有 parent scheduler prefix"):
            with self.subTest(document="docs/zh-TW/OPERATIONS.md", phrase=phrase):
                self.assertIn(phrase, chinese_compact)
        self.assertNotIn("journal preserves the adapter result, raw output",
                         english_compact)
        self.assertNotIn("journal 會保留 adapter result、raw output",
                         chinese_compact)

    def test_folder_verification_report_refresh_is_documented(self):
        install_global_contracts(self)
        english = {
            "format.md": contracts.installed_contract_text("format.md"),
            "docs/VERIFICATION.md": (_PROJECT_ROOT / "docs/VERIFICATION.md").read_text(
                encoding="utf-8"),
        }
        for name, text in english.items():
            compact = " ".join(text.split())
            phrases = (
                ("refreshes `_report.md` after", "best-effort")
                if name == "format.md" else
                ("refreshes that folder's `_report.md` exactly once",
                 "all verification locks are released",
                 "best-effort report refresh"))
            for phrase in phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        chinese = (_PROJECT_ROOT / "docs/zh-TW/VERIFICATION.md").read_text(
            encoding="utf-8")
        compact = "".join(chinese.split())
        for phrase in ("恰好刷新一次該folder的`_report.md`",
                       "receiptoperationsettle且所有verificationlock釋放後",
                       "best-effortreportrefresh"):
            with self.subTest(document="docs/zh-TW/VERIFICATION.md",
                              phrase=phrase):
                self.assertIn(phrase, compact)

    def test_format_states_the_provisioned_candidate_link_rule(self):
        install_global_contracts(self)
        text = contracts.installed_contract_text("format.md")
        for phrase in (
                "reviewed-profile ignored\ndirectory links Assent provisioned",
                "ordinary ignored leaf files that sit inside an otherwise "
                "tracked\ndirectory",
                "Arbitrary ignored content is never exposed",
                "removed before the temporary worktree"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_task_session_is_told_to_review_a_required_ignored_directory(self):
        """A zero-memory session reads instructions.md, never format.md."""
        install_global_contracts(self)
        text = " ".join(
            contracts.installed_contract_text("instructions.md").split())
        for phrase in (
                "assent shared-paths review --path DIR --watch FILE",
                "Never hand-create a source-worktree link",
                "Never copy the ignored directory tree in",
                "never modify anything inside the linked target"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        # The scheduled reading scope must stay as it is: the session is not
        # sent to the format contract to learn this.
        scope = text.split("An **assent-scheduled task session** reads only:")[1]
        self.assertNotIn("format.md", scope.split("## Working rules")[0])

    def test_contract_ownership_and_filename_language_have_one_owner(self):
        install_global_contracts(self)
        instructions = " ".join(
            contracts.installed_contract_text("instructions.md").split())
        format_text = " ".join(
            contracts.installed_contract_text("format.md").split())
        for phrase in (
                "repository-specific development constraints belong in `AGENTS.md`",
                "scheduled-session procedure belongs in `instructions.md`",
                "persisted artifact schemas, filename rules, state meanings",
                "Other documents may reference an owned rule, but must not duplicate it"):
            with self.subTest(document="instructions.md", phrase=phrase):
                self.assertIn(phrase, instructions)
        for phrase in (
                "descriptive `name` segment has no canonical-language requirement",
                "preserves the human-requested language, including Unicode",
                "task identity and dependency references use only the filename prefix `tNNN`",
                "paired `.r.toml` journal keeps the same descriptive segment"):
            with self.subTest(document="format.md", phrase=phrase):
                self.assertIn(phrase, format_text)
        self.assertNotIn("no canonical-language requirement", instructions)
        self.assertNotIn("human-requested language", instructions)

    def test_the_ignored_input_diagnosis_is_documented_in_english_and_chinese(self):
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in english.items():
            compact = " ".join(text.split())
            phrases = (("`Ignored input diagnosis:`", "junction")
                       if name != "docs/VERIFICATION.md" else
                       ("Ignored input diagnosis:", "junction",
                        "shared-paths review"))
            with self.subTest(document=name):
                for phrase in phrases:
                    with self.subTest(phrase=phrase):
                        self.assertIn(phrase, compact)
        chinese = {
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/zh-TW/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/zh-TW/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            phrases = (("`Ignoredinputdiagnosis:`", "junction")
                       if name != "docs/zh-TW/VERIFICATION.md" else
                       ("Ignoredinputdiagnosis:", "junction",
                        "assentshared-pathsreview"))
            with self.subTest(document=name):
                for phrase in phrases:
                    with self.subTest(phrase=phrase):
                        self.assertIn(phrase, compact)

    def test_shared_path_states_are_documented_in_english_and_chinese(self):
        """The three-state contract and its staleness rules reach every reader."""
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        session = english.pop("instructions.md")
        for name, text in english.items():
            compact = " ".join(text.split())
            for phrase in ("REVIEWED-NONE", "REVIEWED-PATHS", "STALE",
                           "`.assent/manifest.toml`",
                           "assent shared-paths review"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
        # The scheduled session reads only instructions.md, so the remedy -- not
        # the state vocabulary a scheduler owns -- is what has to be there.
        compact = " ".join(session.split())
        for phrase in ("`.assent/manifest.toml`", "`UNKNOWN` or `STALE`",
                       "assent shared-paths review --path DIR --watch FILE",
                       "assent shared-paths review --none --watch FILE"):
            with self.subTest(document="instructions.md", phrase=phrase):
                self.assertIn(phrase, compact)

        chinese = {
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/zh-TW/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/zh-TW/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            for phrase in ("REVIEWED-NONE", "REVIEWED-PATHS", "STALE",
                           "`.assent/manifest.toml`",
                           "assentshared-pathsreview",
                           "`shared_inputs_sha256`"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

    def test_the_zero_candidate_fast_path_is_documented_everywhere(self):
        """The state's exact meaning and its limits must reach every reader.

        Naming it is not enough: the documents have to say that it describes a
        successful ignored-entry query and not a semantic "nothing is needed",
        which is precisely the claim a reader would otherwise assume.
        """
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        session = english.pop("instructions.md")
        for name, text in english.items():
            compact = " ".join(text.split())
            phrases = (("NO-IGNORED-DIRECTORY-CANDIDATE",
                        "ignored-entry query",
                        "semantically needs no shared input")
                       if name != "docs/VERIFICATION.md" else
                       ("NO-IGNORED-DIRECTORY-CANDIDATE",
                        "ignored-entry query",
                        "not a semantic claim that the project never needs "
                        "shared input"))
            for phrase in phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
        compact = " ".join(session.split())
        for phrase in ("NO-IGNORED-DIRECTORY-CANDIDATE",
                       "nothing to review"):
            with self.subTest(document="instructions.md", phrase=phrase):
                self.assertIn(phrase, compact)

        chinese = {
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/zh-TW/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/zh-TW/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            for phrase in ("NO-IGNORED-DIRECTORY-CANDIDATE",
                           "ignored-entry", "語意上"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

    def test_link_cleanup_contract_is_present_in_all_reader_documents(self):
        documents = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "docs/OPERATIONS.md": (_PROJECT_ROOT / "docs/OPERATIONS.md").read_text(
                encoding="utf-8"),
        }
        required = (
            "Assent detaches each directory-link object before any recursive "
            "Git or filesystem removal and never traverses its resolved target.",
            "External link targets survive success, refusal, failure, "
            "interruption, and retry.",
        )
        for name, text in documents.items():
            compact = " ".join(text.split())
            phrases = (required if name != "docs/OPERATIONS.md" else (
                "A directory junction, directory symlink, or other directory "
                "reparse point is detached as a link object before any recursive "
                "Git or filesystem removal.",
                "The remover never traverses the link's resolved target.",
                "External targets survive success, refusal, failure, "
                "interruption, and retry."))
            for phrase in phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        chinese = (_PROJECT_ROOT / "docs/zh-TW/OPERATIONS.md").read_text(
            encoding="utf-8")
        compact = "".join(chinese.split())
        for phrase in (
                "directoryjunction、directorysymlink或其他directoryreparsepoint會先以"
                "linkobject脫離",
                "絕不穿越resolvedtarget",
                "外部target在成功、拒絕、失敗、中斷與重試後都保留"):
            with self.subTest(document="docs/zh-TW/OPERATIONS.md",
                              phrase=phrase):
                self.assertIn(phrase, compact)

    def test_limited_run_verification_is_conditional_in_every_document(self):
        """`--verify` with `--once`/`--task` is gated, never blanket-refused."""
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/COMMANDS.md": (
                _PROJECT_ROOT / "docs/COMMANDS.md").read_text(
                    encoding="utf-8"),
        }
        required = (
            "verifies only when that limited run left the single selected "
            "folder complete",
            "an incomplete folder fails the request without writing a receipt",
        )
        # The old blanket refusal must not come back anywhere; `...` staying
        # incompatible with the two selectors is a separate, still-true rule.
        forbidden = (
            "`--verify` cannot be combined with `--once`",
            "`--verify` is refused with `--once`",
            "refuse the flag",
            "incompatible with `--once`",
        )
        for name, text in english.items():
            compact = " ".join(text.split())
            phrases = (required if name != "docs/COMMANDS.md" else (
                "only if the limited run left every task complete",
                "an incomplete folder fails the request",
                "before any candidate or full verifier exists and writes no "
                "receipt"))
            for phrase in phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
            for phrase in forbidden:
                with self.subTest(document=name, forbidden=phrase):
                    self.assertNotIn(phrase, compact)

        chinese = {
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/zh-TW/COMMANDS.md": (
                _PROJECT_ROOT / "docs/zh-TW/COMMANDS.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            phrases = (("只有在該次受限執行讓所選資料夾變成完成時才驗證",
                        "資料夾未完成則此請求失敗且不寫下receipt")
                       if name != "docs/zh-TW/COMMANDS.md" else
                       ("只有A的所有task完成時才驗證",
                        "未完成folder時，會在candidate/verifier建立前拒絕且不寫receipt"))
            for phrase in phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
            with self.subTest(document=name, forbidden="blanket refusal"):
                self.assertNotIn("不可與`--once`、`--task`併用", compact)
                self.assertNotIn("不能與刻意在資料夾收尾前停止的", compact)

    def test_auto_fix_lifecycle_and_derived_state_are_documented(self):
        """The auto-fix contract must remain discoverable in every owning surface."""
        install_global_contracts(self)
        root = _PROJECT_ROOT
        english = {
            "AGENTS.md": (root / "AGENTS.md").read_text(encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "instructions.md": contracts.installed_contract_text("instructions.md"),
            "README.md": (root / "README.md").read_text(encoding="utf-8"),
            "WORKFLOW.md": (root / "docs/WORKFLOW.md").read_text(encoding="utf-8"),
            "COMMANDS.md": (root / "docs/COMMANDS.md").read_text(encoding="utf-8"),
            "CONFIGURATION.md": (root / "docs/CONFIGURATION.md").read_text(
                encoding="utf-8"),
            "VERIFICATION.md": (root / "docs/VERIFICATION.md").read_text(
                encoding="utf-8"),
            "OPERATIONS.md": (root / "docs/OPERATIONS.md").read_text(
                encoding="utf-8"),
            "CONSENSUS.md": (root / "docs/CONSENSUS.md").read_text(
                encoding="utf-8"),
        }
        required = (
            "run --auto-fix",
            "read-only",
            "pre-existing technical debt",
            "directly interacting code",
            "finite",
            "never creates tasks",
            "never accepts",
            "_auto_fix.toml",
        )
        for name, text in english.items():
            compact = " ".join(text.split())
            with self.subTest(document=name):
                self.assertIn("run --auto-fix", compact)
                self.assertIn("read-only", compact)
                self.assertIn("without", compact)
                self.assertIn("repair", compact)
        english_contract = " ".join(" ".join(text.split())
                                    for text in english.values())
        for phrase in required:
            with self.subTest(english_contract=phrase):
                self.assertIn(phrase, english_contract)

        format_text = english["format.md"]
        for field in (
                "source_tree", "task_plan_sha256", "review_prompt_sha256",
                "reviewer_adapter", "reviewer_model", "reviewer_effort",
                "current_finding_fingerprints", "observed_states",
                "consumed_fixer_profiles"):
            with self.subTest(state_field=field):
                self.assertIn(field, format_text)
        for phrase in (
                "malformed state refuses closed",
                "PASSED (fresh)", "FAILED (fresh)",
                "Automatic repair of durable folder-review findings",
                "authorization: run --auto-fix",
                "prompt-plus-detection",
                "source deletion",
                "focused sweep"):
            with self.subTest(format_phrase=phrase):
                self.assertIn(phrase, format_text)

        configuration = (root / "assent/templates/assent.toml").read_text(
            encoding="utf-8")
        self.assertIn("# [auto_fix.review]", configuration)
        self.assertIn('# model = "prime"', configuration)
        self.assertIn('# effort = "heavy"', configuration)
        self.assertIn("different vendor from the worker rotation", configuration)
        self.assertIn("only an explicit `run --auto-fix` invocation starts the review",
                      configuration)
        self.assertIn("ordinary run without the flag does neither", configuration)

        chinese = {
            "WORKFLOW.zh-TW.md": (root / "docs/zh-TW/WORKFLOW.md").read_text(
                encoding="utf-8"),
            "COMMANDS.zh-TW.md": (root / "docs/zh-TW/COMMANDS.md").read_text(
                encoding="utf-8"),
            "CONFIGURATION.zh-TW.md": (
                root / "docs/zh-TW/CONFIGURATION.md").read_text(encoding="utf-8"),
            "VERIFICATION.zh-TW.md": (
                root / "docs/zh-TW/VERIFICATION.md").read_text(encoding="utf-8"),
            "OPERATIONS.zh-TW.md": (
                root / "docs/zh-TW/OPERATIONS.md").read_text(encoding="utf-8"),
            "CONSENSUS.zh-TW.md": (
                root / "docs/zh-TW/CONSENSUS.md").read_text(encoding="utf-8"),
        }
        translated_required = (
            "`run --auto-fix`", "唯讀", "既有 technical debt", "直接互動程式碼",
            "不會自動建立 task", "絕不自動接受 folder", "`_auto_fix.toml`",
        )
        chinese_contract = "".join("".join(text.split())
                                   for text in chinese.values())
        for phrase in translated_required:
            with self.subTest(chinese_contract=phrase):
                self.assertIn("".join(phrase.split()), chinese_contract)

    def test_reader_recovery_never_recommends_raw_recursive_worktree_removal(self):
        paths = (
            _PROJECT_ROOT / "AGENTS.md",
            _PROJECT_ROOT / "assent/templates/format.md",
            _PROJECT_ROOT / "README.md",
            _PROJECT_ROOT / "README.zh-TW.md",
        )
        for path in paths:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("git worktree remove", text)
                self.assertNotIn("Remove residue manually", text)
                self.assertNotIn("請自行執行\n`git", text)


class TestContractValidation(unittest.TestCase):
    def test_current_contracts_report_no_error(self):
        install_global_contracts(self)
        self.assertEqual(contracts.contract_errors(), [])
        contracts.require_contracts()  # must not raise

    def test_a_windows_editor_rewriting_line_endings_stays_current(self):
        home = install_global_contracts(self)
        path = home / "instructions.md"
        path.write_bytes(
            path.read_text(encoding="utf-8").replace("\n", "\r\n")
            .encode("utf-8"))
        self.assertEqual(contracts.contract_errors(), [])

    def test_missing_contracts_are_named_one_by_one(self):
        home = install_global_contracts(self)
        (home / "format.md").unlink()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn(str(home / "format.md"), errors[0])
        self.assertIn("missing", errors[0])

    def test_an_empty_user_home_names_both_contracts(self):
        home = install_global_contracts(self)
        for name in contracts.CONTRACT_NAMES:
            (home / name).unlink()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 2)
        for name, error in zip(contracts.CONTRACT_NAMES, errors):
            with self.subTest(contract=name):
                self.assertIn(str(home / name), error)

    def test_a_stale_contract_is_reported_as_differing(self):
        home = install_global_contracts(self)
        (home / "instructions.md").write_text(
            "an older assent's working instructions\n", encoding="utf-8")
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("stale", errors[0])
        self.assertIn("instructions.md", errors[0])

    def test_an_unreadable_contract_is_reported_not_raised(self):
        home = install_global_contracts(self)
        (home / "format.md").unlink()
        (home / "format.md").mkdir()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be read", errors[0])

    def test_require_contracts_fails_closed_with_the_init_remedy(self):
        home = install_global_contracts(self)
        (home / "instructions.md").unlink()
        with self.assertRaises(AssentError) as ctx:
            contracts.require_contracts()
        message = str(ctx.exception)
        self.assertIn(str(home / "instructions.md"), message)
        self.assertIn("assent init", message)


if __name__ == "__main__":
    unittest.main()
