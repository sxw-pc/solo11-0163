"""Evidence-driven offline-to-online acquisition attribution.

A booking is only an appointment, not an acquired customer. A real first
conversion requires (1) a signed store visit whose garment reaches the
``accepted`` intake state and (2) signed closing evidence (a dual-signed paper
coupon redemption, or a customer-signed commitment backed by a trusted logged-in
session). Device-shared QR scans without such evidence stay classified as
shared-device noise. Every result here is derived purely from journaled events,
so :func:`reevaluate` replays identically on an empty database during audit.
"""

VALID = 'valid'
WEB_CHANNELS = ('web', 'login')  # channels backed by cookie/session consent


def _rows(db, sql):
    return [dict(r) for r in db.execute(sql).fetchall()]


def reevaluate(db):
    """Recompute every derived attribution table from raw evidence events."""
    touches = _rows(db, 'SELECT * FROM campaign_touch ORDER BY at,event_id')
    bookings = _rows(db, 'SELECT * FROM service_booking ORDER BY at,event_id')
    issues = _rows(db, 'SELECT * FROM qr_coupon_issue ORDER BY issued_at,event_id')
    redeems = _rows(db, 'SELECT * FROM qr_coupon_redeem ORDER BY at,event_id')
    referrals = _rows(db, 'SELECT * FROM referral_credential ORDER BY at,event_id')
    declarations = _rows(db, 'SELECT * FROM shared_device_declaration ORDER BY at,event_id')
    links = _rows(db, 'SELECT * FROM identity_link ORDER BY at,event_id')
    withdrawals = _rows(db, 'SELECT * FROM consent_withdrawal ORDER BY at,event_id')
    commitments = _rows(db, 'SELECT * FROM login_commitment ORDER BY at,event_id')
    visits = _rows(db, 'SELECT * FROM store_visit ORDER BY at,event_id')
    garments = {r['id']: dict(r) for r in db.execute('SELECT * FROM garment').fetchall()}
    intake = {r['garment_id']: dict(r) for r in db.execute('SELECT * FROM intake_decision').fetchall()}

    shared_visitors = {d['visitor'] for d in declarations}
    withdrawn_at = {w['visitor']: w['at'] for w in withdrawals}
    visitor_customers = {}
    for link in links:
        visitor_customers.setdefault(link['visitor'], set()).add(link['customer'])
    customer_visitors = {}
    for link in links:
        customer_visitors.setdefault(link['customer'], set()).add(link['visitor'])
    all_customers = {l['customer'] for l in links} | {v['customer'] for v in visits}

    # ---- phase 1: statically validate referral credentials -----------------
    referral_by_id = {}
    for ref in referrals:
        if ref['referrer'] == ref['referee']:
            state, reason = 'invalid', 'referrer and referee must be different people'
        elif (ref['referrer_signer'] != ref['referrer']
              or ref['referee_signer'] != ref['referee']
              or ref['clerk_signer'] in (ref['referrer'], ref['referee'], '')):
            state, reason = 'invalid', 'dual-party and independent clerk signatures incomplete'
        else:
            state, reason = 'pending_verification', 'signed credential awaits a matching coupon redemption'
        ref['state'], ref['reason'] = state, reason
        referral_by_id[ref['event_id']] = ref

    # ---- phase 2: validate coupon redemptions against issues ---------------
    issue_index = {(i['coupon_no'], i['campaign'], i['shop']): i for i in issues}
    visit_by_id = {v['event_id']: v for v in visits}
    spent_issues = set()
    valid_redeems = []
    for red in redeems:
        key = (red['coupon_no'], red['campaign'], red['shop'])
        issue = issue_index.get(key)
        ref = referral_by_id.get(red['referral_event_id']) if red['referral_event_id'] else None
        visit = visit_by_id.get(red['visit_id'])
        if issue is None:
            state, reason = 'invalid', 'coupon number collision: no issue matches campaign and issuing shop'
        elif issue['event_id'] in spent_issues:
            state, reason = 'invalid', 'coupon already redeemed; forwarded screenshot is not a second receipt'
        elif red['customer_signer'] == red['clerk_signer'] or not red['clerk_signer']:
            state, reason = 'invalid', 'redemption lacks an independent clerk signature'
        elif visit is None or visit['status'] != 'signed' or visit['customer'] != red['customer_signer']:
            state, reason = 'invalid', 'redemption is not tied to this customer\'s signed store visit'
        elif red['referral_event_id'] and (
                ref is None or ref['state'] == 'invalid' or ref['referee'] != red['customer_signer']):
            state, reason = 'invalid', 'referral credential does not identify the redeeming customer as referee'
        else:
            state, reason = VALID, 'dual-signed paper coupon verified against issuing store and signed visit'
            spent_issues.add(issue['event_id'])
            valid_redeems.append(red)
        red['state'], red['reason'] = state, reason
        if state == VALID and ref is not None:
            ref['state'], ref['reason'] = VALID, 'referral closed by the referee\'s own coupon redemption'

    # ---- phase 3: validate login commitments against trusted sessions ------
    valid_commitments = []
    for com in commitments:
        if com['signer'] != com['customer']:
            state, reason = 'invalid', 'commitment is not signed by the named customer'
            continue
        backing = None
        for visitor in customer_visitors.get(com['customer'], ()):  # linked visitors only
            withdrawn = withdrawn_at.get(visitor)
            for tch in touches:
                if (tch['visitor'] == visitor and tch['campaign'] == com['campaign']
                        and tch['channel'] == 'login' and tch['at'] <= com['at']
                        and not (withdrawn is not None and withdrawn < tch['at'])):
                    backing = tch
        if backing is None:
            com['state'], com['reason'] = 'invalid', 'no trusted logged-in session (pre-withdrawal) backs this commitment'
        else:
            com['state'], com['reason'] = VALID, 'customer-signed commitment backed by trusted logged-in session'
            com['backing_touch'] = backing['event_id']
            valid_commitments.append(com)

    # ---- phase 4: classify every digital touch -----------------------------
    pinned = {}  # touch event_id -> customer

    def pin(touch_event, customer):
        pinned.setdefault(touch_event, set()).add(customer)

    for red in valid_redeems:
        issue = issue_index[(red['coupon_no'], red['campaign'], red['shop'])]
        for t in touches:  # the scan that issued the paper coupon, if digital
            if (issue['visitor'] and t['visitor'] == issue['visitor']
                    and t['campaign'] == red['campaign'] and t['at'] <= red['at']):
                pin(t['event_id'], red['customer_signer'])
    for com in valid_commitments:
        pin(com['backing_touch'], com['customer'])

    classifications = []
    for t in touches:
        withdrawn = withdrawn_at.get(t['visitor'])
        if t['event_id'] in pinned:
            customers = sorted(pinned[t['event_id']])
            role, usable, customer, reason = (
                'pinned', 1, customers[0],
                'touch claimed by signed closing evidence of ' + ','.join(customers))
        elif t['channel'] in WEB_CHANNELS and withdrawn is not None and withdrawn < t['at']:
            role, usable, customer, reason = (
                'withdrawn', 0, None,
                'cookie/session consent withdrawn before this touch; unusable for attribution')
        elif t['visitor'] in shared_visitors:
            role, usable, customer, reason = (
                'shared_device_noise', 0, None,
                'shared household device; touch cannot be assigned to one person without signed evidence')
        else:
            role, usable, reason = 'unlinked', 1, 'touch without a signed identity or closing evidence chain'
            customer = next(iter(visitor_customers.get(t['visitor'], ())), None)
        classifications.append((t['event_id'], t['visitor'], t['campaign'], role,
                                usable, customer, reason))

    # ---- phase 5: gate signed store visits on real intake results ----------
    visit_states = {}
    for v in visits:
        if v['status'] != 'signed':
            visit_states[v['event_id']] = ('cancelled', 'signed intake was returned or cancelled; facts retained, conversion removed')
            continue
        garment = garments.get(v['garment_id'])
        if garment is None:
            visit_states[v['event_id']] = ('intake_pending', 'no garment intake record for this visit')
        else:
            decision = intake.get(v['garment_id'])
            if decision is None or decision['state'] == 'pending':
                visit_states[v['event_id']] = ('intake_pending', 'store visit awaits an accepted intake decision')
            elif decision['state'] != 'accepted':
                visit_states[v['event_id']] = ('intake_blocked', f"intake decision is {decision['state']}; no real accepted store entry")
            else:
                visit_states[v['event_id']] = ('valid', 'signed visit with accepted intake; real store entry verified')
    valid_visits = [v for v in visits if visit_states[v['event_id']][0] == 'valid']

    # ---- phase 6: customer-level first valid conversion --------------------
    first_visit = {}
    for v in sorted(valid_visits, key=lambda x: (x['at'], x['event_id'])):
        first_visit.setdefault(v['customer'], v)

    customer_rows = {}
    for customer in sorted(all_customers):
        fv = first_visit.get(customer)
        if fv is None:
            cancelled = [v for v in visits if v['customer'] == customer]
            if cancelled and all(visit_states[v['event_id']][0] == 'cancelled' for v in cancelled):
                state, reason = 'visit_cancelled', 'all signed visits were returned or cancelled'
            else:
                state, reason = 'no_valid_visit', 'no accepted signed store visit on record'
            customer_rows[customer] = dict(state=state, campaign=None, source_event=None,
                                           evidence_kind=None, first_at=None, reason=reason)
            continue
        evidence = []  # (at, source_event, campaign, kind)
        for red in valid_redeems:
            if red['visit_id'] == fv['event_id'] and red['customer_signer'] == customer:
                evidence.append((red['at'], red['event_id'], red['campaign'], 'paper_coupon'))
        for com in valid_commitments:
            if com['customer'] == customer and com['at'] <= fv['at']:
                evidence.append((com['at'], com['event_id'], com['campaign'], 'login_commitment'))
        campaigns = {e[2] for e in evidence}
        if len(campaigns) > 1:
            detail = '; '.join(sorted(f'{e[2]} via {e[1]}' for e in evidence))
            row = dict(state='disputed', campaign=None, source_event=None, evidence_kind=None,
                       first_at=fv['at'],
                       reason='contradictory closing evidence claims the same store entry for several campaigns: ' + detail)
        elif len(campaigns) == 1:
            ev = sorted(evidence)[0]
            row = dict(state='acquired', campaign=ev[2], source_event=ev[1], evidence_kind=ev[3],
                       first_at=fv['at'], reason='first valid store entry closed by signed ' + ev[3] + ' evidence')
        else:
            linked_touch_events = {t['event_id'] for t in touches
                                   if t['visitor'] in customer_visitors.get(customer, ())}
            digital_signal = any(c[0] in linked_touch_events for c in classifications)
            if digital_signal:
                state, reason = 'provisional', 'digital touch exists (possibly shared-device noise) but no signed closing evidence; not a verified acquisition'
            else:
                state, reason = 'organic', 'real store entry without any campaign touch on a linked device'
            row = dict(state=state, campaign=None, source_event=None, evidence_kind=None,
                       first_at=fv['at'], reason=reason)
        customer_rows[customer] = row

    # ---- phase 7: per-booking decisions ------------------------------------
    visit_by_garment = {}
    for v in visits:
        visit_by_garment.setdefault(v['garment_id'], []).append(v)

    booking_rows = []
    for b in bookings:
        linked = visitor_customers.get(b['visitor'], set())
        garment_visits = visit_by_garment.get(b['garment_id'], [])
        customer = next((v['customer'] for v in garment_visits), None)
        if customer is None and len(linked) == 1:
            customer = next(iter(linked))
        if customer is None:
            if linked:
                row = dict(state='identity_unresolved', campaign=None, source_event=None,
                           customer=None, evidence_kind=None,
                           reason='shared device links to several people and no signed visit resolves this booking')
            else:  # legacy unsigned path: keep last-touch, but never claim a real customer
                touch = next((t for t in reversed(touches)
                              if t['visitor'] == b['visitor'] and t['at'] <= b['at']
                              and not (t['channel'] in WEB_CHANNELS
                                       and (w := withdrawn_at.get(t['visitor'])) is not None
                                       and w < t['at'])), None)
                if touch:
                    row = dict(state='legacy_provisional', campaign=touch['campaign'],
                               source_event=touch['event_id'], customer=None, evidence_kind=None,
                               reason='unsigned legacy path; last-touch guess, no signed acquisition evidence')
                else:
                    row = dict(state='organic', campaign=None, source_event=None,
                               customer=None, evidence_kind=None, reason='booking without any usable touch')
            booking_rows.append((b['event_id'], row))
            continue
        crow = customer_rows[customer]
        fv = first_visit.get(customer)
        this_visit = garment_visits[0] if garment_visits else None
        if crow['state'] == 'acquired' and fv is not None and this_visit is not None \
                and this_visit['event_id'] != fv['event_id']:
            row = dict(state='repeat_visit', campaign=crow['campaign'], source_event=crow['source_event'],
                       customer=customer, evidence_kind=crow['evidence_kind'],
                       reason='same customer already had a first valid conversion; this booking counts once')
        elif crow['state'] == 'acquired' and fv is not None and this_visit is None:
            row = dict(state='repeat_visit', campaign=crow['campaign'], source_event=crow['source_event'],
                       customer=customer, evidence_kind=crow['evidence_kind'],
                       reason='appointment without its own accepted visit; customer already converted elsewhere')
        elif this_visit is not None and visit_states[this_visit['event_id']][0] in ('intake_blocked', 'intake_pending', 'cancelled'):
            st, why = visit_states[this_visit['event_id']]
            row = dict(state=st, campaign=None, source_event=None, customer=customer,
                       evidence_kind=None, reason=why)
        else:
            row = dict(customer=customer, **{k: crow[k] for k in
                     ('state', 'campaign', 'source_event', 'evidence_kind', 'reason')})
        booking_rows.append((b['event_id'], row))

    # ---- phase 8: persist derived tables with monotonic versions -----------
    _replace(db, 'touch_classification',
             ('event_id', 'visitor', 'campaign', 'role', 'usable', 'customer', 'reason'),
             classifications)
    _replace_state(db, 'qr_coupon_redeem', 'event_id',
                   ((r['event_id'], r['state'], r['reason']) for r in redeems),
                   ('state', 'reason'))
    _replace_state(db, 'login_commitment', 'event_id',
                   ((r['event_id'], r['state'], r['reason']) for r in commitments),
                   ('state', 'reason'))
    _replace_state(db, 'referral_credential', 'event_id',
                   ((r['event_id'], r['state'], r['reason']) for r in referrals),
                   ('state', 'reason'))
    old_acq = {r['booking_id']: dict(r) for r in db.execute('SELECT * FROM acquisition_decision')}
    old_cust = {r['customer']: dict(r) for r in db.execute('SELECT * FROM customer_acquisition')}
    db.execute('DELETE FROM acquisition_decision')
    db.execute('DELETE FROM customer_acquisition')
    for booking_id, row in booking_rows:
        old = old_acq.get(booking_id)
        version = _bump(old, row)
        db.execute('INSERT INTO acquisition_decision VALUES(?,?,?,?,?,?,?,?)',
                   (booking_id, row['state'], row['campaign'], row['source_event'],
                    row['customer'], row['evidence_kind'], row['reason'], version))
    for customer, row in customer_rows.items():
        old = old_cust.get(customer)
        version = _bump(old, row)
        db.execute('INSERT INTO customer_acquisition VALUES(?,?,?,?,?,?,?,?)',
                   (customer, row['state'], row['campaign'], row['source_event'],
                    row['evidence_kind'], row['first_at'], row['reason'], version))
    return [dict(r) for r in db.execute('SELECT * FROM acquisition_decision ORDER BY booking_id')]


def _bump(old, row):
    if old and all(old.get(k) == v for k, v in row.items()):
        return old['version']
    return (old['version'] + 1) if old else 1


def _replace(db, table, columns, rows):
    db.execute(f'DELETE FROM {table}')
    sql = f"INSERT INTO {table} VALUES({','.join('?' for _ in columns)})"
    for row in rows:
        db.execute(sql, tuple(row))


def _replace_state(db, table, key, rows, state_cols):
    for values in rows:
        sets = ','.join(f'{col}=?' for col in state_cols)
        db.execute(f'UPDATE {table} SET {sets} WHERE {key}=?', (*values[1:], values[0]))


def decide(db, booking_id):
    """Legacy entry point: reevaluate the whole evidence graph, then return one booking."""
    reevaluate(db)
    row = db.execute('SELECT * FROM acquisition_decision WHERE booking_id=?', (booking_id,)).fetchone()
    return dict(row) if row else None


def campaign_totals(db):
    """Reproducible per-campaign acquisition counts (first valid conversions only)."""
    totals = {}
    for row in db.execute("SELECT campaign,state FROM customer_acquisition WHERE state='acquired'"):
        totals[row['campaign']] = totals.get(row['campaign'], 0) + 1
    disputed = db.execute("SELECT COUNT(*) FROM customer_acquisition WHERE state='disputed'").fetchone()[0]
    return {'acquired_by_campaign': dict(sorted(totals.items())),
            'disputed_customers': disputed,
            'acquired_customers': sum(totals.values())}
