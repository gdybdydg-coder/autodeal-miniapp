"""Fabricated state only. Entire suite blocks network before importing runtime."""
from pathlib import Path
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/'repo'),str(ROOT/'repo/experiments/free_search/tests')]
from experiments.free_search.network_guard import OutboundGuard
guard=OutboundGuard()

class TransferTests(unittest.TestCase):
    def setUp(self):
        import state_transfer as t
        self.t=t;self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        for name in t.REQUIRED:
            with self.t.connect(self.source/name) as db:
                if name==t.LOCK:db.execute('CREATE TABLE executor(id INTEGER PRIMARY KEY,token TEXT,started REAL)')
                else:
                    db.execute('CREATE TABLE history(first_seen REAL, publication_at REAL, state TEXT)')
                    db.execute("INSERT INTO history VALUES(100,NULL,'uncertain')")
        self.outbox=self.source/'continuous-browser-outbox';self.outbox.mkdir()
        (self.outbox/'1234.job.json').write_text('{"first_seen":100}')
        (self.outbox/'1234.result.json').write_text('{"state":"expired"}')
        (self.source/'owner-service.stop').write_text('operator_stop')
        (self.source/'owner-service-status.json').write_text('{"state":"stopped"}')
        (self.source/'owner-service.credentials.xml').write_text('DO NOT COPY')
        (self.source/'comparison-current-access-20261021.json').write_text('DO NOT COPY')
        self.proof=self.root/'proof.json'
        self.proof.write_text(json.dumps({'task':'AutoDeal-Owner-Free-Search','enabled':False,'at':time.time()}))
        self.bundle=self.root/'bundle'

    def export(self):
        return self.t.export_state(self.source,self.bundle,disabled_proof=self.proof)

    def test_round_trip_history_never_reset_and_no_secrets(self):
        self.export();dest=self.root/'restored';self.t.restore_state(self.bundle,dest)
        with self.t.connect(dest/'night21-candidates.sqlite3') as db:
            self.assertEqual(db.execute('SELECT * FROM history').fetchone(),(100,None,'uncertain'))
        with self.t.connect(self.source/self.t.LOCK) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM executor').fetchone()[0],0)
        for name in ('owner-service.credentials.xml','comparison-current-access-20261021.json','cloud-activation.json'):
            self.assertFalse((dest/name).exists())
        self.assertEqual((dest/'continuous-browser-outbox/1234.result.json').read_text(),'{"state":"expired"}')

    def test_busy_lock_never_reclaimed(self):
        with self.t.connect(self.source/self.t.LOCK) as db:db.execute("INSERT INTO executor VALUES(1,'other',1)")
        with self.assertRaisesRegex(ValueError,'executor_busy'):self.export()
        with self.t.connect(self.source/self.t.LOCK) as db:self.assertEqual(db.execute('SELECT token FROM executor').fetchone()[0],'other')
        self.assertFalse(self.bundle.exists())

    def test_active_local_service_blocks_export(self):
        (self.source/'owner-service-status.json').write_text('{"state":"running"}')
        with self.assertRaisesRegex(ValueError,'graceful_local_stop'):self.export()

    def test_live_browser_blocks_export(self):
        (self.outbox/'browser-service.lock').write_text('busy')
        with self.assertRaisesRegex(ValueError,'browser_still_owned'):self.export()

    def test_disabled_autostart_required(self):
        self.proof.write_text(json.dumps({'task':'AutoDeal-Owner-Free-Search','enabled':True,'at':time.time()}))
        with self.assertRaisesRegex(ValueError,'disabled_proof'):self.export()

    def test_old_counter_required(self):
        (self.source/'public-mean-50-20261020.sqlite3').unlink()
        with self.assertRaisesRegex(ValueError,'historical_state_missing'):self.export()

    def test_tampered_history_rejected(self):
        self.export();(self.bundle/'continuous-browser-outbox/1234.job.json').write_text('changed')
        with self.assertRaisesRegex(ValueError,'hash_mismatch'):self.t.restore_state(self.bundle,self.root/'restored')

    def test_no_overwrite_existing_server_history(self):
        self.export();dest=self.root/'restored';self.t.restore_state(self.bundle,dest)
        with self.assertRaisesRegex(ValueError,'destination_not_empty'):self.t.restore_state(self.bundle,dest)

    def test_manifest_cannot_escape_or_include_secrets(self):
        for name in ('../outside','/root/secret','C:/secret','owner-service.credentials.xml','continuous-browser-outbox/../secret'):
            with self.assertRaises(ValueError):self.t.safe_name(name)

    def test_source_hold_is_preserved(self):
        (self.source/'owner-browser-hold.json').write_text('{"reason":"source_status_403"}')
        self.export();dest=self.root/'restored';self.t.restore_state(self.bundle,dest)
        self.assertTrue((dest/'owner-browser-hold.json').is_file())

    def test_server_requires_owner_matched_activation(self):
        import cloud_entry as entry
        self.export();dest=self.root/'restored';self.t.restore_state(self.bundle,dest)
        manifest=json.loads((dest/'handoff-manifest.json').read_text())
        with self.assertRaises(FileNotFoundError):entry.verify_handoff(dest,111)
        activation={'version':1,'owner_id':111,'handoff_id':manifest['handoff_id'],'local_autostart_disabled':True}
        (dest/'cloud-activation.json').write_text(json.dumps(activation))
        self.assertEqual(entry.verify_handoff(dest,111)['handoff_id'],manifest['handoff_id'])
        with self.assertRaisesRegex(ValueError,'invalid'):entry.verify_handoff(dest,222)

class CloudRuntimeTests(unittest.TestCase):
    def test_egress_retains_bytes_and_restart_reserve(self):
        from experiments.free_search.owner_cloud_egress import Egress,RESERVE,LIMIT
        with tempfile.TemporaryDirectory() as tmp:
            counter=[10];now=[1792606400]
            gate=Egress(Path(tmp)/'egress.sqlite3',clock=lambda:now[0],counter=lambda:counter[0])
            counter[0]+=100
            self.assertEqual(gate.check(),RESERVE+100)
            gate.close()
            gate=Egress(Path(tmp)/'egress.sqlite3',clock=lambda:now[0],counter=lambda:0)
            self.assertEqual(gate.check(),2*RESERVE+100)
            gate.db.execute('UPDATE months SET used=?',(LIMIT,))
            with self.assertRaisesRegex(ValueError,'egress_guard'):gate.check()
            gate.close()

    def test_egress_ignores_loopback_and_counts_transmit_only(self):
        from experiments.free_search.owner_cloud_egress import transmitted
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'netdev'
            f.write_text(' lo: 100 0 0 0 0 0 0 0 100 0 0 0 0 0 0 0\n eth0: 9999 0 0 0 0 0 0 0 77 0 0 0 0 0 0 0\n')
            self.assertEqual(transmitted(f),77)

    def test_linux_binary_is_detected(self):
        from experiments.free_search.run_owner_service import browser_binary_available
        with tempfile.TemporaryDirectory() as tmp:
            binary=Path(tmp)/'chromium-123/chrome-linux64/chrome'
            binary.parent.mkdir(parents=True);binary.write_text('fixture')
            self.assertTrue(browser_binary_available(tmp,'posix'))
            self.assertFalse(browser_binary_available(tmp,'nt'))

    def test_only_nonsecret_linux_configuration_reaches_children(self):
        from experiments.free_search.owner_local_bridge import child_environment
        env={'HOME':'/root','AUTODEAL_OWNER_STATE_DIR':'/var/data','AUTODEAL_OWNER_BROWSER_BINARIES':'/opt/browsers',
             'AUTODEAL_OWNER_DATABASE_URL':'SECRET','AUTODEAL_OWNER_TELEGRAM_TOKEN':'SECRET','HTTP_PROXY':'SECRET'}
        actual=child_environment(env)
        self.assertEqual(len(actual),3);self.assertNotIn('SECRET',json.dumps(actual))

    def test_internal_tls_dsn_normalized_without_changing_secret(self):
        from cloud_entry import environment
        env={'AUTODEAL_OWNER_ID':'111','SUBSCRIPTION_EXPECTED_ADMIN_ID':'111',
             'AUTODEAL_OWNER_DATABASE_URL':'postgresql://fixture:synthetic@dpg-dal2giqd0e5s738ajb20-a/db',
             'AUTODEAL_OWNER_TELEGRAM_TOKEN':'synthetic'}
        self.assertEqual(environment(env),111)
        self.assertTrue(env['AUTODEAL_OWNER_DATABASE_URL'].startswith('postgresql+psycopg://'))
        self.assertIn('sslmode=require',env['AUTODEAL_OWNER_DATABASE_URL'])
        env['RIA_API_KEY']='synthetic'
        with self.assertRaisesRegex(ValueError,'paid_credentials_forbidden'):environment(env)

    def test_no_paid_worker_or_app_in_server_package(self):
        for name in ('app','worker','monitor','auto_ria','ria_ai_price','ria_search'):
            self.assertFalse((ROOT/'repo/backend'/(name+'.py')).exists())
            self.assertNotIn('backend.'+name,sys.modules)

if __name__=='__main__':
    with guard.isolated():
        import test_owner_local_bridge
        suite=unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(test_owner_local_bridge))
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    print('OFFLINE',guard.summary())
    raise SystemExit(0 if result.wasSuccessful() and guard.attempted==0 else 1)
