import json


def _solvent(db):
    barrels=[]
    for row in db.execute('SELECT * FROM solvent_decision ORDER BY barrel_id'):
        item=dict(row)
        item['quantities']=json.loads(item['quantities'])
        item['history']=[dict(h) for h in db.execute("SELECT version,state,reason FROM decision_history WHERE subsystem='solvent_decision' AND garment_id=? ORDER BY id",(row['barrel_id'],))]
        barrels.append(item)
    operations=[]
    for row in db.execute('SELECT * FROM solvent_operation ORDER BY operation_id'):
        item=dict(row)
        item['quantities']=json.loads(item['quantities'])
        item['history']=[dict(h) for h in db.execute("SELECT version,state,reason FROM decision_history WHERE subsystem='solvent_operation' AND garment_id=? ORDER BY id",(row['operation_id'],))]
        operations.append(item)
    totals={key:0.0 for key in ('purchased_l','reusable_l','pending_l','waste_on_hand_l','in_transit_l','transferred_l','evaporation_l','unexplained_l')}
    for barrel in barrels:
        q=barrel['quantities']
        totals['purchased_l']+=q['purchased_l']
        totals['reusable_l']+=q['reusable_l']
        totals['pending_l']+=q['pending_l']
        totals['waste_on_hand_l']+=q['residue_l']
        totals['in_transit_l']+=q['in_transit_l']
        totals['transferred_l']+=q['transferred_l']
        totals['evaporation_l']+=q['evaporation_l']
        totals['unexplained_l']+=q['unexplained_l']
    totals={key:round(value,3) for key,value in totals.items()}
    accounted=sum(totals[key] for key in ('reusable_l','pending_l','waste_on_hand_l','in_transit_l','transferred_l','evaporation_l','unexplained_l'))
    totals['conserved']=abs(totals['purchased_l']-accounted)<=0.001
    return {'barrels':barrels,'operations':operations,'totals':totals}


def report(db):
    garments=[]
    for g in db.execute('SELECT * FROM garment ORDER BY id'):
        item=dict(g)
        for table,key in [('intake_decision','intake'),('process_decision','process'),('compensation_decision','compensation')]:
            row=db.execute(f'SELECT * FROM {table} WHERE garment_id=?',(g['id'],)).fetchone()
            item[key]=dict(row) if row else None
        item['history']=[dict(row) for row in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE garment_id=? ORDER BY id',(g['id'],))]
        garments.append(item)
    return {'garments':garments,'solvent':_solvent(db),'source_files':[dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}


def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
