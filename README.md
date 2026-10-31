# Static Link Order Doctor

Diagnose supported GNU static-archive link-order failures, explain the missing
extraction, and propose a constrained repair. Optional verification runs the
linker against isolated copies without overwriting your output.

Licensed under the [MIT License](LICENSE). The implementation, tests and examples
are freshly authored; no private benchmark source, prompts, graders or fixtures
are included or required.

## Installation and usage

The Python package and CLI are named `link-doctor`. GNU/Linux and GNU binutils
are required; see the supported command and toolchain boundaries below.

Install into a virtual environment from a checkout or a built wheel:

```sh
git clone https://github.com/krisleighmiller/link-doctor.git
cd link-doctor
python3 -m venv .venv
.venv/bin/pip install .
# or: .venv/bin/pip install dist/link_doctor-*.whl
.venv/bin/link-doctor --help
.venv/bin/link-doctor --version
```

### Command intake and working directory

```sh
link-doctor diagnose [--verify] [--json] [--cwd DIR] -- ld -e main -o app main.o ...
link-doctor diagnose [--verify] [--json] [--cwd DIR] -- gcc main.o -L. -lfoo -o app
```

Pass a linker **argv after `--`**, never a shell string; the tool performs no
shell evaluation. Input paths in the supplied argv resolve against `--cwd`
(or the current directory), which is also where `@response` files resolve.
`--help` and `--version` work without a linker argv.

### Result states and exit codes

Exit code 0 means the report's status is `candidate` (a model-valid repair
within the constrained search) or `already_satisfied` (the supplied order
needed no repair). Exit code 2 covers **everything else**, including all usage
errors and the statuses `unknown` (bounded search found no candidate — never a
proof of impossibility), `unsupported` (outside the documented grammar) and
`execution_failure` (missing tools/files, malformed native output or
timeouts). Human and `--json` output carry the same status, verification state,
reasons and provider-change warnings; JSON reports are versioned via
`schema_version`.

Verification is opt-in (`--verify`): the supplied command is never executed
during diagnosis; verification links byte snapshots in a disposable directory,
replaces both `-o` destinations (even output/input aliases), never runs the
produced binary and never overwrites your inputs or existing outputs. A
model-valid candidate is not a linker verdict, and **link success is never
proof of program equivalence** — especially when a repair changes which member
supplies a symbol, which the report always calls out.

### Public examples

Five self-contained examples ship inside the installed package:

```sh
python3 -c "from pathlib import Path; import link_doctor; print(Path(link_doctor.__file__).parent / 'examples')"
```

They cover a basic order error, a cross-archive cycle, a missing provider, a
provider-changing ambiguity and an unsupported command. Each has a `README.md`
and a `build.sh` that compiles with the installed GNU toolchain.

### Compatibility boundary

Intended: CPython >= 3.11 on GNU/Linux with GNU binutils `ld.bfd`/`readelf` at
`/usr/bin`. Actually exercised for this build: CPython 3.14.9 and 3.14.4, GNU
binutils 2.46, GCC/G++ 15.2.0 on Linux 7.0 (x86-64). No broader platform claim
is made. No third-party Python dependencies; standard library only.

### Regression and packaging checks

The full suite (unit, native differential and CLI contract tests) runs with:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
scripts/ci-local.sh          # build wheel+sdist, clean-install and inspect them
```

`.github/workflows/ci.yml` mirrors `scripts/ci-local.sh`. See the
[GitHub Actions runs](https://github.com/krisleighmiller/link-doctor/actions)
for hosted results; the recorded toolchain above describes local checks.
Build evidence stays in `.build/` and is deliberately excluded from the
distributions.


## Phase 4: constrained cycles and static provider resolution

Accepted after independent review and a full separate re-review. Both direct
`ld.bfd` and ordinary GCC/G++ entry paths reuse the inspected member model and
the same bounded repair engine.

Repairs never delete or replace library inputs, or insert whole-archive.
Preference is deterministic for the same input bytes/argv:

1. Original order, then permutations within contiguous user archive spans, in
   input occurrence order (not lexical pathname order).
2. Starting from the original order, breadth-first demand-directed repetitions,
   at most four added archive occurrences. Append only within the provider's
   span, for unresolved demand introduced by a member of that same span.
3. One contiguous subspan group, shortest width first, then left-to-right. A long
   cyclic chain can therefore receive `--start-group A.a B.a --end-group`
   without grouping an unrelated `C.a`.

All strategies share a **720 model-evaluation budget**, including the original.
Permutations can consume the budget before repeats/groups are tried. Repeat
search is intentionally incomplete; it does not combine every permutation with
every repetition, rewrite existing groups, or move inputs across barriers.
Exhaustion is `unknown`, never proof that no repair exists. Group search starts
from the original order and tries one new group at a time, not combinations of
groups in multiple spans or groups plus repeats. No private formal
memo pruning or impossibility certificates are used.

Ordinary static weak/common/strong definitions are supported. Weak undefined
references do not trigger extraction; a resolved weak definition does not demand
a stronger archive provider. Common definitions coalesce; a common demand can
extract a strong defined OBJECT or NOTYPE symbol, regardless of section, but not
a weak, FUNC or common archive definition. ABS symbols remain unsupported.
Reports list coalesced common members, not a prediction of allocated layout.
Loaded strong/strong conflicts invalidate a candidate; unextracted alternatives
do not. Archive/member order determines which eligible alternative is extracted.
JSON `ambiguity`, `selected_providers`, `provider_changes` and `repair` fields,
and human warnings expose those choices. A provider-changing repair is a visible
proposal, **not a claim of program equivalence**, even when the link succeeds.

Fresh differential tests and receipts:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tests/test_phase4.py` writes native argv/status/trace and report receipts to
`.build/phase4-acceptance/`. It contrasts standalone native GCC/G++/ld links
with snapshot verification, compares archive extraction traces, tests a seeded
24-case weak/common/strong matrix, targeted repeats/groups, alternative choices,
conflicts, barriers and preservation. Provider winners are also checked against
native cross-reference/common-allocation maps for direct ld and ordinary GCC/G++.
Failure/timeout injection is explicitly
synthetic. Red test receipts and the native common-symbol probe are retained
there separately. No produced executable is run.

## Ordinary GCC/G++ link commands

```sh
# Source checkout; paths in the supplied argv are relative to --cwd (or your cwd).
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m link_doctor \
  diagnose --cwd '/path/to/build with spaces' --verify --json -- \
  gcc main.o -L. -lleaf -lmiddle -o 'OUT space'

# Fresh C/libc and C++/iostream reversed-order demos, actual GCC/G++ contrasts,
# preservation hashes and human/JSON receipts in .build/phase3-acceptance/:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 tests/phase3_acceptance.py
```

### Command-entry contract

- Pass an **argv**, after `--`; the caller's shell handles its own quoting. Never
  supply a shell command string. No shell evaluation, whitespace splitting,
  redirection, pipes or command substitution is performed by the tool.
- Bare `gcc` and `g++` select trusted `/usr/bin` drivers, **not** caller PATH
  wrappers. Link-only `.o`/indexed `.a` inputs, `-o PATH`, joined/separate `-L`
  and `-l` (including `-l:filename`) are supported. Paths may contain spaces.
- `-Wl,` and `-Xlinker` forward only the same allowlisted linker grammar.
  `--as-needed`, `--no-as-needed`, `--push-state`, `--pop-state`, `-Bstatic`,
  `-Bdynamic`, `--start-group` and `--end-group` are preserved. States/groups
  must balance; restore dynamic mode before the compiler runtime. Existing
  groups are evaluated to a fixed point and left intact. Only contiguous user
  archive spans outside existing groups can be reordered, repeated or grouped;
  objects, options, runtime inputs and groups remain barriers.
- GNU-style `@response` expansion supports quotes, backslash escapes and nested
  files relative to the command cwd. Limits: 8 levels, 64 KiB cumulative file
  contents, 256 expanded arguments, 8192 characters per argument. Cycles,
  malformed quotes/escapes, empty arguments and embedded control characters
  fail closed. `-Wl,@file` is rejected; use top-level `@file`. Repairs are fully
  expanded canonical argv, not edits to response files. Reports preserve both
  original and normalized commands plus response-file hashes.
- Library selection searches all explicit `-L` directories before discovered
  driver paths, in order; within each directory `.so` precedes `.a` unless
  static mode is active. A selected unsupported `.so` is rejected, never
  skipped in favor of a convenient archive. Missing library names are reported
  explicitly. A default output name need not be supplied: verification always
  sets its own disposable output.

### Runtime discovery and safety policy

The driver receives only fixed synthetic `-### DOCTOR_INPUT.o -o DOCTOR_OUTPUT`
arguments in a disposable cwd and sanitized environment. **No supplied command
is executed during diagnosis**, even as a driver dry run. The trusted driver's
startup inputs, search paths and fixed linker flags are parsed, not guessed.
Unknown template options fail closed. User specs, plugins, wrappers, source
compilation, `-B` compiler overrides, alternate linkers, sysroots, maps,
dependency outputs, script options and arbitrary output-producing options
are rejected before discovery. LTO, bitcode and disguised user scripts are
rejected during file inspection. No install or Python package dependency is needed.

GCC's driver-owned `liblto_plugin.so` must resolve to a root-owned path with
non-writable ancestors. Its discovery options are removed: **no plugin,
collect2 or compiler is executed for verification**. Every selected object and
archive member is checked for ELF/LTO metadata first; opt-in verification uses
trusted `ld.bfd` on snapshots of the normalized non-LTO link.

Restricted root-owned compiler-runtime `GROUP`, `INPUT`, `AS_NEEDED` and
`OUTPUT_FORMAT(elf64-x86-64)` scripts are recursively resolved, validated and
snapshotted. Group/as-needed semantics are preserved rather than flattened.
Nested `-l` selections use the actual library search paths, without an implicit
cwd entry. Raw script filenames retain their spelling: relative names (including
nested relative paths) try the original command cwd first, then those search
paths, not the script's parent directory; absolute names are used directly.
A selected invalid cwd file is rejected, never replaced by a system copy.
A system script's child does **not** inherit trust: cwd and user `-L` overrides
receive ordinary user ELF checks, and valid archives are accepted regardless of
a `.so` suffix. Inventory receipts retain spelling, lookup context, selected path
and byte hash; verification renders the selected snapshots. Explicit `-lm`, `-lc`, `-lgcc`, `-lgcc_s` and `-lstdc++` may use this same
restricted runtime path. General user scripts/shared libraries remain outside
scope. Runtime DSOs' `DT_NEEDED` closure is inspected and snapshotted; complete
dynamic-table row counts are checked, and RPATH/RUNPATH/filter/audit semantics
are rejected. Verification resolves dependency aliases in the snapshot directory.
The interpreter path is a validated system path, not an executed program.

The minimum model handles CRT weak references, weak definitions, default and
versioned shared-provider names, runtime IFUNC/TLS/unique provider *presence*,
and initializer sections without discarding their ordinary symbol references.
Weak undefined references do not extract archive members. It does **not** model
runtime dynamic binding, execute IFUNC resolvers, validate relocations/layout,
or establish behavioral equivalence. COMDAT/section groups, user TLS/IFUNC,
user/runtime provider overlap and unsupported special sections cannot yield a
confident repair. Trusted runtime libraries remain a fixed baseline, not general
mixed shared/static or dynamic-binding repair support. These boundaries retain
ordinary static object/archive repair through the compiler runtime; they do not
claim arbitrary C++ COMDAT, section-GC or user-DSO semantics.

Reports include recursive input structure, selected file hashes and trust
classification, discovery argv/template, search paths, missing-symbol consumers,
archive extraction evidence and positional repairs. `missing_provider`,
`duplicate_definitions`, `provider_ambiguity` and `constrained_search_exhausted`
are non-ordering diagnosis categories; unsupported/incompatible input and tool
failures retain their reasons. Model-valid is not linker-verified. Native
verification disagreement withholds the command and returns `unknown`.

Limits additionally include 16 MiB per file, 128 MiB total selected files, 2048
files, 256 members per user archive/1024 per trusted runtime archive, 16 nested
runtime-script expressions, 720 candidate evaluations and 10 seconds per native
call. This is **not a sandbox or a hard disk/process-wide time quota**. The
runtime grammar is intentionally specific to the discovered GNU x86-64 toolchain;
static executables through the driver, non-PIE driver variants, general shared
linking and GC remain unsupported. Tested native acceptance: GCC/G++ 15.2.0
and GNU binutils 2.46. Other templates may be explicitly rejected.

## Phase 2: explicit-input diagnosis and repair

The original internal vertical milestone remains supported with its narrower
explicit-input grammar. Run from source with a CPython interpreter inside the
declared compatibility boundary (see above), on Linux, with GNU readelf and
ld.bfd installed at `/usr/bin`. The tests also
require GNU gcc and ar. No Python dependencies are required.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

### Reproduce the fresh two-archive case

From this directory (builds only in a disposable directory):

```sh
root="$PWD"
build="$(mktemp -d "${TMPDIR:-/tmp}/link-doctor-demo.XXXXXX")"
(
  cd "$build" || exit 1
  for name in main middle leaf; do
    gcc -c -fno-pie -fno-stack-protector \
      "$root/tests/fixtures/two_archives/$name.c" -o "$name.o" || exit 1
  done
  ar rcs libmiddle.a middle.o
  ar rcs libleaf.a leaf.o
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root/src" python3 -m link_doctor \
    diagnose --verify -- ld.bfd -e main -o OUT main.o libleaf.a libmiddle.a
)
rm -rf -- "$build"
```

The original command leaves `leaf` unresolved: `libleaf.a(leaf.o)` was visited
when only `middle` was needed. Later, `libmiddle.a(middle.o)` introduces the
reference to `leaf`. The repair is:

```sh
ld.bfd -e main -o OUT main.o libmiddle.a libleaf.a
```

Add `--json` before `--` for a schema-versioned report with original argv,
ordered inputs/members and their definitions/references, extraction events,
archive-visit evidence, unresolved symbols/consumers, missed providers, search
budget, positional changes, candidate model result and native verification
receipts. Human output includes the same central evidence, proposed command,
changes and verification status. Exit 0 means model-valid candidate or already
satisfied; exit 2 means unsupported/error/unknown, **not** proven impossible.
`execution_failure` distinguishes missing tools/files and timeouts from unsupported
semantics; native rejection of a proposed repair returns `unknown` with
`verification.status = failed` and no usable command.

Without `--verify`, only inspection runs; the supplied linker command is never
executed. With `--verify` and a model-valid candidate, both original and candidate
orders are linked against byte snapshots in a fresh temporary directory. Both
`-o` destinations are replaced, even when the requested output aliases an input.
No linked binary is executed. Original files and existing output sentinels are
untouched. Temporary files are removed on completion. Receipt argv paths therefore
refer to now-disposed snapshots, not reusable executables. Native failure,
timeout or disagreement with the model withholds the usable candidate command.
A proposed command is evidence about the inspected snapshots, not a promise about
files that a user subsequently edits; executing it manually has normal linker
output-overwrite behavior.

## Direct-ld semantics and deliberate rejection boundaries

- Bare `ld` or `ld.bfd` argv only, mapped to trusted `/usr/bin/ld.bfd` rather than
  resolved through the user's PATH. Exactly one separate `-e SYMBOL` and `-o PATH`
  are required. Balanced `--start-group`/`--end-group` are also supported.
  Other flags, absolute executable paths, wrappers, response files,
  scripts, libraries by `-l`, search paths, plugin options, maps/dependency outputs,
  whole-archive/GC modes are rejected before spawning. GCC/G++ use the separate
  driver grammar above.
- Explicit regular `.o` and normal indexed GNU `.a` files only; ELF64,
  little-endian, x86-64 relocatables. Ordinary default-visibility strong, weak
  and common globals with simple C-style names only. Unique/IFUNC/TLS, LTO/bitcode,
  COMDAT/groups, dynamic symbols and special linker sections handled by the
  rejection rules are unsupported. This is a symbol-extraction model, not a
  relocation/layout/ABI validator. A model-valid command is not native success.
- Every member is inspected, even unused members. Duplicate member names, thin
  archives, BSD/64-bit indexes, non-ELF members and `__.LIBDEP` dependency metadata
  are rejected. GNU long member names and path spaces are supported. Archive
  indexes must exactly match the inspected symbol-to-member-offset relationships
  and use nondecreasing member offsets. Noncanonical index order is explicitly
  refused: native probes show it can change an alternative provider without
  changing link success. Repeated archive paths share member identity; repeated
  explicit objects are rejected, not silently deduplicated.
- ELF facts come from `readelf -W -h -S -s`, not a new ELF parser. No debug dumps
  or debug-link following are requested. A small bounded GNU ar **container**
  reader preserves member order/identity, checks index consistency and rejects
  metadata that whole-archive readelf can skip. It is not an archive extraction
  utility: original names are never used as filesystem destinations. `nm` and
  BFD `ar` are deliberately not run for inspection because of implicit plugins.
- Each archive repeatedly extracts needed members until stable. Unused members
  do not inject their references. A later reference does not rescan an earlier
  archive. The explicit `-e` symbol roots archive extraction on the supported
  GNU ld.bfd, as covered by a native archive-only entry-point test.
- Only contiguous archive-only argv spans may be repaired using the Phase 4
  policy above. Objects and every flag stay fixed and act as barriers. Search
  never materializes factorial permutation pools. No valid candidate means
  `unknown`, never an impossibility proof.
- Limits: 64 inputs, 256 members per archive, 16 MiB total input bytes, 8192 chars
  per argv token, 10 seconds per native invocation; captured tool output above
  16 MiB is rejected after the invocation. Tools must resolve to root-owned,
  non-group/world-writable regular files. Subprocesses use argv, `shell=False`,
  a sanitized environment, and only the two hardcoded system tools. Missing
  files/tools, malformed native output, stderr diagnostics and timeouts fail
  closed. **This is not an OS sandbox**: do not treat native binutils as safe
  against arbitrary hostile ELF exploits. Output size checks are not a hard
  disk quota, and there is no process-wide wall-clock budget.

The Phase 1 `outcomes.assess_candidate` boundary is reused: unmet requirements
withhold a command, and model satisfaction alone never sets `verified`.

## Tests

`tests/test_diagnose.py` builds fresh C in temporary directories, never runs linked
programs, and tests two/three-archive chains, correct-order contrast, intra-archive
fixed points, unused members, object/flag barriers, missing providers/entry,
bounded search, byte preservation including output/input aliasing, real GNU
verification, long names/path spaces, unsupported inputs/options, metadata,
malformed indexes, FIFO rejection, partial parser output, and injected tool
failures/timeouts. Mocked failures are fault-injection tests, not claimed native
link evidence. The original seven outcome/native tests remain unchanged.

Canonical plan, provenance, TDD and independent verification evidence are kept
outside this repository, in the author's private planning notes. This project
contains only the fresh implementation, its public examples and its tests.
