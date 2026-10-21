"""Conservative container TX accounting, including private traffic.

One GB per UTC month is a hosting-spend guard, not an AUTO.RIA API quota.
Retains history over restarts; reserves 32 MiB per supervisor start for the
sampling/shutdown gap. The container must expose its own /proc/net/dev counters.
"""
from datetime import datetime,timezone
from pathlib import Path
import sqlite3

LIMIT=1_000_000_000
RESERVE=32*1024*1024

def transmitted(path='/proc/net/dev'):
    total=0;found=False
    for line in Path(path).read_text().splitlines():
        if ':' not in line:continue
        name,stats=line.split(':',1)
        if name.strip()=='lo':continue
        values=stats.split()
        if len(values)!=16:raise ValueError('network_counters_invalid')
        total+=int(values[8]);found=True
    if not found:raise ValueError('network_counters_missing')
    return total

class Egress:
    def __init__(self,path,*,clock,counter=transmitted):
        self.clock,self.counter=clock,counter
        self.db=sqlite3.connect(path,isolation_level=None)
        self.db.execute('CREATE TABLE IF NOT EXISTS months(month TEXT PRIMARY KEY, used INTEGER NOT NULL)')
        try:
            self.last=self.counter();self.month=None
            self.check()
        except BaseException:
            self.db.close()
            raise

    def check(self):
        month=datetime.fromtimestamp(self.clock(),timezone.utc).strftime('%Y-%m')
        current=self.counter()
        delta=max(0,current-self.last)
        # A counter reset within a live process is not a reason to forgive bytes.
        if current<self.last:delta=current+RESERVE
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT OR IGNORE INTO months VALUES(?,0)',(month,))
            self.db.execute('UPDATE months SET used=used+? WHERE month=?',
                            (delta+(RESERVE if month!=self.month else 0),month))
            used=self.db.execute('SELECT used FROM months WHERE month=?',(month,)).fetchone()[0]
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        self.last,self.month=current,month
        if used>=LIMIT-RESERVE:raise ValueError('hosting_egress_guard')
        return used

    def close(self):self.db.close()
