import json


def report(db):
    garments = []
    for g in db.execute('SELECT * FROM garment ORDER BY id'):
        item = dict(g)
        for table, key in [('intake_decision', 'intake'), ('process_decision', 'process'), ('compensation_decision', 'compensation')]:
            row = db.execute(f'SELECT * FROM {table} WHERE garment_id=?', (g['id'],)).fetchone()
            item[key] = dict(row) if row else None
        item['history'] = [dict(row) for row in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE garment_id=? ORDER BY id', (g['id'],))]
        garments.append(item)
    certifications = []
    for plan in db.execute('SELECT * FROM sampling_plan ORDER BY plan_id'):
        from .store_score import certification_view
        view = certification_view(db, plan['plan_id'])
        item = dict(plan)
        item['strata'] = [dict(row) for row in
                          db.execute('SELECT * FROM sampling_stratum WHERE plan_id=? ORDER BY stratum',
                                     (plan['plan_id'],))]
        item['decision'] = {k: view[k] for k in
                            ('state', 'reason', 'valid_samples', 'on_time_samples', 'late_samples',
                             'stratum_summary', 'version')} if view else None
        item['rectification'] = view['rectification'] if view else None
        item['certificates'] = view['certificates'] if view else []
        item['history'] = view['history'] if view else []
        item['visit_adjudications'] = [
            {'event_id': v['event_id'], 'task_id': v['task_id'], 'visit_id': v['visit_id'],
             'stratum': v['stratum'], 'adjudication': v['adjudication'],
             'adjudication_reason': v['adjudication_reason'],
             'effective_minutes': v['effective_minutes']}
            for v in view['visits']] if view else []
        certifications.append(item)
    return {'garments': garments,
            'store_grades': [dict(row) for row in db.execute('SELECT * FROM store_grade ORDER BY shop')],
            'certifications': certifications,
            'promise_policies': [dict(row) for row in
                                 db.execute('SELECT * FROM promise_policy ORDER BY changed_at,event_id')],
            'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
