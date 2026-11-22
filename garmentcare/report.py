import json

from . import catalog


def _decode(value):
    if isinstance(value, list):
        return value
    return json.loads(value) if value else []


def _label_view(db, sku):
    entry = catalog.entry(db, sku)
    if entry is None:
        return None
    revisions = catalog.revisions(db, sku)
    return {
        'entry': entry,
        'revisions': revisions,
        'reviews': [dict(row) for row in db.execute(
            'SELECT * FROM garment_label_review WHERE sku=? ORDER BY garment_id', (sku,))],
        'review_history': [dict(row) for row in db.execute(
            'SELECT garment_id,version,task_state,reason,route,label_state,label_care,label_event,'
            'impact,conflict FROM garment_label_review_history WHERE garment_id IN '
            '(SELECT id FROM garment WHERE sku=?) ORDER BY id', (sku,))],
        'route_events': [dict(row) for row in db.execute(
            'SELECT * FROM label_route_event WHERE sku=? ORDER BY effective_at,event_id', (sku,))],
    }


def report(db, as_of=None):
    garments = []
    for g in db.execute('SELECT * FROM garment ORDER BY id'):
        item = dict(g)
        for table, key in [('intake_decision', 'intake'), ('process_decision', 'process'),
                           ('compensation_decision', 'compensation')]:
            row = db.execute(f'SELECT * FROM {table} WHERE garment_id=?', (g['id'],)).fetchone()
            item[key] = dict(row) if row else None
        review_row = db.execute('SELECT * FROM garment_label_review WHERE garment_id=?', (g['id'],)).fetchone()
        item['label_review'] = dict(review_row) if review_row else None
        item['label_revision_history'] = [dict(row) for row in db.execute(
            'SELECT garment_id,version,task_state,reason,route,label_state,label_care,label_event,'
            'impact,conflict FROM garment_label_review_history WHERE garment_id=? ORDER BY id', (g['id'],))]
        metas = {row['history_id']: dict(row) for row in db.execute(
            'SELECT * FROM decision_history_meta WHERE history_id IN '
            '(SELECT id FROM decision_history WHERE garment_id=?)', (g['id'],))}
        item['history'] = []
        for row in db.execute(
                'SELECT id,subsystem,version,state,reason FROM decision_history WHERE garment_id=? ORDER BY id',
                (g['id'],)):
            entry = dict(row)
            meta = metas.get(entry.pop('id'), {})
            entry['evidence_ref'] = meta.get('evidence_ref')
            item['history'].append(entry)
        garments.append(item)

    labels = []
    for row in db.execute('SELECT * FROM label_entry ORDER BY sku'):
        entry = dict(row)
        for key in ('candidates', 'conflict_events', 'pending_events', 'withdrawn_events', 'source_chain'):
            entry[key] = _decode(entry[key])
        if as_of is not None:
            run = catalog.adjudicate_as_of(db, entry['sku'], as_of)
            entry.update({key: run[key] for key in (
                'state', 'reason', 'careness', 'source', 'source_event', 'material', 'care',
                'effective_at', 'effective_basis', 'candidates', 'pending_events',
                'conflict_events', 'withdrawn_events', 'source_chain')})
            entry['as_of'] = as_of
            entry['superseded_events'] = run.get('superseded_events', [])
        view = _label_view(db, entry['sku'])
        entry['availability'] = catalogue_availability(entry, as_of)
        entry['revision_history'] = view['revisions']
        entry['superseded_events'] = catalog._superseded_from_chain(entry['source_chain'])
        entry['garment_reviews'] = view['reviews']
        entry['review_history'] = view['review_history']
        entry['route_events'] = view['route_events']
        labels.append(entry)

    withdrawn = [dict(row) for row in db.execute('SELECT * FROM label_withdrawal ORDER BY sku,effective_at,event_id')]
    return {
        'garments': garments,
        'labels': labels,
        'withdrawals': withdrawn,
        'source_files': [dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
        'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
    }


def catalogue_availability(entry, as_of=None):
    """What a reader may rely on for a SKU at a business time.

    The adopted instruction is only reported as available when it is verified
    and (if ``as_of`` is supplied) already effective at that time.  Pending
    material is listed separately so it can be chased, not used.
    """
    if entry is None:
        return {'as_of': as_of, 'material': None, 'care': None, 'usable': False,
                'state': 'empty', 'pending': [], 'conflicts': []}
    effective = entry.get('effective_at')
    usable = entry.get('careness') == 'verified' and (
        as_of is None or (effective is not None and effective <= as_of))
    return {
        'as_of': as_of,
        'material': entry.get('material') if usable else None,
        'care': entry.get('care') if usable else None,
        'usable': usable,
        'state': entry.get('state'),
        'adopt_event': entry.get('source_event'),
        'effective_at': effective,
        'pending': _decode(entry.get('pending_events')),
        'conflicts': _decode(entry.get('conflict_events')),
        'withdrawn': _decode(entry.get('withdrawn_events')),
        'superseded': _decode(entry.get('superseded_events')),
    }


def dumps(db, as_of=None):
    return json.dumps(report(db, as_of), ensure_ascii=False, indent=2)
