import json
from .source import record
from .store import update


def decide(db,garment_id):
    intake=db.execute('SELECT * FROM intake_decision WHERE garment_id=?',(garment_id,)).fetchone()
    garment=db.execute('SELECT * FROM garment WHERE id=?',(garment_id,)).fetchone()
    recipes=db.execute('SELECT * FROM recipe WHERE garment_id=? ORDER BY version DESC,event_id',(garment_id,)).fetchall()
    if not recipes:
        state,reason,recipe_id='pending','approved recipe missing',None
    else:
        recipe=recipes[0]
        top=[r for r in recipes if r['version']==recipe['version']]
        operations=db.execute('SELECT * FROM operation WHERE garment_id=? AND recipe_id=? ORDER BY at,event_id',(garment_id,recipe['event_id'])).fetchall()
        if not intake or intake['state']!='accepted':
            state,reason,recipe_id='blocked','intake baseline not accepted',None
        elif len({(r['process'],r['max_temp'],r['banned_chemicals']) for r in top})>1:
            state,reason,recipe_id='frozen','parallel recipe version conflict',None
        elif recipe['approved_by']!=garment['owner'] or recipe['process']!=garment['allowed_process']:
            state,reason,recipe_id='blocked','recipe not approved under intake consent',None
        elif not operations:
            state,reason,recipe_id='scheduled','approved recipe awaits execution',recipe['event_id']
        elif any(op['temp']>recipe['max_temp'] for op in operations):
            state,reason,recipe_id='deviation','processing temperature exceeded approved limit',recipe['event_id']
        elif any(op['chemical'] in json.loads(recipe['banned_chemicals']) for op in operations):
            state,reason,recipe_id='deviation','banned chemical used on material',recipe['event_id']
        elif any(op['from_shop']!=op['to_shop'] and (not op['sender'] or not op['receiver'] or op['sender']==op['receiver']) for op in operations):
            state,reason,recipe_id='custody_gap','cross-shop transfer lacks independent handoff',recipe['event_id']
        else:
            state,reason,recipe_id='completed','temperature, chemicals and custody verified',recipe['event_id']
    result=update(db,'process_decision',garment_id,state=state,reason=reason,recipe_id=recipe_id)
    from .returns import decide as return_decide
    return_decide(db,garment_id)
    return result


def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('recipes',[]):
            if record(db,row['event_id'],'recipe',row):
                db.execute('INSERT INTO recipe VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'],row['garment_id'],row['version'],row['process'],row['max_temp'],json.dumps(row.get('banned_chemicals',[]),sort_keys=True),row['approved_by']))
            touched.add(row['garment_id'])
        for row in data.get('operations',[]):
            if record(db,row['event_id'],'operation',row):
                db.execute('INSERT INTO operation VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['garment_id'],row['recipe_id'],row['kind'],row['at'],row['temp'],row['chemical'],row['from_shop'],row['to_shop'],row.get('sender'),row.get('receiver')))
            touched.add(row['garment_id'])
    with db:
        results=[decide(db,g) for g in sorted(touched)]
        from .delivery import refresh
        refresh(db,touched)
    return results
