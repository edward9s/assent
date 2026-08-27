"""Derived provider-usage evidence."""
import json
import tempfile
import unittest
from pathlib import Path

from assent.adapters import TokenUsage
from assent.usage import read_records, record_invocation


class UsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_record_has_one_unversioned_schema(self):
        self.assertTrue(record_invocation(
            self.directory, invocation_id="invocation", adapter="codex",
            requested_model="gpt", context_kind="task", context_id="t001",
            plan_names=("plan",),
            evidence=(TokenUsage(input_tokens=3, output_tokens=2),)))

        records, invalid = read_records(self.directory)

        self.assertEqual(invalid, 0)
        self.assertEqual(len(records), 1)
        self.assertNotIn("version", records[0])

    def test_versioned_record_is_not_accepted_as_current(self):
        record = {
            "version": 1,
            "invocation_id": "old",
            "time": "2026-08-27T00:00:00+00:00",
            "adapter": "codex",
            "requested_model": "gpt",
            "context": {"kind": "task", "id": "t001"},
            "plans": ["plan"],
            "models": [],
        }
        (self.directory / "_usage.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8")

        self.assertTrue(record_invocation(
            self.directory, invocation_id="old", adapter="codex",
            requested_model="gpt", context_kind="task", context_id="t001",
            plan_names=("plan",), evidence=()))
        records, invalid = read_records(self.directory)

        self.assertEqual(invalid, 1)
        self.assertEqual(len(records), 1)
        self.assertNotIn("version", records[0])


if __name__ == "__main__":
    unittest.main()
