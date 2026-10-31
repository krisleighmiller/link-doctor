#!/bin/sh
# Compile the example inputs with the installed GNU toolchain.
set -e
gcc -c main.c first.c second.c
ar rcs libfirst.a first.o
ar rcs libsecond.a second.o
echo "Built. Diagnose the ambiguous provider with:"
echo "  link-doctor diagnose -- ld -e main -o app main.o libfirst.a libsecond.a"
