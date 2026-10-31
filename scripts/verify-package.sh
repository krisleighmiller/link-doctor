#!/bin/sh
# Verify a built distribution the way a new user would consume it:
# clean venv, install the wheel (and, if given, the sdist), run the CLI
# outside the checkout with PYTHONPATH unset, and run every shipped example
# from the installed package. Usage:
#   scripts/verify-package.sh <wheel> <clean-venv-python> <workdir> [sdist]
# The workdir must be outside the source checkout.
set -eu
WHEEL=$1
PY=$2
WORK=$3
case "$WORK" in
  ""|"/") echo "verify-package: refusing unsafe workdir '$WORK'"; exit 1 ;;
esac
# Resolve to absolute paths: everything is relative to the caller's cwd only
# until the first cd below.
PY=$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")
WHEEL=$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")
SDIST_ARG=${4:-}
if [ -n "$SDIST_ARG" ]; then
  SDIST=$(cd "$(dirname "$SDIST_ARG")" && pwd)/$(basename "$SDIST_ARG")
fi
mkdir -p "$WORK"
WORK=$(cd "$WORK" && pwd)
cd "$WORK"
rm -rf pkgtest
mkdir pkgtest
"$PY" -m venv pkgtest/venv
pkgtest/venv/bin/pip --quiet install "$WHEEL"
cd pkgtest
# Also prove the sdist installs and runs, not just the wheel.
if [ -n "${SDIST:-}" ]; then
  rm -rf sdist-venv
  venv/bin/python -m venv sdist-venv
  sdist-venv/bin/pip --quiet install "$SDIST"
  RESOLVED=$(env -u PYTHONPATH sdist-venv/bin/link-doctor --version)
  echo "sdist install: $RESOLVED"
fi
# CLI surface, installed module genuinely used.
env -u PYTHONPATH venv/bin/link-doctor --version
env -u PYTHONPATH venv/bin/link-doctor --help > help.txt
grep -q "diagnose" help.txt
RESOLVED=$(env -u PYTHONPATH venv/bin/python -c 'import link_doctor; print(link_doctor.__file__)')
case "$RESOLVED" in
  *"$WORK"/pkgtest/venv/*) : ;;
  *) echo "FAIL: link_doctor resolved outside the clean venv: $RESOLVED"; exit 1 ;;
esac
echo "installed module: $RESOLVED"
EXAMPLES=$(env -u PYTHONPATH venv/bin/python -c 'from pathlib import Path; import link_doctor; print(Path(link_doctor.__file__).parent / "examples")')
for example in 01-basic-order 02-cycle 03-missing-provider 04-ambiguity 05-unsupported; do
  cp -r "$EXAMPLES/$example" .
  case $example in
    01-basic-order) INPUTS="libleaf.a libprovider.a"; EXPECTED="candidate" ;;
    02-cycle) INPUTS="libstart.a libmiddle.a"; EXPECTED="candidate" ;;
    03-missing-provider) INPUTS=""; EXPECTED="missing_provider" ;;
    04-ambiguity) INPUTS="libfirst.a libsecond.a"; EXPECTED="candidate" ;;
    05-unsupported) INPUTS="-L."; EXPECTED="unsupported" ;;
  esac
  ( cd "$example" && sh build.sh )
  set +e
  ( cd "$example" && env -u PYTHONPATH "$WORK/pkgtest/venv/bin/link-doctor" diagnose --json \
    -- ld -e main -o app main.o $INPUTS ) > "$example.json" 2>/dev/null
  STATUS=$?
  set -e
  case $EXPECTED in
    candidate) WANT=0 ;;
    *) WANT=2 ;;
  esac
  if [ "$STATUS" -ne "$WANT" ]; then
    echo "FAIL: $example expected exit $WANT, got $STATUS"
    exit 1
  fi
  env -u PYTHONPATH "$WORK/pkgtest/venv/bin/python" -c '
import json, sys
expected, path = sys.argv[1], sys.argv[2]
report = json.load(open(path))
if expected == "missing_provider":
    assert report["status"] == "unknown" and report["diagnosis"] == expected, report
else:
    assert report["status"] == expected, report
print(path + ": status=" + report["status"] + " as expected")
' "$EXPECTED" "$example.json"
done
echo "verify-package: PASS"
