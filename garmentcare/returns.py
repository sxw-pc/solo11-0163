import json
from .source import record
from .store import update


def decide(db,garment_id):
    garment=db.execute('SELECT * FROM garment WHERE id=?',(garment_id,)).fetchone()
    if not garment:return None
    intake=db.execute('SELECT * FROM intake_decision WHERE garment_id=?',(garment_id,)).fetchone()
    process=db.execute('SELECT * FROM process_decision WHERE garment_id=?',(garment_id,)).fetchone()
    receipts=db.execute('SELECT * FROM return_receipt WHERE garment_id=? ORDER BY at DESC,event_id',(garment_id,)).fetchall()
    if not receipts:
        state,reason,payable,frozen='pending','return receipt missing',0,0
    else:
        receipt=receipts[0]
        nominal=process['nominal_state'] if process and process['nominal_state'] else (process['state'] if process else None)
        nominal_intake=intake['state'] if intake else None
        authority=db.execute('SELECT * FROM pickup_authority WHERE garment_id=? AND person=? AND valid_to>=? ORDER BY valid_to DESC',
                             (garment_id,receipt['person'],receipt['at'][:10])).fetchone()
        baseline=db.execute('SELECT * FROM intake_photo WHERE event_id=?',(intake['photo_id'],)).fetchone() if intake and intake['photo_id'] else None
        claims=db.execute('SELECT * FROM claim WHERE garment_id=? AND receipt_id=? ORDER BY at,event_id',(garment_id,receipt['event_id'])).fetchall()
        cap=garment['declared_value']*garment['cap_rate']//100
        payable=0
        frozen=0
        if not authority or authority['owner_signer']!=garment['owner'] or receipt['owner_signer']!=receipt['person'] or receipt['bag']!=garment['expected_bag']:
            state,reason='blocked','pickup authority, recipient signature or sealed bag identity invalid'
        elif not intake or nominal_intake!='accepted':
            state,reason='blocked','signed intake baseline unavailable'
        elif not process or nominal not in ('completed','deviation','custody_gap'):
            state,reason='blocked','care process has not reached return stage'
        elif not claims:
            state,reason='returned','no timely compensation claim'
        else:
            timely=[c for c in claims if c['at']<=garment['claim_until'] and c['at']>=receipt['at']]
            if not timely:
                state,reason='late_claim','claim arrived after signed deadline'
            else:
                new=[c for c in timely if c['damage'] not in json.loads(baseline['damage'])]
                if not new:
                    state,reason='preexisting','claimed damage already fixed at intake'
                else:
                    frozen=min(cap,max(c['requested_amount'] for c in new))
                    if nominal in ('deviation','custody_gap'):
                        state,reason='liable','new damage follows documented process or custody deviation'
                        payable=frozen
                        frozen=0
                    else:
                        state,reason='review','new damage requires separate liability review'
    return update(db,'compensation_decision',garment_id,state=state,reason=reason,payable=payable,frozen=frozen,nominal_state=state)


def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('authorities',[]):
            if record(db,row['event_id'],'pickup_authority',row):
                db.execute('INSERT INTO pickup_authority VALUES(?,?,?,?,?)',(row['event_id'],row['garment_id'],row['person'],row['owner_signer'],row['valid_to']))
            touched.add(row['garment_id'])
        for row in data.get('receipts',[]):
            if record(db,row['event_id'],'return_receipt',row):
                db.execute('INSERT INTO return_receipt VALUES(?,?,?,?,?,?,?)',(row['event_id'],row['garment_id'],row['person'],row['at'],row['bag'],row['condition'],row['owner_signer']))
            touched.add(row['garment_id'])
        for row in data.get('claims',[]):
            if record(db,row['event_id'],'claim',row):
                db.execute('INSERT INTO claim VALUES(?,?,?,?,?,?,?)',(row['event_id'],row['garment_id'],row['receipt_id'],row['at'],row['photo_sha'],row['damage'],row['requested_amount']))
            touched.add(row['garment_id'])
        return [decide(db,g) for g in sorted(touched)]
