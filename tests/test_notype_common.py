"""Native regression for common overridden by strong NOTYPE archive data.

A plausible wrong model leaves x common because the strong definition lacks an
OBJECT annotation. Native extraction, final ELF section and bytes are the oracle;
link success alone cannot distinguish that model from the correct one.
No generated binary is executed.
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
RECEIPTS = ROOT / '.build' / 'notype-common-regressions'


class NotypeCommonNativeTests(unittest.TestCase):
    def exercise(self, driver):
        receipts = {'driver': driver, 'commands': []}
        RECEIPTS.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix='notype-common-') as directory:
                work = Path(directory)
                env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'LANG': 'C',
                       'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
                # Preserve the host's scratch location for subprocess temporaries.
                if 'TMPDIR' in os.environ:
                    env['TMPDIR'] = os.environ['TMPDIR']

                def run(argv):
                    p = subprocess.run(argv, cwd=work, env=env, capture_output=True,
                                       text=True, timeout=30)
                    receipts['commands'].append({'argv': argv, 'returncode': p.returncode,
                                                  'stdout': p.stdout, 'stderr': p.stderr})
                    self.assertEqual(p.returncode, 0, p.stderr)
                    return p.stdout

                (work / 'main.c').write_text('int x; int main(void) { return x; }\n')
                (work / 'n.s').write_text(
                    '.data\n.balign 4\n.globl x\nx: .long 77\n'
                    '.section .note.GNU-stack,"",@progbits\n')
                (work / 'OUT').write_bytes(b'preserve requested output\x00')
                run(['/usr/bin/gcc', '-fcommon', '-c', 'main.c', '-o', 'main.o'])
                run(['/usr/bin/as', 'n.s', '-o', 'n.o'])
                run(['/usr/bin/ar', 'rcs', 'N.a', 'n.o'])
                symbols = run(['/usr/bin/readelf', '-Ws', 'n.o'])
                self.assertRegex(symbols, r'NOTYPE\s+GLOBAL\s+DEFAULT\s+\d+\s+x\s*(?:\n|$)')
                command = ([driver, '-e', 'main'] if driver == 'ld.bfd' else [driver])
                command += ['-o', 'OUT', 'main.o', 'N.a']
                native = command.copy()
                native[0] = '/usr/bin/' + driver
                native[native.index('-o') + 1] = 'native.out'
                native += ['-t', '-t'] if driver == 'ld.bfd' else ['-Wl,-t,-t']
                trace = run(native)
                self.assertIn('(N.a)n.o', trace, trace)
                final_symbols = run(['/usr/bin/readelf', '-Ws', 'native.out'])
                final_sections = run(['/usr/bin/readelf', '-WS', 'native.out'])
                symbol = re.search(r'^\s*\d+:\s+([0-9a-fA-F]+)\s+\d+\s+\S+\s+GLOBAL\s+DEFAULT\s+(\d+)\s+x\s*$',
                                   final_symbols, re.M)
                self.assertIsNotNone(symbol, final_symbols)
                section = re.search(r'^\s*\[\s*' + symbol[2] + r'\]\s+\.data\s+PROGBITS\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)',
                                    final_sections, re.M)
                self.assertIsNotNone(section, final_sections)
                relative = int(symbol[1], 16) - int(section[1], 16)
                self.assertGreaterEqual(relative, 0)
                self.assertLessEqual(relative + 4, int(section[3], 16))
                offset = int(section[2], 16) + relative
                image = (work / 'native.out').read_bytes()
                self.assertEqual(image[:6], b'\x7fELF\x02\x01')
                value = int.from_bytes(image[offset:offset + 4], 'little')
                self.assertEqual(value, 77)
                receipts['native_oracle'] = {'extracted': 'N.a(n.o)', 'section': '.data',
                                             'symbol': 'x', 'value': value}

                def hashes():
                    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in work.iterdir() if p.is_file()}

                before = hashes()
                report = json.loads(run([sys.executable, '-m', 'link_doctor', 'diagnose',
                                         '--verify', '--json', '--', *command]))
                receipts['report'] = report
                receipts['preservation'] = {'before': before, 'after': hashes()}
                self.assertEqual(before, hashes())
                self.assertEqual(report['status'], 'already_satisfied', report)
                self.assertTrue(report['verified'], report)
                # Separate subtests expose both wrong extraction and provider
                # evidence instead of stopping after the first mismatch.
                def member_label(identity):
                    # Driver inventory uses absolute paths; direct ld preserves
                    # explicit spelling. Compare the same fixture member.
                    archive, separator, member = identity.rpartition('(')
                    return Path(archive).name + separator + member if separator else identity

                for stage in ('original', 'candidate'):
                    with self.subTest(driver=driver, stage=stage, evidence='extraction'):
                        self.assertIn('N.a(n.o)', [member_label(item) for item in report[stage]['extracted']])
                    with self.subTest(driver=driver, stage=stage, evidence='provider'):
                        provider = report[stage]['selected_providers']['x']
                        self.assertEqual(member_label(provider['member']), 'N.a(n.o)')
                        self.assertEqual(provider['strength'], 'strong')
        finally:
            (RECEIPTS / (driver + '.json')).write_text(json.dumps(receipts, indent=2) + '\n')

    def test_ld_extracts_notype_data_over_common(self):
        self.exercise('ld.bfd')

    def test_gcc_extracts_notype_data_over_common(self):
        self.exercise('gcc')

    def test_gxx_extracts_notype_data_over_common(self):
        self.exercise('g++')


if __name__ == '__main__':
    unittest.main()
