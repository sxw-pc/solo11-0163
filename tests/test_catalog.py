import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garmentcare.__main__ import audit, run_import
from garmentcare.report import report
from garmentcare.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, command, data):
    return run_import(db, command, json.dumps(data, ensure_ascii=False).encode())


class CatalogHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def entry(self, sku='coat-sku-1'):
        for label in report(self.db)['labels']:
            if label['sku'] == sku:
                return label
        return None

    def garment(self, garment_id='coat-1'):
        for item in report(self.db)['garments']:
            if item['id'] == garment_id:
                return item
        return None


class PublicationIdentityCase(CatalogHarness):
    def test_repeat_delivery_of_same_envelope_is_idempotent(self):
        raw = (FIX / 'label_page_ok.json').read_bytes()
        first = run_import(self.db, 'label-import', raw)
        self.assertEqual(run_import(self.db, 'label-import', raw), first)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM label_page').fetchone()[0], 1)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_same_bytes_fetched_twice_is_one_publication(self):
        feed(self.db, 'label-import', fixture('label_same_content_refetch.json'))
        entry = self.entry()
        self.assertEqual(entry['state'], 'verified')
        # Two fetch events, one publication.  The chain carries a single
        # page_fetch item with both events grouped under it.
        self.assertEqual(entry['candidates'], ['page-v2-fetch-a'])
        self.assertEqual(entry['source_event'], 'page-v2-fetch-a')
        fetches = [link for link in entry['source_chain'] if link['role'] == 'page_fetch']
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0]['fetch_events'], ['page-v2-fetch-a', 'page-v2-fetch-b'])

    def test_same_url_reissue_is_a_new_publication(self):
        feed(self.db, 'label-import', fixture('label_reissue_same_url.json'))
        entry = self.entry()
        self.assertEqual(entry['state'], 'verified')
        self.assertEqual(entry['revision'], '3')
        self.assertEqual(entry['source_event'], 'page-rev-b')
        self.assertNotEqual(
            [link['content_sha'] for link in entry['source_chain'] if link['role'] == 'page_fetch'][0],
            [link['content_sha'] for link in entry['source_chain'] if link['role'] == 'page_fetch'][1],
        )

    def test_fetch_order_does_not_decide_adoption(self):
        feed(self.db, 'label-import', fixture('label_fetch_reorder.json'))
        entry = self.entry()
        # The newest fetch still declares the older effective date, while the
        # older fetch declares the even older revision; declared time wins.
        self.assertEqual(entry['state'], 'verified')
        self.assertEqual(entry['source_event'], 'page-v2-late-fetch')
        self.assertEqual(entry['revision'], '2')

    def test_fetched_at_is_not_effective_at(self):
        data = {'pages': [{
            'event_id': 'page-future-fetch', 'source': 'maker-a', 'sku': 'coat-sku-1',
            'fetched_at': '2026-10-20T09:00:00',
            'html': ('<meta name="sku" content="coat-sku-1"><meta name="material" content="wool">'
                     '<meta name="care" content="dry"><meta name="effectivetime" content="2099-01-01">'
                     '<meta name="signer" content="maker-a/qa">'),
        }]}
        entry = feed(self.db, 'label-import', data)[0]
        self.assertIsNone(entry['effective_at'])
        self.assertEqual(entry['effective_basis'], 'missing')
        self.assertEqual(entry['state'], 'pending')


class EffectiveAndPendingCase(CatalogHarness):
    def test_pending_page_never_overrides_effective_signed_page(self):
        feed(self.db, 'label-import', fixture('label_route_fixture.json')['manufacturer_page'])
        before = self.entry()
        self.assertEqual((before['state'], before['care']), ('verified', 'dry'))

        feed(self.db, 'label-import', fixture('label_pending_no_effective.json'))
        after = self.entry()
        # The unsigned-effective "wet" page is newer by fetch time but cannot
        # usurp the effective, signed v2 instruction.
        self.assertEqual(after['state'], 'verified')
        self.assertEqual(after['care'], 'dry')
        self.assertEqual(after['source_event'], 'page-v2')
        self.assertIn('page-undated', after['pending_events'])

    def test_pending_page_alone_is_material_for_review_not_authority(self):
        entry = feed(self.db, 'label-import', fixture('label_pending_no_effective.json'))[0]
        self.assertEqual(entry['state'], 'pending')
        self.assertEqual(entry['careness'], 'pending')
        self.assertIsNone(entry['effective_at'])
        self.assertEqual(entry['pending_events'], ['page-undated'])

    def test_as_of_before_declared_effective_selects_prior_revision(self):
        feed(self.db, 'label-import', fixture('label_route_fixture.json')['manufacturer_page'])
        # page-v2 declares effective 2026-10-05; at 2026-10-02 only v1 is in force.
        at_early = report(self.db, '2026-10-02')['labels'][0]
        self.assertEqual(at_early['source_event'], 'page-v1')
        self.assertIn('page-v2', at_early['pending_events'])
        at_late = report(self.db, '2026-10-15')['labels'][0]
        self.assertEqual(at_late['source_event'], 'page-v2')

    def test_unsigned_effective_page_is_pending(self):
        data = {'pages': [{
            'event_id': 'page-unsigned', 'source': 'maker-a', 'sku': 'coat-sku-1',
            'fetched_at': '2026-10-20T09:00:00',
            'html': ('<meta name="sku" content="coat-sku-1"><meta name="material" content="silk">'
                     '<meta name="care" content="dry"><meta name="effective" content="2026-10-01">'),
        }]}
        entry = feed(self.db, 'label-import', data)[0]
        self.assertEqual(entry['state'], 'pending')
        self.assertIn('signature', entry['reason'])


class ConflictCase(CatalogHarness):
    def test_two_effective_sources_with_different_care_are_unresolved(self):
        entry = feed(self.db, 'label-import', fixture('label_contradiction.json'))[0]
        self.assertEqual(entry['state'], 'conflict')
        self.assertEqual(entry['source_event'], '')
        self.assertEqual(set(entry['conflict_events']), {'page-maker-a-v2', 'page-maker-b-v1'})

    def test_conflict_does_not_pick_latest_event_or_largest_time(self):
        entries = feed(self.db, 'label-import', fixture('label_contradiction.json'))
        self.assertNotIn(entries[0]['care'], ('dry', 'wet'))

    def test_same_source_same_revision_different_content_conflicts(self):
        base = '<html><head><meta name="sku" content="coat-sku-1"><meta name="revision" content="4">' \
               '<meta name="signer" content="maker-a/qa"><meta name="effective" content="2026-10-01">'
        data = {'pages': [
            {'event_id': 'page-a', 'source': 'maker-a', 'sku': 'coat-sku-1', 'fetched_at': '2026-10-02T00:00:00',
             'html': base + '<meta name="material" content="wool"><meta name="care" content="dry"></head></html>'},
            {'event_id': 'page-b', 'source': 'maker-a', 'sku': 'coat-sku-1', 'fetched_at': '2026-10-03T00:00:00',
             'html': base + '<meta name="material" content="wool"><meta name="care" content="wet"></head></html>'},
        ]}
        entry = feed(self.db, 'label-import', data)[0]
        self.assertEqual(entry['state'], 'conflict')
        self.assertEqual(set(entry['conflict_events']), {'page-a', 'page-b'})


class WithdrawalCase(CatalogHarness):
    def test_withdrawal_only_retires_its_referenced_publication(self):
        feed(self.db, 'label-import', fixture('label_route_fixture.json')['manufacturer_page'])
        feed(self.db, 'label-import', fixture('label_withdrawal.json'))
        entry = self.entry()
        self.assertEqual(entry['withdrawn_events'], ['page-v2'])
        # v2 is gone; the older v1 cache must not silently resurface as current.
        self.assertNotEqual(entry['source_event'], 'page-v2')
        self.assertEqual(entry['state'], 'withdrawn')
        self.assertIn('page-v1', entry['superseded_events'])

    def test_withdrawal_does_not_touch_a_different_source(self):
        feed(self.db, 'label-import', fixture('label_contradiction.json'))
        before = self.entry()
        self.assertEqual(before['state'], 'conflict')
        feed(self.db, 'label-import', fixture('label_withdrawal.json'))
        after = self.entry()
        # maker-a's v2 is withdrawn, so only maker-b's "wet" instruction is
        # left standing; the withdrawal never removes maker-b evidence.
        self.assertEqual(after['source_event'], 'page-maker-b-v1')
        self.assertEqual(after['care'], 'wet')
        self.assertEqual(after['state'], 'verified')
        self.assertNotIn('page-maker-b-v1', after['withdrawn_events'])

    def test_withdrawal_effective_time_gates_re_adjudication(self):
        feed(self.db, 'label-import', fixture('label_route_fixture.json')['manufacturer_page'])
        feed(self.db, 'label-import', fixture('label_withdrawal.json'))
        # The withdrawal is declared effective 2026-10-18.  Just before that
        # instant v2 is still the governing instruction even though the
        # withdrawal record was fetched after v2.
        early = report(self.db, '2026-10-17T23:59:59')['labels'][0]
        self.assertEqual(early['source_event'], 'page-v2')
        late = report(self.db, '2026-10-18T00:00:01')['labels'][0]
        self.assertNotEqual(late['source_event'], 'page-v2')

    def test_withdrawal_cannot_shadow_another_sources_revision(self):
        # A withdrawal from maker-b that names maker-a's revision must not
        # match maker-a's publication.
        feed(self.db, 'label-import', fixture('label_route_fixture.json')['manufacturer_page'])
        data = {'withdrawals': [{
            'event_id': 'withdrawal-cross', 'source': 'maker-b', 'sku': 'coat-sku-1',
            'fetched_at': '2026-10-18T08:00:00', 'target_revision': '2',
            'declared_effective_at': '2026-10-18T00:00:00', 'signer': 'maker-b/quality',
        }]}
        feed(self.db, 'label-import', data)
        entry = self.entry()
        self.assertEqual(entry['source_event'], 'page-v2')
        self.assertEqual(entry['state'], 'verified')

    def test_replay_after_withdrawal_matches(self):
        feed(self.db, 'label-import', fixture('label_route_fixture.json')['manufacturer_page'])
        feed(self.db, 'label-import', fixture('label_withdrawal.json'))
        self.assertEqual(audit(self.db)['status'], 'matched')


class RouteBundleMixin:
    def build(self, route_fixture):
        bundle = fixture(route_fixture)
        feed(self.db, 'garment-import', bundle['garment'])
        feed(self.db, 'label-import', bundle['manufacturer_page'])
        feed(self.db, 'label-import', bundle['physical_label'])
        feed(self.db, 'process-import', bundle['recipe'])
        feed(self.db, 'return-import', bundle['return'])
        feed(self.db, 'label-route', bundle['route'])
        return bundle


class TwoAuthorizedRoutesCase(RouteBundleMixin, CatalogHarness):
    """The same pages, tag and signed intake, differing only in the chosen authority."""

    def test_manufacturer_route_keeps_existing_recipe(self):
        self.build('label_route_fixture.json')
        entry = self.entry()
        item = self.garment()
        self.assertEqual((entry['state'], entry['material'], entry['care']), ('verified', 'wool', 'dry'))
        self.assertEqual(entry['source_event'], 'page-v2')
        self.assertEqual(item['label_review']['route'], 'manufacturer')
        self.assertEqual(item['label_review']['task_state'], 'noted')  # tag disagreement recorded
        self.assertEqual(item['process']['state'], 'completed')
        self.assertEqual(item['process']['nominal_state'], 'completed')
        self.assertEqual(item['compensation']['state'], 'returned')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_physical_route_opens_wet_conflict_and_freezes_new_processing(self):
        self.build('label_route_physical.json')
        entry = self.entry()
        item = self.garment()
        self.assertEqual(entry['care'], 'dry')  # catalogue verdict unchanged
        self.assertEqual(item['label_review']['route'], 'physical_label')
        self.assertEqual(item['label_review']['label_state'], 'conflict')
        self.assertEqual((item['label_review']['label_material'], item['label_review']['label_care']),
                         ('wool', 'wet'))
        self.assertEqual(item['label_review']['task_state'], 'conflict_pending')
        self.assertEqual(item['label_review']['impact'], 'blocked_new_processing')
        # Already-executed operations and the true return facts survive.
        self.assertEqual(item['process']['state'], 'completed')
        self.assertEqual(item['process']['nominal_state'], 'completed')
        self.assertIn('new processing eligibility frozen', item['process']['reason'])
        self.assertEqual(item['compensation']['state'], 'returned')

    def test_routes_share_evidence_but_differ_in_work_order(self):
        self.build('label_route_fixture.json')
        manufacturer = self.garment()
        self.db.close()
        self.db = connect(Path(self.tmp.name) / 'physical.sqlite')
        self.build('label_route_physical.json')
        physical = self.garment()
        self.assertEqual(manufacturer['process']['state'], 'completed')
        self.assertEqual(physical['process']['nominal_state'], 'completed')
        self.assertNotEqual(manufacturer['label_review']['task_state'], physical['label_review']['task_state'])
        self.assertEqual(manufacturer['label_review']['label_care'], 'dry')
        self.assertEqual(physical['label_review']['label_care'], 'wet')
        self.assertEqual(
            self.db.execute('SELECT body FROM evidence WHERE event_id=?', ('page-v2',)).fetchone()['body'],
            json.dumps(self._page_payload(), ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        )

    def _page_payload(self):
        bundle = fixture('label_route_fixture.json')
        bundle_physical = fixture('label_route_physical.json')
        self.assertEqual(bundle['manufacturer_page'], bundle_physical['manufacturer_page'])
        self.assertEqual(bundle['physical_label'], bundle_physical['physical_label'])
        return bundle['manufacturer_page']['pages'][1]

    def test_switching_route_after_decision_readjudicates(self):
        bundle = self.build('label_route_fixture.json')
        self.assertEqual(self.garment()['label_review']['task_state'], 'noted')
        switch = dict(bundle['route'], route='physical_label',
                      basis='owner now relies on the signed physical tag and pauses processing',
                      at='2026-10-16T10:00:00')
        feed(self.db, 'label-route', switch)
        item = self.garment()
        self.assertEqual(item['label_review']['task_state'], 'conflict_pending')
        self.assertEqual(item['label_review']['label_care'], 'wet')

    def test_signed_intake_photo_is_never_rewritten(self):
        self.build('label_route_physical.json')
        photo = self.db.execute('SELECT * FROM intake_photo WHERE event_id=?', ('photo-route-1',)).fetchone()
        self.assertEqual((photo['material'], photo['owner_signer'], photo['photo_sha']),
                         ('wool', 'owner-1', 'intake-photo-route'))
        garment = self.db.execute('SELECT * FROM garment WHERE id=?', ('coat-1',)).fetchone()
        self.assertEqual(garment['material'], 'wool')
        self.assertEqual(garment['allowed_process'], 'dry')

    def test_late_v2_withdrawal_readjudicates_both_routes(self):
        for name in ('label_route_fixture.json', 'label_route_physical.json'):
            with self.subTest(route=name):
                self.db.close()
                self.db = connect(Path(self.tmp.name) / f'{name}.sqlite')
                self.build(name)
                before = self.entry()
                self.assertEqual(before['source_event'], 'page-v2')
                feed(self.db, 'label-import', fixture('label_withdrawal.json'))
                after = self.entry()
                item = self.garment()
                # v2 is retired for both routes; neither re-uses the old cache.
                self.assertEqual(after['withdrawn_events'], ['page-v2'])
                self.assertNotEqual(after['source_event'], 'page-v2')
                self.assertIn('page-v1', after['superseded_events'])
                self.assertEqual(after['material'], '')  # no adopted material
                self.assertIn(item['label_review']['label_state'], ('pending', 'conflict', 'withdrawn'))
                self.assertEqual(item['label_review']['impact'], 'blocked_new_processing')
                self.assertEqual(item['process']['nominal_state'], 'completed')
                self.assertEqual(audit(self.db)['status'], 'matched')

    def test_withdrawal_does_not_retroactively_invent_prior_validity(self):
        self.build('label_route_fixture.json')
        feed(self.db, 'label-import', fixture('label_withdrawal.json'))
        # At an as-of before the withdrawal took effect the v2 page is still
        # the governing authority; only afterwards must decisions re-adjudicate.
        prior = report(self.db, '2026-10-17T23:59:59')['labels'][0]
        self.assertEqual(prior['source_event'], 'page-v2')
        self.assertEqual(prior['care'], 'dry')
        after = report(self.db, '2026-10-18T00:00:01')['labels'][0]
        self.assertIn(after['source_event'], (None, ''))
        self.assertIn(after['material'], (None, ''))


class GarmentLinkageCase(RouteBundleMixin, CatalogHarness):
    def test_unlinked_garment_is_not_affected(self):
        bundle = fixture('label_route_fixture.json')
        garment = json.loads(json.dumps(bundle['garment']))
        garment['garments'][0]['sku'] = ''
        feed(self.db, 'garment-import', garment)
        feed(self.db, 'label-import', bundle['manufacturer_page'])
        item = self.garment()
        self.assertEqual(item['label_review']['label_state'], 'not_tracked')
        self.assertEqual(item['intake']['state'], 'accepted')

    def test_new_processing_after_conflict_is_frozen_but_past_preserved(self):
        bundle = self.build('label_route_physical.json')
        # A second recipe prepared later must not become eligible while the
        # wet/dry care authority is unresolved.
        later = {'recipes': [{
            'event_id': 'recipe-2', 'garment_id': 'coat-1', 'version': 2, 'process': 'dry',
            'max_temp': 40, 'banned_chemicals': ['bleach'], 'approved_by': 'owner-1',
        }]}
        feed(self.db, 'process-import', later)
        item = self.garment()
        self.assertEqual(item['process']['state'], 'frozen')
        self.assertEqual(item['process']['nominal_state'], 'scheduled')
        operations = self.db.execute('SELECT COUNT(*) FROM operation WHERE garment_id=?', ('coat-1',)).fetchone()[0]
        self.assertEqual(operations, 1)

    def test_signed_physical_tag_missing_signature_is_unverified(self):
        bundle = fixture('label_route_physical.json')
        feed(self.db, 'garment-import', bundle['garment'])
        feed(self.db, 'label-import', bundle['manufacturer_page'])
        tag = json.loads(json.dumps(bundle['physical_label']))
        tag['labels'][0]['signer'] = ''
        feed(self.db, 'label-import', tag)
        feed(self.db, 'label-route', bundle['route'])
        self.assertEqual(self.garment()['label_review']['label_state'], 'unverified')

    def test_conflicting_physical_tags_resolve_deterministically(self):
        feed(self.db, 'garment-import', fixture('label_route_physical.json')['garment'])
        feed(self.db, 'label-import', fixture('label_route_physical.json')['manufacturer_page'])
        feed(self.db, 'label-import', fixture('label_conflicting_tags.json'))
        feed(self.db, 'label-route', fixture('label_route_physical.json')['route'])
        review = self.garment()['label_review']
        # Most recent independently signed tag is the tie-breaker, and the
        # difference against the manufacturer page still raises a conflict.
        self.assertEqual(review['label_care'], 'dry')
        self.assertEqual(review['label_event'], 'tag-2')
        self.assertEqual(review['label_state'], 'conflict')

    def test_audit_replays_label_and_route_decisions(self):
        self.build('label_route_physical.json')
        self.assertEqual(audit(self.db)['status'], 'matched')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM file_journal').fetchone()[0], 6)


class CliCase(CatalogHarness):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'garmentcare', *args], cwd=ROOT,
                              text=True, capture_output=True, check=True)

    def test_cli_label_import_report_audit_roundtrip(self):
        tmp = Path(self.tmp.name)
        bundle = fixture('label_route_fixture.json')
        state = tmp / 'cli.sqlite'
        files = {
            'garment.json': bundle['garment'],
            'page.json': bundle['manufacturer_page'],
            'tag.json': bundle['physical_label'],
            'recipe.json': bundle['recipe'],
            'return.json': bundle['return'],
        }
        for name, payload in files.items():
            (tmp / name).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf8')
        self.run_cli('garment-import', '--db', str(state), '--file', str(tmp / 'garment.json'))
        self.run_cli('label-import', '--db', str(state), '--file', str(tmp / 'page.json'))
        self.run_cli('label-import', '--db', str(state), '--file', str(tmp / 'tag.json'))
        self.run_cli('process-import', '--db', str(state), '--file', str(tmp / 'recipe.json'))
        self.run_cli('return-import', '--db', str(state), '--file', str(tmp / 'return.json'))
        self.run_cli('label-route', '--db', str(state), '--garment', 'coat-1', '--route', 'manufacturer',
                     '--basis', bundle['route']['basis'], '--at', bundle['route']['at'])
        out = json.loads(self.run_cli('report', '--db', str(state), '--as-of', '2026-10-15').stdout)
        self.assertEqual(out['labels'][0]['care'], 'dry')
        self.assertEqual(out['garments'][0]['process']['state'], 'completed')
        audited = json.loads(self.run_cli('audit', '--db', str(state)).stdout)
        self.assertEqual(audited['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
