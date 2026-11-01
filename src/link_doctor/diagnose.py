"""Small explicit-input GNU linker-order model; no shell and no ELF parser.

Only readelf inspects ELF. The bounded ar *container* reader is deliberate:
readelf skips non-ELF members, while ar/nm may load implicit BFD plugins.
We must reject metadata and preserve/check member identity and the index.
"""
from dataclasses import dataclass
import itertools
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .outcomes import assess_candidate, withhold_candidate

MAX_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 256
MAX_INPUTS = 64
SEARCH_LIMIT = 720
TOOL_TIMEOUT = 10
TOOLS = {"readelf": "/usr/bin/readelf", "ld.bfd": "/usr/bin/ld.bfd"}
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}


class Rejected(Exception):
    """Unsupported or untrustworthy input, not evidence of impossibility."""


class ExecutionFailure(Exception):
    """Tool, timeout or I/O failure, distinct from unsupported semantics."""


def native(tool, args, cwd):
    path = Path(TOOLS[tool]).resolve()
    try:
        mode = path.stat()
        if mode.st_uid != 0 or mode.st_mode & 0o022 or not stat.S_ISREG(mode.st_mode):
            raise Rejected(f"untrusted system tool: {path}")
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result = subprocess.run([str(path), *map(str, args)], cwd=cwd, env=ENV,
                                    stdout=out, stderr=err, timeout=TOOL_TIMEOUT, shell=False)
            if out.tell() > MAX_BYTES or err.tell() > MAX_BYTES:
                raise ExecutionFailure(f"{tool} output exceeds inspection limit")
            out.seek(0)
            err.seek(0)
            return result.returncode, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionFailure(f"{tool} unavailable or failed: {exc}") from exc


@dataclass(frozen=True)
class Member:
    identity: str
    defines: frozenset[str]
    requires: frozenset[str]
    weak_requires: frozenset[str] = frozenset()
    weak_defines: frozenset[str] = frozenset()
    commons: frozenset[str] = frozenset()
    data_defines: frozenset[str] = frozenset()
    sections: frozenset[str] = frozenset()


@dataclass
class Input:
    token: str
    position: int
    snapshot: Path
    members: list[Member]
    archive: bool


def normalize(argv):
    if len(argv) > MAX_INPUTS + 5 or any(len(t) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in t) for t in argv):
        raise Rejected("command exceeds limits or contains control characters")
    if not argv or argv[0] not in ("ld", "ld.bfd"):
        raise Rejected("only bare ld or ld.bfd is supported; paths and wrappers are rejected")
    inputs, entry, output = [], None, None
    depth = 0
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in ("-e", "-o"):
            if i + 1 >= len(argv) or not argv[i + 1] or argv[i + 1].startswith(("-", "@")):
                raise Rejected(f"missing or unsupported value for {token}")
            if token == "-e":
                if entry is not None or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", argv[i + 1]):
                    raise Rejected("entry must be a single ordinary symbol")
                entry = argv[i + 1]
            else:
                if output is not None:
                    raise Rejected("duplicate output option")
                output = argv[i + 1]
            i += 2
        elif token in ('--start-group','--end-group'):
            depth += 1 if token == '--start-group' else -1
            if depth < 0 or depth > 8: raise Rejected('unbalanced or too-deep archive group')
            i += 1
        elif token.startswith(("-", "@")) or Path(token).suffix not in (".o", ".a"):
            raise Rejected(f"unsupported flag or input: {token}")
        else:
            inputs.append((i, token))
            i += 1
    if depth: raise Rejected('unbalanced archive group')
    if not inputs or len(inputs) > MAX_INPUTS or entry is None or output is None:
        raise Rejected("require -e SYMBOL, -o OUTPUT and 1..64 explicit .o/.a inputs")
    return inputs, entry


def read_bytes(path):
    # Opening special files can block; reject before and after opening.
    if not stat.S_ISREG(path.stat().st_mode):
        raise Rejected(f"input is not a regular file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise Rejected(f"input type or size unsupported: {path}")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Rejected("input exceeds size limit")
    return data


def archive_members(data: bytes, member_limit=MAX_MEMBERS):
    if not data.startswith(b"!<arch>\n"):
        raise Rejected("only normal GNU indexed archives supported (no thin archives)")
    cursor, names, index, members = 8, b"", None, []
    while cursor < len(data):
        offset = cursor
        header = data[cursor:cursor + 60]
        if len(header) != 60 or header[58:] != b"`\n":
            raise Rejected("malformed archive header")
        try:
            size = int(header[48:58].decode("ascii").strip())
            name = header[:16].decode("ascii").rstrip()
        except (ValueError, UnicodeError) as exc:
            raise Rejected("malformed archive fields") from exc
        cursor += 60
        if size < 0 or cursor + size > len(data):
            raise Rejected("malformed archive member size")
        payload = data[cursor:cursor + size]
        cursor += size
        if size % 2:
            if data[cursor:cursor + 1] != b"\n":
                raise Rejected("malformed archive padding")
            cursor += 1
        if name == "/":
            if index is not None or members:
                raise Rejected("duplicate or misplaced archive index")
            index = payload
            continue
        if name == "//":
            if names or members:
                raise Rejected("duplicate or misplaced archive name table")
            names = payload
            continue
        if re.fullmatch(r"/\d+", name):
            n = int(name[1:])
            if n >= len(names) or (n and names[n - 2:n] != b"/\n"):
                raise Rejected("invalid GNU long member name offset")
            end = names.find(b"/\n", n)
            if end < 0:
                raise Rejected("unterminated archive member name")
            try:
                name = names[n:end].decode("ascii")
            except UnicodeError as exc:
                raise Rejected("non-ASCII archive member name") from exc
        else:
            name = name.removesuffix("/")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", name) or name == "__.LIBDEP":
            raise Rejected(f"unsupported archive member or dependency metadata: {name}")
        if any(m[1] == name for m in members):
            raise Rejected(f"duplicate archive member identity: {name}")
        members.append((offset, name, payload))
        if len(members) > member_limit:
            raise Rejected("archive member limit exceeded")
    if index is None or not members:
        if len(data) == 8:
            # A bare end-of-header is a valid GNU archive that provides
            # nothing (glibc 2.34+ compat stubs such as libpthread.a are
            # exactly these eight bytes and link natively).
            return [], b""
        raise Rejected("require a nonempty archive with GNU symbol index")
    return members, index


def inspect_elf(path, identity, cwd):
    if not path.read_bytes().startswith(b"\x7fELF"):
        raise Rejected(f"non-ELF member/input (scripts, bitcode and metadata unsupported): {identity}")
    code, text, errors = native("readelf", ["-W", "-h", "-S", "-s", path], cwd)
    if code or errors:
        raise Rejected(f"readelf rejected {identity}: {errors.strip()}")
    for expected in ("ELF64", "2's complement, little endian", "REL (Relocatable file)", "Advanced Micro Devices X86-64"):
        if expected not in text:
            raise Rejected(f"unsupported ELF format: {identity}")
    if re.search(r"\.gnu\.lto|\.llvm|\bGROUP\b|\bDYNSYM\b|\.gnu\.warning|\.gnu\.linkonce|\.gnu\.liblist", text):
        raise Rejected(f"LTO, groups, dynamic symbols, TLS or special linker sections unsupported: {identity}")
    sections = re.findall(r"^\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s*(.*?)\s+\d+\s+\d+\s+\d+\s*$", text, re.M)
    declared_sections = re.findall(r"Number of section headers:\s+(\d+)\s*$", text, re.M)
    section_indexes = [int(i) for i in re.findall(r"^\s*\[\s*(\d+)\]", text, re.M)]
    if (len(declared_sections) != 1
            or section_indexes != list(range(int(declared_sections[0])))
            or len(sections) != int(declared_sections[0]) - 1):
        raise Rejected(f"unreadable or partially parsed section headers: {identity}")
    if not sections or sum(t == "SYMTAB" for _, t, *_ in sections) != 1:
        raise Rejected(f"require one ordinary symbol table: {identity}")
    for name, kind, addr, off, size, es, flags in sections:
        if not set(flags) <= set("WAXMSI") or name.startswith((".init_array", ".fini_array", ".preinit_array")):
            raise Rejected(f"unsupported section semantics: {identity}: {name}")
    defines, requires = set(), set()
    weak_requires, weak_defines, commons, data_defines = set(), set(), set(), set()
    rows = re.findall(r"^[ \t]*\d+:[ \t]+\S+[ \t]+\S+[ \t]+(\S+)[ \t]+(\S+)[ \t]+(\S+)[ \t]+(\S+)(?:[ \t]+([^\n]*))?$", text, re.M)
    counts = re.findall(r"^Symbol table '.symtab' contains (\d+) entries:$", text, re.M)
    if len(counts) != 1 or not rows or len(rows) != int(counts[0]):
        raise Rejected(f"unreadable or partially parsed symbol table: {identity}")
    for kind, binding, visibility, section, symbol in rows:
        symbol = symbol.strip()
        if binding not in ("LOCAL", "GLOBAL", "WEAK") or kind in ("IFUNC", "TLS"):
            raise Rejected(f"unique/indirect/TLS symbols unsupported: {identity}: {symbol}")
        if binding == "LOCAL":
            continue
        if kind not in ("NOTYPE", "FUNC", "OBJECT") or visibility != "DEFAULT" or (section not in ("UND", "COM") and not section.isdigit()):
            raise Rejected(f"unsupported global symbol semantics: {identity}: {symbol}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", symbol):
            raise Rejected(f"unsupported symbol name: {identity}: {symbol}")
        if section == 'UND':
            (weak_requires if binding == 'WEAK' else requires).add(symbol)
        else:
            defines.add(symbol)
            if binding == 'WEAK': weak_defines.add(symbol)
            elif section == 'COM': commons.add(symbol)
            # GNU ELF common-demand eligibility is a strong, defined,
            # non-function symbol, not a writable/data section test. In this
            # supported grammar that includes NOTYPE even in .text; UND,
            # COM and WEAK were handled above and ABS remains unsupported.
            elif kind in ('OBJECT', 'NOTYPE'): data_defines.add(symbol)
    return Member(identity, frozenset(defines), frozenset(requires), frozenset(weak_requires),
                  frozenset(weak_defines), frozenset(commons), frozenset(data_defines),
                  sections=frozenset(name for name, *_ in sections))


def inspect(argv, cwd, scratch):
    positions, entry = normalize(argv)
    inputs, providers, seen = [], {}, {}
    total = 0
    for number, (position, token) in enumerate(positions):
        resolved = (cwd / token).resolve()
        if resolved in seen:
            prior = seen[resolved]
            if not prior.archive: raise Rejected('repeated explicit object input')
            inputs.append(Input(token, position, prior.snapshot, prior.members, True))
            continue
        data = read_bytes(cwd / token)
        total += len(data)
        if total > MAX_BYTES:
            raise Rejected("combined input size exceeds limit")
        snapshot = scratch / f"input{number}{Path(token).suffix}"
        snapshot.write_bytes(data)
        members = []
        if token.endswith(".a"):
            entries, index = archive_members(data)
            expected = set()
            for n, (offset, name, payload) in enumerate(entries):
                memberfile = scratch / f"member{number}-{n}.o"
                memberfile.write_bytes(payload)
                member = inspect_elf(memberfile, f"{token}({name})", scratch)
                members.append(member)
                expected.update((symbol, offset) for symbol in member.defines)
            # An empty archive has no index to validate; it provides nothing.
            if members:
                if len(index) < 4:
                    raise Rejected("malformed archive index")
                count = int.from_bytes(index[:4], "big")
                if count > MAX_BYTES // 4 or 4 + count * 4 > len(index):
                    raise Rejected("malformed archive index size")
                offsets = [int.from_bytes(index[4 + i * 4:8 + i * 4], "big") for i in range(count)]
                if offsets != sorted(offsets):
                    raise Rejected('noncanonical archive index order unsupported')
                raw_names = index[4 + count * 4:].split(b"\0")
                # GNU ar may append an extra zero byte for index alignment.
                if len(raw_names) < count + 1 or any(raw_names[count:]):
                    raise Rejected("malformed archive index names")
                try:
                    actual = [(s.decode("ascii"), o) for s, o in zip(raw_names[:count], offsets)]
                except UnicodeError as exc:
                    raise Rejected("unsupported archive index symbols") from exc
                if len(set(actual)) != len(actual) or set(actual) != expected:
                    raise Rejected("archive index does not exactly match inspected definitions")
        else:
            members = [inspect_elf(snapshot, token, scratch)]
        for member in members:
            for symbol in member.defines:
                providers.setdefault(symbol, []).append(member.identity)
        inputs.append(Input(token, position, snapshot, members, token.endswith(".a")))
        seen[resolved] = inputs[-1]
    # The output path may not alias an input: ld refuses "input file is the
    # same as output file" for any ordering, so the command can never link
    # natively and must not be reported as satisfied.
    resolved_output = (cwd / argv[argv.index("-o") + 1]).resolve()
    if resolved_output in seen:
        raise Rejected(f"output file is the same as input file: {resolved_output}")
    return inputs, entry, providers


def nodes_for(inputs, argv=None):
    from .driver import Node, Unit
    by_position = {}
    for item in inputs:
        units = [Unit(m.identity, set(m.defines), set(m.requires), set(m.weak_defines),
                      set(m.commons), set(m.data_defines), sections=set(m.sections)) for m in item.members]
        by_position[item.position] = Node(item.token, snapshot=item.snapshot, members=units,
                                          kind='archive' if item.archive else 'object', position=item.position)
    if argv is None: return list(by_position.values())
    roots, stack = [], []
    current = roots
    for i,t in enumerate(argv):
        if t == '--start-group':
            group = Node(t,kind='group',children=[])
            current.append(group); stack.append(current); current = group.children
        elif t == '--end-group': current = stack.pop()
        elif i in by_position: current.append(by_position[i])
    return roots


def simulate(inputs, entry):
    from .driver import model
    return model(nodes_for(inputs), entry)


def diagnose(argv, verify=False, cwd=None):
    if argv and argv[0] in ('gcc', 'g++'):
        from .driver import diagnose as driver_diagnose
        return driver_diagnose(argv, verify, cwd)
    report = {"schema_version": 1, "status": "unknown", "command": None, "verified": False,
              "verification": {"status": "not_requested"}, "original_command": list(argv)}
    try:
        with tempfile.TemporaryDirectory(prefix="link-doctor-") as directory:
            scratch = Path(directory)
            inputs, entry, providers = inspect(argv, Path(cwd or Path.cwd()), scratch)
            report["inputs"] = [{"path": item.token, "argv_index": item.position, "archive": item.archive,
                                 "members": [{"identity": m.identity, "defines": sorted(m.defines),
                                              "requires": sorted(m.requires)} for m in item.members]} for item in inputs]
            from .driver import Node, model, render
            from . import repair
            nodes = nodes_for(inputs, argv)
            original = model(nodes, entry)
            report["original"] = original
            report["ambiguity"] = {s:p for s,p in sorted(providers.items()) if len(p)>1}
            report["missed"] = [{"symbol": symbol, "consumer": consumer, "provider": provider}
                                for symbol in original["unresolved"] if symbol in providers
                                for consumer in original["consumers"][symbol] for provider in providers[symbol]]
            chosen, candidate, kind, attempts = repair.search(nodes, lambda ns:model(ns,entry), SEARCH_LIMIT,
                                                              lambda target:Node('--start-group',kind='group',children=target))
            report["search"] = {"attempts":attempts,"limit":SEARCH_LIMIT,"scope":"reorder, then up to four demand-directed repeats, then contiguous groups; archive spans only"}
            if chosen is None:
                report["diagnosis"] = 'duplicate_definitions' if original['duplicates'] else 'missing_provider' if any(s not in providers for s in original['unresolved']) else 'constrained_search_exhausted'
                report["reason"] = "No model-valid candidate found within constrained bounded search; not proof of impossibility."
                return report
            def tokens(ns):
                result=[]
                for n in ns:
                    if n.kind=='group': result.extend(['--start-group',*tokens(n.children),'--end-group'])
                    else: result.append(n.token)
                return result
            command = repair.command(argv,nodes,chosen,tokens)
            report.update(assess_candidate(command, candidate["unresolved"]).to_dict())
            report["command"] = list(command)
            report["candidate"] = candidate
            report["repair"] = kind
            report["provider_changes"] = repair.provider_changes(original,candidate)
            report["status"] = "already_satisfied" if not original["unresolved"] and not original['duplicates'] else "candidate"
            report["changes"] = [{"argv_index":n.position,"from":n.token,"to":tokens(chosen[id(n)])}
                                 for n in nodes if id(n) in chosen and tokens(chosen[id(n)]) != [n.token]]
            if verify:
                report["verification"] = {"status": "running"}
                for label, mapping in (("original", {}), ("candidate", chosen)):
                    replacements = {item.position:[str(item.snapshot)] for item in inputs}
                    for n in nodes:
                        if id(n) in mapping:
                            replacements[n.position] = [v for node in mapping[id(n)] for v in render(node)]
                    replacements[argv.index('-o')+1] = [str(scratch/(label+'.out'))]
                    isolated = [v for i,t in enumerate(argv) if i for v in replacements.get(i,[t])]
                    code, stdout, stderr = native("ld.bfd", isolated, scratch)
                    report["verification"][label] = {"returncode":code,"stdout":stdout,"stderr":stderr,"argv":[TOOLS['ld.bfd'],*isolated]}
                success = report["verification"]["candidate"]["returncode"] == 0
                original_ok = report["verification"]["original"]["returncode"] == 0
                agrees = original_ok == (not original["unresolved"] and not original['duplicates'])
                report["verified"] = success and agrees
                report["verification"]["status"] = "passed" if report["verified"] else "failed"
                if not report["verified"]:
                    # Withheld evidence, not instructions: edits of a candidate
                    # the native link rejected must not stay actionable.
                    withhold_candidate(report)
                    report.update(status="unknown", command=None,
                                  reason="Native link failed or disagreed with the supported-semantics model; candidate withheld.")
            return report
    except (Rejected, ExecutionFailure, OSError, ValueError) as exc:
        if report["verification"]["status"] == "running":
            report["verification"].update(status="error", reason=str(exc))
        status = "execution_failure" if isinstance(exc, (ExecutionFailure, OSError)) else "unsupported"
        report.update(status=status, command=None, verified=False, reason=str(exc))
        return report
