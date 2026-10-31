# Example 02: cross-archive cycle

`main.c` calls `start()` from `libstart.a`. The `start` member needs
`middle()` from `libmiddle.a`, and the `middle` member in turn needs
`finish()`, which is a second member of `libstart.a` that the linker has
already passed. No reordering can satisfy this demand; the repair is an
archive group.

Build:

    sh build.sh

Diagnose the failing command:

    link-doctor diagnose -- ld -e main -o app main.o libstart.a libmiddle.a

Expected result: exit code 0, `Status: candidate`, and a model-validated
command that groups the archives (`--start-group libstart.a libmiddle.a
--end-group`). Add `--verify` for isolated native confirmation; the produced
executable is never run.
