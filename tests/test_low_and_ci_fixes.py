"""Regressions for the 2026-10-31 v0.5.1 review's low/edge-case findings.

Each behavioral test carries its native oracle or is explicitly labeled as
fault injection or a parser-level test. CI, README and script items from
the same review section are verified by the hosted workflow, not here.

Review: UnifiedVault/Work/Link_Doctor/Evidence/2026-10-31 v0.5.1
post-release code review.md, "Low and edge cases".
"""
import contextlib
import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from link_doctor import diagnose as engine
from link_doctor import driver
from link_doctor import __main__ as cli


def run(argv, cwd):
    """Native oracle run; the linked output is never executed."""
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def build_object(work, name, body):
    (work / (name + ".c")).write_text(body)
    r = run(["gcc", "-c", name + ".c", "-o", name + ".o"], work)
    assert r.returncode == 0, r.stderr


def build_archive(work, archive, members):
    objects = []
    for name, body in members:
        build_object(work, name, body)
        objects.append(name + ".o")
    r = run(["ar", "rcs", archive, *objects], work)
    assert r.returncode == 0, r.stderr


class ScratchCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="ldoc-low-"))
        cls.addClassCleanup(shutil.rmtree, cls.work, ignore_errors=True)

    def run_cli(self, arguments):
        """Run the real CLI text path in the fixture directory."""
        text = io.StringIO()
        with contextlib.chdir(self.work):
            with mock.patch.object(sys, "argv", ["link-doctor", *arguments]):
                with contextlib.redirect_stdout(text):
                    code = cli.main()
        return code, text.getvalue()


def archive_bytes(size_field, payload, name=b"a.o/"):
    """Hand-crafted archive: valid index, one member with a chosen size text.

    Parser-level fixture (no `ar`): the member payload is inert because
    only archive_members' header validation is under test.
    """
    def header(member, size_text):
        return (member.ljust(16, b" ") + b"0".ljust(12) + b"0".ljust(6)
                + b"0".ljust(6) + b"0".ljust(8) + size_text.ljust(10) + b"`\n")
    data = b"!<arch>\n"
    data += header(b"/", b"0000000004") + b"\x00\x00\x00\x00"
    data += header(name, size_field) + payload
    return data


class ArchiveHeaderDigitsTests(unittest.TestCase):
    """Parser level: archive header numeric fields must be strict ASCII
    digits. Python int() also accepts "+12" and "1_0", parsing a different
    member size than BFD would; such headers must be rejected."""

    def test_underscore_size_field_is_rejected(self):
        with self.assertRaises(engine.Rejected) as raised:
            engine.archive_members(archive_bytes(b"1_0", b"A" * 10))
        self.assertIn("malformed archive fields", str(raised.exception))

    def test_signed_size_field_is_rejected(self):
        with self.assertRaises(engine.Rejected) as raised:
            engine.archive_members(archive_bytes(b"+000000010", b"A" * 10))
        self.assertIn("malformed archive fields", str(raised.exception))

    def test_digits_size_field_still_parses(self):
        members, index = engine.archive_members(
            archive_bytes(b"0000000010", b"A" * 10))
        self.assertEqual([m[1] for m in members], ["a.o"])
        self.assertEqual(index, b"\x00\x00\x00\x00")


class MissedProviderTests(ScratchCase):
    """gcc mode reported only the first provider for an unresolved symbol
    while ld mode listed all of them; the reports must agree."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "mp",
                     "extern int f(void);\nint main(void){return f();}\n")
        build_archive(cls.work, "A.a",
                     [("a", "extern int w(void);\nint f(void){return w();}\n")])
        build_archive(cls.work, "B.a", [("b", "int w(void){return 1;}\n")])
        build_archive(cls.work, "C.a", [("c", "int w(void){return 2;}\n")])

    def test_gcc_mode_lists_all_missed_providers(self):
        # The reorder repair exists, but the missed-provider facts describe
        # the original order and must list every provider, as ld mode does.
        report = driver.diagnose(
            ["gcc", "mp.o", "B.a", "C.a", "A.a", "-o", "x"], cwd=self.work)
        self.assertEqual(report["status"], "candidate", report.get("reason"))
        providers = {entry["provider"].split("/")[-1] for entry in report["missed"]}
        self.assertEqual(providers, {"B.a(b.o)", "C.a(c.o)"}, report["missed"])
        self.assertEqual(len(report["missed"]), 2, report["missed"])

    def test_ld_mode_still_lists_all_missed_providers(self):
        report = engine.diagnose(
            ["ld.bfd", "-e", "main", "-o", "OUT", "mp.o", "B.a", "C.a", "A.a"],
            cwd=self.work)
        providers = {entry["provider"] for entry in report["missed"]}
        self.assertEqual(providers, {"B.a(b.o)", "C.a(c.o)"}, report["missed"])


class ResponseFileTests(ScratchCase):
    """A missing @response file is a command-intake problem, not a tool
    failure: it must classify as unsupported, not execution_failure."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "rsp", "int main(void){return 0;}\n")

    def test_missing_response_file_is_unsupported(self):
        report = driver.diagnose(
            ["gcc", "@missing.rsp", "rsp.o", "-o", "x"], cwd=self.work)
        self.assertEqual(report["status"], "unsupported", report.get("reason"))
        self.assertIn("response", report["reason"])


class InternalErrorTests(ScratchCase):
    """Fault injection: an unexpected internal exception must fail closed
    as an execution_failure with the exception named, never a traceback
    or a nonzero-for-the-wrong-reason exit."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "boom", "int main(void){return 0;}\n")

    def test_ld_mode_internal_error_fails_closed(self):
        with mock.patch.object(engine, "native",
                               side_effect=RuntimeError("injected internal error")):
            report = engine.diagnose(
                ["ld.bfd", "-e", "main", "-o", "OUT", "boom.o"],
                cwd=self.work)
        self.assertEqual(report["status"], "execution_failure", report)
        self.assertIn("RuntimeError", report["reason"])
        self.assertIn("injected internal error", report["reason"])
        self.assertIsNone(report["command"])
        self.assertFalse(report["verified"])

    def test_gcc_mode_internal_error_fails_closed(self):
        with mock.patch.object(engine, "native",
                               side_effect=RuntimeError("injected internal error")):
            report = driver.diagnose(["gcc", "boom.o", "-o", "x"], cwd=self.work)
        self.assertEqual(report["status"], "execution_failure", report)
        self.assertIn("RuntimeError", report["reason"])

    def test_cli_internal_error_exits_two(self):
        with mock.patch.object(engine, "native",
                               side_effect=RuntimeError("injected internal error")):
            code, text = self.run_cli(
                ["diagnose", "--", "ld.bfd", "-e", "main", "-o", "OUT", "boom.o"])
        self.assertEqual(code, 2)
        self.assertIn("execution_failure", text)
        self.assertNotIn("Traceback", text)


class ConstructorSectionTests(ScratchCase):
    """ld mode rejected .init_array/.fini_array/.preinit_array sections
    while gcc mode accepted them, yet bare ld links them natively; the
    constructor-section policy must agree."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "ctor",
                     "void __attribute__((constructor)) setup(void){}\n"
                     "int main(void){return 0;}\n")

    def test_native_bare_ld_accepts_constructor_sections(self):
        native = run(["ld.bfd", "-e", "main", "-o", "native-ctor",
                      "ctor.o"], self.work)
        self.assertEqual(native.returncode, 0, native.stderr)

    def test_ld_mode_accepts_constructor_sections(self):
        report = engine.diagnose(
            ["ld.bfd", "-e", "main", "-o", "OUT", "ctor.o"],
            verify=True, cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))
        self.assertTrue(report["verified"], report)

    def test_gcc_mode_still_accepts_constructor_sections(self):
        native = run(["gcc", "ctor.o", "-o", "native-ctor-gcc"], self.work)
        self.assertEqual(native.returncode, 0, native.stderr)
        report = driver.diagnose(["gcc", "ctor.o", "-o", "x"],
                                 verify=True, cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))


class DriverGlobalStateTests(ScratchCase):
    """template() wrote the resolved compiler into the module-level TOOLS
    table; a diagnosis must not mutate module state."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "gs", "int main(void){return 0;}\n")

    def test_diagnose_does_not_mutate_tools(self):
        before = dict(engine.TOOLS)
        report = driver.diagnose(["gcc", "gs.o", "-o", "x"], cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))
        self.assertEqual(engine.TOOLS, before)
        # trusted() resolves the compiler symlink to its versioned target.
        self.assertIn("gcc", Path(report["discovery"]["driver"]).name,
                      report["discovery"])


class VisitNoiseTests(ScratchCase):
    """gcc mode repeated near-identical archive-visit lines once per group
    pass; identical visit records carry no evidence and must appear once."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "vn", "int main(void){return 0;}\n")

    def test_no_identical_duplicate_archive_visits(self):
        report = driver.diagnose(["gcc", "vn.o", "-o", "x"], cwd=self.work)
        visits = report["original"]["archive_visits"]
        entries = [(v["archive"], tuple(v["needed_on_arrival"]),
                    tuple(v["not_extracted"])) for v in visits]
        self.assertEqual(len(entries), len(set(entries)), visits)
        # Evidence is retained: distinct states of the same archive stay.
        self.assertTrue(any(v["archive"] == "-lgcc" for v in visits), visits)


if __name__ == "__main__":
    unittest.main()
