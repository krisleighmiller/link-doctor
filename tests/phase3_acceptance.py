"""Repeatable Phase 3 acceptance receipts. Never execute linked programs.

Run from product root: PYTHONPATH=src python3 tests/phase3_acceptance.py
Fresh fixtures and native outputs are disposable; JSON/human receipts persist.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / '.build' / 'phase3-acceptance'
DEST.mkdir(parents=True, exist_ok=True)
ENV = dict(os.environ, PYTHONPATH=str(ROOT/'src'), PYTHONDONTWRITEBYTECODE='1')


def run(argv, cwd, check=False):
    p = subprocess.run(argv, cwd=cwd, env=ENV, capture_output=True, text=True, timeout=30)
    receipt = dict(argv=list(map(str,argv)), cwd=str(cwd), returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)
    if check and p.returncode:
        raise RuntimeError(receipt)
    return receipt


def hashes(work):
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir() if p.is_file()}


results = []
for compiler, extension, leaf in (
    ('gcc', '.c', '#include <stdio.h>\nint leaf(void){return puts("hello");}'),
    ('g++', '.cc', '#include <iostream>\nint leaf(){std::cout << "hello"; return 0;}'),
):
    with tempfile.TemporaryDirectory(prefix='phase3 '+compiler+' space ') as directory:
        work = Path(directory)
        sources = {'main':'extern int middle(); int main(){return middle();}',
                   'middle':'extern int leaf(); int middle(){return leaf();}', 'leaf':leaf}
        build = []
        for name, source in sources.items():
            (work/(name+extension)).write_text(source)
            build.append(run([compiler,'-c',name+extension,'-o',name+'.o'],work,True))
        for name in ('middle','leaf'):
            build.append(run(['ar','rcs','lib'+name+'.a',name+'.o'],work,True))
        (work/'OUT space').write_bytes(b'preserve existing output\n')
        (work/'link.rsp').write_text('main.o -L. -lleaf -lmiddle -o "OUT space"')
        command=[compiler,'@link.rsp']
        before=hashes(work)
        cli=[sys.executable,'-m','link_doctor','diagnose','--verify','--json','--',*command]
        receipt=run(cli,work,True)
        report=json.loads(receipt['stdout'])
        assert report['status']=='candidate' and report['verified'], report.get('reason')
        assert before==hashes(work), 'diagnosis changed inputs or output'
        native_original=run([compiler,'main.o','-L.','-lleaf','-lmiddle','-o','native-original'],work)
        native_repaired=run([compiler,'main.o','-L.','-lmiddle','-lleaf','-o','native-repaired'],work)
        assert native_original['returncode']==1, native_original
        assert native_repaired['returncode']==0, native_repaired
        human=run([sys.executable,'-m','link_doctor','diagnose','--verify','--',*command],work,True)
        item={'compiler':compiler,'build':build,'report':report,'cli':{k:v for k,v in receipt.items() if k!='stdout'},
              'native_original':native_original,'native_repaired':native_repaired,
              'preservation_before':before,'preservation_after':{k:hashes(work)[k] for k in before},
              'native_output_sha256':hashlib.sha256((work/'native-repaired').read_bytes()).hexdigest()}
        assert item['preservation_before']==item['preservation_after']
        tag='c' if compiler=='gcc' else 'cpp'
        (DEST/(tag+'.json')).write_text(json.dumps(item,indent=2)+'\n')
        (DEST/(tag+'-human.txt')).write_text(human['stdout'])
        results.append({'compiler':compiler,'status':report['status'],'verified':report['verified'],
                        'native_original_exit':native_original['returncode'],'native_repaired_exit':native_repaired['returncode'],
                        'inventory_files':len(report['inventory']),'preserved':True})
versions={tool:run([tool,'--version'],ROOT,True)['stdout'].splitlines()[0] for tool in ('gcc','g++','ld.bfd','readelf')}
summary={'versions':versions,'results':results,'binaries_executed':False,'fixture_outputs_disposed':True}
(DEST/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
paths=[ROOT/'README.md', *sorted((ROOT/'src').rglob('*.py')), *sorted((ROOT/'tests').rglob('*.py'))]
manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
(DEST/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(summary,indent=2))
