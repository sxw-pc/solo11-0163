import json


def report(db):
    garments=[]
    for g in db.execute('SELECT * FROM garment ORDER BY id'):
        item=dict(g)
        for table,key in [('intake_decision','intake'),('process_decision','process'),('compensation_decision','compensation')]:
            row=db.execute(f'SELECT * FROM {table} WHERE garment_id=?',(g['id'],)).fetchone()
            item[key]=dict(row) if row else None
        item['history']=[dict(row) for row in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE garment_id=? ORDER BY id',(g['id'],))]
        garments.append(item)
    out={'garments':garments,'alterations':[dict(row) for row in db.execute('SELECT * FROM alteration_decision ORDER BY garment_id')],
         'source_files':[dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
         'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hem_baseline'").fetchone():
        out['rework']={
            'decisions':[dict(row) for row in db.execute('SELECT * FROM rework_decision ORDER BY garment_id')],
            'fabric_ledger':[dict(row) for row in db.execute('SELECT * FROM fabric_ledger ORDER BY bolt_id')],
            'cuts':[dict(row) for row in db.execute('SELECT event_id,garment_id,bolt_id,template_id,x,y,area,at FROM cut_receipt ORDER BY at,event_id')],
            'cost_lines':[dict(row) for row in db.execute('SELECT * FROM rework_cost_line ORDER BY garment_id,event_id')],
            'remeasures':[dict(row) for row in db.execute('SELECT event_id,garment_id,at,phase,measured_hem,valid,reason FROM remeasure ORDER BY at,event_id')]}
    return out


def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
