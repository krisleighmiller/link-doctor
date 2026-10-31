#!/bin/sh
# Compile the example inputs with the installed GNU toolchain.
set -e
gcc -c main.c
echo "Built. Diagnose the missing provider with:"
echo "  link-doctor diagnose -- ld -e main -o app main.o"
