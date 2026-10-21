"""Read-only evidence audit. Never an authorization token or sender."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .valuation import estimate
from .owner_valuation_policy import OWNER_VALUATION_POLICY as POLICY


def assess_asking_research(subject, peers, scan, now):
    if type(now) not in (float, int) or not math.isfinite(now) or now <= 0:
        raise ValueError('invalid_audit_clock')
    blockers = []
    eligible = []
    ids = set()
    rejected = {}
    if not isinstance(subject, dict):
        subject = {}
    if not isinstance(peers, list):
        peers = []
        blockers.append('peer_evidence_missing')
    for row in peers:
        ident = row.get('listing_id') if isinstance(row, dict) else None
        if not isinstance(ident, str) or not ident or ident in ids or ident == subject.get('listing_id'):
            blockers.append('peer_identity_invalid_or_duplicate')
            continue
        ids.add(ident)
        for field in ('generation', 'drive'):
            if not subject.get(field) or not row.get(field):
                rejected[ident] = field+'_unknown'
                break
            if row[field] != subject[field]:
                rejected[ident] = field+'_mismatch'
                break
        else:
            eligible.append(row)
    if len(eligible) < POLICY.min_peers:
        blockers.append('fewer_than_five_candidate_peers')
    # Recompute rather than trusting persisted ready/status/sample_count fields.
    valuation = estimate(subject, eligible, now, POLICY).as_dict()
    if valuation['status'] != 'estimated':
        blockers.append(valuation['reason'])
    complete = (isinstance(scan, dict) and scan.get('phase') == 'complete'
                and scan.get('baseline') == 0 and type(scan.get('last_at')) in (float, int)
                and math.isfinite(scan['last_at']) and 0 <= now-scan['last_at'] <= 300)
    if not complete:
        blockers.append('current_complete_nonbaseline_scan_missing')
    # Static reports cannot attest to current live DB access, a reviewed cutover,
    # or transactional delivery eligibility. These remain mandatory runtime gates.
    blockers.extend(['current_owner_access_and_filters_not_revalidated',
                     'reviewed_live_integration_not_verified'])
    return dict(as_of=now, ready_for_delivery=False, authorization=False,
                blockers=sorted(set(blockers)), valuation=valuation,
                rejected=rejected, publication_at=None, whole_market_recall=None,
                audit_scope='persisted_evidence_only_not_live_authorization')


def assess(subject, peers, scan, now):
    # Persisted peer evidence never authorizes the new public-mean source.
    if type(now) not in (float, int) or not math.isfinite(now) or now <= 0:
        raise ValueError('invalid_audit_clock')
    complete = (isinstance(scan, dict) and scan.get('phase') == 'complete'
        and scan.get('baseline') == 0 and type(scan.get('last_at')) in (int, float)
        and math.isfinite(scan['last_at']) and 0 <= now-scan['last_at'] <= 300)
    blockers = ['public_mean_numeric_source_not_live_verified',
        'current_owner_access_and_filters_not_revalidated', 'reviewed_live_integration_not_verified']
    if not complete: blockers.append('current_complete_nonbaseline_scan_missing')
    return dict(as_of=now, ready_for_delivery=False, authorization=False,
        blockers=blockers, valuation=None, required_peers=0, rejected={},
        publication_at=None, whole_market_recall=None,
        audit_scope='public_mean_persisted_evidence_not_authorization')


def read_sqlite(path, query):
    # mode=ro prevents creating a missing database or changing existing evidence.
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    try:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query)]
    finally:
        db.close()


def audit(workspace, now):
    workspace = Path(workspace)
    errors, hashes = [], {}

    def read(name):
        try:
            data = (workspace/name).read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError('object_required')
            return result
        except (OSError, ValueError):
            errors.append(name)
            return {}

    details = read('night-control-details.json')
    pg = read('night-fullpath-postgres-20261020-0312.json')
    concurrency = read('owner-postgres-concurrency-20261019.json')
    for report, name in ((pg, 'pg_fullpath_schema'), (concurrency, 'pg_concurrency_schema')):
        if not isinstance(report.get('cases'), list):
            errors.append(name)
            report['cases'] = []
            report['status'] = 'unknown'
    try:
        rows = read_sqlite(workspace/'night-20261020.owner-observations.sqlite3',
                           'SELECT phase,baseline,last_at,reason FROM scans ORDER BY id DESC LIMIT 1')
        scan = rows[0] if rows else {}
    except (OSError, sqlite3.Error):
        scan = {}
        errors.append('scan_database')
    try:
        budget = read_sqlite(workspace/'night-20261020.sqlite3',
            'SELECT max_gets,max_bytes,hold FROM budget WHERE id=1')[0]
        budget.update(read_sqlite(workspace/'night-20261020.sqlite3',
            'SELECT count(*) AS reserved_gets,coalesce(sum(cap),0) AS reserved_bytes,'
            'coalesce(sum(bytes),0) AS received_bytes,'
            'sum(CASE WHEN completed IS NULL THEN 1 ELSE 0 END) AS unfinished FROM requests')[0])
    except (OSError, sqlite3.Error, IndexError):
        budget = {}
        errors.append('budget_database')
    peer_data = details.get('accumulated_pool')
    peers = peer_data.get('rows') if isinstance(peer_data, dict) else None
    result = assess(details.get('subject'), peers, scan, now)
    result.update(evidence_errors=errors, input_sha256=hashes, scan=scan, budget=budget,
                  pg_fullpath_report_status=pg.get('status'),
                  pg_fullpath_cases=len(pg.get('cases', [])),
                  pg_concurrency_report_status=concurrency.get('status'),
                  pg_concurrency_cases=len(concurrency.get('cases', [])),
                  pg_transport_kind='synthetic_not_Telegram',
                  real_deal_delivery_verified=False,
                  diagnostic_message_excluded=27366)
    if errors:
        result['blockers'].append('evidence_missing_or_invalid')
    if (budget.get('max_gets') != 40 or budget.get('max_bytes') != 100000000
            or budget.get('reserved_gets', 41) > 40
            or budget.get('reserved_bytes', 100000001) > 100000000
            or budget.get('hold') != 0 or budget.get('unfinished') != 0):
        result['blockers'].append('budget_policy_or_source_state_not_clear')
    return result
