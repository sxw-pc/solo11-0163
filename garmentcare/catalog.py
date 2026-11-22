"""Offline manufacturer care-label page collection and label adjudication.

The collector deliberately separates four facts that a naive "newest fetch
wins" rule conflates:

* when a page was *fetched* (``fetched_at``),
* when the publisher *declared the instruction effective* (``effective_at``),
* who *signed* the publication (``signer``),
* the *digest of the original page bytes* (``content_sha`` / ``page_sha``).

A publication is the unit of withdrawal: two fetches of identical bytes are
one publication, while a re-issued revision is a new publication.  A
withdrawal removes only the publication it explicitly references.  Pages
without a declared effective time are kept as pending material and never
displace a signed, effective instruction.
"""

import json
import re
from html.parser import HTMLParser

from .source import canonical_json, digest, record, sha256

META_KEYS = {
    'sku', 'material', 'care', 'effective', 'revision', 'signer',
    'withdraws', 'url', 'revision-of', 'content-sha',
}


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        values = dict(attrs)
        key = values.get('name')
        if key in META_KEYS:
            self.values[key] = values.get('content', '')


def _meta(html):
    parser = MetaParser()
    parser.feed(html)
    return parser.values


def _pick(row, meta, name, meta_name=None):
    value = row.get(name)
    if value in (None, ''):
        value = meta.get(meta_name or name, '')
    return value if value is not None else ''


def _revision_key(value):
    numbers = tuple(int(part) for part in re.findall(r'\d+', str(value or '')))
    return numbers or (-1,)


def _compile_page(row):
    meta = _meta(row['html'])
    sku = _pick(row, meta, 'sku')
    if sku != row['sku']:
        raise ValueError('page SKU does not match envelope')
    declared = _pick(row, meta, 'declared_effective_at', 'effective') or None
    return {
        'event_id': row['event_id'],
        'source': row['source'],
        'sku': sku,
        'fetched_at': row['fetched_at'],
        'html': row['html'],
        'declared_effective_at': declared,
        'effective_at': declared,
        'effective_basis': 'declared' if declared else 'missing',
        'content_sha': sha256(row['html']),
        'page_sha': digest({
            'source': row['source'],
            'sku': sku,
            'html': row['html'],
            'revision': _pick(row, meta, 'revision'),
            'declared_effective_at': declared,
            'signer': _pick(row, meta, 'signer'),
        }),
        'revision': _pick(row, meta, 'revision'),
        'signer': _pick(row, meta, 'signer'),
        'material': _pick(row, meta, 'material'),
        'care': _pick(row, meta, 'care'),
        'revision_of': _pick(row, meta, 'revision_of', 'revision-of'),
        'withdraws': _pick(row, meta, 'withdraws'),
        'url': _pick(row, meta, 'url'),
    }


def _compile_withdrawal(row):
    declared = row.get('declared_effective_at') or row.get('effective_at') or None
    return {
        'event_id': row['event_id'],
        'source': row['source'],
        'sku': row['sku'],
        'fetched_at': row['fetched_at'],
        'target_event': row.get('target_event') or '',
        'declared_effective_at': declared,
        'effective_at': declared or row['fetched_at'],
        'effective_basis': 'declared' if declared else 'fetch_time_fallback',
        'reason': row.get('reason', ''),
        'content_sha': sha256(canonical_json(row)),
        'revision': row.get('revision', ''),
        'signer': row.get('signer', ''),
        'target_source': row.get('target_source') or row.get('source'),
        'target_content_sha': row.get('target_content_sha') or '',
        'target_revision': row.get('target_revision') or '',
    }


PAGE_COLUMNS = (
    'event_id', 'source', 'sku', 'fetched_at', 'html', 'declared_effective_at',
    'effective_at', 'effective_basis', 'content_sha', 'page_sha', 'revision',
    'signer', 'declared_material', 'declared_care', 'revision_of', 'withdraws', 'url',
)

WITHDRAWAL_COLUMNS = (
    'event_id', 'source', 'sku', 'fetched_at', 'target_event', 'effective_at',
    'declared_effective_at', 'effective_basis', 'reason', 'content_sha', 'revision',
    'signer', 'target_source', 'target_content_sha', 'target_revision',
)


def _store_page(db, page):
    values = dict(page, declared_material=page['material'], declared_care=page['care'])
    db.execute(
        f"INSERT INTO label_page({','.join(PAGE_COLUMNS)}) VALUES({','.join('?' for _ in PAGE_COLUMNS)})",
        tuple(values.get(column) for column in PAGE_COLUMNS),
    )


def _store_withdrawal(db, withdrawal):
    db.execute(
        f"INSERT INTO label_withdrawal({','.join(WITHDRAWAL_COLUMNS)}) VALUES({','.join('?' for _ in WITHDRAWAL_COLUMNS)})",
        tuple(withdrawal.get(column) for column in WITHDRAWAL_COLUMNS),
    )


def _publications(db, sku):
    groups = {}
    for row in db.execute('SELECT * FROM label_page WHERE sku=? ORDER BY fetched_at,event_id', (sku,)):
        meta = _meta(row['html'])
        page_sha = row['page_sha'] or digest({
            'source': row['source'], 'sku': row['sku'], 'html': row['html'],
            'revision': row['revision'], 'declared_effective_at': row['declared_effective_at'],
            'signer': row['signer'],
        })
        group = groups.get(page_sha)
        if group is None:
            group = groups[page_sha] = {
                'page_sha': page_sha,
                'content_sha': row['content_sha'] or sha256(row['html']),
                'source': row['source'],
                'sku': row['sku'],
                'material': row['declared_material'] or meta.get('material', ''),
                'care': row['declared_care'] or meta.get('care', ''),
                'revision': row['revision'] or meta.get('revision', ''),
                'signer': row['signer'] or meta.get('signer', ''),
                'declared_effective_at': row['declared_effective_at'],
                'effective_at': row['effective_at'],
                'effective_basis': row['effective_basis'],
                'revision_of': row['revision_of'] or meta.get('revision-of', ''),
                'url': row['url'] or meta.get('url', ''),
                'events': [],
                'fetched_at': row['fetched_at'],
                'withdrawn_by': [],
            }
        group['events'].append(row['event_id'])
        group['fetched_at'] = min(group['fetched_at'], row['fetched_at'])
    return list(groups.values())


def _withdrawals(db, sku):
    return [dict(row) for row in db.execute(
        'SELECT * FROM label_withdrawal WHERE sku=? ORDER BY effective_at,fetched_at,event_id', (sku,))]


def _withdrawal_hits(withdrawal, publication):
    # A withdrawal may only remove a publication published by the same source.
    if withdrawal['source'] != publication['source']:
        return False
    if withdrawal['target_source'] and withdrawal['target_source'] != publication['source']:
        return False
    identifiers = (
        withdrawal['target_event'] in publication['events'],
        bool(withdrawal['target_content_sha']) and withdrawal['target_content_sha'] == publication['content_sha'],
        bool(withdrawal['target_revision']) and withdrawal['target_revision'] == publication['revision'],
    )
    return any(identifiers)


def _source_chain(publications, withdrawals, result):
    chain = []
    for publication in sorted(publications, key=lambda p: (p['fetched_at'], p['page_sha'])):
        # One publication may have been fetched more than once.  It stays one
        # evidence item, with every fetch event listed under it.
        chain.append({
            'role': 'page_fetch',
            'event_id': _first_event(publication),
            'fetch_events': sorted(publication['events']),
            'source': publication['source'],
            'fetched_at': publication['fetched_at'],
            'declared_effective_at': publication['declared_effective_at'],
            'effective_at': publication['effective_at'],
            'effective_basis': publication['effective_basis'],
            'revision': publication['revision'],
            'signer': publication['signer'],
            'content_sha': publication['content_sha'],
            'page_sha': publication['page_sha'],
            'url': publication['url'],
            'withdrawn_by': sorted(publication['withdrawn_by']),
        })
    for withdrawal in withdrawals:
        chain.append({
            'role': 'withdrawal',
            'event_id': withdrawal['event_id'],
            'source': withdrawal['source'],
            'fetched_at': withdrawal['fetched_at'],
            'effective_at': withdrawal['effective_at'],
            'effective_basis': withdrawal['effective_basis'],
            'revision': withdrawal['revision'],
            'signer': withdrawal['signer'],
            'target_event': withdrawal['target_event'],
            'target_source': withdrawal['target_source'],
            'target_revision': withdrawal['target_revision'],
        })
    chain.append({
        'role': 'adoption',
        'event_id': result.get('adopt_event'),
        'state': result['state'],
        'careness': result['careness'],
        'as_of': result['as_of'],
        'superseded_events': result.get('superseded_events', []),
    })
    return chain


def _adjudicate(db, sku, as_of):
    publications = _publications(db, sku)
    withdrawals = [w for w in _withdrawals(db, sku) if w['effective_at'] <= as_of]

    for publication in publications:
        for withdrawal in withdrawals:
            if _withdrawal_hits(withdrawal, publication) and withdrawal['event_id'] not in publication['withdrawn_by']:
                publication['withdrawn_by'].append(withdrawal['event_id'])

    live = [p for p in publications if not p['withdrawn_by']]

    # A withdrawal of one release of a source retires earlier releases of that
    # same source too: falling back to an older cached copy would pretend the
    # withdrawn revision is still in force.  Other sources are untouched.
    superseded = []
    for publication in live:
        if not publication['revision']:
            continue
        for other in publications:
            if other is publication or other['source'] != publication['source']:
                continue
            if not other['withdrawn_by']:
                continue
            if not other['revision']:
                continue
            if _revision_key(other['revision']) > _revision_key(publication['revision']):
                superseded.append((publication, other))
                break

    superseded_events = sorted(event for publication, _other in superseded for event in publication['events'])
    superseded_keys = {(publication['source'], publication['page_sha']) for publication, _other in superseded}
    if superseded:
        live = [p for p in live if (p['source'], p['page_sha']) not in superseded_keys]

    confirmed = [
        p for p in live
        if p['effective_basis'] == 'declared' and p['signer'] and p['effective_at'] and p['effective_at'] <= as_of
    ]
    confirmed_pages = {p['page_sha'] for p in confirmed}
    pending = [
        p for p in live
        if p['page_sha'] not in confirmed_pages
        and (p['effective_at'] is None or p['effective_at'] > as_of or not p['signer'])
    ]

    pending_events = sorted(_first_event(p) for p in pending)
    withdrawn_events = sorted(event for p in publications if p['withdrawn_by'] for event in p['events'])
    superseded_events = sorted(set(superseded_events) - set(withdrawn_events))
    conflict_events = []
    adopt = None
    state = 'empty'
    reason = 'no care page collected'
    careness = 'empty'
    effective_at = None
    effective_basis = 'missing'

    if confirmed:
        representatives = {}
        source_conflict = []
        for publication in confirmed:
            key = (publication['effective_at'], _revision_key(publication['revision']))
            current = representatives.get(publication['source'])
            if current is None or key > current[0]:
                representatives[publication['source']] = (key, [publication])
            elif key == current[0]:
                current[1].append(publication)
        chosen = []
        for source in sorted(representatives):
            _key, tops = representatives[source]
            if len({(p['material'], p['care']) for p in tops}) > 1:
                source_conflict.extend(tops)
            chosen.append(sorted(tops, key=lambda p: (p['effective_at'], p['page_sha']))[-1])

        if source_conflict:
            conflict_events = sorted(_first_event(p) for p in source_conflict)
            state, reason, careness = 'conflict', 'same source published contradictory effective instructions', 'conflict'
        else:
            values = {(p['material'], p['care']) for p in chosen}
            if len(values) > 1:
                conflict_events = sorted(_first_event(p) for p in chosen)
                state, reason, careness = 'conflict', 'effective sources publish contradictory care instructions', 'conflict'
            else:
                adopt = sorted(chosen, key=lambda p: (p['effective_at'], p['source'], p['page_sha']))[-1]
                state, reason, careness = 'verified', 'effective instruction confirmed by source signature', 'verified'
                effective_at = adopt['effective_at']
                effective_basis = adopt['effective_basis']
    elif pending:
        provisional_values = {(p['material'], p['care']) for p in pending}
        if len(provisional_values) > 1:
            conflict_events = sorted(_first_event(p) for p in pending)
            reason = 'pages without a confirmed effective time disagree'
        else:
            candidate = sorted(pending, key=lambda p: (p['fetched_at'], p['page_sha']))[-1]
            adopt = candidate
            reasons = set()
            for publication in pending:
                if publication['effective_basis'] != 'declared':
                    reasons.add('no declared effective time')
                elif not publication['signer']:
                    reasons.add('declared effective page lacks a source signature')
                elif publication['effective_at'] > as_of:
                    reasons.add('declared effective time is after the business as-of time')
            reason = 'pending material: ' + ', '.join(sorted(reasons))
        state, careness = 'pending', 'pending'
    elif superseded:
        state, reason, careness = 'withdrawn', (
            'newer release withdrawn; earlier releases of the same source are superseded and '
            'cannot be reused as current'), 'withdrawn'
    elif publications:
        state, reason, careness = 'withdrawn', 'all collected publications were explicitly withdrawn', 'withdrawn'

    result = {
        'sku': sku,
        'state': state,
        'reason': reason,
        'careness': careness,
        'adopt_event': _first_event(adopt) if adopt else None,
        'adopt_source': adopt['source'] if adopt else '',
        'adopt_material': adopt['material'] if adopt else '',
        'adopt_care': adopt['care'] if adopt else '',
        'revision': adopt['revision'] if adopt else '',
        'signer': adopt['signer'] if adopt else '',
        'content_sha': adopt['content_sha'] if adopt else '',
        'page_sha': adopt['page_sha'] if adopt else '',
        'effective_at': effective_at if adopt and careness == 'verified' else (adopt['effective_at'] if adopt else None),
        'effective_basis': effective_basis if adopt and careness == 'verified' else (adopt['effective_basis'] if adopt else 'missing'),
        'as_of': as_of,
        'candidates': sorted(_first_event(p) for p in confirmed),
        'pending_events': pending_events,
        'conflict_events': conflict_events,
        'withdrawn_events': withdrawn_events,
        'superseded_events': superseded_events,
        'source_chain': [],
    }
    result['source_chain'] = _source_chain(publications, withdrawals, result)
    return result


def _first_event(publication):
    return sorted(publication['events'])[0]


RESULT_COLUMNS = (
    'sku', 'source', 'material', 'care', 'source_event', 'declared_effective_at',
    'effective_at', 'effective_basis', 'content_sha', 'page_sha', 'revision', 'signer',
    'state', 'reason', 'careness', 'as_of', 'candidates', 'conflict_events',
    'pending_events', 'withdrawn_events', 'source_chain', 'version',
)


def _entry_values(result, version):
    return {
        'sku': result['sku'],
        'source': result['adopt_source'],
        'material': result['adopt_material'],
        'care': result['adopt_care'],
        'source_event': result['adopt_event'] or '',
        'declared_effective_at': result['effective_at'],
        'effective_at': result['effective_at'],
        'effective_basis': result['effective_basis'],
        'content_sha': result['content_sha'],
        'page_sha': result['page_sha'],
        'revision': result['revision'],
        'signer': result['signer'],
        'state': result['state'],
        'reason': result['reason'],
        'careness': result['careness'],
        'as_of': result['as_of'],
        'candidates': canonical_json(result['candidates']),
        'conflict_events': canonical_json(result['conflict_events']),
        'pending_events': canonical_json(result['pending_events']),
        'withdrawn_events': canonical_json(result['withdrawn_events']),
        'source_chain': canonical_json(result['source_chain']),
        'version': version,
    }


def _persist(db, result):
    previous = db.execute('SELECT * FROM label_entry WHERE sku=?', (result['sku'],)).fetchone()
    old_version = previous['version'] if previous else 0
    values = _entry_values(result, old_version + 1)
    comparable = {key: value for key, value in values.items() if key != 'version'}
    if previous and all(previous[key] == value for key, value in comparable.items()):
        return dict(previous)
    db.execute(
        f"INSERT INTO label_entry({','.join(RESULT_COLUMNS)}) VALUES({','.join('?' for _ in RESULT_COLUMNS)}) "
        f"ON CONFLICT(sku) DO UPDATE SET " + ','.join(
            f'{column}=excluded.{column}' for column in RESULT_COLUMNS if column != 'sku'),
        tuple(values[column] for column in RESULT_COLUMNS),
    )

    latest = db.execute('SELECT version FROM label_revision WHERE sku=? ORDER BY version DESC LIMIT 1',
                        (result['sku'],)).fetchone()
    same = latest is not None and db.execute(
        'SELECT 1 FROM label_revision WHERE sku=? AND at_as_of=? AND state=? AND reason=? AND careness=? '
        'AND adopt_event IS ? AND candidate_events=? AND conflict_count=? AND pending_count=? '
        'AND withdrawn_events=? AND source_chain=?',
        (result['sku'], result['as_of'], result['state'], result['reason'], result['careness'],
         result['adopt_event'], canonical_json(result['candidates']), len(result['conflict_events']),
         len(result['pending_events']), canonical_json(result['withdrawn_events']),
         canonical_json(result['source_chain']))).fetchone()
    if not same:
        version = (latest['version'] + 1) if latest else 1
        cursor = db.execute(
            'INSERT INTO label_revision(sku,version,at_as_of,state,reason,careness,adopt_event,adopt_source,'
            'adopt_material,adopt_care,effective_at,conflict_count,pending_count,candidate_events,'
            'pending_events,rejected_events,withdrawn_events,source_chain) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (result['sku'], version, result['as_of'], result['state'], result['reason'], result['careness'],
             result['adopt_event'], result['adopt_source'], result['adopt_material'], result['adopt_care'],
             result['effective_at'], len(result['conflict_events']), len(result['pending_events']),
             canonical_json(result['candidates']), canonical_json(result['pending_events']), '[]',
             canonical_json(result['withdrawn_events']), canonical_json(result['source_chain'])),
        )
        db.execute('INSERT INTO catalog_source_chain(sku,history_id,state,adopt_event,as_of) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(sku) DO UPDATE SET history_id=excluded.history_id,state=excluded.state,'
                   'adopt_event=excluded.adopt_event,as_of=excluded.as_of',
                   (result['sku'], cursor.lastrowid, result['state'], result['adopt_event'], result['as_of']))
    return dict(db.execute('SELECT * FROM label_entry WHERE sku=?', (result['sku'],)).fetchone())


def _decode(row):
    if row is None:
        return None
    item = dict(row)
    for key in ('candidates', 'conflict_events', 'pending_events', 'withdrawn_events', 'source_chain'):
        if key in item:
            item[key] = json.loads(item[key])
    item['superseded_events'] = _superseded_from_chain(item.get('source_chain') or [])
    return item


def _superseded_from_chain(chain):
    for link in chain:
        if link.get('role') == 'adoption':
            return link.get('superseded_events', [])
    return []


def entry(db, sku):
    return _decode(db.execute('SELECT * FROM label_entry WHERE sku=?', (sku,)).fetchone())


def entry_as_of(db, sku, as_of):
    """Read a SKU's authoritative instruction at a business instant.

    ``label_entry`` stores the latest adjudication.  When a caller asks about a
    different time, the same stored publications and withdrawals are re-run at
    that instant; nothing is persisted and fetched_at never stands in for the
    decision time.
    """
    store = entry(db, sku)
    target = as_of
    if target is None:
        target = store['as_of'] if store else ''
    result = _adjudicate(db, sku, target)
    result.update({
        'source': result['adopt_source'],
        'source_event': result['adopt_event'],
        'material': result['adopt_material'],
        'care': result['adopt_care'],
        'declared_effective_at': result['effective_at'],
        'version': store['version'] if store else 1,
    })
    return result


def adjudicate_as_of(db, sku, as_of):
    """Recompute the SKU verdict at a business time without persisting it.

    Fetch timestamps and withdrawal arrival times are not decision times; the
    caller supplies the instant they actually care about.
    """
    return entry_as_of(db, sku, as_of)


def revisions(db, sku=None):
    if sku is None:
        rows = db.execute('SELECT * FROM label_revision ORDER BY sku,version').fetchall()
    else:
        rows = db.execute('SELECT * FROM label_revision WHERE sku=? ORDER BY version', (sku,)).fetchall()
    return [_decode(row) for row in rows]


def physical_labels(db, garment_id, sku):
    rows = db.execute(
        'SELECT * FROM label_label_event WHERE garment_id=? AND sku=? ORDER BY signed_at DESC,event_key',
        (garment_id, sku)).fetchall()
    return [dict(row) for row in rows]


def _latest_physical(db, garment, sku, as_of):
    """Newest independently signed physical tag in force at ``as_of``.

    Tags signed on the same day are ordered by their label code then event key,
    never by which row was inserted first.
    """
    candidates = [
        row for row in physical_labels(db, garment['id'], sku)
        if row['source_kind'] == 'physical_label' and row['signed_at'] <= as_of and row['signer']
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda row: (row['signed_at'], row['label_code'], row['event_key']))[-1]


def _distinct_tag_values(db, garment, sku, as_of):
    rows = [
        row for row in physical_labels(db, garment['id'], sku)
        if row['source_kind'] == 'physical_label' and row['signed_at'] <= as_of and row['signer']
    ]
    return {(row['material'], row['care']) for row in rows}


def _write_review(db, garment, fields):
    previous = db.execute('SELECT * FROM garment_label_review WHERE garment_id=?', (garment['id'],)).fetchone()
    version = (previous['version'] + 1) if previous else 1
    comparable = ('sku', 'route', 'label_material', 'label_care', 'label_state', 'label_event',
                  'label_revision', 'checked_as_of', 'task_state', 'reason', 'impact', 'conflict')
    if previous and all(previous[key] == fields[key] for key in comparable):
        return dict(previous)
    names = ('garment_id', *comparable, 'version')
    values = (garment['id'], *(fields[key] for key in comparable), version)
    db.execute(
        f"INSERT INTO garment_label_review({','.join(names)}) VALUES({','.join('?' for _ in names)}) "
        f"ON CONFLICT(garment_id) DO UPDATE SET " + ','.join(
            f'{name}=excluded.{name}' for name in (*comparable, 'version')),
        values,
    )
    db.execute(
        'INSERT INTO garment_label_review_history(garment_id,version,task_state,reason,route,label_state,'
        'label_care,label_event,impact,conflict) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (garment['id'], version, fields['task_state'], fields['reason'], fields['route'],
         fields['label_state'], fields['label_care'], fields['label_event'], fields['impact'],
         fields['conflict']),
    )
    return dict(db.execute('SELECT * FROM garment_label_review WHERE garment_id=?', (garment['id'],)).fetchone())


def garment_label(db, garment, as_of=None):
    """Adjudicate the care authority selected for one garment.

    ``manufacturer`` uses the SKU catalogue's effective page.  ``physical_label``
    uses the independently signed store tag for this garment.  The choice is an
    explicit customer authorization (``garment.label_route``); it is never
    inferred from event ids or the largest timestamp.
    """
    sku = garment['sku'] if 'sku' in garment.keys() else ''
    route = (garment['label_route'] if 'label_route' in garment.keys() else '') or 'manufacturer'
    if not sku:
        return _write_review(db, garment, {
            'sku': '', 'route': route, 'label_material': None, 'label_care': None,
            'label_state': 'not_tracked', 'label_event': None, 'label_revision': None,
            'checked_as_of': as_of or garment['accepted_at'], 'task_state': 'none',
            'reason': 'no catalogue SKU linked to garment', 'impact': 'continue', 'conflict': 0,
        })

    catalogue = entry_as_of(db, sku, as_of) if as_of else entry(db, sku)
    checked_as_of = as_of or max(garment['accepted_at'], (catalogue or {}).get('as_of') or '')
    physical = _latest_physical(db, garment, sku, checked_as_of)
    unsigned_tag = None
    if physical is None:
        unsigned_tag = next((row for row in physical_labels(db, garment['id'], sku)
                             if row['source_kind'] == 'physical_label' and row['signed_at'] <= checked_as_of
                             and not row['signer']), None)
    physical_valid = bool(physical) and physical['signer'] != garment['owner']
    tag_values = _distinct_tag_values(db, garment, sku, checked_as_of)

    manufacturer_verified = catalogue is not None and catalogue['careness'] == 'verified'
    manufacturer_values = ((catalogue['material'], catalogue['care']) if manufacturer_verified else None)
    physical_values = ((physical['material'], physical['care']) if physical_valid else None)

    event = None
    revision = catalogue['revision'] if catalogue else None
    if route == 'physical_label':
        event = physical['event_key'] if physical else None
        material = physical['material'] if physical_valid else None
        care = physical['care'] if physical_valid else None
        base = {
            'sku': sku, 'route': route, 'label_material': material, 'label_care': care,
            'label_event': event, 'label_revision': revision, 'checked_as_of': checked_as_of,
            'conflict': 0,
        }
        if not physical and unsigned_tag:
            base.update(label_state='unverified', task_state='review', impact='review_required',
                        reason='physical tag exists but lacks an independent signer and cannot establish authority')
        elif not physical:
            base.update(label_state='pending', task_state='pending_evidence', impact='review_required',
                        reason='chosen physical-label authority has no signed tag for this garment')
        elif catalogue and catalogue['state'] == 'withdrawn':
            base.update(label_state='withdrawn', task_state='re_adjudicate', impact='blocked_new_processing',
                        conflict=1,
                        reason=('manufacturer publication withdrawn and the physical tag chose a different '
                                'care direction; re-sign the care process before new processing'))
        elif len(tag_values) > 1:
            base.update(label_state='conflict', task_state='conflict_pending', impact='blocked_new_processing',
                        conflict=1,
                        reason='independently signed physical tags disagree with each other; no tag is selected')
        elif not physical_valid:
            base.update(label_state='unverified', task_state='review', impact='review_required',
                        reason='physical tag lacks an independent signer and cannot establish authority')
        elif manufacturer_values and physical_values != manufacturer_values:
            base.update(label_state='conflict', task_state='conflict_pending', impact='blocked_new_processing',
                        conflict=1,
                        reason=(f"signed physical tag says {physical['material']}/{physical['care']} but the "
                                f"effective manufacturer page says {manufacturer_values[0]}/{manufacturer_values[1]}; "
                                'stop new processing and re-sign the care process'))
        elif physical['material'] != garment['material']:
            base.update(label_state='conflict', task_state='conflict_pending', impact='frozen_intake',
                        conflict=1,
                        reason=(f"physical tag material {physical['material']} conflicts with the signed "
                                f"intake material {garment['material']}; signed intake evidence is not rewritten"))
        elif physical['care'] != garment['allowed_process']:
            base.update(label_state='conflict', task_state='resign_process', impact='blocked_new_processing',
                        conflict=1,
                        reason=(f"physical tag care {physical['care']} conflicts with the owner-authorized "
                                f"process {garment['allowed_process']}; pause and re-sign the process"))
        else:
            base.update(label_state='verified', task_state='none', impact='continue',
                        reason='physical tag matches the owner-authorized care process')
        return _write_review(db, garment, base)

    if catalogue is None or catalogue['state'] == 'empty':
        return _write_review(db, garment, {
            'sku': sku, 'route': route, 'label_material': None, 'label_care': None,
            'label_state': 'pending', 'label_event': None, 'label_revision': None,
            'checked_as_of': checked_as_of, 'task_state': 'pending_evidence', 'impact': 'review_required',
            'reason': 'no care page collected for the SKU', 'conflict': 0,
        })
    if catalogue['state'] == 'conflict':
        return _write_review(db, garment, {
            'sku': sku, 'route': route, 'label_material': None, 'label_care': None,
            'label_state': 'conflict', 'label_event': None, 'label_revision': None,
            'checked_as_of': checked_as_of, 'task_state': 'conflict_pending',
            'impact': 'blocked_new_processing', 'conflict': 1,
            'reason': 'effective sources disagree; no instruction is adopted for the chosen authority',
        })
    if catalogue['state'] == 'pending':
        return _write_review(db, garment, {
            'sku': sku, 'route': route, 'label_material': catalogue['material'] or None,
            'label_care': catalogue['care'] or None, 'label_state': 'pending',
            'label_event': catalogue['source_event'] or None, 'label_revision': catalogue['revision'] or None,
            'checked_as_of': checked_as_of, 'task_state': 'pending_evidence', 'impact': 'review_required',
            'reason': 'only unconfirmed pages exist; they cannot replace effective signed instructions',
            'conflict': 0,
        })
    if catalogue['state'] == 'withdrawn':
        return _write_review(db, garment, {
            'sku': sku, 'route': route, 'label_material': None, 'label_care': None,
            'label_state': 'withdrawn', 'label_event': None, 'label_revision': None,
            'checked_as_of': checked_as_of, 'task_state': 're_adjudicate', 'impact': 'blocked_new_processing',
            'reason': ('the referenced manufacturer publication was withdrawn; the care basis must be '
                       're-adjudicated before any new processing'),
            'conflict': 1,
        })

    material = catalogue['material']
    care = catalogue['care']
    tag_conflict = len(tag_values) > 1
    physical_conflict = bool(physical_values) and physical_values != (material, care)
    base = {
        'sku': sku, 'route': route, 'label_material': material, 'label_care': care,
        'label_event': catalogue['source_event'] or None, 'label_revision': catalogue['revision'] or None,
        'checked_as_of': checked_as_of, 'conflict': 1 if tag_conflict or physical_conflict else 0,
    }
    if material != garment['material']:
        base.update(label_state='conflict', task_state='conflict_pending', impact='frozen_intake',
                    reason=(f"effective page material {material} conflicts with signed intake material "
                            f"{garment['material']}; signed intake evidence is not rewritten"))
    elif care != garment['allowed_process']:
        base.update(label_state='conflict', task_state='resign_process', impact='blocked_new_processing',
                    reason=(f"effective page care {care} conflicts with the owner-authorized process "
                            f"{garment['allowed_process']}; do not override the signed consent"))
    elif tag_conflict:
        base.update(label_state='conflict', task_state='conflict_pending', impact='blocked_new_processing',
                    reason='signed physical tags disagree with each other; the manufacturer authority is noted '
                           'but the tag dispute needs review before new processing')
    elif physical_conflict:
        base.update(label_state='verified', task_state='noted', impact='continue',
                    reason=('manufacturer authority confirmed; signed physical tag disagrees and is recorded '
                            'as a review note, not silently selected'))
    else:
        base.update(label_state='verified', task_state='none', impact='continue',
                    reason='effective manufacturer instruction matches the owner-authorized process')
    return _write_review(db, garment, base)


def _affected_garments(db, skus):
    if not skus:
        return []
    marks = ','.join('?' for _ in skus)
    return [row['id'] for row in db.execute(
        f'SELECT id FROM garment WHERE sku IN ({marks}) ORDER BY id', tuple(sorted(skus)))]


def import_data(db, data):
    touched = set()
    as_of = data.get('as_of')
    times = []
    payload = len(data.get('pages', [])) + len(data.get('withdrawals', [])) + len(data.get('labels', []))
    with db:
        for row in data.get('pages', []):
            page = _compile_page(row)
            if record(db, row['event_id'], 'label_page', row):
                _store_page(db, page)
            if page['withdraws']:
                withdrawal = _compile_withdrawal({
                    'event_id': row['event_id'] + '#withdraw', 'source': row['source'], 'sku': row['sku'],
                    'fetched_at': row['fetched_at'], 'declared_effective_at': page['declared_effective_at'],
                    'target_event': page['withdraws'], 'signer': page['signer'],
                    'reason': 'withdrawal declared in page',
                })
                if record(db, withdrawal['event_id'], 'label_withdrawal', withdrawal):
                    _store_withdrawal(db, withdrawal)
            times.append(row['fetched_at'])
            touched.add(row['sku'])
        for row in data.get('withdrawals', []):
            withdrawal = _compile_withdrawal(row)
            if record(db, row['event_id'], 'label_withdrawal', row):
                _store_withdrawal(db, withdrawal)
            times.append(row['fetched_at'])
            touched.add(row['sku'])
        for row in data.get('labels', []):
            if record(db, row['event_id'], 'physical_label', row):
                db.execute(
                    'INSERT INTO label_label_event(event_key,garment_id,sku,source,source_kind,label_code,material,'
                    'care,signer,signed_at,effective_at,source_event) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row['event_id'], row.get('garment_id', ''), row['sku'], row['source'],
                     row.get('source_kind', 'physical_label'), row.get('label_code', ''), row['material'],
                     row['care'], row.get('signer', ''), row['signed_at'], row['signed_at'], row.get('source_event')),
                )
            times.append(row['signed_at'])
            touched.add(row['sku'])
        if as_of is None:
            existing = [row[0] for row in db.execute(
                'SELECT MAX(fetched_at) FROM label_page UNION ALL SELECT MAX(fetched_at) FROM label_withdrawal')]
            times.extend(time for time in existing if time)
            if not times and not payload:
                as_of = ''
            else:
                as_of = max(times) if times else ''
        for sku in sorted(touched):
            _persist(db, _adjudicate(db, sku, as_of))
        affected = _affected_garments(db, touched)
        if affected:
            from . import intake
            for garment_id in affected:
                intake.decide(db, garment_id)
    return [entry(db, sku) for sku in sorted(touched)]
