"""Run with `link-doctor` or `python -m link_doctor diagnose [--verify] [--json] -- ld ...`"""
import argparse
import json
import re
import shlex
import sys
from . import __version__
from .diagnose import diagnose

# File-derived text (symbol names, member identities, linker stderr) can
# carry raw terminal control bytes; text output escapes them instead of
# emitting them raw. JSON output needs no helper: json.dumps escapes
# control characters by design.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def safe(text):
    return _CONTROL.sub(lambda m: "\\x%02x" % ord(m.group(0)), str(text))


def main():
    parser = argparse.ArgumentParser(
        prog="link-doctor",
        description="Diagnose a deliberately restricted GNU static archive link order.")
    parser.add_argument("--version", action="version", version=f"link-doctor {__version__}")
    parser.add_argument("action", choices=["diagnose"])
    parser.add_argument("--verify", action="store_true", help="opt in to isolated native links; never run the binary")
    parser.add_argument("--cwd", help="resolve inputs and response files from this working directory")
    parser.add_argument("--json", action="store_true", help="emit structured evidence")
    args = sys.argv[1:]
    if "--" in args:
        split = args.index("--")
        options = parser.parse_args(args[:split])
        command = args[split + 1:]
    else:
        # --help and --version exit from within parse_args; anything else is a
        # command without the mandatory "--" separator and is a usage error.
        options = parser.parse_args(args)
        parser.error("separate the supplied linker argv with --")
        return 2
    report = diagnose(command, verify=options.verify, cwd=options.cwd)
    if options.json:
        print(json.dumps(report, indent=2))
    else:
        print("Status:", report["status"])
        if "reason" in report:
            print(safe(report["reason"]))
        if report.get('diagnosis'):
            print('Diagnosis:', report['diagnosis'])
        for visit in report.get("original", {}).get("archive_visits", []):
            if visit["not_extracted"]:
                members = visit['not_extracted']
                detail = ', '.join(safe(m) for m in members[:8])
                if len(members) > 8:
                    detail += f' … ({len(members) - 8} more; full list in JSON)'
                print(f"At {safe(visit['archive'])}: needed {', '.join(safe(s) for s in visit['needed_on_arrival']) or '(none)'}; "
                      f"did not extract {detail}")
        for symbol, consumers in report.get("original", {}).get("consumers", {}).items():
            print(f"Unresolved requirement {safe(symbol)} from {', '.join(safe(c) for c in consumers)}")
        for item in report.get("missed", []):
            print(f"Unresolved {safe(item['symbol'])}: consumer {safe(item['consumer'])}; missed provider {safe(item['provider'])}")
        for item in report.get("changes", []):
            print(f"argv[{item['argv_index']}]: {safe(item['from'])} -> {[safe(t) for t in item['to']]}")
        if report['status'] == 'already_satisfied':
            print('No repair needed: original command satisfies the supported model.')
        elif report.get('repair'):
            print('Repair strategy:', report['repair']['kind'])
        for symbol, providers in report.get('ambiguity', {}).items():
            winner = report.get('candidate', report.get('original', {})).get('selected_providers', {}).get(symbol)
            print(f"Alternative providers for {safe(symbol)}: {', '.join(safe(p) for p in providers)}; selected: {safe(winner)}")
        for change in report.get('provider_changes', []):
            print(f"WARNING: provider changed for {safe(change['symbol'])}: {safe(change['from'])} -> {safe(change['to'])}")
        if report.get('ambiguity') or report.get('provider_changes'):
            print('Provider choice can change behavior: link success is not program equivalence.')
        if report["command"]:
            print("Model-validated command:", safe(shlex.join(report["command"])))
        print("Native verification:", report["verification"]["status"])
        for label in ("original", "candidate"):
            result = report["verification"].get(label)
            if result:
                print(f"  {label} linker exit: {result['returncode']}")
                if result["stderr"]:
                    print(safe(result["stderr"]).rstrip())
        if not report["verified"]:
            print("Not linker-verified; model satisfaction is not proof of a successful native link.")
    return 0 if report["status"] in ("candidate", "already_satisfied") else 2


if __name__ == "__main__":
    raise SystemExit(main())
