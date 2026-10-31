#!/bin/sh
# Compile the example inputs with the installed GNU toolchain.
# libstart.a holds two members: start (needs middle) and finish.
set -e
gcc -c main.c a_start.c b_middle.c a_finish.c
ar rcs libstart.a a_start.o a_finish.o
ar rcs libmiddle.a b_middle.o
echo "Built. Diagnose the cross-archive cycle with:"
echo "  link-doctor diagnose -- ld -e main -o app main.o libstart.a libmiddle.a"
