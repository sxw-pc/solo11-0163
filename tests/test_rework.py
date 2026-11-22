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
from garmentcare import rework

ROOT = Path(__file__).resolve().parents[1]
RW = ROOT / 'fixtures' / 'rework'


def load(name):
    return json.loads((RW / name).read_text(encoding='utf8'))


def feed(db, data):
    return run_import(db, 'rework-import', json.dumps(data, ensure_ascii=False).encode())


def feed_fit(db, data):
    return run_import(db, 'fit-import', json.dumps(data, ensure_ascii=False).encode())


class ReworkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def decision(self, garment_id):
        return dict(self.db.execute('SELECT * FROM rework_decision WHERE garment_id=?', (garment_id,)).fetchone())

    def ledger(self, bolt_id):
        return dict(self.db.execute('SELECT * FROM fabric_ledger WHERE bolt_id=?', (bolt_id,)).fetchone())

    def load_band_journey(self):
        feed_fit(self.db, load('rw_fit.json'))
        feed(self.db, load('rw_baseline.json'))
        feed(self.db, load('rw_fabric.json'))
        feed(self.db, load('rw_route1.json'))
        feed(self.db, load('rw_route2.json'))
        feed(self.db, load('rw_remeasure_r2.json'))

    # ---------------------------------------------------------- fixture journey

    def test_old_simple_feasibility_still_runs_alongside_rework(self):
        result = feed_fit(self.db, load('rw_fit.json'))
        self.assertEqual({r['state'] for r in result}, {'needs_material'})
        feed(self.db, load('rw_baseline.json'))
        self.assertEqual(self.decision('coat-r1')['state'], 'route_pending')

    def test_band_route_shared_bolt_both_accepted_and_real_consumption(self):
        self.load_band_journey()
        for gid in ('coat-r1', 'coat-r2'):
            d = self.decision(gid)
            self.assertEqual(d['state'], 'accepted', gid)
            self.assertEqual(d['route'], 'pattern_band')
            self.assertEqual(d['within_tolerance'], 1)
            self.assertEqual(d['final_hem'], 94)
            self.assertEqual(d['cut_open'], 0)
            self.assertIn('regular overlock 2cm', d['finished_edge'])
            # labor 700 + fabric 20x10x12 = 2400
            self.assertEqual(d['total_cost'], 3100)
        ledger = self.ledger('bolt-A')
        # two 20x10 cuts = 400 sq cm consumed, not a blind area decrement
        self.assertEqual((ledger['used_area'], ledger['available_area'], ledger['state']),
                         (400, 3000 - 400, 'open'))

    def test_same_remnant_cannot_be_double_allocated(self):
        self.load_band_journey()
        # A third layout sitting exactly on coat-r1's cut must be rejected even
        # though the bolt still has plenty of total area left.
        greedy = {'layouts': [{
            'event_id': 'layout-greedy', 'garment_id': 'coat-r1', 'bolt_id': 'bolt-A',
            'template_id': 'band-tpl', 'x': 0, 'y': 0}]}
        feed(self.db, greedy)
        plan = dict(self.db.execute('SELECT * FROM layout_plan WHERE event_id=?', ('layout-greedy',)).fetchone())
        self.assertEqual(plan['valid'], 0)
        self.assertIn('irreversible cut', plan['reason'])
        with self.assertRaisesRegex(ValueError, 'rejected'):
            feed(self.db, {'cuts': [{'event_id': 'cut-greedy', 'garment_id': 'coat-r1',
                                     'layout_event': 'layout-greedy', 'at': '2026-11-05T09:00:00'}]})
        # ledger unchanged after the failed attempt
        self.assertEqual(self.ledger('bolt-A')['used_area'], 400)

    def test_defect_zone_blocks_placement_despite_enough_area(self):
        feed_fit(self.db, load('rw_fit.json'))
        feed(self.db, load('rw_baseline.json'))
        feed(self.db, load('rw_fabric.json'))
        feed(self.db, load('rw_route1.json'))
        blocked = dict(self.db.execute('SELECT * FROM layout_plan WHERE event_id=?',
                                       ('layout-r2-blocked',)).fetchone())
        self.assertEqual(blocked['valid'], 0)
        self.assertIn('defect no-cut zone', blocked['reason'])
        d2 = self.decision('coat-r2')
        self.assertEqual(d2['state'], 'waiting_material')
        self.assertEqual(d2['total_cost'], 0)
        # bolt-B has 360 sq cm (>= the 200 sq cm piece) but no legal placement
        bolt_b = self.ledger('bolt-B')
        self.assertEqual(bolt_b['state'], 'exhausted')
        self.assertEqual(bolt_b['used_area'], 0)

    def test_grain_mismatch_and_stripe_period_are_enforced(self):
        feed(self.db, load('rw_fabric.json'))
        bolt = dict(self.db.execute('SELECT * FROM fabric_bolt WHERE bolt_id=?', ('bolt-A',)).fetchone())
        tpl = dict(self.db.execute('SELECT * FROM cut_template WHERE template_id=?', ('band-tpl',)).fetchone())
        cross = dict(tpl, template_id='cross-tpl', grain_required='weft_x')
        db2_data = {'templates': [cross]}
        feed(self.db, db2_data)
        cross_tpl = dict(self.db.execute('SELECT * FROM cut_template WHERE template_id=?', ('cross-tpl',)).fetchone())
        ok, reason = rework.legal_placement(self.db, bolt, cross_tpl, 0, 0)
        self.assertFalse(ok)
        self.assertIn('grain', reason)
        # y=5 breaks the 10cm stripe period
        ok, reason = rework.legal_placement(self.db, bolt, tpl, 0, 5)
        self.assertFalse(ok)
        self.assertIn('pattern period', reason)

    def test_late_shrinkage_recomputes_acceptance_but_keeps_cut_occupation(self):
        self.load_band_journey()
        feed(self.db, load('rw_shrink_r1.json'))
        d1 = self.decision('coat-r1')
        self.assertEqual(d1['state'], 'rejected_shrinkage')
        self.assertEqual(d1['within_tolerance'], 0)
        self.assertEqual(d1['final_hem'], 93)
        # the cut is not reversed: fabric stays consumed and cost stays booked
        self.assertEqual(self.ledger('bolt-A')['used_area'], 400)
        self.assertEqual(d1['total_cost'], 3100)
        # the other garment's dual-signed result is untouched
        self.assertEqual(self.decision('coat-r2')['state'], 'accepted')

    def test_corrected_target_never_returns_cut_fabric_to_the_roll(self):
        self.load_band_journey()
        correction = copy.deepcopy(load('rw_fit.json'))
        correction['measures'][0]['event_id'] = 'm-r1-corrected'
        correction['measures'][0]['target_hem'] = 90
        correction['measures'][0]['at'] = '2026-11-09T09:00:00'
        feed_fit(self.db, correction)
        d1 = self.decision('coat-r1')
        # re-evaluated against the new measuring stick: 94 vs corrected 90 fails
        self.assertEqual(d1['within_tolerance'], 0)
        # but the irreversible material fact stands
        self.assertEqual(self.ledger('bolt-A')['used_area'], 400)
        self.assertEqual(d1['total_cost'], 3100)
        self.assertEqual(d1['cut_open'], 0)

    def test_pre_sew_remeasure_does_not_count_as_acceptance(self):
        feed_fit(self.db, load('nr_fit.json'))
        feed(self.db, load('nr_baseline.json'))
        feed(self.db, load('nr_route_early.json'))
        early = dict(self.db.execute('SELECT * FROM remeasure WHERE event_id=?', ('rm-n1-early',)).fetchone())
        self.assertEqual(early['valid'], 0)
        self.assertIn('predates', early['reason'])
        d = self.decision('coat-n1')
        self.assertEqual(d['state'], 'awaiting_sew')
        self.assertIsNone(d['final_hem'])

    def test_narrow_route_uses_no_fabric_and_keeps_narrow_edge(self):
        feed_fit(self.db, load('nr_fit.json'))
        feed(self.db, load('nr_baseline.json'))
        feed(self.db, load('nr_route_early.json'))
        feed(self.db, load('nr_sew_ok.json'))
        d = self.decision('coat-n1')
        self.assertEqual(d['state'], 'accepted')
        self.assertEqual(d['route'], 'narrow_let_out')
        self.assertEqual(d['final_hem'], 94)
        self.assertIn('narrow overlock 1cm', d['finished_edge'])
        # only labor, zero fabric consumed anywhere
        self.assertEqual(d['total_cost'], 550)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cut_receipt').fetchone()[0], 0)
        kinds = {r['kind'] for r in self.db.execute('SELECT DISTINCT kind FROM rework_cost_line')}
        self.assertEqual(kinds, {'labor'})

    def test_narrow_route_late_shrinkage_failure_is_not_signed_off(self):
        feed_fit(self.db, load('nr_fit.json'))
        feed(self.db, load('nr_baseline.json'))
        feed(self.db, load('nr_route_early.json'))
        feed(self.db, load('nr_sew_ok.json'))
        feed(self.db, load('nr_shrink.json'))
        d = self.decision('coat-n1')
        self.assertEqual(d['state'], 'rejected_shrinkage')
        self.assertEqual(d['within_tolerance'], 0)
        self.assertEqual(d['final_hem'], 93)

    def test_one_garment_cannot_borrow_another_garments_remeasure(self):
        self.load_band_journey()
        # coat-r2 sewn at 15:00 but its own remeasure removed; r1's 16:00 record
        # belongs to r1 and must never validate r2.
        self.db.execute("DELETE FROM remeasure WHERE garment_id='coat-r2'")
        rework._refresh_remeasure_validity(self.db, 'coat-r2')
        rework.decide(self.db, 'coat-r2')
        d2 = self.decision('coat-r2')
        self.assertEqual(d2['state'], 'sewn_pending_measure')
        self.assertIsNone(d2['final_hem'])
        # restoring r2's own dual-signed remeasure accepts it again
        fresh = copy.deepcopy(load('rw_remeasure_r2.json'))
        fresh['remeasures'][0]['event_id'] = 'rm-r2-again'
        feed(self.db, fresh)
        self.assertEqual(self.decision('coat-r2')['state'], 'accepted')

    def test_dual_signature_required(self):
        feed_fit(self.db, load('nr_fit.json'))
        feed(self.db, load('nr_baseline.json'))
        feed(self.db, load('nr_route_early.json'))
        bad = load('nr_sew_ok.json')
        bad['remeasures'][0]['event_id'] = 'rm-bad-sig'
        bad['remeasures'][0]['owner_signer'] = 'tailor-1'  # same as measurer
        feed(self.db, bad)
        row = dict(self.db.execute('SELECT * FROM remeasure WHERE event_id=?', ('rm-bad-sig',)).fetchone())
        self.assertEqual(row['valid'], 0)
        self.assertIn('dual independent signatures', row['reason'])

    def test_route_without_signed_consent_is_blocked(self):
        feed_fit(self.db, load('nr_fit.json'))
        baseline = load('nr_baseline.json')
        baseline['hem_baselines'][0]['narrow_consent'] = False
        feed(self.db, baseline)
        feed(self.db, load('nr_route_early.json'))
        d = self.decision('coat-n1')
        self.assertEqual(d['state'], 'blocked')
        self.assertIn('narrow overlock option not signed', d['reason'])

    def test_cut_open_liability_before_sewing(self):
        feed_fit(self.db, load('rw_fit.json'))
        feed(self.db, load('rw_baseline.json'))
        feed(self.db, load('rw_fabric.json'))
        feed(self.db, load('rw_route1.json'))
        feed(self.db, {'cuts': [{'event_id': 'cut-r1-only', 'garment_id': 'coat-r1',
                                 'layout_event': 'layout-r1', 'at': '2026-11-04T09:00:00'}]})
        d1 = self.decision('coat-r1')
        self.assertEqual(d1['state'], 'cut_open')
        self.assertEqual(d1['cut_open'], 1)
        # fabric cost is already booked as the cost basis of unfinished work
        self.assertEqual(d1['total_cost'], 2400)
        self.assertEqual(self.ledger('bolt-A')['used_area'], 200)

    def test_band_sewing_cannot_start_before_cutting(self):
        feed_fit(self.db, load('rw_fit.json'))
        feed(self.db, load('rw_baseline.json'))
        feed(self.db, load('rw_fabric.json'))
        feed(self.db, load('rw_route1.json'))
        with self.assertRaisesRegex(ValueError, 'before the band is cut'):
            feed(self.db, {'sewing': [{'event_id': 'sew-too-early', 'garment_id': 'coat-r1',
                                       'at': '2026-11-04T10:00:00', 'route': 'pattern_band',
                                       'finished_edge': 'regular overlock 2cm hem with matching band'}]})

    def test_audit_recomputes_from_measuring_sticks_layouts_and_cuts(self):
        self.load_band_journey()
        feed(self.db, load('rw_shrink_r1.json'))
        result = audit(self.db)
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['rework_audit']['rework_garments'], 2)
        self.assertEqual(result['rework_audit']['cuts'], 2)
        rep = report(self.db)
        self.assertEqual(len(rep['rework']['fabric_ledger']), 2)
        self.assertEqual(len(rep['rework']['cost_lines']), 4)

    def test_re_importing_identical_files_is_idempotent(self):
        self.load_band_journey()
        # The audit replay re-feeds every source file verbatim into a fresh db;
        # re-feeding here must not duplicate receipts, cost lines or decisions.
        feed(self.db, load('rw_route2.json'))
        feed(self.db, load('rw_route2.json'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cut_receipt').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sew_record').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM rework_cost_line').fetchone()[0], 4)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_audit_detects_tampered_fabric_ledger(self):
        self.load_band_journey()
        # Recompute layer: raw cut receipts vs a corrupted derived ledger.
        self.db.execute("UPDATE fabric_ledger SET used_area=99 WHERE bolt_id='bolt-A'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'fabric ledger disagrees'):
            rework.audit_recompute(self.db)
        # Replay layer on a separately tampered db (audit_recompute above
        # repairs its own connection, so the corruption must be re-created).
        tampered = connect(Path(self.tmp.name) / 'tampered.sqlite')
        for command, name in [('fit-import', 'rw_fit.json'), ('rework-import', 'rw_baseline.json'),
                              ('rework-import', 'rw_fabric.json'), ('rework-import', 'rw_route1.json'),
                              ('rework-import', 'rw_route2.json'), ('rework-import', 'rw_remeasure_r2.json')]:
            run_import(tampered, command, (RW / name).read_bytes())
        tampered.execute("UPDATE fabric_ledger SET used_area=99 WHERE bolt_id='bolt-A'")
        tampered.commit()
        with self.assertRaisesRegex(ValueError, 'replayed source files disagree'):
            audit(tampered)
        tampered.close()

    def test_cli_end_to_end_rework_import_and_audit(self):
        db_path = Path(self.tmp.name) / 'cli.sqlite'
        order = [
            ('fit-import', 'rw_fit.json'), ('rework-import', 'rw_baseline.json'),
            ('rework-import', 'rw_fabric.json'), ('rework-import', 'rw_route1.json'),
            ('rework-import', 'rw_route2.json'), ('rework-import', 'rw_remeasure_r2.json')]
        for command, name in order:
            r = subprocess.run([sys.executable, '-m', 'garmentcare', command,
                                '--db', str(db_path), '--file', str(RW / name)],
                               cwd=ROOT, text=True, capture_output=True, check=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, '-m', 'garmentcare', 'audit', '--db', str(db_path)],
                           cwd=ROOT, text=True, capture_output=True, check=True)
        payload = json.loads(r.stdout)
        self.assertEqual(payload['status'], 'matched')
        self.assertEqual(payload['rework_audit']['cuts'], 2)
        r = subprocess.run([sys.executable, '-m', 'garmentcare', 'report', '--db', str(db_path)],
                           cwd=ROOT, text=True, capture_output=True, check=True)
        decisions = {d['garment_id']: d for d in json.loads(r.stdout)['rework']['decisions']}
        self.assertEqual(decisions['coat-r1']['state'], 'accepted')
        self.assertEqual(decisions['coat-r2']['state'], 'accepted')


if __name__ == '__main__':
    unittest.main()
