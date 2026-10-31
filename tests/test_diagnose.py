"""Native end-to-end tests using newly authored C, never executed binaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from unittest.mock import patch
from link_doctor import diagnose as engine

ROOT = Path(__file__).resolve().parents[1]


class DiagnoseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="doctor test ")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        for name in ("main", "middle", "leaf"):
            self.compile(name, (ROOT / "tests/fixtures/two_archives" / (name + ".c")).read_text())
        self.archive("libmiddle.a", "middle.o")
        self.archive("libleaf.a", "leaf.o")
        self.command = ["ld.bfd", "-e", "main", "-o", "untouched out", "main.o", "libleaf.a", "libmiddle.a"]
        (self.work / "untouched out").write_bytes(b"sentinel")

    def compile(self, name, source, extra=()):
        (self.work / (name + ".c")).write_text(source)
        self.native(["gcc", "-c", "-fno-pie", "-fno-stack-protector", *extra, name + ".c", "-o", name + ".o"])

    def archive(self, name, *members):
        self.native(["ar", "rcs", name, *members])

    def native(self, command):
        return subprocess.run(command, cwd=self.work, capture_output=True, text=True, check=True, timeout=20)

    def cli(self, command=None, *options):
        result = subprocess.run([sys.executable, "-m", "link_doctor", "diagnose", "--json", *options, "--", *(command or self.command)], cwd=self.work,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=30)
        self.assertIn(result.returncode, (0, 2), result.stderr)
        return json.loads(result.stdout)

    def test_two_archive_diagnosis_preserves_inputs_without_linking(self):
        # Flattening archives or always claiming failure cannot pass both orders.
        before = {p.name: p.read_bytes() for p in self.work.iterdir()}
        report = self.cli()
        self.assertEqual(report["status"], "candidate")
        self.assertFalse(report["verified"])
        self.assertEqual(report["verification"]["status"], "not_requested")
        self.assertEqual(report["original"]["unresolved"], ["leaf"])
        self.assertEqual(report["missed"], [{"symbol": "leaf", "consumer": "libmiddle.a(middle.o)", "provider": "libleaf.a(leaf.o)"}])
        self.assertEqual(report["command"], self.command[:-2] + ["libmiddle.a", "libleaf.a"])
        self.assertEqual(report["candidate"]["unresolved"], [])
        self.assertEqual({p.name: p.read_bytes() for p in self.work.iterdir()}, before)
        good = self.cli(report["command"])
        self.assertEqual(good["status"], "already_satisfied")
        self.assertEqual(good["original"]["unresolved"], [])

    def test_opt_in_verifies_both_orders_without_changing_any_files(self):
        before = {p.name: p.read_bytes() for p in self.work.iterdir()}
        report = self.cli(None, "--verify")
        self.assertTrue(report["verified"], report)
        self.assertEqual(report["verification"]["status"], "passed")
        original = report["verification"]["original"]
        self.assertNotEqual(original["returncode"], 0)
        self.assertIn("leaf", original["stderr"])
        self.assertEqual(report["verification"]["candidate"]["returncode"], 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.work.iterdir()}, before)

    def test_intra_archive_fixed_point_and_unused_members(self):
        self.compile("unneeded", "extern int absent(void); int unused(void){return absent();}")
        self.archive("combined.a", "leaf.o", "middle.o", "unneeded.o")
        report = self.cli(self.command[:6] + ["combined.a"], "--verify")
        self.assertEqual(report["status"], "already_satisfied", report)
        self.assertTrue(report["verified"])
        self.assertEqual(report["original"]["extracted"], ["combined.a(leaf.o)", "combined.a(middle.o)", "main.o"])
        self.assertNotIn("absent", report["original"]["unresolved"])

    def test_entry_symbol_extracts_archive_member(self):
        # Native GNU ld.bfd roots an explicit -e symbol, even without objects.
        command = ["ld.bfd", "-e", "middle", "-o", "untouched out", "libleaf.a", "libmiddle.a"]
        report = self.cli(command, "--verify")
        self.assertEqual(report["status"], "candidate", report)
        self.assertEqual(report["original"]["unresolved"], ["leaf"])
        self.assertEqual(report["command"][-2:], ["libmiddle.a", "libleaf.a"])
        self.assertTrue(report["verified"], report)
        self.assertEqual(report["verification"]["original"]["returncode"], 1)
        self.assertEqual(report["verification"]["candidate"]["returncode"], 0)

    def test_missing_provider_and_missing_entry_are_unknown(self):
        for command in (self.command[:-2] + ["libmiddle.a"], [*self.command[:2], "absent_entry", *self.command[3:]]):
            with self.subTest(command=command):
                report = self.cli(command, "--verify")
                self.assertEqual(report["status"], "unknown", report)
                self.assertIsNone(report["command"])
                self.assertFalse(report["verified"])

    def test_flags_and_objects_are_barriers(self):
        command = ["ld.bfd", "-e", "main", "main.o", "libleaf.a", "-o", "untouched out", "libmiddle.a"]
        report = self.cli(command)
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["search"]["attempts"], 1)

    def test_no_native_link_without_opt_in(self):
        with patch.object(engine, "native", wraps=engine.native) as calls:
            report = engine.diagnose(self.command, cwd=self.work)
        self.assertEqual(report["status"], "candidate")
        self.assertTrue(calls.call_args_list)
        self.assertEqual({call.args[0] for call in calls.call_args_list}, {"readelf"})

    def test_tool_errors_and_timeouts_fail_closed(self):
        with patch.dict(engine.TOOLS, {"readelf": str(self.work / "missing")}):
            report = engine.diagnose(self.command, cwd=self.work)
        self.assertIsNone(report["command"])
        with patch.object(engine.subprocess, "run", side_effect=subprocess.TimeoutExpired("readelf", 10)):
            report = engine.diagnose(self.command, cwd=self.work)
        self.assertIsNone(report["command"])
        actual = engine.native
        def fail_link(tool, args, cwd):
            if tool == "ld.bfd":
                raise engine.ExecutionFailure("linker timeout")
            return actual(tool, args, cwd)
        with patch.object(engine, "native", side_effect=fail_link):
            report = engine.diagnose(self.command, verify=True, cwd=self.work)
        self.assertIsNone(report["command"])
        self.assertFalse(report["verified"])
        self.assertEqual(report["verification"]["status"], "error")

    def test_native_failure_withholds_candidate(self):
        actual = engine.native
        def failed(tool, args, cwd):
            return (1, "", "link failure") if tool == "ld.bfd" else actual(tool, args, cwd)
        with patch.object(engine, "native", side_effect=failed):
            report = engine.diagnose(self.command, verify=True, cwd=self.work)
        self.assertEqual(report["status"], "unknown")
        self.assertIsNone(report["command"])
        self.assertEqual(report["verification"]["status"], "failed")

    def test_rejected_command_surface_does_not_spawn(self):
        commands = [["/usr/bin/ld.bfd", *self.command[1:]], ["gcc", *self.command[1:]]]
        commands += [self.command + [flag] for flag in ("@response", "--plugin=evil", "-T", "--gc-sections", "-Map=untouched out", "--dependency-file=untouched out", "--start-group", "-lfoo", "-L.", "-r", "--whole-archive")]
        commands += [self.command + ["-o", "second"], self.command + ["-e", "main"]]
        for command in commands:
            with self.subTest(command=command), patch.object(engine, "native") as calls:
                report = engine.diagnose(command, verify=True, cwd=self.work)
                self.assertEqual(report["status"], "unsupported")
                self.assertIsNone(report["command"])
                calls.assert_not_called()

    def test_unsupported_inputs_and_semantics(self):
        sources = {"weak": "__attribute__((weak)) int odd(void){return 1;}",
                   "common": "int odd;", "tls": "__thread int odd;",
                   "hidden": '__attribute__((visibility("hidden"))) int odd(void){return 1;}'}
        for name, source in sources.items():
            self.compile(name, source, ("-fcommon",) if name == "common" else ())
            report = self.cli(self.command + [name + ".o"])
            # Phase 4 admits ordinary weak/common ELF symbols, not TLS/visibility.
            self.assertEqual(report["status"], "candidate" if name in ('weak','common') else "unsupported", (name, report))
            if name in ('weak','common'):
                verified = self.cli(self.command + [name + '.o'], '--verify')
                self.assertTrue(verified['verified'], verified)
        self.compile("lto", "int odd(void){return 1;}", ("-flto",))
        for filename, data in (("script.o", b"INPUT(main.o)"), ("bad.o", b"\x7fELFbroken")):
            (self.work / filename).write_bytes(data)
        for filename in ("lto.o", "script.o", "bad.o", "missing.o"):
            with self.subTest(filename=filename):
                report = self.cli(self.command + [filename], "--verify")
                self.assertEqual(report["status"], "execution_failure" if filename == "missing.o" else "unsupported", report)
                self.assertIsNone(report["command"])
        self.native(["ar", "rcsT", "thin.a", "leaf.o"])
        self.native(["ar", "rcs", "metadata.a", "--record-libdeps=-levil", "leaf.o"])
        (self.work / "script.txt").write_text("INPUT(evil.o)")
        self.archive("script.a", "leaf.o", "script.txt")
        self.native(["ar", "qc", "duplicate.a", "leaf.o", "leaf.o"])
        self.native(["ar", "s", "duplicate.a"])
        self.native(["ar", "rcS", "unindexed.a", "leaf.o"])
        for filename in ("thin.a", "metadata.a", "script.a", "duplicate.a", "unindexed.a"):
            with self.subTest(filename=filename):
                report = self.cli(self.command[:-2] + [filename, "libmiddle.a"], "--verify")
                self.assertEqual(report["status"], "unsupported", report)
                self.assertIsNone(report["command"])

    def test_alternative_definitions_in_unextracted_members_are_visible(self):
        self.compile("duplicate", "int leaf(void){return 100;}")
        self.archive("duplicate.a", "duplicate.o")
        report = self.cli(self.command + ["duplicate.a"], '--verify')
        self.assertEqual(report["status"], "already_satisfied")
        self.assertTrue(report['verified'], report)
        self.assertIn('leaf', report['ambiguity'])
        self.assertEqual(report['candidate']['selected_providers']['leaf']['member'], 'duplicate.a(duplicate.o)')

    def test_long_member_names_and_input_path_spaces(self):
        longname = "unusually long member filename"
        self.compile(longname, "int leaf(void){return 4;}")
        self.archive("leaf space.a", longname + ".o")
        report = self.cli(self.command[:-2] + ["leaf space.a", "libmiddle.a"], "--verify")
        self.assertTrue(report["verified"], report)
        self.assertEqual(report["missed"][0]["provider"], "leaf space.a(" + longname + ".o)")

    def test_bounded_search_honestly_unknown(self):
        with patch.object(engine, "SEARCH_LIMIT", 1):
            report = engine.diagnose(self.command, cwd=self.work)
        self.assertEqual(report["status"], "unknown")
        self.assertIsNone(report["command"])
        self.assertEqual(report["search"]["attempts"], 1)

    def test_report_explains_missed_extraction_with_real_member_facts(self):
        report = self.cli()
        visit = report["original"]["archive_visits"][0]
        self.assertEqual(visit, {"archive": "libleaf.a", "needed_on_arrival": ["middle"],
                                "not_extracted": ["libleaf.a(leaf.o)"]})
        facts = report["inputs"][2]["members"][0]
        self.assertEqual(facts["defines"], ["middle"])
        self.assertEqual(facts["requires"], ["leaf"])
        self.assertEqual(facts["identity"], "libmiddle.a(middle.o)")

    def test_three_archive_contrast_and_object_barrier(self):
        self.compile("leaf", "extern int bottom(void); int leaf(void){return bottom();}")
        self.compile("bottom", "int bottom(void){return 11;}")
        self.archive("libleaf.a", "leaf.o")
        self.archive("libbottom.a", "bottom.o")
        command = self.command[:6] + ["libbottom.a", "libleaf.a", "libmiddle.a"]
        report = self.cli(command, "--verify")
        self.assertTrue(report["verified"], report)
        self.assertEqual(report["command"][-3:], ["libmiddle.a", "libleaf.a", "libbottom.a"])
        self.compile("neutral", "int neutral(void){return 0;}")
        barrier = self.command[:6] + ["libbottom.a", "libleaf.a", "neutral.o", "libmiddle.a"]
        self.assertEqual(self.cli(barrier)["status"], "unknown")

    def test_output_aliasing_input_is_redirected_not_overwritten(self):
        before = (self.work / "main.o").read_bytes()
        command = self.command.copy()
        command[4] = "main.o"
        self.assertTrue(self.cli(command, "--verify")["verified"])
        self.assertEqual((self.work / "main.o").read_bytes(), before)

    def test_partial_native_parse_is_rejected(self):
        actual = engine.native
        def incomplete(tool, args, cwd):
            code, text, errors = actual(tool, args, cwd)
            # Fault injection: emulate an unrecognized native symbol-table row.
            return code, "\n".join(line for line in text.splitlines() if "FUNC    GLOBAL" not in line), errors
        with patch.object(engine, "native", side_effect=incomplete):
            report = engine.diagnose(self.command[:6] + ["leaf.o", "middle.o"], cwd=self.work)
        self.assertEqual(report["status"], "unsupported", report)
        self.assertIn("symbol table", report["reason"])

    def test_partial_section_parse_is_rejected(self):
        actual = engine.native
        def incomplete(tool, args, cwd):
            code, text, errors = actual(tool, args, cwd)
            return code, "\n".join(line for line in text.splitlines() if ".comment" not in line), errors
        with patch.object(engine, "native", side_effect=incomplete):
            report = engine.diagnose(self.command, cwd=self.work)
        self.assertEqual(report["status"], "unsupported", report)
        self.assertIn("section", report["reason"])

    def test_execution_errors_are_not_unsupported_semantics(self):
        with patch.dict(engine.TOOLS, {"readelf": str(self.work / "missing")}):
            report = engine.diagnose(self.command, cwd=self.work)
        self.assertEqual(report["status"], "execution_failure", report)
        with patch.object(engine.subprocess, "run", side_effect=subprocess.TimeoutExpired("readelf", 10)):
            report = engine.diagnose(self.command, cwd=self.work)
        self.assertEqual(report["status"], "execution_failure", report)
        missing = engine.diagnose(self.command + ["missing.o"], cwd=self.work)
        self.assertEqual(missing["status"], "execution_failure", missing)

    def test_control_characters_and_oversized_argv_rejected(self):
        for output in ("bad\noutput", "a" * 8193):
            command = self.command.copy()
            command[4] = output
            self.assertEqual(self.cli(command)["status"], "unsupported")

    def test_corrupt_index_and_nonregular_input_fail_closed(self):
        data = bytearray((self.work / "libleaf.a").read_bytes())
        # GNU archive's first index: magic(8), header(60), count(4), member offset.
        data[72:76] = b"\0\0\0\0"
        (self.work / "corrupt.a").write_bytes(data)
        self.assertEqual(self.cli(self.command[:-2] + ["corrupt.a", "libmiddle.a"])["status"], "unsupported")
        os.mkfifo(self.work / "fifo.o")
        self.assertEqual(self.cli(self.command + ["fifo.o"])["status"], "unsupported")


if __name__ == "__main__":
    unittest.main()
