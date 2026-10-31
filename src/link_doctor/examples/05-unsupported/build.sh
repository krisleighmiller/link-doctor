#!/bin/sh
# Compile the example inputs with the installed GNU toolchain.
set -e
gcc -c main.c
echo "Built. Diagnose the unsupported command with:"
echo "  link-doctor diagnose -- ld -e main -o app main.o -L."
