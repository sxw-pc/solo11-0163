import json
from .source import record
from .store import update


def authority_ok(db,garment,person,at,owner_signer):
    authority=db.execute('SELECT * FROM pickup_authority WHERE garment_id=? AND person=? AND valid_to>=? ORDER BY valid_to DESC',
                         (garment['id'],person,at[:10])).fetchone()
    return bool(authority and authority['owner_signer']==garment['owner'] and owner_signer==person)


def _bag_at(db,garment_id,at):
    """Bag identity at a moment, following the order split timeline when there is one."""
    from .delivery import bag_for
    ordered=bag_for(db,garment_id,at)
    return ordered


def _receipt_for(db,garment,receipts,delivery):
    """The receipt of record for one garment. A signed order handoff is itself the
    signature evidence; a bagmate's receipt is never substituted for it."""
    if delivery and delivery['state'] in ('delivered','dispute') and delivery['receipt_id']:
        handoff=db.execute('SELECT * FROM order_handoff WHERE event_id=?',(delivery['receipt_id'],)).fetchone()
        if handoff:
            return {'event_id':handoff['event_id'],'person':handoff['person'],'at':handoff['at'],
                    'bag':handoff['bag'],'condition':handoff['condition'],'owner_signer':handoff['owner_signer']}
    if delivery and delivery['state']=='delivered' and delivery['bag']:
        matching=[r for r in receipts if r['bag']==delivery['bag']]
        if matching:return matching[0]
    return receipts[0] if receipts else None


def _order_gate(db,garment_id):
    delivery=db.execute('SELECT * FROM delivery_decision WHERE garment_id=?',(garment_id,)).fetchone()
    if not delivery:return None
    if delivery['claim_frozen']:
        return 'frozen','signed handoff preserved; late intake conflict freezes compensation'
    order=db.execute('SELECT * FROM order_decision WHERE order_id=?',(delivery['order_id'],)).fetchone()
    if order and order['state']=='handoff_dispute':
        mine=[d for d in json.loads(order['disputes'])
              if d['garment_id']==garment_id and d['kind'] in ('duplicate_delivery','unauthorized_recipient')]
        if mine:
            return 'frozen','conflicting signed handoffs block claim attribution'
    return None


def decide(db,garment_id):
    garment=db.execute('SELECT * FROM garment WHERE id=?',(garment_id,)).fetchone()
    if not garment:return None
    intake=db.execute('SELECT * FROM intake_decision WHERE garment_id=?',(garment_id,)).fetchone()
    process=db.execute('SELECT * FROM process_decision WHERE garment_id=?',(garment_id,)).fetchone()
    delivery=db.execute('SELECT * FROM delivery_decision WHERE garment_id=?',(garment_id,)).fetchone()
    receipts=db.execute('SELECT * FROM return_receipt WHERE garment_id=? ORDER BY at DESC,event_id',(garment_id,)).fetchall()
    receipt=_receipt_for(db,garment,receipts,delivery)
    if not receipt:
        state,reason,payable,frozen='pending','return receipt missing',0,0
    else:
        baseline=db.execute('SELECT * FROM intake_photo WHERE event_id=?',(intake['photo_id'],)).fetchone() if intake and intake['photo_id'] else None
        claims=db.execute('SELECT * FROM claim WHERE garment_id=? AND receipt_id=? ORDER BY at,event_id',(garment_id,receipt['event_id'])).fetchall()
        cap=garment['declared_value']*garment['cap_rate']//100
        payable=0
        frozen=0
        expected=_bag_at(db,garment_id,receipt['at']) or garment['expected_bag']
        gate=_order_gate(db,garment_id)
        if gate:
            state,reason=gate
            frozen=cap
        elif not authority_ok(db,garment,receipt['person'],receipt['at'],receipt['owner_signer']) or receipt['bag']!=expected:
            state,reason='blocked','pickup authority, recipient signature or sealed bag identity invalid'
        elif not intake or intake['state']!='accepted':
            state,reason='blocked','signed intake baseline unavailable'
        elif not process or process['state'] not in ('completed','deviation','custody_gap'):
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
                    if process['state'] in ('deviation','custody_gap'):
                        state,reason='liable','new damage follows documented process or custody deviation'
                        payable=frozen
                        frozen=0
                    else:
                        state,reason='review','new damage requires separate liability review'
    return update(db,'compensation_decision',garment_id,state=state,reason=reason,payable=payable,frozen=frozen)


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
    with db:
        from .delivery import refresh
        refresh(db,touched)
        results=[decide(db,g) for g in sorted(touched)]
    return results
