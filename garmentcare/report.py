import json


def _json_columns(row, keys):
    item = dict(row)
    for key in keys:
        if key in item and item[key]:
            item[key] = json.loads(item[key])
    return item


def report(db):
    garments=[]
    for g in db.execute('SELECT * FROM garment ORDER BY id'):
        item=dict(g)
        for table,key in [('intake_decision','intake'),('process_decision','process'),('compensation_decision','compensation')]:
            row=db.execute(f'SELECT * FROM {table} WHERE garment_id=?',(g['id'],)).fetchone()
            item[key]=dict(row) if row else None
        item['history']=[dict(row) for row in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE garment_id=? ORDER BY id',(g['id'],))]
        garments.append(item)
    investigate = [_json_columns(row, ('evidence', 'affected', 'work_orders'))
                   for row in db.execute('SELECT * FROM energy_investigation ORDER BY site,period')]
    return {'garments':garments,
            'energy_usage':[dict(row) for row in db.execute('SELECT * FROM energy_usage ORDER BY site')],
            'energy_investigation':investigate,
            'work_orders':[dict(row) for row in db.execute('SELECT * FROM work_order ORDER BY order_id')],
            'source_files':[dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}


def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
