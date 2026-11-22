import json
from .source import record
from .store import update


def decide(db,garment_id):
    garment=db.execute('SELECT * FROM garment WHERE id=?',(garment_id,)).fetchone()
    if not garment: return None
    photos=db.execute('SELECT * FROM intake_photo WHERE garment_id=? ORDER BY version DESC,event_id',(garment_id,)).fetchall()
    if not photos:
        state,reason,photo_id='pending','signed intake photo missing',None
    else:
        photo=photos[0]
        same=[p for p in photos if p['version']==photo['version']]
        if len({(p['material'],p['damage'],p['photo_sha']) for p in same})>1:
            state,reason,photo_id='frozen','parallel intake image version conflict',None
        elif photo['material']!=garment['material']:
            state,reason,photo_id='frozen','material label conflicts with signed intake',None
        elif photo['owner_signer']!=garment['owner'] or photo['clerk_signer']==garment['owner']:
            state,reason,photo_id='pending','independent signed baseline missing',None
        elif garment['allowed_process']=='':
            state,reason,photo_id='pending','owner has not authorized a care process',None
        else:
            state,reason,photo_id='accepted','material, baseline and care consent verified',photo['event_id']
    result=update(db,'intake_decision',garment_id,state=state,reason=reason,photo_id=photo_id)
    from .process import decide as process_decide
    process_decide(db,garment_id)
    return result


def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('garments',[]):
            if record(db,row['event_id'],'garment',row):
                db.execute('INSERT INTO garment VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['id'],row['owner'],row['material'],row['accepted_at'],row['declared_value'],row['cap_rate'],row['allowed_process'],row['pickup_person'],row['claim_until'],row['expected_bag']))
            touched.add(row['id'])
        for row in data.get('photos',[]):
            if record(db,row['event_id'],'intake_photo',row):
                db.execute('INSERT INTO intake_photo VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['garment_id'],row['version'],row['photo_sha'],json.dumps(row['damage'],sort_keys=True),row['owner_signer'],row['clerk_signer'],row['material']))
            touched.add(row['garment_id'])
    with db:
        results=[decide(db,g) for g in sorted(touched)]
        from .delivery import refresh
        refresh(db,touched)
    return results
