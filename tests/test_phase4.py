"""Fresh native differential cases; no produced binary is executed."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from link_doctor import diagnose as engine

RECEIPTS = Path(__file__).resolve().parents[1] / '.build/phase4-acceptance'

class Phase4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='phase4-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.receipts = []
        (self.work/'OUT').write_text('sentinel')

    def tearDown(self):
        RECEIPTS.mkdir(parents=True, exist_ok=True)
        (RECEIPTS/(self.id().split('.')[-1]+'.json')).write_text(json.dumps(self.receipts, indent=2))

    def run_native(self, argv):
        p = subprocess.run(argv, cwd=self.work, env=engine.ENV, capture_output=True, text=True, timeout=20)
        self.receipts.append({'argv':argv,'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
        return p

    def obj(self, name, text, *flags):
        (self.work/(name+'.c')).write_text(text)
        p=self.run_native(['gcc','-c','-fcommon',*flags,name+'.c','-o',name+'.o'])
        self.assertEqual(p.returncode,0,p.stderr)

    def ar(self, name, *members):
        self.assertEqual(self.run_native(['ar','rcs',name+'.a',*[m+'.o' for m in members]]).returncode,0)

    def check(self, inputs, driver='ld.bfd', expected='candidate'):
        cmd=([driver,'-e','main'] if driver=='ld.bfd' else [driver])+['-o','OUT',*inputs]
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.work.iterdir()}
        r=engine.diagnose(cmd,verify=True,cwd=self.work)
        self.receipts.append({'report':r})
        self.assertEqual(r['status'],expected,r)
        self.assertTrue(r['verified'],r)
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.work.iterdir()}
        self.receipts.append({'preservation':{'before':before,'after':after,'unchanged':before==after}})
        self.assertEqual(before,after)
        original_native=cmd.copy(); original_native[original_native.index('-o')+1]='independent-original.out'
        original_result=self.run_native(original_native)
        predicted_ok=not r['original']['unresolved'] and not r['original']['duplicates']
        self.assertEqual(original_result.returncode==0,predicted_ok,(original_result.stderr,r))
        native=r['command'].copy(); native[native.index('-o')+1]='independent.out'
        native += ['-t','-t'] if driver=='ld.bfd' else ['-Wl,-t,-t']
        p=self.run_native(native)
        self.assertEqual(p.returncode,0,p.stderr)
        import re
        actual = {(Path(a).name,m) for a,m in re.findall(r'^\((.+)\)(.+)$',p.stdout,re.M) if (self.work/Path(a).name).exists()}
        predicted = {(Path(a).name,m) for ident in r['candidate']['extracted'] for a,m in re.findall(r'^(.*)\(([^()]+)\)$',ident) if (self.work/Path(a).name).exists()}
        self.assertEqual(actual,predicted,(p.stdout,r))
        return r

    def cycle(self):
        self.obj('main','extern int a(void); int main(void){return a();}')
        self.obj('a','extern int b(void); int a(void){return b();}')
        self.obj('b','extern int tail(void); int b(void){return tail();}')
        self.obj('tail','int tail(void){return 7;}')
        self.ar('A','tail','a'); self.ar('B','b')

    def test_cycle_targeted_repeat_both_frontends(self):
        self.cycle()
        for driver in ('ld.bfd','gcc','g++'):
            r=self.check(['main.o','A.a','B.a'],driver)
            self.assertEqual(r['repair']['kind'],'repeat')
            self.assertEqual(r['command'][-3:],['A.a','B.a','A.a'])
            self.assertEqual(r['candidate']['unresolved'],[])

    def test_cycle_existing_group_and_repeat(self):
        self.cycle()
        for driver in ('ld.bfd','gcc'):
            flags=['--start-group','A.a','B.a','--end-group']
            if driver=='gcc': flags=['-Wl,'+s if s.startswith('--') else s for s in flags]
            self.check(['main.o',*flags],driver,'already_satisfied')
            self.check(['main.o','A.a','B.a','A.a'],driver,'already_satisfied')

    def test_weak_common_strong_and_alternatives(self):
        self.obj('main','extern int value; int main(void){return value;}')
        self.obj('weak','__attribute__((weak)) int value=3;')
        self.obj('common','int value;')
        self.obj('strong','int value=9;')
        self.obj('other','int value=11;')
        self.ar('S','strong'); self.ar('T','other')
        for driver in ('ld.bfd','gcc'):
            for inputs in (['main.o','weak.o','strong.o'],['main.o','common.o','strong.o'],['main.o','common.o','S.a'],['main.o','S.a','T.a'],['main.o','weak.o','S.a'],['main.o','common.o','weak.o'],['main.o','weak.o','common.o']):
                r=self.check(inputs,driver,'already_satisfied')
                self.assertIn('value',r['candidate']['selected_providers'])
            r=self.check(['main.o','S.a','T.a'],driver,'already_satisfied')
            self.assertIn('value',r['ambiguity'])
            self.assertIn('strong.o',r['candidate']['selected_providers']['value']['member'])

    def test_selected_provider_matches_native_cross_reference(self):
        import re
        self.obj('main','extern int value; int main(void){return value;}')
        self.obj('weak','__attribute__((weak)) int value=3;')
        self.obj('common','int value;')
        self.obj('strong','int value=9;')
        self.obj('other','int value=11;')
        self.ar('S','strong'); self.ar('T','other')
        for driver in ('ld.bfd','gcc','g++'):
            for inputs in (['weak.o','strong.o'], ['strong.o','weak.o'],
                           ['common.o','strong.o'], ['weak.o','common.o'],
                           ['S.a','T.a'], ['T.a','S.a'], ['weak.o','S.a']):
                r=self.check(['main.o',*inputs],driver,'already_satisfied')
                cmd=r['command'].copy(); cmd[cmd.index('-o')+1]='cref.out'
                cmd.extend(['--cref','-Map=cref.map'] if driver=='ld.bfd' else ['-Wl,--cref,-Map=cref.map'])
                native=self.run_native(cmd)
                self.assertEqual(native.returncode,0,native.stderr)
                link_map=(self.work/'cref.map').read_text()
                self.receipts.append({'native_link_map':link_map})
                selected=r['candidate']['selected_providers']['value']
                # --cref can list an overridden weak definition first when a
                # common wins. The common-allocation table is authoritative.
                pattern=r'^value\s+0x[0-9a-f]+\s+(\S+)' if selected['strength']=='common' else r'^value\s+(\S+)'
                winner=re.search(pattern,link_map,re.M)
                self.assertIsNotNone(winner,link_map)
                # Driver inventory uses resolved absolute identities; the
                # standalone native command keeps the original relative argv.
                self.assertEqual(winner.group(1),selected['member'].removeprefix(str(self.work)+'/'))

    def test_weak_undefined_never_extracts(self):
        self.obj('main','extern int optional(void) __attribute__((weak)); int main(void){return optional ? optional() : 0;}')
        self.obj('optional','extern int absent(void); int optional(void){return absent();}')
        self.ar('O','optional')
        for driver in ('ld.bfd','gcc'):
            r=self.check(['main.o','O.a'],driver,'already_satisfied')
            self.assertFalse(any('optional.o' in m for m in r['original']['extracted']))

    def test_duplicate_strong_not_repairable(self):
        self.obj('main','int main(void){return 0;} int x=1;')
        self.obj('other','int x=2;')
        for driver in ('ld.bfd','gcc'):
            cmd=([driver,'-e','main'] if driver=='ld.bfd' else [driver])+['main.o','other.o','-o','OUT']
            r=engine.diagnose(cmd,verify=True,cwd=self.work)
            self.assertEqual(r['status'],'unknown',r)
            self.assertEqual(r['diagnosis'],'duplicate_definitions')
            self.assertIsNone(r['command'])
            cmd[-1]='native.out'
            self.assertNotEqual(self.run_native(cmd).returncode,0)

    def test_conflicting_alternative_order_can_be_repaired(self):
        self.obj('main','extern int a(void),value(void); int main(void){return a()+value();}')
        self.obj('a','extern int tail(void); int a(void){return tail();}')
        self.obj('tail','int tail(void){return 2;} int value(void){return 3;}')
        self.obj('alt','int value(void){return 8;}')
        self.ar('A','a'); self.ar('T','tail'); self.ar('V','alt')
        for driver in ('ld.bfd','gcc'):
            r=self.check(['main.o','A.a','V.a','T.a'],driver)
            self.assertEqual(r['original']['unresolved'],[])
            self.assertEqual(r['original']['duplicates'],['value'])
            self.assertTrue(r['provider_changes'])

    def test_provider_change_is_explicit(self):
        self.obj('main','extern int a(void); extern int value(void); int main(void){return a()+value();}')
        self.obj('a','extern int tail(void); int a(void){return tail();}')
        self.obj('tail','int tail(void){return 2;} int value(void){return 3;}')
        self.obj('alt','int value(void){return 8;}')
        self.ar('A','a'); self.ar('T','tail'); self.ar('V','alt')
        for driver in ('ld.bfd','gcc'):
            r=self.check(['main.o','V.a','T.a','A.a'],driver)
            self.assertTrue(r['provider_changes'],r)
            self.assertIn('value',r['ambiguity'])

    def test_deterministic_and_budget_unknown(self):
        self.cycle()
        cmd=['gcc','main.o','A.a','B.a','-o','OUT']
        actual=engine.native
        def no_link(tool,args,cwd):
            self.assertNotEqual(tool,'ld.bfd','diagnosis must not link')
            return actual(tool,args,cwd)
        with patch.object(engine,'native',side_effect=no_link):
            a=engine.diagnose(cmd,cwd=self.work); b=engine.diagnose(cmd,cwd=self.work)
        self.assertEqual(a['status'],'candidate',a)
        self.assertEqual(a['command'],b['command'])
        with patch.object(engine,'SEARCH_LIMIT',1):
            r=engine.diagnose(cmd,cwd=self.work)
        self.assertEqual(r['status'],'unknown',r)
        self.assertIsNone(r['command'])
        self.assertEqual(r['search']['attempts'],1)

    def test_reorder_preferred_and_barriers_fixed(self):
        self.obj('main','extern int a(void); int main(void){return a();}')
        self.obj('a','extern int b(void); int a(void){return b();}')
        self.obj('b','int b(void){return 1;}')
        self.ar('A','a'); self.ar('B','b')
        r=self.check(['main.o','B.a','A.a'],'gcc')
        self.assertEqual(r['repair']['kind'],'reorder')
        cmd=['gcc','main.o','B.a','-Wl,--as-needed','A.a','-o','OUT']
        r=engine.diagnose(cmd,cwd=self.work)
        self.assertEqual(r['status'],'unknown',r)

    def test_targeted_group_after_repeat_bound(self):
        self.obj('main','extern int f0(void); int main(void){return f0();}')
        for i in range(9):
            self.obj('f'+str(i), ('extern int f%d(void); int f%d(void){return f%d();}'%(i+1,i,i+1)) if i<8 else 'int f8(void){return 1;}')
        self.ar('A',*['f'+str(i) for i in reversed(range(0,9,2))])
        self.ar('B',*['f'+str(i) for i in reversed(range(1,9,2))])
        self.obj('unused','extern int absent(void); int unused(void){return absent();}')
        self.ar('C','unused')
        # Per-strategy budget shares (reorder: limit - limit//4 attempts,
        # repeats: limit - limit//8) move the old 10/11 margin: the group
        # fix still needs 6 reorder + 1 repeat + 1 group attempt, so
        # limit 7 starves it and limit 8 reaches it.
        for limit,status in ((7,'unknown'),(8,'candidate')):
            with patch.object(engine,'SEARCH_LIMIT',limit):
                bounded=engine.diagnose(['ld.bfd','-e','main','-o','OUT','main.o','A.a','B.a','C.a'],cwd=self.work)
            self.receipts.append({'group_budget':limit,'report':bounded})
            self.assertEqual(bounded['status'],status,bounded)
            self.assertEqual(bounded['search']['attempts'],limit)
            self.assertFalse(bounded['verified'])
            if status=='unknown': self.assertIsNone(bounded['command'])
        for driver in ('ld.bfd','gcc','g++'):
            r=self.check(['main.o','A.a','B.a','C.a'],driver)
            self.assertEqual(r['repair']['kind'],'group',r)
            self.assertEqual(r['repair']['archives'],['A.a','B.a'])
            self.assertEqual(r['command'][-1],'C.a')

    def test_common_archive_extraction_and_coalescing(self):
        self.obj('main','int value; int main(void){return value;}')
        self.obj('large','int value[10];')
        self.obj('strong','int value=3;')
        self.obj('func','int value(void){return 2;}')
        self.obj('weak','__attribute__((weak)) int value=4;')
        for name in ('large','strong','func','weak'): self.ar(name,name)
        for name in ('large','strong','func','weak'):
            r=self.check(['main.o',name+'.a'],'ld.bfd','already_satisfied')
            self.assertEqual(any('('+name+'.o)' in ident for ident in r['candidate']['extracted']),name=='strong')
        r=self.check(['main.o','large.o'],'ld.bfd','already_satisfied')
        self.assertEqual(r['candidate']['selected_providers']['value']['coalesced_members'],['large.o','main.o'])
        symbols=self.run_native(['readelf','-Ws','independent.out']).stdout
        self.assertRegex(symbols,r'\s40\s+OBJECT\s+GLOBAL\s+DEFAULT\s+\d+\s+value')

    def test_alternative_provider_order_inside_archive(self):
        self.obj('main','extern int value(void); int main(void){return value();}')
        self.obj('weak','__attribute__((weak)) int value(void){return 2;}')
        self.obj('strong','int value(void){return 3;}')
        self.ar('W','weak','strong'); self.ar('S','strong','weak')
        for name,winner in [('W','weak.o'),('S','strong.o')]:
            r=self.check(['main.o',name+'.a'],'ld.bfd','already_satisfied')
            self.assertIn(winner,r['candidate']['selected_providers']['value']['member'])

    def test_noncanonical_provider_index_is_refused(self):
        self.obj('main','extern int value(void); int main(void){return value();}')
        self.obj('weak','__attribute__((weak)) int value(void){return 2;}')
        self.obj('strong','int value(void){return 3;}')
        self.ar('A','weak','strong')
        data=bytearray((self.work/'A.a').read_bytes())
        self.assertEqual(int.from_bytes(data[68:72],'big'),2)
        data[72:80]=data[76:80]+data[72:76]
        (self.work/'A.a').write_bytes(data)
        native=self.run_native(['ld.bfd','-e','main','main.o','A.a','-t','-t','-o','native.out'])
        self.assertEqual(native.returncode,0,native.stderr)
        self.assertIn('(A.a)strong.o',native.stdout)
        for driver in ('ld.bfd','gcc'):
            argv=([driver,'-e','main'] if driver=='ld.bfd' else [driver])+['main.o','A.a','-o','OUT']
            r=engine.diagnose(argv,verify=True,cwd=self.work)
            self.assertEqual(r['status'],'unsupported',r)
            self.assertIn('index order',r['reason'])

    def test_seeded_native_extraction_matrix(self):
        import random, re
        rng=random.Random(4021)
        self.obj('main','extern int s0,s1; int main(void){return s0+s1;}')
        for case in range(24):
            for i in range(6):
                symbol=rng.randrange(4); demand=rng.randrange(5)
                strength=rng.choice(('strong','weak','common'))
                declaration=('__attribute__((weak)) ' if strength=='weak' else '')+'int s%d'%symbol+(';' if strength=='common' else '=%d;'%(i+1))
                self.obj('m'+str(i), declaration+' extern int s%d; int use%d(void){return s%d;}'%(demand,i,demand))
            for name,members in [('A',('m0','m1')),('B',('m2','m3')),('C',('m4','m5'))]: self.ar(name,*members)
            libs=['A.a','B.a','C.a']; rng.shuffle(libs)
            cmd=['ld.bfd','-e','main','-o','OUT','main.o',*libs]
            report=engine.diagnose(cmd,cwd=self.work)
            self.assertIn('original',report,report)
            native=self.run_native([*cmd[:-4],'main.o',*libs,'-t','-t'])
            predicted=report['original']
            self.assertEqual(native.returncode==0,not predicted['unresolved'] and not predicted['duplicates'],(case,report,native.stderr))
            actual={(Path(a).name,m) for a,m in re.findall(r'^\((.+)\)(.+)$',native.stdout,re.M)}
            selected={(Path(a).name,m) for ident in predicted['extracted'] for a,m in re.findall(r'^(.*)\(([^()]+)\)$',ident)}
            self.assertEqual(actual,selected,(case,report,native.stdout))
            self.receipts.append({'case':case,'original':predicted})

    def test_cycle_verification_failure_and_timeout_withhold(self):
        self.cycle()
        actual=engine.native
        for driver in ('ld.bfd','gcc'):
            cmd=([driver,'-e','main'] if driver=='ld.bfd' else [driver])+['main.o','A.a','B.a','-o','OUT']
            def failure(tool,args,cwd):
                return (1,'','injected link failure') if tool=='ld.bfd' else actual(tool,args,cwd)
            with patch.object(engine,'native',side_effect=failure): r=engine.diagnose(cmd,verify=True,cwd=self.work)
            self.assertEqual(r['status'],'unknown',r)
            self.assertIsNone(r['command']); self.assertFalse(r['verified'])
            def timeout(tool,args,cwd):
                if tool=='ld.bfd': raise engine.ExecutionFailure('injected timeout')
                return actual(tool,args,cwd)
            with patch.object(engine,'native',side_effect=timeout): r=engine.diagnose(cmd,verify=True,cwd=self.work)
            self.assertEqual(r['status'],'execution_failure',r)
            self.assertIsNone(r['command']); self.assertFalse(r['verified'])

    def test_human_provider_warning(self):
        import sys
        self.obj('main','extern int value(void); int main(void){return value();}')
        self.obj('a','int value(void){return 1;}'); self.obj('b','int value(void){return 2;}')
        self.ar('A','a'); self.ar('B','b')
        env={**engine.ENV,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src'),'PYTHONDONTWRITEBYTECODE':'1'}
        p=subprocess.run([sys.executable,'-m','link_doctor','diagnose','--cwd',str(self.work),'--','ld.bfd','-e','main','-o','OUT','main.o','A.a','B.a'],env=env,text=True,capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('Alternative providers for value',p.stdout)
        self.assertIn('not program equivalence',p.stdout)

if __name__=='__main__': unittest.main()
