import json


def _json(text, default):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def report(db):
    garments=[]
    for g in db.execute('SELECT * FROM garment ORDER BY id'):
        item=dict(g)
        for table,key in [('intake_decision','intake'),('process_decision','process'),
                          ('delivery_decision','delivery'),('compensation_decision','compensation')]:
            row=db.execute(f'SELECT * FROM {table} WHERE garment_id=?',(g['id'],)).fetchone()
            item[key]=dict(row) if row else None
        item['history']=[dict(row) for row in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE garment_id=? ORDER BY id',(g['id'],))]
        delivery=item['delivery']
        item['claim_anchor']=None
        if delivery and delivery['receipt_id']:
            anchor=None
            for table in ('order_handoff','return_receipt'):
                anchor=db.execute(f'SELECT event_id,at,bag,person FROM {table} WHERE event_id=?',(delivery['receipt_id'],)).fetchone()
                if anchor:break
            item['claim_anchor']=dict(anchor) if anchor else {'event_id':delivery['receipt_id']}
        garments.append(item)
    orders=[]
    for o in db.execute('SELECT * FROM care_order ORDER BY order_id'):
        item=dict(o)
        decision=db.execute('SELECT * FROM order_decision WHERE order_id=?',(o['order_id'],)).fetchone()
        item['decision']=_order_view(decision)
        item['members']=[dict(row) for row in db.execute(
            'SELECT garment_id FROM order_member WHERE order_id=? ORDER BY garment_id',(o['order_id'],))]
        item['splits']=[dict(row) for row in db.execute(
            'SELECT event_id,garment_id,from_bag,to_bag,new_seal,at FROM order_split WHERE order_id=? ORDER BY at,event_id',(o['order_id'],))]
        item['handoffs']=[dict(row) for row in db.execute(
            'SELECT event_id,bag,seal,person,at,condition FROM order_handoff WHERE order_id=? ORDER BY at,event_id',(o['order_id'],))]
        item['lineage']=[dict(row) for row in db.execute(
            'SELECT bag,garment_id,from_bag,split_event,at FROM bag_membership WHERE order_id=? ORDER BY at,bag,garment_id',(o['order_id'],))]
        item['history']=[dict(row) for row in db.execute(
            'SELECT version,state,reason FROM order_decision_history WHERE order_id=? ORDER BY id',(o['order_id'],))]
        orders.append(item)
    return {'garments':garments,'orders':orders,
            'source_files':[dict(row) for row in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]}


def _order_view(decision):
    if not decision:
        return None
    from .delivery import STATE_LABELS
    view=dict(decision)
    for field in ('delivered','undelivered','frozen','disputes'):
        view[field]=_json(view[field],[] if field!='disputes' else [])
    view['label']=STATE_LABELS.get(view['state'],view['state'])
    return view


def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
