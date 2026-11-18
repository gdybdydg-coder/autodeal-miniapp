"""Finish a stopped offline shard, preserving already-passed case evidence."""
import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pytest

parser=argparse.ArgumentParser()
parser.add_argument('--shard',required=True,type=int)
args=parser.parse_args()
folder=ROOT/'test-results/radar'
manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
report=folder/f'backend-{args.shard}.xml'
passed=[]
for case in ET.parse(report).getroot().iter('testcase'):
    if not list(case):
        module=case.attrib['classname'].replace('.','/')+'.py'
        passed.append(module+'::'+case.attrib['name'])
files=next(row['files'] for row in manifest['shards'] if row['index']==args.shard)
print(json.dumps({'shard':args.shard,'previously_passed':len(passed),'new_and_failed_cases':'run'}),flush=True)
sys.exit(pytest.main([*files,'-q','--disable-warnings','--tb=short',
    '--junitxml='+str(folder/f'backend-{args.shard}-resume.xml'),
    *('--deselect='+nodeid for nodeid in passed)]))
