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
from garmentcare.attribution import reevaluate, campaign_totals

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


class AttributionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def garments(self):
        feed(self.db, 'garment-import', fixture('attribution_garments.json'))

    def campaign(self, name):
        self.garments()
        return feed(self.db, 'campaign-import', fixture(name))

    def map_rows(self, table, key):
        return {r[key]: dict(r) for r in self.db.execute(f'SELECT * FROM {table}')}

    def customers(self):
        return self.map_rows('customer_acquisition', 'customer')

    def bookings(self):
        return self.map_rows('acquisition_decision', 'booking_id')

    def touches(self):
        return self.map_rows('touch_classification', 'event_id')

    def redeems(self):
        return self.map_rows('qr_coupon_redeem', 'event_id')

    def assert_audit_matched(self):
        self.assertEqual(audit(self.db)['status'], 'matched')

    # ---- legacy unsigned path ---------------------------------------------
    def test_legacy_campaign_ok_stays_provisional(self):
        result = feed(self.db, 'campaign-import', fixture('campaign_ok.json'))
        row = result[0]
        self.assertEqual(row['state'], 'legacy_provisional')
        self.assertEqual(row['campaign'], 'autumn-care')
        self.assertIsNone(row['customer'])
        self.assertIn('legacy', row['reason'])
        self.assert_audit_matched()

    # ---- shared device: paper coupon closes campaign A --------------------
    def test_paper_coupon_wins_over_last_touch_b(self):
        self.campaign('attribution_paper.json')
        customers, bookings, touches = self.customers(), self.bookings(), self.touches()
        jia = customers['customer-jia']
        self.assertEqual((jia['state'], jia['campaign'], jia['evidence_kind']),
                         ('acquired', 'campaign-a', 'paper_coupon'))
        self.assertEqual(jia['source_event'], 'redeem-a')
        # the afternoon B scan is retained only as shared-device noise
        self.assertEqual(touches['touch-b']['role'], 'shared_device_noise')
        self.assertEqual(touches['touch-b']['usable'], 0)
        # the A scan is pinned to jia by the redeemed paper coupon chain
        self.assertEqual(touches['touch-a']['role'], 'pinned')
        self.assertEqual(touches['touch-a']['customer'], 'customer-jia')
        # two bookings of the same customer count as one first conversion
        self.assertEqual(bookings['booking-jia-1']['state'], 'acquired')
        self.assertEqual(bookings['booking-jia-2']['state'], 'repeat_visit')
        totals = campaign_totals(self.db)
        self.assertEqual(totals['acquired_by_campaign'], {'campaign-a': 1})
        self.assertEqual(totals['acquired_customers'], 1)
        # referral closed by the referee's own dual-signed redemption
        refs = self.map_rows('referral_credential', 'event_id')
        self.assertEqual(refs['ref-1']['state'], 'valid')
        self.assert_audit_matched()

    def test_shared_device_keeps_two_people_distinct(self):
        self.campaign('attribution_paper.json')
        links = self.db.execute('SELECT DISTINCT customer FROM identity_link ORDER BY customer').fetchall()
        self.assertEqual([r['customer'] for r in links], ['customer-jia', 'customer-spouse'])
        customers = self.customers()
        self.assertIn('customer-spouse', customers)
        self.assertEqual(customers['customer-spouse']['state'], 'no_valid_visit')

    # ---- shared device: trusted login commitment closes campaign B --------
    def test_login_commitment_attributes_to_b_pending_paper(self):
        self.campaign('attribution_login.json')
        customers, bookings, touches = self.customers(), self.bookings(), self.touches()
        jia = customers['customer-jia']
        self.assertEqual((jia['state'], jia['campaign'], jia['evidence_kind']),
                         ('acquired', 'campaign-b', 'login_commitment'))
        self.assertEqual(bookings['booking-jia-1']['campaign'], 'campaign-b')
        self.assertEqual(touches['touch-b-login']['role'], 'pinned')
        self.assertEqual(touches['touch-b-login']['customer'], 'customer-jia')
        # the A paper coupon was issued but never redeemed: pending, not counted
        self.assertEqual(touches['touch-a']['role'], 'shared_device_noise')
        issues = self.map_rows('qr_coupon_issue', 'event_id')
        self.assertEqual(issues['issue-a']['coupon_no'], 'QP-A-001')
        self.assertEqual(campaign_totals(self.db)['acquired_by_campaign'], {'campaign-b': 1})
        commits = self.map_rows('login_commitment', 'event_id')
        self.assertEqual(commits['commit-b']['state'], 'valid')
        self.assert_audit_matched()

    def test_login_commitment_without_backing_session_is_invalid(self):
        data = fixture('attribution_login.json')
        # remove the trusted login touch that backs the commitment
        data['touches'] = [t for t in data['touches'] if t['event_id'] != 'touch-b-login']
        self.garments()
        feed(self.db, 'campaign-import', data)
        commits = self.map_rows('login_commitment', 'event_id')
        self.assertEqual(commits['commit-b']['state'], 'invalid')
        self.assertEqual(self.customers()['customer-jia']['state'], 'provisional')

    # ---- contradictory evidence: disputed, never double counted ------------
    def test_contradictory_evidence_marks_dispute(self):
        self.campaign('attribution_dispute.json')
        jia = self.customers()['customer-jia']
        self.assertEqual(jia['state'], 'disputed')
        self.assertIsNone(jia['campaign'])
        self.assertIn('redeem-a', jia['reason'])
        self.assertIn('commit-b', jia['reason'])
        totals = campaign_totals(self.db)
        self.assertEqual(totals['acquired_customers'], 0)
        self.assertEqual(totals['disputed_customers'], 1)
        self.assert_audit_matched()

    # ---- cancelled store visit: facts retained, conversion removed --------
    def test_cancelled_visit_removes_conversion_but_keeps_touch_facts(self):
        data = fixture('attribution_cancel.json')
        data['store_visits'][0]['status'] = 'cancelled'
        self.garments()
        feed(self.db, 'campaign-import', data)
        self.assertEqual(self.customers()['customer-bing']['state'], 'visit_cancelled')
        self.assertEqual(self.bookings()['booking-bing-1']['state'], 'cancelled')
        # raw touch facts remain on the ledger; the redeem is invalid without an
        # accepted visit, so nothing gets pinned or counted as an acquisition
        self.assertEqual(self.touches()['touch-bing']['role'], 'unlinked')
        self.assertEqual(self.redeems()['redeem-bing']['state'], 'invalid')
        self.assertEqual(campaign_totals(self.db)['acquired_customers'], 0)
        self.assert_audit_matched()

    # ---- blocked intake: booking is not an acquisition --------------------
    def test_blocked_intake_blocks_acquisition(self):
        self.garments()
        bad = {'garments': [{'event_id': 'g-yi-block', 'id': 'coat-yi-2', 'owner': 'customer-yi',
                             'material': 'wool', 'accepted_at': '2026-11-02', 'declared_value': 9000,
                             'cap_rate': 50, 'allowed_process': 'dry', 'pickup_person': 'picker-2',
                             'claim_until': '2026-11-20', 'expected_bag': 'bag-yi-2'}],
               'photos': [{'event_id': 'p-yi-block', 'garment_id': 'coat-yi-2', 'version': 1,
                           'photo_sha': 'x', 'damage': [], 'owner_signer': 'customer-yi',
                           'clerk_signer': 'customer-yi', 'material': 'wool'}]}
        feed(self.db, 'garment-import', bad)  # clerk == owner -> intake pending
        data = copy.deepcopy(fixture('attribution_paper.json'))
        data['bookings'] = [{'event_id': 'booking-yi-1', 'visitor': 'visitor-2',
                             'garment_id': 'coat-yi-2', 'at': '2026-11-02T15:30:00'}]
        data['store_visits'] = [{'event_id': 'visit-yi-1', 'customer': 'customer-yi',
                                 'shop': 'shop-2', 'at': '2026-11-02T16:00:00',
                                 'garment_id': 'coat-yi-2', 'status': 'signed',
                                 'customer_signer': 'customer-yi', 'clerk_signer': 'clerk-2'}]
        data['identity_links'] = [{'event_id': 'link-yi', 'visitor': 'visitor-2',
                                   'customer': 'customer-yi', 'kind': 'signed_phone',
                                   'account': 'phone-yi', 'at': '2026-11-02T08:00:00'}]
        data['touches'] = [{'event_id': 'touch-yi', 'visitor': 'visitor-2',
                            'campaign': 'campaign-a', 'channel': 'qr', 'at': '2026-11-02T08:30:00'}]
        for key in ('coupon_issues', 'coupon_redeems', 'referrals',
                    'shared_device_declarations', 'login_commitments'):
            data.pop(key, None)
        feed(self.db, 'campaign-import', data)
        self.assertEqual(self.bookings()['booking-yi-1']['state'], 'intake_pending')
        self.assertEqual(campaign_totals(self.db)['acquired_customers'], 0)
        self.assert_audit_matched()

    # ---- consent withdrawal ------------------------------------------------
    def test_consent_withdrawal_excludes_later_web_touch_only(self):
        self.campaign('attribution_consent.json')
        touches = self.touches()
        self.assertEqual(touches['touch-ding-1']['role'], 'unlinked')  # pre-withdrawal, still usable
        self.assertEqual(touches['touch-ding-2']['role'], 'withdrawn')
        self.assertEqual(touches['touch-ding-2']['usable'], 0)
        # no signed closing evidence -> provisional, never a claimed acquisition
        self.assertEqual(self.customers()['customer-ding']['state'], 'provisional')
        self.assertEqual(self.bookings()['booking-ding-1']['state'], 'provisional')
        self.assert_audit_matched()

    # ---- coupon number collision resolved by shop + campaign ---------------
    def test_coupon_collision_requires_issuing_shop_and_source(self):
        self.campaign('attribution_fraud.json')
        redeems = self.redeems()
        # redeemed at shop-2 but the campaign-a issue belongs to shop-1
        self.assertEqual(redeems['redeem-collide']['state'], 'invalid')
        self.assertIn('collision', redeems['redeem-collide']['reason'])
        # the campaign-b / shop-2 issue with the same number still exists untouched
        issues = self.map_rows('qr_coupon_issue', 'event_id')
        self.assertEqual(issues['issue-collide-2']['shop'], 'shop-2')

    # ---- forwarded screenshot cannot be redeemed twice --------------------
    def test_forwarded_screenshot_is_not_a_second_receipt(self):
        self.campaign('attribution_fraud.json')
        redeems = self.redeems()
        # first presentment of QP-X-2 is a genuine dual-signed redemption...
        self.assertEqual(redeems['redeem-shot-1']['state'], 'valid')
        # ...the re-presented screenshot of the same coupon must be rejected
        self.assertEqual(redeems['redeem-shot-2']['state'], 'invalid')
        self.assertIn('already redeemed', redeems['redeem-shot-2']['reason'])

    # ---- referral credentials ----------------------------------------------
    def test_self_referral_and_wrong_referee_are_invalid(self):
        self.campaign('attribution_fraud.json')
        refs = self.map_rows('referral_credential', 'event_id')
        self.assertEqual(refs['ref-self']['state'], 'invalid')
        self.assertEqual(refs['ref-wrong']['state'], 'invalid')
        # a redemption citing an invalid referee credential is itself invalid
        self.assertEqual(self.redeems()['redeem-ref-wrong']['state'], 'invalid')

    def test_pending_referral_is_listed_separately_from_acquisitions(self):
        # paper fixture's ref-1 closes valid; build an unverified one instead
        data = fixture('attribution_paper.json')
        data['coupon_redeems'][0]['referral_event_id'] = None
        self.garments()
        feed(self.db, 'campaign-import', data)
        refs = self.map_rows('referral_credential', 'event_id')
        self.assertEqual(refs['ref-1']['state'], 'pending_verification')
        payload = report(self.db)
        self.assertTrue(any(r['state'] == 'pending_verification' for r in payload['referrals']))

    # ---- evidence recomputation across imports -----------------------------
    def test_referrer_and_referee_stay_distinct_on_shared_device(self):
        data = fixture('attribution_paper.json')
        # the referrer customer-yi also uses the same household phone
        data['identity_links'].append(
            {'event_id': 'link-yi-shared', 'visitor': 'visitor-1', 'customer': 'customer-yi',
             'kind': 'signed_phone', 'account': 'phone-family', 'at': '2026-11-02T08:31:00'})
        data['shared_device_declarations'][0]['users'] = ['customer-jia', 'customer-spouse', 'customer-yi']
        self.garments()
        feed(self.db, 'campaign-import', data)
        people = {r['customer'] for r in self.db.execute('SELECT DISTINCT customer FROM identity_link')}
        self.assertEqual(people, {'customer-jia', 'customer-spouse', 'customer-yi'})
        customers = self.customers()
        self.assertEqual(customers['customer-jia']['state'], 'acquired')
        self.assertEqual(customers['customer-yi']['state'], 'no_valid_visit')
        # booking on the shared device resolves to the visit customer, not a merged person
        self.assertEqual(self.bookings()['booking-jia-1']['customer'], 'customer-jia')

    def test_withdrawal_does_not_erase_a_previously_signed_paper_coupon(self):
        data = fixture('attribution_paper.json')
        # jia withdraws web consent long after the morning QR scans and signed coupon
        data['consent_withdrawals'] = [
            {'event_id': 'withdraw-jia', 'visitor': 'visitor-1', 'at': '2026-11-03T09:00:00'}]
        self.garments()
        feed(self.db, 'campaign-import', data)
        jia = self.customers()['customer-jia']
        self.assertEqual((jia['state'], jia['campaign']), ('acquired', 'campaign-a'))
        self.assertEqual(self.redeems()['redeem-a']['state'], 'valid')
        self.assert_audit_matched()

    def test_adding_intake_after_campaign_import_reopens_gating(self):
        data = fixture('attribution_paper.json')
        feed(self.db, 'campaign-import', data)  # no garments yet
        self.assertEqual(self.customers()['customer-jia']['state'], 'no_valid_visit')
        self.garments()  # accepted intake arrives later
        jia = self.customers()['customer-jia']
        self.assertEqual((jia['state'], jia['campaign']), ('acquired', 'campaign-a'))
        self.assert_audit_matched()

    def test_versions_are_stable_under_repeated_reevaluation(self):
        self.campaign('attribution_paper.json')
        before = self.bookings()['booking-jia-1']['version']
        reevaluate(self.db)
        reevaluate(self.db)
        self.assertEqual(self.bookings()['booking-jia-1']['version'], before)

    # ---- CLI end to end ----------------------------------------------------
    def test_cli_touch_import_to_report_and_audit(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        py = sys.executable
        subprocess.run([py, '-m', 'garmentcare', 'garment-import', '--db', str(path),
                        '--file', str(FIX / 'attribution_garments.json')],
                       cwd=ROOT, check=True, capture_output=True, text=True)
        r = subprocess.run([py, '-m', 'garmentcare', 'campaign-import', '--db', str(path),
                            '--file', str(FIX / 'attribution_paper.json')],
                           cwd=ROOT, check=True, capture_output=True, text=True)
        decisions = json.loads(r.stdout)
        self.assertEqual(decisions[0]['state'], 'acquired')
        r = subprocess.run([py, '-m', 'garmentcare', 'report', '--db', str(path)],
                           cwd=ROOT, check=True, capture_output=True, text=True)
        payload = json.loads(r.stdout)
        self.assertEqual(payload['campaign_totals']['acquired_by_campaign'], {'campaign-a': 1})
        r = subprocess.run([py, '-m', 'garmentcare', 'audit', '--db', str(path)],
                           cwd=ROOT, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
