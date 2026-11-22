"""Shop scoring: baseline observation grade and sampled service certification."""

import hashlib
import json


def decide(db, shop):
    rows = db.execute('SELECT * FROM service_check WHERE shop=?', (shop,)).fetchall()
    failed = sum(row['queue_minutes'] > row['promise_minutes'] for row in rows)
    state = 'needs_improvement' if failed else 'meets_promise'
    db.execute('INSERT INTO store_grade VALUES(?,?,?,?) ON CONFLICT(shop) DO UPDATE SET state=excluded.state,failed_visits=excluded.failed_visits,checked_visits=excluded.checked_visits',
               (shop, state, failed, len(rows)))
    return dict(db.execute('SELECT * FROM store_grade WHERE shop=?', (shop,)).fetchone())


# ---------------------------------------------------------------------------
# Sampled "ten minute reception promise" certification
# ---------------------------------------------------------------------------

def _parse_receipt(value):
    if not value:
        return None
    parsed = json.loads(value)
    return parsed


def adjudicate_visit(db, visit):
    """Return (status, reason, effective_minutes) for one imported visit.

    status is one of: valid, awaiting_receipt, ticket_disputed,
    duplicate_rescan, out_of_frame. Only ``valid`` rows count as independent
    random samples of the sealed sampling frame.
    """
    task = db.execute('SELECT * FROM sampling_task WHERE task_id=? AND plan_id=?',
                      (visit['task_id'], visit['plan_id'])).fetchone()
    if not task:
        return 'out_of_frame', 'visit is not linked to a pre-signed frame task', None
    check_in = _parse_receipt(visit['check_in_receipt'])
    ticket_ref = check_in.get('ticket') if check_in else None
    resolution = db.execute('SELECT * FROM ticket_resolution WHERE visit_event_id=?',
                            (visit['event_id'],)).fetchone()
    if resolution and resolution['verdict'] == 'rescan':
        return 'duplicate_rescan', 'ticket original proves this visit rescanned an earlier ticket', None
    if ticket_ref:
        # Only a *later* check-in reusing a ticket already seen on an earlier
        # visit is suspicious; the original visit keeps its sample standing.
        for other in db.execute('SELECT * FROM visit_evidence WHERE plan_id=? AND event_id<>?',
                                (visit['plan_id'], visit['event_id'])).fetchall():
            other_ticket = _parse_receipt(other['check_in_receipt']).get('ticket')
            if other_ticket != ticket_ref:
                continue
            if (other['observed_at'], other['event_id']) < (visit['observed_at'], visit['event_id']):
                if not resolution:
                    return 'ticket_disputed', 'shared queue ticket awaits independent physical evidence', None
    if not visit['service_receipt'] or not visit['presence_ref'] or not visit['assessor']:
        return 'awaiting_receipt', 'independent reception terminal receipt or presence trail missing', None
    if resolution and resolution['verdict'] != 'independent':
        return 'ticket_disputed', f"unresolved ticket verdict: {resolution['verdict']}", None
    effective = visit['receipt_minutes']
    if effective is None:
        effective = visit['observed_minutes']
    return 'valid', 'independent visit with queue and reception terminal receipts', effective


def _cert_snapshot(plan, summary, valid_total, on_time_total, cert_id, issued_at):
    rate = round(on_time_total * 100 / valid_total) if valid_total else 0
    return {
        'plan_id': plan['plan_id'], 'shop': plan['shop'], 'week': plan['week'],
        'promise_minutes': plan['promise_minutes'],
        'on_time_threshold': plan['on_time_threshold'],
        'valid_sample_count': valid_total, 'on_time_count': on_time_total,
        'on_time_rate': rate, 'strata': summary, 'cert_id': cert_id,
        'issued_at': issued_at,
    }


def _snapshot_sha(snapshot):
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(body).hexdigest()


def _batch_timestamp(db, plan_id):
    stamps = []
    queries = [
        'SELECT uploaded_at AS v FROM visit_evidence WHERE plan_id=?',
        'SELECT observed_at AS v FROM visit_evidence WHERE plan_id=?',
        'SELECT decided_at AS v FROM ticket_resolution WHERE plan_id=?',
        'SELECT signed_at AS v FROM sampling_task WHERE plan_id=?',
    ]
    for sql in queries:
        stamps.extend(r['v'] for r in db.execute(sql, (plan_id,)).fetchall() if r['v'])
    plan = db.execute('SELECT sealed_at FROM sampling_plan WHERE plan_id=?', (plan_id,)).fetchone()
    if plan:
        stamps.append(plan['sealed_at'])
    return max(stamps) if stamps else None


def _history(db, plan_id, version, state, reason, at):
    last = db.execute('SELECT state,reason FROM certification_history WHERE plan_id=? ORDER BY id DESC LIMIT 1',
                      (plan_id,)).fetchone()
    if last and last['state'] == state and last['reason'] == reason:
        return
    db.execute('INSERT INTO certification_history(plan_id,version,state,reason,at) VALUES(?,?,?,?,?)',
               (plan_id, version, state, reason, at))


def certification_decide(db, plan_id):
    """Recompute the certification result for one sealed sampling plan.

    Maintains the valid sample set, publish/pending/fail state, certificate
    versions (including revoked/superseded external publications) and the
    reception-capability rectification duty. All outputs are derived purely
    from imported evidence so audit replay stays deterministic.
    """
    plan = db.execute('SELECT * FROM sampling_plan WHERE plan_id=?', (plan_id,)).fetchone()
    if not plan:
        raise ValueError(f'unknown sampling plan: {plan_id}')
    strata = db.execute('SELECT * FROM sampling_stratum WHERE plan_id=? ORDER BY stratum', (plan_id,)).fetchall()
    visits = db.execute('SELECT * FROM visit_evidence WHERE plan_id=? ORDER BY observed_at,event_id',
                        (plan_id,)).fetchall()

    adjudicated = []
    for visit in visits:
        status, reason, effective = adjudicate_visit(db, visit)
        task = db.execute('SELECT promise_minutes FROM sampling_task WHERE task_id=?',
                          (visit['task_id'],)).fetchone()
        promise = task['promise_minutes'] if task else plan['promise_minutes']
        on_time = bool(status == 'valid' and effective is not None and effective <= promise)
        adjudicated.append({'visit': visit, 'status': status, 'reason': reason,
                            'effective_minutes': effective, 'on_time': on_time, 'promise': promise})

    summary = []
    valid_total = on_time_total = late_total = 0
    for stratum in strata:
        rows = [a for a in adjudicated if a['visit']['stratum'] == stratum['stratum'] and a['status'] == 'valid']
        on_time = sum(a['on_time'] for a in rows)
        late = len(rows) - on_time
        summary.append({'stratum': stratum['stratum'], 'frame': stratum['frame'],
                        'min_samples': stratum['min_samples'], 'valid': len(rows),
                        'on_time': on_time, 'late': late})
        valid_total += len(rows)
        on_time_total += on_time
        late_total += late

    missing = [s['stratum'] for s in summary if s['valid'] < s['min_samples']]
    if missing:
        state = 'pending_more_samples'
        reason = 'below minimum independent samples in stratum: ' + ','.join(sorted(set(missing)))
    elif valid_total == 0:
        state, reason = 'pending_more_samples', 'no valid independent visits imported'
    else:
        rate = on_time_total * 100 / valid_total
        if rate >= plan['on_time_threshold']:
            state = 'certifiable'
            reason = f'all strata meet minimum samples and on-time rate {rate:.1f}% >= {plan["on_time_threshold"]}%'
        else:
            state = 'not_certified'
            reason = f'on-time rate {rate:.1f}% below {plan["on_time_threshold"]}% threshold; reception capability rectification required'

    summary_text = json.dumps(summary, sort_keys=True)
    old = db.execute('SELECT * FROM certification_decision WHERE plan_id=?', (plan_id,)).fetchone()
    version = old['version'] + 1 if old else 1
    at = _batch_timestamp(db, plan_id)
    db.execute(
        'INSERT INTO certification_decision VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET '
        'state=excluded.state,reason=excluded.reason,valid_samples=excluded.valid_samples,'
        'on_time_samples=excluded.on_time_samples,late_samples=excluded.late_samples,'
        'stratum_summary=excluded.stratum_summary,version=excluded.version',
        (plan_id, state, reason, valid_total, on_time_total, late_total, summary_text, version))
    _history(db, plan_id, version, state, reason, at)

    latest_cert = db.execute('SELECT * FROM certificate WHERE plan_id=? ORDER BY version DESC LIMIT 1',
                             (plan_id,)).fetchone()
    if state != 'certifiable':
        if latest_cert and latest_cert['state'] == 'published':
            db.execute('UPDATE certificate SET state=?,revoked_at=?,reason=? WHERE plan_id=? AND version=?',
                       ('revoked', at, f'certification overturned: {reason}', plan_id, latest_cert['version']))
            _history(db, plan_id, version, 'revoked',
                     f'certificate v{latest_cert["version"]} revoked: {reason}', at)
        if state == 'not_certified':
            rect = db.execute('SELECT * FROM rectification WHERE plan_id=?', (plan_id,)).fetchone()
            if not rect or rect['state'] != 'open':
                db.execute('INSERT INTO rectification VALUES(?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET '
                           'state=excluded.state,reason=excluded.reason,opened_at=excluded.opened_at,closed_at=NULL',
                           (plan_id, 'open', reason, at, None))
    else:
        rect = db.execute('SELECT * FROM rectification WHERE plan_id=?', (plan_id,)).fetchone()
        if rect and rect['state'] == 'open':
            db.execute('UPDATE rectification SET state=?,closed_at=? WHERE plan_id=?',
                       ('closed', at, plan_id))
        snapshot = _cert_snapshot(plan, summary, valid_total, on_time_total,
                                  latest_cert['cert_id'] if latest_cert else f'cert-{plan_id}-v{version}',
                                  latest_cert['issued_at'] if latest_cert else at)
        sha = _snapshot_sha(snapshot)
        if not latest_cert or latest_cert['state'] != 'published' or latest_cert['content_sha'] != sha:
            if latest_cert and latest_cert['state'] == 'published':
                db.execute('UPDATE certificate SET state=? WHERE plan_id=? AND version=?',
                           ('superseded', plan_id, latest_cert['version']))
                new_version = latest_cert['version'] + 1
                recipients = latest_cert['recipients']
            else:
                new_version = (latest_cert['version'] + 1) if latest_cert else 1
                recipients = latest_cert['recipients'] if latest_cert else '[]'
            snapshot['cert_id'] = f'cert-{plan_id}-v{new_version}'
            sha = _snapshot_sha(snapshot)
            db.execute('INSERT INTO certificate VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (plan_id, new_version, snapshot['cert_id'], 'published', at, None,
                        reason, valid_total, on_time_total, summary_text, recipients, sha))
            _history(db, plan_id, version, 'published',
                     f'certificate v{new_version} issued: {reason}', at)

    return certification_view(db, plan_id)


def publish_certificate(db, plan_id, recipients, issued_at, cert_id=None):
    """Record external publication of the current certificate.

    The displayed content is frozen at this point; later overturn can only
    revoke/reissue, never rewrite a published version.
    """
    decision = db.execute('SELECT * FROM certification_decision WHERE plan_id=?', (plan_id,)).fetchone()
    if not decision or decision['state'] != 'certifiable':
        raise ValueError(f'plan {plan_id} is not certifiable; refusing external publication')
    latest = db.execute('SELECT * FROM certificate WHERE plan_id=? ORDER BY version DESC LIMIT 1',
                        (plan_id,)).fetchone()
    if not latest:
        raise ValueError(f'no certificate exists for plan {plan_id}')
    if latest['state'] != 'published':
        raise ValueError(f'latest certificate of plan {plan_id} is not publishable')
    new_recipients = json.dumps(sorted(recipients), ensure_ascii=False)
    if cert_id is not None and cert_id != latest['cert_id']:
        raise ValueError('cert_id does not match the current certificate version')
    db.execute('UPDATE certificate SET recipients=? WHERE plan_id=? AND version=?',
               (new_recipients, plan_id, latest['version']))
    return certification_view(db, plan_id)


def revoke_certificate(db, plan_id, reason, revoked_at):
    latest = db.execute('SELECT * FROM certificate WHERE plan_id=? ORDER BY version DESC LIMIT 1',
                        (plan_id,)).fetchone()
    if not latest:
        raise ValueError(f'no certificate exists for plan {plan_id}')
    if latest['state'] != 'published':
        raise ValueError(f'latest certificate of plan {plan_id} is already {latest["state"]}')
    db.execute('UPDATE certificate SET state=?,revoked_at=?,reason=? WHERE plan_id=? AND version=?',
               ('revoked', revoked_at, reason, plan_id, latest['version']))
    _history(db, plan_id, latest['version'], 'revoked',
             f'certificate v{latest["version"]} revoked: {reason}', revoked_at)
    return certification_view(db, plan_id)


def certification_view(db, plan_id):
    decision = db.execute('SELECT * FROM certification_decision WHERE plan_id=?', (plan_id,)).fetchone()
    if not decision:
        return None
    result = dict(decision)
    result['stratum_summary'] = json.loads(decision['stratum_summary'])
    result['visits'] = []
    for visit in db.execute('SELECT * FROM visit_evidence WHERE plan_id=? ORDER BY observed_at,event_id',
                            (plan_id,)).fetchall():
        status, reason, effective = adjudicate_visit(db, visit)
        task = db.execute('SELECT promise_minutes FROM sampling_task WHERE task_id=?',
                          (visit['task_id'],)).fetchone()
        item = dict(visit)
        item['check_in_receipt'] = _parse_receipt(visit['check_in_receipt'])
        item['service_receipt'] = _parse_receipt(visit['service_receipt'])
        item['adjudication'] = status
        item['adjudication_reason'] = reason
        item['effective_minutes'] = effective
        item['promise_minutes'] = task['promise_minutes'] if task else None
        item['on_time'] = bool(status == 'valid' and effective is not None
                               and task is not None and effective <= task['promise_minutes'])
        result['visits'].append(item)
    result['certificates'] = [dict(row) for row in
                               db.execute('SELECT * FROM certificate WHERE plan_id=? ORDER BY version', (plan_id,))]
    rect = db.execute('SELECT * FROM rectification WHERE plan_id=?', (plan_id,)).fetchone()
    result['rectification'] = dict(rect) if rect else None
    result['history'] = [dict(row) for row in
                         db.execute('SELECT version,state,reason,at FROM certification_history WHERE plan_id=? ORDER BY id',
                                    (plan_id,))]
    return result
