#!/bin/sh
# Local equivalent of .github/workflows/ci.yml. Records the toolchain, runs
# the full regression/differential suite, builds the distributions, then
# verifies a clean install outside the checkout exactly as the workflow
# would. Check the Actions run for the current hosted result; local success
# is not a substitute for hosted evidence. Usage: scripts/ci-local.sh [evidence-dir]
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
EVIDENCE=${1:-$ROOT/.build/ci-local}
mkdir -p "$EVIDENCE"

echo "== toolchain =="
{
  python3 --version
  gcc --version | head -1
  g++ --version | head -1
  ld.bfd --version | head -1
  readelf --version | head -1
  ar --version | head -1
} > "$EVIDENCE/toolchain.txt"
cat "$EVIDENCE/toolchain.txt"

echo "== regression suite =="
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v \
  > "$EVIDENCE/regression.txt" 2>&1
tail -4 "$EVIDENCE/regression.txt"

echo "== build distributions =="
rm -rf dist
python3 -m venv "$EVIDENCE/build-venv"
"$EVIDENCE/build-venv/bin/pip" --quiet install --upgrade pip
"$EVIDENCE/build-venv/bin/pip" --quiet install build==1.6.1
"$EVIDENCE/build-venv/bin/python" -m build > "$EVIDENCE/build.txt" 2>&1
ls -l dist/

echo "== clean install and examples outside the checkout =="
python3 -m venv "$EVIDENCE/clean-venv"
# Private working directory instead of a fixed shared path.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/link-doctor-package-check.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
scripts/verify-package.sh "$ROOT"/dist/link_doctor-*.whl \
  "$EVIDENCE/clean-venv/bin/python" "$WORK" "$ROOT"/dist/link_doctor-*.tar.gz \
  > "$EVIDENCE/verify-package.txt" 2>&1
tail -20 "$EVIDENCE/verify-package.txt"

echo "== distribution content inspection =="
"$EVIDENCE/build-venv/bin/python" scripts/inspect-distributions.py dist \
  > "$EVIDENCE/inspection.txt" 2>&1
cat "$EVIDENCE/inspection.txt"

echo "ci-local: DONE"
