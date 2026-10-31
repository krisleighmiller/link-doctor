# Public examples

These are fresh, self-contained examples shipped with the package. Each one
compiles with the installed GNU toolchain and reproduces one supported outcome.
No private fixtures, graders or task material are used or needed.

Copy an example out of the installed package (or run in place), build it, and
diagnose the failing command:

    python3 -c "from pathlib import Path; import link_doctor; print(Path(link_doctor.__file__).parent / 'examples')"
    cd <examples>/01-basic-order && sh build.sh
    link-doctor diagnose -- ld -e main -o app main.o libleaf.a libprovider.a

| Example | What it shows | Expected status | Exit code |
| --- | --- | --- | --- |
| 01-basic-order | simple archive order error, repaired by reordering | candidate | 0 |
| 02-cycle | cross-archive cycle that needs `--start-group/--end-group` | candidate | 0 |
| 03-missing-provider | a required symbol no input provides | unknown | 2 |
| 04-ambiguity | a repair that changes which member supplies a symbol | candidate | 0 |
| 05-unsupported | a command outside the supported surface | unsupported | 2 |

Exit code 0 means a model-valid candidate (or an already-satisfied command) was
reported. Exit code 2 covers every other status and all usage errors. Link
success never proves program equivalence; see the project README.
