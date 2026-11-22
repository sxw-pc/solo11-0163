import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from garmentcare.__main__ import run_import, audit
from garmentcare.store import connect
from garmentcare.store_score import certification_view

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'
PLAN = 'plan-shop-a-w47'

BASE_FILES = ['cert_plan.json', 'cert_tasks.json', 'cert_visits.json']


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


def feed_file(db, name):
    return run_import(db, 'certification-import', (FIX / name).read_bytes())


def base(db):
    for name in BASE_FILES:
        feed_file(db, name)


def decision(db, plan_id=PLAN):
    view = certification_view(db, plan_id)
    return view, view['state']


class CertificationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_disputed_ticket_blocks_any_certificate_before_physical_evidence(self):
        base(self.db)
        view, state = decision(self.db)
        self.assertEqual(state, 'pending_more_samples')
        visit10 = next(v for v in view['visits'] if v['event_id'] == 'visit-10')
        self.assertEqual(visit10['adjudication'], 'ticket_disputed')
        idle = next(s for s in view['stratum_summary'] if s['stratum'] == 'idle')
        self.assertEqual(idle['valid'], 4)
        self.assertEqual(view['valid_samples'], 9)
        self.assertEqual(view['certificates'], [])

    def test_branch_a_independent_ticket_fails_certification_and_opens_rectification(self):
        base(self.db)
        feed_file(self.db, 'cert_resolution_independent.json')
        view, state = decision(self.db)
        self.assertEqual(state, 'not_certified')
        visit10 = next(v for v in view['visits'] if v['event_id'] == 'visit-10')
        self.assertEqual(visit10['adjudication'], 'valid')
        self.assertEqual(visit10['effective_minutes'], 18)
        self.assertEqual((view['valid_samples'], view['on_time_samples']), (10, 9))
        self.assertEqual(view['rectification']['state'], 'open')
        self.assertIn('reception capability', view['rectification']['reason'])
        self.assertEqual(view['certificates'], [])

    def test_branch_b_rescan_plus_predrawn_task_certifies_and_publishes(self):
        base(self.db)
        feed_file(self.db, 'cert_resolution_rescan.json')
        view, state = decision(self.db)
        self.assertEqual(state, 'pending_more_samples')
        visit10 = next(v for v in view['visits'] if v['event_id'] == 'visit-10')
        self.assertEqual(visit10['adjudication'], 'duplicate_rescan')
        feed_file(self.db, 'cert_task11_visit.json')
        view, state = decision(self.db)
        self.assertEqual(state, 'certifiable')
        visit11 = next(v for v in view['visits'] if v['event_id'] == 'visit-11')
        self.assertEqual(visit11['adjudication'], 'valid')
        self.assertEqual(view['valid_samples'], 10)
        self.assertEqual(view['on_time_samples'], 10)
        feed_file(self.db, 'cert_publish.json')
        view = certification_view(self.db, PLAN)
        cert = view['certificates'][-1]
        self.assertEqual(cert['state'], 'published')
        self.assertEqual(json.loads(cert['recipients']),
                         ['display-wall@shop-a', 'web-portal@shop-a'])
        self.assertEqual(view['rectification'], None)

    def test_late_receipt_overturns_published_certificate_and_revokes_it(self):
        base(self.db)
        feed_file(self.db, 'cert_resolution_rescan.json')
        feed_file(self.db, 'cert_task11_visit.json')
        feed_file(self.db, 'cert_publish.json')
        published = certification_view(self.db, PLAN)['certificates'][-1]
        self.assertEqual(published['state'], 'published')
        feed_file(self.db, 'cert_late_receipt.json')
        view = certification_view(self.db, PLAN)
        self.assertEqual(view['state'], 'not_certified')
        stored = next(c for c in view['certificates'] if c['version'] == published['version'])
        self.assertEqual(stored['state'], 'revoked')
        self.assertTrue(stored['revoked_at'])
        # The externally displayed content remains preserved, only marked revoked.
        self.assertEqual(stored['on_time_count'], 10)
        self.assertEqual(view['rectification']['state'], 'open')
        visit1 = next(v for v in view['visits'] if v['event_id'] == 'visit-01')
        self.assertEqual(visit1['effective_minutes'], 18)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_promise_widening_does_not_retroactively_excuse_old_overtime(self):
        feed_file(self.db, 'cert_plan.json')
        feed_file(self.db, 'cert_tasks.json')
        feed_file(self.db, 'cert_promise_change.json')
        # An additional late observation signed under the 10-minute task must stay overtime
        # even though the shop later publishes a 15-minute policy.
        late_visit = {
            'visits': [{
                'event_id': 'visit-shop-b-1', 'plan_id': PLAN, 'task_id': 't-idle-1',
                'shop': 'shop-a', 'visit_id': 'phys-visit-b-1',
                'observed_at': '2026-11-17T14:40:00', 'assessor': 'assessor-b-1',
                'check_in_receipt': {'terminal': 'queue-terminal-1', 'ticket': 'Q-B-1',
                                     'issued_at': '2026-11-17T14:40:00', 'device_sig': 'sig:Q-B-1'},
                'service_receipt': {'terminal': 'reception-terminal-1', 'minutes': 12,
                                    'at': '2026-11-17T14:52:00', 'device_sig': 'sig:R-B-1'},
                'presence_ref': 'presence:geo-b-1', 'observed_minutes': 12,
                'uploaded_at': '2026-11-17T14:52:00'}]}
        feed(self.db, 'certification-import', late_visit)
        view = certification_view(self.db, PLAN)
        row = next(v for v in view['visits'] if v['event_id'] == 'visit-shop-b-1')
        self.assertEqual(row['adjudication'], 'valid')
        # 12 minutes is on time under 15, but the task was signed at 10, so it stays late.
        self.assertEqual(row['effective_minutes'], 12)
        self.assertFalse(row['on_time'])
        # Recomputing the plan must never read the new policy into signed tasks.
        task = self.db.execute('SELECT promise_minutes FROM sampling_task WHERE task_id=?',
                               ('t-idle-1',)).fetchone()
        self.assertEqual(task['promise_minutes'], 10)
        policy = self.db.execute('SELECT * FROM promise_policy').fetchone()
        self.assertEqual(policy['promise_minutes'], 15)

    def test_out_of_frame_visit_cannot_fill_the_sample_quota(self):
        base(self.db)
        feed_file(self.db, 'cert_resolution_rescan.json')
        # The same physical evidence as visit-11 but linked to a task that is not in the
        # sealed frame: it must be retained as evidence yet excluded from random samples.
        data = fixture('cert_task11_visit.json')
        data['visits'][0]['event_id'] = 'visit-rogue'
        data['visits'][0]['task_id'] = 't-rogue-1'
        data['visits'][0]['visit_id'] = 'phys-visit-rogue'
        data['visits'][0]['check_in_receipt']['ticket'] = 'Q-ROGUE'
        data['visits'][0]['presence_ref'] = 'presence:geo-rogue'
        feed(self.db, 'certification-import', data)
        view, state = decision(self.db)
        self.assertEqual(state, 'pending_more_samples')
        rogue = next(v for v in view['visits'] if v['event_id'] == 'visit-rogue')
        self.assertEqual(rogue['adjudication'], 'out_of_frame')
        self.assertEqual(view['valid_samples'], 9)
        # A self-selected record outside the frame cannot rescue the missing idle slot.
        idle = next(s for s in view['stratum_summary'] if s['stratum'] == 'idle')
        self.assertEqual(idle['valid'], 4)
        # Only a visit attached to the pre-drawn task 11 fills the frame.
        feed_file(self.db, 'cert_task11_visit.json')
        view, state = decision(self.db)
        self.assertEqual(state, 'certifiable')
        self.assertEqual(view['valid_samples'], 10)

    def test_two_visit_ids_for_one_physical_visit_never_count_twice(self):
        base(self.db)
        # Even without a resolution, a shared queue ticket suspends the second visit.
        view = certification_view(self.db, PLAN)
        flagged = [v for v in view['visits'] if v['adjudication'] == 'ticket_disputed']
        self.assertEqual([v['event_id'] for v in flagged], ['visit-10'])
        total_valid = sum(1 for v in view['visits'] if v['adjudication'] == 'valid')
        self.assertEqual(total_valid, 9)

    def test_missing_stratum_is_pending_not_failure(self):
        feed_file(self.db, 'cert_plan.json')
        feed_file(self.db, 'cert_tasks.json')
        data = fixture('cert_visits.json')
        data['visits'] = [v for v in data['visits'] if v['event_id'].startswith('visit-0')]
        data['checks'] = []
        feed(self.db, 'certification-import', data)
        view, state = decision(self.db)
        self.assertEqual(state, 'pending_more_samples')
        self.assertIn('idle', view['reason'])

    def test_sealed_plan_cannot_be_rewritten_after_tasks_exist(self):
        feed_file(self.db, 'cert_plan.json')
        feed_file(self.db, 'cert_tasks.json')
        altered = fixture('cert_plan.json')
        altered['sampling_plans'][0]['promise_minutes'] = 15
        with self.assertRaisesRegex(ValueError, 'cannot be modified'):
            feed(self.db, 'certification-import', altered)
        plan = self.db.execute('SELECT promise_minutes FROM sampling_plan').fetchone()
        self.assertEqual(plan['promise_minutes'], 10)
        # Re-importing the identical sealed plan is an idempotent no-op.
        feed_file(self.db, 'cert_plan.json')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_baseline_grade_still_uses_every_imported_observation(self):
        base(self.db)
        grade = self.db.execute("SELECT * FROM store_grade WHERE shop='shop-a'").fetchone()
        self.assertEqual(grade['state'], 'needs_improvement')
        self.assertEqual(grade['checked_visits'], 10)

    def test_audit_replay_matches_for_both_physical_evidence_branches(self):
        for branch in ['cert_resolution_independent.json', 'cert_resolution_rescan.json']:
            with self.subTest(branch=branch):
                db = connect(Path(self.tmp.name) / f'audit-{branch}.sqlite')
                base(db)
                feed_file(db, branch)
                if branch.endswith('rescan.json'):
                    feed_file(db, 'cert_task11_visit.json')
                    feed_file(db, 'cert_publish.json')
                self.assertEqual(audit(db)['status'], 'matched')
                db.close()

    def test_cli_plan_to_certification_and_audit(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        sequence = BASE_FILES + ['cert_resolution_rescan.json', 'cert_task11_visit.json',
                                 'cert_publish.json']
        for name in sequence:
            r = subprocess.run([sys.executable, '-B', '-m', 'garmentcare',
                                'certification-import', '--db', str(path),
                                '--file', str(FIX / name)],
                               cwd=ROOT, text=True, capture_output=True, check=True)
            payload = json.loads(r.stdout)
        final = payload[-1]
        self.assertEqual(final['state'], 'certifiable')
        self.assertEqual(final['certificates'][-1]['state'], 'published')
        r = subprocess.run([sys.executable, '-B', '-m', 'garmentcare', 'audit', '--db', str(path)],
                           cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(r.stdout)['status'], 'matched')
        r = subprocess.run([sys.executable, '-B', '-m', 'garmentcare', 'report', '--db', str(path)],
                           cwd=ROOT, text=True, capture_output=True, check=True)
        report = json.loads(r.stdout)
        self.assertEqual(report['certifications'][0]['plan_id'], PLAN)


if __name__ == '__main__':
    unittest.main()
