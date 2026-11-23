"""Cross-shop processing fund clearing and actual payment ledger.

Money facts (customer order price, bank receipts/postings, inter-shop
transfers, compensation payouts and bounced tickets) are recorded as
immutable events.  Each external movement is bound to one order plus an
explicit payer and payee, so an equal amount on a different order can
never offset two garments.  A bank ``received`` acknowledgement is only
a pending ticket: money counts once a matching ``applied`` posting with
the correct order/payer/payee exists, and a bounce ticket voids the
reference entirely.

Signed processing agreements and signed liability protocols are versioned
decisions.  Re-adjudication after the fact never rewrites historical
payments or handoffs; new financial duties become a new version and any
money that already moved is recovered through a claim instead.
"""
import csv
import io
import json
from .source import record, file_record

DIRECTIONS = ('customer_payment', 'shop_transfer', 'compensation', 'recovery')
KINDS = ('received', 'applied', 'bounce')
LIABLE_SHOPS = ('from_shop', 'to_shop')
INT_FIELDS = ('amount', 'fee')


class _Ref:
    """One bank reference folded from its ordered ticket events."""
    __slots__ = ('ref', 'events', 'bounced', 'posted', 'first_at', 'last_at')

    def __init__(self, events):
        self.events = events
        self.ref = events[0]['bank_ref']
        kinds = [e['kind'] for e in events]
        self.bounced = 'bounce' in kinds
        self.posted = 'applied' in kinds
        self.first_at = events[0]['at']
        self.last_at = events[-1]['at']


def _require(row, field):
    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{row.get('event_id', '?')}: missing {field}")
    return value


def _amount(row, field):
    value = _require(row, field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{row['event_id']}: {field} must be a positive integer")
    return value


def _validate_movement(row):
    _require(row, 'order_id')
    _require(row, 'garment_id')
    if row['direction'] not in DIRECTIONS:
        raise ValueError(f"{row['event_id']}: bad direction {row['direction']}")
    if row['kind'] not in KINDS:
        raise ValueError(f"{row['event_id']}: bad kind {row['kind']}")
    _require(row, 'payer')
    _require(row, 'payee')
    _amount(row, 'amount')
    _require(row, 'bank_ref')
    _require(row, 'at')


def _coerce(value):
    value = value.strip()
    if value == '':
        return None
    try:
        return int(value)
    except ValueError:
        return value


def parse_csv(raw):
    """Parse one settlement CSV file into the import payload shape."""
    text = raw.decode('utf-8-sig') if isinstance(raw, (bytes, bytearray)) else raw
    rows = list(csv.DictReader(io.StringIO(text)))
    data = {}
    groups = (('orders', 'orders'), ('agreements', 'agreements'),
              ('movements', 'movements'), ('liabilities', 'liabilities'))
    buckets = {key: [] for key, _ in groups}
    for raw_row in rows:
        row = {key: _coerce(value) for key, value in raw_row.items()}
        row = {key: value for key, value in row.items() if value is not None}
        kind = row.pop('record_type', None)
        if kind not in buckets:
            raise ValueError(f"CSV row has unknown record_type: {kind}")
        buckets[kind].append(row)
    for key, payload_key in groups:
        if buckets[key]:
            data[payload_key] = buckets[key]
    return data


def _ref_groups(db, order_id):
    rows = db.execute('SELECT * FROM bank_movement WHERE order_id=? ORDER BY at,event_id',
                      (order_id,)).fetchall()
    groups = {}
    for row in rows:
        groups.setdefault(row['bank_ref'], []).append(dict(row))
    return [_Ref(events) for events in groups.values()]


def _expected_match(ref, direction, order_id, payer=None, payee=None):
    """A reference counts only when order, direction and both parties match."""
    head = ref.events[0]
    if head['direction'] != direction or head['order_id'] != order_id:
        return False
    if payer is not None and head['payer'] != payer:
        return False
    if payee is not None and head['payee'] != payee:
        return False
    return True


def _store_decision(db, order_id, garment_id, version, state, reason, payload):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    old = db.execute('SELECT version,state,reason,payload FROM shop_settlement_decision WHERE order_id=?',
                     (order_id,)).fetchone()
    if old and (old['state'], old['reason'], old['payload']) == (state, reason, body):
        return dict(old)
    db.execute('''INSERT INTO shop_settlement_decision(order_id,garment_id,version,state,reason,payload)
                  VALUES(?,?,?,?,?,?)
                  ON CONFLICT(order_id) DO UPDATE SET garment_id=excluded.garment_id,
                    version=excluded.version,state=excluded.state,reason=excluded.reason,payload=excluded.payload''',
               (order_id, garment_id, version, state, reason, body))
    db.execute('INSERT INTO decision_history(subsystem,garment_id,version,state,reason) VALUES(?,?,?,?,?)',
               ('shop_settlement_decision', garment_id, version, state, reason))
    return dict(db.execute('SELECT * FROM shop_settlement_decision WHERE order_id=?',
                           (order_id,)).fetchone())


def decide(db, garment_id):
    order = db.execute('SELECT * FROM shop_order WHERE garment_id=? ORDER BY at,event_id',
                       (garment_id,)).fetchone()
    if not order:
        return None
    order = dict(order)
    order_id = order['order_id']
    refs = _ref_groups(db, order_id)

    agreements = db.execute('SELECT * FROM processing_agreement WHERE order_id=? ORDER BY version,event_id',
                            (order_id,)).fetchall()
    agreement = dict(agreements[-1]) if agreements else None
    liabilities = db.execute('SELECT * FROM liability_decision WHERE order_id=? ORDER BY version,event_id',
                             (order_id,)).fetchall()
    liability = dict(liabilities[-1]) if liabilities else None
    comp = db.execute('SELECT * FROM compensation_decision WHERE garment_id=?', (garment_id,)).fetchone()
    cross_ops = db.execute('SELECT * FROM operation WHERE garment_id=? AND from_shop<>to_shop',
                           (garment_id,)).fetchall()
    custody_gap = any(not op['sender'] or not op['receiver'] or op['sender'] == op['receiver']
                      for op in cross_ops)

    shop_a = order['receiving_shop']
    shop_b = agreement['to_shop'] if agreement else None
    fee = agreement['fee'] if agreement else 0
    price = order['amount']

    # The liability protocol records a contractual role; resolve it to the
    # actual shop id before matching bank tickets.
    liable_party = {'from_shop': shop_a, 'to_shop': shop_b}.get(
        liability['liable_shop']) if liability else None
    a_comp_duty = liability['amount'] if liability and liability['liable_shop'] == 'from_shop' else 0
    b_comp_duty = liability['amount'] if liability and liability['liable_shop'] == 'to_shop' else 0
    comp_duty = a_comp_duty + b_comp_duty

    # Applied money, split by direction and validated against order and parties.
    customer_paid = 0
    transfer_paid = 0          # A -> B processing fees already applied
    comp_paid = 0
    recovery_paid = 0         # B -> A clawbacks already applied
    held = []                 # tickets that exist but must not clear anything
    for ref in refs:
        head = ref.events[0]
        amount = head['amount']
        if len({e['amount'] for e in ref.events}) != 1 \
                or len({(e['payer'], e['payee']) for e in ref.events}) != 1:
            raise ValueError(f"bank reference {ref.ref}: ticket events disagree on amount or parties")
        if ref.bounced:
            held.append({'bank_ref': ref.ref, 'reason': 'bounce ticket received', 'amount': amount})
            continue
        if not ref.posted:
            held.append({'bank_ref': ref.ref, 'reason': 'bank received ticket is not applied',
                         'amount': amount})
            continue
        if head['order_id'] != order_id:
            held.append({'bank_ref': ref.ref, 'reason': 'movement bound to another order',
                         'amount': amount})
            continue
        if head['direction'] == 'customer_payment' and head['payer'] == order['customer'] \
                and head['payee'] == shop_a:
            customer_paid += amount
        elif head['direction'] == 'shop_transfer' and shop_b and head['payer'] == shop_a \
                and head['payee'] == shop_b:
            transfer_paid += amount
        elif head['direction'] == 'compensation' and head['payee'] == order['customer']:
            # Payout money only clears after a signed liability protocol exists
            # and the paying shop is the shop that accepted the duty.
            if liable_party and head['payer'] == liable_party:
                comp_paid += amount
            else:
                held.append({'bank_ref': ref.ref,
                             'reason': 'compensation posting precedes signed liability protocol',
                             'amount': amount})
        elif head['direction'] == 'recovery' and shop_b and head['payer'] == shop_b \
                and head['payee'] == shop_a:
            if liable_party == shop_b:
                recovery_paid += amount
            else:
                held.append({'bank_ref': ref.ref, 'reason': 'recovery without B liability protocol',
                             'amount': amount})
        else:
            held.append({'bank_ref': ref.ref, 'reason': 'payer/payee do not match order duties',
                         'amount': amount})

    # Contractual dues under the current liability version.
    a_due = price - fee - a_comp_duty
    b_due = fee - b_comp_duty

    # What each shop has actually received so far.  Recovery moves B->A and
    # cancels out of the two-shop total; compensation leaves the shop that
    # signed the current liability protocol.
    a_comp_paid = comp_paid if liability and liability['liable_shop'] == 'from_shop' else 0
    b_comp_paid = comp_paid if liability and liability['liable_shop'] == 'to_shop' else 0
    a_received = customer_paid - transfer_paid + recovery_paid - a_comp_paid
    b_received = transfer_paid - recovery_paid - b_comp_paid

    pending_transfer = max(0, fee - transfer_paid)
    pending_comp = max(0, comp_duty - comp_paid)
    # If B already received the full fee and a later version moves the payout
    # duty to B, the historical transfer stays on the books.  The clawback
    # claim covers both the fee slice now attributable to the payout and any
    # compensation A already paid under the superseded version; it is settled
    # by a B->A recovery posting, never by rewriting the old transfer.
    recovery_receivable = max(0, b_received - b_due) if liability \
        and liability['liable_shop'] == 'to_shop' else 0

    frozen_share = comp['frozen'] if comp else 0

    problems = []
    if not agreement:
        problems.append('signed cross-shop processing agreement missing')
    elif agreement['from_signer'] == agreement['to_signer'] or not agreement['from_signer'] \
            or not agreement['to_signer']:
        problems.append('processing agreement lacks two independent signatures')
    if custody_gap:
        problems.append('cross-shop operation lacks valid signatures from both shops')
    if comp and comp['state'] in ('liable', 'review') and not liability:
        problems.append('compensation duty exists but no signed liability protocol')
    if liability and (not liability['from_signer'] or not liability['to_signer']
                      or liability['from_signer'] == liability['to_signer']):
        problems.append('liability protocol lacks two independent signatures')
    if held:
        problems.append('bank tickets held or bounced')
    if customer_paid < price:
        problems.append('customer payment not fully applied')
    if not custody_gap and agreement and transfer_paid < fee:
        problems.append('processing fee transfer pending')
    if comp_duty and comp_paid < comp_duty:
        problems.append('compensation payout pending')
    if recovery_receivable > recovery_paid:
        problems.append('re-adjudication clawback claim outstanding')

    state = 'closed' if not problems else ('frozen' if frozen_share and not comp_duty else 'open')
    reason = 'all signed duties matched by applied bank postings' if not problems else '; '.join(problems)

    payload = {
        'order_id': order_id,
        'garment_id': garment_id,
        'customer': order['customer'],
        'receiving_shop': shop_a,
        'processing_shop': shop_b,
        'agreement_fee': fee,
        'agreement_version': agreement['version'] if agreement else None,
        'liability_version': liability['version'] if liability else None,
        'liable_shop': liability['liable_shop'] if liability else None,
        'customer_account': {'order_price': price, 'paid': customer_paid,
                             'refund_due': comp_duty, 'refund_paid': comp_paid},
        'receiving_shop': {'due': a_due, 'received': a_received,
                           'compensation_duty': a_comp_duty, 'compensation_paid': a_comp_paid},
        'processing_shop': {'due': b_due, 'received': b_received,
                            'compensation_duty': b_comp_duty, 'compensation_paid': b_comp_paid},
        'pending_transfer': pending_transfer,
        'pending_compensation': pending_comp,
        'recovery_receivable': recovery_receivable,
        'recovery_received': recovery_paid,
        'frozen_share': frozen_share,
        'held_tickets': held,
        'conservation': {
            'customer_net': customer_paid - comp_paid,
            'shops_retained_total': a_received + b_received,
            'expected': price - comp_duty,
        },
    }
    version = liability['version'] if liability else (agreement['version'] if agreement else 1)
    return _store_decision(db, order_id, garment_id, version, state, reason, payload)


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('orders', []):
            if record(db, row['event_id'], 'shop_order', row):
                _amount(row, 'amount')
                db.execute('INSERT INTO shop_order VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], _require(row, 'order_id'), _require(row, 'garment_id'),
                            _require(row, 'customer'), _require(row, 'receiving_shop'),
                            row['amount'], _require(row, 'at')))
            touched.add(row['garment_id'])
        for row in data.get('agreements', []):
            if record(db, row['event_id'], 'processing_agreement', row):
                if not _require(row, 'from_signer') or not _require(row, 'to_signer') \
                        or row['from_signer'] == row['to_signer']:
                    raise ValueError(f"{row['event_id']}: agreement needs two independent signers")
                db.execute('INSERT INTO processing_agreement VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], _require(row, 'order_id'), _require(row, 'garment_id'),
                            _require(row, 'from_shop'), _require(row, 'to_shop'), _amount(row, 'fee'),
                            row['from_signer'], row['to_signer'], _require(row, 'signed_at'),
                            int(row.get('version', 1))))
            touched.add(row['garment_id'])
        for row in data.get('movements', []):
            if record(db, row['event_id'], 'bank_movement', row):
                _validate_movement(row)
                if row['kind'] == 'bounce':
                    target = db.execute('SELECT bank_ref FROM bank_movement WHERE event_id=?',
                                        (_require(row, 'reverses_event'),)).fetchone()
                    if not target:
                        raise ValueError(f"{row['event_id']}: bounce references unknown event")
                    if target['bank_ref'] != row['bank_ref']:
                        raise ValueError(f"{row['event_id']}: bounce bank_ref must match original ticket")
                db.execute('INSERT INTO bank_movement VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['order_id'], row['garment_id'], row['direction'],
                            row['kind'], row['payer'], row['payee'], row['amount'], row['bank_ref'],
                            row['at'], row.get('reverses_event')))
            touched.add(row['garment_id'])
        for row in data.get('liabilities', []):
            if record(db, row['event_id'], 'liability_decision', row):
                if _require(row, 'liable_shop') not in LIABLE_SHOPS:
                    raise ValueError(f"{row['event_id']}: bad liable_shop")
                if not _require(row, 'from_signer') or not _require(row, 'to_signer') \
                        or row['from_signer'] == row['to_signer']:
                    raise ValueError(f"{row['event_id']}: liability needs two independent signers")
                db.execute('INSERT INTO liability_decision VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], _require(row, 'order_id'), _require(row, 'garment_id'),
                            int(row.get('version', 1)), row['liable_shop'], _amount(row, 'amount'),
                            _require(row, 'reason'), row['from_signer'], row['to_signer'],
                            _require(row, 'signed_at')))
            touched.add(row['garment_id'])
        return [decide(db, g) for g in sorted(touched)]


def import_file(db, command, raw):
    if raw.lstrip()[:1] in (b'{', b'['):
        import json as _json
        data = _json.loads(raw)
    else:
        data = parse_csv(raw)
    file_record(db, command, raw)
    return import_data(db, data)
