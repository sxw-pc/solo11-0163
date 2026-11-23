import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garmentcare.__main__ import run_import, audit
from garmentcare.store import connect
from garmentcare.report import report
from garmentcare.shop_settlement import parse_csv

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'
SETTLE = ['settlement_garments.json', 'settlement_process.json', 'settlement_returns.json']


def load(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, data):
    raw = data if isinstance(data, (bytes, bytearray)) else json.dumps(data, ensure_ascii=False).encode()
    return run_import(db, kind, raw)


class SettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        feed(self.db, 'garment-import', load('settlement_garments.json'))
        feed(self.db, 'process-import', load('settlement_process.json'))
        feed(self.db, 'return-import', load('settlement_returns.json'))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def settle(self, order_id):
        for row in report(self.db)['settlements']:
            if row['order_id'] == order_id:
                return row
        raise AssertionError(f'no settlement for {order_id}')

    def feed_settlement(self, name):
        feed(self.db, 'settlement-import', (FIX / name).read_bytes())

    def assert_conservation(self, p):
        c = p['conservation']
        self.assertEqual(c['customer_net'], c['expected'])
        self.assertEqual(c['shops_retained_total'], c['expected'])

    def test_route_a_receiving_shop_bears_payout(self):
        self.feed_settlement('settlement_route_a.json')
        s = self.settle('order-a')
        self.assertEqual(s['state'], 'closed')
        p = s['payload']
        self.assertEqual(p['customer_account'], {'order_price': 600, 'paid': 600, 'refund_due': 100, 'refund_paid': 100})
        self.assertEqual(p['receiving_shop']['due'], 200)
        self.assertEqual(p['receiving_shop']['received'], 200)
        self.assertEqual(p['processing_shop']['due'], 300)
        self.assertEqual(p['processing_shop']['received'], 300)
        self.assertEqual(p['liable_shop'], 'from_shop')
        self.assert_conservation(p)

    def test_route_b_csv_processing_shop_bears_payout(self):
        self.feed_settlement('settlement_route_b.csv')
        s = self.settle('order-b')
        self.assertEqual(s['state'], 'closed')
        p = s['payload']
        self.assertEqual(p['customer_account']['paid'], 600)
        self.assertEqual(p['customer_account']['refund_paid'], 100)
        self.assertEqual(p['receiving_shop']['due'], 300)
        self.assertEqual(p['receiving_shop']['received'], 300)
        self.assertEqual(p['processing_shop']['due'], 200)
        self.assertEqual(p['processing_shop']['received'], 200)
        self.assertEqual(p['liable_shop'], 'to_shop')
        self.assertEqual(p['recovery_receivable'], 0)
        self.assert_conservation(p)

    def test_bounce_ticket_voids_payout_until_replacement_applied(self):
        self.feed_settlement('settlement_bounce_early.json')
        before = self.settle('order-c')
        self.assertIn('bounce', before['payload']['held_tickets'][0]['reason'])
        self.assertEqual(before['payload']['customer_account']['refund_paid'], 0)
        self.assertEqual(before['state'], 'open')
        self.feed_settlement('settlement_bounce_repay.json')
        after = self.settle('order-c')
        self.assertEqual(after['payload']['customer_account']['refund_paid'], 100)
        # The bounced reference stays on the books as a held ticket even after
        # the replacement posting clears the money.
        bounced = [t for t in after['payload']['held_tickets'] if 'bounce' in t['reason']]
        self.assertEqual(len(bounced), 1)
        self.assert_conservation(after['payload'])

    def test_early_compensation_ticket_is_held_not_amount_matched(self):
        self.feed_settlement('settlement_bounce_early.json')
        d = self.settle('order-d')
        held = d['payload']['held_tickets']
        self.assertTrue(any(t['bank_ref'] == 'ref-early-d' for t in held))
        self.assertEqual(d['payload']['customer_account']['paid'], 0)
        self.assertEqual(d['payload']['pending_transfer'], 300)
        d = self.settle('order-d')
        c = self.settle('order-c')
        # The equal-amount 100 ticket on order-c is never offered to order-d.
        self.assertTrue(all(t['bank_ref'] != 'ref-comp-c' for t in d['payload']['held_tickets']))
        # On order-c the ticket is held only because it later bounced, not because
        # of any amount match with order-d.
        self.assertTrue(all('bounce' in t['reason'] for t in c['payload']['held_tickets']))

    def test_cross_order_same_amount_does_not_double_offset(self):
        self.feed_settlement('settlement_cross_order.json')
        g = self.settle('order-g')
        stray = [t for t in g['payload']['held_tickets'] if t['bank_ref'] == 'ref-stray-g']
        self.assertEqual(len(stray), 1)
        self.assertEqual(g['payload']['pending_compensation'], 100)
        self.assertEqual(g['payload']['customer_account']['refund_paid'], 0)

    def test_readjudication_creates_clawback_without_rewriting_transfer(self):
        self.feed_settlement('settlement_rejudge_v1.json')
        v1 = self.settle('order-e')
        self.assertEqual(v1['state'], 'closed')
        self.assertEqual(v1['payload']['processing_shop']['received'], 300)
        # The original A->B transfer fact stays on the ledger.
        transfers = self.db.execute(
            "SELECT COUNT(*) FROM bank_movement WHERE order_id='order-e' AND direction='shop_transfer' AND kind='applied'").fetchone()[0]
        self.assertEqual(transfers, 1)
        self.feed_settlement('settlement_rejudge_v2.json')
        v2 = self.settle('order-e')
        self.assertEqual(v2['version'], 2)
        self.assertEqual(v2['payload']['liable_shop'], 'to_shop')
        self.assertEqual(v2['payload']['recovery_receivable'], 100)
        self.assertEqual(v2['state'], 'open')
        # Historical transfer (received + applied) stays unchanged.
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM bank_movement WHERE order_id='order-e' AND direction='shop_transfer'").fetchone()[0], 2)
        self.assertEqual(self.db.execute(
            "SELECT amount FROM bank_movement WHERE order_id='order-e' AND direction='shop_transfer' AND kind='applied'").fetchone()['amount'], 300)
        self.feed_settlement('settlement_rejudge_recovery.json')
        v3 = self.settle('order-e')
        self.assertEqual(v3['payload']['recovery_received'], 100)
        self.assertEqual(v3['payload']['processing_shop']['received'], 200)
        self.assertEqual(v3['payload']['receiving_shop']['received'], 400)
        self.assertEqual(v3['payload']['recovery_receivable'], 0)

    def test_custody_gap_blocks_processing_fee_settlement(self):
        # suit-f cross-shop operation has a sender but no independent receiver.
        self.feed_settlement('settlement_route_a.json')
        f = self.db.execute("SELECT state FROM process_decision WHERE garment_id='suit-f'").fetchone()
        self.assertEqual(f['state'], 'custody_gap')
        self.feed_settlement('settlement_cross_order.json')
        # No order-f settlement rows were created without an order, and importing
        # an agreement for suit-f still cannot settle while custody is unsigned.
        data = {
            'orders': [{'event_id': 'ord-f', 'order_id': 'order-f', 'garment_id': 'suit-f',
                        'customer': 'cust-f', 'receiving_shop': 'shop-a', 'amount': 600, 'at': '2026-11-01'}],
            'agreements': [{'event_id': 'agr-f', 'order_id': 'order-f', 'garment_id': 'suit-f',
                            'from_shop': 'shop-a', 'to_shop': 'shop-b', 'fee': 300,
                            'from_signer': 'mgr-a', 'to_signer': 'mgr-b', 'signed_at': '2026-11-01', 'version': 1}],
            'movements': [
                {'event_id': 'mv-f-pay', 'order_id': 'order-f', 'garment_id': 'suit-f',
                 'direction': 'customer_payment', 'kind': 'applied', 'payer': 'cust-f',
                 'payee': 'shop-a', 'amount': 600, 'bank_ref': 'ref-pay-f', 'at': '2026-11-01T12:00'},
                {'event_id': 'mv-f-fee', 'order_id': 'order-f', 'garment_id': 'suit-f',
                 'direction': 'shop_transfer', 'kind': 'applied', 'payer': 'shop-a',
                 'payee': 'shop-b', 'amount': 300, 'bank_ref': 'ref-fee-f', 'at': '2026-11-06T12:00'}]}
        feed(self.db, 'settlement-import', data)
        sf = self.settle('order-f')
        self.assertEqual(sf['state'], 'open')
        self.assertIn('both shops', sf['reason'])

    def test_frozen_compensation_share_is_never_treated_as_paid(self):
        # A compliant garment (no deviation, custody signed) with a new-damage
        # claim reaches the review state, so returns.py freezes the share.
        gid = 'suit-frozen'
        feed(self.db, 'garment-import', {
            'garments': [{'event_id': 'g-frozen', 'id': gid, 'owner': 'cust-z', 'material': 'wool',
                          'accepted_at': '2026-11-01', 'declared_value': 2000, 'cap_rate': 100,
                          'allowed_process': 'dry', 'pickup_person': 'pick-z',
                          'claim_until': '2026-11-20', 'expected_bag': 'bag-frozen'}],
            'photos': [{'event_id': 'ph-frozen', 'garment_id': gid, 'version': 1,
                        'photo_sha': 'sha-z', 'damage': ['old-scuff'],
                        'owner_signer': 'cust-z', 'clerk_signer': 'clerk-a', 'material': 'wool'}]})
        feed(self.db, 'process-import', {
            'recipes': [{'event_id': 'rec-frozen', 'garment_id': gid, 'version': 1, 'process': 'dry',
                         'max_temp': 40, 'banned_chemicals': ['bleach'], 'approved_by': 'cust-z'}],
            'operations': [{'event_id': 'op-frozen', 'garment_id': gid, 'recipe_id': 'rec-frozen',
                            'kind': 'clean', 'at': '2026-11-02', 'temp': 30, 'chemical': 'mild',
                            'from_shop': 'shop-a', 'to_shop': 'shop-b',
                            'sender': 'worker-a', 'receiver': 'worker-b'}]})
        feed(self.db, 'return-import', {
            'authorities': [{'event_id': 'au-frozen', 'garment_id': gid, 'person': 'pick-z',
                             'owner_signer': 'cust-z', 'valid_to': '2026-11-20'}],
            'receipts': [{'event_id': 'rt-frozen', 'garment_id': gid, 'person': 'pick-z',
                          'at': '2026-11-04', 'bag': 'bag-frozen', 'condition': 'returned',
                          'owner_signer': 'pick-z'}],
            'claims': [{'event_id': 'cl-frozen', 'garment_id': gid, 'receipt_id': 'rt-frozen',
                        'at': '2026-11-05', 'photo_sha': 'x', 'damage': 'tear', 'requested_amount': 100}]})
        comp = self.db.execute("SELECT state,frozen,payable FROM compensation_decision WHERE garment_id=?",
                               (gid,)).fetchone()
        self.assertEqual(comp['state'], 'review')
        self.assertEqual(comp['frozen'], 100)
        self.assertEqual(comp['payable'], 0)
        data = {
            'orders': [{'event_id': 'ord-frozen', 'order_id': 'order-frozen', 'garment_id': gid,
                        'customer': 'cust-z', 'receiving_shop': 'shop-a', 'amount': 600, 'at': '2026-11-01'}],
            'agreements': [{'event_id': 'agr-frozen', 'order_id': 'order-frozen', 'garment_id': gid,
                            'from_shop': 'shop-a', 'to_shop': 'shop-b', 'fee': 300,
                            'from_signer': 'mgr-a', 'to_signer': 'mgr-b', 'signed_at': '2026-11-01', 'version': 1}],
            'movements': [
                {'event_id': 'mv-fz-pay', 'order_id': 'order-frozen', 'garment_id': gid,
                 'direction': 'customer_payment', 'kind': 'applied', 'payer': 'cust-z',
                 'payee': 'shop-a', 'amount': 600, 'bank_ref': 'ref-pay-fz', 'at': '2026-11-01T12:00'}]}
        feed(self.db, 'settlement-import', data)
        s = self.settle('order-frozen')
        self.assertEqual(s['payload']['frozen_share'], 100)
        self.assertEqual(s['payload']['customer_account']['refund_paid'], 0)
        self.assertEqual(s['state'], 'frozen')

    def test_received_without_applied_does_not_count(self):
        data = {
            'orders': [{'event_id': 'ord-r', 'order_id': 'order-r', 'garment_id': 'suit-b',
                        'customer': 'cust-b', 'receiving_shop': 'shop-a', 'amount': 600, 'at': '2026-11-01'}],
            'agreements': [{'event_id': 'agr-r', 'order_id': 'order-r', 'garment_id': 'suit-b',
                            'from_shop': 'shop-a', 'to_shop': 'shop-b', 'fee': 300,
                            'from_signer': 'mgr-a', 'to_signer': 'mgr-b', 'signed_at': '2026-11-01', 'version': 1}],
            'movements': [{'event_id': 'mv-r-1', 'order_id': 'order-r', 'garment_id': 'suit-b',
                           'direction': 'customer_payment', 'kind': 'received', 'payer': 'cust-b',
                           'payee': 'shop-a', 'amount': 600, 'bank_ref': 'ref-r', 'at': '2026-11-01T10:00'}]}
        feed(self.db, 'settlement-import', data)
        s = self.settle('order-r')
        self.assertEqual(s['payload']['customer_account']['paid'], 0)
        self.assertTrue(any('not applied' in t['reason'] for t in s['payload']['held_tickets']))

    def test_idempotent_replay_and_conflicting_event_rejected(self):
        self.feed_settlement('settlement_route_a.json')
        rows_before = self.db.execute('SELECT COUNT(*) FROM bank_movement').fetchone()[0]
        self.feed_settlement('settlement_route_a.json')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bank_movement').fetchone()[0], rows_before)
        bad = load('settlement_route_a.json')
        bad['orders'][0]['amount'] = 601
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'settlement-import', bad)

    def test_unsigned_agreement_and_liability_rejected(self):
        bad_agreement = {
            'agreements': [{'event_id': 'agr-x', 'order_id': 'order-x', 'garment_id': 'suit-a',
                            'from_shop': 'shop-a', 'to_shop': 'shop-b', 'fee': 300,
                            'from_signer': 'same', 'to_signer': 'same', 'signed_at': '2026-11-01'}]}
        with self.assertRaisesRegex(ValueError, 'two independent signers'):
            feed(self.db, 'settlement-import', bad_agreement)
        bad_liability = {
            'liabilities': [{'event_id': 'lib-x', 'order_id': 'order-a', 'garment_id': 'suit-a',
                             'version': 1, 'liable_shop': 'from_shop', 'amount': 100,
                             'reason': 'x', 'from_signer': 'm', 'to_signer': 'm', 'signed_at': '2026-11-07'}]}
        with self.assertRaisesRegex(ValueError, 'two independent signers'):
            feed(self.db, 'settlement-import', bad_liability)

    def test_bounce_must_reference_existing_ticket(self):
        bad = {'movements': [{'event_id': 'mv-bad-bounce', 'order_id': 'order-a', 'garment_id': 'suit-a',
                              'direction': 'compensation', 'kind': 'bounce', 'payer': 'shop-a',
                              'payee': 'cust-a', 'amount': 100, 'bank_ref': 'ref-x',
                              'at': '2026-11-09', 'reverses_event': 'missing-event'}]}
        with self.assertRaisesRegex(ValueError, 'unknown event'):
            feed(self.db, 'settlement-import', bad)

    def test_csv_parser_roundtrip(self):
        parsed = parse_csv((FIX / 'settlement_route_b.csv').read_bytes())
        self.assertEqual({r['event_id'] for r in parsed['orders']}, {'ord-b'})
        self.assertEqual(parsed['orders'][0]['amount'], 600)
        self.assertEqual(len(parsed['movements']), 6)
        self.assertEqual(parsed['liabilities'][0]['liable_shop'], 'to_shop')

    def test_audit_rebuilds_fund_attribution_and_versions(self):
        for name in ['settlement_route_b.csv', 'settlement_bounce_early.json',
                     'settlement_bounce_repay.json', 'settlement_rejudge_v1.json',
                     'settlement_rejudge_v2.json', 'settlement_rejudge_recovery.json',
                     'settlement_cross_order.json']:
            self.feed_settlement(name)
        result = audit(self.db)
        self.assertEqual(result['status'], 'matched')
        # Re-adjudication is preserved as ordered versions, never overwritten.
        history = [dict(r) for r in self.db.execute(
            "SELECT DISTINCT version,state FROM decision_history "
            "WHERE subsystem='shop_settlement_decision' AND garment_id='suit-e' ORDER BY version")]
        self.assertEqual([h['version'] for h in history], [1, 2])
        latest = self.settle('order-e')
        self.assertEqual(latest['version'], 2)
        self.assertEqual(latest['payload']['recovery_received'], 100)

    def test_cli_settlement_import_report_audit(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        base = [sys.executable, '-m', 'garmentcare']
        commands = [
            ('garment-import', 'settlement_garments.json'),
            ('process-import', 'settlement_process.json'),
            ('return-import', 'settlement_returns.json'),
            ('settlement-import', 'settlement_route_b.csv'),
        ]
        for kind, name in commands:
            subprocess.run(base + [kind, '--db', str(path), '--file', str(FIX / name)],
                           cwd=ROOT, check=True, capture_output=True, text=True)
        rep = subprocess.run(base + ['report', '--db', str(path)], cwd=ROOT,
                             check=True, capture_output=True, text=True)
        data = json.loads(rep.stdout)
        self.assertTrue(any(s['order_id'] == 'order-b' and s['state'] == 'closed'
                            for s in data['settlements']))
        aud = subprocess.run(base + ['audit', '--db', str(path)], cwd=ROOT,
                             check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(aud.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
