"""Regressions for the 2026-10-31 v0.5.1 post-release code review findings 1-8.

Each test builds its own fixture in a fresh scratch directory, establishes
the native oracle with a real disposable link (never executes the output),
and then asserts the tool agrees with that oracle. Fault-injection and
synthetic-model tests are labeled as such in their docstrings; they add
evidence for a specific finding and are not native oracles.

Review: UnifiedVault/Work/Link_Doctor/Evidence/2026-10-31 v0.5.1
post-release code review.md
"""
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
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


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ScratchCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="ldoc-review-"))
        cls.addClassCleanup(shutil.rmtree, cls.work, ignore_errors=True)

    def run_cli(self, arguments):
        """Run the real CLI text path in the fixture directory."""
        text = io.StringIO()
        with contextlib.chdir(self.work):
            with mock.patch.object(sys, "argv", ["link-doctor", *arguments]):
                with contextlib.redirect_stdout(text):
                    code = cli.main()
        return code, text.getvalue()


class ForwardedSearchOrderTests(ScratchCase):
    """Finding 1: -Wl,-L forwarded directories search after the driver's own
    -L directories, and the model-validated command must keep the -Wl
    spelling, because respelling it as -L changes native selection."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "fwd",
                     "extern double cos(double);\n"
                     "int main(void){return (int)cos(0.0);}\n")
        (cls.work / "shadow").mkdir()
        build_object(cls.work / "shadow", "s",
                     "double cos(double x){(void)x;return 0.5;}\n")
        r = run(["ar", "rcs", "libm.a", "s.o"], cls.work / "shadow")
        assert r.returncode == 0, r.stderr

    def test_native_forwarded_L_searches_after_driver_dirs(self):
        # Native oracle: the forwarded directory is too late; the system
        # libm wins. The respelled contrast picks shadow/libm.a instead.
        forwarded = run(["gcc", "fwd.o", "-Wl,-L./shadow", "-lm",
                         "-Wl,--trace", "-o", "native-fwd"], self.work)
        self.assertEqual(forwarded.returncode, 0, forwarded.stderr)
        self.assertFalse([l for l in forwarded.stdout.splitlines()
                          if "shadow/libm.a" in l], forwarded.stdout)
        self.assertTrue([l for l in forwarded.stdout.splitlines()
                         if l.endswith("/libm.so")], forwarded.stdout)
        respelled = run(["gcc", "fwd.o", "-L./shadow", "-lm",
                        "-Wl,--trace", "-o", "native-direct"], self.work)
        self.assertEqual(respelled.returncode, 0, respelled.stderr)
        self.assertTrue([l for l in respelled.stdout.splitlines()
                         if "shadow/libm.a" in l], respelled.stdout)

    def test_model_matches_native_selection_for_forwarded_L(self):
        report = driver.diagnose(
            ["gcc", "fwd.o", "-Wl,-L./shadow", "-lm", "-o", "p2"],
            verify=True, cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))
        receipt = next(r for r in report["inventory"] if r["selection"] == "-lm")
        self.assertNotIn("shadow", receipt["path"], receipt)
        self.assertTrue(receipt["path"].endswith("/libm.so"), receipt["path"])
        # The forwarded directory is searched last, after the driver's.
        self.assertEqual(report["search_paths"][-1],
                         str((self.work / "shadow").resolve()),
                         report["search_paths"])
        # The model-validated command must not respell the flag.
        self.assertIn("-Wl,-L./shadow", report["normalized_command"])
        self.assertTrue(report["verified"])

    def test_direct_L_still_searched_before_driver_dirs(self):
        # Contrast guard: a direct driver -L is searched first, and the
        # canonical command keeps the user's -L spelling.
        report = driver.diagnose(
            ["gcc", "fwd.o", "-L./shadow", "-lm", "-o", "p3"],
            verify=True, cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))
        receipt = next(r for r in report["inventory"] if r["selection"] == "-lm")
        self.assertTrue(receipt["path"].endswith("shadow/libm.a"), receipt["path"])


class OutputAliasingTests(ScratchCase):
    """Finding 2: a command whose output path equals an input path fails
    natively regardless of order; the tool must not report it as satisfied."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "alias",
                     "extern int f(void);\nint main(void){return f();}\n")
        build_object(cls.work, "aliasf", "int f(void){return 1;}\n")

    def test_ld_rejects_output_equal_to_input(self):
        native = run(["ld.bfd", "-e", "main", "-o", "alias.o",
                      "alias.o", "aliasf.o"], self.work)
        self.assertNotEqual(native.returncode, 0, native.stdout)
        self.assertIn("is the same as output file", native.stderr)
        before = sha(self.work / "alias.o")
        report = engine.diagnose(["ld.bfd", "-e", "main", "-o", "alias.o",
                                  "alias.o", "aliasf.o"],
                                 verify=True, cwd=self.work)
        self.assertEqual(report["status"], "unsupported", report.get("reason"))
        self.assertIn("same as", report["reason"])
        self.assertFalse(report["verified"])
        self.assertEqual(sha(self.work / "alias.o"), before)

    def test_gcc_rejects_output_equal_to_input(self):
        native = run(["gcc", "alias.o", "aliasf.o", "-o", "alias.o"], self.work)
        self.assertNotEqual(native.returncode, 0, native.stdout)
        self.assertIn("is the same as output file", native.stderr)
        before = sha(self.work / "alias.o")
        report = driver.diagnose(["gcc", "alias.o", "aliasf.o", "-o", "alias.o"],
                                 verify=True, cwd=self.work)
        self.assertEqual(report["status"], "unsupported", report.get("reason"))
        self.assertIn("same as", report["reason"])
        self.assertFalse(report["verified"])
        self.assertEqual(sha(self.work / "alias.o"), before)


class WithheldCandidateTests(ScratchCase):
    """Finding 3, fault injection: when native verification rejects a
    candidate, its argv edits must not remain actionable report fields or
    text output; they move under an explicit withheld_candidate key."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "wc", "extern int f0(void);\nint main(void){return f0();}\n")
        build_archive(cls.work, "A.a", [("a", "extern int f1(void);\nint f0(void){return f1();}\n")])
        build_archive(cls.work, "B.a", [("b", "int f1(void){return 1;}\n")])
        # Reversed on purpose: the original order leaves f1 unresolved, so
        # a model-valid repair exists and can be withheld by verification.
        cls.command = ["ld.bfd", "-e", "main", "-o", "OUT",
                       "wc.o", "B.a", "A.a"]

    def test_withheld_candidate_is_not_actionable(self):
        real_native = engine.native
        def injected(tool, args, cwd):
            if any(str(a).endswith("candidate.out") for a in args):
                return 1, "", "injected native failure for the candidate output"
            return real_native(tool, args, cwd)
        with mock.patch.object(engine, "native", injected):
            report = engine.diagnose(self.command, verify=True, cwd=self.work)
        self.assertEqual(report["status"], "unknown", report.get("reason"))
        self.assertIsNone(report["command"])
        # The original order genuinely fails natively; the candidate's
        # failure is the injection.
        self.assertEqual(report["verification"]["original"]["returncode"], 1)
        self.assertIn("f1", report["verification"]["original"]["stderr"])
        self.assertEqual(report["verification"]["candidate"]["returncode"], 1)
        for key in ("changes", "repair", "candidate", "provider_changes"):
            self.assertNotIn(key, report, key)
        withheld = report["withheld_candidate"]
        self.assertEqual(withheld["repair"]["kind"], "reorder")
        self.assertTrue(withheld["changes"])
        json.dumps(report)  # still serializable evidence

    def test_withheld_candidate_prints_no_edits(self):
        real_native = engine.native
        def injected(tool, args, cwd):
            if any(str(a).endswith("candidate.out") for a in args):
                return 1, "", "injected native failure for the candidate output"
            return real_native(tool, args, cwd)
        with mock.patch.object(engine, "native", injected):
            code, text = self.run_cli(
                ["diagnose", "--verify", "--", *self.command])
        self.assertEqual(code, 2)
        self.assertNotIn("argv[", text)
        self.assertNotIn("Repair strategy", text)
        self.assertNotIn("Model-validated command", text)
        self.assertIn("withheld", text)


class BudgetAllocationTests(ScratchCase):
    """Finding 4: reorder candidates are tried fewest-moves first, and the
    permutation strategy may not consume the whole attempt budget, so a
    seven-archive span whose fix is a single swap of the first two archives
    is still found (6! >= 720 starved it before)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "seed", "extern int s0(void);\nint main(void){return s0();}\n")
        cls.archives = []
        for i in range(7):
            body = (f"extern int s{i+1}(void);\nint s{i}(void){{return s{i+1}();}}\n"
                    if i < 6 else f"int s{i}(void){{return {i};}}\n")
            build_archive(cls.work, f"lib{i}.a", [(f"m{i}", body)])
            cls.archives.append(f"lib{i}.a")
        # Six-archive contrast chain whose last member requires nothing.
        cls.archives6 = []
        for i in range(6):
            body = (f"extern int s{i+1}(void);\nint s{i}(void){{return s{i+1}();}}\n"
                    if i < 5 else f"int s{i}(void){{return {i};}}\n")
            build_archive(cls.work, f"short{i}.a", [(f"n{i}", body)])
            cls.archives6.append(f"short{i}.a")

    def test_single_swap_of_first_archives_found_in_seven_archive_span(self):
        seven = ["lib1.a", "lib0.a", *self.archives[2:]]
        report = engine.diagnose(
            ["ld.bfd", "-e", "main", "-o", "OUT", "seed.o", *seven],
            verify=True, cwd=self.work)
        self.assertEqual(report["status"], "candidate", report.get("reason"))
        self.assertEqual(report["repair"]["kind"], "reorder", report["repair"])
        self.assertLessEqual(report["search"]["attempts"], 64,
                             report["search"])
        self.assertEqual(report["command"][6:8], ["lib0.a", "lib1.a"],
                         report["command"])
        self.assertTrue(report["verified"])

    def test_six_archive_contrast_still_repairs(self):
        six = ["short1.a", "short0.a", *self.archives6[2:]]
        report = engine.diagnose(
            ["ld.bfd", "-e", "main", "-o", "OUT", "seed.o", *six],
            verify=True, cwd=self.work)
        self.assertEqual(report["status"], "candidate", report.get("reason"))
        self.assertEqual(report["repair"]["kind"], "reorder", report["repair"])
        self.assertTrue(report["verified"])


class CommonDemandCostTests(unittest.TestCase):
    """Finding 5, synthetic model test (no filesystem): the set of pending
    common symbols is maintained incrementally in include() instead of
    rescanning every selected symbol for every archive member on every
    pass. Members are stored in reverse order so extraction takes one pass
    per member across a runtime library with 80,000 selected symbols."""

    def test_common_demand_scan_is_incremental(self):
        count, wide = 48, 80000
        shared = driver.Node("libbig.so", kind="shared",
                             members=[driver.Unit("libbig",
                                                  {f"g{i}" for i in range(wide)},
                                                  set())])
        obj = driver.Node("obj.o", kind="object",
                          members=[driver.Unit("obj", {"_start", "c"}, {"p0"},
                                               commons={"c"})])
        members = [driver.Unit(f"a({i}.o)", {f"p{i}"},
                               ({f"p{i+1}"} if i + 1 < count else set()),
                               data_defines={f"d{i}"})
                   for i in range(count)][::-1]
        archive = driver.Node("chain.a", kind="archive", members=members)
        start = time.perf_counter()
        result = driver.model([shared, obj, archive])
        elapsed = time.perf_counter() - start
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(len(result["extracted"]), count + 2)
        self.assertEqual(result["selected_providers"]["c"]["strength"], "common")
        self.assertLess(elapsed, 1.0,
                        f"model took {elapsed:.2f}s; common demand is not incremental")


class TerminalInjectionTests(ScratchCase):
    """Finding 6: file-derived text can carry raw terminal control bytes.
    binutils 2.46 readelf renders them as caret notation, but the native
    linker echoes them raw in its stderr, which the CLI prints for
    verification results; text output must escape them everywhere."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Written as bytes so the symbol really contains ESC and BEL;
        # shell printf escapes would emit literal backslashes and prove
        # nothing.
        (cls.work / "evil.s").write_bytes(
            b'\t.text\n\t.globl main\nmain:\n'
            b'\tcall\t"\x1b[31mEVIL\x07"\n\tret\n')
        r = run(["gcc", "-c", "evil.s", "-o", "evil.o"], cls.work)
        assert r.returncode == 0, r.stderr
        build_object(cls.work, "wc", "extern int f0(void);\nint main(void){return f0();}\n")
        build_archive(cls.work, "A.a", [("a", "extern int f1(void);\nint f0(void){return f1();}\n")])
        build_archive(cls.work, "B.a", [("b", "int f1(void){return 1;}\n")])
        cls.command = ["ld.bfd", "-e", "main", "-o", "OUT",
                       "wc.o", "B.a", "A.a"]

    def test_native_stderr_really_carries_control_bytes(self):
        # Native oracle: ld echoes the symbol's raw ESC/BEL bytes.
        native = subprocess.run(["ld.bfd", "-e", "main", "-o", "n.out",
                                  "evil.o"], cwd=self.work, capture_output=True)
        self.assertNotEqual(native.returncode, 0)
        self.assertIn(b"\x1b[31mEVIL\x07", native.stderr)

    def test_ld_mode_rejection_stays_control_safe(self):
        # readelf caret-escapes the symbol, so this message must stay free
        # of control bytes in both the current and fixed tool.
        code, text = self.run_cli(
            ["diagnose", "--", "ld.bfd", "-e", "main", "-o", "OUT", "evil.o"])
        self.assertEqual(code, 2)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("EVIL", text)

    def test_verification_stderr_is_escaped(self):
        # Fault injection with real bytes: native ld's own stderr (raw
        # control bytes, per the oracle above) is injected as the
        # candidate's verification failure.
        native = subprocess.run(["ld.bfd", "-e", "main", "-o", "n.out",
                                  "evil.o"], cwd=self.work, capture_output=True)
        raw_stderr = native.stderr.decode("utf-8", "replace")
        real_native = engine.native
        def injected(tool, args, cwd):
            if any(str(a).endswith("candidate.out") for a in args):
                return 1, "", raw_stderr
            return real_native(tool, args, cwd)
        with mock.patch.object(engine, "native", injected):
            code, text = self.run_cli(["diagnose", "--verify", "--",
                                       *self.command])
        self.assertEqual(code, 2)
        self.assertIn("EVIL", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("\\x1b[31mEVIL\\x07", text)


class LinkerScriptSymbolTests(ScratchCase):
    """Finding 7: the default linker script provides its own symbols
    (_end, __bss_start, __ehdr_start, ...), and orphan-section handling
    provides __start_/__stop_ bounds for a named section that is present.
    None of these come from archive members, so they must not be reported
    as a missing provider; a bound for an absent section stays unresolved."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.work / "ldsym.s").write_text(
            '\t.section mysec,"a"\n'
            '\t.globl mysec_marker\nmysec_marker:\n\t.byte 7\n'
            '\t.section .rodata.extras,"a"\n'
            '\t.quad __start_mysec\n'
            '\t.quad __stop_mysec\n'
            '\t.quad _end\n'
            '\t.quad __bss_start\n'
            '\t.quad __ehdr_start\n'
            '\t.text\n\t.globl main\nmain:\n\tret\n')
        r = run(["gcc", "-c", "ldsym.s", "-o", "ldsym.o"], cls.work)
        assert r.returncode == 0, r.stderr
        (cls.work / "nosec.s").write_text(
            '\t.section .rodata.neg,"a"\n'
            '\t.quad __start_nosec\n'
            '\t.text\n\t.globl main\nmain:\n\tret\n')
        r = run(["gcc", "-c", "nosec.s", "-o", "nosec.o"], cls.work)
        assert r.returncode == 0, r.stderr

    def test_gcc_mode_resolves_script_symbols(self):
        native = run(["gcc", "ldsym.o", "-o", "native-gcc"], self.work)
        self.assertEqual(native.returncode, 0, native.stderr)
        symbols = run(["readelf", "-Ws", "native-gcc"], self.work)
        self.assertIn("__start_mysec", symbols.stdout)
        report = driver.diagnose(["gcc", "ldsym.o", "-o", "p7"],
                                 verify=True, cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))
        self.assertEqual(report["original"]["unresolved"], [])
        self.assertTrue(report["verified"])

    def test_ld_mode_resolves_script_symbols(self):
        native = run(["ld.bfd", "-e", "main", "-o", "native-ld",
                      "ldsym.o"], self.work)
        self.assertEqual(native.returncode, 0, native.stderr)
        report = engine.diagnose(["ld.bfd", "-e", "main", "-o", "OUT",
                                  "ldsym.o"], verify=True, cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))
        self.assertEqual(report["original"]["unresolved"], [])
        self.assertTrue(report["verified"])

    def test_bound_for_absent_section_stays_unresolved(self):
        native = run(["gcc", "nosec.o", "-o", "native-neg"], self.work)
        self.assertNotEqual(native.returncode, 0, native.stdout)
        report = driver.diagnose(["gcc", "nosec.o", "-o", "p8"], cwd=self.work)
        self.assertEqual(report["status"], "unknown")
        self.assertIn("__start_nosec", report["original"]["unresolved"])
        self.assertEqual(report["diagnosis"], "missing_provider")


class EmptyArchiveTests(ScratchCase):
    """Finding 8: a GNU archive with no members and no index (eight header
    bytes) is a valid input that provides nothing, including the glibc
    >= 2.34 compat stubs -lpthread and -ldl; it must not be rejected."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        build_object(cls.work, "plain", "int main(void){return 0;}\n")
        build_object(cls.work, "plaine", "extern int f(void);\nint main(void){return f();}\n")
        build_object(cls.work, "plainf", "int f(void){return 1;}\n")
        r = run(["ar", "rcs", "nothing.a"], cls.work)
        assert r.returncode == 0, r.stderr
        assert (cls.work / "nothing.a").stat().st_size == 8

    def test_ld_mode_accepts_user_empty_archive(self):
        native = run(["ld.bfd", "-e", "main", "-o", "native-empty",
                      "plaine.o", "plainf.o", "nothing.a"], self.work)
        self.assertEqual(native.returncode, 0, native.stderr)
        before = sha(self.work / "nothing.a")
        report = engine.diagnose(["ld.bfd", "-e", "main", "-o", "OUT",
                                  "plaine.o", "plainf.o", "nothing.a"],
                                 verify=True, cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))
        self.assertTrue(report["verified"])
        nothing = next(i for i in report["inputs"] if i["path"] == "nothing.a")
        self.assertTrue(nothing["archive"])
        self.assertEqual(nothing["members"], [])
        self.assertEqual(sha(self.work / "nothing.a"), before)

    def test_gcc_mode_accepts_compat_stub_archives(self):
        native = run(["gcc", "plain.o", "-lpthread", "-ldl",
                      "-o", "native-pthread"], self.work)
        self.assertEqual(native.returncode, 0, native.stderr)
        report = driver.diagnose(["gcc", "plain.o", "-lpthread", "-ldl",
                                 "-o", "p9"], verify=True, cwd=self.work)
        self.assertEqual(report["status"], "already_satisfied", report.get("reason"))
        self.assertTrue(report["verified"])
        stubs = [i for i in report["inputs"] if i["path"] in ("-lpthread", "-ldl")]
        self.assertEqual(len(stubs), 2, report["inputs"])
        self.assertTrue(all(i["archive"] and i["members"] == [] for i in stubs))


if __name__ == "__main__":
    unittest.main()
