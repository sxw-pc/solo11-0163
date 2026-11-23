import json


def report(db):
    garments=[]
    for g in db.execute('SELECT * FROM garment ORDER BY id'):
        item=dict(g)
        for table,key in [('intake_decision','intake'),('process_decision','process'),('compensation_decision','compensation')]:
            row=db.execute(f'SELECT * FROM {table} WHERE garment_id=?',(g['id'],)).fetchone()
            item[key]=dict(row) if row else None
        settlement=db.execute('SELECT * FROM shop_settlement_decision WHERE garment_id=?',(g['id'],)).fetchone()
        if settlement:
            settlement=dict(settlement)
            settlement.pop('at',None)
            settlement['payload']=json.loads(settlement['payload'])
        item['settlement']=settlement
        item['history']=[dict(row) for row in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE garment_id=? ORDER BY id',(g['id'],))]
        garments.append(item)
    settlements=[]
    for row in db.execute('SELECT * FROM shop_settlement_decision ORDER BY order_id'):
        entry=dict(row)
        entry.pop('at',None)
        entry['payload']=json.loads(entry['payload'])
        settlements.append(entry)
    return {'garments':garments,'settlements':settlements,
            'source_files':[dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}


def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
