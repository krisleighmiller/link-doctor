"""Fresh native driver acceptance; no produced program is executed."""
import unittest
from pathlib import Path
import tempfile
import subprocess
from link_doctor import diagnose as engine

class DriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='driver space ')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
    def run_native(self, *args):
        return subprocess.run(args, cwd=self.work, capture_output=True, text=True, check=True)
    def build(self, name, source, compiler='gcc'):
        path = self.work / (name + ('.cc' if compiler == 'g++' else '.c'))
        path.write_text(source)
        self.run_native(compiler, '-c', str(path), '-o', name + '.o')
    def lookup_receipt(self, name, **data):
        import os, json
        if os.environ.get('DOCTOR_LOOKUP_RECEIPTS'):
            dest = Path(os.environ['DOCTOR_LOOKUP_RECEIPTS'])
            dest.mkdir(parents=True, exist_ok=True)
            (dest/(name+'.json')).write_text(json.dumps(data, indent=2)+'\n')

    def native_receipt(self, args):
        result = subprocess.run(['/usr/bin/'+args[0], *args[1:]], cwd=self.work,
                                env={'PATH':'/usr/bin:/bin','LC_ALL':'C','LANG':'C'},
                                capture_output=True, text=True, timeout=30)
        return dict(argv=args, cwd=str(self.work), returncode=result.returncode,
                    stdout=result.stdout, stderr=result.stderr)

    def test_cwd_invalid_runtime_filename_withholds_false_repair(self):
        self.build('main', 'extern int middle(void); int main(){return middle();}')
        self.build('middle', 'extern int leaf(void); int middle(){return leaf();}')
        self.build('leaf', 'int leaf(){return 0;}')
        for name in ('middle','leaf'):
            self.run_native('ar','rcs','lib'+name+'.a',name+'.o')
        shadow = self.work/'libgcc_s.so.1'
        shadow.write_text('THIS IS NOT A LINK INPUT')
        (self.work/'OUT').write_text('preserve output')
        before = {p.name:p.read_bytes() for p in self.work.iterdir()}
        args = ['gcc','main.o','libleaf.a','libmiddle.a','-o','OUT']
        report = engine.diagnose(args, verify=True, cwd=self.work)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.work.iterdir()})
        # Independently run the exact proposed repair when the buggy code supplies one.
        repaired = list(report['command'] or ['gcc','main.o','libmiddle.a','libleaf.a','-o','OUT'])
        repaired[repaired.index('-o')+1] = 'native-repaired'
        native = self.native_receipt(repaired)
        self.lookup_receipt('invalid-cwd', report=report, native_repaired=native, preserved=True)
        self.assertNotEqual(native['returncode'], 0, native)
        self.assertIn('libgcc_s.so.1', native['stderr'])
        self.assertFalse(report['verified'], report)
        self.assertIsNone(report['command'])
        self.assertEqual(report['status'], 'unsupported', report)
        self.assertIn(str(shadow), report['reason'])

    def test_cwd_archive_runtime_filename_is_real_provider(self):
        import hashlib
        self.build('main', 'extern int doctor_cwd_provider(void); int main(){return doctor_cwd_provider();}')
        self.build('provider', 'int doctor_cwd_provider(){return 0;}')
        self.run_native('ar','rcs','libgcc_s.so.1','provider.o')
        (self.work/'OUT').write_text('preserve output')
        before = {p.name:p.read_bytes() for p in self.work.iterdir()}
        args = ['gcc','main.o','-o','OUT']  # deliberately no -L.
        report = engine.diagnose(args, verify=True, cwd=self.work)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.work.iterdir()})
        native = self.native_receipt(['gcc','main.o','-Wl,-t','-o','native-provider'])
        self.lookup_receipt('valid-cwd', report=report, native=native, preserved=True,
                            output_sha256=hashlib.sha256((self.work/'native-provider').read_bytes()).hexdigest() if native['returncode']==0 else None)
        self.assertEqual(native['returncode'], 0, native)
        self.assertIn('libgcc_s.so.1', native['stdout'])
        self.assertEqual(report['status'], 'already_satisfied', report)
        self.assertTrue(report['verified'], report)
        receipt = next(r for r in report['inventory'] if r['path']==str(self.work/'libgcc_s.so.1'))
        self.assertEqual(receipt['selection'], 'libgcc_s.so.1')
        self.assertEqual(receipt['kind'], 'archive')
        self.assertFalse(receipt['trusted_runtime'])
        self.assertEqual(receipt['sha256'], hashlib.sha256(before['libgcc_s.so.1']).hexdigest())
        self.assertTrue(any('libgcc_s.so.1(provider.o)' in s for s in report['original']['extracted']))

    def test_script_filename_and_library_search_native_contrast(self):
        from link_doctor.driver import Inventory
        self.build('provider', 'int doctor_lookup_provider(){return 0;}')
        search = self.work/'search'
        search.mkdir()
        (search/'nested').mkdir()
        self.run_native('ar','rcs','lookup.a','provider.o')
        (search/'lookup.a').write_bytes((self.work/'lookup.a').read_bytes())
        (search/'nested'/'lookup.a').write_bytes((self.work/'lookup.a').read_bytes())
        scratch = self.work/'snapshots'
        scratch.mkdir()
        evidence = []
        # Raw script names use cwd first, -l: names only search -L directories;
        # missing cwd names (including nested relative names) fall back to -L.
        for token, local in [('lookup.a', True), ('-l:lookup.a', True),
                             ('lookup.a', False), ('nested/lookup.a', False)]:
            if not local and (self.work/'lookup.a').exists():
                (self.work/'lookup.a').unlink()
            (search/'input.script').write_text('INPUT('+token+')')
            native = self.native_receipt(['ld.bfd','-r','-t','-L'+str(search),
                                          str(search/'input.script'),'-o','native-contrast'])
            inventory = Inventory(self.work, scratch, [search])
            node = inventory.script('INPUT('+token+')', False)[0].children[0]
            expected = self.work/token if local and not token.startswith('-l') else search/token.removeprefix('-l:')
            evidence.append(dict(token=token, native=native, selected=str(node.path)))
            self.assertEqual(native['returncode'], 0, native)
            self.assertIn(str(expected) if expected.parent!=self.work else token, native['stdout'])
            self.assertEqual(node.path, expected)
            self.assertEqual(node.token, token)
        self.lookup_receipt('search-contrast', cases=evidence)

    def test_cpp_iostream(self):
        self.build('main', '#include <iostream>\nint main(){std::cout << "hello";}', 'g++')
        report = engine.diagnose(['g++', 'main.o', '-o', 'OUT'], verify=True, cwd=self.work)
        self.assertEqual(report['status'], 'already_satisfied', report.get('reason', report))
        self.assertTrue(report['verified'], report.get('verification'))

    def test_search_response_forwarding_and_group(self):
        self.build('main', 'extern int a(void); int main(){return a();}')
        self.build('a', 'extern int b(void); int a(void){return b();}')
        self.build('b', 'int b(void){return 0;}')
        self.run_native('ar','rcs','liba.a','a.o')
        self.run_native('ar','rcs','libb.a','b.o')
        (self.work/'nested.rsp').write_text('"main.o" -L. -lb -la -o "OUT space"')
        (self.work/'args.rsp').write_text('@nested.rsp')
        report = engine.diagnose(['gcc','@args.rsp'], verify=True, cwd=self.work)
        self.assertEqual(report['status'], 'candidate', report.get('reason', report))
        self.assertTrue(report['verified'], report.get('verification'))
        grouped = engine.diagnose(['gcc','main.o','-L.','-Wl,--start-group,-lb','-Xlinker','-la','-Wl,--end-group'],verify=True,cwd=self.work)
        self.assertEqual(grouped['status'],'already_satisfied',grouped.get('reason', grouped))
        self.assertTrue(grouped['verified'],grouped.get('verification'))

    def test_nonordering_missing_duplicate_and_ambiguity(self):
        self.build('main', 'extern int absent(void); int main(){return absent();}')
        missing=engine.diagnose(['gcc','main.o'],cwd=self.work)
        self.assertEqual(missing.get('diagnosis'),'missing_provider',missing.get('reason'))
        self.build('a','int absent(){return 0;}')
        self.build('b','int absent(){return 1;}')
        duplicate=engine.diagnose(['gcc','main.o','a.o','b.o'],cwd=self.work)
        self.assertEqual(duplicate.get('diagnosis'),'duplicate_definitions',duplicate.get('reason'))
        self.run_native('ar','rcs','liba.a','a.o')
        self.run_native('ar','rcs','libb.a','b.o')
        ambiguous=engine.diagnose(['gcc','liba.a','main.o','libb.a'],verify=True,cwd=self.work)
        self.assertEqual(ambiguous['status'],'already_satisfied',ambiguous)
        self.assertTrue(ambiguous['verified'],ambiguous)
        self.assertIn('absent',ambiguous['ambiguity'])
        self.assertIn('libb.a',ambiguous['candidate']['selected_providers']['absent']['member'])

    def test_response_escapes_and_rejection_before_spawn(self):
        from link_doctor.driver import response_tokens
        from unittest.mock import patch
        self.assertEqual(response_tokens("a"+chr(92)+" b 'c"+chr(92)+" d' \"e"+chr(92)+" f\""), ['a b','c d','e f'])
        (self.work/'loop').write_text('@loop')
        with patch.object(engine,'native',side_effect=AssertionError('must not spawn')):
            for tail in [['main.c'],['-specs=bad','main.o'],['-Wl,-Map,VICTIM','main.o'],['-Wl,-plugin,x','main.o'],['@loop'],['-Wl,--pop-state','main.o'],['-Xlinker','@evil']]:
                report=engine.diagnose(['gcc',*tail],verify=True,cwd=self.work)
                self.assertIn(report['status'],('unsupported','execution_failure'),report)

    def test_weak_demand_does_not_extract(self):
        self.build('main','extern int optional(void) __attribute__((weak)); int main(){return optional ? optional():0;}')
        self.build('optional','extern int missing(void); int optional(){return missing();}')
        self.run_native('ar','rcs','liboptional.a','optional.o')
        report=engine.diagnose(['gcc','main.o','liboptional.a'],verify=True,cwd=self.work)
        self.assertEqual(report['status'],'already_satisfied',report.get('reason'))
        self.assertTrue(report['verified'],report.get('verification'))
        self.assertFalse(any('optional.o)' in s for s in report['original']['extracted']))

    def test_static_selection_and_nested_runtime_search(self):
        self.build('main','int main(){return 0;}')
        self.build('dummy','int doctor_runtime_dummy(){return 0;}')
        self.run_native('ar','rcs','libgcc.a','dummy.o')
        report=engine.diagnose(['gcc','main.o','-L.'],verify=True,cwd=self.work)
        self.assertTrue(report['verified'],report.get('reason',report.get('verification')))
        selected={r['path'] for r in report['inventory']}
        self.assertIn(str(self.work/'libgcc.a'),selected)
        self.assertFalse(any(p.endswith('/15/libgcc.a') for p in selected))
        native=self.run_native('gcc','main.o','-L.','-Wl,-t','-o','native')
        self.assertIn('./libgcc.a',native.stdout)
        (self.work/'libgcc.a').unlink()
        # A .so wins absent -Bstatic; never skip a selected unsupported file.
        (self.work/'libdummy.so').write_text('not a library')
        self.run_native('ar','rcs','libdummy.a','dummy.o')
        rejected=engine.diagnose(['gcc','main.o','-L.','-ldummy'],cwd=self.work)
        self.assertEqual(rejected['status'],'unsupported')
        static=engine.diagnose(['gcc','main.o','-L.','-Wl,--push-state,-Bstatic','-ldummy','-Wl,--pop-state'],verify=True,cwd=self.work)
        self.assertTrue(static['verified'],static.get('reason',static.get('verification')))

    def test_no_link_without_opt_in(self):
        from unittest.mock import patch
        self.build('main','int main(){return 0;}')
        real=engine.native
        def guard(tool,args,cwd):
            self.assertNotEqual(tool,'ld.bfd')
            if tool=='gcc': self.assertEqual(args,['-###','DOCTOR_INPUT.o','-o','DOCTOR_OUTPUT'])
            return real(tool,args,cwd)
        with patch.object(engine,'native',side_effect=guard):
            report=engine.diagnose(['gcc','main.o'],cwd=self.work)
        self.assertEqual(report['status'],'already_satisfied',report.get('reason'))
        self.assertFalse(report['verified'])

    def test_nested_runtime_child_does_not_inherit_trust(self):
        self.build('main','int main(){return 0;}')
        (self.work/'special.s').write_text('.section .text.extra,"axG",@progbits,extra,comdat\n.globl extra\nextra: ret\n')
        self.run_native('gcc','-c','special.s','-o','special.o')
        self.run_native('ar','rcs','libgcc.a','special.o')
        report=engine.diagnose(['gcc','main.o','-L.'],cwd=self.work)
        self.assertEqual(report['status'],'unsupported',report.get('reason'))
        self.assertIn('group',report['reason'].lower())

    def test_repeated_object_and_excluded_sections_fail_closed(self):
        self.build('main','int main(){return 0;}')
        repeated=engine.diagnose(['gcc','main.o','main.o'],cwd=self.work)
        self.assertIsNone(repeated['command'])
        crt=self.run_native('gcc','-print-file-name=crtbeginS.o').stdout.strip()
        implicit=engine.diagnose(['gcc','main.o',crt],cwd=self.work)
        self.assertIsNone(implicit['command'])
        (self.work/'exclude.s').write_text('.section .text.extra,"axe",@progbits\n.globl extra\nextra: ret\n')
        self.run_native('gcc','-c','exclude.s','-o','exclude.o')
        report=engine.diagnose(['gcc','main.o','exclude.o'],cwd=self.work)
        self.assertEqual(report['status'],'unsupported',report.get('reason'))

    def test_output_alias_and_response_limits(self):
        self.build('main','int main(){return 0;}')
        (self.work/'args').write_text('main.o -o main.o')
        before=(self.work/'main.o').read_bytes()
        report=engine.diagnose(['gcc','@args'],verify=True,cwd=self.work)
        # ld refuses an output that aliases an input for any order, so the
        # aliased command is rejected rather than reported as verified.
        self.assertEqual(report['status'],'unsupported',report.get('reason'))
        self.assertIn('same as input file',report.get('reason'))
        self.assertFalse(report['verified'],report)
        self.assertEqual(before,(self.work/'main.o').read_bytes())
        (self.work/'args').write_text('x '*400)
        report=engine.diagnose(['gcc','@args'],cwd=self.work)
        self.assertEqual(report['status'],'unsupported')
        (self.work/'args').write_text('"unterminated')
        report=engine.diagnose(['gcc','@args'],cwd=self.work)
        self.assertEqual(report['status'],'unsupported')

    def test_partial_dynamic_table_and_tool_failure(self):
        from unittest.mock import patch
        self.build('main','int main(){return 0;}')
        real=engine.native
        def partial(tool,args,cwd):
            code,out,err=real(tool,args,cwd)
            if tool=='readelf' and '-d' in args:
                out='\n'.join(line for line in out.splitlines() if '(NEEDED)' not in line)
            return code,out,err
        with patch.object(engine,'native',side_effect=partial):
            report=engine.diagnose(['gcc','main.o'],cwd=self.work)
        self.assertEqual(report['status'],'unsupported',report.get('reason'))
        self.assertIn('dynamic',report['reason'])
        with patch.object(engine,'native',side_effect=engine.ExecutionFailure('injected timeout')):
            report=engine.diagnose(['gcc','main.o'],cwd=self.work)
        self.assertEqual(report['status'],'execution_failure')

    def test_response_native_quote_contrast(self):
        self.build('main','int main(){return 0;}')
        (self.work/'main.o').rename(self.work/'my main.o')
        (self.work/'quoted.rsp').write_text('"my main.o" -o "OUT space"')
        self.run_native('gcc','@quoted.rsp')
        before=(self.work/'OUT space').read_bytes()
        report=engine.diagnose(['gcc','@quoted.rsp'],verify=True,cwd=self.work)
        self.assertTrue(report['verified'],report.get('reason',report.get('verification')))
        self.assertEqual(report['command'],['gcc','my main.o','-o','OUT space'])
        self.assertEqual(before,(self.work/'OUT space').read_bytes())

    def test_runtime_overlap_with_user_provider_is_ambiguous(self):
        self.build('main','int puts(const char *p){return 0;} int main(){return 0;}')
        report=engine.diagnose(['gcc','main.o'],cwd=self.work)
        self.assertEqual(report.get('diagnosis'),'provider_ambiguity',report.get('reason'))
        self.assertIsNone(report['command'])

    def test_cli_cwd(self):
        import os,sys,json
        self.build('main','int main(){return 0;}')
        env=dict(os.environ,PYTHONPATH=str(Path(engine.__file__).resolve().parent.parent),PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run([sys.executable,'-m','link_doctor','diagnose','--cwd',str(self.work),'--json','--','gcc','main.o'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'already_satisfied')

    def test_explicit_math_runtime(self):
        self.build('main','#include <math.h>\nvolatile double x; int main(){return cos(x)>0;}')
        report=engine.diagnose(['gcc','main.o','-lm'],verify=True,cwd=self.work)
        self.assertTrue(report['verified'],report.get('reason',report.get('verification')))
        self.assertEqual(report['status'],'already_satisfied')

    def test_missing_library_incompatible_and_verification_disagreement(self):
        from unittest.mock import patch
        self.build('main','int main(){return 0;}')
        missing=engine.diagnose(['gcc','main.o','-lNO_SUCH_DOCTOR_LIBRARY'],cwd=self.work)
        self.assertEqual(missing.get('diagnosis'),'missing_provider',missing.get('reason'))
        self.run_native('gcc','-m32','-c','main.c','-o','wrong.o')
        wrong=engine.diagnose(['gcc','wrong.o'],cwd=self.work)
        self.assertEqual(wrong['status'],'unsupported')
        self.assertIn('incompatible',wrong['reason'])
        self.run_native('gcc','-flto','-c','main.c','-o','lto.o')
        lto=engine.diagnose(['gcc','lto.o'],cwd=self.work)
        self.assertEqual(lto['status'],'unsupported')
        real=engine.native
        def fail_link(tool,args,cwd):
            return (1,'','injected linker failure') if tool=='ld.bfd' else real(tool,args,cwd)
        with patch.object(engine,'native',side_effect=fail_link):
            failed=engine.diagnose(['gcc','main.o'],verify=True,cwd=self.work)
        self.assertEqual(failed['status'],'unknown')
        self.assertIsNone(failed['command'])
        self.assertFalse(failed['verified'])

    def test_ordinary_c_runtime_reversed_archives(self):
        # Omitting libc, treating weak CRT demand as strong, or flattening archives fails.
        self.build('main', 'extern int middle(void); int main(void){return middle();}')
        self.build('middle', 'extern int leaf(void); int middle(void){return leaf();}')
        self.build('leaf', '#include <stdio.h>\nint leaf(void){return puts("hello");}')
        self.run_native('ar', 'rcs', 'libmiddle.a', 'middle.o')
        self.run_native('ar', 'rcs', 'libleaf.a', 'leaf.o')
        (self.work / 'OUT').write_bytes(b'sentinel')
        before = {p.name:p.read_bytes() for p in self.work.iterdir()}
        report = engine.diagnose(['gcc', 'main.o', 'libleaf.a', 'libmiddle.a', '-o', 'OUT'], verify=True, cwd=self.work)
        self.assertEqual(report['status'], 'candidate', report)
        self.assertTrue(report['verified'], report)
        self.assertEqual(report['original']['unresolved'], ['leaf'])
        self.assertEqual(report['command'][1:4], ['main.o', 'libmiddle.a', 'libleaf.a'])
        self.assertEqual(report['verification']['original']['returncode'], 1)
        self.assertEqual(report['verification']['candidate']['returncode'], 0)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.work.iterdir()})
