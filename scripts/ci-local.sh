#!/bin/sh
# Local equivalent of .github/workflows/ci.yml. Records the toolchain, runs
# the full regression/differential suite, builds the distributions, then
# verifies a clean install outside the checkout exactly as the workflow
# would. Honest status: hosted CI has never run; this script is the
# actually-exercised path. Usage: scripts/ci-local.sh [evidence-dir]
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
"$EVIDENCE/build-venv/bin/pip" --quiet install --upgrade pip build
"$EVIDENCE/build-venv/bin/python" -m build > "$EVIDENCE/build.txt" 2>&1
ls -l dist/

echo "== clean install and examples outside the checkout =="
python3 -m venv "$EVIDENCE/clean-venv"
WORK="${TMPDIR:-/tmp}/link-doctor-package-check"
scripts/verify-package.sh "$ROOT"/dist/link_doctor-*.whl \
  "$EVIDENCE/clean-venv/bin/python" "$WORK" "$ROOT"/dist/link_doctor-*.tar.gz \
  > "$EVIDENCE/verify-package.txt" 2>&1
tail -20 "$EVIDENCE/verify-package.txt"

echo "== distribution content inspection =="
"$EVIDENCE/build-venv/bin/python" scripts/inspect-distributions.py dist \
  > "$EVIDENCE/inspection.txt" 2>&1
cat "$EVIDENCE/inspection.txt"

echo "ci-local: DONE"
