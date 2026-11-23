import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from garmentcare.__main__ import run_import,audit
from garmentcare.store import connect
from garmentcare.report import report

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'


def fixture(name):return json.loads((FIX/name).read_text(encoding='utf8'))


class SolventCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def feed(self,kind,data):return run_import(self.db,kind,json.dumps(data,ensure_ascii=False).encode())
    def base(self):
        self.feed('garment-import',fixture('garment_ok.json'))
        self.feed('process-import',fixture('process_ok.json'))
        self.feed('process-import',fixture('solvent_ops.json'))
        self.feed('solvent-import',fixture('solvent_base.json'))
    def totals(self):return report(self.db)['solvent']['totals']
    def barrel(self,barrel_id):
        row=dict(self.db.execute('SELECT * FROM solvent_decision WHERE barrel_id=?',(barrel_id,)).fetchone())
        row['quantities']=json.loads(row['quantities'])
        return row
    def operation(self,operation_id):
        row=dict(self.db.execute('SELECT * FROM solvent_operation WHERE operation_id=?',(operation_id,)).fetchone())
        row['quantities']=json.loads(row['quantities'])
        return row

    def test_distill_route_closes_mass_balance(self):
        self.base();self.feed('solvent-import',fixture('solvent_distill.json'))
        totals=self.totals()
        self.assertEqual((totals['purchased_l'],totals['reusable_l'],totals['evaporation_l'],totals['waste_on_hand_l'],totals['transferred_l'],totals['unexplained_l']),(20,16,2,2,0,0))
        self.assertTrue(totals['conserved'])
        self.assertEqual(self.barrel('B1')['state'],'closed')
        self.assertEqual(self.barrel('R1')['state'],'closed')
        self.assertEqual(self.barrel('G1')['state'],'closed')
        self.assertEqual(self.barrel('G1')['quantities']['reusable_l'],12)
        self.assertEqual(self.operation('op-1')['state'],'balanced')
        self.assertEqual(self.operation('op-2')['quantities']['evaporation_l'],1)
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_outbound_route_weighed_receipt_closes(self):
        self.base();self.feed('solvent-import',fixture('solvent_outbound.json'));self.feed('solvent-import',fixture('solvent_receipt_weighed.json'))
        totals=self.totals()
        self.assertEqual((totals['reusable_l'],totals['transferred_l'],totals['evaporation_l']),(4,14,2))
        self.assertTrue(totals['conserved'])
        self.assertEqual(self.barrel('R1')['state'],'closed')
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_accepted_receipt_is_not_weighed_closure(self):
        self.base();self.feed('solvent-import',fixture('solvent_outbound.json'));self.feed('solvent-import',fixture('solvent_receipt_accepted.json'))
        r1=self.barrel('R1')
        self.assertEqual(r1['state'],'open')
        self.assertIn('weighed',r1['reason'])
        self.assertEqual(r1['quantities']['in_transit_l'],14)
        totals=self.totals()
        self.assertEqual(totals['transferred_l'],0)
        self.assertTrue(totals['conserved'])
    def test_weighed_receipt_arrives_before_handover(self):
        self.base();self.feed('solvent-import',fixture('solvent_receipt_weighed.json'))
        self.assertEqual(self.barrel('R1')['state'],'open')
        self.feed('solvent-import',fixture('solvent_outbound.json'))
        self.assertEqual(self.barrel('R1')['state'],'closed')
        self.assertEqual(self.totals()['transferred_l'],14)
    def test_same_reflux_cannot_be_distilled_and_shipped(self):
        self.base();self.feed('solvent-import',fixture('solvent_distill.json'));self.feed('solvent-import',fixture('solvent_outbound.json'))
        r1=self.barrel('R1')
        self.assertEqual(r1['state'],'conflict')
        self.assertIn('reflux',r1['reason'])
        self.assertEqual(r1['quantities']['out_l'],28)
        self.assertFalse(self.totals()['conserved'])
    def test_late_calibration_recomputes_but_signed_weights_stay(self):
        self.base();self.feed('solvent-import',fixture('solvent_outbound.json'));self.feed('solvent-import',fixture('solvent_receipt_weighed.json'))
        before=self.barrel('B1')
        self.assertEqual((before['quantities']['remaining_l'],before['quantities']['evaporation_l']),(4,2))
        self.feed('solvent-import',fixture('solvent_calibration.json'))
        after=self.barrel('B1')
        self.assertEqual(after['version'],before['version']+1)
        self.assertEqual(after['quantities']['drawn_l'],16.8)
        self.assertEqual(after['quantities']['remaining_l'],3.2)
        self.assertEqual(after['quantities']['evaporation_l'],2.8)
        r1=self.barrel('R1')
        self.assertEqual(r1['quantities']['handed_over_l'],14)
        self.assertEqual(r1['quantities']['transferred_l'],14)
        self.assertEqual(self.db.execute('SELECT sealed_weight_kg FROM solvent_handover').fetchone()[0],22.4)
        self.assertEqual(self.db.execute('SELECT received_weight_kg FROM solvent_waste_receipt').fetchone()[0],22.4)
        self.assertTrue(self.totals()['conserved'])
    def test_unauthorized_operation_still_consumes_solvent(self):
        data=fixture('garment_ok.json')
        data['garments'][0]['id']='coat-2';data['garments'][0]['allowed_process']='wet'
        data['photos'][0]['garment_id']='coat-2'
        self.feed('garment-import',data)
        self.feed('process-import',{'recipes':[{'event_id':'recipe-w','garment_id':'coat-2','version':1,'process':'dry','max_temp':40,'banned_chemicals':[],'approved_by':'owner-1'}],
                                    'operations':[{'event_id':'op-w','garment_id':'coat-2','recipe_id':'recipe-w','kind':'clean','at':'2026-11-02','temp':30,'chemical':'mild','from_shop':'shop-a','to_shop':'shop-a','sender':'worker-a','receiver':'worker-a'}]})
        self.feed('solvent-import',{'barrels':[{'event_id':'barrel-b9','barrel_id':'B9','batch':'b9','chemical':'perc','role':'purchase','liters':20,'weight_kg':32,'density_kg_per_l':1.6,'meter_id':'M0','at':'2026-11-01','supplier':'chem-supply'},
                                               {'event_id':'barrel-r9','barrel_id':'R9','batch':'b9','chemical':'perc','role':'recovery','liters':0,'weight_kg':0,'density_kg_per_l':1.6,'meter_id':'M0','at':'2026-11-01','supplier':'workshop'}],
                                    'draws':[{'event_id':'draw-w','barrel_id':'B9','operation_id':'op-w','liters':8,'meter_id':'M1','at':'2026-11-02','shop':'shop-a'}],
                                    'refluxes':[{'event_id':'reflux-w','barrel_id':'R9','operation_id':'op-w','liters':7,'meter_id':'M2','at':'2026-11-02'}]})
        op=self.operation('op-w')
        self.assertEqual(op['state'],'unauthorized')
        self.assertIn('retained',op['reason'])
        self.assertEqual(self.db.execute("SELECT state FROM process_decision WHERE garment_id='coat-2'").fetchone()[0],'blocked')
        b9=self.barrel('B9')
        self.assertEqual(b9['quantities']['drawn_l'],8)
        self.assertEqual(b9['quantities']['remaining_l'],12)
        totals=self.totals()
        self.assertEqual((totals['purchased_l'],totals['reusable_l'],totals['pending_l'],totals['evaporation_l']),(20,12,7,1))
        self.assertTrue(totals['conserved'])
    def test_cross_shop_operation_keeps_solvent_alignment(self):
        self.base()
        self.feed('process-import',{'operations':[{'event_id':'op-3','garment_id':'coat-1','recipe_id':'recipe-1','kind':'clean','at':'2026-11-04','temp':30,'chemical':'mild','from_shop':'shop-a','to_shop':'shop-b','sender':'worker-a'}]})
        self.feed('solvent-import',{'barrels':[{'event_id':'barrel-b2','barrel_id':'B2','batch':'b2','chemical':'perc','role':'purchase','liters':10,'weight_kg':16,'density_kg_per_l':1.6,'meter_id':'M0','at':'2026-11-04','supplier':'chem-supply'}],
                                    'draws':[{'event_id':'draw-3','barrel_id':'B2','operation_id':'op-3','liters':8,'meter_id':'M1','at':'2026-11-04','shop':'shop-a'}],
                                    'refluxes':[{'event_id':'reflux-3','barrel_id':'R1','operation_id':'op-3','liters':7,'meter_id':'M2','at':'2026-11-04'}]})
        self.assertEqual(self.db.execute("SELECT state FROM process_decision WHERE garment_id='coat-1'").fetchone()[0],'custody_gap')
        op=self.operation('op-3')
        self.assertEqual(op['state'],'balanced')
        self.assertEqual(op['quantities']['evaporation_l'],1)
        b2=self.barrel('B2')
        self.assertEqual((b2['state'],b2['quantities']['drawn_l'],b2['quantities']['remaining_l']),('closed',8,2))
    def test_stock_check_flags_unexplained_loss(self):
        self.base();self.feed('solvent-import',fixture('solvent_stock_short.json'))
        b1=self.barrel('B1')
        self.assertEqual(b1['state'],'short')
        self.assertEqual(b1['quantities']['unexplained_l'],1)
        self.assertEqual(b1['quantities']['reusable_l'],3)
        totals=self.totals()
        self.assertEqual(totals['unexplained_l'],1)
        self.assertTrue(totals['conserved'])
    def test_route_authorization_without_weighing_stays_open(self):
        self.base()
        self.feed('solvent-import',{'routes':[{'event_id':'route-d','route_id':'RD-1','kind':'distill','approved_by':'manager-1','valid_from':'2026-11-01','valid_to':'2026-11-30'}]})
        r1=self.barrel('R1')
        self.assertEqual(r1['state'],'open')
        self.assertEqual(r1['quantities']['pending_l'],14)
        self.assertEqual(self.totals()['reusable_l'],4)
        self.feed('solvent-import',{'barrels':[{'event_id':'barrel-g1','barrel_id':'G1','batch':'batch-2026-11','chemical':'perc','role':'regenerated','liters':0,'weight_kg':0,'density_kg_per_l':1.6,'meter_id':'M0','at':'2026-11-04','supplier':'workshop'}],
                                    'distillations':[{'event_id':'distill-x','route_id':'RD-X','input_barrel_id':'R1','output_barrel_id':'G1','input_liters':14,'reusable_liters':12,'residue_liters':2,'meter_id':'M3','at':'2026-11-04'}]})
        r1=self.barrel('R1')
        self.assertEqual(r1['state'],'open')
        self.assertIn('approved route',r1['reason'])
        self.assertEqual(self.barrel('G1')['quantities']['regenerated_in_l'],0)
        self.assertEqual(self.totals()['reusable_l'],4)
    def test_idempotent_reimport_and_content_conflict(self):
        self.base();self.feed('solvent-import',fixture('solvent_distill.json'))
        version=self.barrel('R1')['version']
        self.feed('solvent-import',fixture('solvent_base.json'));self.feed('solvent-import',fixture('solvent_distill.json'))
        self.assertEqual(self.barrel('R1')['version'],version)
        bad=fixture('solvent_base.json');bad['draws'][0]['liters']=9
        with self.assertRaisesRegex(ValueError,'event_id conflict'):self.feed('solvent-import',bad)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM solvent_draw').fetchone()[0],2)
        self.assertEqual(self.barrel('R1')['version'],version)
    def test_weighed_shortfall_is_unexplained(self):
        self.base();self.feed('solvent-import',fixture('solvent_outbound.json'))
        self.feed('solvent-import',{'waste_receipts':[{'event_id':'receipt-9','handover_id':'handover-1','status':'weighed','received_liters':13,'received_weight_kg':20.8,'at':'2026-11-06','signer':'processor-scale'}]})
        r1=self.barrel('R1')
        self.assertEqual(r1['state'],'short')
        self.assertEqual(r1['quantities']['transferred_l'],13)
        self.assertEqual(r1['quantities']['unexplained_l'],1)
    def test_cli_solvent_import_report_audit(self):
        path=Path(self.tmp.name)/'cli.sqlite'
        for command,name in [('garment-import','garment_ok.json'),('process-import','process_ok.json'),('process-import','solvent_ops.json'),('solvent-import','solvent_base.json'),('solvent-import','solvent_distill.json')]:
            subprocess.run([sys.executable,'-m','garmentcare',command,'--db',str(path),'--file',str(FIX/name)],cwd=ROOT,text=True,capture_output=True,check=True)
        r=subprocess.run([sys.executable,'-m','garmentcare','report','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(r.stdout)['solvent']['totals']['reusable_l'],16)
        r=subprocess.run([sys.executable,'-m','garmentcare','audit','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(r.stdout)['status'],'matched')

if __name__=='__main__':unittest.main()
