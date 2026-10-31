"""Phase 5 packaging and CLI contract tests.

These are fresh tests for the release surface: noninteractive --help/--version,
a stable exit-status contract, agreement between human and versioned JSON
output (including error reports), and the shipped public examples that must
work without the private task collection. No private fixtures are used.
"""
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from link_doctor import diagnose as engine
import link_doctor

SRC = Path(engine.__file__).resolve().parent.parent
ROOT = SRC.parent
EXAMPLES = Path(link_doctor.__file__).resolve().parent / "examples"
ENV = dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")


def cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-m", "link_doctor", *args],
                          cwd=cwd, env=ENV, capture_output=True, text=True, timeout=120)


class WorkBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="phase5 ")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def build(self, name, source, compiler="gcc"):
        path = self.work / (name + (".cc" if compiler == "g++" else ".c"))
        path.write_text(source)
        subprocess.run([compiler, "-c", str(path), "-o", name + ".o"], cwd=self.work,
                       capture_output=True, text=True, check=True)


class CliSurfaceTests(WorkBase):
    def test_help_needs_no_linker_argv(self):
        result = cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", result.stdout)
        self.assertIn("diagnose", result.stdout)
        self.assertIn("--verify", result.stdout)
        self.assertIn("--cwd", result.stdout)
        self.assertIn("--json", result.stdout)

    def test_version_matches_package(self):
        result = cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"link-doctor {link_doctor.__version__}")
        after_action = cli("diagnose", "--version")
        self.assertEqual(after_action.returncode, 0, after_action.stderr)
        self.assertEqual(after_action.stdout.strip(), f"link-doctor {link_doctor.__version__}")

    def test_missing_separator_is_usage_error(self):
        result = cli("diagnose")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("--", result.stderr)


class ExitStatusTests(WorkBase):
    """Exit 0 only for candidate/already_satisfied; 2 for every other status and for usage errors."""

    def setUp(self):
        super().setUp()
        self.build("main", "extern int provider(void); int main(void){return provider();}")
        self.build("provider", "extern int helper(void); int provider(void){return helper();}")
        self.build("helper", "int helper(void){return 0;}")
        for archive, member in (("libprovider.a", "provider.o"), ("libleaf.a", "helper.o")):
            subprocess.run(["ar", "rcs", archive, member], cwd=self.work,
                           capture_output=True, check=True)

    def diagnose(self, *argv):
        return cli("diagnose", "--json", "--", *argv, cwd=self.work)

    def test_candidate_is_zero(self):
        result = self.diagnose("ld", "-e", "main", "-o", "app", "main.o",
                               "libleaf.a", "libprovider.a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "candidate")

    def test_already_satisfied_is_zero(self):
        result = self.diagnose("ld", "-e", "main", "-o", "app", "main.o",
                               "libprovider.a", "libleaf.a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "already_satisfied")

    def test_missing_provider_is_two(self):
        self.build("lonely", "extern int absent(void); int main(void){return absent();}")
        result = self.diagnose("ld", "-e", "main", "-o", "app", "lonely.o")
        self.assertEqual(result.returncode, 2, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["diagnosis"], "missing_provider")

    def test_unsupported_input_is_two(self):
        result = self.diagnose("ld", "-e", "main", "-o", "app", "main.o", "-L.")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "unsupported")

    def test_malformed_usage_is_two(self):
        self.assertEqual(cli("diagnose", "--bogus").returncode, 2)
        self.assertEqual(cli("diagnose", "--").returncode, 2)


class HumanJsonAgreementTests(WorkBase):
    """Human and JSON reports must agree on status, verification state and provider warnings."""

    def setUp(self):
        super().setUp()
        self.build("main", "extern int provider(void); int main(void){return provider();}")
        self.build("provider", "extern int helper(void); int provider(void){return helper();}")
        self.build("helper", "int helper(void){return 0;}")
        for archive, member in (("libprovider.a", "provider.o"), ("libleaf.a", "helper.o")):
            subprocess.run(["ar", "rcs", archive, member], cwd=self.work,
                           capture_output=True, check=True)

    def run_pair(self, *argv):
        human = cli("diagnose", "--", *argv, cwd=self.work)
        machine = cli("diagnose", "--json", "--", *argv, cwd=self.work)
        self.assertEqual(human.returncode, machine.returncode)
        return human.stdout, json.loads(machine.stdout)

    def assert_agreement(self, human, report):
        self.assertIn(f"Status: {report['status']}", human)
        self.assertIn(f"Native verification: {report['verification']['status']}", human)
        if report.get("reason"):
            self.assertIn(report["reason"], human)
        warns = "provider changed for" in human
        self.assertEqual(warns, bool(report.get("provider_changes")), human)
        equivalence = "link success is not program equivalence"
        self.assertEqual(equivalence in human,
                         bool(report.get("ambiguity") or report.get("provider_changes")))

    def test_candidate_agreement(self):
        human, report = self.run_pair("ld", "-e", "main", "-o", "app",
                                      "main.o", "libleaf.a", "libprovider.a")
        self.assertEqual(report["status"], "candidate")
        self.assertEqual(report["schema_version"], 1)
        self.assert_agreement(human, report)
        self.assertIn("Model-validated command:", human)
        self.assertIn(shlex_join(report["command"]), human)

    def test_error_agreement(self):
        human, report = self.run_pair("ld", "-e", "main", "-o", "app", "main.o", "-L.")
        self.assertEqual(report["status"], "unsupported")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["command"], None)
        self.assert_agreement(human, report)

    def test_missing_provider_agreement(self):
        self.build("lonely", "extern int absent(void); int main(void){return absent();}")
        human, report = self.run_pair("ld", "-e", "main", "-o", "app", "lonely.o")
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["diagnosis"], "missing_provider")
        self.assert_agreement(human, report)


def shlex_join(parts):
    import shlex
    return shlex.join(parts)


class ExampleAssetTests(WorkBase):
    """The shipped examples must exist as package assets and run without private material."""

    EXPECTED = ("01-basic-order", "02-cycle", "03-missing-provider",
                "04-ambiguity", "05-unsupported")

    def test_examples_ship_with_readme_and_build_script(self):
        self.assertTrue(EXAMPLES.is_dir(), EXAMPLES)
        for name in self.EXPECTED:
            directory = EXAMPLES / name
            self.assertTrue(directory.is_dir(), directory)
            self.assertTrue((directory / "README.md").is_file(), directory)
            self.assertTrue((directory / "build.sh").is_file(), directory)
            self.assertTrue(list(directory.glob("*.c")), directory)

    def test_basic_order_example_reaches_candidate(self):
        target = self.work / "basic"
        __import__("shutil").copytree(EXAMPLES / "01-basic-order", target)
        subprocess.run(["bash", "build.sh"], cwd=target, capture_output=True, check=True)
        result = cli("diagnose", "--json", "--", "ld", "-e", "main", "-o", "app",
                     "main.o", "libleaf.a", "libprovider.a", cwd=target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "candidate")

    def test_unsupported_example_is_refused(self):
        target = self.work / "unsupported"
        __import__("shutil").copytree(EXAMPLES / "05-unsupported", target)
        subprocess.run(["bash", "build.sh"], cwd=target, capture_output=True, check=True)
        result = cli("diagnose", "--json", "--", "ld", "-e", "main", "-o", "app",
                     "main.o", "-L.", cwd=target)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "unsupported")


class PackagingMetadataTests(unittest.TestCase):
    def test_pyproject_declares_entry_point_and_authorized_mit_license(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        project = pyproject["project"]
        self.assertEqual(project["name"], "link-doctor")
        self.assertEqual(project["scripts"], {"link-doctor": "link_doctor.__main__:main"})
        self.assertIn("requires-python", project)
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["license-files"], ["LICENSE"])
        self.assertIn("Permission is hereby granted", (ROOT / "LICENSE").read_text())
        self.assertNotIn("classifiers", project)
        self.assertEqual(pyproject["build-system"]["build-backend"], "setuptools.build_meta")

    def test_ci_configuration_present_and_pins_python(self):
        workflow = ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(workflow.is_file(), workflow)
        text = workflow.read_text()
        self.assertIn("unittest discover -s tests", text)
        self.assertIn("python-version", text)


if __name__ == "__main__":
    unittest.main()
