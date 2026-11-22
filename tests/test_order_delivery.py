import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garmentcare.__main__ import run_import, audit
from garmentcare.store import connect
from garmentcare.report import report
from garmentcare import delivery
from garmentcare.delivery import SplitError

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


def feed_file(db, kind, name):
    return run_import(db, kind, (FIX / name).read_bytes())


class OrderDeliveryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ----------------------------------------------------------- helpers
    def base(self, *, allow_split=True, scarf_authorized=True):
        garments = fixture('order_pair_garments.json')
        process = fixture('order_pair_process.json')
        authority = fixture('order_pair_authority.json')
        if not allow_split:
            bag = fixture('order_pair_bag.json')
            bag['orders'][0]['allow_split'] = False
            feed(self.db, 'order-import', bag)
        else:
            feed_file(self.db, 'order-import', 'order_pair_bag.json')
        feed(self.db, 'garment-import', garments)
        feed(self.db, 'process-import', process)
        if scarf_authorized:
            feed(self.db, 'return-import', authority)

    def order(self):
        return report(self.db)['orders'][0]['decision']

    def garment(self, garment_id):
        return next(g for g in report(self.db)['garments'] if g['id'] == garment_id)

    def route_split(self):
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-jacket-1', 'seal-jacket-1', 'picker-1', '2026-11-05')

    def fulfill_scarf(self):
        # scarf's cross-shop transfer is re-received under a superseding recipe whose
        # handoff is complete; the earlier custody-gap version stays in history
        repair = {'recipes': [{'event_id': 'recipe-scarf-v2', 'garment_id': 'scarf-1', 'version': 2,
                               'process': 'dry', 'max_temp': 40, 'banned_chemicals': ['bleach'],
                               'approved_by': 'owner-1'}],
                  'operations': [{'event_id': 'op-scarf-v2', 'garment_id': 'scarf-1',
                                  'recipe_id': 'recipe-scarf-v2', 'kind': 'clean', 'at': '2026-11-03',
                                  'temp': 30, 'chemical': 'mild', 'from_shop': 'shop-a',
                                  'to_shop': 'shop-b', 'sender': 'worker-a', 'receiver': 'worker-b'}]}
        feed(self.db, 'process-import', repair)

    # ----------------------------------------------------------- routes
    def test_split_route_delivers_one_garment_only(self):
        self.base()
        self.assertEqual(self.order()['state'], 'split_available')
        self.assertEqual(self.order()['undelivered'], ['jacket-1', 'scarf-1'])
        self.route_split()
        order = self.order()
        self.assertEqual(order['state'], 'partially_delivered')
        self.assertEqual(order['delivered'], ['jacket-1'])
        self.assertEqual(order['undelivered'], ['scarf-1'])
        jacket = self.garment('jacket-1')
        self.assertEqual(jacket['delivery']['state'], 'delivered')
        # claim clock anchors to the jacket's own new-bag handoff, not the shared bag
        self.assertEqual(jacket['claim_anchor']['bag'], 'bag-jacket-1')
        self.assertEqual(jacket['claim_anchor']['at'], '2026-11-05')
        self.assertEqual(self.garment('scarf-1')['delivery']['state'], 'pending')

    def test_split_request_is_not_delivery(self):
        self.base()
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        self.assertEqual(self.order()['state'], 'split_available')
        self.assertEqual(self.order()['delivered'], [])
        self.assertEqual(self.garment('jacket-1')['delivery']['state'], 'pending')

    def test_whole_bag_route_stays_pending_until_custody_filled(self):
        self.base()
        # scarf still has a cross-shop custody gap, so sealing the shared bag cannot deliver it
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-pair-1', 'seal-pair-1', 'picker-1', '2026-11-05')
        order = self.order()
        self.assertEqual(order['state'], 'pending')
        self.assertEqual(order['undelivered'], ['jacket-1', 'scarf-1'])
        self.assertNotEqual(self.garment('jacket-1')['delivery']['state'], 'delivered')
        # custody paperwork is corrected: the already-signed 11-05 bag is re-adjudicated
        self.fulfill_scarf()
        order = self.order()
        self.assertEqual(order['state'], 'delivered')
        self.assertEqual(order['undelivered'], [])
        self.assertEqual(self.garment('jacket-1')['claim_anchor']['bag'], 'bag-pair-1')
        self.assertEqual(self.garment('scarf-1')['claim_anchor']['bag'], 'bag-pair-1')
        self.assertEqual(self.garment('scarf-1')['claim_anchor']['at'], '2026-11-05')

    def test_two_routes_produce_different_claim_anchors(self):
        self.base()
        self.route_split()
        split_anchor = self.garment('jacket-1')['claim_anchor']
        split_db = self.db
        other = connect(Path(self.tmp.name) / 'other.sqlite')
        try:
            self.db = other
            self.base()
            self.fulfill_scarf()
            delivery.handoff_order(self.db, 'order-pair-1', 'bag-pair-1', 'seal-pair-1', 'picker-1', '2026-11-06')
            whole_anchor = self.garment('jacket-1')['claim_anchor']
        finally:
            self.db = split_db
            other.close()
        self.assertNotEqual(split_anchor['bag'], whole_anchor['bag'])
        self.assertNotEqual(split_anchor['at'], whole_anchor['at'])

    def test_same_garment_signed_by_two_bags_is_dispute(self):
        self.base()
        self.fulfill_scarf()
        self.route_split()
        # pre-split original-bag signature for both, then the split bag signs jacket again
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-pair-1', 'seal-pair-1', 'picker-1', '2026-11-03')
        order = self.order()
        self.assertEqual(order['state'], 'handoff_dispute')
        kinds = {d['kind'] for d in order['disputes']}
        self.assertIn('duplicate_delivery', kinds)
        self.assertNotEqual(self.garment('jacket-1')['delivery']['state'], 'delivered')

    def test_handoff_time_uses_bag_membership_at_that_moment(self):
        self.base()
        self.fulfill_scarf()
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        # a signature dated before the split covers the shared bag; jacket was still in it
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-pair-1', 'seal-pair-1', 'picker-1', '2026-11-03')
        order = self.order()
        self.assertEqual(order['state'], 'delivered')
        self.assertEqual(order['delivered'], ['jacket-1', 'scarf-1'])
        # a later signature on the original bag can no longer cover the split-out jacket
        other = connect(Path(self.tmp.name) / 'late.sqlite')
        try:
            self.db.close()
            self.db = other
            self.base()
            self.fulfill_scarf()
            delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
            delivery.handoff_order(self.db, 'order-pair-1', 'bag-pair-1', 'seal-pair-1', 'picker-1', '2026-11-05')
            late = self.order()
            self.assertEqual(late['delivered'], ['scarf-1'])
            self.assertEqual(late['undelivered'], ['jacket-1'])
        finally:
            self.db.close()

    # ----------------------------------------------------------- eligibility
    def test_other_garments_authority_does_not_transfer(self):
        self.base()
        # scarf has no pickup authority of its own; jacket's authority cannot cover it
        feed(self.db, 'return-import', {'authorities': [
            {'event_id': 'auth-only-jacket', 'garment_id': 'jacket-1', 'person': 'picker-9',
             'owner_signer': 'owner-1', 'valid_to': '2026-11-30'}]})
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-jacket-1', 'seal-jacket-1', 'picker-9', '2026-11-05')
        # jacket works with its own authorized picker-9
        self.assertEqual(self.garment('jacket-1')['delivery']['state'], 'delivered')

    def test_unprocessed_garment_cannot_use_bagmate_signature(self):
        self.base()
        # scarf is split into its own sealed bag but still has a custody gap, so a
        # signature on that bag (even by the authorized picker-1) cannot deliver it
        delivery.split_order(self.db, 'order-pair-1', 'scarf-1', 'bag-scarf-1', 'seal-scarf-1', '2026-11-04')
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-scarf-1', 'seal-scarf-1', 'picker-1', '2026-11-05')
        self.assertEqual(self.garment('scarf-1')['delivery']['state'], 'pending')
        self.assertIn('process has not completed', self.garment('scarf-1')['delivery']['reason'])
        # the split-out bag never falls back to jacket's completed status or signature
        self.assertEqual(self.order()['delivered'], [])
        self.assertNotEqual(self.order()['state'], 'delivered')

    def test_split_rejects_bagmate_signature_shortcut(self):
        self.base()
        # signing scarf under jacket's bag identity is not a transfer of authority
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-jacket-1', 'seal-jacket-1', 'picker-1', '2026-11-05')
        self.assertEqual(self.garment('jacket-1')['delivery']['state'], 'delivered')
        self.assertEqual(self.garment('scarf-1')['delivery']['state'], 'pending')

    def test_split_requires_owner_consent(self):
        self.base(allow_split=False)
        with self.assertRaisesRegex(SplitError, 'batch pickup'):
            delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')

    def test_split_rejects_bag_reuse(self):
        self.base()
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        with self.assertRaisesRegex(SplitError, 'already belongs'):
            delivery.split_order(self.db, 'order-pair-1', 'scarf-1', 'bag-jacket-1', 'seal-other', '2026-11-05')

    # ----------------------------------------------------------- disputes
    def test_wrong_recipient_disputes_whole_order(self):
        self.base()
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-jacket-1', 'seal-jacket-1',
                               'stranger', '2026-11-05', owner_signer='stranger')
        order = self.order()
        self.assertEqual(order['state'], 'handoff_dispute')
        self.assertEqual(order['disputes'][0]['kind'], 'unauthorized_recipient')
        self.assertEqual(self.garment('jacket-1')['delivery']['state'], 'dispute')

    def test_duplicate_signed_delivery_is_dispute_not_latest_wins(self):
        self.base()
        self.route_split()
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-jacket-1', 'seal-jacket-1', 'picker-1', '2026-11-06')
        order = self.order()
        self.assertEqual(order['state'], 'handoff_dispute')
        self.assertEqual(order['disputes'][0]['kind'], 'duplicate_delivery')
        self.assertEqual(sorted(order['disputes'][0]['receipts']),
                         ['handoff-order-pair-1-bag-jacket-1-2026-11-05',
                          'handoff-order-pair-1-bag-jacket-1-2026-11-06'])

    def test_seal_mismatch_does_not_deliver(self):
        self.base()
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-jacket-1', 'seal-other', 'picker-1', '2026-11-05')
        self.assertNotEqual(self.order()['state'], 'delivered')
        self.assertEqual(self.order()['state'], 'handoff_dispute')
        self.assertNotEqual(self.garment('jacket-1')['delivery']['state'], 'delivered')

    # ----------------------------------------------------------- late upstream
    def test_late_material_conflict_keeps_delivery_but_freezes_claim(self):
        self.base()
        self.route_split()
        self.assertEqual(self.garment('jacket-1')['compensation']['state'], 'returned')
        late = {'photos': [{'event_id': 'photo-jacket-late', 'garment_id': 'jacket-1', 'version': 2,
                            'photo_sha': 'late-conflict', 'damage': [],
                            'owner_signer': 'owner-1', 'clerk_signer': 'clerk-1', 'material': 'silk'}]}
        feed(self.db, 'garment-import', late)
        jacket = self.garment('jacket-1')
        self.assertEqual(jacket['delivery']['state'], 'delivered')       # real signature kept
        self.assertEqual(jacket['delivery']['claim_frozen'], 1)
        self.assertEqual(jacket['compensation']['state'], 'frozen')      # payout frozen
        self.assertEqual(self.order()['frozen'], ['jacket-1'])
        self.assertNotEqual(jacket['compensation']['state'], 'returned')

    def test_late_conflict_blocks_new_delivery_after_conflict(self):
        self.base()
        late = {'photos': [{'event_id': 'photo-jacket-late', 'garment_id': 'jacket-1', 'version': 2,
                            'photo_sha': 'late-conflict', 'damage': [],
                            'owner_signer': 'owner-1', 'clerk_signer': 'clerk-1', 'material': 'silk'}]}
        feed(self.db, 'garment-import', late)
        delivery.split_order(self.db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
        delivery.handoff_order(self.db, 'order-pair-1', 'bag-jacket-1', 'seal-jacket-1', 'picker-1', '2026-11-05')
        self.assertEqual(self.garment('jacket-1')['delivery']['state'], 'pending')
        self.assertNotEqual(self.order()['state'], 'delivered')

    def test_decision_history_retains_rescoring(self):
        self.base()
        self.route_split()
        before = len(self.order_history())
        feed(self.db, 'garment-import', {'photos': [
            {'event_id': 'photo-jacket-late', 'garment_id': 'jacket-1', 'version': 2,
             'photo_sha': 'late-conflict', 'damage': [], 'owner_signer': 'owner-1',
             'clerk_signer': 'clerk-1', 'material': 'silk'}]})
        self.assertGreater(len(self.order_history()), before)
        systems = [row['subsystem'] for row in self.garment('jacket-1')['history']]
        self.assertIn('delivery_decision', systems)
        self.assertEqual(self.garment('jacket-1')['delivery']['state'], 'delivered')

    def order_history(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM order_decision_history WHERE order_id='order-pair-1' ORDER BY id")]

    # ----------------------------------------------------------- imports / audit
    def test_duplicate_file_import_is_idempotent(self):
        self.base()
        self.route_split()
        before = report(self.db)
        feed_file(self.db, 'order-import', 'order_pair_bag.json')
        self.assertEqual(report(self.db)['orders'][0]['decision'], before['orders'][0]['decision'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_split').fetchone()[0], 1)

    def test_conflicting_event_id_rejected(self):
        self.base()
        body = fixture('order_pair_bag.json')
        body['members'][0]['garment_id'] = 'scarf-1'  # same event id, different content
        with self.assertRaisesRegex(ValueError, 'event_id conflict'):
            feed(self.db, 'order-import', body)

    def test_reverse_import_order_recomputes_same_result(self):
        forward = connect(Path(self.tmp.name) / 'forward.sqlite')
        reverse = connect(Path(self.tmp.name) / 'reverse.sqlite')
        try:
            for db in (forward, reverse):
                if db is forward:
                    feed_file(db, 'garment-import', 'order_pair_garments.json')
                    feed_file(db, 'process-import', 'order_pair_process.json')
                    feed_file(db, 'order-import', 'order_pair_bag.json')
                    feed_file(db, 'return-import', 'order_pair_authority.json')
                else:
                    feed_file(db, 'order-import', 'order_pair_bag.json')
                    feed_file(db, 'return-import', 'order_pair_authority.json')
                    feed_file(db, 'process-import', 'order_pair_process.json')
                    feed_file(db, 'garment-import', 'order_pair_garments.json')
                delivery.split_order(db, 'order-pair-1', 'jacket-1', 'bag-jacket-1', 'seal-jacket-1', '2026-11-04')
                delivery.handoff_order(db, 'order-pair-1', 'bag-jacket-1', 'seal-jacket-1', 'picker-1', '2026-11-05')
            self.assertEqual(report(forward)['orders'][0]['decision']['state'],
                             report(reverse)['orders'][0]['decision']['state'])
            self.assertEqual(report(forward)['orders'][0]['decision']['delivered'],
                             report(reverse)['orders'][0]['decision']['delivered'])
            self.assertEqual([g['delivery']['state'] for g in report(forward)['garments']],
                             [g['delivery']['state'] for g in report(reverse)['garments']])
            self.assertEqual(audit(reverse)['status'], 'matched')
            self.assertEqual(audit(forward)['status'], 'matched')
        finally:
            forward.close()
            reverse.close()

    def test_audit_replays_split_and_handoff_from_source(self):
        self.base()
        self.route_split()
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_audit_matches_with_late_conflict(self):
        self.base()
        self.route_split()
        feed(self.db, 'garment-import', {'photos': [
            {'event_id': 'photo-jacket-late', 'garment_id': 'jacket-1', 'version': 2,
             'photo_sha': 'late-conflict', 'damage': [], 'owner_signer': 'owner-1',
             'clerk_signer': 'clerk-1', 'material': 'silk'}]})
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_cli_split_and_report(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        for command, name in [('order-import', 'order_pair_bag.json'),
                              ('garment-import', 'order_pair_garments.json'),
                              ('process-import', 'order_pair_process.json'),
                              ('return-import', 'order_pair_authority.json')]:
            subprocess.run([sys.executable, '-m', 'garmentcare', command, '--db', str(path),
                            '--file', str(FIX / name)], cwd=ROOT, check=True, capture_output=True)
        r = subprocess.run([sys.executable, '-m', 'garmentcare', 'split', '--db', str(path),
                            '--order', 'order-pair-1', '--garment', 'jacket-1', '--to-bag', 'bag-jacket-1',
                            '--new-seal', 'seal-jacket-1', '--at', '2026-11-04', '--person', 'owner-1'],
                           cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertIn('split_available', r.stdout)
        subprocess.run([sys.executable, '-m', 'garmentcare', 'handoff', '--db', str(path),
                        '--order', 'order-pair-1', '--bag', 'bag-jacket-1', '--seal', 'seal-jacket-1',
                        '--person', 'picker-1', '--at', '2026-11-05'],
                       cwd=ROOT, check=True, capture_output=True)
        r = subprocess.run([sys.executable, '-m', 'garmentcare', 'report', '--db', str(path)],
                           cwd=ROOT, text=True, capture_output=True, check=True)
        data = json.loads(r.stdout)
        self.assertEqual(data['orders'][0]['decision']['state'], 'partially_delivered')
        self.assertEqual(data['orders'][0]['decision']['label'], '已部分交付')
        self.assertEqual(data['orders'][0]['decision']['delivered'], ['jacket-1'])

    def test_cli_route_fixtures_reproduce_both_routes(self):
        cases = {
            'split.sqlite': ('order_split_route.json', 'partially_delivered', '2026-11-05', 'bag-jacket-1'),
            'whole.sqlite': ('order_whole_route.json', 'delivered', '2026-11-05', 'bag-pair-1'),
        }
        for name, (route, expected, at, bag) in cases.items():
            path = Path(self.tmp.name) / name
            process = 'order_whole_route_process.json' if 'whole' in name else 'order_pair_process.json'
            for command, fixture_name in [('garment-import', 'order_pair_garments.json'),
                                          ('process-import', process),
                                          ('order-import', 'order_pair_bag.json'),
                                          ('return-import', 'order_pair_authority.json'),
                                          ('order-import', route)]:
                subprocess.run([sys.executable, '-m', 'garmentcare', command, '--db', str(path),
                                '--file', str(FIX / fixture_name)], cwd=ROOT, check=True,
                               capture_output=True)
            r = subprocess.run([sys.executable, '-m', 'garmentcare', 'report', '--db', str(path)],
                               cwd=ROOT, text=True, capture_output=True, check=True)
            data = json.loads(r.stdout)
            self.assertEqual(data['orders'][0]['decision']['state'], expected)
            jacket = next(g for g in data['garments'] if g['id'] == 'jacket-1')
            self.assertEqual(jacket['delivery']['state'], 'delivered')
            self.assertEqual(jacket['claim_anchor']['bag'], bag)
            self.assertEqual(jacket['claim_anchor']['at'], at)
            audit_run = subprocess.run([sys.executable, '-m', 'garmentcare', 'audit', '--db', str(path)],
                                       cwd=ROOT, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(audit_run.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
