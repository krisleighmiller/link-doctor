# Example 03: missing provider

`main.c` calls `inventory_total()`, but no object or archive in the command
defines it. This is not an order problem, and no reordering can fix it, so the
tool must not pretend otherwise.

Build:

    sh build.sh

Diagnose the failing command:

    link-doctor diagnose -- ld -e main -o app main.o

Expected result: exit code 2, `Status: unknown`, `Diagnosis:
missing_provider`. `unknown` means no model-valid candidate was found within
the bounded search; it is never a claim that no repair exists in general. With
`--json`, the report carries `schema_version`, `status`, and `diagnosis`
fields with the same content.
