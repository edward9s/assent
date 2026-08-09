"""Tests for task/journal file parsing and writeback (format contract: templates/format.md)."""
import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from assent import AssentError
from assent.plan import (Plan, add_scope_entries, append_entry,
                         journal_path_for, parse_task_file, read_entries,
                         read_workflow_state,
                         same_except_status, scope_text_with_entries,
                         scope_text_without_entries,
                         set_status, task_text_sha256, WorkflowState,
                         workflow_state_path, write_workflow_state)

_OK = 'python -c "raise SystemExit(0)"'


def task_text(*, title="Task", deps=(), model="lite", effort=None,
              status="TODO", scope=("src/",), verify=_OK,
              goal="Do one thing.", behavior="", acceptance="- done", notes="",
              workflow=None, extra_line=None, drop=()) -> str:
    lines = []

    def add(key, line):
        if key not in drop:
            lines.append(line)

    add("title", f"title = {json.dumps(title, ensure_ascii=False)}")
    add("deps", "deps = [" + ", ".join(json.dumps(d) for d in deps) + "]")
    add("model", f"model = {json.dumps(model)}")
    if effort:
        add("effort", f"effort = {json.dumps(effort)}")
    if workflow is not None:
        add("workflow", "workflow = [" + ", ".join(
            f"{{ role = {json.dumps(role)} }}" for role in workflow) + "]")
    add("status", f"status = {json.dumps(status)}")
    add("scope", "scope = [" + ", ".join(json.dumps(s) for s in scope) + "]")
    add("verify", f"verify = {json.dumps(verify, ensure_ascii=False)}")
    if extra_line:
        lines.append(extra_line)
    add("goal", f'goal = """\n{goal}\n"""')
    if behavior:
        add("behavior", f'behavior = """\n{behavior}\n"""')
    add("acceptance", f'acceptance = """\n{acceptance}\n"""')
    if notes:
        add("notes", f'notes = """\n{notes}\n"""')
    return "\n".join(lines) + "\n"


class PlanTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, filename: str, text: str) -> Path:
        path = self.dir / filename
        path.write_text(text, encoding="utf-8", newline="\n")
        return path


class TestParseTaskFile(PlanTestCase):
    def test_formal_task_path_parsed_and_preserved(self):
        path = self.write("t001_demo.e.toml", task_text())
        task = parse_task_file(path)
        self.assertEqual(task.id, "t001")
        self.assertEqual(task.path, path.resolve())
        self.assertEqual(task.journal_path.name, "t001_demo.r.toml")

    def test_workflow_state_round_trips_started_boundary(self):
        state = WorkflowState(
            "task", "t001", 2, False, "abc123", ("PASS: focused",))
        write_workflow_state(self.dir, state)

        self.assertEqual(read_workflow_state(self.dir), state)
        self.assertEqual(workflow_state_path(self.dir).name, "_workflow.toml")

    def test_unicode_name_keeps_english_title_and_pairs_journal_without_translation(self):
        path = self.write("t001_中文任務.e.toml", task_text(title="English task title"))
        task = parse_task_file(path)
        self.assertEqual(task.id, "t001")
        self.assertEqual(task.title, "English task title")
        self.assertEqual(task.journal_path.name, "t001_中文任務.r.toml")

    def test_valid_task_parsed(self):
        path = self.write("t001_demo.e.toml", task_text(
            title="Scaffold", deps=(), model="prime", effort="heavy",
            scope=("src/", "tests/"), notes="a note"))
        task = parse_task_file(path)
        self.assertEqual(task.id, "t001")
        self.assertEqual(task.title, "Scaffold")
        self.assertEqual(task.model, "prime")
        self.assertEqual(task.effort, "heavy")
        self.assertEqual(task.status, "TODO")
        self.assertEqual(task.scope, ["src/", "tests/"])
        self.assertEqual(task.journal_path.name, "t001_demo.r.toml")
        self.assertIn("Do one thing", task.goal)
        self.assertIn("a note", task.notes)

    def test_id_comes_from_filename_only(self):
        path = self.write("t042_x.e.toml", task_text())
        self.assertEqual(parse_task_file(path).id, "t042")

    def test_bad_filename_rejected(self):
        for name in ("t42_x.e.toml", "task001_x.e.toml", "t001.e.toml",
                     "t001_x.md"):
            path = self.write(name, task_text())
            with self.assertRaises(AssentError):
                parse_task_file(path)

    def test_retired_task_file_rejected_with_migration_error(self):
        path = self.write("t001_demo.toml", task_text())
        with self.assertRaisesRegex(AssentError, "Legacy task file.*retired.*move"):
            parse_task_file(path)

    def test_journal_file_rejected_before_parsing_fields(self):
        path = self.write("t001_demo.r.toml", "[[entry]]\n")
        with self.assertRaisesRegex(AssentError, "Journal file.*must not be parsed as a task file"):
            parse_task_file(path)

    def test_missing_required_fields_rejected(self):
        for key in ("title", "deps", "model", "status", "scope", "verify",
                    "goal", "acceptance"):
            path = self.write("t001_x.e.toml", task_text(drop=(key,)))
            with self.assertRaisesRegex(AssentError, key):
                parse_task_file(path)

    def test_unknown_key_rejected(self):
        path = self.write("t001_x.e.toml", task_text(extra_line='oops = "x"'))
        with self.assertRaisesRegex(AssentError, "undefined fields"):
            parse_task_file(path)

    def test_task_workflow_is_optional_ordered_and_may_be_empty(self):
        absent = parse_task_file(self.write("t001_absent.e.toml", task_text()))
        stated = parse_task_file(self.write(
            "t002_stated.e.toml", task_text(workflow=("prepare", "implement"))))
        empty = parse_task_file(self.write(
            "t003_empty.e.toml", task_text(workflow=())))

        self.assertIsNone(absent.workflow)
        self.assertEqual(stated.workflow, ("prepare", "implement"))
        self.assertEqual(empty.workflow, ())

    def test_task_workflow_rejects_malformed_entries(self):
        for value in ('"implement"', '["implement"]', '[{ role = "" }]',
                      '[{ role = "implement", extra = true }]'):
            with self.subTest(value=value):
                path = self.write(
                    "t001_bad.e.toml", task_text(extra_line=f"workflow = {value}"))
                with self.assertRaisesRegex(AssentError, "workflow"):
                    parse_task_file(path)

    def test_invalid_toml_rejected(self):
        path = self.write("t001_x.e.toml", "title = [unclosed\n")
        with self.assertRaisesRegex(AssentError, "TOML"):
            parse_task_file(path)

    def test_brand_model_name_rejected(self):
        # Tiers are a strict enum: a task file must never write a vendor model name
        path = self.write("t001_x.e.toml", task_text(model="fable"))
        with self.assertRaisesRegex(AssentError, "tier"):
            parse_task_file(path)

    def test_bad_status_rejected(self):
        path = self.write("t001_x.e.toml", task_text(status="DOING"))
        with self.assertRaises(AssentError):
            parse_task_file(path)

    def test_bad_effort_rejected(self):
        path = self.write("t001_x.e.toml", task_text(effort="max"))
        with self.assertRaises(AssentError):
            parse_task_file(path)

    def test_old_effort_rejected_with_new_vocabulary(self):
        path = self.write("t001_x.e.toml", task_text(effort="high"))
        with self.assertRaisesRegex(AssentError, "heavy / normal / slight"):
            parse_task_file(path)

    def test_empty_scope_fail_closed(self):
        path = self.write("t001_x.e.toml", task_text(scope=()))
        with self.assertRaisesRegex(AssentError, "fail-closed"):
            parse_task_file(path)

    def test_full_verifier_as_a_task_gate_rejected(self):
        # The full verifier is folder closeout's own stage: naming it here makes
        # every task re-run the whole suite, and on a slow project it outlives
        # what a session can wait for at all.
        for command in ("python .assent/verify.py",
                        "python .assent\\verify.py",
                        "py -3 .assent/verify.py --quiet",
                        "python C:/proj/.assent/verify.py"):
            with self.subTest(command=command):
                path = self.write("t001_x.e.toml", task_text(verify=command))
                with self.assertRaisesRegex(AssentError, "full verifier"):
                    parse_task_file(path)

    def test_a_narrow_gate_naming_its_own_verify_module_is_accepted(self):
        # Only the project's own .assent/verify.py is refused; a test module that
        # merely happens to be about verification is an ordinary focused gate.
        path = self.write(
            "t001_x.e.toml",
            task_text(verify="python -m unittest tests.test_verify_py"))
        self.assertEqual(parse_task_file(path).verify,
                         "python -m unittest tests.test_verify_py")

    def test_bad_dep_id_rejected(self):
        path = self.write("t002_x.e.toml", task_text(deps=("W1",)))
        with self.assertRaisesRegex(AssentError, "tNNN"):
            parse_task_file(path)

    def test_self_dependency_rejected(self):
        path = self.write("t001_x.e.toml", task_text(deps=("t001",)))
        with self.assertRaisesRegex(AssentError, "must not depend on itself"):
            parse_task_file(path)


class TestPlanParse(PlanTestCase):
    def test_formal_tasks_share_dependency_graph_and_selection(self):
        second = self.write("t002_b.e.toml", task_text(deps=("t001",)))
        first = self.write("t001_a.e.toml", task_text(status="DONE"))
        plan = Plan.parse(self.dir)
        self.assertEqual([task.path for task in plan.tasks],
                         [first.resolve(), second.resolve()])
        task, resumed = plan.next_task()
        self.assertEqual(task.path, second.resolve())
        self.assertFalse(resumed)

    def test_tasks_sorted_by_filename(self):
        self.write("t002_b.e.toml", task_text(deps=("t001",)))
        self.write("t001_a.e.toml", task_text())
        plan = Plan.parse(self.dir)
        self.assertEqual([t.id for t in plan.tasks], ["t001", "t002"])

    def test_unicode_name_orders_and_resolves_dependencies_by_tnnn(self):
        self.write("t002_second.e.toml", task_text(
            title="Second task", deps=("t001",)))
        self.write("t001_中文任務.e.toml", task_text(
            title="English first task", status="DONE"))
        plan = Plan.parse(self.dir)
        self.assertEqual([task.path.name for task in plan.tasks],
                         ["t001_中文任務.e.toml", "t002_second.e.toml"])
        self.assertEqual(plan.tasks[1].deps, ["t001"])
        task, resumed = plan.next_task()
        self.assertEqual(task.id, "t002")
        self.assertFalse(resumed)

    def test_non_task_files_ignored(self):
        self.write("t001_a.e.toml", task_text())
        self.write("t001_a.r.toml", "[[entry]]\ntime = \"x\"\nby = \"ai\"\n"
                                    "event = \"note\"\nsummary = \"s\"\n")
        self.write("_report.md", "report")
        plan = Plan.parse(self.dir)
        self.assertEqual(len(plan.tasks), 1)

    def test_multiple_task_journal_pairs_do_not_affect_plan(self):
        self.write("t001_a.e.toml", task_text(status="DONE"))
        self.write("t001_a.r.toml", task_text(deps=("t999",)))
        self.write("t002_b.e.toml", task_text(deps=("t001",)))
        self.write("t002_b.r.toml", task_text(deps=("t002",)))

        plan = Plan.parse(self.dir)

        self.assertEqual([task.id for task in plan.tasks], ["t001", "t002"])
        task, resumed = plan.next_task()
        self.assertEqual(task.id, "t002")
        self.assertFalse(resumed)

    def test_journals_do_not_hide_real_dependency_cycle(self):
        self.write("t001_a.e.toml", task_text(deps=("t002",)))
        self.write("t001_a.r.toml", task_text())
        self.write("t002_b.e.toml", task_text(deps=("t001",)))
        self.write("t002_b.r.toml", task_text())
        with self.assertRaisesRegex(AssentError, "cycle"):
            Plan.parse(self.dir)

    def test_empty_folder_rejected(self):
        with self.assertRaisesRegex(AssentError, "no task files"):
            Plan.parse(self.dir)

    def test_missing_folder_rejected(self):
        with self.assertRaises(AssentError):
            Plan.parse(self.dir / "nope")

    def test_duplicate_id_rejected(self):
        self.write("t001_a.e.toml", task_text())
        self.write("t001_b.e.toml", task_text())
        with self.assertRaisesRegex(AssentError, "Duplicate task id"):
            Plan.parse(self.dir)

    def test_retired_task_residue_rejected_even_with_formal_task(self):
        self.write("t001_a.e.toml", task_text())
        self.write("t001_a.toml", task_text())
        with self.assertRaisesRegex(AssentError, "retired legacy task files.*move"):
            Plan.parse(self.dir)

    def test_only_retired_task_residue_rejected_instead_of_ignored(self):
        self.write("t001_a.toml", task_text())
        with self.assertRaisesRegex(AssentError, "retired legacy task files.*move"):
            Plan.parse(self.dir)

    def test_unknown_dep_rejected(self):
        self.write("t001_a.e.toml", task_text(deps=("t009",)))
        with self.assertRaisesRegex(AssentError, "depends on a task that does not exist"):
            Plan.parse(self.dir)

    def test_dependency_cycle_rejected(self):
        self.write("t001_a.e.toml", task_text(deps=("t002",)))
        self.write("t002_b.e.toml", task_text(deps=("t001",)))
        with self.assertRaisesRegex(AssentError, "cycle"):
            Plan.parse(self.dir)


class TestNextTask(PlanTestCase):
    def test_first_todo_with_met_deps(self):
        self.write("t001_a.e.toml", task_text(status="DONE"))
        self.write("t002_b.e.toml", task_text(deps=("t001",)))
        task, resumed = Plan.parse(self.dir).next_task()
        self.assertEqual(task.id, "t002")
        self.assertFalse(resumed)

    def test_wip_takes_priority_as_resume(self):
        self.write("t001_a.e.toml", task_text())
        self.write("t002_b.e.toml", task_text(status="WIP"))
        task, resumed = Plan.parse(self.dir).next_task()
        self.assertEqual(task.id, "t002")
        self.assertTrue(resumed)

    def test_blocked_dep_gates_downstream_but_not_others(self):
        self.write("t001_a.e.toml", task_text(status="BLOCKED"))
        self.write("t002_b.e.toml", task_text(deps=("t001",)))
        self.write("t003_c.e.toml", task_text())
        task, resumed = Plan.parse(self.dir).next_task()
        self.assertEqual(task.id, "t003")

    def test_skip_satisfies_dependency(self):
        self.write("t001_a.e.toml", task_text(status="SKIP"))
        self.write("t002_b.e.toml", task_text(deps=("t001",)))
        task, _ = Plan.parse(self.dir).next_task()
        self.assertEqual(task.id, "t002")

    def test_all_settled_returns_none(self):
        self.write("t001_a.e.toml", task_text(status="DONE"))
        self.write("t002_b.e.toml", task_text(status="BLOCKED"))
        self.assertIsNone(Plan.parse(self.dir).next_task())


class TestSetStatus(PlanTestCase):
    def test_formal_task_status_writeback(self):
        path = self.write("t001_x.e.toml", task_text())
        set_status(path, "DONE")
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_only_status_line_changes(self):
        path = self.write("t001_x.e.toml", task_text(notes="the word status appears in prose"))
        before = path.read_text(encoding="utf-8")
        set_status(path, "BLOCKED")
        after = path.read_text(encoding="utf-8")
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        # every line other than the status line stays byte-identical
        b_lines, a_lines = before.splitlines(), after.splitlines()
        self.assertEqual(len(b_lines), len(a_lines))
        diff = [i for i, (b, a) in enumerate(zip(b_lines, a_lines)) if b != a]
        self.assertEqual(len(diff), 1)
        self.assertIn('status = "BLOCKED"', a_lines[diff[0]])

    def test_crlf_preserved(self):
        path = self.dir / "t001_x.e.toml"
        path.write_bytes(task_text().replace("\n", "\r\n").encode("utf-8"))
        set_status(path, "DONE")
        raw = path.read_bytes()
        self.assertIn(b'status = "DONE"\r\n', raw)
        self.assertNotIn(b"\n\n\n", raw)

    def test_fake_status_line_in_prose_after_real_one_is_ignored(self):
        path = self.write("t001_x.e.toml", task_text(
            notes='status = "TODO"'))  # fake status line inside a multi-line string, after the real one
        set_status(path, "DONE")
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertIn('status = "TODO"', parse_task_file(path).notes)

    def test_fake_status_line_before_real_one_detected(self):
        # malformed file with goal before status: if the replace hits the prose
        # line by mistake, the re-parse validation must catch it
        text = ('title = "x"\ndeps = []\nmodel = "lite"\n'
                'goal = """\nstatus = "TODO"\n"""\n'
                'status = "TODO"\nscope = ["src/"]\n'
                f'verify = {json.dumps(_OK)}\n'
                'acceptance = """\n- ok\n"""\n')
        path = self.write("t001_x.e.toml", text)
        with self.assertRaises(AssentError):
            set_status(path, "DONE")

    def test_invalid_status_value_rejected(self):
        path = self.write("t001_x.e.toml", task_text())
        with self.assertRaises(AssentError):
            set_status(path, "DOING")


class TestAddScopeEntries(PlanTestCase):
    def test_append_is_atomic_reversible_and_preserves_unrelated_bytes(self):
        path = self.write("t001_x.e.toml", task_text(
            scope=("src/base.py",), notes="scope = [\"not-real.py\"]"))
        before = path.read_text(encoding="utf-8")
        predicted = scope_text_with_entries(
            before, ["src/existing.py", "tests/new_case.py"])
        before_sha, after_sha = add_scope_entries(
            path, ["src/existing.py", "tests/new_case.py"],
            expected_sha256=task_text_sha256(before))
        after = path.read_text(encoding="utf-8")

        self.assertEqual(before_sha, task_text_sha256(before))
        self.assertEqual(after_sha, task_text_sha256(after))
        self.assertEqual(after, predicted)
        self.assertEqual(parse_task_file(path).scope, [
            "src/base.py", "src/existing.py", "tests/new_case.py"])
        self.assertEqual(scope_text_without_entries(
            after, ["src/existing.py", "tests/new_case.py"]), before)
        self.assertIn('scope = ["not-real.py"]', after)

    def test_compare_and_swap_and_duplicate_refuse_without_mutation(self):
        path = self.write("t001_x.e.toml", task_text(scope=("src/base.py",)))
        before = path.read_bytes()
        with self.assertRaisesRegex(AssentError, "changed before"):
            add_scope_entries(path, ["tests/new.py"], expected_sha256="0" * 64)
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(AssentError, "duplicate"):
            add_scope_entries(path, ["src/base.py"])
        self.assertEqual(path.read_bytes(), before)


class TestStructuralCompare(PlanTestCase):
    def test_identical_except_status_ok(self):
        path = self.write("t001_x.e.toml", task_text())
        a = parse_task_file(path)
        set_status(path, "DONE")
        b = parse_task_file(path)
        self.assertEqual(same_except_status(a, b), [])

    def test_tampered_fields_reported(self):
        path = self.write("t001_x.e.toml", task_text())
        a = parse_task_file(path)
        self.write("t001_x.e.toml", task_text(
            status="DONE", scope=("src/", "secret/"), verify='echo ok'))
        b = parse_task_file(path)
        diff = same_except_status(a, b)
        self.assertIn("scope", diff)
        self.assertIn("verify", diff)

    def test_tampered_workflow_is_reported(self):
        path = self.write("t001_x.e.toml", task_text(workflow=("prepare",)))
        original = parse_task_file(path)
        self.write("t001_x.e.toml", task_text(
            status="DONE", workflow=("implement",)))

        self.assertEqual(
            same_except_status(original, parse_task_file(path)), ["workflow"])


class TestJournal(PlanTestCase):
    def test_append_creates_valid_toml_and_accumulates(self):
        journal = self.dir / "t001_x.r.toml"
        append_entry(journal, by="codex", requested_model="gpt-test",
                     requested_effort="max",
                     event="done", summary="first entry",
                     detail="details\nmultiple lines", time_str="2026-07-17T00:00:00+00:00")
        append_entry(journal, by="scheduler", agent="codex",
                     requested_model="gpt-test", event="blocked",
                     summary="second entry")
        entries = read_entries(journal)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["summary"], "first entry")
        self.assertEqual(entries[0]["requested_model"], "gpt-test")
        self.assertEqual(entries[0]["requested_effort"], "max")
        self.assertIn("multiple lines", entries[0]["detail"])
        self.assertEqual(entries[1]["by"], "scheduler")
        self.assertEqual(entries[1]["agent"], "codex")

        lines = journal.read_text(encoding="utf-8").splitlines()
        first = lines.index('by = "codex"')
        self.assertEqual(lines[first + 1], 'requested_model = "gpt-test"')
        self.assertEqual(lines[first + 2], 'requested_effort = "max"')
        second = lines.index('by = "scheduler"')
        self.assertEqual(lines[second + 1], 'agent = "codex"')
        self.assertEqual(lines[second + 2], 'requested_model = "gpt-test"')

    def test_summary_with_quotes_and_backslashes(self):
        journal = self.dir / "t001_x.r.toml"
        tricky = 'He said "C:\\Users\\L" fails'
        append_entry(journal, by="claude", requested_model="sonnet",
                     event="note", summary=tricky)
        self.assertEqual(read_entries(journal)[0]["summary"], tricky)

    def test_detail_with_triple_quotes_sanitized(self):
        journal = self.dir / "t001_x.r.toml"
        append_entry(journal, by="claude", requested_model="sonnet",
                     event="note", summary="s",
                     detail="content containing '''")
        with open(journal, "rb") as f:
            tomllib.load(f)  # only needs to still be valid TOML

    def test_new_entry_rejects_legacy_ai_identity(self):
        with self.assertRaises(AssentError):
            append_entry(self.dir / "t001_x.r.toml", by="ai", event="done",
                         summary="s")

    def test_read_legacy_ai_entry_without_new_fields(self):
        journal = self.dir / "t001_x.r.toml"
        journal.write_text(
            '[[entry]]\ntime = "2026-07-17T00:00:00+00:00"\n'
            'by = "ai"\nevent = "done"\nsummary = "legacy data"\n',
            encoding="utf-8")
        entries = read_entries(journal)
        self.assertEqual(entries[0]["by"], "ai")
        self.assertNotIn("requested_model", entries[0])

    def test_bad_by_rejected(self):
        with self.assertRaises(AssentError):
            append_entry(self.dir / "t001_x.r.toml", by="human", event="e",
                         summary="s")

    def test_empty_requested_effort_rejected(self):
        with self.assertRaisesRegex(AssentError, "requested_effort"):
            append_entry(self.dir / "t001_x.r.toml", by="codex", event="done",
                         summary="s", requested_effort=" ")

    def test_read_entries_missing_file_empty(self):
        self.assertEqual(read_entries(self.dir / "t009_x.r.toml"), [])

    def test_journal_path_for_rejects_retired_task_file(self):
        with self.assertRaisesRegex(AssentError, r"must be tNNN_name\.e\.toml"):
            journal_path_for(Path("a/t001_x.toml"))

    def test_journal_path_for_rejects_journal_file(self):
        with self.assertRaisesRegex(AssentError, r"must be tNNN_name\.e\.toml"):
            journal_path_for(Path("a/t001_x.r.toml"))

    def test_formal_journal_path_for_removes_execution_marker(self):
        journal = journal_path_for(Path("a/t001_x.e.toml"))
        self.assertEqual(journal.name, "t001_x.r.toml")
        self.assertNotEqual(journal.name, "t001_x.e.r.toml")

    def test_task_and_journal_pairs_are_adjacent_when_sorted(self):
        names = [
            journal_path_for(Path("t002_b.e.toml")).name,
            "t001_a.e.toml",
            journal_path_for(Path("t001_a.e.toml")).name,
            "t002_b.e.toml",
        ]
        self.assertEqual(sorted(names), [
            "t001_a.e.toml", "t001_a.r.toml",
            "t002_b.e.toml", "t002_b.r.toml",
        ])

    def test_old_journal_name_is_not_adopted(self):
        task = self.write("t001_x.e.toml", task_text())
        old_journal = self.write("r001_x.toml", "[[entry]]\n")
        self.assertNotEqual(journal_path_for(task), old_journal)
        self.assertEqual(journal_path_for(task).name, "t001_x.r.toml")


if __name__ == "__main__":
    unittest.main()
