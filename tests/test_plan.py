"""任務檔/日誌檔解析與寫回測試(格式契約:templates/format.md)。"""
import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from agents import AgentsError
from agents.plan import (Plan, append_entry, journal_path_for, parse_task_file,
                         read_entries, same_except_status, set_status)

_OK = 'python -c "raise SystemExit(0)"'


def task_text(*, title="任務", deps=(), model="lite", effort=None,
              status="TODO", scope=("src/",), verify=_OK,
              goal="做一件事。", behavior="", acceptance="- 完成", notes="",
              extra_line=None, drop=()) -> str:
    lines = []

    def add(key, line):
        if key not in drop:
            lines.append(line)

    add("title", f"title = {json.dumps(title, ensure_ascii=False)}")
    add("deps", "deps = [" + ", ".join(json.dumps(d) for d in deps) + "]")
    add("model", f"model = {json.dumps(model)}")
    if effort:
        add("effort", f"effort = {json.dumps(effort)}")
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
    def test_valid_task_parsed(self):
        path = self.write("t001_demo.toml", task_text(
            title="骨架", deps=(), model="prime", effort="high",
            scope=("src/", "tests/"), notes="附註"))
        task = parse_task_file(path)
        self.assertEqual(task.id, "t001")
        self.assertEqual(task.title, "骨架")
        self.assertEqual(task.model, "prime")
        self.assertEqual(task.effort, "high")
        self.assertEqual(task.status, "TODO")
        self.assertEqual(task.scope, ["src/", "tests/"])
        self.assertEqual(task.journal_path.name, "r001_demo.toml")
        self.assertIn("做一件事", task.goal)
        self.assertIn("附註", task.notes)

    def test_id_comes_from_filename_only(self):
        path = self.write("t042_x.toml", task_text())
        self.assertEqual(parse_task_file(path).id, "t042")

    def test_bad_filename_rejected(self):
        for name in ("t42_x.toml", "task001_x.toml", "t001.toml", "t001_x.md"):
            path = self.write(name, task_text())
            with self.assertRaises(AgentsError):
                parse_task_file(path)

    def test_missing_required_fields_rejected(self):
        for key in ("title", "deps", "model", "status", "scope", "verify",
                    "goal", "acceptance"):
            path = self.write("t001_x.toml", task_text(drop=(key,)))
            with self.assertRaisesRegex(AgentsError, key):
                parse_task_file(path)

    def test_unknown_key_rejected(self):
        path = self.write("t001_x.toml", task_text(extra_line='oops = "x"'))
        with self.assertRaisesRegex(AgentsError, "未定義的欄位"):
            parse_task_file(path)

    def test_invalid_toml_rejected(self):
        path = self.write("t001_x.toml", "title = [unclosed\n")
        with self.assertRaisesRegex(AgentsError, "TOML"):
            parse_task_file(path)

    def test_brand_model_name_rejected(self):
        # 檔位嚴格制:任務檔絕不寫廠牌型號
        path = self.write("t001_x.toml", task_text(model="fable"))
        with self.assertRaisesRegex(AgentsError, "檔位"):
            parse_task_file(path)

    def test_bad_status_rejected(self):
        path = self.write("t001_x.toml", task_text(status="DOING"))
        with self.assertRaises(AgentsError):
            parse_task_file(path)

    def test_bad_effort_rejected(self):
        path = self.write("t001_x.toml", task_text(effort="max"))
        with self.assertRaises(AgentsError):
            parse_task_file(path)

    def test_empty_scope_fail_closed(self):
        path = self.write("t001_x.toml", task_text(scope=()))
        with self.assertRaisesRegex(AgentsError, "fail-closed"):
            parse_task_file(path)

    def test_bad_dep_id_rejected(self):
        path = self.write("t002_x.toml", task_text(deps=("W1",)))
        with self.assertRaisesRegex(AgentsError, "tNNN"):
            parse_task_file(path)

    def test_self_dependency_rejected(self):
        path = self.write("t001_x.toml", task_text(deps=("t001",)))
        with self.assertRaisesRegex(AgentsError, "依賴自己"):
            parse_task_file(path)


class TestPlanParse(PlanTestCase):
    def test_tasks_sorted_by_filename(self):
        self.write("t002_b.toml", task_text(deps=("t001",)))
        self.write("t001_a.toml", task_text())
        plan = Plan.parse(self.dir)
        self.assertEqual([t.id for t in plan.tasks], ["t001", "t002"])

    def test_non_task_files_ignored(self):
        self.write("t001_a.toml", task_text())
        self.write("r001_a.toml", "[[entry]]\ntime = \"x\"\nby = \"ai\"\n"
                                  "event = \"note\"\nsummary = \"s\"\n")
        self.write("report.md", "報告")
        plan = Plan.parse(self.dir)
        self.assertEqual(len(plan.tasks), 1)

    def test_empty_folder_rejected(self):
        with self.assertRaisesRegex(AgentsError, "沒有任務檔"):
            Plan.parse(self.dir)

    def test_missing_folder_rejected(self):
        with self.assertRaises(AgentsError):
            Plan.parse(self.dir / "nope")

    def test_duplicate_id_rejected(self):
        self.write("t001_a.toml", task_text())
        self.write("t001_b.toml", task_text())
        with self.assertRaisesRegex(AgentsError, "重複"):
            Plan.parse(self.dir)

    def test_unknown_dep_rejected(self):
        self.write("t001_a.toml", task_text(deps=("t009",)))
        with self.assertRaisesRegex(AgentsError, "不存在的任務"):
            Plan.parse(self.dir)

    def test_dependency_cycle_rejected(self):
        self.write("t001_a.toml", task_text(deps=("t002",)))
        self.write("t002_b.toml", task_text(deps=("t001",)))
        with self.assertRaisesRegex(AgentsError, "循環"):
            Plan.parse(self.dir)


class TestNextTask(PlanTestCase):
    def test_first_todo_with_met_deps(self):
        self.write("t001_a.toml", task_text(status="DONE"))
        self.write("t002_b.toml", task_text(deps=("t001",)))
        task, resumed = Plan.parse(self.dir).next_task()
        self.assertEqual(task.id, "t002")
        self.assertFalse(resumed)

    def test_wip_takes_priority_as_resume(self):
        self.write("t001_a.toml", task_text())
        self.write("t002_b.toml", task_text(status="WIP"))
        task, resumed = Plan.parse(self.dir).next_task()
        self.assertEqual(task.id, "t002")
        self.assertTrue(resumed)

    def test_blocked_dep_gates_downstream_but_not_others(self):
        self.write("t001_a.toml", task_text(status="BLOCKED"))
        self.write("t002_b.toml", task_text(deps=("t001",)))
        self.write("t003_c.toml", task_text())
        task, resumed = Plan.parse(self.dir).next_task()
        self.assertEqual(task.id, "t003")

    def test_skip_satisfies_dependency(self):
        self.write("t001_a.toml", task_text(status="SKIP"))
        self.write("t002_b.toml", task_text(deps=("t001",)))
        task, _ = Plan.parse(self.dir).next_task()
        self.assertEqual(task.id, "t002")

    def test_all_settled_returns_none(self):
        self.write("t001_a.toml", task_text(status="DONE"))
        self.write("t002_b.toml", task_text(status="BLOCKED"))
        self.assertIsNone(Plan.parse(self.dir).next_task())


class TestSetStatus(PlanTestCase):
    def test_only_status_line_changes(self):
        path = self.write("t001_x.toml", task_text(notes="內文提到 status 一詞"))
        before = path.read_text(encoding="utf-8")
        set_status(path, "BLOCKED")
        after = path.read_text(encoding="utf-8")
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        # 除 status 行外逐行位元組不變
        b_lines, a_lines = before.splitlines(), after.splitlines()
        self.assertEqual(len(b_lines), len(a_lines))
        diff = [i for i, (b, a) in enumerate(zip(b_lines, a_lines)) if b != a]
        self.assertEqual(len(diff), 1)
        self.assertIn('status = "BLOCKED"', a_lines[diff[0]])

    def test_crlf_preserved(self):
        path = self.dir / "t001_x.toml"
        path.write_bytes(task_text().replace("\n", "\r\n").encode("utf-8"))
        set_status(path, "DONE")
        raw = path.read_bytes()
        self.assertIn(b'status = "DONE"\r\n', raw)
        self.assertNotIn(b"\n\n\n", raw)

    def test_fake_status_line_in_prose_after_real_one_is_ignored(self):
        path = self.write("t001_x.toml", task_text(
            notes='status = "TODO"'))  # 多行字串裡的假 status 行(在真行之後)
        set_status(path, "DONE")
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertIn('status = "TODO"', parse_task_file(path).notes)

    def test_fake_status_line_before_real_one_detected(self):
        # goal 排在 status 之前的畸形檔:替換誤中散文行時,重解析驗證必須抓到
        text = ('title = "x"\ndeps = []\nmodel = "lite"\n'
                'goal = """\nstatus = "TODO"\n"""\n'
                'status = "TODO"\nscope = ["src/"]\n'
                f'verify = {json.dumps(_OK)}\n'
                'acceptance = """\n- ok\n"""\n')
        path = self.write("t001_x.toml", text)
        with self.assertRaises(AgentsError):
            set_status(path, "DONE")

    def test_invalid_status_value_rejected(self):
        path = self.write("t001_x.toml", task_text())
        with self.assertRaises(AgentsError):
            set_status(path, "DOING")


class TestStructuralCompare(PlanTestCase):
    def test_identical_except_status_ok(self):
        path = self.write("t001_x.toml", task_text())
        a = parse_task_file(path)
        set_status(path, "DONE")
        b = parse_task_file(path)
        self.assertEqual(same_except_status(a, b), [])

    def test_tampered_fields_reported(self):
        path = self.write("t001_x.toml", task_text())
        a = parse_task_file(path)
        self.write("t001_x.toml", task_text(
            status="DONE", scope=("src/", "secret/"), verify='echo ok'))
        b = parse_task_file(path)
        diff = same_except_status(a, b)
        self.assertIn("scope", diff)
        self.assertIn("verify", diff)


class TestJournal(PlanTestCase):
    def test_append_creates_valid_toml_and_accumulates(self):
        journal = self.dir / "r001_x.toml"
        append_entry(journal, by="ai", event="done", summary="第一筆",
                     detail="細節\n多行", time_str="2026-07-17T00:00:00+00:00")
        append_entry(journal, by="scheduler", event="blocked", summary="第二筆")
        entries = read_entries(journal)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["summary"], "第一筆")
        self.assertIn("多行", entries[0]["detail"])
        self.assertEqual(entries[1]["by"], "scheduler")

    def test_summary_with_quotes_and_backslashes(self):
        journal = self.dir / "r001_x.toml"
        tricky = 'He said "C:\\Users\\L" fails'
        append_entry(journal, by="ai", event="note", summary=tricky)
        self.assertEqual(read_entries(journal)[0]["summary"], tricky)

    def test_detail_with_triple_quotes_sanitized(self):
        journal = self.dir / "r001_x.toml"
        append_entry(journal, by="ai", event="note", summary="s",
                     detail="含 ''' 的內容")
        with open(journal, "rb") as f:
            tomllib.load(f)  # 仍是有效 TOML 即可

    def test_bad_by_rejected(self):
        with self.assertRaises(AgentsError):
            append_entry(self.dir / "r001_x.toml", by="human", event="e",
                         summary="s")

    def test_read_entries_missing_file_empty(self):
        self.assertEqual(read_entries(self.dir / "r009_x.toml"), [])

    def test_journal_path_for(self):
        self.assertEqual(journal_path_for(Path("a/t001_x.toml")).name,
                         "r001_x.toml")


if __name__ == "__main__":
    unittest.main()
