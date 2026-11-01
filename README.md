# Link Doctor

[![PyPI](https://img.shields.io/pypi/v/link-doctor.svg)](https://pypi.org/project/link-doctor/)
[![Python](https://img.shields.io/pypi/pyversions/link-doctor.svg)](https://pypi.org/project/link-doctor/)
[![CI](https://github.com/krisleighmiller/link-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/krisleighmiller/link-doctor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/pypi/l/link-doctor.svg)](https://github.com/krisleighmiller/link-doctor/blob/main/LICENSE)

Link Doctor diagnoses GNU static-library link-order failures. It explains
which archive member the linker missed, proposes a constrained repair, and can
optionally verify the proposed command with the real linker without
overwriting your existing output.

The classic failure it addresses: when linking static archives, GNU `ld`
extracts an archive member only if, at the moment it visits that archive, it
already needs a symbol the member defines. If `libleaf.a` appears before
`libprovider.a`, but a member extracted from `libprovider.a` is what first
needs `libleaf.a`, the link fails with undefined references that vanish when
you reorder the archives. Link Doctor models this extraction at the
archive-member level, reports exactly which member was missed, and proposes
the smallest supported change that makes the command link.

Link Doctor supports a documented subset of GNU/Linux link commands. It is
not a replacement linker or a general-purpose build repair tool. **A
successful link does not prove equivalent program behavior**, particularly
when a repair changes which symbol provider wins; provider changes are
reported explicitly for review.

## Install

Requires Python **3.11 or newer** and a GNU/Linux x86-64 system with `readelf`
and `ld.bfd` in `/usr/bin`. GCC/G++ command inspection additionally requires
the corresponding system compiler, and building the examples requires GCC and
`ar`. There are no third-party Python runtime dependencies.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install link-doctor
link-doctor --help
link-doctor --version
```

The test suite runs on CPython 3.11 and 3.14 in hosted CI. Native checks use
GCC/G++ 15.2 and GNU binutils 2.46; hosted CI records its own toolchain
versions. No Windows, macOS or non-GNU linker compatibility is claimed.

## Quickstart

The package ships with ready-to-run examples. With the virtual environment
above active, copy the basic example to a temporary directory and build it:

```sh
demo="$(mktemp -d)"
python -c 'from pathlib import Path; import shutil, sys, link_doctor; shutil.copytree(Path(link_doctor.__file__).parent / "examples" / "01-basic-order", sys.argv[1], dirs_exist_ok=True)' "$demo"
cd "$demo"
sh build.sh

link-doctor diagnose -- ld -e main -o app main.o libleaf.a libprovider.a
```

Here, `main.o` needs `provider()`. The member that defines it lives in
`libprovider.a` and needs `helper()` from `libleaf.a`. The linker visits
`libleaf.a` before it knows it needs `helper`, so the original order fails.
Link Doctor identifies that missed extraction and proposes:

```sh
ld -e main -o app main.o libprovider.a libleaf.a
```

To check the original and proposed orders with the native linker:

```sh
link-doctor diagnose --verify -- ld -e main -o app main.o libleaf.a libprovider.a
```

Verification links isolated copies, redirects both outputs, and never runs
the resulting program. It does not create or replace `app` in your working
directory. Running the proposed command yourself has ordinary linker
output-overwrite behavior.

## Usage

`link-doctor` has a single action, `diagnose`. Pass the link command as
**arguments after `--`**, not as a quoted shell-command string. Your shell
handles argument quoting; Link Doctor does not evaluate shell syntax,
redirection, pipes or command substitution.

```sh
# A supported GCC link-only command:
link-doctor diagnose -- gcc main.o -L./lib -lconsumer -lprovider -o app

# Equivalent G++ command, with machine-readable output:
link-doctor diagnose --json -- g++ main.o -L./lib -lconsumer -lprovider -o app

# Resolve relative paths from a particular build directory:
link-doctor diagnose --cwd "/path/to/build directory" -- gcc @link.rsp

# Native verification is always explicit:
link-doctor diagnose --verify --json -- gcc @link.rsp
```

| Option | Effect |
|---|---|
| `--cwd DIR` | Resolve inputs and response files from this working directory |
| `--json` | Emit the structured, versioned report instead of human output |
| `--verify` | Opt in to isolated native links of the original and proposed commands; never run the result |
| `--version` | Print the version and exit |

Use the command from the actual failed link step. Compile-and-link commands,
such as `gcc main.c -o app`, are not supported. The selected tools are trusted
system executables, not wrappers found through the caller's `PATH`.

C++ support follows the same narrow grammar: the g++ example above is a
C-style link-only command. Real C++ (templates, `std::string`, inline
functions) carries COMDAT section groups, which are rejected as out of
scope, so only C-style C++ links diagnose.

### Command support

| Entry point | Supported intake | Important boundaries |
|---|---|---|
| `ld` / `ld.bfd` | Explicit `.o` and indexed GNU `.a` paths; exactly one separate `-e SYMBOL` and `-o PATH`; balanced archive groups | No `-L`, `-l`, response files, executable paths, wrappers or other linker options |
| `gcc` / `g++` | Link-only objects/archives; optional `-o`; joined or separate `-L` and `-l`, including `-l:filename`; bounded top-level `@response` files | No source compilation, arbitrary driver flags, alternate linkers, sysroots or general user scripts/shared libraries |
| Driver forwarding | `-Wl,` and `-Xlinker` for the allowlisted linker grammar | Forwarding does not make otherwise unsupported flags safe or supported |
| Driver state | `--as-needed`, `--no-as-needed`, `--push-state`, `--pop-state`, `-Bstatic`, `-Bdynamic`, `--start-group`, `--end-group` | States/groups must balance; restore dynamic mode before the compiler runtime |

Paths containing spaces and GNU long archive-member names are supported.
Existing groups are evaluated to a fixed point and kept intact. Direct-linker
and compiler-driver intake have different boundaries; support in one does not
imply support in the other.

### Response files and library lookup

On the GCC/G++ path, GNU-style response expansion handles quotes, backslash
escapes and nested files relative to the command's working directory. Cycles,
malformed quoting, empty arguments and control characters are rejected.
`-Wl,@file` is not supported; use top-level `@file` instead. Repairs are
expanded argument lists, not edits to your response files. Reports retain
original and normalized commands and response-file hashes.

Library selection searches explicit `-L` directories before discovered driver
paths, in order. Within a directory, `.so` precedes `.a` unless static mode is
active. An unsupported selected `.so` is rejected, not skipped in favor of a
convenient archive. Missing library names are reported explicitly.

### Reading the result

| Status | Meaning |
|---|---|
| `candidate` | A proposed command satisfies the supported model. Check native verification separately. |
| `already_satisfied` | The original command satisfies the supported model; no repair is needed. |
| `unknown` | No acceptable candidate was established within the constraints/budget, or native verification disagreed. This is **not** proof that no repair exists. |
| `unsupported` | The command or an input requires semantics outside the documented boundary. |
| `execution_failure` | An inspection/verification tool, file operation or timeout failed. |

Exit **0** means `candidate` or `already_satisfied`. Exit **2** covers other
outcomes and usage errors. Without `--verify`, the supplied link command is
never executed, so exit 0 alone does not mean a native link succeeded.

Human output and `--json` report the outcome, unresolved-symbol consumers,
missed providers, archive visits, proposed edits and verification status.
JSON uses `schema_version: 1`; it also provides inspected inputs/member
facts, search budget, selected providers and native command receipts. Driver
reports include runtime discovery and selected-file inventory.

`verified` and `verification.status` describe optional native snapshot
linking. Native disagreement withholds the usable command and returns
`unknown`; tool failures remain `execution_failure`. Verification checks that
the proposed command links, not that the resulting program is equivalent to
the original.

Alternative providers appear in `ambiguity` and `selected_providers`. Repairs
that change a selected provider expose `provider_changes` and a human warning.
Review those changes before using the proposed command.

## How repairs are chosen

Link Doctor models extraction at the **archive-member level**, rather than
flattening each archive into one symbol set. Unused members do not inject
their references. Each visited archive extracts needed members until stable;
a later reference does not automatically rescan an earlier ordinary archive.
Explicit `-e` seeds entry-symbol demand on the supported GNU linker.

The deterministic repair preference is:

1. Reorder archives within an eligible contiguous archive span.
2. Add demand-directed repeats, up to four extra archive occurrences.
3. Add a targeted group around a contiguous subspan, trying smaller groups
   first.

Objects, options, existing groups and implicit runtime inputs remain fixed.
The tool preserves original archive occurrences; it does not silently remove
inputs, add `--whole-archive`, substitute a selected library or move an object
to make a command pass.

All strategies share a **720-model-evaluation budget**, including the original.
The search is intentionally incomplete: permutations can consume the budget
before repeats or groups are tried, and there is no claim of global optimality
or impossibility when the search is exhausted.

Ordinary static weak/common/strong definitions are supported. Weak undefined
references do not trigger extraction, and a resolved weak definition does not
demand a stronger archive provider. Common definitions coalesce. Common demand
can extract a strong defined OBJECT or NOTYPE symbol regardless of section, but
not a weak, FUNC or common archive definition. ABS remains unsupported. Loaded
strong/strong conflicts invalidate a candidate; unextracted alternatives do
not.

## Safety

- **Diagnosis is non-linking.** It performs controlled inspection, not
  execution of the supplied command. Driver discovery uses fixed synthetic
  arguments in a disposable directory and a sanitized environment, not the
  user's argv.
- **Verification is opt-in.** Original and candidate links use byte snapshots
  in a temporary directory. All supported output destinations are replaced,
  including output paths that alias an input. Unsupported output-producing
  options are rejected.
- **No generated program is run.** Temporary link outputs are removed
  afterward; receipt paths identify disposed snapshots, not reusable
  executables.
- **Input selection matters.** Inspected paths, hashes and lookup context are
  recorded. Native verification uses the selected snapshots, not substitute
  libraries. Later user edits to input files invalidate that snapshot
  evidence.
- **No shell evaluation.** Subprocesses use argv and trusted native tools.
  Arbitrary plugins, specs, wrappers and script options are not executed.

This is **not an OS sandbox**. Native parser vulnerabilities, hard disk
quotas and a whole-process execution deadline are not solved by using
temporary files and per-command timeouts. Do not treat arbitrary hostile
binaries as safe.

## Limits and compatibility

The supported object/archive format is ordinary little-endian x86-64 ELF64
relocatables in normal indexed GNU archives. Every member is inspected, even
if unused. Duplicate member names, thin archives, BSD/64-bit indexes, non-ELF
members and `__.LIBDEP` metadata are rejected. Archive indexes must match
inspected symbols/member offsets and use nondecreasing member offsets;
noncanonical index ordering can alter provider selection and is refused.
Repeated archive paths share member identity; repeated explicit objects are
rejected rather than silently deduplicated.

ELF facts come from `readelf`, without debug dumps or debug-link following.
A bounded GNU archive-container reader checks structure, members and indexes;
it does not extract original names as filesystem destinations. `nm` and BFD
`ar` are not used for inspection because of implicit plugin loading.

The model is about symbol extraction, not relocation, layout or ABI
validation. LTO/bitcode, general COMDAT/section-group handling, garbage
collection, user TLS/IFUNC, arbitrary shared-library/dynamic-binding repairs
and general linker scripts are outside scope. Direct ld supports simple
C-style global names; the compiler-driver path handles its separately
validated symbol/runtime grammar. Static executables and non-PIE driver
variants are not currently supported.

### Compiler runtime handling

GCC/G++ startup files, search paths and fixed linker flags are discovered
from the installed toolchain, not hard-coded. Unknown compiler templates are
rejected. The driver-owned LTO plugin path is validated but its discovery
options are removed; verification runs trusted `ld.bfd`, not plugins,
`collect2` or the compiler.

Restricted root-owned runtime scripts using `GROUP`, `INPUT`, `AS_NEEDED` and
`OUTPUT_FORMAT(elf64-x86-64)` are resolved recursively with their semantics
preserved. Explicit `-lm`, `-lc`, `-lgcc`, `-lgcc_s` and `-lstdc++` can use
this path. A system script's child does **not** inherit trust: user-directory
files receive ordinary user-input checks.

Raw relative script filenames try the original command cwd, then library
search paths, not the script's parent directory. Nested `-l` lookups use
search paths without implicitly adding cwd. A selected invalid local file is
rejected rather than replaced by a system copy. Valid archives are inspected
as archives even if selected under a `.so` filename.

Runtime DSOs' `DT_NEEDED` closure is inspected and snapshotted. RPATH/RUNPATH,
filter and audit semantics are rejected. Necessary CRT weak references,
versioned provider names and runtime IFUNC/TLS/unique provider presence are
accounted for, but dynamic binding is not modeled and IFUNC resolvers are
never executed. The runtime remains a fixed baseline, not general user-DSO
repair support.

### Resource bounds

| Boundary | Direct ld | GCC/G++ |
|---|---|---|
| Inputs | 64 explicit inputs | 2,048 selected files |
| Input bytes | 16 MiB total | 16 MiB per file, 128 MiB total |
| Members per archive | 256 | 256 user / 1,024 trusted runtime |
| Argument length | 8,192 characters | 8,192 characters |
| Model evaluations | 720 | 720 |
| Native invocation timeout | 10 seconds | 10 seconds |

Driver response files additionally allow eight nesting levels, 64 KiB
cumulative contents and 256 expanded arguments. Runtime-script expressions
are limited to 16 nesting levels. Native output-size checks happen after
invocation, not as a hard disk quota. Errors and incomplete parsing fail
closed.

## Examples

Five examples ship in the installed `link_doctor/examples` directory and are
also available in [the source tree](https://github.com/krisleighmiller/link-doctor/tree/main/src/link_doctor/examples):

| Example | Demonstrates |
|---|---|
| `01-basic-order` | A missed extraction fixed by reordering |
| `02-cycle` | A cross-archive dependency cycle |
| `03-missing-provider` | A symbol with no available provider |
| `04-ambiguity` | Alternative providers and their visible effects |
| `05-unsupported` | An explicitly rejected command |

Each contains its own sources, a `build.sh` and a README with expected
results. Copy examples to a working directory before building them; do not
modify an installed package in place.

## Development

```sh
git clone https://github.com/krisleighmiller/link-doctor.git
cd link-doctor
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests -v
scripts/ci-local.sh
```

The test suite covers native differential cases, final-symbol/provider checks,
input/output preservation, command normalization, and package/CLI behavior.
`ci-local.sh` builds wheel and sdist files, checks clean installation outside
the checkout, runs the shipped examples and inspects distribution contents;
generated artifacts stay in the ignored `.build/` directory.

Hosted [CI runs](https://github.com/krisleighmiller/link-doctor/actions/workflows/ci.yml)
record their toolchain versions and run regression and package checks. The
[publishing workflow](https://github.com/krisleighmiller/link-doctor/actions/workflows/publish.yml)
is manual, defaults to a no-upload rehearsal, and requires a
protected-environment approval for actual registry uploads through PyPI
Trusted Publishing.

## License

[MIT](https://github.com/krisleighmiller/link-doctor/blob/main/LICENSE)
