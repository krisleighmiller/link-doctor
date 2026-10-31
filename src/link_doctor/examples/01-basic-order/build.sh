#!/bin/sh
# Compile the example inputs with the installed GNU toolchain.
set -e
gcc -c main.c provider.c helper.c
ar rcs libprovider.a provider.o
ar rcs libleaf.a helper.o
echo "Built. Diagnose the failing command with:"
echo "  link-doctor diagnose -- ld -e main -o app main.o libleaf.a libprovider.a"
