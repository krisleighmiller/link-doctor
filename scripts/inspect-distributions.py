#!/usr/bin/env python3
"""Inspect built distributions for content the release must not contain.

Checks the wheel and sdist listings for private or accidental material:
build evidence directories, private task-collection references, native
compiled artifacts, bytecode, logs, absolute host paths and stray
credentials. Exits nonzero on the first violation. Intended for CI and local
package validation.
"""
import argparse
import re
import sys
import tarfile
import zipfile
from pathlib import Path

# Forbidden in file/directory names within the distributions.
NAME_PATTERNS = (
    re.compile(r"\.build"),
    re.compile(r"(?i)private"),
    re.compile(r"(?i)(api[_-]?key|secret|token|credential|password)"),
    re.compile(r"(?i)(handoff|scratch)"),
)
# File types that must never appear in the distributions.
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".o", ".a", ".out", ".so", ".log",
                      ".whl", ".tar.gz", ".rsp", ".bin", ".elf")
# Suspicious content inside text files: absolute host paths and credentials.
# The word "private" is deliberately NOT scanned in prose: the package
# honestly states its provenance (no private material included, no license
# chosen) and those statements must be allowed to mention it.
CONTENT_PATTERNS = (
    re.compile(r"/home/[A-Za-z0-9._-]+"),
    re.compile(r"/Users/[A-Za-z0-9._-]+"),
    re.compile(r"/root\b"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]|secret\s*[=:]|password\s*[=:]|credential\s*[=:])"),
)
TEXT_SUFFIXES = (".py", ".md", ".toml", ".cfg", ".txt", ".in", ".yml", ".sh")


def check_names(names, label):
    problems = []
    for name in names:
        if name.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"{label}: compiled/bytecode/log artifact: {name}")
        for pattern in NAME_PATTERNS:
            if pattern.search(name):
                problems.append(f"{label}: forbidden name pattern {pattern.pattern!r}: {name}")
    return problems


def check_content(text_files, label):
    problems = []
    for name, text in text_files:
        # Exempt this script itself: it contains the detection patterns as
        # literals, so scanning its own source is a guaranteed false positive.
        if name.endswith("inspect-distributions.py"):
            continue
        for pattern in CONTENT_PATTERNS:
            for match in pattern.finditer(text):
                context = text[max(0, match.start() - 40):match.end() + 40]
                problems.append(f"{label}: suspicious content in {name}: ...{context!r}...")
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", nargs="?", default="dist",
                        help="directory holding the built wheel and sdist")
    args = parser.parse_args()
    dist = Path(args.dist_dir)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if not wheels or not sdists:
        print(f"FAIL: expected one wheel and one sdist in {dist}; found "
              f"{[p.name for p in wheels + sdists]}")
        return 2
    problems = []
    for wheel_path in wheels:
        with zipfile.ZipFile(wheel_path) as wheel:
            names = wheel.namelist()
            text_files = [(n, wheel.read(n).decode("utf-8", "replace"))
                          for n in names if n.endswith(TEXT_SUFFIXES)]
            problems += check_names(names, wheel_path.name)
            problems += check_content(text_files, wheel_path.name)
    for sdist_path in sdists:
        with tarfile.open(sdist_path) as sdist:
            names = sdist.getnames()
            text_files = []
            for n in names:
                if n.endswith(TEXT_SUFFIXES):
                    member = sdist.extractfile(n)
                    if member is not None:
                        text_files.append((n, member.read().decode("utf-8", "replace")))
            problems += check_names(names, sdist_path.name)
            problems += check_content(text_files, sdist_path.name)
    if problems:
        print("FAIL")
        for problem in problems:
            print(" ", problem)
        return 1
    print("PASS: no private data, absolute host paths, credentials, build")
    print("evidence, logs or native compiled artifacts found in:")
    for path in wheels + sdists:
        print(" ", path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
