"""Fresh product tests. No benchmark code or fixtures are imported."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from link_doctor.outcomes import assess_candidate


class OutcomeTests(unittest.TestCase):
    def test_nonempty_candidate_with_unmet_requirement_is_unknown(self):
        result = assess_candidate(("main.o", "libanswer.a"), ("unresolved: answer",))
        self.assertEqual(result.status, "unknown")
        self.assertIsNone(result.command)
        self.assertEqual(result.unmet_requirements, ("unresolved: answer",))
        self.assertFalse(result.verified)

    def test_satisfied_candidate_is_not_linker_verified(self):
        result = assess_candidate(("main.o", "libanswer.a"), ())
        self.assertEqual(result.status, "candidate")
        self.assertEqual(result.command, ("main.o", "libanswer.a"))
        self.assertFalse(result.verified)

    def test_no_candidate_is_unknown_not_impossible(self):
        result = assess_candidate(None, ())
        self.assertEqual(result.status, "unknown")
        self.assertIsNone(result.command)

    def test_empty_candidate_is_distinct_from_no_candidate(self):
        result = assess_candidate((), ())
        self.assertEqual(result.status, "candidate")
        self.assertEqual(result.command, ())

    def test_serialized_failed_candidate_cannot_claim_success(self):
        payload = json.loads(json.dumps(assess_candidate(("bad.a",), ("missing",)).to_dict()))
        self.assertEqual(payload["status"], "unknown")
        self.assertIsNone(payload["command"])
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["unmet_requirements"], ["missing"])

    def test_caller_mutation_does_not_change_assessment(self):
        command = ["main.o"]
        unmet = ["missing"]
        result = assess_candidate(command, unmet)
        command.clear()
        unmet.clear()
        self.assertEqual(result.unmet_requirements, ("missing",))
        self.assertEqual(result.status, "unknown")


class NativeLinkerTests(unittest.TestCase):
    def test_fresh_archive_order_fixture(self):
        for tool in ("gcc", "ld.bfd", "ar"):
            if shutil.which(tool) is None:
                self.fail(f"Native integration prerequisite missing: {tool}")
        fixtures = Path(__file__).parent / "fixtures" / "archive_order"
        with tempfile.TemporaryDirectory(prefix="link-doctor-test-") as directory:
            work = Path(directory)
            for name in ("main", "answer"):
                subprocess.run(["gcc", "-c", "-fno-pie", "-fno-stack-protector",
                                str(fixtures / f"{name}.c"), "-o", str(work / f"{name}.o")],
                               check=True, capture_output=True, timeout=20)
            subprocess.run(["ar", "rcs", "libanswer.a", "answer.o"], cwd=work,
                           check=True, capture_output=True, timeout=20)
            bad = ("libanswer.a", "main.o")
            good = ("main.o", "libanswer.a")
            def link(command, output):
                return subprocess.run(["ld.bfd", "-e", "main", "-o", output, *command],
                                      cwd=work, capture_output=True, text=True, timeout=20)
            failed = link(bad, "bad.out")
            self.assertNotEqual(failed.returncode, 0, failed.stderr)
            self.assertIn("answer", failed.stderr)
            rejected = assess_candidate(bad, ("native link failed",))
            self.assertEqual(rejected.status, "unknown")
            self.assertIsNone(rejected.command)
            passed = link(good, "good.out")
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertTrue((work / "good.out").is_file())
            # Assessment alone never upgrades a command to verified.
            candidate = assess_candidate(good, ())
            self.assertEqual(candidate.status, "candidate")
            self.assertFalse(candidate.verified)


if __name__ == "__main__":
    unittest.main()
