"""Run every backend test file in independent offline pytest processes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--shards',type=int,default=4)
parser.add_argument('--maxfail',type=int,default=12)
args=parser.parse_args()
files=list((ROOT/'backend/tests').glob('test_*.py'))
groups=[[] for _ in range(args.shards)]
loads=[0]*args.shards
for file in sorted(files,key=lambda p:p.stat().st_size,reverse=True):
    index=min(range(args.shards),key=loads.__getitem__)
    groups[index].append(file)
    loads[index]+=file.stat().st_size
folder=ROOT/'test-results/radar'
folder.mkdir(parents=True,exist_ok=True)
manifest={'all_test_files':len(files),'provider_access':'blocked by existing conftest socket/DNS fence','shards':[]}
for i,g in enumerate(groups):
    manifest['shards'].append({'index':i,'files':[str(f.relative_to(ROOT)) for f in sorted(g)]})
(folder/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
def run(i):
    started=time.monotonic()
    log=folder/f'backend-{i}.log'
    report=folder/f'backend-{i}.xml'
    cmd=[sys.executable,'-m','pytest',*manifest['shards'][i]['files'],'-q','--disable-warnings','--tb=short',
         '--junitxml='+str(report)]
    if args.maxfail:cmd.append('--maxfail='+str(args.maxfail))
    with log.open('w',encoding='utf-8') as stream:
        result=subprocess.run(cmd,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    row={'index':i,'exit_code':result.returncode,'elapsed_seconds':round(time.monotonic()-started,2)}
    if report.exists():
        suite=ET.parse(report).getroot()
        row['tests']=[{k:v for k,v in item.attrib.items() if k in ('tests','errors','failures','skipped','time')}
                      for item in suite.findall('testsuite')]
    print(json.dumps(row),flush=True)
    return row
with ThreadPoolExecutor(max_workers=args.shards) as pool:
    results=list(pool.map(run,range(args.shards)))
(folder/'summary.json').write_text(json.dumps({'shards':results,'all_test_files':len(files)},indent=2),encoding='utf-8')
sys.exit(0 if all(r['exit_code']==0 for r in results) else 1)
