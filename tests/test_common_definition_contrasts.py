"""Differential common-demand tests; never execute the linked images.

GNU ELF's 'data symbol' test is not a section flag test: strong NOTYPE
text also pulls, whereas FUNC in writable data does not. ABS remains outside
Doctor's public grammar, even though the native linker extracts it.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECEIPTS = ROOT / '.build' / 'notype-common-fix' / 'contrasts'
CASES = {
    'text': ('.text', '', True, '.text'),
    'rodata': ('.section .rodata,"a",@progbits', '', True, '.rodata'),
    'bss': ('.bss', '', True, '.bss'),
    'nonalloc': ('.section .custom,"",@progbits', '', True, '.custom'),
    'executable': ('.section .custom,"ax",@progbits', '', True, '.custom'),
    'function_data': ('.data', '.type x,@function', False, '.bss'),
    'object_text': ('.text', '.type x,@object', True, '.text'),
    'weak_data': ('.data', '.weak x', False, '.bss'),
    'common': (None, '.comm x,4,4', False, '.bss'),
    'absolute': (None, '.globl x\n.set x,77', True, 'ABS'),
}


class CommonDefinitionContrasts(unittest.TestCase):
    def exercise(self, driver):
        RECEIPTS.mkdir(parents=True, exist_ok=True)
        for name, (section, annotation, extracts, final_section) in CASES.items():
            with self.subTest(driver=driver, case=name), tempfile.TemporaryDirectory(prefix='common-contrast-') as directory:
                work = Path(directory)
                receipt = {'driver': driver, 'case': name, 'commands': []}
                env = dict(os.environ, LC_ALL='C', LANG='C', PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1')

                def run(argv, expected=0):
                    p = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True, timeout=30)
                    receipt['commands'].append({'argv': argv, 'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
                    self.assertEqual(p.returncode, expected, p.stderr)
                    return p.stdout

                try:
                    (work / 'main.c').write_text('int x; int main(void){return x;}\n')
                    source = annotation if section is None else section + '\n.balign 4\n.globl x\n' + annotation + '\nx: ' + ('.zero 4' if name == 'bss' else '.long 77')
                    (work / 'n.s').write_text(source + '\n.section .note.GNU-stack,"",@progbits\n')
                    run(['/usr/bin/gcc', '-fcommon', '-fno-pie', '-c', 'main.c', '-o', 'main.o'])
                    run(['/usr/bin/as', 'n.s', '-o', 'n.o'])
                    run(['/usr/bin/ar', 'rcs', 'N.a', 'n.o'])
                    command = [driver] + (['-e', 'main'] if driver == 'ld.bfd' else []) + ['-o', 'OUT', 'main.o', 'N.a']
                    native = ['/usr/bin/' + driver, *command[1:]]
                    native[native.index('OUT')] = 'native.out'
                    native += ['-t', '-t'] if driver == 'ld.bfd' else ['-Wl,-t,-t']
                    # Absolute PC-relative references require a non-PIE native
                    # contrast; ABS inputs are rejected by Doctor regardless.
                    if name == 'absolute' and driver != 'ld.bfd':
                        native += ['-no-pie']
                    trace = run(native)
                    self.assertEqual('(N.a)n.o' in trace, extracts)
                    symbols = run(['/usr/bin/readelf', '-Ws', 'native.out'])
                    sections = run(['/usr/bin/readelf', '-WS', 'native.out'])
                    symbol = re.search(r'^\s*\d+:\s+([0-9a-fA-F]+)\s+\d+\s+\S+\s+GLOBAL\s+DEFAULT\s+(\S+)\s+x\s*$', symbols, re.M)
                    self.assertIsNotNone(symbol)
                    if final_section == 'ABS':
                        self.assertEqual(symbol[2], 'ABS')
                        self.assertEqual(int(symbol[1], 16), 77)
                        value = 77
                    else:
                        row = re.search(r'^\s*\[\s*' + symbol[2] + r'\]\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)', sections, re.M)
                        self.assertIsNotNone(row)
                        self.assertEqual(row[1], final_section)
                        relative = int(symbol[1], 16) - int(row[3], 16)
                        self.assertGreaterEqual(relative, 0)
                        self.assertLessEqual(relative + 4, int(row[5], 16))
                        if row[2] == 'NOBITS':
                            value = 0
                        else:
                            offset = int(row[4], 16) + relative
                            value = int.from_bytes((work / 'native.out').read_bytes()[offset:offset + 4], 'little')
                        self.assertEqual(value, 77 if extracts and name != 'bss' else 0)
                    receipt['native_oracle'] = {'extracted': extracts, 'section': final_section, 'value': value}
                    (work / 'OUT').write_bytes(b'output sentinel')
                    def hashes():
                        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir() if p.is_file()}
                    before = hashes()
                    # Diagnosis alone must remain unverified; opt-in verifies only
                    # snapshot link success, not behavioral equivalence.
                    for verify in (False, True):
                        args = [sys.executable, '-m', 'link_doctor', 'diagnose', '--json'] + (['--verify'] if verify else []) + ['--', *command]
                        report = json.loads(run(args, expected=2 if name == 'absolute' else 0))
                        receipt['verified' if verify else 'diagnosis'] = report
                        if name == 'absolute':
                            self.assertEqual(report['status'], 'unsupported')
                            self.assertFalse(report['verified'])
                            continue
                        self.assertEqual(report['status'], 'already_satisfied')
                        self.assertEqual(report['verified'], verify)
                        for stage in ('original', 'candidate'):
                            result = report[stage]
                            self.assertEqual(any(i.endswith('N.a(n.o)') for i in result['extracted']), extracts)
                            provider = result['selected_providers']['x']
                            self.assertTrue(provider['member'].endswith('N.a(n.o)' if extracts else 'main.o'))
                            self.assertEqual(provider['strength'], 'strong' if extracts else 'common')
                    self.assertEqual(before, hashes())
                    receipt['preservation'] = {'before': before, 'after': hashes()}
                finally:
                    (RECEIPTS / (driver + '-' + name + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')

    def test_ld_contrasts(self):
        self.exercise('ld.bfd')

    def test_gcc_contrasts(self):
        self.exercise('gcc')

    def test_gxx_contrasts(self):
        self.exercise('g++')
