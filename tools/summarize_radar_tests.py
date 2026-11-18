"""Reconcile current collected cases with dated JUnit evidence, not summed reruns."""
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
folder=ROOT/'test-results/radar'
collected=subprocess.run([sys.executable,'-m','pytest','backend/tests','--collect-only','-q','--disable-warnings'],
    cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace')
if collected.returncode:
    raise RuntimeError('Current backend collection failed: '+collected.stdout[-2000:])
current={line.strip() for line in collected.stdout.splitlines() if line.startswith('backend/tests/') and '::' in line}
if not current:
    raise RuntimeError('No backend test IDs collected')
latest={}
reports=list(folder.glob('backend-*.xml'))+list(folder.glob('application-final.xml'))
for report in sorted(reports,key=lambda p:p.stat().st_mtime_ns):
    for case in ET.parse(report).getroot().iter('testcase'):
        key=case.attrib['classname'].replace('.','/')+'.py::'+case.attrib['name']
        status=('failed' if case.find('failure') is not None else 'error' if case.find('error') is not None
                else 'skipped' if case.find('skipped') is not None else 'passed')
        latest[key]={'status':status,'report':report.name}
unverified=sorted(key for key in current if latest.get(key,{}).get('status')!='passed')
summary={'collected_current_cases':len(current),'passed_current_cases':len(current)-len(unverified),
    'unverified':[{'id':key,**latest.get(key,{'status':'missing'})} for key in unverified],
    'junit_reports':[p.name for p in reports],
    'note':'Unique current node IDs; historical failures remain recorded and are superseded only by passing reruns.'}
(folder/'final-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False))
sys.exit(1 if unverified else 0)
