"""Offline intake of counter-service observations and sampled certification evidence.

Two intake paths coexist:

* ``checks`` keeps the original simple per-observation quality grade
  (``quality_ok.json`` compatibility).
* The certification path imports a sealed sampling plan first, then pre-signed
  frame tasks, independent visit receipts, physical ticket resolutions, late
  terminal receipts, certificate publications/revocations and promise-policy
  changes. The plan must exist and be sealed before any task or visit can be
  recorded, so staff cannot hand-pick short queues after the fact.
"""

import json

from .source import record
from . import store_score


def _evidence(db, event_id, kind, row):
    if not record(db, event_id, kind, row):
        return False
    return True


def _import_plans(db, data, touched):
    for row in data.get('sampling_plans', []):
        existing = db.execute('SELECT * FROM sampling_plan WHERE plan_id=?', (row['plan_id'],)).fetchone()
        if existing:
            frozen = (existing['shop'], existing['week'], existing['min_per_stratum'],
                      existing['on_time_threshold'], existing['promise_minutes'], existing['sealed_at'])
            incoming = (row['shop'], row['week'], row['min_per_stratum'],
                        row['on_time_threshold'], row['promise_minutes'], row['sealed_at'])
            if frozen != incoming:
                raise ValueError(f"sealed sampling plan {row['plan_id']} cannot be modified")
            existing_strata = db.execute(
                'SELECT stratum,frame,min_samples FROM sampling_stratum WHERE plan_id=? ORDER BY stratum',
                (row['plan_id'],)).fetchall()
            incoming_strata = sorted(
                (s['stratum'], s['frame'], s.get('min_samples', row['min_per_stratum']))
                for s in row['strata'])
            if [tuple(s) for s in existing_strata] != incoming_strata:
                raise ValueError(f"sealed sampling frame of {row['plan_id']} cannot be modified")
            continue
        _evidence(db, row['plan_id'], 'sampling_plan', row)
        db.execute('INSERT INTO sampling_plan VALUES(?,?,?,?,?,?,?)',
                   (row['plan_id'], row['shop'], row['week'], row['min_per_stratum'],
                    row['on_time_threshold'], row['promise_minutes'], row['sealed_at']))
        for stratum in row['strata']:
            min_samples = stratum.get('min_samples', row['min_per_stratum'])
            db.execute('INSERT INTO sampling_stratum VALUES(?,?,?,?)',
                       (row['plan_id'], stratum['stratum'], stratum['frame'], min_samples))
        touched.add(row['plan_id'])


def _import_promise_changes(db, data, touched):
    for row in data.get('promise_changes', []):
        if not _evidence(db, row['event_id'], 'promise_policy', row):
            continue
        db.execute('INSERT INTO promise_policy VALUES(?,?,?,?)',
                   (row['event_id'], row['shop'], row['promise_minutes'], row['changed_at']))
        plans = db.execute('SELECT plan_id FROM sampling_plan WHERE shop=?', (row['shop'],)).fetchall()
        for plan in plans:
            touched.add(plan['plan_id'])


def _import_tasks(db, data, touched):
    for row in data.get('sampling_tasks', []):
        plan = db.execute('SELECT * FROM sampling_plan WHERE plan_id=?', (row['plan_id'],)).fetchone()
        if not plan:
            raise ValueError(f"task {row['task_id']} cannot be created before plan {row['plan_id']} is sealed")
        if db.execute('SELECT 1 FROM sampling_task WHERE task_id=?', (row['task_id'],)).fetchone():
            continue
        if row['signed_at'] < plan['sealed_at']:
            raise ValueError(f"task {row['task_id']} signed before the sampling frame was sealed")
        _evidence(db, row['task_id'], 'sampling_task', row)
        # The promise is fixed at task signing time; later policy changes never
        # rewrite this value, so old overtime cannot be retroactively excused.
        promise = row.get('promise_minutes', plan['promise_minutes'])
        db.execute('INSERT INTO sampling_task VALUES(?,?,?,?,?,?)',
                   (row['task_id'], row['plan_id'], row['stratum'], row['slot_at'],
                    promise, row['signed_at']))
        touched.add(row['plan_id'])


def _import_visits(db, data, touched):
    for row in data.get('visits', []):
        if db.execute('SELECT 1 FROM visit_evidence WHERE event_id=?', (row['event_id'],)).fetchone():
            touched.add(row['plan_id'])
            continue
        plan = db.execute('SELECT * FROM sampling_plan WHERE plan_id=?', (row['plan_id'],)).fetchone()
        if not plan:
            raise ValueError(f"visit {row['event_id']} cannot be recorded before its plan is sealed")
        _evidence(db, row['event_id'], 'visit_evidence', row)
        task = db.execute('SELECT * FROM sampling_task WHERE task_id=? AND plan_id=?',
                          (row['task_id'], row['plan_id'])).fetchone()
        stratum = task['stratum'] if task else row.get('stratum', 'unframed')
        service_receipt = row.get('service_receipt')
        receipt_minutes = service_receipt.get('minutes') if service_receipt else None
        check_in_text = json.dumps(row['check_in_receipt'], ensure_ascii=False, sort_keys=True)
        service_text = json.dumps(service_receipt, ensure_ascii=False, sort_keys=True) if service_receipt else None
        effective = receipt_minutes if receipt_minutes is not None else row['observed_minutes']
        db.execute('INSERT INTO visit_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (row['event_id'], row['plan_id'], row['task_id'], row['shop'], row['visit_id'],
                    stratum, row['observed_at'], row['assessor'], row.get('service_check_id'),
                    check_in_text, service_text, row['presence_ref'], row['observed_minutes'],
                    receipt_minutes, row.get('uploaded_at', row['observed_at']), effective))
        if row.get('service_check_id'):
            check = db.execute('SELECT * FROM service_check WHERE event_id=?',
                               (row['service_check_id'],)).fetchone()
            if check:
                store_score.decide(db, check['shop'])
        touched.add(row['plan_id'])


def _import_late_receipts(db, data, touched):
    for row in data.get('late_receipts', []):
        event_id = row['visit_event_id']
        visit = db.execute('SELECT * FROM visit_evidence WHERE event_id=?', (event_id,)).fetchone()
        if not visit:
            raise ValueError(f'late receipt {row["event_id"]} references unknown visit {event_id}')
        if not _evidence(db, row['event_id'], 'late_receipt', row):
            touched.add(visit['plan_id'])
            continue
        service_text = json.dumps(row['service_receipt'], ensure_ascii=False, sort_keys=True)
        db.execute('UPDATE visit_evidence SET service_receipt=?,receipt_minutes=?,effective_minutes=?,uploaded_at=? '
                   'WHERE event_id=?',
                   (service_text, row['service_receipt']['minutes'], row['service_receipt']['minutes'],
                    row['received_at'], event_id))
        touched.add(visit['plan_id'])


def _import_resolutions(db, data, touched):
    for row in data.get('ticket_resolutions', []):
        if db.execute('SELECT 1 FROM ticket_resolution WHERE event_id=?', (row['event_id'],)).fetchone():
            touched.add(row['plan_id'])
            continue
        if not db.execute('SELECT 1 FROM sampling_plan WHERE plan_id=?', (row['plan_id'],)).fetchone():
            raise ValueError(f"ticket resolution {row['event_id']} references an unknown plan")
        if row['verdict'] not in ('independent', 'rescan'):
            raise ValueError(f"unsupported ticket verdict: {row['verdict']}")
        _evidence(db, row['event_id'], 'ticket_resolution', row)
        db.execute('INSERT INTO ticket_resolution VALUES(?,?,?,?,?,?,?)',
                   (row['event_id'], row['plan_id'], row['ticket_ref'], row['visit_event_id'],
                    row['verdict'], row['chain_ref'], row['decided_at']))
        touched.add(row['plan_id'])


def _import_certificates(db, data, touched):
    for row in data.get('certificate_publications', []):
        plan_id = row['plan_id']
        store_score.publish_certificate(db, plan_id, row['recipients'], row['issued_at'],
                                        cert_id=row.get('cert_id'))
        touched.add(plan_id)
    for row in data.get('certificate_revocations', []):
        store_score.revoke_certificate(db, row['plan_id'], row['reason'], row['revoked_at'])
        touched.add(row['plan_id'])


def import_data(db, data):
    """Import a certification evidence batch and recompute every touched plan.

    Ordering enforces the evidence lifecycle: sealed frame -> signed tasks ->
    visits -> late receipts / physical resolutions -> publications.
    """
    shops = set()
    results = []
    touched_plans = set()
    with db:
        # Legacy simple observation path, unchanged for quality_ok.json.
        for row in data.get('checks', []):
            if record(db, row['event_id'], 'service_check', row):
                db.execute('INSERT INTO service_check VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['shop'], row['visit_id'], row['observed_at'],
                            row['assessor'], row['queue_minutes'], row['promise_minutes']))
            shops.add(row['shop'])
        if shops:
            results.extend(store_score.decide(db, shop) for shop in sorted(shops))

        _import_plans(db, data, touched_plans)
        _import_promise_changes(db, data, touched_plans)
        _import_tasks(db, data, touched_plans)
        _import_visits(db, data, touched_plans)
        _import_late_receipts(db, data, touched_plans)
        _import_resolutions(db, data, touched_plans)
        for plan_id in sorted(touched_plans):
            results.append(store_score.certification_decide(db, plan_id))
        _import_certificates(db, data, touched_plans)
        # Recompute after publications/revocations so the returned view is final.
        for plan_id in sorted(touched_plans):
            view = store_score.certification_view(db, plan_id)
            if view:
                results = [r for r in results
                           if not (isinstance(r, dict) and r.get('plan_id') == plan_id)]
                results.append(view)
        return results
