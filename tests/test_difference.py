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


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


class DifferenceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def investigate(self, name):
        return feed(self.db, 'meter-import', fixture(name))[0]

    def test_even_cutoffs_leave_180_unexplained(self):
        result = self.investigate('meter_topology_ok.json')
        self.assertEqual((result['main_units'], result['branch_units']), (1000, 820))
        self.assertEqual((result['explained_units'], result['unexplained_units']), (820, 180))
        self.assertEqual(result['state'], 'unexplained')
        self.assertEqual(result['responsibility'], 'unattributed')
        kinds = [order['kind'] for order in result['work_orders']]
        self.assertIn('difference-investigation', kinds)
        self.assertNotIn('process', ' '.join(kinds))

    def test_calibration_certificate_closes_difference_and_blames_meter_b(self):
        result = self.investigate('meter_calibration_b.json')
        self.assertEqual(result['state'], 'explained')
        self.assertEqual(result['responsibility'], 'meter:MB')
        self.assertEqual((result['explained_units'], result['unexplained_units']), (1000, 0))
        self.assertEqual(result['adjusted_branch_units'], 1000)
        self.assertEqual(result['next_period'], 'apply-certificate-continuity')
        # Raw reading is preserved, correction lives beside it.
        raw = self.db.execute("SELECT reading FROM meter_reading WHERE event_id='branch-b-2026-11'").fetchone()
        self.assertEqual(raw['reading'], 3420)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM meter_reading WHERE reading=3600').fetchone()[0], 0)

    def test_clamp_test_closes_difference_as_missing_branch(self):
        result = self.investigate('meter_clamp_fan.json')
        self.assertEqual(result['state'], 'explained')
        self.assertEqual(result['responsibility'], 'missing-metering-branch:common-fan')
        self.assertEqual((result['branch_units'], result['common_units'], result['explained_units']), (820, 180, 1000))
        self.assertEqual(result['unexplained_units'], 0)
        self.assertEqual(result['next_period'], 'install-branch-register')
        kinds = [order['kind'] for order in result['work_orders']]
        self.assertIn('unmetered-load-remediation', kinds)
        branches = [item['id'] for item in result['affected'] if item['kind'] == 'branch']
        self.assertIn('missing', branches)

    def test_two_evidence_routes_cannot_both_zero_the_difference(self):
        result = self.investigate('meter_evidence_conflict.json')
        self.assertEqual(result['state'], 'frozen')
        self.assertEqual(result['responsibility'], 'disputed-attribution')
        # 820 + 180 would be 1000; the engine refuses to book both.
        self.assertEqual(result['explained_units'], 820)
        self.assertEqual(result['unexplained_units'], 180)
        self.assertNotEqual(result['explained_units'], 1000)
        self.assertEqual(result['next_period'], 'hold-until-retest')
        kinds = [order['kind'] for order in result['work_orders']]
        self.assertIn('segment-retest', kinds)
        routes = {item['route'] for item in result['evidence']['common']}
        self.assertIn('clamp-test', routes)
        self.assertTrue(result['evidence']['branch'][1]['coverage'] == 'calibrated')
        claims = {item['route']: item['units'] for item in result['evidence']['claimed_explanations']}
        self.assertEqual(claims, {'calibration': 180, 'clamp-test': 180})
        self.assertEqual(result['evidence']['booked']['attribution'], 'frozen')

    def test_misaligned_cutoffs_are_not_subtracted(self):
        result = self.investigate('meter_misaligned.json')
        self.assertEqual(result['state'], 'needs_evidence')
        self.assertEqual(result['responsibility'], 'meter-coverage')
        self.assertEqual(result['unexplained_units'], 180)
        self.assertEqual(result['evidence']['alignment'][0]['meter_id'], 'MB')
        self.assertEqual(result['evidence']['alignment'][0]['actual_cut'], '2026-11-05T18:00:00')
        kinds = [order['kind'] for order in result['work_orders']]
        self.assertIn('clock-synchronization', kinds)

    def test_clock_drift_realigns_when_check_arrives_late(self):
        data = fixture('meter_clock_drift.json')
        pages = {'pages': data['pages'], 'topology': data['topology']}
        feed(self.db, 'meter-import', pages)
        before = report(self.db)['energy_investigation'][0]
        self.assertEqual(before['state'], 'needs_evidence')
        after = feed(self.db, 'meter-import', {'clock_checks': data['clock_checks']})[0]
        self.assertEqual(after['evidence']['alignment'], [])
        self.assertEqual(after['state'], 'unexplained')
        # The clock check is a correction, never a rewrite of the raw value.
        raw = self.db.execute("SELECT reading,clock_offset_seconds,clock_verified FROM meter_reading "
                              "WHERE event_id='branch-b-2026-11'").fetchone()
        self.assertEqual((raw['reading'], raw['clock_offset_seconds'], raw['clock_verified']), (3420, 86400, 1))

    def test_moved_meter_keeps_historical_reading_on_old_branch(self):
        result = self.investigate('meter_move_late.json')
        branches = {item['meter_id']: item['branch_id'] for item in result['evidence']['branch']}
        self.assertEqual(branches['MB'], 'dry-line')
        self.assertTrue(result['evidence']['late_topology_amendment'])
        self.assertEqual(result['branch_units'], 820)
        self.assertEqual(result['unexplained_units'], 180)
        affected = [(item['kind'], item['id']) for item in result['affected']]
        self.assertIn(('branch', 'dry-line'), affected)
        self.assertIn(('segment', 'dry'), affected)
        kinds = [order['kind'] for order in result['work_orders']]
        self.assertIn('topology-verification', kinds)

    def test_late_filing_does_not_rewrite_the_number_already_collected(self):
        data = fixture('meter_move_late.json')
        for row in data['topology']:
            if row['event_id'] == 'topo-move-b':
                row['filed_at'] = '2026-12-04T09:00:00'
        feed(self.db, 'meter-import', data)
        result = report(self.db)['energy_investigation'][0]
        self.assertEqual(result['branch_units'], 820)
        self.assertEqual(result['explained_units'], 820)
        raw = self.db.execute("SELECT reading FROM meter_reading WHERE event_id='branch-b-2026-11'").fetchone()
        self.assertEqual(raw['reading'], 3420)

    def test_reversed_register_keeps_raw_and_widens_trust_interval(self):
        result = self.investigate('meter_reversed.json')
        branch = {item['meter_id']: item for item in result['evidence']['branch']}
        self.assertEqual(branch['MB']['reported'], -420)
        self.assertEqual(branch['MB']['coverage'], 'reversed')
        self.assertEqual((branch['MB']['trust_low'], branch['MB']['trust_high']), (0, 420))
        self.assertEqual(result['state'], 'unexplained')
        self.assertGreater(result['trust_high'], result['trust_low'])
        kinds = [order['kind'] for order in result['work_orders']]
        self.assertIn('meter-inspection', kinds)

    def test_evidence_arriving_before_pages_is_applied(self):
        data = fixture('meter_calibration_b.json')
        feed(self.db, 'meter-import', {'calibrations': data['calibrations'],
                                       'clock_checks': data['clock_checks']})
        result = feed(self.db, 'meter-import', {'pages': data['pages'], 'topology': data['topology']})[0]
        self.assertEqual(result['state'], 'explained')
        self.assertEqual(result['responsibility'], 'meter:MB')
        self.assertEqual((result['explained_units'], result['unexplained_units']), (1000, 0))

    def test_branch_without_earlier_reading_is_not_differenced(self):
        data = fixture('meter_topology_ok.json')
        data['pages'] = [p for p in data['pages'] if p['event_id'] != 'branch-b-2026-10']
        result = feed(self.db, 'meter-import', data)[0]
        self.assertEqual(result['state'], 'needs_evidence')
        branch = {item['meter_id']: item for item in result['evidence']['branch']}
        self.assertEqual(branch['MB']['coverage'], 'no-baseline')
        self.assertEqual(result['next_period'], 'collect-baseline-reading')
        kinds = [order['kind'] for order in result['work_orders']]
        self.assertIn('baseline-reading', kinds)

    def test_common_load_already_under_a_branch_is_not_double_counted(self):
        data = fixture('meter_topology_ok.json')
        data['common_loads'] = [{
            'event_id': 'fan-branch', 'site': 'laundry-a', 'load_id': 'fan', 'description': 'fan',
            'period': '2026-11', 'units': 180, 'branch_metered_units': 0,
            'connected_meter': 'MB', 'source': 'record'}]
        result = feed(self.db, 'meter-import', data)[0]
        self.assertEqual(result['common_units'], 0)
        self.assertEqual(result['unexplained_units'], 180)

    def test_common_load_metered_by_a_branch_is_not_double_counted(self):
        data = fixture('meter_topology_ok.json')
        data['common_loads'] = [{
            'event_id': 'fan-mb', 'site': 'laundry-a', 'load_id': 'fan', 'description': 'fan',
            'period': '2026-11', 'units': 180, 'connected_meter': 'M0',
            'metered_by': 'MB', 'source': 'record'}]
        result = feed(self.db, 'meter-import', data)[0]
        self.assertEqual(result['common_units'], 0)

    def test_unsigned_clamp_test_does_not_close_difference(self):
        data = fixture('meter_clamp_fan.json')
        data['clamp_tests'][0]['signer_b'] = data['clamp_tests'][0]['signer_a']
        data.pop('common_loads')
        result = feed(self.db, 'meter-import', data)[0]
        self.assertEqual(result['common_units'], 0)
        self.assertEqual(result['state'], 'unexplained')

    def test_single_meter_fixture_still_reports_200(self):
        result = feed(self.db, 'meter-import', fixture('meter_ok.json'))
        self.assertEqual(result[0]['units'], 200)
        self.assertEqual(result[0]['state'], 'single-meter')

    def test_reversed_main_register_does_not_close_the_period(self):
        result = self.investigate('meter_main_reversed.json')
        self.assertEqual(result['main_units'], -1000)
        self.assertEqual(result['state'], 'needs_evidence')
        self.assertEqual(result['next_period'], 'verify-main-register')
        kinds = [order['kind'] for order in result['work_orders']]
        self.assertIn('meter-inspection', kinds)

    def test_common_load_counts_only_the_part_outside_branches(self):
        data = fixture('meter_topology_ok.json')
        data['common_loads'] = [{
            'event_id': 'fan-partial', 'site': 'laundry-a', 'load_id': 'fan', 'description': 'fan',
            'period': '2026-11', 'units': 200, 'branch_metered_units': 50,
            'connected_meter': 'M0', 'source': 'record', 'signer_a': 'a', 'signer_b': 'b'}]
        result = feed(self.db, 'meter-import', data)[0]
        self.assertEqual(result['common_units'], 150)
        self.assertEqual(result['unexplained_units'], 30)

    def test_audit_rebuilds_difference_decomposition(self):
        for name in ('meter_topology_ok.json', 'meter_calibration_b.json',
                     'meter_clamp_fan.json', 'meter_evidence_conflict.json',
                     'meter_move_late.json', 'meter_clock_drift.json',
                     'meter_misaligned.json', 'meter_reversed.json'):
            with self.subTest(name=name):
                path = Path(self.tmp.name) / name.replace('.json', '.sqlite')
                db = connect(path)
                try:
                    run_import(db, 'meter-import', (FIX / name).read_bytes())
                    result = audit(db)
                    self.assertEqual(result['status'], 'matched')
                    self.assertEqual(len(result['energy_investigations']), 1)
                finally:
                    db.close()

    def test_cli_investigate_and_audit(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        for args in (['meter-import', '--file', str(FIX / 'meter_topology_ok.json')],
                     ['energy-investigate', '--site', 'laundry-a', '--period', '2026-11']):
            r = subprocess.run([sys.executable, '-B', '-m', 'garmentcare', *args, '--db', str(path)],
                               cwd=ROOT, text=True, capture_output=True, check=True)
            if args[0] == 'energy-investigate':
                out = json.loads(r.stdout)[0]
                self.assertEqual(out['unexplained_units'], 180)
        r = subprocess.run([sys.executable, '-B', '-m', 'garmentcare', 'audit', '--db', str(path)],
                           cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(r.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
