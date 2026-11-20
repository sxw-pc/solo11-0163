import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import garmentcare
from garmentcare.__main__ import run_import,audit
from garmentcare.store import connect
from garmentcare.report import report

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'


def fixture(name):return json.loads((FIX/name).read_text(encoding='utf8'))
def feed(db,kind,data):return run_import(db,kind,json.dumps(data,ensure_ascii=False).encode())


class GarmentCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'state.sqlite')
        self.assertEqual(Path(garmentcare.__file__).resolve().parent,ROOT/'garmentcare')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def state(self,table):return dict(self.db.execute(f'SELECT * FROM {table} WHERE garment_id=?',('coat-1',)).fetchone())
    def normal(self):
        feed(self.db,'garment-import',fixture('garment_ok.json'))
        feed(self.db,'process-import',fixture('process_ok.json'))
        feed(self.db,'return-import',fixture('return_ok.json'))
        return report(self.db)['garments'][0]

    def test_normal_three_ledger_path(self):
        item=self.normal()
        self.assertEqual((item['intake']['state'],item['process']['state'],item['compensation']['state']),('accepted','completed','returned'))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_late_material_conflict_blocks_processing(self):
        self.normal();feed(self.db,'garment-import',fixture('garment_bad.json'))
        self.assertEqual(self.state('intake_decision')['state'],'frozen')
        self.assertEqual(self.state('process_decision')['state'],'blocked')
        self.assertEqual(self.state('compensation_decision')['state'],'blocked')
    def test_parallel_intake_photo_conflict(self):
        feed(self.db,'garment-import',fixture('garment_ok.json'))
        row=fixture('garment_bad.json')['photos'][0];row['material']='wool'
        feed(self.db,'garment-import',{'photos':[row]})
        other=copy.deepcopy(row);other['event_id']='photo-3';other['photo_sha']='alternate'
        feed(self.db,'garment-import',{'photos':[other]})
        self.assertEqual(self.state('intake_decision')['state'],'frozen')
    def test_unsigned_intake_baseline(self):
        data=fixture('garment_ok.json');data['photos'][0]['clerk_signer']='owner-1'
        feed(self.db,'garment-import',data)
        self.assertEqual(self.state('intake_decision')['state'],'pending')
    def test_unapproved_recipe(self):
        feed(self.db,'garment-import',fixture('garment_ok.json'))
        data=fixture('process_ok.json');data['recipes'][0]['approved_by']='clerk-1'
        feed(self.db,'process-import',data)
        self.assertEqual(self.state('process_decision')['state'],'blocked')
    def test_temperature_and_chemical_deviations(self):
        self.normal();feed(self.db,'process-import',fixture('process_bad.json'))
        self.assertEqual(self.state('process_decision')['state'],'deviation')
        self.assertEqual(self.state('compensation_decision')['state'],'returned')
    def test_temperature_alone_changes_processing_state(self):
        self.normal();data=fixture('process_bad.json');data['operations'][0]['chemical']='mild'
        data['operations'][0]['to_shop']='shop-a'
        feed(self.db,'process-import',data)
        self.assertIn('temperature',self.state('process_decision')['reason'])
    def test_banned_chemical_alone_changes_processing_state(self):
        self.normal();data=fixture('process_bad.json');data['operations'][0]['temp']=30
        data['operations'][0]['to_shop']='shop-a'
        feed(self.db,'process-import',data)
        self.assertIn('chemical',self.state('process_decision')['reason'])
    def test_custody_gap_independent_of_temperature(self):
        self.normal();data=fixture('process_bad.json')
        data['operations'][0]['temp']=30;data['operations'][0]['chemical']='mild'
        feed(self.db,'process-import',data)
        self.assertEqual(self.state('process_decision')['state'],'custody_gap')
    def test_unauthorized_pickup(self):
        self.normal();data=fixture('return_ok.json');data.pop('authorities')
        data['receipts'][0]['event_id']='return-2';data['receipts'][0]['person']='stranger';data['receipts'][0]['owner_signer']='stranger';data['receipts'][0]['at']='2026-11-05'
        feed(self.db,'return-import',data)
        self.assertEqual(self.state('compensation_decision')['state'],'blocked')
    def test_wrong_sealed_bag_blocks_return(self):
        self.normal();data=fixture('return_ok.json');data.pop('authorities')
        data['receipts'][0]['event_id']='return-2';data['receipts'][0]['at']='2026-11-05';data['receipts'][0]['bag']='bag-other'
        feed(self.db,'return-import',data)
        self.assertEqual(self.state('compensation_decision')['state'],'blocked')
    def test_new_damage_with_deviation_triggers_capped_payment(self):
        self.normal();feed(self.db,'process-import',fixture('process_bad.json'))
        feed(self.db,'return-import',fixture('return_bad.json'))
        result=self.state('compensation_decision')
        self.assertEqual((result['state'],result['payable']),('liable',5000))
    def test_old_damage_not_compensated(self):
        self.normal();data=fixture('return_bad.json');data['claims'][0]['damage']='old-scuff'
        feed(self.db,'return-import',data)
        self.assertEqual(self.state('compensation_decision')['state'],'preexisting')
    def test_timely_new_damage_without_deviation_frozen_for_review(self):
        self.normal();feed(self.db,'return-import',fixture('return_bad.json'))
        self.assertEqual((self.state('compensation_decision')['state'],self.state('compensation_decision')['frozen']),('review',5000))
    def test_late_claim_does_not_reopen(self):
        self.normal();data=fixture('return_bad.json');data['claims'][0]['at']='2026-11-11'
        feed(self.db,'return-import',data)
        self.assertEqual(self.state('compensation_decision')['state'],'late_claim')
    def test_replay_conflict_and_failed_batch_rollback(self):
        data=fixture('garment_ok.json');feed(self.db,'garment-import',data);feed(self.db,'garment-import',data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM intake_photo').fetchone()[0],1)
        data['photos'][0]['photo_sha']='changed'
        with self.assertRaisesRegex(ValueError,'event_id conflict'):feed(self.db,'garment-import',data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],2)
    def test_cli_demo(self):
        path=Path(self.tmp.name)/'demo.sqlite'
        r=subprocess.run([sys.executable,'-m','garmentcare','demo','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(r.stdout)['garments'][0]['compensation']['state'],'returned')

if __name__=='__main__':unittest.main()
