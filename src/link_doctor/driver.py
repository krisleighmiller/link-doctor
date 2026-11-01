"""Restricted GNU driver intake. Discovery never receives the supplied command.

Only system GCC templates and runtime scripts are trusted as *syntax sources*;
every selected child is inspected independently. No compiler/plugin is used to
link snapshots: verification uses ld.bfd after rejecting LTO inputs.
"""
from dataclasses import dataclass, field, replace
from pathlib import Path
import hashlib
import itertools
import re
import shlex
import tempfile
from . import diagnose as base
from .outcomes import withhold_candidate

MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 2048

# Symbols the default GNU linker script assigns, PROVIDE-style, or the ELF
# backend supplies when referenced (verified natively against ld.bfd 2.46
# for both driver and bare-ld links): they are never archive members, so a
# reference to one must not be reported as a missing provider. PROVIDE
# semantics mean an explicit definition still wins in the walk; only what
# remains unresolved at the end is provided. __start_/__stop_ section
# bounds additionally require their named section among the linked inputs.
LINKER_SCRIPT_SYMBOLS = frozenset({
    '__executable_start', '__etext', '_etext', 'etext',
    '__edata', '_edata', 'edata', '__bss_start',
    '_end', 'end', '__ehdr_start',
    '__preinit_array_start', '__preinit_array_end',
    '__init_array_start', '__init_array_end',
    '__fini_array_start', '__fini_array_end',
    '__rela_iplt_start', '__rela_iplt_end', '__tdata_start',
})
SECTION_BOUND = re.compile(r'__s(?:tart|top)_([A-Za-z_][A-Za-z0-9_]*)')


class MissingLibrary(base.Rejected):
    """No library matched the supported native search order."""

@dataclass
class Unit:
    identity: str
    defines: set
    requires: set
    weak: set = field(default_factory=set)
    commons: set = field(default_factory=set)
    data_defines: set = field(default_factory=set)
    sections: set = field(default_factory=set)

@dataclass
class Node:
    token: str
    path: Path | None = None
    snapshot: Path | None = None
    members: list = field(default_factory=list)
    kind: str = 'object'
    children: list = field(default_factory=list)
    position: int = -1
    as_needed: bool = False
    runtime: bool = False


def trusted(path):
    path = Path(path).resolve()
    for p in (path, *path.parents):
        s = p.stat()
        if s.st_uid != 0 or s.st_mode & 0o022:
            raise base.Rejected(f'untrusted toolchain path: {p}')
    return path


STATE_FLAGS = {'--as-needed','--no-as-needed','--push-state','--pop-state',
               '-Bstatic','-Bdynamic','--start-group','--end-group'}


def response_tokens(text):
    # GNU buildargv rules: whitespace separates, either quote protects space,
    # and backslash escapes the next character even inside single quotes.
    tokens, current, quote, escaped, started = [], [], None, False, False
    for char in text:
        if escaped:
            current.append(char); escaped=False; started=True
        elif char == chr(92):
            escaped=True; started=True
        elif quote:
            if char == quote: quote=None
            else: current.append(char)
        elif char in ('"', "'"):
            quote=char; started=True
        elif char.isspace():
            if started: tokens.append(''.join(current)); current=[]; started=False
        else:
            current.append(char); started=True
    if quote or escaped:
        raise base.Rejected('unterminated response quote/escape')
    if started: tokens.append(''.join(current))
    return tokens


def expand_responses(argv, cwd):
    total, records = 0, []
    def expand(tokens, active=()):
        nonlocal total
        result=[]
        if len(active)>8: raise base.Rejected('response nesting limit')
        for token in tokens:
            if token.startswith('@'):
                path=(cwd/token[1:]).resolve()
                if path in active: raise base.Rejected('response file cycle')
                try:
                    data=base.read_bytes(path)
                except FileNotFoundError as exc:
                    # A missing response file is a command-intake problem,
                    # not a tool failure.
                    raise base.Rejected('missing response file: ' + token) from exc
                total+=len(data)
                if total>65536: raise base.Rejected('response byte limit')
                records.append({'path':str(path),'sha256':hashlib.sha256(data).hexdigest()})
                result.extend(expand(response_tokens(data.decode('utf-8')),(*active,path)))
            else: result.append(token)
            if len(result)>256: raise base.Rejected('response argument limit')
        return result
    return expand(argv), records


def user_args(argv):
    """Normalize supported forwarding to one canonical token per linker input.

    The returned command is an argv array, never a shell program. The original
    response/wrapper spelling remains in original_command, not in the repair.
    A forwarded -L directory must keep its -Wl,-L spelling in the canonical
    command: the driver searches its own -L directories before any forwarded
    one, so respelling changes native selection. Each result entry is
    (canonical position, normalized token, forwarded) so callers can place
    forwarded -L directories at their true native search position.
    """
    flat=[argv[0]]; forwarded=[False]; i=1
    while i<len(argv):
        t=argv[i]; i+=1
        if t.startswith('-Wl,'):
            for part in t[4:].split(','):
                flat.append(part); forwarded.append(True)
        elif t=='-Xlinker':
            if i==len(argv): raise base.Rejected('missing -Xlinker argument')
            flat.append(argv[i]); forwarded.append(True); i+=1
        else:
            flat.append(t); forwarded.append(False)
    if len(flat)>256 or any(not t or len(t)>8192 or any(ord(c)<32 or ord(c)==127 for c in t) for t in flat):
        raise base.Rejected('command exceeds limits, empty token or controls')
    result, canonical, output, i, states, groups = [], [flat[0]], False, 1, 0, 0
    while i<len(flat):
        t=flat[i]; i+=1
        fwd=forwarded[i-1]
        if t in ('-L','-l','-o'):
            if i==len(flat) or flat[i].startswith(('-','@')): raise base.Rejected('missing option value')
            value=flat[i]; i+=1
            if t=='-o':
                if output: raise base.Rejected('duplicate output option')
                output=True; canonical.extend(['-o',value]); continue
            t+=value
        if t in STATE_FLAGS:
            states += (1 if t=='--push-state' else -1 if t=='--pop-state' else 0)
            groups += (1 if t=='--start-group' else -1 if t=='--end-group' else 0)
            if min(states,groups)<0: raise base.Rejected('unbalanced linker state/group')
            result.append((len(canonical),t,fwd)); canonical.append('-Wl,'+t)
        elif (t.startswith(('-L','-l')) and len(t)>2) or (not t.startswith(('-','@')) and Path(t).suffix in ('.o','.a')):
            result.append((len(canonical),t,fwd))
            canonical.append('-Wl,'+t if fwd and t.startswith('-L') else t)
        else: raise base.Rejected('unsupported driver argument: '+t)
    if states or groups: raise base.Rejected('unbalanced linker state/group')
    if not any(not t.startswith('-') or t.startswith('-l') for _,t,_ in result): raise base.Rejected('require link-only inputs')
    return canonical,result


def template(driver, scratch):
    # Fixed synthetic argv: neither user paths nor flags reach driver discovery.
    # The resolved tool is returned, not written into module state.
    tool = str(trusted('/usr/bin/' + driver))
    code, out, err = base.native(tool, ['-###', 'DOCTOR_INPUT.o', '-o', 'DOCTOR_OUTPUT'], scratch)
    if code:
        raise base.ExecutionFailure('compiler discovery failed: ' + err)
    commands = [shlex.split(line) for line in err.splitlines() if '/collect2 ' in line]
    if len(commands) != 1:
        raise base.Rejected('unsupported compiler discovery format')
    args = commands[0][1:]
    clean, i = [], 0
    while i < len(args):
        t = args[i]
        if t == '-plugin':
            plugin = trusted(args[i+1])
            if plugin.name != 'liblto_plugin.so':
                raise base.Rejected('unknown driver-owned plugin')
            i += 2
        elif t.startswith('-plugin-opt='):
            # These are emitted only by the fixed trusted discovery. We never
            # execute collect2/plugins; all LTO/bitcode is rejected on inspection.
            i += 1
        else:
            clean.append(t); i += 1
    if clean.count('DOCTOR_INPUT.o') != 1 or clean.count('DOCTOR_OUTPUT') != 1:
        raise base.Rejected('compiler template markers missing')
    return clean, tool


class Inventory:
    def __init__(self, cwd, scratch, search):
        self.cwd, self.scratch, self.search = cwd, scratch, search
        self.total, self.count = 0, 0
        self.cache, self.active, self.receipts = {}, set(), []
        self.dependencies = {}

    def resolve(self, token, static=False, script_filename=False):
        if token.startswith('-l'):
            name = token[2:]
            names = [name[1:]] if name.startswith(':') else (['lib'+name+'.a'] if static else ['lib'+name+'.so', 'lib'+name+'.a'])
            for directory in self.search:
                for name in names:
                    p = directory / name
                    if p.exists():
                        return p.resolve()
            raise MissingLibrary('library not found: ' + token)
        p = Path(token)
        direct = p if p.is_absolute() else self.cwd / p
        # GNU script INPUT filenames try the original cwd before -L paths.
        # This is not -l: selection, nor lookup relative to the script's parent.
        # Once selected, even invalid/user-owned files must not be skipped.
        if script_filename and not p.is_absolute() and not direct.exists():
            for directory in self.search:
                candidate = directory / p
                if candidate.exists():
                    return candidate.resolve()
            raise MissingLibrary('script input not found: ' + token)
        return direct.resolve()

    def load(self, token, runtime=False, static=False, script_filename=False):
        path = self.resolve(token, static, script_filename)
        # Runtime *syntax* does not confer trusted object semantics on a -L
        # override. Re-evaluate the actual child independently, including parents.
        if runtime:
            try: trusted(path)
            except base.Rejected: runtime = False
        if path in self.active:
            raise base.Rejected('recursive runtime dependency/script: ' + str(path))
        if path in self.cache:
            return self.cache[path]
        data = base.read_bytes(path)
        self.total += len(data); self.count += 1
        if self.total > MAX_TOTAL or self.count > MAX_FILES:
            raise base.Rejected('runtime inventory limit exceeded')
        snapshot = self.scratch / ('file' + str(self.count) + path.suffix)
        snapshot.write_bytes(data)
        node = Node(token, path, snapshot, runtime=runtime)
        self.active.add(path)
        self.receipts.append({'path':str(path), 'selection':token,
                              'lookup_context':{'kind':'library_search' if token.startswith('-l') else 'script_filename' if script_filename else 'explicit_path',
                                                'cwd':str(self.cwd), 'search_paths':list(map(str,self.search))},
                              'sha256':hashlib.sha256(data).hexdigest(), 'bytes':len(data)})
        if data.startswith(b'!<arch>\n'):
            node.kind = 'archive'
            entries, index = base.archive_members(data, 1024 if runtime else base.MAX_MEMBERS)
            expected = set()
            for n, (offset, name, payload) in enumerate(entries):
                memberpath = self.scratch / (snapshot.name + '.' + str(n) + '.o')
                memberpath.write_bytes(payload)
                member = self.elf(memberpath, str(path)+'('+name+')', runtime)
                node.members.append(member)
                expected.update((s, offset) for s in member.defines)
            # An archive with no members and no index (eight header bytes,
            # e.g. glibc 2.34+ -lpthread/-ldl stubs) provides nothing and is
            # a valid input; only a present-but-wrong index is malformed.
            if entries:
                self.index(index, expected)
        elif data.startswith(b'\x7fELF'):
            code, header, err = base.native('readelf', ['-W', '-h', snapshot], self.scratch)
            if code or err:
                raise base.Rejected('invalid ELF: ' + str(path))
            shared = 'DYN (Shared object file)' in header
            if shared:
                if not runtime:
                    raise base.Rejected('user shared libraries outside restricted runtime scope')
                trusted(path)
                node.kind = 'shared'
            node.members = [self.elf(snapshot, str(path), runtime, shared)]
            if shared:
                code, text, err = base.native('readelf', ['-W', '-d', snapshot], self.scratch)
                if code or err:
                    raise base.Rejected('invalid dynamic table')
                counts = re.findall(r'Dynamic section at offset .* contains (\d+) entries:', text)
                rows = re.findall(r'^\s*0x[0-9a-f]+\s+\(([^)]+)\)\s+(.*)$', text, re.M)
                if len(counts)!=1 or len(rows)!=int(counts[0]) or not rows or rows[-1][0]!='NULL':
                    raise base.Rejected('partial dynamic table: ' + str(path))
                if any(tag in ('RPATH','RUNPATH','FILTER','AUXILIARY','AUDIT','DEPAUDIT') for tag,_ in rows):
                    raise base.Rejected('unsupported dynamic search/filter/audit semantics')
                names = re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', text)
                if len(names)!=sum(tag=='NEEDED' for tag,_ in rows):
                    raise base.Rejected('partial dynamic dependencies')
                for name in names:
                    if not re.fullmatch(r'(?:lib|ld-)[A-Za-z0-9_+.-]+\.so(?:\.[0-9]+)*', name):
                        raise base.Rejected('dependency name/path unsupported')
                    dep = self.load('-l:'+name, True)
                    self.dependencies[name] = dep
        else:
            if not runtime:
                raise base.Rejected('user linker scripts/bitcode unsupported: ' + str(path))
            trusted(path)
            node.kind = 'script'
            node.children = self.script(data.decode('ascii'), static)
        self.active.remove(path)
        for receipt in self.receipts:
            if receipt['path']==str(path):
                receipt.update(kind=node.kind, trusted_runtime=runtime, validated=True)
                break
        self.cache[path] = node
        return node

    def index(self, index, expected):
        if len(index) < 4:
            raise base.Rejected('malformed archive index')
        count = int.from_bytes(index[:4], 'big')
        if 4 + count * 4 > len(index):
            raise base.Rejected('malformed archive index size')
        names = index[4+count*4:].split(b'\0')
        if len(names) < count+1 or any(names[count:]):
            raise base.Rejected('malformed archive index names')
        actual = [(s.decode('ascii'), int.from_bytes(index[4+4*i:8+4*i], 'big')) for i,s in enumerate(names[:count])]
        offsets = [o for s,o in actual]
        if offsets != sorted(offsets):
            raise base.Rejected('noncanonical archive index order unsupported')
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise base.Rejected('archive index does not match definitions')

    def elf(self, path, identity, runtime, shared=False):
        code, text, err = base.native('readelf', ['-W','-h','-S', '--dyn-syms' if shared else '-s', path], self.scratch)
        if code or err:
            raise base.Rejected('readelf rejected ' + identity + ': ' + err)
        for expected in ('ELF64', "2's complement, little endian", 'Advanced Micro Devices X86-64', 'DYN (Shared object file)' if shared else 'REL (Relocatable file)'):
            if expected not in text:
                raise base.Rejected('incompatible ELF format: ' + identity)
        if re.search(r'\.gnu\.lto|\.llvm|\.gnu\.liblist', text) or (not runtime and re.search(r'\.gnu\.warning|\.gnu\.linkonce', text)):
            raise base.Rejected('unsupported special ELF sections: ' + identity)
        if not runtime and re.search(r'^\s*\[\s*\d+\].*\bGROUP\b', text, re.M):
            raise base.Rejected('section groups/TLS outside runtime scope: ' + identity)
        indexes = [int(i) for i in re.findall(r'^\s*\[\s*(\d+)\]', text, re.M)]
        counts = re.findall(r'Number of section headers:\s+(\d+)\s*$', text, re.M)
        sections = re.findall(r'^\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s*(.*?)\s+\d+\s+\d+\s+\d+\s*$', text, re.M)
        if len(counts)!=1 or indexes != list(range(int(counts[0]))) or len(sections)!=int(counts[0])-1:
            raise base.Rejected('partial section table: ' + identity)
        for name, kind, addr, off, size, es, flags in sections:
            allowed = set('WAXMSIGT') if runtime else set('WAXMSI')
            if not set(flags) <= allowed:
                raise base.Rejected('unsupported section flags: ' + identity + ': ' + name)
        rows = re.findall(r'^[ \t]*\d+:[ \t]+\S+[ \t]+\S+[ \t]+(\S+)[ \t]+(\S+)[ \t]+(\S+)[ \t]+(\S+)(?:[ \t]+([^\n]*))?$',text,re.M)
        counts = re.findall(r"^Symbol table '(?:.symtab|.dynsym)' contains (\d+) entries:$", text, re.M)
        if runtime and not counts and not rows and not any(s[1] in ('SYMTAB','DYNSYM') for s in sections):
            return Unit(identity, set(), set())
        if len(counts)!=1 or len(rows)!=int(counts[0]):
            raise base.Rejected('partial symbol table: ' + identity)
        definitions, requirements, weak = set(), set(), set()
        commons, data_defines = set(), set()
        for kind, binding, visibility, section, symbol in rows:
            if binding == 'LOCAL':
                continue
            if binding not in (('GLOBAL','WEAK','UNIQUE') if shared else ('GLOBAL','WEAK')):
                raise base.Rejected('unsupported unique/binding: ' + identity)
            if not runtime and (kind in ('TLS','IFUNC') or visibility != 'DEFAULT' or section == 'ABS'):
                raise base.Rejected('unsupported symbol semantics: ' + identity)
            symbol = re.sub(r' \(\d+\)$', '', symbol.strip())
            if not symbol:
                raise base.Rejected('empty nonlocal symbol')
            if section == 'UND':
                if binding != 'WEAK' and not shared:
                    requirements.add(symbol)
            elif not shared or visibility in ('DEFAULT','PROTECTED'):
                definitions.add(symbol)
                if '@@' in symbol:
                    definitions.add(symbol.split('@@')[0])
                    definitions.add(symbol.replace('@@','@'))
                if binding == 'WEAK':
                    weak.add(symbol)
                elif section == 'COM': commons.add(symbol)
                # As in inspect_elf: GNU ELF tests strong non-function
                # definitions, not section writability or OBJECT alone.
                elif binding == 'GLOBAL' and kind in ('OBJECT', 'NOTYPE'):
                    data_defines.add(symbol)
        return Unit(identity, definitions, requirements, weak, commons, data_defines,
                    sections=frozenset(name for name,*_ in sections))

    def script(self, text, static):
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
        tokens = re.findall(r'[^\s(),]+|[(),]', text)
        index = 0
        def sequence(end=False, depth=0):
            nonlocal index
            if depth > 16:
                raise base.Rejected('runtime script nesting limit')
            nodes = []
            while index < len(tokens):
                t = tokens[index]; index += 1
                if t == ')':
                    if not end: raise base.Rejected('unbalanced runtime script')
                    return nodes
                if t == ',': continue
                if t in ('GROUP','INPUT','AS_NEEDED','OUTPUT_FORMAT'):
                    if index >= len(tokens) or tokens[index] != '(':
                        raise base.Rejected('malformed runtime script')
                    index += 1
                    if t == 'OUTPUT_FORMAT':
                        if tokens[index:index+2] != ['elf64-x86-64', ')']:
                            raise base.Rejected('unsupported runtime output format')
                        index += 2
                    else:
                        children = sequence(True, depth+1)
                        nodes.append(Node(t, kind='group' if t == 'GROUP' else 'as_needed' if t == 'AS_NEEDED' else 'script', children=children))
                elif t.startswith('-l'):
                    nodes.append(self.load(t, True, static))
                elif t.startswith('/') or re.fullmatch(r'(?:[A-Za-z0-9_+.-]+/)*[A-Za-z0-9_+.-]+\.(?:so(?:\.[0-9]+)*|a)', t):
                    nodes.append(self.load(t, True, static, script_filename=True))
                else:
                    raise base.Rejected('unsupported runtime script directive/input: ' + t)
            if end: raise base.Rejected('unterminated runtime script')
            return nodes
        return sequence()


def model(nodes, entry='_start'):
    # Static resolution only. Runtime DSOs retain the restricted Phase 3
    # treatment; overlapping user/runtime definitions are refused upstream.
    defined = {'_GLOBAL_OFFSET_TABLE_'}
    needed = {entry:['entry point']}
    extracted, trace, visits, selected = set(), [], [], {}
    seen_visits = set()
    pending_commons, linked_sections = set(), set()
    duplicate = set()
    rank = {'shared':0, 'weak':1, 'common':2, 'strong':3}
    def include(unit, shared=False, reason='selected input'):
        extracted.add(unit.identity)
        if not shared: linked_sections.update(unit.sections)
        for s in sorted(unit.defines):
            strength = 'shared' if shared else 'weak' if s in unit.weak else 'common' if s in unit.commons else 'strong'
            old = selected.get(s)
            if old and strength == old['strength'] == 'strong' and old['member'] != unit.identity:
                duplicate.add(s)
            if not old or rank[strength] > rank[old['strength']]:
                selected[s] = {'member':unit.identity,'strength':strength}
                if strength == 'common':
                    selected[s]['coalesced_members'] = [unit.identity]
                    pending_commons.add(s)
                else:
                    # The set of pending common symbols is maintained here,
                    # incrementally: rescanning every selected symbol for
                    # every archive member on every pass is quadratic in
                    # runtime library size.
                    pending_commons.discard(s)
            elif strength == old['strength'] == 'common':
                old['coalesced_members'] = sorted(set([*old['coalesced_members'],unit.identity]))
        defined.update(unit.defines)
        for s in unit.defines: needed.pop(s, None)
        for s in sorted(unit.requires - defined): needed.setdefault(s, []).append(unit.identity)
        trace.append({'member':unit.identity,'reason':reason,'unresolved':sorted(needed)})
    def visit(node, as_needed=False):
        if node.kind == 'flag': return
        if node.kind in ('script','group','as_needed'):
            while True:
                before = len(extracted)
                for child in node.children: visit(child, as_needed or node.as_needed or node.kind=='as_needed')
                if node.kind != 'group' or len(extracted)==before: break
        elif node.kind == 'archive':
            arrival = sorted(needed)
            while True:
                before = len(extracted)
                for m in node.members:
                    # Common demand pulls strong non-function definitions
                    # (OBJECT or NOTYPE), not weak or other common symbols.
                    common_demand = m.data_defines & pending_commons
                    demand = m.defines.intersection(needed) | common_demand
                    if m.identity not in extracted and demand:
                        include(m, reason='requires ' + ', '.join(sorted(demand)))
                if len(extracted)==before: break
            not_extracted = [m.identity for m in node.members if m.identity not in extracted]
            # Identical visit records carry no evidence; group passes must
            # not repeat them as noise.
            if (node.token, tuple(arrival), tuple(not_extracted)) not in seen_visits:
                seen_visits.add((node.token, tuple(arrival), tuple(not_extracted)))
                visits.append({'archive':node.token,'needed_on_arrival':arrival,'not_extracted':not_extracted})
        elif node.kind == 'shared':
            m = node.members[0]
            if m.identity not in extracted and (not (as_needed or node.as_needed) or m.defines.intersection(needed)):
                include(m, True)
        else:
            if node.members[0].identity not in extracted: include(node.members[0])
    for n in nodes: visit(n)
    for s in list(needed):
        provided = s in LINKER_SCRIPT_SYMBOLS
        if not provided:
            bound = SECTION_BOUND.fullmatch(s)
            provided = bound is not None and bound.group(1) in linked_sections
        if provided:
            # Default-script/orphan-section semantics: provided only when no
            # explicit definition satisfied the reference during the walk.
            needed.pop(s)
    return {'unresolved':sorted(needed), 'consumers':needed,'extracted':sorted(extracted),'trace':trace,'archive_visits':visits,'duplicates':sorted(duplicate), 'selected_providers':selected}


def render(node):
    if node.kind == 'flag': return [node.token]
    if node.kind in ('script','group','as_needed'):
        args = [a for c in node.children for a in render(c)]
        if node.kind=='group': return ['--start-group',*args,'--end-group']
        if node.kind=='as_needed': return ['--push-state','--as-needed',*args,'--pop-state']
        return args
    return [str(node.snapshot)]


def diagnose(argv, verify=False, cwd=None):
    report = {'schema_version':1,'status':'unknown','command':None,'original_command':list(argv),'verified':False,'verification':{'status':'not_requested'}}
    try:
        cwd = Path(cwd or Path.cwd()).resolve()
        expanded, response_receipts = expand_responses(argv, cwd)
        argv, positions = user_args(expanded)  # validate before any subprocess
        report['normalized_command'] = argv
        report['responses'] = response_receipts
        with tempfile.TemporaryDirectory(prefix='link-doctor-driver-') as directory:
            scratch = Path(directory)
            args, tool_path = template(argv[0], scratch)
            report['discovery'] = {'driver':tool_path,
                                   'argv':['-###','DOCTOR_INPUT.o','-o','DOCTOR_OUTPUT'],
                                   'linker_template':args,
                                   'plugin_policy':'trusted GCC liblto_plugin discovery only; never execute plugins; reject LTO/bitcode'}
            # Native search order: direct driver -L directories come before
            # the driver's own -L directories; forwarded -Wl,-L directories
            # reach the linker only at their input position, after all of
            # them. They are appended to the live search list during the
            # walk below.
            search = [(cwd/Path(t[2:])).resolve() for _,t,f in positions if t.startswith('-L') and not f] \
                   + [Path(t[2:]) for t in args if t.startswith('-L')]
            inventory = Inventory(cwd,scratch,search)
            nodes, link, stack, as_needed = [], [], [], False
            i = 0
            while i < len(args):
                t = args[i]; i += 1
                if t.startswith('-L'): continue
                if t == 'DOCTOR_INPUT.o':
                    state_stack, group_stack, static = [], [], False
                    user_objects = set()
                    user_inputs = set()
                    for pos, token, fwd in positions:
                        if token.startswith('-L'):
                            # A forwarded -L reaches the linker at its input
                            # position, after every driver -L directory.
                            if fwd: inventory.search.append((cwd/Path(token[2:])).resolve())
                            continue
                        if token in STATE_FLAGS:
                            if token=='--push-state': state_stack.append((as_needed,static))
                            elif token=='--pop-state': as_needed,static=state_stack.pop()
                            elif token in ('--as-needed','--no-as-needed'): as_needed=token=='--as-needed'
                            elif token in ('-Bstatic','-Bdynamic'): static=token=='-Bstatic'
                            if token=='--start-group':
                                n=Node(token,kind='group')
                                (group_stack[-1].children if group_stack else nodes).append(n)
                                if not group_stack: link.append(n)
                                group_stack.append(n)
                            elif token=='--end-group': group_stack.pop()
                            else:
                                n=Node(token,kind='flag')
                                (group_stack[-1].children if group_stack else nodes).append(n)
                                if not group_stack: link.append(n)
                        else:
                            n=replace(inventory.load(token, runtime=token in ('-lm','-lc','-lgcc','-lgcc_s','-lstdc++'), static=static), token=token, position=pos, as_needed=as_needed)
                            if n.kind=='object' and n.path in user_objects:
                                raise base.Rejected('duplicate explicit object: ' + str(n.path))
                            if n.kind=='object': user_objects.add(n.path)
                            user_inputs.add(n.path)
                            (group_stack[-1].children if group_stack else nodes).append(n)
                            if not group_stack: link.append(n)
                    if static: raise base.Rejected('static runtime unsupported; restore -Bdynamic')
                    # The output path may not alias an input: ld refuses
                    # "input file is the same as output file" for any order,
                    # so this command can never link natively. Without -o
                    # the driver's default a.out cannot be a supported input.
                    if '-o' in argv:
                        resolved_out = (cwd/Path(argv[argv.index('-o')+1])).resolve()
                        if resolved_out in user_inputs:
                            raise base.Rejected('output file is the same as input file: ' + str(resolved_out))
                elif t in ('-o','-m','-dynamic-linker','-z'):
                    value = args[i]; i += 1
                    if t=='-o': value='DOCTOR_OUTPUT'
                    elif t=='-m' and value!='elf_x86_64': raise base.Rejected('unsupported linker target')
                    elif t=='-z' and value not in ('now','relro'): raise base.Rejected('unsupported template -z')
                    elif t=='-dynamic-linker': trusted(value)
                    link.extend([t,value])
                elif t in ('--as-needed','--no-as-needed','--push-state','--pop-state'):
                    if t=='--push-state': stack.append(as_needed)
                    elif t=='--pop-state': as_needed=stack.pop()
                    else: as_needed=t=='--as-needed'
                    link.append(t)
                elif t in ('--build-id','--eh-frame-hdr','--hash-style=gnu','-pie'):
                    link.append(t)
                elif t.startswith('-l') or (t.startswith('/') and t.endswith('.o')):
                    n = replace(inventory.load(t, True), as_needed=as_needed)
                    nodes.append(n); link.append(n)
                else: raise base.Rejected('unsupported compiler template argument: ' + t)
            seen_objects=set()
            def tree(n):
                if n.kind=='object':
                    if n.path in seen_objects: raise base.Rejected('duplicate explicit/implicit object: '+str(n.path))
                    seen_objects.add(n.path)
                return {'token':n.token,'path':str(n.path) if n.path else None,'kind':n.kind,
                        'as_needed':n.as_needed,'trusted_runtime':n.runtime,'children':[tree(c) for c in n.children]}
            report['input_tree']=[tree(n) for n in nodes]
            report['search_paths']=list(map(str,search))
            report['inventory'] = inventory.receipts
            report['inputs'] = [{'path':n.token,'resolved_path':str(n.path),'argv_index':n.position,'archive':n.kind=='archive','members':[{'identity':m.identity,'defines':sorted(m.defines),'requires':sorted(m.requires)} for m in n.members]} for n in nodes]
            original = model(nodes); report['original']=original
            providers = {}
            def gather(n):
                for m in n.members:
                    for s in m.defines: providers.setdefault(s,[]).append(m.identity)
                for c in n.children: gather(c)
            for n in nodes: gather(n)
            report['missed']=[{'symbol':s,'consumer':c,'provider':p} for s in original['unresolved'] if s in providers for c in original['consumers'][s] for p in providers[s]]
            user_providers = {}
            def user_units(n):
                if n.path is not None and not n.runtime:
                    for m in n.members:
                        for s in m.defines:
                            user_providers.setdefault(s,set()).add(m.identity)
                for c in n.children: user_units(c)
            for n in nodes: user_units(n)
            ambiguous = {s:sorted(set(providers[s])) for s,p in user_providers.items() if len(set(providers[s]))>1}
            from . import repair
            report['ambiguity'] = ambiguous
            # Alternative *static* providers have deterministic extraction order.
            # User/runtime interposition remains outside the supported boundary.
            overlap = {s:p for s,p in ambiguous.items() if set(p)-user_providers[s]}
            if overlap:
                report.update(diagnosis='provider_ambiguity', reason='User/runtime provider overlap is outside static resolution support.')
                return report
            chosen, candidate, kind, attempts = repair.search(nodes, model, base.SEARCH_LIMIT,
                                                              lambda target: Node('--start-group',kind='group',children=target))
            report['search']={'attempts':attempts,'limit':base.SEARCH_LIMIT,'scope':'reorder, then up to four demand-directed repeats, then contiguous groups; user archive spans only; runtime fixed'}
            if chosen is None:
                report['diagnosis']='duplicate_definitions' if original['duplicates'] else 'missing_provider' if any(s not in providers for s in original['unresolved']) else 'constrained_search_exhausted'
                report['reason']='No supported model-valid repair; not proof of impossibility.'
                return report
            def tokens(ns):
                result=[]
                for n in ns:
                    if n.kind=='group': result.extend(['-Wl,--start-group',*tokens(n.children),'-Wl,--end-group'])
                    else: result.append(n.token)
                return result
            command=repair.command(argv,nodes,chosen,tokens)
            report.update(status='candidate' if original['unresolved'] or original['duplicates'] else 'already_satisfied',command=command,candidate=candidate,
                          repair=kind, provider_changes=repair.provider_changes(original,candidate),
                          changes=[{'argv_index':n.position,'from':n.token,'to':tokens(chosen[id(n)])} for n in nodes if id(n) in chosen and tokens(chosen[id(n)])!=[n.token]])
            if verify:
                report['verification']={'status':'running'}
                # DT_NEEDED files are independently validated and byte-snapshotted;
                # aliases provide the exact SONAME lookup in the isolated directory.
                for name,n in inventory.dependencies.items():
                    (scratch/name).write_bytes(n.snapshot.read_bytes())
                for label,mapping in (('original',{}),('candidate',chosen)):
                    isolated=['-rpath-link',str(scratch)]
                    for value in link:
                        if isinstance(value,Node):
                            for node in mapping.get(id(value),[value]): isolated.extend(render(node))
                        else: isolated.append(str(scratch/(label+'.out')) if value=='DOCTOR_OUTPUT' else value)
                    code,out,err=base.native('ld.bfd',isolated,scratch)
                    report['verification'][label]={'returncode':code,'stdout':out,'stderr':err,'argv':[base.TOOLS['ld.bfd'],*isolated]}
                ok=report['verification']['candidate']['returncode']==0 and ((report['verification']['original']['returncode']==0)==(not original['unresolved'] and not original['duplicates']))
                report['verified']=ok
                report['verification']['status']='passed' if ok else 'failed'
                if not ok:
                    # Withheld evidence, not instructions: edits of a candidate
                    # the native link rejected must not stay actionable.
                    withhold_candidate(report)
                    report.update(status='unknown',command=None,reason='Native verification disagreed; candidate withheld.')
            return report
    except MissingLibrary as exc:
        report.update(status='unknown', diagnosis='missing_provider', command=None, reason=str(exc))
        return report
    except (base.Rejected,base.ExecutionFailure,OSError,ValueError,IndexError) as exc:
        report.update(status='execution_failure' if isinstance(exc,(base.ExecutionFailure,OSError)) else 'unsupported',command=None,verified=False,reason=str(exc))
        if report['verification']['status']=='running': report['verification'].update(status='error',reason=str(exc))
        return report
    except Exception as exc:
        # Fail closed: an unexpected internal error is still a report with
        # the exception named, never a traceback and exit code 1.
        if report['verification']['status']=='running': report['verification'].update(status='error',reason=str(exc))
        report.update(status='execution_failure',command=None,verified=False,
                      reason='unexpected internal error: ' + type(exc).__name__ + ': ' + str(exc))
        return report
