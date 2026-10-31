# Example 04: ambiguity and provider changes

Two archives both define `value()`. In the failing order the linker extracts
`first.o` from `libfirst.a`, which drags in an unsatisfiable requirement for
`ghost()`. The repair reorders so `libsecond.a` supplies `value()` instead.
The symbol's provider changes, and the tool says so: a provider-changing
repair is a visible proposal, not a claim of program equivalence, even when
the link succeeds.

Build:

    sh build.sh

Diagnose the failing command:

    link-doctor diagnose -- ld -e main -o app main.o libfirst.a libsecond.a

Expected result: exit code 0, `Status: candidate`, the warning
`WARNING: provider changed for value: ...`, and the closing line that link
success is not program equivalence. The JSON report lists the alternative
providers in `ambiguity` and the change in `provider_changes`.
