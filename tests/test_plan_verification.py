import unittest

from assent.verification_common import FullVerifyEvidence


class TestFullVerifyEvidence(unittest.TestCase):
    def test_passed_is_typed_separately_from_other_outcomes(self):
        common = dict(
            plan_names=("alpha",), target_commit="a" * 40,
            source_commits=("b" * 40,), candidate_tree="c" * 40,
            verification_script_sha256="d" * 64,
            ignored_directory_inputs_sha256="e" * 64, exit_code=0)

        self.assertTrue(FullVerifyEvidence("PASSED", **common).passed)
        self.assertFalse(FullVerifyEvidence(
            "VERIFIER_FAILED", **(common | {"exit_code": 1})).passed)


if __name__ == "__main__":
    unittest.main()
