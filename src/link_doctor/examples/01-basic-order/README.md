# Example 01: basic archive order error

`main.c` calls `provider()`, which lives in `libprovider.a` and itself needs
`helper()` from `libleaf.a`. With `libleaf.a` placed before `libprovider.a`,
the linker has no reason to extract `helper.o` when `libprovider.a` extracts
`provider.o` later, so the native link fails.

Build:

    sh build.sh

Diagnose the failing command:

    link-doctor diagnose -- ld -e main -o app main.o libleaf.a libprovider.a

Expected result: exit code 0, `Status: candidate`, and a model-validated
command that reorders the archives (`main.o libprovider.a libleaf.a`). Add
`--verify` to also link the original and candidate orders in an isolated
disposable directory; your inputs and outputs are never touched, and the
produced executable is never run. `--json` prints the same evidence in the
versioned JSON report.
