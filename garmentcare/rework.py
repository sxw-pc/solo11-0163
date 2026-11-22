"""Size rework and fabric occupation ledger.

The two ends stay as they were: ``fit.import_data`` records the signed
measuring stick (current/target/allowance/signer) and ``alteration.decide``
keeps the old ``delta <= allowance`` feasibility.  This module adds the
business facts the simple feasibility cannot see:

* signed hem engineering baseline (fold width, regular/narrow overlock keeps);
* same-batch fabric bolts with width, grain, stripe period and defect zones;
* cut templates whose placement must satisfy grain and pattern alignment;
* irreversible cut receipts (fabric can never be returned to a full roll,
  even when a later measuring stick proves the target was typed wrong);
* sewing records and dual-signed post-sew re-measurement;
* per-garment acceptance against the signed tolerance and per-bolt real
  consumption / available remainder, with cost lines as the cost basis.
"""

from .source import record
from .store import update

ROUTE_NARROW = 'narrow_let_out'
ROUTE_BAND = 'pattern_band'
PHASE_REWORK = 'post_rework'
PHASE_SHRINK = 'late_shrinkage'
PHASE_PRESEW = 'pre_sew'


# ---------------------------------------------------------------- layout engine

def _intersects(a_x, a_y, a_w, a_l, b_x, b_y, b_w, b_l):
    return not (a_x + a_w <= b_x or b_x + b_w <= a_x
                or a_y + a_l <= b_y or b_y + b_l <= a_y)


def legal_placement(db, bolt, template, x, y, ignore_layout=None):
    """Return (ok, reason) for placing ``template`` on ``bolt`` at (x, y).

    Area alone never makes a placement legal: the rectangle must stay inside
    the bolt, respect warp grain, land on a stripe-period boundary, avoid
    every signed defect no-cut zone and every earlier irreversible cut.
    """
    if template['grain_required'] != bolt['grain_warp']:
        return False, 'warp grain direction mismatch'
    if x < 0 or y < 0 or x + template['width'] > bolt['width'] or y + template['length'] > bolt['length']:
        return False, 'template exceeds bolt width or length'
    period = bolt['pattern_period']
    if period and y % period != 0:
        return False, 'stripe pattern period not aligned'
    for zone in db.execute('SELECT * FROM defect_zone WHERE bolt_id=?', (bolt['bolt_id'],)):
        if _intersects(x, y, template['width'], template['length'],
                       zone['x'], zone['y'], zone['width'], zone['length']):
            return False, f"overlaps defect no-cut zone {zone['zone_id']}"
    for cut in db.execute('SELECT * FROM cut_receipt WHERE bolt_id=?', (bolt['bolt_id'],)):
        cut_tpl = db.execute('SELECT width,length FROM cut_template WHERE template_id=?',
                             (cut['template_id'],)).fetchone()
        if _intersects(x, y, template['width'], template['length'],
                       cut['x'], cut['y'], cut_tpl['width'], cut_tpl['length']):
            return False, f"overlaps irreversible cut {cut['event_id']}"
    for plan in db.execute('SELECT * FROM layout_plan WHERE bolt_id=? AND valid=1', (bolt['bolt_id'],)):
        if ignore_layout and plan['event_id'] == ignore_layout:
            continue
        other = db.execute('SELECT * FROM cut_template WHERE template_id=?', (plan['template_id'],)).fetchone()
        if _intersects(x, y, template['width'], template['length'],
                       plan['x'], plan['y'], other['width'], other['length']):
            return False, f"overlaps reserved layout {plan['event_id']}"
    return True, 'fits grain, pattern, defects and existing cuts'


def find_placement(db, bolt, template, ignore_layout=None):
    """First feasible (x, y); a big-enough total area does not guarantee one."""
    period = bolt['pattern_period'] or 1
    y = 0
    while y + template['length'] <= bolt['length']:
        for x in range(0, bolt['width'] - template['width'] + 1):
            ok, _ = legal_placement(db, bolt, template, x, y, ignore_layout)
            if ok:
                return x, y
        y += period
    return None


# ------------------------------------------------------------------- decisions

def _latest_choice(db, garment_id):
    return db.execute('SELECT * FROM rework_choice WHERE garment_id=? ORDER BY at DESC,event_id DESC LIMIT 1',
                      (garment_id,)).fetchone()


def _latest_measure(db, garment_id):
    return db.execute('SELECT * FROM fit_measure WHERE garment_id=? ORDER BY at DESC,event_id DESC LIMIT 1',
                      (garment_id,)).fetchone()


def _valid_remeasures(db, garment_id):
    return db.execute('SELECT * FROM remeasure WHERE garment_id=? AND valid=1 ORDER BY at DESC,event_id DESC',
                      (garment_id,)).fetchall()


def _cost_total(db, garment_id):
    row = db.execute('SELECT COALESCE(SUM(amount),0) AS s FROM rework_cost_line WHERE garment_id=?',
                     (garment_id,)).fetchone()
    return row['s']


def _add_cost(db, event_id, garment_id, kind, amount, basis):
    if db.execute('SELECT 1 FROM rework_cost_line WHERE event_id=?', (event_id,)).fetchone():
        return
    db.execute('INSERT INTO rework_cost_line VALUES(?,?,?,?,?)', (event_id, garment_id, kind, amount, basis))


def _rate(db, key, default):
    row = db.execute('SELECT amount FROM rate_card WHERE rate_key=?', (key,)).fetchone()
    return row['amount'] if row else default


def decide(db, garment_id):
    baseline = db.execute('SELECT * FROM hem_baseline WHERE garment_id=?', (garment_id,)).fetchone()
    if not baseline:
        return None
    measure = _latest_measure(db, garment_id)
    target = measure['target_hem'] if measure else baseline['target_hem']
    delta = target - baseline['current_hem']
    choice = _latest_choice(db, garment_id)
    cut = db.execute('SELECT * FROM cut_receipt WHERE garment_id=? ORDER BY at,event_id LIMIT 1',
                     (garment_id,)).fetchone()
    sew = db.execute('SELECT * FROM sew_record WHERE garment_id=? ORDER BY at,event_id LIMIT 1',
                     (garment_id,)).fetchone()
    remeasures = _valid_remeasures(db, garment_id)
    latest = remeasures[0] if remeasures else None
    open_cut = 1 if cut and not sew else 0

    def finish(state, reason, route=None, edge=None, measured=None):
        within = None
        if measured is not None:
            within = 1 if abs(measured - target) <= baseline['tolerance'] else 0
        return update(db, 'rework_decision', garment_id, state=state, reason=reason,
                      route=route, finished_edge=edge, within_tolerance=within,
                      final_hem=measured, total_cost=_cost_total(db, garment_id), cut_open=open_cut)

    # A corrected measuring stick can make the delta small, but it never erases
    # work already done: the no-rework short circuit only applies before a route
    # is chosen, any fabric is cut or any sewing is recorded.
    work_started = bool(choice or cut or sew)
    if delta <= baseline['hem_width'] - baseline['regular_hem_keep'] and not work_started:
        return finish('no_rework_needed', 'signed target reachable inside regular hem allowance')
    if not choice:
        return finish('route_pending', 'neither narrow let-out nor pattern band route signed')
    route = choice['route']

    if route == ROUTE_NARROW:
        if not baseline['narrow_consent'] or baseline['narrow_hem_keep'] is None:
            return finish('blocked', 'narrow overlock option not signed on the baseline', route)
        release = baseline['hem_width'] - baseline['narrow_hem_keep']
        if release < delta:
            return finish('blocked', 'signed narrow release cannot reach target', route)
        edge = f'narrow overlock {baseline["narrow_hem_keep"]}cm hem kept'
    else:
        if not baseline['band_consent']:
            return finish('blocked', 'same-pattern band option not signed on the baseline', route)
        if not cut:
            waiting = db.execute('SELECT 1 FROM layout_plan WHERE garment_id=? AND valid=1 LIMIT 1',
                                 (garment_id,)).fetchone()
            if waiting:
                return finish('awaiting_cut', 'valid layout reserved; cut receipt not yet issued', route)
            return finish('waiting_material', 'no legal template placement on same-batch bolt', route)
        edge = f'regular overlock {baseline["regular_hem_keep"]}cm hem kept with matching band'

    if not sew:
        return finish('cut_open' if cut else 'awaiting_sew',
                      'fabric cut but sewing not recorded' if cut else 'route ready; sewing not recorded',
                      route, edge)
    if not latest:
        return finish('sewn_pending_measure', 'no valid dual-signed post-sew re-measurement', route, edge)
    if latest['phase'] == PHASE_PRESEW:
        return finish('sewn_pending_measure', 're-measure predates sewing and cannot count as acceptance',
                      route, edge)
    measured = latest['measured_hem']
    if abs(measured - target) <= baseline['tolerance']:
        return finish('accepted', 'final hem within customer signed tolerance', route, edge, measured)
    if latest['phase'] == PHASE_SHRINK:
        return finish('rejected_shrinkage',
                      'late wash-shrinkage measurement outside tolerance; prior cuts stay occupied',
                      route, edge, measured)
    return finish('rejected_rework', 'post-rework re-measurement outside signed tolerance', route, edge, measured)


def refresh_fabric_ledger(db):
    """Real consumption is recomputed from cut receipts, never area bookkeeping."""
    for bolt in db.execute('SELECT * FROM fabric_bolt'):
        used = db.execute('SELECT COALESCE(SUM(area),0) AS s FROM cut_receipt WHERE bolt_id=?',
                          (bolt['bolt_id'],)).fetchone()['s']
        available = bolt['width'] * bolt['length'] - used
        any_template = db.execute('SELECT 1 FROM cut_template LIMIT 1').fetchone()
        state = 'open'
        if any_template:
            blocked = True
            for tpl in db.execute('SELECT * FROM cut_template'):
                if find_placement(db, bolt, tpl) is not None:
                    blocked = False
                    break
            state = 'exhausted' if blocked else 'open'
        db.execute('INSERT INTO fabric_ledger VALUES(?,?,?,?) ON CONFLICT(bolt_id) DO UPDATE SET '
                   'used_area=excluded.used_area,available_area=excluded.available_area,state=excluded.state',
                   (bolt['bolt_id'], used, available, state))


def _refresh_remeasure_validity(db, garment_id):
    sew = db.execute('SELECT * FROM sew_record WHERE garment_id=? ORDER BY at,event_id LIMIT 1',
                     (garment_id,)).fetchone()
    for row in db.execute('SELECT * FROM remeasure WHERE garment_id=?', (garment_id,)):
        if not row['measurer_signer'] or not row['owner_signer'] or row['measurer_signer'] == row['owner_signer']:
            valid, reason = 0, 'dual independent signatures missing'
        elif row['phase'] == PHASE_PRESEW and (not sew or row['at'] < sew['at']):
            valid, reason = 0, 'measurement predates actual sewing'
        elif row['phase'] in (PHASE_REWORK, PHASE_SHRINK) and (not sew or row['at'] < sew['at']):
            valid, reason = 0, 'post-rework measurement predates sewing'
        else:
            valid, reason = 1, 'dual-signed measurement on record'
        db.execute('UPDATE remeasure SET valid=?,reason=? WHERE event_id=?', (valid, reason, row['event_id']))


# --------------------------------------------------------------------- import

def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('hem_baselines', []):
            if record(db, row['garment_id'] + ':baseline', 'hem_baseline', row):
                db.execute('INSERT INTO hem_baseline VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (row['garment_id'], row['signed_at'], row['signer'], row['current_hem'],
                            row['target_hem'], row['tolerance'], row['hem_width'], row['regular_hem_keep'],
                            row.get('narrow_hem_keep'), int(row.get('narrow_consent', False)),
                            int(row.get('band_consent', False))))
            touched.add(row['garment_id'])
        for row in data.get('rate_cards', []):
            if record(db, 'rate:' + row['rate_key'], 'rate_card', row):
                db.execute('INSERT INTO rate_card VALUES(?,?)', (row['rate_key'], row['amount']))
        for row in data.get('bolts', []):
            if record(db, 'bolt:' + row['bolt_id'], 'fabric_bolt', row):
                db.execute('INSERT INTO fabric_bolt VALUES(?,?,?,?,?,?)',
                           (row['bolt_id'], row['width'], row['length'], row['grain_warp'],
                            row.get('pattern_period'), row['unit_price']))
        for row in data.get('defect_zones', []):
            if record(db, row['zone_id'], 'defect_zone', row):
                db.execute('INSERT INTO defect_zone VALUES(?,?,?,?,?,?)',
                           (row['zone_id'], row['bolt_id'], row['x'], row['y'], row['width'], row['length']))
        for row in data.get('templates', []):
            if record(db, 'tpl:' + row['template_id'], 'cut_template', row):
                db.execute('INSERT INTO cut_template VALUES(?,?,?,?)',
                           (row['template_id'], row['width'], row['length'], row['grain_required']))
        for row in data.get('route_choices', []):
            if record(db, row['event_id'], 'rework_choice', row):
                db.execute('INSERT INTO rework_choice VALUES(?,?,?,?)',
                           (row['event_id'], row['garment_id'], row['at'], row['route']))
            touched.add(row['garment_id'])
        for row in data.get('layouts', []):
            if record(db, row['event_id'], 'layout_plan', row):
                bolt = db.execute('SELECT * FROM fabric_bolt WHERE bolt_id=?', (row['bolt_id'],)).fetchone()
                tpl = db.execute('SELECT * FROM cut_template WHERE template_id=?', (row['template_id'],)).fetchone()
                if not bolt or not tpl:
                    raise ValueError(f"layout {row['event_id']} references unknown bolt or template")
                ok, reason = legal_placement(db, bolt, tpl, row['x'], row['y'])
                db.execute('INSERT INTO layout_plan VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['garment_id'], row['bolt_id'], row['template_id'],
                            row['x'], row['y'], int(ok), reason))
            touched.add(row['garment_id'])
        for row in data.get('cuts', []):
            if record(db, row['event_id'], 'cut_receipt', row):
                plan = db.execute('SELECT * FROM layout_plan WHERE event_id=?', (row['layout_event'],)).fetchone()
                if not plan:
                    raise ValueError(f"cut {row['event_id']} has no layout receipt {row['layout_event']}")
                if not plan['valid']:
                    raise ValueError(f"cut {row['event_id']} rejected: {plan['reason']}")
                bolt = db.execute('SELECT * FROM fabric_bolt WHERE bolt_id=?', (plan['bolt_id'],)).fetchone()
                tpl = db.execute('SELECT * FROM cut_template WHERE template_id=?', (plan['template_id'],)).fetchone()
                ok, reason = legal_placement(db, bolt, tpl, plan['x'], plan['y'], ignore_layout=plan['event_id'])
                if not ok:
                    raise ValueError(f"cut {row['event_id']} no longer placeable: {reason}")
                area = tpl['width'] * tpl['length']
                # Cutting is an irreversible material fact: insert the receipt first.
                db.execute('INSERT INTO cut_receipt(event_id,garment_id,layout_event,bolt_id,template_id,x,y,at,area) '
                           'VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['garment_id'], plan['event_id'], plan['bolt_id'],
                            plan['template_id'], plan['x'], plan['y'], row['at'], area))
                basis = (f"{tpl['width']}x{tpl['length']}cm cut from {bolt['bolt_id']} at "
                         f"({plan['x']},{plan['y']}); {bolt['unit_price']} per sq cm")
                _add_cost(db, row['event_id'] + ':fabric', row['garment_id'], 'fabric',
                          area * bolt['unit_price'], basis)
            touched.add(row['garment_id'])
        for row in data.get('sewing', []):
            if record(db, row['event_id'], 'sew_record', row):
                garment_id = row['garment_id']
                baseline = db.execute('SELECT * FROM hem_baseline WHERE garment_id=?', (garment_id,)).fetchone()
                if row['route'] == ROUTE_NARROW:
                    amount = _rate(db, 'narrow_let_out_labor', 550)
                    basis = 'unpick fold and re-stitch with signed narrow overlock; no new fabric consumed'
                else:
                    cut = db.execute('SELECT 1 FROM cut_receipt WHERE garment_id=?', (garment_id,)).fetchone()
                    if not cut:
                        raise ValueError(f"sewing {row['event_id']} cannot start before the band is cut")
                    amount = _rate(db, 'pattern_band_labor', 700)
                    basis = f'attach matching band; regular {baseline["regular_hem_keep"]}cm hem preserved'
                db.execute('INSERT INTO sew_record VALUES(?,?,?,?,?)',
                           (row['event_id'], garment_id, row['at'], row['route'], row['finished_edge']))
                _add_cost(db, row['event_id'] + ':labor', garment_id, 'labor', amount, basis)
            touched.add(row['garment_id'])
        for row in data.get('remeasures', []):
            if record(db, row['event_id'], 'remeasure', row):
                db.execute('INSERT INTO remeasure VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['garment_id'], row['at'], row['phase'],
                            row['measured_hem'], row.get('sew_id'), row['measurer_signer'],
                            row['owner_signer'], 0, 'pending validation'))
            touched.add(row['garment_id'])

        for garment_id in touched:
            _refresh_remeasure_validity(db, garment_id)
        refresh_fabric_ledger(db)
        return [decide(db, garment_id) for garment_id in sorted(touched)]

def audit_recompute(db):
    """Recompute fabric occupation and rework decisions from raw records.

    Used by the file-replay audit: the replay rebuilds every table from the
    original measuring sticks, layouts and cut receipts, then this function
    independently recomputes the derived ledgers and compares them.
    """
    garments = [r['garment_id'] for r in db.execute('SELECT garment_id FROM hem_baseline ORDER BY garment_id')]
    for garment_id in garments:
        _refresh_remeasure_validity(db, garment_id)
    stored = {r['bolt_id']: dict(r) for r in db.execute('SELECT * FROM fabric_ledger')}
    stored_decisions = {gid: dict(db.execute('SELECT * FROM rework_decision WHERE garment_id=?', (gid,)).fetchone())
                        for gid in garments}
    refresh_fabric_ledger(db)
    rebuilt = {r['bolt_id']: dict(r) for r in db.execute('SELECT * FROM fabric_ledger')}
    if stored != rebuilt:
        raise ValueError('fabric ledger disagrees with cut receipts')
    for garment_id in garments:
        decide(db, garment_id)
        after = dict(db.execute('SELECT * FROM rework_decision WHERE garment_id=?', (garment_id,)).fetchone())
        if stored_decisions[garment_id] != after:
            raise ValueError(f'rework decision for {garment_id} disagrees with raw records')
    return {'rework_garments': len(garments),
            'bolts': len(stored),
            'cuts': db.execute('SELECT COUNT(*) FROM cut_receipt').fetchone()[0]}
