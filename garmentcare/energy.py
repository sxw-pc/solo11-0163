"""Physical-topology energy difference investigation.

Per site/period the result is a decomposition, never a single "usage" number:

    main delta -> branch coverage aligned on real cut-off times -> adjusted branch use
               -> common load actually under the top and outside every branch
               -> explained shop use -> unexplained remainder -> responsibility
               -> affected meters/branches/segments -> work orders

Raw readings are preserved.  Clock corrections, supplier calibration and branch
moves change attribution and trusted intervals only; a late topology change
cannot rewrite a number already collected.  An unexplained difference is never
booked as garment-process energy or as theft.
"""

import json
from datetime import datetime

from .utility import corrected_at, read_at_of

REPORTED = 'reported'
MOVED = 'moved'
REVERSED = 'reversed'
CONFLICT = 'certificate-conflict'


def _dt(value):
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _j(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _meters(db, site):
    return {row['meter_id']: dict(row) for row in db.execute('SELECT * FROM meter WHERE site=?', (site,))}


def _top_meter(db, site, meters):
    tops = sorted(m['meter_id'] for m in meters.values() if m['role'] == 'top')
    if tops:
        return tops[0]
    parents = db.execute('SELECT parent_meter FROM meter_topology WHERE site=? AND parent_meter IS NOT NULL '
                         'ORDER BY effective_at,event_id', (site,)).fetchall()
    if parents:
        return parents[-1]['parent_meter']
    on_site = sorted({r['meter_id'] for r in db.execute(
        'SELECT DISTINCT meter_id FROM meter_reading WHERE site=?', (site,))})
    # A site with one register has no branch topology to decompose; that
    # register is its own top.  With several unclassified registers there is no
    # proven top, so the site id is kept as a neutral placeholder.
    return on_site[0] if len(on_site) == 1 else site


def _topology_at(db, site, meter_id, cut_at):
    """Latest topology row effective at cut_at; a reversed row cancels it.

    filed_at is deliberately ignored: a late filing still applies from its own
    effective_at, so only attribution is amended, never a collected reading.
    """
    rows = db.execute('SELECT * FROM meter_topology WHERE site=? AND meter_id=? AND effective_at<=? '
                      'ORDER BY effective_at,event_id', (site, meter_id, cut_at)).fetchall()
    active = None
    for row in rows:
        active = None if row['reversed'] else row
    return dict(active) if active else None


def _period_readings(db, site, period):
    grouped = {}
    for row in db.execute('SELECT * FROM meter_reading WHERE site=? AND period=? ORDER BY meter_id,event_id',
                          (site, period)).fetchall():
        grouped.setdefault(row['meter_id'], []).append(dict(row))
    return grouped


def _cut(row):
    return datetime.fromisoformat(corrected_at(read_at_of(row['period'], row['read_at']),
                                               row['clock_offset_seconds']))


def _previous_within(db, site, meter_id, period):
    """Previous raw reading of the same meter strictly before the named period."""
    row = db.execute('SELECT * FROM meter_reading WHERE site=? AND meter_id=? AND period<? '
                     'ORDER BY period DESC,event_id DESC', (site, meter_id, period)).fetchone()
    return dict(row) if row else None


def _aligned(db, site, period, top):
    """One latest raw reading per meter and the real interval it covers.

    Both ends of the interval matter: a branch read on a different start date or
    a different cut-off cannot be differenced against the main meter window
    unless its clock offset is proven.
    """
    grouped = _period_readings(db, site, period)
    chosen = {}
    for meter_id, rows in grouped.items():
        row = max(rows, key=lambda r: (_cut(r), r['event_id']))
        previous = _previous_within(db, site, meter_id, period)
        chosen[meter_id] = {
            'row': row, 'cut': _cut(row), 'clock_verified': bool(row['clock_verified']),
            'start': _cut(previous) if previous else None, 'previous': previous,
        }
    if not chosen:
        return {}, None, []
    anchor_end = chosen[top]['cut'] if top in chosen else max(v['cut'] for v in chosen.values())
    anchor_start = chosen[top]['start'] if top in chosen else None
    misaligned = []
    for meter_id, value in sorted(chosen.items()):
        if abs((value['cut'] - anchor_end).total_seconds()) > 300:
            misaligned.append({'meter_id': meter_id, 'edge': 'end',
                               'actual_cut': value['cut'].isoformat(),
                               'main_cut': anchor_end.isoformat(),
                               'clock_verified': value['clock_verified'],
                               'note': 'branch reading does not cover the main meter interval'})
        if anchor_start and value['start'] and abs((value['start'] - anchor_start).total_seconds()) > 300:
            misaligned.append({'meter_id': meter_id, 'edge': 'start',
                               'actual_cut': value['start'].isoformat(),
                               'main_cut': anchor_start.isoformat(),
                               'clock_verified': value['clock_verified'],
                               'note': 'branch interval starts on a different read date'})
    return chosen, anchor_end, misaligned


def _calibration(db, site, meter_id, period, cut_at):
    """Double-anchored, supplier-issued certificates for this meter/period."""
    rows = [dict(r) for r in db.execute(
        'SELECT * FROM calibration_cert WHERE site=? AND meter_id=? AND period=? ORDER BY valid_from,event_id',
        (site, meter_id, period)).fetchall()]
    signed = [r for r in rows if r['issuer_role'] in ('supplier', 'calibration-lab') and r['signer']
              and r['valid_from'] <= cut_at and r['valid_to'][:10] >= cut_at[:10]]
    if not signed:
        return None
    if len({r['correction_units'] for r in signed}) > 1:
        return {'conflict': True, 'rows': signed}
    return {'conflict': False, 'correction': sum(r['correction_units'] for r in signed),
            'uncertainty': max(r['uncertainty_units'] for r in signed), 'rows': signed}


def _attribute(db, site, meters, cut):
    result = {}
    for meter_id, meter in meters.items():
        row = _topology_at(db, site, meter_id, cut)
        result[meter_id] = {
            'branch_id': row['branch_id'] if row else meter['branch_id'],
            'parent_meter': row.get('parent_meter') if row else meter['parent_meter'],
            'segment': row.get('segment') if row else None,
            'event_id': row['event_id'] if row else None,
        }
    return result


def _deltas(db, site, period, chosen, meters, attribution, top):
    out, flags = {}, []
    for meter_id, value in chosen.items():
        if meter_id == top:
            continue
        meter = meters.get(meter_id)
        previous = _previous_within(db, site, meter_id, period)
        if previous is None:
            flags.append({'meter_id': meter_id, 'flag': 'no-baseline',
                          'segment': attribution[meter_id]['segment'], 'source_event': value['row']['event_id'],
                          'detail': 'branch register has no earlier reading; interval not covered'})
            out[meter_id] = {'branch_id': attribution[meter_id]['branch_id'], 'raw': 0, 'adjusted': 0,
                             'low': 0, 'high': 0, 'coverage': 'no-baseline',
                             'segment': attribution[meter_id]['segment'],
                             'reading': value['row'], 'previous': None}
            continue
        raw = int(value['row']['reading']) - int(previous['reading'])
        branch_id = attribution[meter_id]['branch_id']
        segment = attribution[meter_id]['segment']
        prior_branch = _topology_at(db, site, meter_id, previous['read_at']) if previous['read_at'] else None
        reading_branch = prior_branch['branch_id'] if prior_branch else None
        if reading_branch and reading_branch != branch_id:
            # Number stays with the branch under which it was collected.
            branch_id = prior_branch['branch_id']
            segment = prior_branch.get('segment') or segment
            flags.append({'meter_id': meter_id, 'flag': MOVED, 'segment': segment,
                          'source_event': prior_branch['event_id'],
                          'detail': f'reading retained on {branch_id}; meter currently on '
                                    f'{attribution[meter_id]["branch_id"]}'})
        base = {'branch_id': branch_id, 'raw': raw, 'segment': segment,
                'reading': value['row'], 'previous': previous}
        if (meter and meter['reversed']) or raw < 0:
            flags.append({'meter_id': meter_id, 'flag': REVERSED, 'segment': segment,
                          'source_event': (meter or {}).get('meter_id'),
                          'detail': 'register reversed; raw reading kept, interval untrusted'})
            out[meter_id] = {**base, 'adjusted': 0, 'low': 0, 'high': abs(raw), 'coverage': REVERSED}
            continue
        cal = _calibration(db, site, meter_id, period, value['cut'].isoformat())
        adjusted, low, high, coverage = raw, raw, raw, REPORTED
        if cal and cal['conflict']:
            coverage = CONFLICT
            low = raw - cal['rows'][0]['uncertainty_units']
            high = raw + max(r['correction_units'] for r in cal['rows']) + cal['rows'][0]['uncertainty_units']
            flags.append({'meter_id': meter_id, 'flag': CONFLICT, 'segment': segment,
                          'source_event': ','.join(r['event_id'] for r in cal['rows']),
                          'detail': 'supplier certificates disagree; raw reading preserved'})
        elif cal:
            adjusted = raw + cal['correction']
            low = adjusted - cal['uncertainty']
            high = adjusted + cal['uncertainty']
            coverage = 'calibrated'
        out[meter_id] = {**base, 'adjusted': adjusted, 'low': low, 'high': high,
                         'coverage': coverage, 'calibration': cal}
    return out, flags


def _accepted_common(db, site, period, top, branch_ids):
    """Only the common load wired under the top and outside every branch."""
    accepted = {}
    for row in db.execute('SELECT * FROM common_load WHERE site=? AND period=? AND valid=1 '
                          'ORDER BY load_id,event_id', (site, period)).fetchall():
        row = dict(row)
        if row['metered_by'] == top:
            continue
        if row['metered_by']:
            continue  # already inside a submeter reading; never double count
        if row['connected_meter'] not in (None, top):
            continue  # wired under a branch: belongs to that branch
        net = int(row['units']) - int(row['branch_metered_units'])
        if net <= 0:
            continue  # no part of it is outside the branches
        accepted[row['load_id']] = {'load_id': row['load_id'], 'units': net,
                                    'description': row['description'], 'source': row['source'],
                                    'evidence': [e for e in (row['evidence_id'],) if e],
                                    'route': 'common-load',
                                    'branch_share': int(row['branch_metered_units'])}
    return accepted


def _accepted_clamps(db, site, period, top, branch_ids):
    accepted = {}
    for row in db.execute('SELECT * FROM clamp_test WHERE site=? AND period=? AND valid=1 '
                          'ORDER BY load_id,event_id', (site, period)).fetchall():
        row = dict(row)
        if not (row['signer_a'] and row['signer_b'] and row['signer_a'] != row['signer_b']):
            continue  # needs two signatures
        if not row['wiring_diagram_id']:
            continue  # needs a signed wiring diagram
        if row['bypassed_meter'] and row['bypassed_meter'] in branch_ids:
            continue  # measured by a branch: not a missing branch
        if row['bypassed_meter'] and row['bypassed_meter'] != top:
            continue  # another top's segment: not this site's difference
        accepted[row['load_id']] = {'load_id': row['load_id'], 'units': int(row['units']),
                                    'description': row['description'], 'source': 'clamp_test',
                                    'evidence': [row['wiring_diagram_id']],
                                    'route': 'clamp-test', 'bypassed_meter': row['bypassed_meter']}
    return accepted


def _clamp_branch(db, site, wiring_diagram_id, fallback='missing'):
    row = db.execute('SELECT * FROM meter_topology WHERE site=? AND document_id=? AND action IN ("install","move") '
                     'ORDER BY effective_at DESC,event_id DESC', (site, wiring_diagram_id)).fetchone()
    return row['branch_id'] if row else fallback


def _conflict_segments(db, site, flags, common, deltas):
    """Segments a re-test must touch: every branch carrying a disputed reading."""
    segments = set()
    for flag in flags:
        if flag['flag'] == CONFLICT and flag['segment']:
            segments.add(flag['segment'])
        if flag['flag'] == CONFLICT:
            row = db.execute('SELECT branch_id FROM meter WHERE meter_id=?', (flag['meter_id'],)).fetchone()
            if row and row['branch_id']:
                segments.add(row['branch_id'])
    for meter_id, delta in deltas.items():
        if delta['coverage'] == 'calibrated':
            segments.add(delta['branch_id'])
    for item in common.values():
        if item['route'] in ('clamp-test', 'conflict'):
            found = False
            for evidence in item.get('evidence', []):
                row = db.execute('SELECT branch_id FROM meter_topology WHERE site=? AND document_id=? '
                                 'AND action IN ("install","move")', (site, evidence)).fetchone()
                if row:
                    segments.add(row['branch_id'])
                    found = True
            if not found:
                segments.add(f'missing-branch:{item["load_id"]}')
    return sorted(s for s in segments if s)


def _next_period(period):
    """Calendar successor of an account period, e.g. 2026-12 from 2026-11."""
    try:
        year, month = int(period[:4]), int(period[5:7])
    except (ValueError, TypeError):
        return None
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f'{year:04d}-{month:02d}'


def _periods(db, site):
    return [r['period'] for r in db.execute(
        'SELECT DISTINCT period FROM meter_reading WHERE site=? ORDER BY period', (site,)).fetchall()]


def investigate(db, site, period):
    meters = _meters(db, site)
    top = _top_meter(db, site, meters)
    if not _period_readings(db, site, period):
        return None
    main_current = main_previous = None
    main_units = 0
    if top in _period_readings(db, site, period):
        main_current = max(_period_readings(db, site, period)[top], key=lambda r: (_cut(r), r['event_id']))
        main_previous = _previous_within(db, site, top, period)
        if main_previous:
            main_units = int(main_current['reading']) - int(main_previous['reading'])

    chosen, main_cut, misaligned = _aligned(db, site, period, top)
    attribution = _attribute(db, site, meters, (main_cut or _dt(f'{period}-01')).isoformat())
    deltas, flags = _deltas(db, site, period, chosen, meters, attribution, top)
    if main_units < 0:
        flags.append({'meter_id': top, 'flag': REVERSED, 'segment': 'main',
                      'source_event': main_current['event_id'] if main_current else None,
                      'detail': 'main register decreased; raw readings kept, interval untrusted'})
    branch_ids = {d['branch_id'] for d in deltas.values() if d['branch_id']}
    branch_ids |= {m['branch_id'] for m in meters.values() if m['role'] == 'branch' and m['branch_id']}

    common = _accepted_common(db, site, period, top, branch_ids)
    clamps = _accepted_clamps(db, site, period, top, branch_ids)
    for load_id, item in clamps.items():
        if load_id in common and common[load_id]['units'] != item['units']:
            flags.append({'meter_id': top, 'flag': CONFLICT, 'segment': None,
                          'source_event': item['evidence'][0],
                          'detail': 'common load record and clamp test disagree'})
            common[load_id]['route'] = 'conflict'
        elif load_id in common:
            # Same physical load confirmed by a double-signed clamp test.
            common[load_id]['route'] = 'clamp-test'
            common[load_id]['evidence'] = sorted(set(common[load_id]['evidence'] + item['evidence']))
        else:
            common[load_id] = item

    reported = sum(d['raw'] for d in deltas.values())
    adjusted = sum(d['adjusted'] for d in deltas.values())
    # The trusted band is an interval, not a scalar sum, so calibration
    # uncertainty and reversed/untested registers widen it instead of hiding.
    low = sum(d['low'] for d in deltas.values())
    high = sum(d['high'] for d in deltas.values())
    common_units = sum(item['units'] for item in common.values())
    difference = main_units - reported
    calib_route = adjusted != reported
    clamp_route = any(item['route'] == 'clamp-test' for item in common.values())
    cert_conflict = any(flag['flag'] == CONFLICT for flag in flags)
    branch_registered = any(m['role'] == 'branch' for m in meters.values())
    single_meter = not branch_registered and not deltas
    # Each evidence route is tested on its own: a certificate explains the gap
    # against the raw branch sum; a clamp test explains it as a load under the
    # top.  Only when the two are additive may they be summed.
    calib_covers = calib_route and low <= main_units <= high
    clamp_covers = clamp_route and reported + common_units == main_units
    additive_covers = low + common_units <= main_units <= high + common_units
    competing = calib_covers and clamp_covers and not additive_covers
    explained = adjusted + common_units

    if single_meter:
        state = 'single-meter'
        responsibility = 'none'
        reason = 'one meter on site; its own difference is the site usage, no branch decomposition claimed'
        explained = main_units
        unexplained = 0
        trust_low = trust_high = main_units
        # No branch registers exist; report the site total, not a branch sum.
        adjusted = main_units
    elif cert_conflict:
        state = 'frozen'
        responsibility = 'disputed-calibration'
        reason = 'calibration certificates conflict; attribution frozen, raw readings kept'
        explained = reported
        unexplained = max(0, difference)
        trust_low, trust_high = low, high
    elif misaligned:
        state = 'needs_evidence'
        responsibility = 'meter-coverage'
        reason = 'branch readings do not cover the same real interval as the main meter'
        unexplained = max(0, main_units - explained)
        trust_low, trust_high = low + common_units, high + common_units
    elif any(flag['flag'] == 'no-baseline' for flag in flags):
        state = 'needs_evidence'
        responsibility = 'meter-coverage'
        reason = 'a branch register has no earlier reading; its interval is not covered'
        unexplained = max(0, main_units - explained)
        trust_low, trust_high = low + common_units, high + common_units
    elif competing:
        state = 'frozen'
        responsibility = 'disputed-attribution'
        reason = ('calibration and clamp evidence each claim the same difference; attribution frozen, '
                  'both evidence sets kept')
        explained = reported
        unexplained = max(0, difference)
        trust_low, trust_high = low, high
    elif calib_covers:
        state = 'explained'
        responsibility = 'meter:' + ','.join(sorted(meter_id for meter_id, d in deltas.items()
                                                    if d['coverage'] == 'calibrated'))
        reason = 'supplier calibration certificate accounts for the difference; responsible meter identified'
        explained = adjusted
        unexplained = 0
        trust_low, trust_high = low, high
    elif clamp_covers:
        state = 'explained'
        responsibility = ('missing-metering-branch:' + ','.join(sorted(common))
                          if clamp_route else 'common-load')
        reason = ('double-signed clamp test and wiring diagram prove a load on the top outside all branches'
                  if clamp_route else 'common load under the top explains the difference')
        explained = reported + common_units
        unexplained = 0
        trust_low, trust_high = low + common_units, high + common_units
    elif additive_covers:
        state = 'explained'
        responsibility = 'common-load' if common_units else 'none'
        reason = ('common load under the top plus branch readings explain the difference' if common_units
                  else 'difference fully explained by branch readings')
        explained = adjusted + common_units
        unexplained = 0
        trust_low, trust_high = low + common_units, high + common_units
    else:
        state = 'needs_evidence' if main_units < 0 else 'unexplained'
        responsibility = 'meter-coverage' if main_units < 0 else 'unattributed'
        reason = ('main register decreased; the interval cannot be closed until it is re-read'
                  if main_units < 0 else
                  'no physical evidence closes the remaining difference; neither process use nor theft is assumed')
        unexplained = max(0, abs(main_units) if main_units < 0 else main_units - explained)
        if main_units < 0:
            explained = 0  # an untrusted main books nothing as explained
        trust_low, trust_high = low + common_units, high + common_units

    next_period = {
        'frozen': 'hold-until-retest',
        'needs_evidence': 'repeat-with-aligned-cutoffs',
        'single-meter': 'normal',
    }.get(state)
    if state == 'needs_evidence' and any(flag['flag'] == 'no-baseline' for flag in flags):
        next_period = None
    if state == 'needs_evidence' and main_units < 0:
        next_period = None
    if next_period is None:
        if responsibility.startswith('missing-metering-branch'):
            next_period = 'install-branch-register'
        elif responsibility.startswith('meter:'):
            next_period = 'apply-certificate-continuity'
        elif any(flag['flag'] == 'no-baseline' for flag in flags):
            next_period = 'collect-baseline-reading'
        elif main_units < 0:
            next_period = 'verify-main-register'
        elif state == 'unexplained':
            next_period = 'third-branch-survey'
        else:
            next_period = 'normal'

    affected = []
    for meter_id, d in sorted(deltas.items()):
        if d['coverage'] != REPORTED:
            affected.append({'kind': 'meter', 'id': meter_id, 'branch_id': d['branch_id'],
                             'reason': d['coverage'], 'reported': d['raw'], 'adjusted': d['adjusted'],
                             'trust_low': d['low'], 'trust_high': d['high']})
        if any(flag['meter_id'] == meter_id and flag['flag'] == MOVED for flag in flags):
            affected.append({'kind': 'branch', 'id': d['branch_id'], 'reason': 'historical reading retained'})
    for flag in flags:
        if flag['flag'] in (MOVED, REVERSED, CONFLICT, 'no-baseline'):
            affected.append({'kind': 'segment', 'id': flag['segment'] or flag['meter_id'],
                             'reason': flag['flag']})
    for load_id, item in common.items():
        if item['route'] == 'clamp-test':
            affected.append({'kind': 'branch',
                             'id': _clamp_branch(db, site, item['evidence'][0], item.get('bypassed_meter') or 'missing'),
                             'reason': f'unmetered common load {load_id}', 'units': item['units']})
    for entry in misaligned:
        affected.append({'kind': 'meter', 'id': entry['meter_id'], 'reason': 'coverage-misaligned'})
    deduped = []
    for item in affected:
        if item not in deduped:
            deduped.append(item)

    orders = []
    for flag in flags:
        if flag['flag'] == MOVED:
            orders.append(('topology-verification', flag['meter_id'],
                           'confirm branch move and its effective segment in the meter book', 'medium',
                           flag.get('source_event')))
        elif flag['flag'] == REVERSED:
            orders.append(('meter-inspection', flag['meter_id'],
                           'inspect reversed register and restore forward measurement', 'high',
                           flag.get('source_event')))
        elif flag['flag'] == CONFLICT:
            orders.append(('certificate-conflict-retest', flag['meter_id'],
                           'keep both certificates on file and re-measure the affected segment', 'high',
                           flag.get('source_event')))
        elif flag['flag'] == 'no-baseline':
            orders.append(('baseline-reading', flag['meter_id'],
                           'collect a start reading for this register before using the period', 'high',
                           flag.get('source_event')))
    for entry in misaligned:
        orders.append(('clock-synchronization', entry['meter_id'],
                       'align reading cut-off with the main meter before next period', 'high', None))
        if not entry['clock_verified']:
            orders.append(('clock-verification', entry['meter_id'],
                           'obtain an independent clock check for this cut-off; misalignment may be clock drift',
                           'high', None))
    if responsibility.startswith('missing-metering-branch'):
        for load_id in sorted(common):
            if common[load_id]['route'] == 'clamp-test':
                orders.append(('unmetered-load-remediation', load_id,
                               'install a register or dedicated branch for the discovered load', 'high', None))
    if state == 'frozen':
        segments = _conflict_segments(db, site, flags, common, deltas)
        for segment in segments:
            if segment.startswith('missing-branch:'):
                load_id = segment.split(':', 1)[1]
                orders.append(('missing-branch-survey', load_id,
                               'wire and register the branch that carries this load, then re-test',
                               'high', None))
        named = [s for s in segments if not s.startswith('missing-branch:')]
        orders.append(('segment-retest', ','.join(named) or top,
                       're-measure branch segments carrying both certificates before attributing the difference',
                       'high', None))
    if state == 'unexplained':
        orders.append(('difference-investigation', ','.join(sorted(branch_ids)) or top,
                       'third-branch survey: inspect unmetered segments, do not book as process energy',
                       'medium', None))
    work_orders = []
    for n, (kind, target, detail, priority, source) in enumerate(orders, 1):
        work_orders.append({'order_id': f'WO-{site}-{period}-{n:02d}', 'kind': kind, 'target': target,
                            'detail': detail, 'priority': priority, 'source_event': source})

    evidence = {
        'main': {'meter_id': top, 'units': main_units,
                 'reading_event': main_current['event_id'] if main_current else None,
                 'previous_event': main_previous['event_id'] if main_previous else None},
        'branch': [{'meter_id': meter_id, 'branch_id': d['branch_id'], 'reported': d['raw'],
                    'adjusted': d['adjusted'], 'trust_low': d['low'], 'trust_high': d['high'],
                    'coverage': d['coverage']} for meter_id, d in sorted(deltas.items())],
        'common': [{'load_id': item['load_id'], 'units': item['units'], 'source': item['source'],
                    'route': item['route'], 'evidence': item['evidence']}
                   for item in sorted(common.values(), key=lambda x: x['load_id'])],
        'alignment': misaligned,
        'difference': difference,
        'claimed_explanations': ([{'route': 'calibration', 'units': adjusted - reported,
                                   'id': sorted(meter_id for meter_id, d in deltas.items()
                                                if d['coverage'] == 'calibrated')}]
                                 if calib_route else []) +
                                ([{'route': 'clamp-test', 'units': sum(
                                    i['units'] for i in common.values() if i['route'] == 'clamp-test'),
                                   'id': sorted(i['load_id'] for i in common.values()
                                                if i['route'] == 'clamp-test')}]
                                 if clamp_route else []),
        'booked': {'explained_units': explained, 'unexplained_units': unexplained,
                   'attribution': 'frozen' if state == 'frozen' else 'settled'},
        'late_topology_amendment': any(flag['flag'] == MOVED for flag in flags),
        'next_account_period': _next_period(period),
    }

    db.execute('DELETE FROM work_order WHERE site=? AND period=?', (site, period))
    for order in work_orders:
        db.execute('INSERT INTO work_order VALUES(?,?,?,?,?,?,?,?)',
                   (order['order_id'], site, period, order['kind'], order['target'],
                    order['detail'], order['priority'], order['source_event']))
    db.execute(
        'INSERT INTO energy_investigation(site,period,main_meter,main_units,branch_units,'
        'adjusted_branch_units,common_units,explained_units,unexplained_units,trust_low,trust_high,'
        'state,responsibility,reason,evidence,affected,work_orders,next_period) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(site,period) DO UPDATE SET '
        'main_meter=excluded.main_meter,main_units=excluded.main_units,branch_units=excluded.branch_units,'
        'adjusted_branch_units=excluded.adjusted_branch_units,common_units=excluded.common_units,'
        'explained_units=excluded.explained_units,unexplained_units=excluded.unexplained_units,'
        'trust_low=excluded.trust_low,trust_high=excluded.trust_high,state=excluded.state,'
        'responsibility=excluded.responsibility,reason=excluded.reason,evidence=excluded.evidence,'
        'affected=excluded.affected,work_orders=excluded.work_orders,next_period=excluded.next_period',
        (site, period, top, main_units, reported, adjusted, common_units,
         explained, unexplained, trust_low, trust_high, state, responsibility, reason,
         _j(evidence), _j(deduped), _j(work_orders), next_period))
    return dict(db.execute('SELECT * FROM energy_investigation WHERE site=? AND period=?',
                           (site, period)).fetchone())


def _public(row):
    out = dict(row)
    out['evidence'] = json.loads(out['evidence']) if out['evidence'] else {}
    out['affected'] = json.loads(out['affected']) if out['affected'] else []
    out['work_orders'] = json.loads(out['work_orders']) if out['work_orders'] else []
    out['work_order_count'] = len(out['work_orders'])
    out['units'] = out['main_units'] if out['state'] == 'single-meter' else out['adjusted_branch_units']
    return out


def investigate_site(db, sites):
    results = []
    for site in sites:
        periods = _periods(db, site)
        if periods:
            row = investigate(db, site, periods[-1])
            if row:
                results.append(_public(row))
    return results


def decide(db, site):
    """Compatibility entry point: baseline usage from the two latest readings."""
    if not db.execute('SELECT 1 FROM meter_reading WHERE site=?', (site,)).fetchone():
        db.execute('INSERT INTO energy_usage VALUES(?,?,?,?) ON CONFLICT(site) DO UPDATE SET '
                   'state=excluded.state,latest_period=excluded.latest_period,units=excluded.units',
                   (site, 'pending', '', 0))
        return dict(db.execute('SELECT * FROM energy_usage WHERE site=?', (site,)).fetchone())
    result = investigate_site(db, [site])[0]
    state = 'measured' if result['state'] == 'explained' else 'review'
    db.execute('INSERT INTO energy_usage VALUES(?,?,?,?) ON CONFLICT(site) DO UPDATE SET '
               'state=excluded.state,latest_period=excluded.latest_period,units=excluded.units',
               (site, state, result['period'], result['units']))
    return dict(db.execute('SELECT * FROM energy_usage WHERE site=?', (site,)).fetchone())
