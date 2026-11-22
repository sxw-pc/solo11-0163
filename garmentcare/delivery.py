"""Order-level delivery arbitration.

An order is one physical bag or, after a split, a family of bags. Delivery is decided
per garment from signed bag handoffs, then rolled up: two garments each being
individually returnable never makes the sealed bag delivered by addition.

Order evidence is append-only and replayable. A split application is lineage only:
it moves a garment to a new bag and never marks it delivered. A handoff is valid only
for a garment whose own intake baseline is accepted and whose own care process is
complete, whose current bag/seal at the handoff time matches, and who has an owner
signed pickup authority for the receiving person. Another garment's authority, bag,
seal or signature never transfers.
"""
import json

from .source import record, file_record
from .store import orders_for_garment, update, update_order

STATE_LABELS = {
    'pending': '整单待交付',
    'split_available': '可分袋交付',
    'partially_delivered': '已部分交付',
    'delivered': '已全部交付',
    'handoff_dispute': '交接争议',
}


class SplitError(ValueError):
    """A split request cannot be proven from the order and garment evidence."""


# ---------------------------------------------------------------- reads

def _order(db, order_id):
    row = db.execute('SELECT * FROM care_order WHERE order_id=?', (order_id,)).fetchone()
    if not row:
        raise SplitError(f'unknown order: {order_id}')
    return dict(row)


def _garment(db, garment_id):
    row = db.execute('SELECT * FROM garment WHERE id=?', (garment_id,)).fetchone()
    return dict(row) if row else None


def _members(db, order):
    return [row['garment_id'] for row in db.execute(
        'SELECT garment_id FROM order_member WHERE order_id=? ORDER BY garment_id', (order['order_id'],))]


def _splits(db, order):
    return sorted((dict(row) for row in db.execute(
        'SELECT * FROM order_split WHERE order_id=?', (order['order_id'],))),
        key=lambda row: (row['at'], row['event_id']))


def _handoffs(db, order):
    return sorted((dict(row) for row in db.execute(
        'SELECT * FROM order_handoff WHERE order_id=?', (order['order_id'],))),
        key=lambda row: (row['at'], row['event_id']))


def bag_layout(db, order, at=None, exclude_event=None):
    """garment_id -> (bag, seal, since) after applying splits visible at `at`."""
    layout = {g: (order['original_bag'], order['original_seal'], order['created_at']) for g in _members(db, order)}
    for split in _splits(db, order):
        if split['event_id'] == exclude_event:
            continue
        if at is not None and split['at'] > at:
            continue
        if split['garment_id'] in layout:
            layout[split['garment_id']] = (split['to_bag'], split['new_seal'], split['at'])
    return layout


def bag_for(db, garment_id, at=None):
    """Current (or historical) bag for a garment, or None when it has no order."""
    order_ids = orders_for_garment(db, garment_id)
    if not order_ids:
        return None
    order = _order(db, order_ids[0])
    return bag_layout(db, order, at=at).get(garment_id, (None, None, None))[0]


# ---------------------------------------------------------------- split legality

def _validate_split(db, order, row):
    """Structural split rules: membership, owner consent, lineage and bag identity."""
    garment_id = row['garment_id']
    if garment_id not in _members(db, order):
        raise SplitError(f'garment {garment_id} is not a member of order {order["order_id"]}')
    garment = _garment(db, garment_id)
    if garment and garment['owner'] != order['owner']:
        raise SplitError(f'garment {garment_id} is not owned by the order owner')
    if row['requested_by'] != order['owner'] or row['owner_signer'] != order['owner']:
        raise SplitError('split requires an owner-signed request')
    if not order['allow_split']:
        raise SplitError('owner has not authorized batch pickup for this order')
    layout = bag_layout(db, order, exclude_event=row['event_id'])
    if garment_id not in layout:
        raise SplitError(f'garment {garment_id} has no bag in order {order["order_id"]}')
    if row['from_bag'] != layout[garment_id][0]:
        raise SplitError(f'split source bag is not garment {garment_id}\'s current bag')
    if row['to_bag'] == row['from_bag']:
        raise SplitError('split target bag must differ from the source bag')
    if row['at'] < layout[garment_id][2]:
        raise SplitError('split cannot predate the garment\'s current bag')
    used = {order['original_bag'], *(split['to_bag'] for split in _splits(db, order) if split['event_id'] != row['event_id'])}
    if row['to_bag'] in used:
        raise SplitError(f'bag {row["to_bag"]} already belongs to this order')
    if not row['new_seal'] or row['new_seal'] == layout[garment_id][1]:
        raise SplitError('split needs a fresh seal distinct from the source seal')


def _build_split(db, order, row):
    garment_id = row['garment_id']
    current = bag_layout(db, order, exclude_event=row.get('event_id'))
    if garment_id not in current:
        raise SplitError(f'garment {garment_id} has no bag in order {order["order_id"]}')
    row = {'event_id': row.get('event_id') or f"split-{order['order_id']}-{garment_id}-{row['to_bag']}",
           'order_id': order['order_id'], 'garment_id': garment_id,
           'from_bag': current[garment_id][0], 'to_bag': row['to_bag'],
           'new_seal': row['new_seal'], 'at': row['at'],
           'requested_by': row.get('requested_by') or order['owner'],
           'owner_signer': row.get('owner_signer') or order['owner']}
    _validate_split(db, order, row)
    return row


def _seed_membership(db, order):
    """Persist the bag lineage: original membership plus one row per split target."""
    for garment_id in _members(db, order):
        db.execute('INSERT OR IGNORE INTO bag_membership(order_id,bag,garment_id,from_bag,split_event,at) VALUES(?,?,?,?,?,?)',
                   (order['order_id'], order['original_bag'], garment_id, None, order['event_id'], order['created_at']))
    for split in _splits(db, order):
        db.execute('INSERT OR IGNORE INTO bag_membership(order_id,bag,garment_id,from_bag,split_event,at) VALUES(?,?,?,?,?,?)',
                   (order['order_id'], split['to_bag'], split['garment_id'], split['from_bag'],
                    split['event_id'], split['at']))


# ---------------------------------------------------------------- handoff legality

def _prior_delivery(db, garment_id):
    row = db.execute('SELECT * FROM delivery_decision WHERE garment_id=?', (garment_id,)).fetchone()
    if row and row['state'] == 'delivered' and row['receipt_id']:
        return dict(row)
    return None


def _evidence_seq(db, event_id):
    """Arrival order of an evidence record; identical in live state and audit replay."""
    row = db.execute('SELECT rowid FROM evidence WHERE event_id=?', (event_id,)).fetchone()
    return row[0] if row else -1


def _latest_intake_evidence_seq(db, garment_id):
    rows = db.execute('SELECT event_id FROM intake_photo WHERE garment_id=?', (garment_id,)).fetchall()
    return max((_evidence_seq(db, row['event_id']) for row in rows), default=-1)


def _resolve_handoff(db, order, garment_id, handoff, layout_at):
    """Classify one garment against one signed bag handoff at the handoff's own time."""
    garment = _garment(db, garment_id)
    if not garment:
        return 'ineligible', 'garment evidence missing'
    bag, seal, _ = layout_at[garment_id]
    if handoff['bag'] != bag:
        return 'not_this_bag', None
    if handoff['seal'] != seal:
        return 'seal_mismatch', None
    intake = db.execute('SELECT * FROM intake_decision WHERE garment_id=?', (garment_id,)).fetchone()
    process = db.execute('SELECT * FROM process_decision WHERE garment_id=?', (garment_id,)).fetchone()
    if intake and intake['state'] == 'frozen':
        # A signature that predates the late conflict remains a real delivery fact;
        # the conflict then freezes new eligibility and the payout. A handoff signed
        # after the conflict is a new delivery and is not allowed.
        if _evidence_seq(db, handoff['event_id']) < _latest_intake_evidence_seq(db, garment_id):
            return 'delivered_frozen', None
        return 'ineligible', 'late intake conflict blocks new delivery eligibility'
    if not intake or intake['state'] != 'accepted':
        return 'ineligible', 'intake baseline not accepted'
    if not process or process['state'] != 'completed':
        return 'ineligible', 'process has not completed'
    from .returns import authority_ok
    if not authority_ok(db, garment, handoff['person'], handoff['at'], handoff['owner_signer']):
        return 'unauthorized', None
    return 'delivered', None


def _is_dispute(outcome):
    return outcome in ('unauthorized', 'seal_mismatch')


def decide_order(db, order_id):
    order = _order(db, order_id)
    members = _members(db, order)
    handoffs = _handoffs(db, order)

    # A signed handoff is a real fact. A later upstream conflict freezes new eligibility
    # and the payout but never rewrites the signature into "never delivered".
    delivered = {}
    disputed = set()
    late_frozen = set()
    frozen = {}
    for garment_id in members:
        prior = _prior_delivery(db, garment_id)
        intake = db.execute('SELECT * FROM intake_decision WHERE garment_id=?', (garment_id,)).fetchone()
        frozen[garment_id] = bool(intake and intake['state'] == 'frozen')
        if prior and frozen[garment_id]:
            delivered[garment_id] = {'bag': prior['bag'], 'seal': None,
                                     'receipt_id': prior['receipt_id'], 'at': None}
            late_frozen.add(garment_id)

    disputes = []
    ineligible = {}
    blocked = False

    for handoff in handoffs:
        layout_at = bag_layout(db, order, at=handoff['at'])
        # A sealed bag is handed over as a unit. If any garment sealed in it may not
        # leave (ineligible intake/process, unauthorized recipient, wrong seal), the
        # whole handoff is invalid; nothing in that bag is marked delivered by it.
        contents = [g for g in members if layout_at[g][0] == handoff['bag']]
        if not contents:
            continue
        outcomes = {g: _resolve_handoff(db, order, g, handoff, layout_at) for g in contents}
        invalid = [(g, outcome) for g, (outcome, _) in outcomes.items()
                   if outcome not in ('delivered', 'delivered_frozen', 'not_this_bag')]
        if invalid:
            blocked = True
            for garment_id, outcome in invalid:
                if _is_dispute(outcome):
                    disputed.add(garment_id)
                    disputes.append({'garment_id': garment_id,
                                     'kind': 'unauthorized_recipient' if outcome == 'unauthorized'
                                     else 'sealed_bag_mismatch',
                                     'receipt': handoff['event_id']})
                else:
                    ineligible[garment_id] = outcomes[garment_id][1]
            continue
        for garment_id, (outcome, _) in outcomes.items():
            if outcome not in ('delivered', 'delivered_frozen'):
                continue
            if outcome == 'delivered_frozen':
                late_frozen.add(garment_id)
            if garment_id in disputed:
                continue
            if garment_id in delivered:
                if delivered[garment_id]['receipt_id'] != handoff['event_id']:
                    disputed.add(garment_id)
                    disputes.append({'garment_id': garment_id, 'kind': 'duplicate_delivery',
                                     'receipts': sorted([delivered[garment_id]['receipt_id'], handoff['event_id']])})
                continue
            delivered[garment_id] = {'bag': handoff['bag'], 'seal': handoff['seal'],
                                     'receipt_id': handoff['event_id'], 'at': handoff['at']}
    for garment_id in disputed:
        delivered.pop(garment_id, None)
        late_frozen.discard(garment_id)

    eligible = {}
    for garment_id in members:
        intake = db.execute('SELECT * FROM intake_decision WHERE garment_id=?', (garment_id,)).fetchone()
        process = db.execute('SELECT * FROM process_decision WHERE garment_id=?', (garment_id,)).fetchone()
        eligible[garment_id] = bool(intake and intake['state'] == 'accepted'
                                    and process and process['state'] == 'completed'
                                    and not frozen[garment_id])

    delivered_ids = sorted(delivered)
    undelivered_ids = sorted(set(members) - set(delivered))
    frozen_ids = sorted(g for g in members if frozen[g] and g in delivered)
    disputed_ids = {d['garment_id'] for d in disputes}
    for garment_id in members:
        row = delivered.get(garment_id)
        if garment_id in disputed_ids:
            kinds = sorted({d['kind'] for d in disputes if d['garment_id'] == garment_id})
            update(db, 'delivery_decision', garment_id, order_id=order_id, state='dispute',
                   reason='conflicting or unauthorized signed handoffs: ' + ','.join(kinds),
                   bag=row['bag'] if row else None, receipt_id=row['receipt_id'] if row else None,
                   claim_frozen=1)
        elif row:
            reason = 'signed handoff under authorized pickup'
            if garment_id in late_frozen:
                reason = 'signed handoff preserved; late intake conflict freezes new eligibility'
            update(db, 'delivery_decision', garment_id, order_id=order_id, state='delivered',
                   reason=reason, bag=row['bag'], receipt_id=row['receipt_id'],
                   claim_frozen=int(garment_id in late_frozen))
        else:
            if frozen[garment_id]:
                reason = 'late intake conflict froze new delivery eligibility'
            elif not eligible[garment_id]:
                reason = ineligible.get(garment_id, 'intake baseline not accepted or process incomplete')
            else:
                reason = 'eligible garment awaiting signed handoff'
            update(db, 'delivery_decision', garment_id, order_id=order_id, state='pending',
                   reason=reason, bag=None, receipt_id=None, claim_frozen=int(frozen[garment_id]))

    if disputes:
        state, reason = 'handoff_dispute', '; '.join(sorted({f'{d["kind"]}:{d["garment_id"]}' for d in disputes}))
    elif delivered_ids and not undelivered_ids:
        state, reason = 'delivered', 'every order garment has a signed handoff'
    elif delivered_ids:
        state, reason = 'partially_delivered', 'some order garments have signed handoffs'
    elif order['allow_split'] and any(eligible.values()) and not all(eligible.values()) and not blocked:
        state, reason = 'split_available', 'owner allows batch pickup; eligible garments may be split out'
    else:
        state, reason = 'pending', 'no signed handoff yet'

    return update_order(db, order_id, state=state, reason=reason,
                        delivered=json.dumps(delivered_ids, sort_keys=True),
                        undelivered=json.dumps(undelivered_ids, sort_keys=True),
                        frozen=json.dumps(frozen_ids, sort_keys=True),
                        disputes=json.dumps(disputes, sort_keys=True, ensure_ascii=False))


def decide_orders_for_garments(db, garment_ids):
    order_ids = set()
    for garment_id in garment_ids:
        order_ids.update(orders_for_garment(db, garment_id))
    return [decide_order(db, order_id) for order_id in sorted(order_ids)]


def refresh(db, garment_ids):
    """Re-adjudicate orders touched by upstream evidence, then claims that depend on them."""
    from . import returns
    ids = set(garment_ids)
    decide_orders_for_garments(db, ids)
    for garment_id in sorted(ids):
        returns.decide(db, garment_id)


# ---------------------------------------------------------------- imports

def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('orders', []):
            if record(db, row['event_id'], 'care_order', row):
                try:
                    db.execute('INSERT INTO care_order VALUES(?,?,?,?,?,?,?)',
                               (row['event_id'], row['order_id'], row['owner'],
                                int(bool(row.get('allow_split'))), row['original_bag'],
                                row['original_seal'], row['created_at']))
                except Exception as exc:
                    raise ValueError(f'order conflict: {row["order_id"]}') from exc
            touched.add(row['order_id'])
        for row in data.get('members', []):
            if record(db, row['event_id'], 'order_member', row):
                db.execute('INSERT OR IGNORE INTO order_member(event_id,order_id,garment_id) VALUES(?,?,?)',
                           (row['event_id'], row['order_id'], row['garment_id']))
            touched.add(row['order_id'])
        for row in data.get('splits', []):
            if record(db, row['event_id'], 'order_split', row):
                db.execute('INSERT INTO order_split VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['order_id'], row['garment_id'], row['from_bag'],
                            row['to_bag'], row['new_seal'], row['at'], row['requested_by'],
                            row['owner_signer']))
            touched.add(row['order_id'])
        for row in data.get('handoffs', []):
            if record(db, row['event_id'], 'order_handoff', row):
                db.execute('INSERT INTO order_handoff VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['order_id'], row['bag'], row['seal'],
                            row['person'], row['at'], row['condition'], row['owner_signer']))
            touched.add(row['order_id'])
        for order_id in sorted(touched):
            order_row = db.execute('SELECT order_id FROM care_order WHERE order_id=?', (order_id,)).fetchone()
            if not order_row:
                continue  # order evidence may arrive after its members; it re-validates on arrival
            order = _order(db, order_id)
            for split in _splits(db, order):
                _validate_split(db, order, split)
            _seed_membership(db, order)
    members = {m for order_id in touched
               if db.execute('SELECT 1 FROM care_order WHERE order_id=?', (order_id,)).fetchone()
               for m in _members(db, _order(db, order_id))}
    with db:
        refresh(db, members)
    rows=db.execute(f"SELECT * FROM order_decision WHERE order_id IN ({','.join('?' for _ in touched)}) ORDER BY order_id",
                    tuple(sorted(touched))).fetchall() if touched else []
    return [dict(row) for row in rows]


def _journaled(db, body):
    """Helper-built evidence must replay through audit exactly like a CLI file."""
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    file_record(db, 'order-import', raw)
    return import_data(db, body)


def split_order(db, order_id, garment_id, to_bag, new_seal, at,
                requested_by=None, owner_signer=None):
    order = _order(db, order_id)
    if _garment(db, garment_id) is None:
        raise SplitError(f'unknown garment: {garment_id}')
    row = {'order_id': order_id, 'garment_id': garment_id, 'to_bag': to_bag,
           'new_seal': new_seal, 'at': at,
           'requested_by': requested_by or order['owner'],
           'owner_signer': owner_signer or order['owner']}
    row = _build_split(db, order, row)
    return _journaled(db, {'splits': [row]})


def handoff_order(db, order_id, bag, seal, person, at, condition='clean', owner_signer=None):
    row = {'event_id': f'handoff-{order_id}-{bag}-{at}', 'order_id': order_id, 'bag': bag,
           'seal': seal, 'person': person, 'at': at, 'condition': condition,
           'owner_signer': owner_signer or person}
    return _journaled(db, {'handoffs': [row]})
