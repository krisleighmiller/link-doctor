# Example 05: unsupported command surface

The tool deliberately supports only an explicit, inspectable command surface
(bare `ld`/`ld.bfd` or ordinary `gcc`/`g++` with explicit inputs). Options
that introduce invisible linker behavior, such as library search paths, are
rejected rather than guessed at.

Build:

    sh build.sh

Diagnose the unsupported command:

    link-doctor diagnose -- ld -e main -o app main.o -L.

Expected result: exit code 2, `Status: unsupported`, with the reason naming
the rejected token. `unsupported` is a statement about the tool's supported
semantics, not a verdict on whether the link could succeed. Supported status
values and their meanings are documented in the project README.
